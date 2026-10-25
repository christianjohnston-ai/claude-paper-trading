"""Ledger replay and accounting, including the hand-calculated example documented in README.md.

Hand-calculated example (all numbers derivable with a pencil):
  Start: $500.00 cash.
  BUY $100.00 notional at an open of $50.00 with 10 bps adverse slippage:
    fill price = 50.00 * 1.001 = 50.05
    quantity   = floor_6dp(100 / 50.05) = 1.998001 shares
    cost       = ceil_cent(1.998001 * 50.05 = 99.99995005) = $100.00 -> cash $400.00, basis $100.00
  Mark at a $52.00 close: value = 1.998001 * 52 = 103.896052 -> $103.90; unrealized +$3.90; equity $503.90
  SELL ALL at a $51.00 open with 10 bps slippage:
    fill price = 51.00 * 0.999 = 50.949
    proceeds   = floor_cent(1.998001 * 50.949 = 101.796152949) = $101.79
    realized   = 101.79 - 100.00 = +$1.79; cash = $501.79; equity $501.79 (+0.358%)
"""
from decimal import Decimal

import pytest

from paper_advisor import accounting, clock, ledger
from paper_advisor import money as m


def _exp(conn):
    now = clock.iso(clock.now_utc())
    cur = conn.execute(
        "INSERT INTO experiments(name, mode, starting_capital_cents, created_at, inception_at, status, benchmark_symbol)"
        " VALUES ('t','fixture',50000,?,?,'active','SPY')", (now, now))
    return cur.lastrowid


T0 = "2026-10-24T05:00:00.000000Z"
T1 = "2026-10-26T13:30:00.000000Z"
T2 = "2026-10-27T13:30:00.000000Z"


def _fund(conn, exp_id):
    with ledger.tx(conn):
        ledger.append_event(conn, exp_id, "PORTFOLIO", "INITIAL_FUNDING", effective_at=T0, recorded_at=T0,
                            cash_delta_cents=50000, idempotency_key=f"funding:{exp_id}:PORTFOLIO")


def _buy(conn, exp_id, symbol, qty, cost, fee=0, when=T1, key=None, price="50.05"):
    with ledger.tx(conn):
        return ledger.append_event(conn, exp_id, "PORTFOLIO", "BUY_FILL", effective_at=when, recorded_at=when,
                                   session_date=when[:10], symbol=symbol, quantity_delta_micro=qty,
                                   cash_delta_cents=-(cost + fee), fee_cents=fee, price=price,
                                   idempotency_key=key or f"buy:{symbol}:{when}:{qty}")


def _sell(conn, exp_id, symbol, qty, proceeds, fee=0, when=T2, key=None, price="50.949"):
    with ledger.tx(conn):
        return ledger.append_event(conn, exp_id, "PORTFOLIO", "SELL_FILL", effective_at=when, recorded_at=when,
                                   session_date=when[:10], symbol=symbol, quantity_delta_micro=-qty,
                                   cash_delta_cents=proceeds - fee, fee_cents=fee, price=price,
                                   idempotency_key=key or f"sell:{symbol}:{when}:{qty}")


