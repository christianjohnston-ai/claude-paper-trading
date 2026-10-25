"""Experiment creation (exactly-once $500 default), prospective config versioning."""
from decimal import Decimal

import pytest

from paper_advisor import accounting, clock, config, db, experiments, orders

from helpers import utc


def test_default_experiment_is_created_exactly_once(forward_db):
    clock.set_override(lambda: utc("2026-10-24T05:00:00"))
    exp_id, created = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    assert created is True
    again, created2 = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    assert (again, created2) == (exp_id, False)
    assert forward_db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
    row = experiments.get(forward_db, exp_id)
    assert row["starting_capital_cents"] == 50000 and row["mode"] == "forward" and row["benchmark_symbol"] == "SPY"
    port = accounting.load_state(forward_db, exp_id, "PORTFOLIO")
    bench = accounting.load_state(forward_db, exp_id, "BENCHMARK")
    assert (port.cash_cents, port.positions, port.funded_cents) == (50000, {}, 50000)
    assert bench.cash_cents == 50000
    n_funding = forward_db.execute("SELECT COUNT(*) FROM ledger_events WHERE event_type='INITIAL_FUNDING'").fetchone()[0]
    assert n_funding == 2  # one per account, never more


def test_benchmark_buy_is_queued_for_first_open_after_inception(forward_db):
    clock.set_override(lambda: utc("2026-10-24T05:00:00"))  # Saturday
    exp_id, _ = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    pend = orders.pending(forward_db, exp_id, account="BENCHMARK")
    assert len(pend) == 1
    o = pend[0]
    assert (o["symbol"], o["side"], o["notional_cents"], o["target_session_date"], o["origin"]) == (
        "SPY", "BUY", 50000, "2026-10-26", "BENCHMARK")
    exp = experiments.get(forward_db, exp_id)
    assert exp["benchmark_start_session"] == "2026-10-26"


def test_default_config_matches_the_brief(forward_db):
    clock.set_override(lambda: utc("2026-10-24T05:00:00"))
    exp_id, _ = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    vid, cfg = config.current(forward_db, exp_id)
    assert cfg.min_order_cents == 500
    assert cfg.max_position_pct == Decimal("0.25")
    assert cfg.min_cash_reserve_pct == Decimal("0.10")
    assert cfg.max_open_positions == 5
    assert cfg.max_new_orders_per_session == 3
    assert cfg.commission_per_order_cents == 0 and cfg.commission_bps == Decimal("0")
    assert cfg.slippage_bps == Decimal("10")
    assert cfg.qty_decimals == 6
    assert cfg.display_timezone == "America/Los_Angeles" and cfg.market_timezone == "America/New_York"
    assert cfg.deposits_withdrawals_enabled is False


def test_config_change_is_prospective_and_versioned(forward_db):
    clock.set_override(lambda: utc("2026-10-24T05:00:00"))
    exp_id, _ = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    clock.set_override(lambda: utc("2026-10-25T05:00:00"))
    with db.transaction(forward_db):
        config.create_version(forward_db, exp_id, {"max_position_pct": "0.20"}, created_by="test",
                              reason="tighter cap", now=clock.now_iso())
    v_now, cfg_now = config.current(forward_db, exp_id)
    assert cfg_now.max_position_pct == Decimal("0.20")
    v_then, cfg_then = config.current(forward_db, exp_id, at_iso="2026-10-24T06:00:00.000000Z")
    assert cfg_then.max_position_pct == Decimal("0.25") and v_then != v_now
    assert forward_db.execute("SELECT COUNT(*) FROM config_versions WHERE experiment_id=?", (exp_id,)).fetchone()[0] == 2


@pytest.mark.parametrize("bad", [
    {"max_position_pct": "1.5"}, {"min_cash_reserve_pct": "-0.1"}, {"max_open_positions": 0},
    {"slippage_bps": "-1"}, {"deposits_withdrawals_enabled": True}, {"unknown_key": 1}, {"min_order_usd": "1.001"},
])
def test_invalid_config_is_rejected(bad):
    with pytest.raises(config.ConfigError):
        config.normalize(bad)


def test_new_capital_creates_a_new_experiment_and_keeps_history(forward_db):
    clock.set_override(lambda: utc("2026-10-24T05:00:00"))
    first, _ = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    second = experiments.create_experiment(forward_db, name="$1,000 experiment", starting_capital_cents=100000,
                                           now=clock.now_utc(), created_by="test")
    assert second != first
    assert experiments.active_experiment_id(forward_db) == second
    assert accounting.load_state(forward_db, first, "PORTFOLIO").cash_cents == 50000
    assert accounting.load_state(forward_db, second, "PORTFOLIO").cash_cents == 100000
    assert experiments.get(forward_db, first)["status"] == "archived"


def test_starting_capital_cannot_be_edited(forward_db):
    import sqlite3
    clock.set_override(lambda: utc("2026-10-24T05:00:00"))
    exp_id, _ = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    with pytest.raises(sqlite3.IntegrityError):
        forward_db.execute("UPDATE experiments SET starting_capital_cents=100000 WHERE id=?", (exp_id,))


