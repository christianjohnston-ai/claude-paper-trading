"""Simulated execution and settlement over the passage of time.

Timeline used below (2026, all UTC; EDT until Nov 1):
  Fri 10-23 close 20:00, final 21:00   Sat 10-24 decisions   Mon 10-26 open 13:30 close 20:00
  Tue 10-27, Wed 10-28, Thu 10-29 likewise.  Thanksgiving Thu 11-26 closed; Fri 11-27 early close
  (EST: open 14:30, close 18:00).

Execution rule under test: a market-on-open order fills at the official OPEN of the first regular
session that opened strictly after the decision, using that session's daily bar, which is only
available once the session has closed. So a Saturday decision fills at Monday's open and is
RECORDED on the first settlement after Monday's close (recorded_at > sim_exec_at, always).
"""
from decimal import Decimal

from paper_advisor import accounting, broker_sim, orders, submission, valuation

from helpers import World
from test_risk import FRESH, d


def setup(fixture_db, **cfg):
    w = World(fixture_db, start="2026-10-23T23:00:00", config_params=cfg or None)
    w.bars_all("2026-10-23", FRESH)
    w.cover_corporate_actions("2026-12-31")
    w.at("2026-10-24T05:00:00")
    return w


def submit(w, decisions):
    return submission.apply_decisions(w.conn, w.exp_id, w.new_run(), decisions, now=w.clock())


def settle(w):
    return broker_sim.settle(w.conn, w.exp_id, now=w.clock())


MON = {"DEMO.SPY": (100, 101.5, 99.5, 101), "DEMO.A": (50, 52.5, 49.5, 52), "DEMO.B": (40, 41.5, 39.5, 41),
       "DEMO.C": (30, 30.5, 29.5, 30), "DEMO.D": 20, "DEMO.E": 10, "DEMO.F": 25}


def test_fill_waits_for_final_bar_and_uses_open_after_decision(fixture_db):
    w = setup(fixture_db)
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    oid = out[0].order_id
    assert out[0].target_session == "2026-10-26"

    w.at("2026-10-26T20:30:00")  # Monday closed 30 minutes ago: bar not final yet
    rep = settle(w)
    assert rep.fills == [] and orders.get(fixture_db, oid)["status"] == "PENDING"

    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")  # Tuesday morning run
    rep = settle(w)
    assert orders.get(fixture_db, oid)["status"] == "FILLED"
    f = fixture_db.execute("SELECT * FROM fills WHERE order_id=?", (oid,)).fetchone()
    assert (Decimal(f["fill_price"]), f["quantity_micro"], f["gross_cents"]) == (Decimal("50.05"), 1_998_001, 10000)
    assert f["decided_at"] == "2026-10-24T05:00:00.000000Z"
    assert f["sim_exec_at"] == "2026-10-26T13:30:00.000000Z"
    assert f["recorded_at"] == "2026-10-27T13:00:00.000000Z"
    st = accounting.load_state(fixture_db, w.exp_id, "PORTFOLIO")
    assert st.cash_cents == 40000 and st.positions == {"DEMO.A": 1_998_001}
    snap = valuation.latest_snapshot(fixture_db, w.exp_id, "PORTFOLIO")
    assert (snap["session_date"], snap["equity_cents"], snap["unrealized_pnl_cents"]) == ("2026-10-26", 50390, 390)

    # Benchmark: $500 of DEMO.SPY at Monday's open (100 * 1.001 = 100.10) -> 4.995004 units for $500.00
    b = accounting.load_state(fixture_db, w.exp_id, "BENCHMARK")
    assert b.positions == {"DEMO.SPY": 4_995_004} and b.cash_cents == 0
    bsnap = valuation.latest_snapshot(fixture_db, w.exp_id, "BENCHMARK")
    assert bsnap["equity_cents"] == 50450  # 4.995004 * 101 = 504.495404 -> 504.50