def test_hand_calculated_example(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    qty = m.buy_quantity(10000, m.buy_fill_price(Decimal("50"), Decimal("10")))
    cost = m.buy_cost_cents(qty, Decimal("50.05"))
    assert (qty, cost) == (1_998_001, 10000)
    _buy(fixture_db, exp_id, "XYZ", qty, cost)

    st = accounting.load_state(fixture_db, exp_id, "PORTFOLIO")
    assert st.cash_cents == 40000
    assert st.positions == {"XYZ": 1_998_001}
    v = accounting.valuation(st, {"XYZ": Decimal("52")})
    assert (v.market_value_cents, v.unrealized_cents, v.equity_cents) == (10390, 390, 50390)

    sell_px = m.sell_fill_price(Decimal("51"), Decimal("10"))
    proceeds = m.sell_proceeds_cents(qty, sell_px)
    assert proceeds == 10179
    _sell(fixture_db, exp_id, "XYZ", qty, proceeds)

    st = accounting.load_state(fixture_db, exp_id, "PORTFOLIO")
    assert st.cash_cents == 50179
    assert st.positions == {}
    assert st.realized_cents == 179
    v = accounting.valuation(st, {})
    assert v.equity_cents == 50179
    assert len(st.closed_trades) == 1
    t = st.closed_trades[0]
    assert t.realized_cents == 179 and t.symbol == "XYZ"


def test_equity_identity_holds(fixture_db):
    """equity - funded == realized + unrealized + dividends (fees live inside basis/proceeds)."""
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    _buy(fixture_db, exp_id, "AAA", 2_000_000, 10010, fee=25, key="b1")
    _buy(fixture_db, exp_id, "BBB", 1_500_000, 7500, key="b2")
    _sell(fixture_db, exp_id, "AAA", 500_000, 2600, fee=10, key="s1")
    with ledger.tx(fixture_db):
        ledger.append_event(fixture_db, exp_id, "PORTFOLIO", "CASH_DIVIDEND", effective_at=T2, recorded_at=T2,
                            session_date=T2[:10], symbol="BBB", cash_delta_cents=37, idempotency_key="d1")
    st = accounting.load_state(fixture_db, exp_id, "PORTFOLIO")
    v = accounting.valuation(st, {"AAA": Decimal("51.10"), "BBB": Decimal("4.98")})
    assert v.equity_cents - st.funded_cents == st.realized_cents + v.unrealized_cents + st.dividends_cents


def test_fifo_lot_relief_and_partial_sells(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    _buy(fixture_db, exp_id, "AAA", 1_000_000, 1000, key="b1")   # 1 share basis $10.00
    _buy(fixture_db, exp_id, "AAA", 1_000_000, 2000, key="b2")   # 1 share basis $20.00
    _sell(fixture_db, exp_id, "AAA", 1_500_000, 4500, key="s1")  # sells lot1 fully + half of lot2
    st = accounting.load_state(fixture_db, exp_id, "PORTFOLIO")
    # basis relieved = 1000 + 2000*0.5 = 2000 -> realized = 4500 - 2000 = 2500
    assert st.realized_cents == 2500
    lots = st.lots["AAA"]
    assert [(l.qty_micro, l.basis_cents) for l in lots] == [(500_000, 1000)]
    assert st.closed_trades == []  # position still open


def test_split_scales_lots_and_keeps_basis(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    _buy(fixture_db, exp_id, "AAA", 1_234_567, 10000, key="b1")
    with ledger.tx(fixture_db):
        ledger.append_event(fixture_db, exp_id, "PORTFOLIO", "SPLIT", effective_at=T2, recorded_at=T2,
                            session_date=T2[:10], symbol="AAA", quantity_delta_micro=1_234_567,
                            idempotency_key="split1", split_num=2, split_den=1)
    st = accounting.load_state(fixture_db, exp_id, "PORTFOLIO")
    assert st.positions["AAA"] == 2_469_134
    assert st.lots["AAA"][0].basis_cents == 10000


def test_split_event_must_match_lot_math(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    _buy(fixture_db, exp_id, "AAA", 1_000_000, 10000, key="b1")
    with ledger.tx(fixture_db):
        ledger.append_event(fixture_db, exp_id, "PORTFOLIO", "SPLIT", effective_at=T2, recorded_at=T2,
                            session_date=T2[:10], symbol="AAA", quantity_delta_micro=999,  # not a valid ratio
                            idempotency_key="split-bad", split_num=2, split_den=1)
    with pytest.raises(accounting.ReplayError):
        accounting.load_state(fixture_db, exp_id, "PORTFOLIO")


def test_overspend_and_oversell_are_rejected_at_append(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    with pytest.raises(ledger.LedgerError):
        _buy(fixture_db, exp_id, "AAA", 1_000_000, 50001, key="too-big")
    _buy(fixture_db, exp_id, "AAA", 1_000_000, 1000, key="ok")
    with pytest.raises(ledger.LedgerError):
        _sell(fixture_db, exp_id, "AAA", 1_000_001, 1000, key="oversell")
    with pytest.raises(ledger.LedgerError):
        _sell(fixture_db, exp_id, "ZZZ", 1, 1, key="short")  # no position -> would be a short


def test_idempotency_key_prevents_duplicates(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    a = _buy(fixture_db, exp_id, "AAA", 1_000_000, 1000, key="same")
    b = _buy(fixture_db, exp_id, "AAA", 1_000_000, 1000, key="same")
    assert a == b
    st = accounting.load_state(fixture_db, exp_id, "PORTFOLIO")
    assert st.positions["AAA"] == 1_000_000


def test_idempotency_key_reuse_with_different_payload_is_an_error(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    _buy(fixture_db, exp_id, "AAA", 1_000_000, 1000, key="same")
    with pytest.raises(ledger.LedgerError):
        _buy(fixture_db, exp_id, "AAA", 2_000_000, 2000, key="same")


def test_adjustment_reversal_is_an_explicit_corrective_event(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    with ledger.tx(fixture_db):
        bad = ledger.append_event(fixture_db, exp_id, "PORTFOLIO", "CASH_DIVIDEND", effective_at=T2, recorded_at=T2,
                                  session_date=T2[:10], symbol="AAA", cash_delta_cents=500, idempotency_key="d-bad")
    with ledger.tx(fixture_db):
        ledger.reverse_event(fixture_db, exp_id, "PORTFOLIO", bad, recorded_at=T2, memo="dividend amount was revised")
    st = accounting.load_state(fixture_db, exp_id, "PORTFOLIO")
    assert st.cash_cents == 50000 and st.dividends_cents == 0
    n = fixture_db.execute("SELECT COUNT(*) FROM ledger_events WHERE experiment_id=?", (exp_id,)).fetchone()[0]
    assert n == 3  # funding + bad dividend + reversal; nothing was edited or deleted


def test_accounts_are_isolated(fixture_db):
    exp_id = _exp(fixture_db)
    _fund(fixture_db, exp_id)
    with ledger.tx(fixture_db):
        ledger.append_event(fixture_db, exp_id, "BENCHMARK", "INITIAL_FUNDING", effective_at=T0, recorded_at=T0,
                            cash_delta_cents=50000, idempotency_key=f"funding:{exp_id}:BENCHMARK")
    _buy(fixture_db, exp_id, "AAA", 1_000_000, 1000, key="p")
    bench = accounting.load_state(fixture_db, exp_id, "BENCHMARK")
    assert bench.cash_cents == 50000 and bench.positions == {}
