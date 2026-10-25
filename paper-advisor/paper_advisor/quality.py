"""Per-symbol data-quality assessment used by validation, briefings and the dashboard.

FRESH   the latest stored final bar is from the last completed session and has no quality flags
STALE   the latest bar is older than the last completed session (feed lag, halt, or failed refresh)
SUSPECT the latest bar carries flags (inconsistent OHLC, unexplained jump, revision)
MISSING no bar at all

Buys require FRESH data. Sells are allowed on non-fresh data (reducing risk should not be blocked
by a feed problem), but the decision records that the data was not fresh.
"""
from __future__ import annotations

from datetime import date

from . import market_calendar as cal, prices


def assess(conn, symbols, now, finalization_minutes: int) -> dict:
    last = cal.last_completed_session(now, finalization_minutes)
    expected = last.date.isoformat() if last else None
    out = {}
    for sym in symbols:
        bar = prices.latest_bar(conn, sym)
        if bar is None:
            out[sym] = {"status": "MISSING", "expected_session": expected, "last_bar_date": None, "flags": [], "sessions_behind": None}
            continue
        flags = prices.flags_of(bar)
        behind = cal.count_sessions_after(date.fromisoformat(bar["session_date"]), date.fromisoformat(expected)) if expected else 0
        status = "STALE" if behind > 0 else ("SUSPECT" if flags else "FRESH")
        out[sym] = {"status": status, "expected_session": expected, "last_bar_date": bar["session_date"],
                    "last_close": bar["close"], "retrieved_at": bar["retrieved_at"], "source": bar["source"],
                    "flags": flags, "sessions_behind": behind}
    return out