def test_decision_after_open_is_not_filled_at_that_days_open(fixture_db):
    w = setup(fixture_db)
    w.at("2026-10-26T14:00:00")  # Monday 10:00 ET
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    oid = out[0].order_id
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")
    settle(w)
    assert orders.get(fixture_db, oid)["status"] == "PENDING"  # Monday's open preceded the decision
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {**MON, "DEMO.A": (53, 54, 52, 53.5)})
    w.at("2026-10-28T13:00:00")
    settle(w)
    f = fixture_db.execute("SELECT * FROM fills WHERE order_id=?", (oid,)).fetchone()
    assert f["sim_exec_at"] == "2026-10-27T13:30:00.000000Z" and Decimal(f["reference_open"]) == Decimal("53")


def test_settlement_is_idempotent(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")
    settle(w)
    counts = lambda: tuple(fixture_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in ("fills", "ledger_events", "valuation_snapshots", "order_events"))
    before = counts()
    settle(w)
    settle(w)
    assert counts() == before


def test_missing_symbol_bar_expires_the_order(fixture_db):
    w = setup(fixture_db)
    oid = submit(w, [d("BUY", "DEMO.A", notional_usd="100")])[0].order_id
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", {k: v for k, v in MON.items() if k != "DEMO.A"})  # DEMO.A did not trade / no data
    w.at("2026-10-27T13:00:00")
    settle(w)
    o = orders.get(fixture_db, oid)
    assert o["status"] == "EXPIRED" and o["status_reason"] == "no_bar_for_target_session"
    assert orders.reserved_cash_cents(fixture_db, w.exp_id) == 0


def test_session_without_any_data_keeps_orders_pending_then_times_out(fixture_db):
    w = setup(fixture_db)
    oid = submit(w, [d("BUY", "DEMO.A", notional_usd="100")])[0].order_id
    w.at("2026-10-27T13:00:00")  # Monday complete but no bars at all were obtained (feed outage)
    rep = settle(w)
    assert orders.get(fixture_db, oid)["status"] == "PENDING"
    assert rep.blocked_session == "2026-10-26" and rep.blocked_reason == "session_data_unavailable"
    w.at("2026-11-02T15:00:00")  # more than the wait window later, still no data for Monday
    rep = settle(w)
    o = orders.get(fixture_db, oid)
    assert o["status"] == "EXPIRED" and o["status_reason"] == "session_data_unavailable_timeout"


def test_costs_are_applied_and_never_exceed_notional(fixture_db):
    w = setup(fixture_db, commission_per_order_usd="1.00", commission_bps="5")
    oid = submit(w, [d("BUY", "DEMO.A", notional_usd="100")])[0].order_id
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")
    settle(w)
    f = fixture_db.execute("SELECT * FROM fills WHERE order_id=?", (oid,)).fetchone()
    assert (f["quantity_micro"], f["gross_cents"], f["commission_cents"]) == (1_977_022, 9895, 105)
    assert f["gross_cents"] + f["commission_cents"] <= 10000 and f["net_cash_cents"] == -10000
    assert Decimal(f["slippage_bps"]) == Decimal("10")


def test_holiday_and_early_close_session(fixture_db):
    w = World(fixture_db, start="2026-11-25T22:00:00")  # Wednesday after the close, before Thanksgiving
    w.bars_all("2026-11-25", FRESH)
    w.cover_corporate_actions("2026-12-31")
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    assert out[0].target_session == "2026-11-27"  # Thanksgiving skipped
    w.at("2026-11-27T19:01:00")  # early close 13:00 ET = 18:00 UTC; bar final after 19:00 UTC
    w.bars_all("2026-11-27", MON)
    settle(w)
    f = fixture_db.execute("SELECT * FROM fills").fetchall()
    port = [x for x in f if x["account"] == "PORTFOLIO"]
    assert port[0]["sim_exec_at"] == "2026-11-27T14:30:00.000000Z"
    snaps = [r["session_date"] for r in fixture_db.execute(
        "SELECT session_date FROM valuation_snapshots WHERE account='PORTFOLIO' ORDER BY session_date")]
    assert "2026-11-26" not in snaps and "2026-11-27" in snaps


def _two_sessions(w, tue_overrides=None, wed_overrides=None):
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {**MON, **(tue_overrides or {})})
    w.at("2026-10-28T21:05:00")
    w.bars_all("2026-10-28", {**MON, **(wed_overrides or {})})


