"""The morning briefing: everything the advisor needs to decide, in one JSON document.

This is also exactly what is transmitted to the model service (Anthropic) when the Claude session
reads it: account state, positions, prices, headlines and skill outputs for a $500 paper account.
No credentials or personal data are included. The database itself never leaves the machine.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from . import accounting, analytics, clock, config, decisions as dec_schema, experiments, market_calendar as cal, metrics
from . import money as m
from . import orders, prices, quality, rules, universe


def _times(now) -> dict:
    return {"utc": clock.iso(now), "pacific": now.astimezone(cal.ZoneInfo("America/Los_Angeles")).strftime("%a %Y-%m-%d %H:%M %Z"),
            "eastern": now.astimezone(cal.NY).strftime("%a %Y-%m-%d %H:%M %Z")}


def limits_snapshot(conn, exp_id: int, now, cfg) -> dict:
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    closes = {}
    for s in st.positions:
        b = prices.latest_bar(conn, s)
        if b is not None:
            closes[s] = Decimal(b["close"])
    v = accounting.valuation(st, closes)
    equity = v.equity_cents
    reserved = orders.reserved_cash_cents(conn, exp_id)
    reserve = int((Decimal(equity) * cfg.min_cash_reserve_pct).to_integral_value(rounding="ROUND_CEILING"))
    target = cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds))
    used = orders.advisor_orders_for_session(conn, exp_id, target.date.isoformat())
    pend_buy_syms = {o["symbol"] for o in orders.pending(conn, exp_id, account="PORTFOLIO") if o["side"] == "BUY"}
    open_syms = set(st.positions) | pend_buy_syms
    return {
        "equity_reference_usd": m.cents_to_str(equity),
        "cash_usd": m.cents_to_str(st.cash_cents),
        "reserved_by_pending_buys_usd": m.cents_to_str(reserved),
        "required_cash_reserve_usd": m.cents_to_str(reserve),
        "available_for_new_buys_usd": m.cents_to_str(max(0, st.cash_cents - reserved - reserve)),
        "max_position_value_usd": m.cents_to_str(int((Decimal(equity) * cfg.max_position_pct).to_integral_value(rounding="ROUND_FLOOR"))),
        "min_order_usd": m.cents_to_str(cfg.min_order_cents),
        "open_positions_incl_pending_buys": len(open_syms), "max_open_positions": cfg.max_open_positions,
        "orders_target_session": target.date.isoformat(),
        "advisor_orders_used_for_target_session": used, "max_new_orders_per_session": cfg.max_new_orders_per_session,
        "orders_remaining_for_target_session": max(0, cfg.max_new_orders_per_session - used),
        "slippage_bps_per_side": str(cfg.slippage_bps), "commission_per_order_usd": m.cents_to_str(cfg.commission_per_order_cents),
        "commission_bps": str(cfg.commission_bps),
        "cost_note": "commission and slippage are configurable SIMULATION ASSUMPTIONS, not verified broker economics",
    }


def positions_detail(conn, exp_id: int, last_session: str | None, earnings: dict) -> list[dict]:
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    out = []
    total_closes = {}
    for sym in sorted(st.positions):
        b = prices.latest_bar(conn, sym)
        total_closes[sym] = Decimal(b["close"]) if b else None
    v = accounting.valuation(st, {k: x for k, x in total_closes.items() if x is not None})
    for p in v.positions:
        active = rules.active_for(conn, exp_id, p.symbol)
        r = active[0] if active else None
        d = conn.execute("SELECT * FROM decisions WHERE id=?", (r["decision_id"],)).fetchone() if r and r["decision_id"] else None
        held = None
        if r and r["entry_session_date"] and last_session:
            held = cal.count_sessions_after(date.fromisoformat(r["entry_session_date"]), date.fromisoformat(last_session))
        lvl = rules.stop_level(r) if r else None
        out.append({
            "symbol": p.symbol, "quantity": m.qty_to_str(p.qty_micro), "avg_cost": str(st.avg_cost(p.symbol).quantize(Decimal("0.0001"))),
            "last_close": None if p.price is None else str(p.price), "market_value_usd": m.cents_to_str(p.market_value_cents),
            "cost_basis_usd": m.cents_to_str(p.basis_cents), "unrealized_usd": m.cents_to_str(p.unrealized_cents),
            "weight_of_equity": round(p.market_value_cents / v.equity_cents, 4) if v.equity_cents else None,
            "exit_rules": None if r is None else {"status": r["status"], "description": rules.describe(r),
                                                   "stop_level": None if lvl is None else str(lvl.quantize(Decimal("0.01"))),
                                                   "max_holding_sessions": r["max_holding_sessions"], "invalidation": r["invalidation"]},
            "entry_session": r["entry_session_date"] if r else None, "sessions_held": held,
            "thesis": d["thesis"] if d else None, "decision_id": d["id"] if d else None,
            "horizon": d["horizon_text"] if d else None, "upcoming_earnings": earnings.get(p.symbol),
        })
    return out


def build(conn, exp_id: int, run, now, refresh: dict | None, settlement: dict, reconciliation: list, skills: dict,
          news: dict | None = None) -> dict:
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    exp = experiments.get(conn, exp_id)
    last = cal.last_completed_session(now, cfg.bar_finalization_minutes)
    last_s = last.date.isoformat() if last else None
    status = cal.market_status(now)
    target = cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds))
    mt = metrics.compute(conn, exp_id)
    earnings_by = (refresh or {}).get("earnings", {}).get("by_symbol", {}) if refresh else {}
    syms = universe.symbols(conn)
    dq = quality.assess(conn, syms, now, cfg.bar_finalization_minutes)
    uni = []
    for sym in syms:
        inst = universe.get(conn, sym)
        ok, why = universe.is_verified(conn, sym, clock.iso(now))
        ind = analytics.indicators(analytics.adjusted_series(conn, sym, last_s)) if last_s else {}
        uni.append({"symbol": sym, "name": inst["name"], "type": inst["asset_type"], "benchmark": bool(inst["is_benchmark"]),
                    "identity_verified": ok, "identity_note": why, "data_status": dq[sym]["status"],
                    "indicators": {k: ind.get(k) for k in ("last_date", "last_close", "ret_1d", "ret_5d", "ret_20d", "ret_60d",
                                                           "ret_252d", "pct_vs_sma50", "pct_vs_sma200", "pct_from_252d_high",
                                                           "vol_20d_annualized", "atr14_pct", "rsi14", "volume_ratio_20_60", "bars")},
                    "upcoming_earnings": earnings_by.get(sym)})
    pend = [{"order_id": o["id"], "origin": o["origin"], "symbol": o["symbol"], "side": o["side"],
             "notional_usd": None if o["notional_cents"] is None else m.cents_to_str(o["notional_cents"]),
             "quantity": "ALL" if o["sell_all"] else (None if o["quantity_micro"] is None else m.qty_to_str(o["quantity_micro"])),
             "decided_at": o["decided_at"], "target_session": o["target_session_date"], "why": o["origin_note"] or o["status_reason"]}
            for o in orders.pending(conn, exp_id, account="PORTFOLIO")]
    strat = experiments.current_strategy(conn, exp_id)
    hyps = [dict(h) for h in conn.execute("SELECT id, created_at, title, hypothesis, status, earliest_effective_session FROM strategy_hypotheses"
                                          " WHERE experiment_id=? AND status IN ('proposed','adopted') ORDER BY id DESC LIMIT 10", (exp_id,))]
    prev_runs = [dict(r) for r in conn.execute(
        "SELECT r.id, r.analysis_session_date, r.version, r.mode, r.status, r.started_at, r.model_id,"
        " (SELECT COUNT(*) FROM decisions d WHERE d.run_id=r.id) AS decisions FROM runs r WHERE r.experiment_id=? AND r.id<?"
        " AND r.run_kind='morning' ORDER BY r.id DESC LIMIT 5", (exp_id, run["id"]))]
    return {
        "PAPER_TRADING": True,
        "notice": "Simulated paper trading with fake money. No real orders exist or can be placed.",
        "experiment": {"id": exp_id, "name": exp["name"], "mode": exp["mode"], "starting_capital_usd": m.cents_to_str(exp["starting_capital_cents"]),
                       "inception_at": exp["inception_at"], "first_investable_session": exp["benchmark_start_session"]},
        "run": {"id": run["id"], "version": run["version"], "mode": run["mode"], "analysis_session": run["analysis_session_date"],
                "supersedes_run_id": run["supersedes_run_id"], "times": _times(now)},
        "market": {**status, "last_completed_session": last_s, "orders_will_target_session": target.date.isoformat(),
                   "target_session_open_utc": clock.iso(target.open_utc), "target_session_early_close": target.early_close,
                   "execution_rule": ("Orders are simulated market-on-open fills at the official open of the first regular session that "
                                      "opens strictly after your decisions are committed (+%ds buffer), using that session's daily bar. "
                                      "Fills are recorded only after that session closes. Prices are end-of-day data, not live quotes."
                                      % cfg.decision_open_buffer_seconds)},
        "data": {"status": (refresh or {}).get("data_status", "NOT_REFRESHED"), "failures": (refresh or {}).get("failures", []),
                 "quality": {s: {"status": q["status"], "last_bar": q["last_bar_date"], "flags": q["flags"]} for s, q in dq.items()},
                 "context": (refresh or {}).get("context", {})},
        "account": {k: mt[k] for k in ("cash_cents", "invested_cents", "equity_cents", "realized_pnl_cents", "unrealized_pnl_cents",
                                         "dividends_cents", "fees_cents", "total_pnl_cents", "total_return", "spy_benchmark_return",
                                         "excess_vs_spy", "max_drawdown", "sessions_valued", "closed_trades", "insufficient_sample",
                                         "sample_note", "provisional_sessions", "as_of_session")},
        "limits": limits_snapshot(conn, exp_id, now, cfg),
        "positions": positions_detail(conn, exp_id, last_s, earnings_by),
        "pending_orders": pend,
        "settlement_this_run": settlement,
        "reconciliation": [r for r in reconciliation if not r["passed"]] or "all checks passed",
        "universe": uni,
        "earnings_calendar": (refresh or {}).get("earnings", {}),
        "news": news or {},
        "skills": skills,
        "strategy": {"version": strat["version"], "title": strat["title"], "description": strat["description"]},
        "hypotheses": hyps,
        "previous_runs": prev_runs,
        "decision_schema": dec_schema.DECISION_SCHEMA_DOC,
    }


def write(paths, run_id: int, data: dict) -> str:
    d = paths.runs_dir / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "briefing.json"
    p.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return str(p)
