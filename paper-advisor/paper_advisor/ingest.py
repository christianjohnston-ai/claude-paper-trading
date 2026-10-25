"""Market-data refresh for one run: identity verification, daily bars + corporate actions, research
context, earnings calendar and headlines. Every response is cached as evidence (http.Fetcher).

Failure policy: a failed source never aborts the run and is never papered over. Failures are listed
in the RefreshReport and in the briefing; symbols without fresh data cannot be bought (quality.py),
and pending orders for a session without data stay pending (broker_sim). data_status summarizes:
  OK                every universe symbol has a bar for the last completed session
  PARTIAL           some symbols are stale/missing (they are blocked for buys)
  DATA_UNAVAILABLE  the benchmark or most of the universe is stale/missing
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import clock, config, db, market_calendar as cal, prices, quality, universe
from .data_sources import http, nasdaq, news, yahoo

CONTEXT_SYMBOLS = {"^VIX": "CBOE Volatility Index (research context only, not tradeable)",
                   "^TNX": "CBOE 10-Year Treasury Yield Index x10 (research context only)"}
IDENTITY_REFRESH_HOURS = 20
EARNINGS_LOOKAHEAD_SESSIONS = 10


@dataclass
class RefreshReport:
    started_at: str
    finished_at: str | None = None
    symbols: dict = field(default_factory=dict)
    identity: dict = field(default_factory=dict)
    earnings: dict = field(default_factory=dict)
    news: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    data_status: str = "UNKNOWN"
    quality: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _evidence(conn, exp_id, run_id, kind, title, *, symbol=None, url=None, publisher=None, published_at=None,
              retrieved_at=None, snapshot_id=None, summary="", flags=(), created_by="system") -> int:
    cur = conn.execute(
        "INSERT INTO evidence_items(experiment_id, run_id, kind, symbol, title, url, publisher, published_at, retrieved_at, snapshot_id,"
        " summary, quality_flags, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (exp_id, run_id, kind, symbol, title[:500], url, publisher, published_at, retrieved_at, snapshot_id, summary[:4000],
         json.dumps(list(flags)), created_by, clock.now_iso()))
    return int(cur.lastrowid)


def _identity_due(conn, symbols, now) -> bool:
    for s in symbols:
        v = universe.latest_verification(conn, s)
        if v is None or (now - clock.parse_iso(v["verified_at"])).total_seconds() > IDENTITY_REFRESH_HOURS * 3600:
            return True
    return False


def refresh(conn, paths, experiment_id: int, now, run_id: int | None, transport=None, news_symbols=(),
            history_days: int = 420, include_news: bool = True, include_earnings: bool = True,
            include_context: bool = True) -> RefreshReport:
    rep = RefreshReport(started_at=clock.now_iso())
    _, cfg = config.current(conn, experiment_id, clock.iso(now))
    fetcher = http.Fetcher(conn, paths.evidence_dir, transport=transport, run_id=run_id)
    insts = conn.execute("SELECT * FROM instruments WHERE in_universe=1 ORDER BY symbol").fetchall()
    syms = [i["symbol"] for i in insts]

    directory = None
    if _identity_due(conn, syms, now):
        directory = {}
        for kind, url in (("nasdaq", nasdaq.NASDAQ_LISTED_URL), ("other", nasdaq.OTHER_LISTED_URL)):
            r = fetcher.get("nasdaqtrader_symdir", url, accept="text/plain", ext="txt")
            if not r.ok:
                rep.failures.append(f"symbol directory {kind}: {r.error}")
                directory = None
                break
            rows, created = nasdaq.parse_directory(r.body.decode("utf-8", "replace"), kind)
            directory.update(rows)
            rep.identity[f"{kind}_file_created"] = created
            rep.identity[f"{kind}_snapshot_id"] = r.snapshot_id

    for inst in insts:
        sym = inst["symbol"]
        last = prices.latest_bar(conn, sym)
        start = (date.fromisoformat(last["session_date"]) - timedelta(days=15)) if last else (now.date() - timedelta(days=history_days))
        r = fetcher.get("yahoo_chart_v8", yahoo.chart_url(inst["yahoo_symbol"]), yahoo.chart_params(start, now),
                        accept="application/json", ext="json")
        if not r.ok:
            rep.failures.append(f"{sym} bars: {r.error}")
            rep.symbols[sym] = {"error": r.error}
            continue
        try:
            parsed = yahoo.parse_chart(r.body)
        except (ValueError, KeyError) as exc:
            rep.failures.append(f"{sym} bars: unparseable response ({exc})")
            continue
        with db.transaction(conn):
            summary = yahoo.ingest_chart(conn, sym, parsed, r.snapshot_id, r.retrieved_at, cfg.bar_finalization_minutes, start.isoformat())
            if directory is not None:
                status, details = universe.verify_against_directory(inst, directory.get(sym), parsed.get("meta"))
                details["sources"] = {"directory_snapshots": [rep.identity.get("nasdaq_snapshot_id"), rep.identity.get("other_snapshot_id")],
                                      "yahoo_snapshot": r.snapshot_id}
                universe.record_verification(conn, sym, status, details, clock.now_iso(), run_id)
                rep.identity[sym] = status if status == "VERIFIED" else f"FAILED: {'; '.join(details['problems'])}"
        summary.pop("flags", None) if not summary.get("flags") else None
        rep.symbols[sym] = {k: v for k, v in summary.items() if v not in ([], {}, None, 0) or k in ("stored", "last_final_bar")}
        if summary.get("error"):
            rep.failures.append(f"{sym} bars: {summary['error']}")
        if summary.get("non_session"):
            rep.failures.append(f"{sym}: provider returned bars on non-session dates {summary['non_session']} (calendar or feed mismatch)")

    if include_context:
        for sym, desc in CONTEXT_SYMBOLS.items():
            r = fetcher.get("yahoo_chart_v8", yahoo.chart_url(sym), {"range": "1mo", "interval": "1d"},
                            accept="application/json", ext="json")
            if not r.ok:
                rep.failures.append(f"context {sym}: {r.error}")
                continue
            try:
                p = yahoo.parse_chart(r.body)
                closes = [(b["date"], float(b["close"])) for b in p["bars"]
                          if cal.session(date.fromisoformat(b["date"])) and
                          cal.session(date.fromisoformat(b["date"])).close_utc + timedelta(minutes=cfg.bar_finalization_minutes) <= now]
                if not closes:
                    raise ValueError("no completed bars")
                last_d, last_c = closes[-1]
                prev5 = closes[-6][1] if len(closes) >= 6 else None
                rep.context[sym] = {"description": desc, "last_session": last_d, "last_close": round(last_c, 2),
                                    "change_5_sessions": None if prev5 is None else round(last_c - prev5, 2)}
                with db.transaction(conn):
                    _evidence(conn, experiment_id, run_id, "macro", f"{sym} close {round(last_c, 2)} on {last_d}", symbol=sym,
                              url=r.url, publisher="Yahoo Finance chart API", published_at=None, retrieved_at=r.retrieved_at,
                              snapshot_id=r.snapshot_id, summary=json.dumps(rep.context[sym]))
            except (ValueError, KeyError, IndexError) as exc:
                rep.failures.append(f"context {sym}: {exc}")

    if include_earnings:
        sessions, d = [], cal.next_session_after(now).date
        while len(sessions) < EARNINGS_LOOKAHEAD_SESSIONS:
            s = cal.session(d)
            if s is not None:
                sessions.append(d)
            d += timedelta(days=1)
        found, unknown = {}, []
        for sd in sessions:
            r = fetcher.get("nasdaq_earnings", nasdaq.EARNINGS_URL, {"date": sd.isoformat()}, accept="application/json", ext="json")
            if not r.ok:
                unknown.append(sd.isoformat())
                continue
            try:
                rows = nasdaq.parse_earnings(r.body)
            except (ValueError, KeyError):
                unknown.append(sd.isoformat())
                continue
            for row in rows:
                if row["symbol"] in syms:
                    found.setdefault(row["symbol"], []).append({"date": sd.isoformat(), "time": nasdaq.TIME_LABEL.get(row["time"], row["time"])})
                    with db.transaction(conn):
                        _evidence(conn, experiment_id, run_id, "earnings", f"{row['symbol']} earnings {sd.isoformat()} "
                                  f"({nasdaq.TIME_LABEL.get(row['time'], row['time'])})", symbol=row["symbol"], url=r.url,
                                  publisher="Nasdaq earnings calendar", retrieved_at=r.retrieved_at, snapshot_id=r.snapshot_id,
                                  summary=json.dumps(row), flags=["date_may_be_estimated"])
        rep.earnings = {"window_sessions": [s.isoformat() for s in sessions], "by_symbol": found, "unknown_dates": unknown}
        if unknown:
            rep.failures.append(f"earnings calendar unavailable for {unknown}: treat earnings risk on those dates as UNKNOWN")

    if include_news and news_symbols:
        rep.news, fails = fetch_news(conn, fetcher, experiment_id, run_id, news_symbols)
        rep.failures.extend(fails)

    rep.quality = quality.assess(conn, syms, now, cfg.bar_finalization_minutes)
    stale = [s for s, q in rep.quality.items() if q["status"] != "FRESH"]
    bench = cfg.benchmark_symbol
    if bench in stale or len(stale) > len(syms) // 2:
        rep.data_status = "DATA_UNAVAILABLE"
    elif stale:
        rep.data_status = "PARTIAL"
    else:
        rep.data_status = "OK"
    rep.finished_at = clock.now_iso()
    return rep


def fetch_news(conn, fetcher, experiment_id: int, run_id: int | None, symbols) -> tuple[dict, list]:
    """Headlines for the given universe symbols (Yahoo Finance RSS), stored as evidence with publication
    and retrieval times. Headlines are flagged unverified: they are leads, not facts."""
    out, failures, seen = {}, [], set()
    for sym in symbols:
        inst = universe.get(conn, sym)
        if inst is None:
            failures.append(f"news {sym}: not in the research universe")
            continue
        r = fetcher.get("yahoo_rss", news.YAHOO_RSS, news.yahoo_params(inst["yahoo_symbol"]),
                        accept="application/rss+xml, application/xml", ext="xml")
        if not r.ok:
            failures.append(f"news {sym}: {r.error}")
            continue
        try:
            items = news.parse_rss(r.body)
        except Exception as exc:  # noqa: BLE001 - any parse failure is reported, not raised
            failures.append(f"news {sym}: {exc}")
            continue
        kept = []
        with db.transaction(conn):
            for it in items:
                link = news.safe_link(it["link"])
                if not link or (sym, link) in seen:
                    continue
                seen.add((sym, link))
                eid = _evidence(conn, experiment_id, run_id, "news", it["title"], symbol=sym, url=link,
                                publisher=it["publisher"] or "Yahoo Finance RSS", published_at=it["published_at"],
                                retrieved_at=r.retrieved_at, snapshot_id=r.snapshot_id, summary=it["summary"],
                                flags=["headline_unverified"] + ([] if it["published_at"] else ["no_publication_time"]))
                kept.append({"evidence_id": eid, "title": it["title"], "published_at": it["published_at"], "url": link})
        out[sym] = kept
    return out, failures
