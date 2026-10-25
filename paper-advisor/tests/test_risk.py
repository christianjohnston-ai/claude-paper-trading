"""Deterministic batch validation of proposed orders against the experiment's limits.

All limits are evaluated across the WHOLE proposed batch plus orders already pending from earlier
runs, with cash reservations, because the brief requires validating "the entire proposed batch,
including outstanding orders and reserved cash".

Default limits ($500 equity): max position 25% = $125; cash reserve 10% = $50 must remain after all
planned buys; 5 open positions; 3 new advisor orders per session; $5 minimum order.
"""
from paper_advisor import orders, submission

from helpers import World

FRESH = {"DEMO.SPY": 100, "DEMO.A": 50, "DEMO.B": 40, "DEMO.C": 30, "DEMO.D": 20, "DEMO.E": 10, "DEMO.F": 25}


def d(action, symbol=None, **kw):
    base = {
        "action": action, "symbol": symbol, "thesis": f"test thesis for {symbol}", "counterargument": "test counter",
        "horizon_sessions": 20, "horizon_text": "about a month",
        "exit_rules": {"stop_loss_pct": "0.08", "take_profit_pct": None, "max_holding_sessions": 30,
                       "invalidation": "close below the 50-day average"},
        "confidence": "0.55", "uncertainty": "high", "key_risks": "market drawdown", "evidence_ids": [], "skills_used": [],
    }
    base.update(kw)
    return base


def world_with_fresh_data(fixture_db):
    w = World(fixture_db, start="2026-10-23T23:00:00")  # Friday evening (after close+buffer)
    w.bars_all("2026-10-23", FRESH)
    w.at("2026-10-24T05:00:00")  # Saturday: last completed session = Friday 10-23
    return w


def submit(w, decisions):
    run_id = w.new_run()
    return submission.apply_decisions(w.conn, w.exp_id, run_id, decisions, now=w.clock())


def statuses(outcomes):
    return [(o.symbol, o.status, tuple(o.reasons)) for o in outcomes]


def test_position_cap_rejects_oversized_buy(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="125.01")])
    assert out[0].status == "REJECTED" and "max_position_pct" in out[0].reasons
    ok = submit(w, [d("BUY", "DEMO.A", notional_usd="125.00")])
    assert ok[0].status == "ACCEPTED"


def test_batch_cannot_overspend_or_break_cash_reserve(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="125"), d("BUY", "DEMO.B", notional_usd="125"),
                     d("BUY", "DEMO.C", notional_usd="125")])
    assert [o.status for o in out] == ["ACCEPTED"] * 3  # $375 reserved, $125 cash left >= $50 reserve
    # Next session: $125 cash left, reserve needs $50 -> at most $75 more can be committed.
    w.at("2026-10-26T13:00:00")  # Monday pre-open; the three buys still pending for this same open
    out2 = submit(w, [d("BUY", "DEMO.D", notional_usd="80")])
    assert out2[0].status == "REJECTED"
    assert "max_new_orders_per_session" in out2[0].reasons  # 4th advisor order for the Monday open
    assert "cash_reserve" in out2[0].reasons


def test_pending_reservations_from_earlier_runs_count(fixture_db):
    w = world_with_fresh_data(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="125"), d("BUY", "DEMO.B", notional_usd="125")])
    assert orders.reserved_cash_cents(fixture_db, w.exp_id) == 25000
    out = submit(w, [d("BUY", "DEMO.C", notional_usd="125")])  # third order, 3rd slot, $250 -> $125 left
    assert out[0].status == "ACCEPTED"
    assert orders.reserved_cash_cents(fixture_db, w.exp_id) == 37500


def test_max_new_orders_per_session(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("BUY", s, notional_usd="20") for s in ("DEMO.A", "DEMO.B", "DEMO.C", "DEMO.D")])
    assert [o.status for o in out] == ["ACCEPTED", "ACCEPTED", "ACCEPTED", "REJECTED"]
    assert "max_new_orders_per_session" in out[3].reasons


def test_minimum_order_and_cent_precision(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="4.99"), d("BUY", "DEMO.B", notional_usd="10.005")])
    assert out[0].status == "REJECTED" and "min_order" in out[0].reasons
    assert out[1].status == "REJECTED" and "invalid_amount" in out[1].reasons


def test_no_shorts_and_sell_quantity_limits(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("SELL", "DEMO.A", quantity="1")])
    assert out[0].status == "REJECTED" and "no_position" in out[0].reasons


