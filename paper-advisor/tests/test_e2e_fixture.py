"""End-to-end isolated fixture scenario across multiple sessions (see fixture_demo.py for the script).

Expected values were computed by hand before the code was run (2026-10-24), using the conservative
rounding rules in money.py:
  A: $120 at 50.00 open -> 50.05 -> 2.397602 sh, cost $120.00. HOLD tightens stop to 6% (47.05).
     Tue close 46.50 <= 47.05 -> exit at Wed open 45.80 * 0.999 = 45.7542 -> $109.70; realized -$10.30.
  B: $100 at 40.04 -> 2.497502 sh; 2:1 split at Tue open -> 4.995004 sh.
  C: $60 at 30.03 -> 1.998001 sh; $0.30 dividend ex Wed -> floor(0.5994003) = $0.59.
  E: decided Tue 09:00 ET (pre-open) -> fills Tue open 10.01 -> 4.995004 sh ($50.00).
  F: decided Mon 12-01 10:00 ET (after open) -> fills Tue 12-02 open 25.025 -> 3.196803 sh ($80.00).
  Final (Tue 12-02 close): cash 200.29; B 107.89; C 61.34; E 52.70; F 82.48 -> equity $504.70.
  SPY benchmark: 4.995004 units at 100.10; Tue 12-02 close 102.70 -> $512.99.
"""
from pathlib import Path

from paper_advisor import accounting, db, fixture_demo, metrics, valuation
from paper_advisor.paths import AppPaths


def test_fixture_scenario_end_to_end(tmp_path):
    paths = AppPaths(tmp_path / "app")
    out = fixture_demo.run_demo(paths, rebuild=True)
    runs = out["runs"]
    assert [r["mode"] for r in runs] == ["analysis", "reconcile_only", "analysis", "analysis", "analysis"]
    first = runs[0]["outcomes"]
    assert [o["status"] for o in first] == ["ACCEPTED", "ACCEPTED", "ACCEPTED", "REJECTED"]
    assert set(first[3]["reasons"]) == {"max_new_orders_per_session", "max_position_pct", "cash_reserve"}
    assert runs[3]["settled"]["sessions_processed"] == ["2025-11-25", "2025-11-26", "2025-11-28"]  # missed mornings
    assert runs[3]["outcomes"][0]["target_session"] == "2025-12-02"  # after-open decision -> next session
    assert out["reconciliation_passed"] is True

    conn = db.open_existing(Path(out["fixture_db"]))
    exp_id = out["experiment_id"]
    fills = {(f["symbol"], f["side"]): f for f in conn.execute("SELECT * FROM fills WHERE account='PORTFOLIO'")}
    assert fills[("DEMO.A", "BUY")]["quantity_micro"] == 2_397_602 and fills[("DEMO.A", "BUY")]["gross_cents"] == 12000
    assert fills[("DEMO.A", "SELL")]["fill_price"] == "45.754200" and fills[("DEMO.A", "SELL")]["gross_cents"] == 10970
    assert fills[("DEMO.E", "BUY")]["sim_exec_at"] == "2025-11-25T14:30:00.000000Z"
    assert fills[("DEMO.F", "BUY")]["sim_exec_at"] == "2025-12-02T14:30:00.000000Z"
    for f in fills.values():
        assert f["decided_at"] < f["sim_exec_at"] < f["bar_retrieved_at"] <= f["recorded_at"]
    # Missed days: Tuesday/Wednesday events were simulated then but recorded the next Monday.
    assert fills[("DEMO.A", "SELL")]["recorded_at"] == "2025-12-01T15:00:00.000000Z"

    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    assert st.positions == {"DEMO.B": 4_995_004, "DEMO.C": 1_998_001, "DEMO.E": 4_995_004, "DEMO.F": 3_196_803}
    assert (st.cash_cents, st.realized_cents, st.dividends_cents) == (20029, -1030, 59)
    snaps = {s["session_date"]: s for s in valuation.snapshots(conn, exp_id, "PORTFOLIO")}
    assert "2025-11-27" not in snaps                       # Thanksgiving: no session, no snapshot
    assert snaps["2025-11-24"]["equity_cents"] == 50062
    assert snaps["2025-12-02"]["equity_cents"] == 50470
    assert valuation.latest_snapshot(conn, exp_id, "BENCHMARK")["equity_cents"] == 51299
    rule = conn.execute("SELECT origin_note FROM orders WHERE origin='RULE'").fetchone()[0]
    assert rule.startswith("STOP_LOSS: close 46.5000 on 2025-11-25")
    hyp = conn.execute("SELECT * FROM strategy_hypotheses").fetchall()
    assert len(hyp) == 1 and hyp[0]["status"] == "proposed" and hyp[0]["earliest_effective_session"] >= "2025-12-03"
    assert conn.execute("SELECT COUNT(*) FROM ledger_events WHERE event_type='INITIAL_FUNDING'").fetchone()[0] == 2

    # Persistence after restart: a fresh process/connection sees exactly the same record.
    before = metrics.compute(conn, exp_id)
    conn.close()
    conn2 = db.open_existing(Path(out["fixture_db"]))
    assert metrics.compute(conn2, exp_id) == before
    again = fixture_demo.run_demo(paths, rebuild=False)  # rerunning without --rebuild changes nothing
    assert again["metrics"]["equity_cents"] == 50470 and "already built" in again["note"]
    conn2.close()
