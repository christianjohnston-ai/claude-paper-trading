"""Reconciliation: prove on every run that cash, holdings, orders, fills, ledger and snapshots agree.

Holdings are never stored as mutable state, so "reconciling the ledger" means: replay it from scratch
and check every independent record against the replay. Any failure is recorded and shown in the
briefing, the report and the dashboard; it does not stop data collection, but the morning command
tells the advisor not to trade on an unreconciled account.
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import accounting, clock, prices, valuation


def run_checks(conn, experiment_id: int, run_id: int | None = None, now=None) -> list[dict]:
    now_iso = clock.iso(now) if now is not None else clock.now_iso()
    results = []

    def rec(name, passed, **details):
        results.append({"check": name, "passed": bool(passed), "details": details})

    states = {}
    for account in ("PORTFOLIO", "BENCHMARK"):
        try:
            states[account] = accounting.load_state(conn, experiment_id, account)
            rec(f"ledger_replay:{account}", True, last_seq=states[account].last_seq)
        except accounting.ReplayError as exc:
            rec(f"ledger_replay:{account}", False, error=str(exc))

    n_fund = conn.execute("SELECT account, COUNT(*) n FROM ledger_events WHERE experiment_id=? AND event_type='INITIAL_FUNDING' GROUP BY account",
                          (experiment_id,)).fetchall()
    rec("funding_exactly_once", {r["account"]: r["n"] for r in n_fund} == {"PORTFOLIO": 1, "BENCHMARK": 1},
        counts={r["account"]: r["n"] for r in n_fund})

    fills = conn.execute("SELECT * FROM fills WHERE experiment_id=?", (experiment_id,)).fetchall()
    ev = {r["fill_id"]: r for r in conn.execute("SELECT * FROM ledger_events WHERE experiment_id=? AND fill_id IS NOT NULL", (experiment_id,))}
    bad = [f["id"] for f in fills if f["id"] not in ev or int(ev[f["id"]]["cash_delta_cents"]) != int(f["net_cash_cents"])
           or abs(int(ev[f["id"]]["quantity_delta_micro"])) != int(f["quantity_micro"])]
    orphan_events = conn.execute("SELECT COUNT(*) FROM ledger_events WHERE experiment_id=? AND event_type IN ('BUY_FILL','SELL_FILL') AND fill_id IS NULL",
                                 (experiment_id,)).fetchone()[0]
    rec("fills_match_ledger", not bad and orphan_events == 0, mismatched_fill_ids=bad, fill_events_without_fill=orphan_events)

    filled = {r["id"] for r in conn.execute("SELECT id FROM orders WHERE experiment_id=? AND status='FILLED'", (experiment_id,))}
    fill_orders = {f["order_id"] for f in fills}
    rec("orders_match_fills", filled == fill_orders, filled_without_fill=sorted(filled - fill_orders),
        fill_without_filled_order=sorted(fill_orders - filled))

    timing = [f["id"] for f in fills if not (f["decided_at"] < f["sim_exec_at"] < f["bar_retrieved_at"] <= f["recorded_at"] <= now_iso)]
    rec("fill_timing", not timing, violating_fill_ids=timing)

    if "PORTFOLIO" in states:
        st = states["PORTFOLIO"]
        reserved = conn.execute("SELECT COALESCE(SUM(reserved_cash_cents),0) FROM orders WHERE experiment_id=? AND account='PORTFOLIO'"
                                " AND status='PENDING' AND side='BUY'", (experiment_id,)).fetchone()[0]
        rec("reservations_covered_by_cash", reserved <= st.cash_cents, reserved_cents=reserved, cash_cents=st.cash_cents)

    for account, st in states.items():
        snap = valuation.latest_snapshot(conn, experiment_id, account)
        if snap is None:
            rec(f"snapshot_consistency:{account}", True, note="no snapshot yet")
            continue
        at = accounting.load_state(conn, experiment_id, account, through_seq=snap["ledger_seq_through"])
        marks = {p["symbol"]: Decimal(p["close"]) for p in json.loads(snap["positions_json"]) if p["close"] is not None}
        v = accounting.valuation(at, marks)
        ok = (at.cash_cents == snap["cash_cents"] and v.equity_cents == snap["equity_cents"]
              and at.realized_cents == snap["realized_pnl_cents"] and v.unrealized_cents == snap["unrealized_pnl_cents"])
        identity = v.equity_cents - at.funded_cents == at.realized_cents + v.unrealized_cents + at.dividends_cents + at.adjustments_cents
        rec(f"snapshot_consistency:{account}", ok and identity, session=snap["session_date"], stored_equity=snap["equity_cents"],
            recomputed_equity=v.equity_cents, identity_holds=identity)

    for r in results:
        conn.execute("INSERT INTO reconciliation_results(run_id, experiment_id, at, check_name, passed, details_json) VALUES (?,?,?,?,?,?)",
                     (run_id, experiment_id, now_iso, r["check"], int(r["passed"]), json.dumps(r["details"], default=str)))
    return results
