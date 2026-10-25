"""Stored daily bars (as-traded) and source snapshots.

Rules enforced here:
- A bar is only stored if it was retrieved strictly after its session closed. A bar fetched during
  the session is a moving, partial bar; storing it would let a later fill or signal use prices that
  were still changing. (Ingestion additionally waits a finalization buffer after the close.)
- The FIRST stored version of a bar is the one every fill and valuation uses (bar_for returns
  revision 1). If a later fetch returns different values, a new revision is stored with a
  'revised_from_r1' flag for the data-quality report instead of silently changing history.
- Values are as-traded: ingestion un-adjusts provider back-adjustment for later splits before
  calling insert_bar (see data_sources.yahoo).
"""
from __future__ import annotations

import json
from datetime import date as date_cls
from decimal import ROUND_HALF_EVEN, Decimal

from . import clock, market_calendar as cal

PRICE_PLACES = Decimal("0.0001")


class NotFinalError(ValueError):
    pass


def _q(x) -> Decimal:
    return Decimal(str(x)).quantize(PRICE_PLACES, rounding=ROUND_HALF_EVEN)


def record_snapshot(conn, *, source: str, url: str, requested_at: str, retrieved_at: str, http_status: int | None,
                    parse_status: str, params: dict | None = None, content_sha256: str | None = None,
                    content_path: str | None = None, bytes_: int | None = None, market_asof: str | None = None,
                    error: str | None = None, run_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO source_snapshots(source, url, params_json, requested_at, retrieved_at, http_status, content_sha256,"
        " content_path, bytes, market_asof, parse_status, error, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, url, json.dumps(params or {}, sort_keys=True), requested_at, retrieved_at, http_status, content_sha256,
         content_path, bytes_, market_asof, parse_status, error, run_id))
    return int(cur.lastrowid)


def insert_bar(conn, symbol: str, session_date: str, o, h, l, c, volume: int, *, source: str, snapshot_id: int | None,
               retrieved_at: str, market_ts: str | None = None, split_factor: str = "1", quality_flags=()) -> tuple[int | None, str]:
    s = cal.session(date_cls.fromisoformat(session_date))
    if s is None:
        raise ValueError(f"{session_date} is not an NYSE session; refusing to store a bar for it")
    if clock.parse_iso(retrieved_at) <= s.close_utc:
        raise NotFinalError(f"{symbol} {session_date} bar retrieved at {retrieved_at}, before the session closed")
    o, h, l, c = _q(o), _q(h), _q(l), _q(c)
    flags = list(quality_flags)
    if min(o, h, l, c) <= 0:
        flags.append("non_positive_price")
    if h < max(o, c) or l > min(o, c) or h < l:
        flags.append("ohlc_inconsistent")
    existing = conn.execute("SELECT * FROM price_bars WHERE symbol=? AND session_date=? ORDER BY revision",
                            (symbol, session_date)).fetchall()
    if existing:
        last = existing[-1]
        same = (Decimal(last["open"]), Decimal(last["high"]), Decimal(last["low"]), Decimal(last["close"]), int(last["volume"])) == (o, h, l, c, int(volume))
        if same:
            return int(last["id"]), "duplicate"
        first = existing[0]
        price_same = (Decimal(first["open"]), Decimal(first["high"]), Decimal(first["low"]), Decimal(first["close"])) == (o, h, l, c)
        if price_same:
            # Volume-only revisions are common (late prints); not worth a new revision.
            return int(first["id"]), "duplicate"
        flags.append(f"revised_from_r{first['revision']}")
        rev = int(last["revision"]) + 1
        status = "revised"
    else:
        rev, status = 1, "inserted"
    cur = conn.execute(
        "INSERT INTO price_bars(symbol, session_date, open, high, low, close, volume, source, snapshot_id, retrieved_at,"
        " market_ts, split_factor, revision, quality_flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, session_date, str(o), str(h), str(l), str(c), int(volume), source, snapshot_id, retrieved_at, market_ts,
         str(split_factor), rev, json.dumps(sorted(set(flags)))))
    return int(cur.lastrowid), status


def bar_for(conn, symbol: str, session_date: str):
    """The first-observed (revision 1) bar for a session, used for fills and valuations."""
    return conn.execute("SELECT * FROM price_bars WHERE symbol=? AND session_date=? ORDER BY revision LIMIT 1",
                        (symbol, session_date)).fetchone()


def latest_bar(conn, symbol: str, on_or_before: str | None = None):
    if on_or_before is None:
        return conn.execute("SELECT * FROM price_bars WHERE symbol=? AND revision=1 ORDER BY session_date DESC LIMIT 1",
                            (symbol,)).fetchone()
    return conn.execute("SELECT * FROM price_bars WHERE symbol=? AND revision=1 AND session_date<=? ORDER BY session_date DESC LIMIT 1",
                        (symbol, on_or_before)).fetchone()


def revisions(conn, symbol: str, session_date: str):
    return conn.execute("SELECT * FROM price_bars WHERE symbol=? AND session_date=? ORDER BY revision",
                        (symbol, session_date)).fetchall()


def history(conn, symbol: str, through: str | None = None, limit: int | None = None):
    """Oldest-first revision-1 bars, optionally through a date and limited to the most recent N."""
    sql = "SELECT * FROM price_bars WHERE symbol=? AND revision=1"
    args: list = [symbol]
    if through is not None:
        sql += " AND session_date<=?"
        args.append(through)
    sql += " ORDER BY session_date DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return list(reversed(conn.execute(sql, args).fetchall()))


def symbols_with_bar(conn, session_date: str) -> set[str]:
    return {r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM price_bars WHERE session_date=?", (session_date,))}


def session_has_data(conn, session_date: str, universe_symbols: list[str]) -> bool:
    """True when enough of the universe has a bar for the session that its absence for one symbol
    means that symbol did not trade (halt, delisting, data gap), not that our feed is down."""
    have = symbols_with_bar(conn, session_date) & set(universe_symbols)
    if not universe_symbols:
        return False
    return len(have) >= max(1, (len(universe_symbols) + 1) // 2)


def flags_of(bar) -> list[str]:
    return json.loads(bar["quality_flags"] or "[]") if bar is not None else []
