"""Splits and cash dividends, plus a record of which date ranges we actually checked.

Policy (README "Prices, splits and dividends"):
- Prices are stored as-traded; a split is applied to holdings as a SPLIT ledger event at the open of
  its ex-date (before that open's fills), and a cash dividend is credited as cash on its ex-date to
  holders of record at the prior close. This is identical for the portfolio and the SPY benchmark,
  and it is why we never use dividend-adjusted prices for valuation: using both an adjusted price
  AND a cash dividend would count the dividend twice.
- Dividend cash is credited on the ex-date rather than the pay date (pay dates are not reliably
  available from our keyless sources). That slightly overstates when the cash becomes spendable; the
  amounts are tiny relative to a $500 account and the treatment is the same for the benchmark.
- If a held symbol's corporate actions were not checked through a valuation date, that valuation is
  marked provisional instead of being presented as final.
"""
from __future__ import annotations

from decimal import Decimal


class CorporateActionConflict(RuntimeError):
    pass


def record(conn, symbol: str, action_type: str, ex_date: str, *, ratio: tuple[int, int] | None = None,
           amount: Decimal | None = None, source: str, snapshot_id: int | None, now: str) -> tuple[int, str]:
    if action_type == "SPLIT":
        if ratio is None or ratio[0] <= 0 or ratio[1] <= 0:
            raise ValueError("split needs a positive ratio")
        num, den, amt = int(ratio[0]), int(ratio[1]), None
    elif action_type == "CASH_DIVIDEND":
        if amount is None or Decimal(amount) <= 0:
            raise ValueError("dividend needs a positive amount")
        num = den = None
        # format(..., 'f') avoids scientific notation ('1E+1') in stored, human-audited values
        amt = format(Decimal(amount).normalize(), "f")
    else:
        raise ValueError(f"unsupported corporate action {action_type}")
    existing = conn.execute("SELECT * FROM corporate_actions WHERE symbol=? AND action_type=? AND ex_date=?",
                            (symbol, action_type, ex_date)).fetchone()
    if existing is not None:
        same = (existing["ratio_num"], existing["ratio_den"]) == (num, den) and (
            (existing["amount"] is None and amt is None) or (existing["amount"] is not None and amt is not None
                                                              and Decimal(existing["amount"]) == Decimal(amt)))
        return int(existing["id"]), ("duplicate" if same else "conflict")
    cur = conn.execute(
        "INSERT INTO corporate_actions(symbol, action_type, ex_date, ratio_num, ratio_den, amount, source, snapshot_id, first_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)", (symbol, action_type, ex_date, num, den, amt, source, snapshot_id, now))
    return int(cur.lastrowid), "inserted"


def on_date(conn, ex_date: str, symbol: str | None = None):
    if symbol is None:
        return conn.execute("SELECT * FROM corporate_actions WHERE ex_date=? ORDER BY action_type DESC, symbol", (ex_date,)).fetchall()
    return conn.execute("SELECT * FROM corporate_actions WHERE ex_date=? AND symbol=? ORDER BY action_type DESC",
                        (ex_date, symbol)).fetchall()


def for_symbol(conn, symbol: str):
    return conn.execute("SELECT * FROM corporate_actions WHERE symbol=? ORDER BY ex_date", (symbol,)).fetchall()


def splits_after(conn, symbol: str, after_date: str, through_date: str):
    return conn.execute("SELECT * FROM corporate_actions WHERE symbol=? AND action_type='SPLIT' AND ex_date>? AND ex_date<=? ORDER BY ex_date",
                        (symbol, after_date, through_date)).fetchall()


def set_coverage(conn, symbol: str, checked_from: str, checked_through: str, now: str, snapshot_id: int | None) -> None:
    row = conn.execute("SELECT * FROM corporate_action_coverage WHERE symbol=?", (symbol,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO corporate_action_coverage(symbol, checked_from, checked_through, checked_at, snapshot_id) VALUES (?,?,?,?,?)",
                     (symbol, checked_from, checked_through, now, snapshot_id))
    else:
        conn.execute("UPDATE corporate_action_coverage SET checked_from=MIN(checked_from, ?), checked_through=MAX(checked_through, ?),"
                     " checked_at=?, snapshot_id=COALESCE(?, snapshot_id) WHERE symbol=?",
                     (checked_from, checked_through, now, snapshot_id, symbol))


def covered_through(conn, symbol: str) -> str | None:
    r = conn.execute("SELECT checked_through FROM corporate_action_coverage WHERE symbol=?", (symbol,)).fetchone()
    return None if r is None else r["checked_through"]