def test_stop_loss_is_close_confirmed_and_exits_at_next_open(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])  # stop 8%: 50.050025 * 0.92 = 46.046023
    # Tuesday trades down to 40 intraday but CLOSES at 46.00 (below the stop) -> exit at Wednesday open.
    _two_sessions(w, tue_overrides={"DEMO.A": (49, 49, 40, 46)}, wed_overrides={"DEMO.A": (44, 45, 43, 44.5)})
    w.at("2026-10-29T13:00:00")  # missed Tue/Wed mornings; everything reconciles in order now
    rep = settle(w)
    assert [s for s in rep.sessions_processed] == ["2026-10-26", "2026-10-27", "2026-10-28"]
    rule_orders = fixture_db.execute("SELECT * FROM orders WHERE origin='RULE'").fetchall()
    assert len(rule_orders) == 1
    ro = rule_orders[0]
    assert ro["decided_at"] == "2026-10-27T20:00:00.000000Z" and ro["target_session_date"] == "2026-10-28"
    assert ro["origin_note"].startswith("STOP_LOSS") and ro["status"] == "FILLED"
    f = fixture_db.execute("SELECT * FROM fills WHERE order_id=?", (ro["id"],)).fetchone()
    assert Decimal(f["fill_price"]) == Decimal("43.956")  # 44 * 0.999, the gap is realized, not the stop level
    st = accounting.load_state(fixture_db, w.exp_id, "PORTFOLIO")
    assert st.positions == {} and st.realized_cents == 8782 - 10000
    assert f["recorded_at"] == "2026-10-29T13:00:00.000000Z"


def test_take_profit_and_max_holding_rules(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100", exit_rules={"stop_loss_pct": "0.08", "take_profit_pct": "0.10",
                                                                  "max_holding_sessions": None, "invalidation": "x"}),
               d("BUY", "DEMO.B", notional_usd="100", exit_rules={"stop_loss_pct": None, "take_profit_pct": None,
                                                                  "max_holding_sessions": 2, "invalidation": "x"})])
    _two_sessions(w, tue_overrides={"DEMO.A": (52, 56, 52, 56)})
    w.at("2026-10-29T13:00:00")
    settle(w)
    reasons = {r["symbol"]: r["origin_note"] for r in fixture_db.execute("SELECT * FROM orders WHERE origin='RULE'")}
    assert reasons["DEMO.A"].startswith("TAKE_PROFIT")
    assert reasons["DEMO.B"].startswith("MAX_HOLDING")  # Tue=1, Wed=2 sessions held -> exit Thu open
    b_exit = fixture_db.execute("SELECT target_session_date FROM orders WHERE origin='RULE' AND symbol='DEMO.B'").fetchone()[0]
    assert b_exit == "2026-10-29"


