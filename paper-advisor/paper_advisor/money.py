"""Exact money and share-quantity arithmetic for the simulated account.

Units. SYNC WARNING: these units are baked into every SQLite column named *_cents or *_micro
(ledger_events, orders, fills, valuation snapshots). Changing a unit requires a migration that
rewrites every stored row; do not change them casually.
- cash: integer cents (USD)
- share quantities: integer micro-shares (1 share = 1_000_000), i.e. 6 decimal places
- prices: Decimal; simulated fill prices are quantized to 6 decimal places (micro-dollars)

Rounding policy. The simulated account must never be the side that benefits from a rounding choice,
otherwise thousands of tiny favorable roundings could manufacture a fake edge:
- buy fill price   = open * (1 + slippage), rounded UP to 6 dp
- sell fill price  = open * (1 - slippage), rounded DOWN to 6 dp
- buy quantity     = notional / buy fill price, rounded DOWN to 6 dp, so cost <= notional
- buy cost         = quantity * price, rounded UP to the cent
- sell proceeds    = quantity * price, rounded DOWN to the cent
- commission       = rounded UP to the cent
- dividend cash    = rounded DOWN to the cent
- mark-to-market   = HALF_EVEN to the cent (a valuation, not a cash event, so neutral rounding)

Integer math is used for every cash event so the results are exact and reproducible across
machines; Decimal is only used where a decimal price must be multiplied.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

QTY_SCALE = 1_000_000
QTY_DECIMALS = 6
PRICE_QUANT = Decimal("0.000001")
CENT = Decimal("0.01")
_PREC = 50  # far above any magnitude we use (prices < 1e7, quantities < 1e12 micro-shares)


def _dec(value) -> Decimal:
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a decimal number: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return d


def parse_usd(value) -> int:
    """Parse a non-negative USD amount into integer cents. Sub-cent input is rejected, not rounded,
    because silently rounding a user- or model-supplied amount would change what was decided."""
    d = _dec(value)
    if d < 0:
        raise ValueError(f"amount must be >= 0: {value!r}")
    if d != d.quantize(CENT):
        raise ValueError(f"amount has more than 2 decimal places: {value!r}")
    with localcontext() as ctx:
        ctx.prec = _PREC
        return int(d * 100)


def parse_qty(value) -> int:
    """Parse a positive share quantity into integer micro-shares (max 6 decimal places)."""
    d = _dec(value)
    if d <= 0:
        raise ValueError(f"quantity must be > 0: {value!r}")
    with localcontext() as ctx:
        ctx.prec = _PREC
        scaled = d * QTY_SCALE
        if scaled != scaled.to_integral_value():
            raise ValueError(f"quantity has more than {QTY_DECIMALS} decimal places: {value!r}")
        return int(scaled)


def parse_price(value) -> Decimal:
    d = _dec(value)
    if d <= 0:
        raise ValueError(f"price must be > 0: {value!r}")
    return d


def cents_to_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    a = abs(int(cents))
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    a = abs(int(cents))
    return f"{sign}${a // 100:,}.{a % 100:02d}"


def cents_to_decimal(cents: int) -> Decimal:
    return Decimal(int(cents)) / 100


def qty_to_str(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    a = abs(int(micro))
    return f"{sign}{a // QTY_SCALE}.{a % QTY_SCALE:06d}"


def qty_to_decimal(micro: int) -> Decimal:
    return Decimal(int(micro)) / QTY_SCALE


def bps_to_fraction(bps) -> Decimal:
    return _dec(bps) / Decimal(10000)


def _price_micros(price: Decimal) -> int:
    """Price as integer micro-dollars. Callers must pass a price already at <= 6 dp (fill prices are
    quantized by buy_fill_price/sell_fill_price), otherwise this would hide a rounding step."""
    p = _dec(price)
    with localcontext() as ctx:
        ctx.prec = _PREC
        scaled = p * QTY_SCALE
        if scaled != scaled.to_integral_value():
            raise ValueError(f"price must have at most 6 decimal places here: {price!r}")
        return int(scaled)


def buy_fill_price(open_price, slippage_bps) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PREC
        return (_dec(open_price) * (1 + bps_to_fraction(slippage_bps))).quantize(PRICE_QUANT, rounding=ROUND_CEILING)


def sell_fill_price(open_price, slippage_bps) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PREC
        return (_dec(open_price) * (1 - bps_to_fraction(slippage_bps))).quantize(PRICE_QUANT, rounding=ROUND_FLOOR)


def buy_quantity(notional_cents: int, fill_price) -> int:
    """Micro-shares purchasable with notional_cents at fill_price, floored.
    qty_micro = floor(notional_cents/100 / (price_micro/1e6) * 1e6) = floor(notional_cents*1e10 / price_micro)."""
    pm = _price_micros(fill_price)
    if pm <= 0:
        raise ValueError("fill price must be positive")
    return (int(notional_cents) * 10_000_000_000) // pm


def buy_cost_cents(qty_micro: int, fill_price) -> int:
    """Cash paid for qty_micro at fill_price, rounded UP to the cent. qty*price in cents = qm*pm/1e10."""
    num = int(qty_micro) * _price_micros(fill_price)
    return -(-num // 10_000_000_000)


def sell_proceeds_cents(qty_micro: int, fill_price) -> int:
    num = int(qty_micro) * _price_micros(fill_price)
    return num // 10_000_000_000


def market_value_cents(qty_micro: int, price) -> int:
    with localcontext() as ctx:
        ctx.prec = _PREC
        v = Decimal(int(qty_micro)) * _dec(price) / Decimal(10_000)
        return int(v.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def commission_cents(gross_cents: int, per_order_cents: int, bps) -> int:
    with localcontext() as ctx:
        ctx.prec = _PREC
        variable = (Decimal(int(gross_cents)) * _dec(bps) / Decimal(10_000)).to_integral_value(rounding=ROUND_CEILING)
    return int(per_order_cents) + int(variable)


def dividend_cents(qty_micro: int, amount_per_share) -> int:
    with localcontext() as ctx:
        ctx.prec = _PREC
        v = Decimal(int(qty_micro)) * _dec(amount_per_share) / Decimal(10_000)
        return int(v.to_integral_value(rounding=ROUND_FLOOR))


def split_quantity(qty_micro: int, numerator: int, denominator: int) -> int:
    """New micro-share quantity after a numerator:denominator split. Exact for forward integer
    splits; reverse/odd ratios floor and the caller records any fractional remainder explicitly."""
    if numerator <= 0 or denominator <= 0:
        raise ValueError("split ratio must be positive")
    return (int(qty_micro) * int(numerator)) // int(denominator)


def ratio(a, b) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 28
        return _dec(a) / _dec(b)


def fmt_pct(fraction: Decimal | None, places: int = 2, signed: bool = True) -> str:
    if fraction is None:
        return "n/a"
    v = (fraction * 100).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    return f"{'+' if signed and v > 0 else ''}{v}%"
