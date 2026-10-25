"""Money and quantity arithmetic.

Every rounding rule here is deliberately conservative: when a rounding choice exists, the simulated
account is never the side that benefits (buys round quantity down and cost up; sells round proceeds
down). The hand-calculated example in README.md uses exactly these functions.
"""
from decimal import Decimal

import pytest

from paper_advisor import money as m


def test_parse_usd_is_exact_and_rejects_sub_cent_amounts():
    assert m.parse_usd("500") == 50000
    assert m.parse_usd("500.00") == 50000
    assert m.parse_usd("5.1") == 510
    with pytest.raises(ValueError):
        m.parse_usd("1.005")  # sub-cent amounts are an input error, never silently rounded
    with pytest.raises(ValueError):
        m.parse_usd("-1")
    with pytest.raises(ValueError):
        m.parse_usd("abc")


def test_parse_qty_supports_six_decimals_exactly():
    assert m.parse_qty("1") == 1_000_000
    assert m.parse_qty("0.000001") == 1
    assert m.parse_qty("1.998001") == 1_998_001
    with pytest.raises(ValueError):
        m.parse_qty("0.0000001")  # 7 decimals exceeds the documented precision
    with pytest.raises(ValueError):
        m.parse_qty("0")


def test_format_helpers_round_trip():
    assert m.cents_to_str(50000) == "500.00"
    assert m.cents_to_str(-1234) == "-12.34"
    assert m.fmt_usd(-1234) == "-$12.34"
    assert m.fmt_usd(5) == "$0.05"
    assert m.qty_to_str(1_998_001) == "1.998001"
    assert m.qty_to_str(2_000_000) == "2.000000"


def test_buy_fill_price_adds_slippage_and_rounds_up():
    # 10 bps adverse on a $50.00 open -> $50.05 exactly
    assert m.buy_fill_price(Decimal("50"), Decimal("10")) == Decimal("50.050000")
    # 123.4567 * 1.001 = 123.5801567 -> rounded UP to 6 dp
    assert m.buy_fill_price(Decimal("123.4567"), Decimal("10")) == Decimal("123.580157")


def test_sell_fill_price_subtracts_slippage_and_rounds_down():
    assert m.sell_fill_price(Decimal("51"), Decimal("10")) == Decimal("50.949000")
    # 123.4567 * 0.999 = 123.3332433 -> rounded DOWN to 6 dp
    assert m.sell_fill_price(Decimal("123.4567"), Decimal("10")) == Decimal("123.333243")


def test_buy_quantity_floors_so_cost_never_exceeds_notional():
    price = Decimal("50.05")
    qty = m.buy_quantity(10000, price)
    assert qty == 1_998_001  # 100 / 50.05 = 1.998001998... floored to 6 dp
    cost = m.buy_cost_cents(qty, price)
    assert cost == 10000  # 99.99995005 rounded UP to the cent
    assert cost <= 10000


@pytest.mark.parametrize("notional_cents,price", [
    (500, "0.07"), (1999, "333.333333"), (12500, "759.97"), (10001, "1.000001"), (500, "4999.99"),
])
def test_buy_cost_never_exceeds_notional_for_awkward_prices(notional_cents, price):
    p = Decimal(price)
    qty = m.buy_quantity(notional_cents, p)
    assert qty >= 0
    assert m.buy_cost_cents(qty, p) <= notional_cents


def test_sell_proceeds_round_down_to_the_cent():
    assert m.sell_proceeds_cents(1_998_001, Decimal("50.949")) == 10179  # 101.796152949 -> 101.79


def test_market_value_rounds_half_even():
    assert m.market_value_cents(1_998_001, Decimal("52")) == 10390  # 103.896052 -> 103.90
    assert m.market_value_cents(1_000_000, Decimal("0.125")) == 12  # 0.125 -> 0.12 (half-even)
    assert m.market_value_cents(1_000_000, Decimal("0.135")) == 14  # 0.135 -> 0.14 (half-even)


def test_commission_rounds_up():
    assert m.commission_cents(10000, per_order_cents=0, bps=Decimal("0")) == 0
    assert m.commission_cents(10000, per_order_cents=100, bps=Decimal("0")) == 100
    assert m.commission_cents(10001, per_order_cents=0, bps=Decimal("1")) == 2  # 1.0001 cents -> 2


def test_dividend_cash_rounds_down():
    assert m.dividend_cents(1_998_001, Decimal("0.24")) == 47  # 0.47952024 -> 0.47


def test_split_quantity_is_exact_for_integer_ratios_and_floors_otherwise():
    assert m.split_quantity(1_998_001, 2, 1) == 3_996_002
    assert m.split_quantity(1_000_000, 3, 2) == 1_500_000
    assert m.split_quantity(1, 1, 3) == 0  # reverse split dust floors; caller records cash-in-lieu


def test_pct_and_bps_helpers():
    assert m.ratio(Decimal("503.90"), Decimal("500")) == Decimal("1.0078")
    assert m.bps_to_fraction(Decimal("10")) == Decimal("0.001")