def test_split_adjusts_position_pending_sell_and_rule(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.B", notional_usd="100")])  # Monday open 40 -> 40.04 -> 2.497502 shares
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", MON)
    w.at("2026-10-27T21:30:00")
    settle(w)
    sell = submit(w, [d("SELL", "DEMO.B", quantity="1", exit_rules=None)])  # 1 pre-split share, Wednesday open
    assert sell[0].status == "ACCEPTED"
    w.split("DEMO.B", "2026-10-28", 2, 1)
    w.at("2026-10-28T21:05:00")
    w.bars_all("2026-10-28", {**MON, "DEMO.B": (20.6, 21.2, 20.4, 21)})
    w.at("2026-10-29T13:00:00")
    settle(w)
    so = orders.get(fixture_db, sell[0].order_id)
    assert so["status"] == "FILLED" and so["quantity_micro"] == 2_000_000  # scaled with the 2:1 split
    st = accounting.load_state(fixture_db, w.exp_id, "PORTFOLIO")
    assert st.positions["DEMO.B"] == 4_995_004 - 2_000_000
    assert st.realized_cents == 4115 - 4004
    # The 8% stop (reference 40.040016 -> 20.020008 after the split) must NOT fire on a 21.00 close.
    assert fixture_db.execute("SELECT COUNT(*) FROM orders WHERE origin='RULE'").fetchone()[0] == 0
    snap = valuation.latest_snapshot(fixture_db, w.exp_id, "PORTFOLIO")
    assert snap["session_date"] == "2026-10-28" and snap["provisional"] == 0


def test_dividend_goes_to_holders_of_record_not_ex_date_buyers(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.C", notional_usd="60")])  # Monday open 30 -> 1.998001 shares
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", MON)
    w.at("2026-10-27T21:30:00")
    settle(w)
    submit(w, [d("BUY", "DEMO.C", notional_usd="30")])  # buys AT the ex-date open: not entitled
    w.dividend("DEMO.C", "2026-10-28", "0.24")
    w.at("2026-10-28T21:05:00")
    w.bars_all("2026-10-28", {**MON, "DEMO.C": (29.8, 30.2, 29.6, 30)})
    w.at("2026-10-29T13:00:00")
    settle(w)
    st = accounting.load_state(fixture_db, w.exp_id, "PORTFOLIO")
    assert st.dividends_cents == 47  # floor(1.998001 * 0.24 = 0.47952024)
    v = accounting.valuation(st, {"DEMO.C": Decimal("30")})
    assert v.equity_cents - st.funded_cents == st.realized_cents + v.unrealized_cents + st.dividends_cents


def test_benchmark_receives_dividends_too(fixture_db):
    w = setup(fixture_db)
    w.dividend("DEMO.SPY", "2026-10-27", "1.50")
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", MON)
    w.at("2026-10-28T13:00:00")
    settle(w)
    b = accounting.load_state(fixture_db, w.exp_id, "BENCHMARK")
    assert b.dividends_cents == 749  # floor(4.995004 * 1.50 = 7.492506)


def test_missing_corporate_action_coverage_marks_valuation_provisional(fixture_db):
    w = World(fixture_db, start="2026-10-23T23:00:00")
    w.bars_all("2026-10-23", FRESH)
    w.at("2026-10-24T05:00:00")
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")
    settle(w)
    snap = valuation.latest_snapshot(fixture_db, w.exp_id, "PORTFOLIO")
    assert snap["provisional"] == 1
    assert "corporate_actions_unverified:DEMO.A" in snap["provisional_reasons_json"]


def test_stale_mark_is_flagged_not_hidden(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {k: v for k, v in MON.items() if k != "DEMO.A"})  # no Tuesday mark for DEMO.A
    w.at("2026-10-28T13:00:00")
    settle(w)
    snap = valuation.latest_snapshot(fixture_db, w.exp_id, "PORTFOLIO")
    assert snap["session_date"] == "2026-10-27" and snap["provisional"] == 1
    assert "stale_mark:DEMO.A" in snap["provisional_reasons_json"]


def test_expired_rule_exit_rearms_but_never_duplicates(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    without_a = {k: v for k, v in MON.items() if k != "DEMO.A"}
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {**MON, "DEMO.A": (49, 49, 45, 45)})  # close below stop -> exit for Wed
    w.at("2026-10-28T21:05:00")
    w.bars_all("2026-10-28", without_a)                             # DEMO.A has no Wed bar -> exit expires
    w.at("2026-10-29T21:05:00")
    w.bars_all("2026-10-29", {**MON, "DEMO.A": (44, 44, 43, 44)})  # still below stop -> one new exit for Fri
    w.at("2026-10-30T13:00:00")
    settle(w)
    rule_orders = fixture_db.execute("SELECT status, target_session_date FROM orders WHERE origin='RULE' ORDER BY id").fetchall()
    assert [(r["status"], r["target_session_date"]) for r in rule_orders] == [
        ("EXPIRED", "2026-10-28"), ("PENDING", "2026-10-30")]
    pend = [o for o in orders.pending(fixture_db, w.exp_id) if o["origin"] == "RULE"]
    assert len(pend) == 1


def test_partial_advisor_sell_does_not_switch_off_the_stop(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])  # 1.998001 shares, stop at 46.046023
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-26T21:30:00")
    settle(w)
    w.at("2026-10-27T14:00:00")  # Tuesday after the open: the partial sell targets WEDNESDAY's open
    part = submit(w, [d("SELL", "DEMO.A", quantity="0.5", exit_rules=None)])
    assert part[0].status == "ACCEPTED" and part[0].target_session == "2026-10-28"
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {**MON, "DEMO.A": (49, 49, 45, 45)})  # Tuesday close breaches the stop
    w.at("2026-10-27T21:30:00")
    settle(w)
    # The still-pending partial sell must not suppress the stop for the remaining 1.498001 shares.
    rule = fixture_db.execute("SELECT * FROM orders WHERE origin='RULE' AND symbol='DEMO.A'").fetchall()
    assert len(rule) == 1 and rule[0]["status"] == "PENDING" and rule[0]["sell_all"] == 1
    assert rule[0]["target_session_date"] == "2026-10-28"