def test_stale_data_blocks_buys(fixture_db):
    w = World(fixture_db, start="2026-10-22T23:00:00")
    w.bars_all("2026-10-22", FRESH)  # Thursday bars only
    w.at("2026-10-24T05:00:00")      # Saturday: Friday 10-23 is the last completed session -> data is stale
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="50")])
    assert out[0].status == "REJECTED" and "stale_data" in out[0].reasons


def test_suspect_data_blocks_buys(fixture_db):
    w = World(fixture_db, start="2026-10-23T23:00:00")
    w.bars_all("2026-10-23", {k: v for k, v in FRESH.items() if k != "DEMO.A"})
    w.bar("DEMO.A", "2026-10-23", 50, flags=["unexplained_jump"])
    w.at("2026-10-24T05:00:00")
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="50")])
    assert out[0].status == "REJECTED" and "suspect_data" in out[0].reasons


def test_universe_and_exit_rule_requirements(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("BUY", "TQQQ", notional_usd="50"),
                     d("BUY", "DEMO.A", notional_usd="50", exit_rules={"stop_loss_pct": None, "take_profit_pct": None,
                                                                       "max_holding_sessions": None, "invalidation": ""})])
    assert out[0].status == "REJECTED" and "not_in_universe" in out[0].reasons
    assert out[1].status == "REJECTED" and "missing_exit_rule" in out[1].reasons


def test_duplicate_symbol_in_batch_is_rejected(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="50"), d("BUY", "DEMO.A", notional_usd="50")])
    assert out[0].status == "ACCEPTED"
    assert out[1].status == "REJECTED" and "duplicate_symbol_in_batch" in out[1].reasons


def test_decision_after_open_targets_next_session(fixture_db):
    w = world_with_fresh_data(fixture_db)
    w.at("2026-10-26T14:00:00")  # Monday 10:00 ET, market already open
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="50")])
    assert out[0].status == "ACCEPTED"
    assert out[0].target_session == "2026-10-27"


def test_cancel_only_before_target_open(fixture_db):
    w = world_with_fresh_data(fixture_db)
    first = submit(w, [d("BUY", "DEMO.A", notional_usd="50")])
    oid = first[0].order_id
    out = submit(w, [d("CANCEL", "DEMO.A", cancel_order_id=oid)])
    assert out[0].status == "ACCEPTED"
    assert orders.get(fixture_db, oid)["status"] == "CANCELED"
    again = submit(w, [d("BUY", "DEMO.B", notional_usd="50")])
    w.at("2026-10-26T13:31:00")  # target open has passed
    late = submit(w, [d("CANCEL", "DEMO.B", cancel_order_id=again[0].order_id)])
    assert late[0].status == "REJECTED" and "target_session_already_open" in late[0].reasons


def test_hold_and_no_trade_are_recorded_without_orders(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("NO_TRADE", None, exit_rules=None)])
    assert out[0].status == "RECORDED" and out[0].order_id is None


def test_every_decision_and_check_is_persisted_before_any_execution(fixture_db):
    w = world_with_fresh_data(fixture_db)
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="125.01"), d("BUY", "DEMO.B", notional_usd="50")])
    n_dec = fixture_db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    n_checks = fixture_db.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0]
    n_fills = fixture_db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert n_dec == 2 and n_checks > 4 and n_fills == 0
    rej = orders.get(fixture_db, out[0].order_id)
    assert rej["status"] == "REJECTED"


def test_buy_too_small_to_fill_after_commission_is_rejected(fixture_db):
    """broker_sim pays commission out of the order and expires an order whose cost is below the minimum,
    so with a $1.00 commission a $5.99 buy could only ever expire at the open: refuse it up front."""
    from paper_advisor import broker_sim
    from test_broker_sim import MON
    w = World(fixture_db, start="2026-10-23T23:00:00", config_params={"commission_per_order_usd": "1.00"})
    w.bars_all("2026-10-23", FRESH)
    w.cover_corporate_actions("2026-12-31")
    w.at("2026-10-24T05:00:00")
    out = submit(w, [d("BUY", "DEMO.A", notional_usd="5.99")])
    assert out[0].status == "REJECTED" and "min_order" in out[0].reasons
    ok = submit(w, [d("BUY", "DEMO.B", notional_usd="6.00")])
    assert ok[0].status == "ACCEPTED"
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")
    broker_sim.settle(fixture_db, w.exp_id, now=w.clock())
    assert orders.get(fixture_db, ok[0].order_id)["status"] == "FILLED"  # the smallest accepted buy really fills
