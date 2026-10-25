"""End-of-session valuation snapshots (derived data, written once per session per account).

Marks use the session's as-traded CLOSE from the first-observed final bar. A snapshot is marked
PROVISIONAL, with machine-readable reasons, whenever it could be misleading:
  stale_mark:SYM                  no bar for SYM that session; the latest earlier close was used
  no_mark:SYM                     no close at all; valued at cost basis
  corporate_actions_unverified:SYM split/dividend data not checked through this session
  data_quality:SYM                the bar used carries quality flags
  calendar_unverified             the session date is outside the official NYSE table
  no_data_for_session             the feed never delivered this session (see broker_sim)
Snapshots are immutable; a correction is a new revision with a reason (never an edit).
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import accounting, corporate_actions, prices


def snapshot(conn, exp_id: int, account: str, s, now_iso: str, run_id=None, extra_reason: str | None = None) -> int | None:
    sd = s.date.isoformat()
    if conn.execute("SELECT 1 FROM valuation_snapshots WHERE experiment_id=? AND account=? AND session_date=?",
                    (exp_id, account, sd)).fetchone():
        return None
    st = accounting.load_state(conn, exp_id, account)
    marks, reasons, pos_detail = {}, [], []
    for sym in sorted(st.positions):
        bar = prices.bar_for(conn, sym, sd)
        mark_date = sd
        if bar is None:
            bar = prices.latest_bar(conn, sym, on_or_before=sd)
            if bar is None:
                reasons.append(f"no_mark:{sym}")
            else:
                mark_date = bar["session_date"]
                reasons.append(f"stale_mark:{sym}")
        if bar is not None:
            marks[sym] = Decimal(bar["close"])
            if prices.flags_of(bar):
                reasons.append(f"data_quality:{sym}")
        cov = corporate_actions.covered_through(conn, sym)
        if cov is None or cov < sd:
            reasons.append(f"corporate_actions_unverified:{sym}")
        pos_detail.append({"symbol": sym, "mark_date": mark_date if bar is not None else None})
    if not s.verified:
        reasons.append("calendar_unverified")
    if extra_reason:
        reasons.append(extra_reason)
    v = accounting.valuation(st, marks)
    by_sym = {p["symbol"]: p for p in pos_detail}
    positions = [{"symbol": p.symbol, "qty_micro": p.qty_micro, "close": None if p.price is None else str(p.price),
                  "mark_date": by_sym[p.symbol]["mark_date"], "market_value_cents": p.market_value_cents,
                  "basis_cents": p.basis_cents, "unrealized_cents": p.unrealized_cents} for p in v.positions]
    cur = conn.execute(
        "INSERT INTO valuation_snapshots(experiment_id, account, session_date, revision, as_of_utc, computed_at, cash_cents,"
        " invested_cents, equity_cents, realized_pnl_cents, unrealized_pnl_cents, dividends_cents, fees_cents,"
        " traded_notional_cents, positions_json, provisional, provisional_reasons_json, ledger_seq_through, run_id)"
        " VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (exp_id, account, sd, s.close_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), now_iso, st.cash_cents, v.market_value_cents,
         v.equity_cents, st.realized_cents, v.unrealized_cents, st.dividends_cents, st.fees_cents, st.traded_notional_cents,
         json.dumps(positions), int(bool(reasons)), json.dumps(sorted(set(reasons))), st.last_seq, run_id))
    return int(cur.lastrowid)


def snapshots(conn, exp_id: int, account: str):
    """Latest revision per session, oldest first."""
    return conn.execute(
        "SELECT v.* FROM valuation_snapshots v JOIN (SELECT session_date, MAX(revision) r FROM valuation_snapshots"
        " WHERE experiment_id=? AND account=? GROUP BY session_date) x ON v.session_date=x.session_date AND v.revision=x.r"
        " WHERE v.experiment_id=? AND v.account=? ORDER BY v.session_date", (exp_id, account, exp_id, account)).fetchall()


def latest_snapshot(conn, exp_id: int, account: str):
    rows = snapshots(conn, exp_id, account)
    return rows[-1] if rows else None