def test_hold_cannot_widen_or_remove_a_stop(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])  # stop 8%
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-26T21:30:00")
    settle(w)
    no_rules = {"stop_loss_pct": None, "take_profit_pct": None, "max_holding_sessions": None, "invalidation": ""}
    wider = {"stop_loss_pct": "0.20", "take_profit_pct": None, "max_holding_sessions": 30, "invalidation": "x"}
    tighter = {"stop_loss_pct": "0.05", "take_profit_pct": "0.15", "max_holding_sessions": 30, "invalidation": "x"}
    out = submit(w, [d("HOLD", "DEMO.A", exit_rules=no_rules)])
    assert out[0].status == "REJECTED" and "missing_exit_rule" in out[0].reasons and "stop_loosening_not_allowed" in out[0].reasons
    out = submit(w, [d("HOLD", "DEMO.A", exit_rules=wider)])
    assert out[0].status == "REJECTED" and "stop_loosening_not_allowed" in out[0].reasons
    out = submit(w, [d("HOLD", "DEMO.A", exit_rules=tighter)])
    assert out[0].status == "ACCEPTED"
    active = fixture_db.execute("SELECT * FROM position_rules WHERE symbol='DEMO.A' AND status='ACTIVE'").fetchall()
    assert len(active) == 1 and Decimal(active[0]["stop_loss_pct"]) == Decimal("0.05")
    assert active[0]["reference_price"] is not None and active[0]["entry_session_date"] == "2026-10-26"


def _bought_a(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="60")])  # 1.198801 sh for $60.00: ref 50.050008, 8% stop 46.046007
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-26T21:30:00")
    settle(w)
    return w


def test_add_on_buy_cannot_widen_the_stop(fixture_db):
    w = _bought_a(fixture_db)
    wide = {"stop_loss_pct": "0.20", "take_profit_pct": None, "max_holding_sessions": 30, "invalidation": "x"}
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="40", exit_rules=wide)])
    assert out[0].status == "REJECTED" and "stop_loosening_not_allowed" in out[0].reasons


