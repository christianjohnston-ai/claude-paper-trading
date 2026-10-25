"""Simulated broker: settles pending orders, corporate actions and exit rules, session by session.

THIS IS THE ONLY EXECUTION PATH AND IT IS LOCAL. It reads stored daily bars from SQLite and writes
simulated fills to the ledger. It has no network access, no brokerage client and no credentials;
tests/test_no_live_broker.py asserts that.

Execution policy (README "When do fills happen?"):
- Order type: market-on-open (MOO) for the first regular session whose open is strictly after the
  decision instant (plus a small safety buffer, see submission). Daily bars are the only price data,
  so we can never execute inside a bar we have already observed.
- Fill evidence: that session's OPEN from its first-observed final daily bar. The bar only exists
  after the session closed, so every fill is recorded LATER than it is simulated to happen; the fill
  row keeps decided_at < sim_exec_at < bar_retrieved_at <= recorded_at (schema CHECKs).
- Price: BUY at open*(1+slippage) rounded up, SELL at open*(1-slippage) rounded down. Gaps are
  realized as they happened because the open is used, not any rule level.
- BUY quantity = floor((notional - commission budget) / fill price) at 6 dp, so cash spent never
  exceeds the reserved notional. SELL ALL sells the quantity held at the fill instant.
- Expiry: an MOO order is good for its target session only. No bar for the symbol on a session
  where the rest of the universe traded -> EXPIRED (halt/delisting/data gap). No data at all for a
  completed session -> left PENDING (feed outage) until session_data_wait_days have passed, then
  EXPIRED with session_data_unavailable_timeout.
- Within one session: splits, then dividends (holders of record at the prior close), then fills
  (all SELLs before BUYs), then exit-rule evaluation on the close, then valuation snapshots.
- Sessions are processed strictly in order, each in its own transaction, so a crash leaves every
  session either fully applied or untouched (restart-safe and idempotent).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from . import accounting, clock, config, corporate_actions, db, experiments, ledger, market_calendar as cal, orders, prices, rules, universe, valuation
from . import money as m


@dataclass
class SettlementReport:
    sessions_processed: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    expired: list = field(default_factory=list)
    rule_orders: list = field(default_factory=list)
    corporate_actions: list = field(default_factory=list)
    snapshots: list = field(default_factory=list)
    blocked_session: str | None = None
    blocked_reason: str | None = None
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _settled_key(exp_id: int) -> str:
    return f"settled_through:{exp_id}"


def settled_through(conn, exp_id: int) -> str | None:
    return db.get_state(conn, _settled_key(exp_id))


def settle(conn, experiment_id: int, now, run_id: int | None = None) -> SettlementReport:
    rep = SettlementReport()
    _, cfg = config.current(conn, experiment_id, clock.iso(now))
    exp = experiments.get(conn, experiment_id)
    last = cal.last_completed_session(now, cfg.bar_finalization_minutes)
    if last is None:
        return rep
    done = settled_through(conn, experiment_id)
    first = date.fromisoformat(exp["benchmark_start_session"]) if done is None else cal.following_session(date.fromisoformat(done)).date
    if first > last.date:
        return rep
    universe_syms = universe.symbols(conn)
    for s in cal.sessions_between(first, last.date):
        with db.transaction(conn):
            ok = _process_session(conn, exp, s, now, cfg, universe_syms, rep, run_id)
        if not ok:
            break
    return rep


def _process_session(conn, exp, s, now, cfg, universe_syms, rep: SettlementReport, run_id) -> bool:
    exp_id = exp["id"]
    sd = s.date.isoformat()
    now_iso = clock.iso(now)
    pending_here = [o for o in orders.pending(conn, exp_id) if o["target_session_date"] <= sd]
    if not prices.session_has_data(conn, sd, universe_syms):
        deadline = s.close_utc + timedelta(days=cfg.session_data_wait_days)
        if now < deadline:
            rep.blocked_session, rep.blocked_reason = sd, "session_data_unavailable"
            return False
        for o in pending_here:
            _expire(conn, o, "session_data_unavailable_timeout", now_iso, s)
            rep.expired.append(o["id"])
        rep.warnings.append(f"{sd}: no market data obtained within {cfg.session_data_wait_days} days; orders expired, "
                            "valuation uses earlier closes and is provisional")
        _snapshots(conn, exp_id, s, now_iso, run_id, rep, extra_reason="no_data_for_session")
        db.set_state(conn, _settled_key(exp_id), sd)
        rep.sessions_processed.append(sd)
        return True

    # 1-2. Corporate actions effective at this session's open, for positions carried from the prior close.
    for ca in corporate_actions.on_date(conn, sd):
        for account in ("PORTFOLIO", "BENCHMARK"):
            st = accounting.load_state(conn, exp_id, account)
            held = st.positions.get(ca["symbol"], 0)
            if held <= 0:
                continue
            if ca["action_type"] == "SPLIT":
                num, den = int(ca["ratio_num"]), int(ca["ratio_den"])
                new_total = sum(m.split_quantity(l.qty_micro, num, den) for l in st.lots.get(ca["symbol"], []))
                ledger.append_event(conn, exp_id, account, "SPLIT", effective_at=clock.iso(s.open_utc), recorded_at=now_iso,
                                    session_date=sd, symbol=ca["symbol"], quantity_delta_micro=new_total - held,
                                    split_num=num, split_den=den, corporate_action_id=ca["id"],
                                    idempotency_key=f"split:{exp_id}:{account}:{ca['id']}", memo=f"{num}:{den} split", run_id=run_id)
                if account == "PORTFOLIO":
                    rules.on_split(conn, exp_id, ca["symbol"], num, den, now_iso)
                    for o in orders.pending(conn, exp_id, account="PORTFOLIO", symbol=ca["symbol"]):
                        if o["side"] == "SELL" and not o["sell_all"]:
                            orders.set_quantity(conn, o["id"], m.split_quantity(int(o["quantity_micro"]), num, den),
                                                f"adjusted for {num}:{den} split effective {sd}", now_iso)
                rep.corporate_actions.append(f"{account} {ca['symbol']} split {num}:{den} on {sd}")
            else:
                amount = Decimal(ca["amount"])
                cash = m.dividend_cents(held, amount)
                if cash > 0:
                    ledger.append_event(conn, exp_id, account, "CASH_DIVIDEND", effective_at=clock.iso(s.open_utc), recorded_at=now_iso,
                                        session_date=sd, symbol=ca["symbol"], cash_delta_cents=cash, price=str(amount),
                                        corporate_action_id=ca["id"], idempotency_key=f"div:{exp_id}:{account}:{ca['id']}",
                                        memo=f"cash dividend {amount}/share on {m.qty_to_str(held)} shares (ex {sd})", run_id=run_id)
                    rep.corporate_actions.append(f"{account} {ca['symbol']} dividend {m.fmt_usd(cash)} ex {sd}")

    # 3. Fills at the open: SELLs first (they free cash), then BUYs, oldest first.
    todo = sorted(pending_here, key=lambda o: (o["account"], 0 if o["side"] == "SELL" else 1, o["id"]))
    for o in todo:
        o = orders.get(conn, o["id"])  # re-read: a split may have adjusted the quantity
        if o["target_session_date"] < sd:
            _expire(conn, o, "missed_target_session", now_iso, s)
            rep.expired.append(o["id"])
            continue
        bar = prices.bar_for(conn, o["symbol"], sd)
        if bar is None:
            _expire(conn, o, "no_bar_for_target_session", now_iso, s)
            rep.expired.append(o["id"])
            continue
        fid = _fill(conn, exp_id, o, bar, s, cfg, now_iso, run_id, rep)
        if fid is not None:
            rep.fills.append(fid)

    # 4. Exit rules on this session's completed close.
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    for sym in sorted(st.positions):
        bar = prices.bar_for(conn, sym, sd)
        active = [r for r in rules.active_for(conn, exp_id, sym) if r["status"] == "ACTIVE"]
        if not active:
            continue
        if bar is None:
            rep.warnings.append(f"{sd}: no close for {sym}; exit rules not evaluated")
            continue
        held_now = st.positions[sym]
        # Only a pending FULL exit makes the rule redundant. A partial advisor SELL must not switch
        # the stop off for the rest of the position: if the rule fires, its SELL ALL is queued too
        # and, filling after the partial (same open, older id first), sells whatever remains.
        if any(o["side"] == "SELL" and (o["sell_all"] or int(o["quantity_micro"] or 0) >= held_now)
               for o in orders.pending(conn, exp_id, account="PORTFOLIO", symbol=sym)):
            continue
        rule = active[0]
        hits = rules.evaluate(rule, Decimal(bar["close"]), sd)
        if not hits:
            continue
        nxt = cal.following_session(s.date)
        reason = f"{hits[0]}: close {bar['close']} on {sd} vs rule [{rules.describe(rule)}]"
        oid = orders.create(conn, experiment_id=exp_id, account="PORTFOLIO", origin="RULE", symbol=sym, side="SELL",
                            sell_all=True, decided_at=clock.iso(s.close_utc), target_session_date=nxt.date.isoformat(),
                            client_order_id=f"rule:{rule['id']}:{sd}", now=now_iso, rule_id=rule["id"],
                            decision_id=rule["decision_id"], status="PENDING", status_reason=reason, origin_note=reason)
        conn.execute("UPDATE position_rules SET status='TRIGGERED', status_reason=?, updated_at=? WHERE id=?", (reason, now_iso, rule["id"]))
        rep.rule_orders.append(oid)

    # 5. End-of-session valuations for both accounts.
    _snapshots(conn, exp_id, s, now_iso, run_id, rep)
    db.set_state(conn, _settled_key(exp_id), sd)
    rep.sessions_processed.append(sd)
    return True


def _snapshots(conn, exp_id, s, now_iso, run_id, rep, extra_reason=None):
    for account in ("PORTFOLIO", "BENCHMARK"):
        sid = valuation.snapshot(conn, exp_id, account, s, now_iso, run_id, extra_reason=extra_reason)
        if sid is not None:
            rep.snapshots.append(f"{account} {s.date.isoformat()}")


def _expire(conn, o, reason, now_iso, s):
    orders.transition(conn, o["id"], "EXPIRED", reason, now_iso, effective_at=clock.iso(s.close_utc))
    if o["origin"] == "ADVISOR" and o["side"] == "BUY":
        rules.on_entry_not_filled(conn, o["decision_id"], f"entry order expired: {reason}", now_iso)
    if o["origin"] == "RULE":
        rules.on_exit_expired(conn, o["rule_id"], now_iso)
    if o["origin"] == "BENCHMARK" and o["side"] == "BUY":
        _requeue_benchmark(conn, o, reason, now_iso, s)


def _requeue_benchmark(conn, o, reason, now_iso, s):
    """The passive benchmark's rule ("invest all its cash at the first open after inception") was committed at
    inception, so when an open cannot fill it, the order moves to the next open and keeps the ORIGINAL decision
    instant: no lookahead, because the rule was fixed before either open. Expiring it for good left the benchmark
    in cash forever, which silently turned every comparison into advisor-vs-cash.
    Idempotent per target session (client_order_id), and it repeats each session until an open fills it.
    SYNC WARNING: experiments._create_tx creates the original order (bench:<exp>:initial). Anything that shows
    when the benchmark bought must read its actual fill (or pending order), not experiments.benchmark_start_session."""
    nxt = cal.following_session(s.date)
    orders.create(conn, experiment_id=o["experiment_id"], account="BENCHMARK", origin="BENCHMARK", symbol=o["symbol"],
                  side="BUY", notional_cents=o["notional_cents"], decided_at=o["decided_at"],
                  target_session_date=nxt.date.isoformat(), client_order_id=f"bench:{o['experiment_id']}:retry:{nxt.date.isoformat()}",
                  now=now_iso, reserved_cash_cents=o["reserved_cash_cents"], config_version_id=o["config_version_id"],
                  status="PENDING", origin_note=o["origin_note"],
                  status_reason=f"passive benchmark: re-queued for the next open because the {s.date.isoformat()} open could not fill it ({reason})")


def _fill(conn, exp_id, o, bar, s, cfg, now_iso, run_id, rep) -> int | None:
    account = o["account"]
    sd = s.date.isoformat()
    open_px = Decimal(bar["open"])
    st = accounting.load_state(conn, exp_id, account)
    sim_exec = clock.iso(s.open_utc)
    if o["side"] == "BUY":
        price = m.buy_fill_price(open_px, cfg.slippage_bps)
        budget = int(o["notional_cents"]) - cfg.commission_per_order_cents - m.commission_cents(int(o["notional_cents"]), 0, cfg.commission_bps)
        qty = m.buy_quantity(max(budget, 0), price)
        gross = m.buy_cost_cents(qty, price)
        commission = m.commission_cents(gross, cfg.commission_per_order_cents, cfg.commission_bps) if qty > 0 else 0
        if qty <= 0 or gross < cfg.min_order_cents and account == "PORTFOLIO":
            _expire(conn, o, "below_minimum_at_fill", now_iso, s)
            rep.expired.append(o["id"])
            return None
        if gross + commission > int(o["notional_cents"]) or gross + commission > st.cash_cents:
            # Cannot happen if reservations are honored; refuse rather than overdraw.
            orders.transition(conn, o["id"], "CANCELED", "insufficient_cash_at_fill", now_iso, effective_at=sim_exec)
            rep.warnings.append(f"order {o['id']} canceled: insufficient cash at fill")
            return None
        net = -(gross + commission)
    else:
        held = st.positions.get(o["symbol"], 0)
        if held <= 0:
            _expire(conn, o, "no_position_at_fill", now_iso, s)
            rep.expired.append(o["id"])
            return None
        qty = held if o["sell_all"] else min(int(o["quantity_micro"]), held)
        price = m.sell_fill_price(open_px, cfg.slippage_bps)
        gross = m.sell_proceeds_cents(qty, price)
        commission = min(m.commission_cents(gross, cfg.commission_per_order_cents, cfg.commission_bps), gross)
        net = gross - commission
    cur = conn.execute(
        "INSERT INTO fills(order_id, experiment_id, account, symbol, side, session_date, bar_id, reference_open, slippage_bps,"
        " fill_price, quantity_micro, gross_cents, commission_cents, net_cash_cents, decided_at, sim_exec_at, bar_retrieved_at,"
        " recorded_at, run_id, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o["id"], exp_id, account, o["symbol"], o["side"], sd, bar["id"], str(open_px), str(cfg.slippage_bps), str(price), qty,
         gross, commission, net, o["decided_at"], sim_exec, bar["retrieved_at"], now_iso, run_id,
         "simulated market-on-open fill from the official daily bar; recorded after the session closed"))
    fill_id = int(cur.lastrowid)
    if o["side"] == "BUY":
        ledger.append_event(conn, exp_id, account, "BUY_FILL", effective_at=sim_exec, recorded_at=now_iso, session_date=sd,
                            symbol=o["symbol"], quantity_delta_micro=qty, cash_delta_cents=net, fee_cents=commission,
                            price=str(price), fill_id=fill_id, order_id=o["id"], idempotency_key=f"fill:{o['id']}", run_id=run_id)
    else:
        ledger.append_event(conn, exp_id, account, "SELL_FILL", effective_at=sim_exec, recorded_at=now_iso, session_date=sd,
                            symbol=o["symbol"], quantity_delta_micro=-qty, cash_delta_cents=net, fee_cents=commission,
                            price=str(price), fill_id=fill_id, order_id=o["id"], idempotency_key=f"fill:{o['id']}", run_id=run_id)
    orders.transition(conn, o["id"], "FILLED", f"filled {m.qty_to_str(qty)} @ {price} at {sd} open", now_iso, effective_at=sim_exec,
                      details={"fill_id": fill_id})
    if account == "PORTFOLIO":
        after = accounting.load_state(conn, exp_id, "PORTFOLIO")
        if o["side"] == "BUY":
            rules.on_buy_fill(conn, exp_id, o["symbol"], o["decision_id"], after.avg_cost(o["symbol"]), sd, now_iso)
        elif o["symbol"] not in after.positions:
            rules.on_position_closed(conn, exp_id, o["symbol"], now_iso)
    return fill_id
