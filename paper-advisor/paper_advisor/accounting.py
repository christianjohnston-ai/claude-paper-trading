"""Deterministic replay of the ledger into holdings, tax lots, cash and P&L.

Accounting conventions (documented in README "Accounting"):
- Lot relief is FIFO. A buy's cost basis = gross cost + commission. A sell's realized P&L =
  (gross proceeds - commission) - FIFO basis relieved. Partial relief pro-rates a lot's basis with
  exact integer math rounded half-even; a lot that is fully consumed releases its entire remaining
  basis, so no cent can be lost or invented across partial sells.
- Splits multiply every open lot's quantity by num/den (floored to the micro-share) and leave basis
  unchanged. The SPLIT event must carry exactly the resulting quantity delta, otherwise replay
  fails loudly (a mismatched split is a data error, not something to paper over).
- Cash dividends are cash income recognized on the ex-date (not part of basis, not "realized P&L").
- Identity that reconciliation checks every run:
      equity - funded == realized + unrealized + dividends + adjustments
- A "trade" (round trip) runs from a flat position to the next return to flat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from . import ledger
from . import money as m


class ReplayError(RuntimeError):
    pass


@dataclass
class Lot:
    open_seq: int
    symbol: str
    opened_at: str
    session_date: str | None
    qty_micro: int
    basis_cents: int
    fill_id: int | None


@dataclass
class Trade:
    symbol: str
    opened_at: str
    entry_session: str | None
    cost_cents: int = 0
    proceeds_cents: int = 0
    realized_cents: int = 0
    fees_cents: int = 0
    dividends_cents: int = 0
    max_qty_micro: int = 0
    buy_fill_ids: list = field(default_factory=list)
    sell_fill_ids: list = field(default_factory=list)
    closed_at: str | None = None
    exit_session: str | None = None


@dataclass
class State:
    funded_cents: int = 0
    cash_cents: int = 0
    positions: dict = field(default_factory=dict)
    lots: dict = field(default_factory=dict)
    realized_cents: int = 0
    dividends_cents: int = 0
    adjustments_cents: int = 0
    fees_cents: int = 0
    traded_notional_cents: int = 0
    closed_trades: list = field(default_factory=list)
    open_trades: dict = field(default_factory=dict)
    last_seq: int = 0

    def basis_cents(self, symbol: str) -> int:
        return sum(l.basis_cents for l in self.lots.get(symbol, []))

    def avg_cost(self, symbol: str) -> Decimal | None:
        q = self.positions.get(symbol, 0)
        if q <= 0:
            return None
        return (Decimal(self.basis_cents(symbol)) / 100) / (Decimal(q) / m.QTY_SCALE)


@dataclass
class PositionValue:
    symbol: str
    qty_micro: int
    price: Decimal
    market_value_cents: int
    basis_cents: int
    unrealized_cents: int


@dataclass
class Valuation:
    cash_cents: int
    market_value_cents: int
    unrealized_cents: int
    equity_cents: int
    positions: list
    missing_prices: list


def _prorate(total: int, part: int, whole: int) -> int:
    q, r = divmod(total * part, whole)
    if 2 * r > whole or (2 * r == whole and q % 2 == 1):
        q += 1
    return q


def replay(rows) -> State:
    st = State()
    by_id = {}
    for row in rows:
        r = dict(row)
        by_id[r["id"]] = r
        et = r["event_type"]
        sym = r["symbol"]
        dq = int(r["quantity_delta_micro"])
        dc = int(r["cash_delta_cents"])
        fee = int(r["fee_cents"])
        if r["seq"] != st.last_seq + 1:
            raise ReplayError(f"ledger sequence gap at seq {r['seq']} (expected {st.last_seq + 1})")
        st.last_seq = r["seq"]

        if et == "INITIAL_FUNDING":
            st.cash_cents += dc
            st.funded_cents += dc
        elif et == "BUY_FILL":
            st.cash_cents += dc
            prev = st.positions.get(sym, 0)
            st.positions[sym] = prev + dq
            basis = -dc
            st.fees_cents += fee
            st.traded_notional_cents += basis - fee
            st.lots.setdefault(sym, []).append(Lot(r["seq"], sym, r["effective_at"], r["session_date"], dq, basis, r["fill_id"]))
            if prev == 0:
                st.open_trades[sym] = Trade(sym, r["effective_at"], r["session_date"])
            t = st.open_trades[sym]
            t.cost_cents += basis
            t.fees_cents += fee
            t.max_qty_micro = max(t.max_qty_micro, st.positions[sym])
            t.buy_fill_ids.append(r["fill_id"])
        elif et == "SELL_FILL":
            qty = -dq
            held = st.positions.get(sym, 0)
            if qty > held:
                raise ReplayError(f"seq {r['seq']}: sells {qty} {sym} but only {held} held")
            relieved, remaining = 0, qty
            lots = st.lots.get(sym, [])
            while remaining > 0:
                if not lots:
                    raise ReplayError(f"seq {r['seq']}: no lots left for {sym}")
                lot = lots[0]
                take = min(lot.qty_micro, remaining)
                part = lot.basis_cents if take == lot.qty_micro else _prorate(lot.basis_cents, take, lot.qty_micro)
                lot.qty_micro -= take
                lot.basis_cents -= part
                relieved += part
                remaining -= take
                if lot.qty_micro == 0:
                    lots.pop(0)
            proceeds_net = dc
            pnl = proceeds_net - relieved
            st.cash_cents += dc
            st.realized_cents += pnl
            st.fees_cents += fee
            st.traded_notional_cents += proceeds_net + fee
            st.positions[sym] = held - qty
            t = st.open_trades.get(sym)
            if t is not None:
                t.proceeds_cents += proceeds_net
                t.realized_cents += pnl
                t.fees_cents += fee
                t.sell_fill_ids.append(r["fill_id"])
            if st.positions[sym] == 0:
                del st.positions[sym]
                st.lots.pop(sym, None)
                if t is not None:
                    t.closed_at = r["effective_at"]
                    t.exit_session = r["session_date"]
                    st.closed_trades.append(t)
                    del st.open_trades[sym]
        elif et == "CASH_DIVIDEND":
            st.cash_cents += dc
            st.dividends_cents += dc
            if sym in st.open_trades:
                st.open_trades[sym].dividends_cents += dc
        elif et == "SPLIT":
            num, den = r["split_num"], r["split_den"]
            if not num or not den:
                raise ReplayError(f"seq {r['seq']}: split without ratio")
            before = st.positions.get(sym, 0)
            new_total = 0
            for lot in st.lots.get(sym, []):
                lot.qty_micro = m.split_quantity(lot.qty_micro, num, den)
                new_total += lot.qty_micro
            if new_total - before != dq:
                raise ReplayError(f"seq {r['seq']}: split {num}:{den} of {before} {sym} should change quantity by "
                                  f"{new_total - before}, event says {dq}")
            if before:
                st.positions[sym] = new_total
                if sym in st.open_trades:
                    st.open_trades[sym].max_qty_micro = max(st.open_trades[sym].max_qty_micro, new_total)
        elif et == "REVERSAL":
            orig = by_id.get(r["reverses_event_id"])
            if orig is None:
                raise ReplayError(f"seq {r['seq']}: reversal of unknown event {r['reverses_event_id']}")
            st.cash_cents += dc
            if orig["event_type"] == "CASH_DIVIDEND":
                st.dividends_cents += dc
            elif orig["event_type"] == "ADJUSTMENT":
                st.adjustments_cents += dc
            else:
                raise ReplayError(f"seq {r['seq']}: reversal of non-reversible {orig['event_type']}")
        elif et == "ADJUSTMENT":
            st.cash_cents += dc
            st.adjustments_cents += dc
        else:
            raise ReplayError(f"unknown event type {et}")
        if st.cash_cents < 0:
            raise ReplayError(f"cash negative after seq {r['seq']}")
    return st


def load_state(conn, experiment_id: int, account: str, through_seq: int | None = None) -> State:
    return replay(ledger.events(conn, experiment_id, account, through_seq))


def valuation(state: State, prices: dict) -> Valuation:
    positions, missing = [], []
    mv_total = unreal_total = 0
    for sym in sorted(state.positions):
        qty = state.positions[sym]
        basis = state.basis_cents(sym)
        px = prices.get(sym)
        if px is None:
            # A held symbol with no mark is valued at cost and reported as missing, never silently
            # at zero (which would invent a loss) or at a stale price without saying so.
            missing.append(sym)
            mv, px_used = basis, None
        else:
            px_used = Decimal(str(px))
            mv = m.market_value_cents(qty, px_used)
        positions.append(PositionValue(sym, qty, px_used, mv, basis, mv - basis))
        mv_total += mv
        unreal_total += mv - basis
    return Valuation(state.cash_cents, mv_total, unreal_total, state.cash_cents + mv_total, positions, missing)