def test_averaging_down_keeps_the_old_stop_level_as_a_floor(fixture_db):
    w = _bought_a(fixture_db)
    same = {"stop_loss_pct": "0.08", "take_profit_pct": None, "max_holding_sessions": 30, "invalidation": "x"}
    assert submit(w, [d("BUY", "DEMO.A", notional_usd="60", exit_rules=same)])[0].status == "ACCEPTED"
    # Tuesday opens at 47 (add-on fills there, average cost ~48.5, 8% stop would be ~44.6),
    # then closes at 46.00: below the CARRIED 46.046007 floor, so the stop must fire.
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {**MON, "DEMO.A": (47, 47.5, 45.5, 46)})
    w.at("2026-10-27T21:30:00")
    settle(w)
    active = fixture_db.execute("SELECT * FROM position_rules WHERE symbol='DEMO.A' AND status IN ('ACTIVE','TRIGGERED')").fetchall()
    assert Decimal(active[0]["stop_floor_price"]) == Decimal("46.046007")
    rule = fixture_db.execute("SELECT * FROM orders WHERE origin='RULE' AND symbol='DEMO.A'").fetchall()
    assert len(rule) == 1 and rule[0]["origin_note"].startswith("STOP_LOSS")


def test_hold_parity_with_batch_exit_and_pending_add_on(fixture_db):
    w = _bought_a(fixture_db)
    tighter = {"stop_loss_pct": "0.05", "take_profit_pct": None, "max_holding_sessions": 30, "invalidation": "x"}
    out = submit(w, [d("SELL", "DEMO.A", quantity="ALL", exit_rules=None), d("HOLD", "DEMO.A", exit_rules=tighter)])
    assert out[0].status == "ACCEPTED" and out[1].status == "REJECTED" and "duplicate_symbol_in_batch" in out[1].reasons


def test_hold_restatement_refused_while_add_on_entry_pending(fixture_db):
    w = _bought_a(fixture_db)
    same = {"stop_loss_pct": "0.08", "take_profit_pct": None, "max_holding_sessions": 30, "invalidation": "x"}
    assert submit(w, [d("BUY", "DEMO.A", notional_usd="30", exit_rules=same)])[0].status == "ACCEPTED"
    tighter = {"stop_loss_pct": "0.05", "take_profit_pct": None, "max_holding_sessions": 30, "invalidation": "x"}
    out = submit(w, [d("HOLD", "DEMO.A", exit_rules=tighter)])
    assert out[0].status == "REJECTED" and "entry_pending_for_symbol" in out[0].reasons


def test_hold_check_records_shares_as_shares_not_micro_units(fixture_db):
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")
    settle(w)
    out = submit(w, [d("HOLD", "DEMO.A")])
    obs = fixture_db.execute("SELECT observed FROM risk_checks WHERE decision_id=? AND check_name='no_position'",
                             (out[0].decision_id,)).fetchone()[0]
    assert obs == "1.998001"  # it was the raw micro-share integer "1998001", unlike the SELL path


def test_benchmark_that_cannot_fill_its_first_open_buys_at_the_next_open(fixture_db):
    """The passive benchmark's rule (invest everything at the first open after inception) was committed at
    inception. If that open cannot fill it (no data within the wait window), it is re-queued for the next
    open instead of expiring for good: an expired benchmark sat in cash forever and silently turned every
    comparison into advisor-vs-cash."""
    w = setup(fixture_db)

    def bench():
        return fixture_db.execute("SELECT * FROM orders WHERE experiment_id=? AND account='BENCHMARK' ORDER BY id", (w.exp_id,)).fetchall()
    first = bench()[0]
    assert first["target_session_date"] == "2026-10-26"
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", MON)  # Tuesday has data; Monday never gets any
    w.at("2026-11-02T15:00:00")  # past Monday's wait window
    settle(w)
    rows = bench()
    assert rows[0]["status"] == "EXPIRED" and rows[0]["status_reason"] == "session_data_unavailable_timeout"
    assert len(rows) == 2 and rows[1]["status"] == "FILLED" and rows[1]["target_session_date"] == "2026-10-27"
    f = fixture_db.execute("SELECT * FROM fills WHERE order_id=?", (rows[1]["id"],)).fetchone()
    # Same commitment instant as the original rule (no lookahead), filled at the next open with data.
    assert f["session_date"] == "2026-10-27" and f["decided_at"] == first["decided_at"]
    assert accounting.load_state(fixture_db, w.exp_id, "BENCHMARK").cash_cents < 50000