def test_reactivating_an_archived_experiment_keeps_statuses_consistent(fixture_db):
    """"Make active" moved only the active pointer, so the newer experiment kept status 'active' while the
    pointer named the older one; every screen that shows a status then contradicted the active one."""
    from paper_advisor import clock as clk
    from helpers import World
    w = World(fixture_db, start="2026-10-23T23:00:00")
    second = experiments.create_experiment(fixture_db, name="second", starting_capital_cents=100000, now=clk.now_utc(),
                                           created_by="test", config_params={"benchmark_symbol": "DEMO.SPY"})
    experiments.set_active(fixture_db, w.exp_id)
    statuses = {r["id"]: r["status"] for r in experiments.all_experiments(fixture_db)}
    assert experiments.active_experiment_id(fixture_db) == w.exp_id
    assert statuses == {w.exp_id: "active", second: "archived"}


def test_reactivated_experiment_catches_up_like_a_missed_day(fixture_db):
    """While archived nothing settles for an experiment; made active again, settlement resumes where it
    stopped: its pending benchmark order fills at its ORIGINAL open (decided before it, no lookahead)."""
    from helpers import World
    from test_broker_sim import MON
    from test_risk import FRESH
    from paper_advisor import broker_sim, orders as orders_mod
    w = World(fixture_db, start="2026-10-23T23:00:00")
    w.bars_all("2026-10-23", FRESH)
    w.cover_corporate_actions("2026-12-31")
    first = w.exp_id
    w.at("2026-10-24T05:00:00")
    second = experiments.create_experiment(fixture_db, name="second", starting_capital_cents=100000, now=w.clock(),
                                           created_by="test", config_params={"benchmark_symbol": "DEMO.SPY"})
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T13:00:00")
    experiments.set_active(fixture_db, first)
    broker_sim.settle(fixture_db, first, now=w.clock())
    bench = fixture_db.execute("SELECT * FROM orders WHERE experiment_id=? AND account='BENCHMARK'", (first,)).fetchone()
    assert bench["status"] == "FILLED"
    fill = fixture_db.execute("SELECT * FROM fills WHERE order_id=?", (bench["id"],)).fetchone()
    assert fill["session_date"] == "2026-10-26"  # the original target open, not a later one
    assert second != first and not orders_mod.pending(fixture_db, first)


def test_smallest_tradable_capital_follows_the_limits():
    """A buy must be at least the minimum order, fit inside the max position and leave the cash reserve,
    so with the defaults ($5 minimum, 25% max position) no experiment under $20.00 could ever buy."""
    assert config.min_tradable_capital_cents({}) == 2000
    assert config.min_tradable_capital_cents({"max_position_pct": "1"}) == 556  # the 10% reserve binds: ceil(500 / 0.9)
    # Commission is paid out of the order, and an order worth less than the minimum after it expires at the
    # fill, so the smallest buy that can fill is larger than the minimum.
    assert config.min_tradable_capital_cents({"commission_per_order_usd": "1.00"}) == 2400  # (500 + 100) / 0.25
    assert config.min_tradable_capital_cents({"commission_bps": "100"}) == 2024  # ceil(500 / 0.99) = 506; 506 / 0.25


def test_an_experiment_too_small_to_ever_buy_is_refused(forward_db):
    before = forward_db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    with pytest.raises(experiments.CapitalTooSmall, match=r"\$20\.00"):
        experiments.create_experiment(forward_db, name="tiny", starting_capital_cents=1999, now=clock.now_utc(), created_by="test")
    assert forward_db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == before  # nothing half-created
    experiments.create_experiment(forward_db, name="smallest", starting_capital_cents=2000, now=clock.now_utc(), created_by="test")


def test_experiments_cannot_switch_or_be_created_while_a_run_holds_the_lock(forward_db):
    """Switching the active experiment (or creating one, which archives it) mid-run would let the run's decisions
    land on an experiment that is no longer active. Refused in the experiments module, so CLI and dashboard agree."""
    from paper_advisor import runs
    first, _ = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    second = experiments.create_experiment(forward_db, name="second", starting_capital_cents=100000, now=clock.now_utc(),
                                           created_by="test")
    with db.transaction(forward_db):
        rid = runs.create_run_row(forward_db, second, "morning", "2026-10-26", version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(forward_db, second, rid, clock.now_utc(), 180)
    with pytest.raises(experiments.RunInProgress, match=f"run #{rid}"):
        experiments.set_active(forward_db, first)
    with pytest.raises(experiments.RunInProgress):
        experiments.create_experiment(forward_db, name="third", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    assert experiments.active_experiment_id(forward_db) == second


def test_tradability_fixes_name_the_one_field_changes_that_let_an_account_buy():
    """The fixes offered next to a "no buy can pass" warning: each is the nearest value of ONE field that lets this
    equity place a buy, and only fields that actually bind are offered."""
    # $200 minimum on $500: the 25% position cap ($125) binds, the reserve does not.
    assert config.tradability_fixes({"min_order_usd": "200.00"}, 50000) == {"min_order_usd": "125.00", "max_position_pct": "0.40"}
    # A commission raises the smallest fillable buy to $201: ceil(201 / 500 * 100) / 100 = 0.41.
    assert config.tradability_fixes({"min_order_usd": "200.00", "commission_per_order_usd": "1.00"}, 50000) == {
        "min_order_usd": "124.00", "max_position_pct": "0.41"}
    # $480 minimum with a 100% cap on $500: the 10% reserve binds ($450 of room), the cap does not.
    assert config.tradability_fixes({"min_order_usd": "480.00", "max_position_pct": "1"}, 50000) == {
        "min_order_usd": "450.00", "min_cash_reserve_pct": "0.04"}
    assert config.tradability_fixes({}, 50000) == {}  # the account can already buy
