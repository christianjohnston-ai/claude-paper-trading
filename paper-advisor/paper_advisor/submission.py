"""The deterministic gate between the advisor's proposals and the simulated broker.

apply_decisions() validates a whole batch of decisions and persists, in ONE transaction, every
decision, every risk-check result (passed or failed, with observed and limit values) and every
resulting order. Nothing here can fill an order: execution only happens later in broker_sim, at
the open of a session that starts strictly after this transaction's decision time. That ordering is
how the brief's "persist the decision and evidence BEFORE any eligible simulated execution" holds.

Limits are evaluated cumulatively across the batch AND everything already pending:
  equity_ref        = cash + sum(held qty * last completed close)   (positions without a close at cost)
  position cap      = held value + pending buys + batch buys + this buy <= max_position_pct * equity_ref
  cash reserve      = cash - pending reservations - batch reservations - this buy >= min_cash_reserve_pct * equity_ref
  open positions    = |held U pending-buy symbols U batch-buy symbols U {this}| <= max_open_positions
                      (a pending exit does NOT free a slot until it fills: it may not fill)
  orders/session    = advisor orders already placed for the target session + batch + 1 <= max
Pending SELL proceeds are never counted as available cash (they may not fill).

Processing order inside a batch is CANCEL, SELL, HOLD/NO_TRADE, BUY (risk-reducing first), each
group in submission order. Results are returned in submission order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from . import accounting, clock, config, db, market_calendar as cal, orders, prices, quality, rules, universe
from . import money as m

ACTIONS = ("BUY", "SELL", "HOLD", "NO_TRADE", "CANCEL")
GROUP = {"CANCEL": 0, "SELL": 1, "HOLD": 2, "NO_TRADE": 2, "BUY": 3}


@dataclass
class DecisionOutcome:
    seq: int
    decision_id: int
    action: str
    symbol: str | None
    status: str  # ACCEPTED | REJECTED | RECORDED
    reasons: list = field(default_factory=list)
    order_id: int | None = None
    target_session: str | None = None
    checks: list = field(default_factory=list)


class SubmissionError(ValueError):
    pass


def _dec_or_none(v):
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {v!r}") from exc
    if not d.is_finite():
        raise ValueError(f"not finite: {v!r}")
    return d


def _parse_exit_rules(er, cfg):
    """Returns (stop, tp, max_hold, invalidation, problems)."""
    if not er:
        return None, None, None, "", []
    problems = []
    try:
        stop = _dec_or_none(er.get("stop_loss_pct"))
        tp = _dec_or_none(er.get("take_profit_pct"))
    except ValueError:
        return None, None, None, "", ["invalid_exit_rule"]
    mh = er.get("max_holding_sessions")
    if mh is not None:
        try:
            mh = int(mh)
        except (TypeError, ValueError):
            problems.append("invalid_exit_rule")
            mh = None
    if stop is not None and not (Decimal(0) < stop <= cfg.max_stop_loss_pct):
        problems.append("invalid_exit_rule")
    if tp is not None and not (Decimal(0) < tp <= Decimal(10)):
        problems.append("invalid_exit_rule")
    if mh is not None and not (1 <= mh <= 1260):
        problems.append("invalid_exit_rule")
    return stop, tp, mh, str(er.get("invalidation") or "").strip(), sorted(set(problems))


def apply_decisions(conn, experiment_id: int, run_id: int, decisions: list[dict], now) -> list[DecisionOutcome]:
    with db.transaction(conn):
        return apply_decisions_tx(conn, experiment_id, run_id, decisions, now)


def apply_decisions_tx(conn, experiment_id: int, run_id: int, decisions: list[dict], now) -> list[DecisionOutcome]:
    if not conn.in_transaction:
        raise SubmissionError("apply_decisions_tx requires an open transaction")
    now_iso = clock.iso(now)
    cfg_vid, cfg = config.current(conn, experiment_id, now_iso)
    state = accounting.load_state(conn, experiment_id, "PORTFOLIO")
    batch_symbols = {str(d.get("symbol") or "").strip().upper() for d in decisions if d.get("symbol")}
    syms = set(universe.symbols(conn)) | set(state.positions) | batch_symbols
    dq = quality.assess(conn, sorted(s for s in syms if s), now, cfg.bar_finalization_minutes)

    last_close: dict[str, Decimal] = {}
    for sym in state.positions:
        b = prices.latest_bar(conn, sym)
        if b is not None:
            last_close[sym] = Decimal(b["close"])
    val = accounting.valuation(state, last_close)
    equity_ref = val.equity_cents
    held_value = {p.symbol: p.market_value_cents for p in val.positions}

    target = cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds))
    target_date = target.date.isoformat()

    pending_all = orders.pending(conn, experiment_id, account="PORTFOLIO")
    reserved = orders.reserved_cash_cents(conn, experiment_id)
    pending_buy_value: dict[str, int] = {}
    pending_exit: set[str] = set()
    pending_sell_qty: dict[str, int] = {}
    for o in pending_all:
        if o["side"] == "BUY":
            pending_buy_value[o["symbol"]] = pending_buy_value.get(o["symbol"], 0) + int(o["notional_cents"])
        else:
            pending_exit.add(o["symbol"])
            held = state.positions.get(o["symbol"], 0)
            pending_sell_qty[o["symbol"]] = pending_sell_qty.get(o["symbol"], 0) + (held if o["sell_all"] else int(o["quantity_micro"]))
    existing_orders = orders.advisor_orders_for_session(conn, experiment_id, target_date)

    ctx = {"batch_reserved": 0, "batch_orders": 0, "batch_buy_value": {}, "batch_symbols": set(), "batch_exit": set(),
           "canceled": set()}
    outcomes: dict[int, DecisionOutcome] = {}
    indexed = list(enumerate(decisions, start=1))
    indexed.sort(key=lambda t: (GROUP.get(str(t[1].get("action", "")).upper(), 9), t[0]))

    for seq, d in indexed:
        action = str(d.get("action", "")).upper()
        symbol = str(d.get("symbol") or "").strip().upper() or None
        checks: list[dict] = []

        def check(name, passed, observed=None, limit=None, message=""):
            checks.append({"check": name, "passed": bool(passed), "observed": None if observed is None else str(observed),
                           "limit": None if limit is None else str(limit), "message": message})
            return passed

        notional = qty = None
        sell_all = False
        parse_problems = []
        if action == "BUY":
            try:
                notional = m.parse_usd(d.get("notional_usd"))
                if notional == 0:
                    raise ValueError("zero")
            except (ValueError, TypeError):
                parse_problems.append("invalid_amount")
        elif action == "SELL":
            q = d.get("quantity")
            if str(q).strip().upper() == "ALL":
                sell_all = True
            else:
                try:
                    qty = m.parse_qty(q)
                except (ValueError, TypeError):
                    parse_problems.append("invalid_quantity")
        stop, tp, mh, invalidation, rule_problems = _parse_exit_rules(d.get("exit_rules"), cfg)

        cur = conn.execute(
            "INSERT INTO decisions(run_id, experiment_id, seq, action, symbol, notional_cents, quantity_micro, sell_all, cancel_order_id,"
            " thesis, counterargument, horizon_sessions, horizon_text, stop_loss_pct, take_profit_pct, max_holding_sessions,"
            " invalidation, confidence, uncertainty, key_risks, skills_used_json, evidence_ids_json, decided_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, experiment_id, seq, action if action in ACTIONS else "NO_TRADE", symbol, notional, qty, int(sell_all),
             d.get("cancel_order_id"), str(d.get("thesis") or ""), str(d.get("counterargument") or ""),
             d.get("horizon_sessions"), str(d.get("horizon_text") or ""), None if stop is None else str(stop),
             None if tp is None else str(tp), mh, invalidation, None if d.get("confidence") is None else str(d.get("confidence")),
             str(d.get("uncertainty") or ""), str(d.get("key_risks") or ""), json.dumps(d.get("skills_used") or []),
             json.dumps(d.get("evidence_ids") or []), now_iso))
        decision_id = int(cur.lastrowid)
        out = DecisionOutcome(seq, decision_id, action, symbol, "RECORDED", [], None, None, checks)
        outcomes[seq] = out

        if action not in ACTIONS:
            check("action_allowed", False, action, "/".join(ACTIONS), "unknown action")
        elif action == "NO_TRADE":
            check("action_allowed", True, action, None, "no order")
        elif action == "CANCEL":
            _cancel(conn, experiment_id, d, now, now_iso, cfg, check, ctx, decision_id)
        elif action == "HOLD":
            if check("no_position", symbol in state.positions, m.qty_to_str(state.positions.get(symbol, 0)), "> 0",
                     "HOLD applies to a held position"):
                # One decision per symbol per batch: a HOLD next to a SELL/BUY of the same symbol is
                # contradictory, and processing order (SELL before HOLD) must not let a HOLD restate
                # rules for a position the same batch is exiting.
                check("duplicate_symbol_in_batch", symbol not in ctx["batch_symbols"] and symbol not in ctx["batch_exit"],
                      symbol, None, "one decision per symbol per batch")
                if d.get("exit_rules"):
                    _hold_restatement_checks(conn, experiment_id, symbol, stop, tp, mh, invalidation, rule_problems,
                                             pending_exit, cfg, check)
                if all(c["passed"] for c in checks):
                    ctx["batch_symbols"].add(symbol)
                    if d.get("exit_rules"):
                        rules.replace_from_hold(conn, experiment_id, symbol, decision_id, stop, tp, mh, invalidation, now_iso)
        elif action == "SELL":
            _sell_checks(state, symbol, qty, sell_all, parse_problems, pending_exit, pending_sell_qty, last_close, dq, cfg,
                         existing_orders, ctx, check)
        elif action == "BUY":
            _buy_checks(conn, experiment_id, state, symbol, notional, parse_problems, stop, tp, mh, invalidation, rule_problems,
                        dq, cfg, equity_ref, held_value, pending_buy_value, pending_exit, reserved, existing_orders, ctx, check, now_iso)

        failed = [c["check"] for c in checks if not c["passed"]]
        out.reasons = failed
        if action in ("BUY", "SELL") and not parse_problems and symbol:
            oid = orders.create(conn, experiment_id=experiment_id, account="PORTFOLIO", origin="ADVISOR", symbol=symbol,
                                side=action, notional_cents=notional if action == "BUY" else None,
                                quantity_micro=qty if action == "SELL" else None, sell_all=sell_all, decided_at=now_iso,
                                target_session_date=target_date, client_order_id=f"adv:{run_id}:{seq}", now=now_iso,
                                reserved_cash_cents=notional if action == "BUY" else 0, run_id=run_id,
                                decision_id=decision_id, config_version_id=cfg_vid, status="PROPOSED")
            out.order_id = oid
            out.target_session = target_date
            if failed:
                orders.transition(conn, oid, "REJECTED", ";".join(failed), now_iso)
            else:
                orders.transition(conn, oid, "PENDING", f"accepted for market-on-open {target_date}", now_iso)
                ctx["batch_orders"] += 1
                ctx["batch_symbols"].add(symbol)
                if action == "BUY":
                    ctx["batch_reserved"] += notional
                    ctx["batch_buy_value"][symbol] = ctx["batch_buy_value"].get(symbol, 0) + notional
                    rules.create_awaiting(conn, experiment_id, symbol, decision_id, stop, tp, mh, invalidation, now_iso)
                else:
                    ctx["batch_exit"].add(symbol)
        elif action in ("BUY", "SELL"):
            out.target_session = target_date
        if action in ("BUY", "SELL", "CANCEL", "HOLD"):
            out.status = "REJECTED" if failed else "ACCEPTED"
        elif action not in ACTIONS:
            out.status = "REJECTED"
        for c in checks:
            conn.execute("INSERT INTO risk_checks(run_id, decision_id, order_id, check_name, passed, observed, limit_value, message, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?)", (run_id, decision_id, out.order_id, c["check"], int(c["passed"]),
                                                          c["observed"], c["limit"], c["message"], now_iso))
    return [outcomes[i] for i in sorted(outcomes)]


def _hold_restatement_checks(conn, experiment_id, symbol, stop, tp, mh, invalidation, rule_problems, pending_exit, cfg, check):
    """A HOLD may restate a position's exit rules prospectively, but it may not weaken the
    precommitted risk control: moving a stop further away (or deleting it) after the fact is exactly
    the discipline failure precommitment exists to prevent. Tightening the stop, and changing the
    take-profit or the holding period, are allowed. Restatements are refused while an exit is
    already queued, so a pending exit cannot be orphaned from the rule that produced it."""
    check("invalid_exit_rule", not rule_problems, ",".join(rule_problems) or "ok", "valid ranges")
    if cfg.require_exit_rule_on_buy:
        has_rule = stop is not None or tp is not None or mh is not None
        check("missing_exit_rule", has_rule and bool(invalidation), "rule" if has_rule else "none",
              "stop/target/max-holding + invalidation text", "restated rules must still include a mechanical exit")
    active = rules.active_for(conn, experiment_id, symbol)
    triggered = any(r["status"] == "TRIGGERED" for r in active)
    check("exit_already_pending", symbol not in pending_exit and not triggered, symbol, None,
          "cannot restate exit rules while an exit order is pending")
    awaiting = conn.execute("SELECT COUNT(*) FROM position_rules WHERE experiment_id=? AND symbol=? AND status='AWAITING_ENTRY'",
                            (experiment_id, symbol)).fetchone()[0]
    check("entry_pending_for_symbol", awaiting == 0, awaiting, 0,
          "cannot restate rules while an add-on entry for the symbol is pending (its rules would supersede these)")
    _stop_not_loosened(active, stop, check)


def _stop_not_loosened(active_rules, stop, check):
    current_stop = next((Decimal(r["stop_loss_pct"]) for r in active_rules if r["stop_loss_pct"] is not None), None)
    if current_stop is not None:
        check("stop_loosening_not_allowed", stop is not None and stop <= current_stop,
              "removed" if stop is None else str(stop), f"<= {current_stop}",
              "a stop-loss may be tightened but never widened or removed by a later decision; the old stop level "
              "is also carried as a floor when adding to a position")


def _orders_check(existing_orders, ctx, cfg, check):
    n = existing_orders + ctx["batch_orders"] + 1
    check("max_new_orders_per_session", n <= cfg.max_new_orders_per_session, n, cfg.max_new_orders_per_session,
          "advisor orders for the target session including this one")


def _sell_checks(state, symbol, qty, sell_all, parse_problems, pending_exit, pending_sell_qty, last_close, dq, cfg,
                 existing_orders, ctx, check):
    for p in parse_problems:
        check(p, False, None, None, "quantity must be a positive number with at most 6 decimals, or ALL")
    held = state.positions.get(symbol, 0)
    if not check("no_position", held > 0, m.qty_to_str(held), "> 0", "long-only: cannot sell what is not held"):
        return
    check("duplicate_symbol_in_batch", symbol not in ctx["batch_symbols"], symbol, None, "one order per symbol per batch")
    check("exit_already_pending", symbol not in pending_exit, symbol, None, "an exit for this symbol is already pending")
    if not parse_problems:
        avail = held - pending_sell_qty.get(symbol, 0)
        want = held if sell_all else qty
        check("exceeds_available_quantity", want <= avail, m.qty_to_str(want), m.qty_to_str(avail),
              "held minus quantity already committed to pending sells")
        if not sell_all and qty < held and symbol in last_close:
            value = m.market_value_cents(qty, last_close[symbol])
            check("min_order", value >= cfg.min_order_cents, m.cents_to_str(value), m.cents_to_str(cfg.min_order_cents),
                  "partial sells must be worth at least the minimum order (selling the whole position is always allowed)")
    st = dq.get(symbol, {}).get("status")
    check("data_quality_for_sell", True, st, "FRESH preferred", "sells are allowed on non-fresh data (risk reduction)")
    _orders_check(existing_orders, ctx, cfg, check)


def _buy_checks(conn, experiment_id, state, symbol, notional, parse_problems, stop, tp, mh, invalidation, rule_problems,
                dq, cfg, equity_ref, held_value, pending_buy_value, pending_exit, reserved, existing_orders, ctx, check, now_iso):
    for p in parse_problems:
        check(p, False, None, None, "amount must be a positive USD amount with at most 2 decimals")
    inst = universe.get(conn, symbol) if symbol else None
    if not check("not_in_universe", inst is not None and inst["in_universe"] == 1, symbol, "research universe",
                 "only instruments in the documented, verified research universe may be bought"):
        return
    ok, why = universe.is_verified(conn, symbol, now_iso)
    # Say how THIS instrument is verified: the fixture's synthetic DEMO.* symbols have no official listing, and
    # "must be verified against official listings" on them contradicted the fixture's own health panel.
    check("instrument_unverified", ok, why, "VERIFIED within 14 days",
          "synthetic fixture instrument, verified as part of the fixture scenario" if universe.verified_as_fixture(conn, symbol)
          else "identity must be verified against official listings")
    check("asset_type_not_allowed", inst["asset_type"] in universe.ALLOWED_ASSET_TYPES and inst["leverage"] == "1x",
          f"{inst['asset_type']} {inst['leverage']}", "COMMON_STOCK/ETF 1x")
    q = dq.get(symbol, {"status": "MISSING"})
    if q["status"] == "MISSING":
        check("missing_data", False, None, q.get("expected_session"), "no stored price data")
    elif q["status"] == "STALE":
        check("stale_data", False, q["last_bar_date"], q["expected_session"], "latest bar must be the last completed session")
    elif q["status"] == "SUSPECT":
        check("suspect_data", False, ",".join(q["flags"]), "no flags", "latest bar has data-quality flags")
    else:
        check("fresh_data", True, q["last_bar_date"], q["expected_session"])
    if cfg.require_exit_rule_on_buy:
        has_rule = stop is not None or tp is not None or mh is not None
        check("missing_exit_rule", has_rule and bool(invalidation), "rule" if has_rule else "none",
              "stop/target/max-holding + invalidation text", "every entry needs a precommitted exit and an invalidation statement")
    if rule_problems:
        check("invalid_exit_rule", False, ",".join(rule_problems), "valid ranges")
    check("duplicate_symbol_in_batch", symbol not in ctx["batch_symbols"], symbol, None, "one order per symbol per batch")
    check("conflicting_pending_exit", symbol not in pending_exit and symbol not in ctx["batch_exit"], symbol, None,
          "cannot buy while an exit for the symbol is pending")
    if symbol in state.positions:
        # Adding to a held position replaces its rules on fill; the add-on may not widen the stop.
        _stop_not_loosened(rules.active_for(conn, experiment_id, symbol), stop, check)
    _orders_check(existing_orders, ctx, cfg, check)
    if notional is None:
        return
    # broker_sim pays commission out of the order and expires a buy whose cost is below the minimum, so the
    # minimum applies to what is left for shares. With the default $0 commission that is the notional itself.
    # SYNC WARNING: mirrors broker_sim._fill (budget, below_minimum_at_fill) and config.min_fillable_order_cents.
    after_fee = notional - m.commission_cents(notional, cfg.commission_per_order_cents, cfg.commission_bps)
    if after_fee == notional:
        check("min_order", notional >= cfg.min_order_cents, m.cents_to_str(notional), m.cents_to_str(cfg.min_order_cents))
    else:
        check("min_order", after_fee >= cfg.min_order_cents, m.cents_to_str(after_fee), m.cents_to_str(cfg.min_order_cents),
              f"order value after commission (${m.cents_to_str(notional)} minus ${m.cents_to_str(notional - after_fee)}) "
              "vs the minimum order: a smaller fill would expire at the open")
    pos_after = held_value.get(symbol, 0) + pending_buy_value.get(symbol, 0) + ctx["batch_buy_value"].get(symbol, 0) + notional
    cap = int((Decimal(equity_ref) * cfg.max_position_pct).to_integral_value(rounding="ROUND_FLOOR"))
    check("max_position_pct", pos_after <= cap, m.cents_to_str(pos_after), m.cents_to_str(cap),
          f"position value incl. pending and this order vs {format((cfg.max_position_pct * 100).normalize(), 'f')}% of equity "
          f"${m.cents_to_str(equity_ref)}")
    open_syms = set(state.positions) | set(pending_buy_value) | set(ctx["batch_buy_value"]) | {symbol}
    check("max_open_positions", len(open_syms) <= cfg.max_open_positions, len(open_syms), cfg.max_open_positions,
          "held + pending/batch buys (pending exits do not free a slot)")
    cash_after = state.cash_cents - reserved - ctx["batch_reserved"] - notional
    reserve = int((Decimal(equity_ref) * cfg.min_cash_reserve_pct).to_integral_value(rounding="ROUND_CEILING"))
    check("insufficient_cash", cash_after >= 0, m.cents_to_str(cash_after + notional), m.cents_to_str(notional),
          "cash minus all reservations must cover this order")
    check("cash_reserve", cash_after >= reserve, m.cents_to_str(cash_after), m.cents_to_str(reserve),
          f"cash left after all planned buys vs a {format((cfg.min_cash_reserve_pct * 100).normalize(), 'f')}% reserve")


def _cancel(conn, experiment_id, d, now, now_iso, cfg, check, ctx, decision_id):
    try:
        oid = int(d.get("cancel_order_id"))
    except (TypeError, ValueError):
        check("unknown_order", False, d.get("cancel_order_id"), None, "cancel_order_id must be an order id")
        return
    o = orders.get(conn, oid)
    if not check("unknown_order", o is not None and o["experiment_id"] == experiment_id and o["account"] == "PORTFOLIO"
                  and o["origin"] == "ADVISOR", oid, None, "only this experiment's advisor orders can be canceled"):
        return
    if not check("order_not_cancellable", o["status"] == "PENDING", o["status"], "PENDING"):
        return
    s = cal.session(date.fromisoformat(o["target_session_date"]))
    deadline = s.open_utc - timedelta(seconds=cfg.decision_open_buffer_seconds)
    if not check("target_session_already_open", now < deadline, now_iso, clock.iso(deadline),
                 "a market-on-open order can only be canceled before its open"):
        return
    orders.transition(conn, oid, "CANCELED", f"canceled by advisor decision {decision_id}", now_iso)
    if o["side"] == "BUY":
        rules.on_entry_not_filled(conn, o["decision_id"], "entry order canceled", now_iso)
    ctx["canceled"].add(oid)
