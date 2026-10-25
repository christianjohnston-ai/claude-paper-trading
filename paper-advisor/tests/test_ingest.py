"""Refresh orchestration with a fake transport (no network): identity verification from official
listing directories + Yahoo metadata, bar storage, data status, evidence trail, and loud reporting of
partial failures (an incomplete earnings calendar is UNKNOWN, never "no earnings")."""
from datetime import datetime, timezone
from pathlib import Path

from paper_advisor import clock, ingest, prices, universe

from test_data_sources import REC, FakeTransport, chart_payload

UTC = timezone.utc


def test_forward_refresh_verifies_identity_and_stores_bars(forward_db, app_paths):
    clock.set_override(lambda: datetime(2026, 10, 24, 5, 0, tzinfo=UTC))
    spy = [i for i in universe.FORWARD_UNIVERSE if i.symbol == "SPY"]
    universe.install(forward_db, spy, now=clock.now_iso())
    from paper_advisor import experiments
    exp_id, _ = experiments.ensure_default_experiment(forward_db, clock.now_utc())
    t = FakeTransport({
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt": (200, {}, (REC / "nasdaqtrader_nasdaqlisted_excerpt_2026-10-23.txt").read_bytes()),
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt": (200, {}, (REC / "nasdaqtrader_otherlisted_excerpt_2026-10-23.txt").read_bytes()),
        "https://query1.finance.yahoo.com/v8/finance/chart/SPY": (200, {}, (REC / "yahoo_chart_SPY_1mo_retrieved_2026-10-24.json").read_bytes()),
    })
    rep = ingest.refresh(forward_db, app_paths, exp_id, clock.now_utc(), run_id=None, transport=t,
                         include_news=False, include_earnings=False, include_context=False)
    assert rep.identity["SPY"] == "VERIFIED"
    ok, why = universe.is_verified(forward_db, "SPY", clock.now_iso())
    assert ok, why
    assert prices.bar_for(forward_db, "SPY", "2026-10-23")["close"] == "759.9700"
    assert rep.data_status == "OK" and rep.failures == []


def test_refresh_reports_failures_and_records_evidence(fixture_db, app_paths):
    from helpers import World
    w = World(fixture_db, start="2026-10-24T05:00:00")
    bars = [("2026-10-22", 100, 101, 99, 100.5, 1000), ("2026-10-23", 100.5, 102, 100, 101.2, 1000)]
    resp = {f"https://query1.finance.yahoo.com/v8/finance/chart/{s}": (200, {}, chart_payload(bars))
            for s in ("DEMO.SPY", "DEMO.A", "DEMO.B", "DEMO.C", "DEMO.D", "DEMO.E", "DEMO.F")}
    resp["https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"] = (200, {}, chart_payload(bars))
    resp["https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX"] = (503, {}, b"unavailable")
    resp["https://api.nasdaq.com/api/calendar/earnings?date=2026-10-27"] = (500, {}, b"error")
    resp["https://api.nasdaq.com/api/calendar/earnings"] = (200, {}, (REC / "nasdaq_earnings_2026-10-28_retrieved_2026-10-24.json").read_bytes())
    resp["https://feeds.finance.yahoo.com/rss/2.0/headline"] = (200, {}, (REC / "yahoo_rss_AAPL_retrieved_2026-10-24.xml").read_bytes())
    t = FakeTransport(resp)
    rep = ingest.refresh(fixture_db, app_paths, w.exp_id, clock.now_utc(), run_id=None, transport=t, news_symbols=["DEMO.A"])
    assert rep.data_status == "OK"
    assert "2026-10-27" in rep.earnings["unknown_dates"]
    assert any("earnings calendar unavailable" in f for f in rep.failures)
    assert any("^TNX" in f for f in rep.failures) and "^VIX" in rep.context
    news_rows = fixture_db.execute("SELECT * FROM evidence_items WHERE kind='news' AND symbol='DEMO.A'").fetchall()
    assert news_rows and all("headline_unverified" in r["quality_flags"] for r in news_rows)
    assert all(r["retrieved_at"] and r["url"].startswith("http") for r in news_rows)
    snaps = fixture_db.execute("SELECT COUNT(*) FROM source_snapshots WHERE source='yahoo_chart_v8'").fetchone()[0]
    assert snaps >= 9  # 7 instruments + 2 context symbols, every response recorded including the failure
