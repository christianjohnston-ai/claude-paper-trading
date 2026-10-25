"""Data ingestion: as-traded un-adjustment, finality, calendar mismatches, revisions, identity checks,
the GET-only host allowlist, evidence caching, RSS and earnings parsing.

Recorded real responses live in tests/fixtures/recorded (see its README). Small Yahoo-shaped payloads
built inline below exercise edge cases (partial bars, weekend bars, jumps) that a recording cannot
be guaranteed to contain.
"""
import gzip
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from paper_advisor import clock, corporate_actions, db, prices, universe
from paper_advisor.data_sources import http, nasdaq, news, yahoo

REC = Path(__file__).parent / "fixtures" / "recorded"
UTC = timezone.utc


def _ts(d: str, hh=13, mm=30) -> int:
    return int(datetime.fromisoformat(d).replace(hour=hh, minute=mm, tzinfo=UTC).timestamp())


def chart_payload(rows, splits=(), divs=(), meta=None) -> bytes:
    """rows: (date, o, h, l, c, v). Builds the same JSON shape Yahoo returns."""
    ts = [_ts(r[0]) for r in rows]
    q = {k: [r[i] for r in rows] for i, k in enumerate(("open", "high", "low", "close", "volume"), start=1)}
    ev = {}
    if splits:
        ev["splits"] = {str(_ts(s[0])): {"date": _ts(s[0]), "numerator": s[1], "denominator": s[2], "splitRatio": f"{s[1]}:{s[2]}"} for s in splits}
    if divs:
        ev["dividends"] = {str(_ts(x[0])): {"date": _ts(x[0]), "amount": x[1]} for x in divs}
    res = {"meta": meta or {"symbol": "DEMO.A", "instrumentType": "EQUITY", "currency": "USD"}, "timestamp": ts,
           "indicators": {"quote": [q], "adjclose": [{"adjclose": q["close"]}]}}
    if ev:
        res["events"] = ev
    return json.dumps({"chart": {"result": [res], "error": None}}).encode()


def test_yahoo_split_back_adjustment_is_undone():
    parsed = yahoo.parse_chart((REC / "yahoo_chart_NVDA_2024-06-03_2024-06-12.json").read_bytes())
    assert parsed["splits"] == [{"ex_date": "2024-06-10", "num": 10, "den": 1}]
    raw = {b["date"]: b for b in parsed["bars"]}
    assert raw["2024-06-07"]["close"].quantize(Decimal("0.001")) == Decimal("120.888")  # Yahoo's back-adjusted value
    data = yahoo.unadjust(parsed)
    bars = {b["date"]: b for b in data["bars"]}
    assert bars["2024-06-07"]["close"] == Decimal("1208.8800")  # what actually traded before the split
    assert bars["2024-06-07"]["split_factor"] == "10"
    assert bars["2024-06-10"]["close"] == Decimal("121.7900") and bars["2024-06-10"]["split_factor"] == "1"
    assert data["dividends"] == [{"ex_date": "2024-06-11", "amount": Decimal("0.01")}]


def test_ingest_drops_partial_and_non_session_bars_and_records_coverage(fixture_db):
    payload = chart_payload([("2026-10-23", 50, 51, 49, 50.5, 1000), ("2026-10-24", 50.5, 50.5, 50.5, 50.5, 0),
                             ("2026-10-26", 51, 52, 50, 51.7, 900)])
    parsed = yahoo.parse_chart(payload)
    with db.transaction(fixture_db):
        snap = prices.record_snapshot(fixture_db, source="test", url="https://query1.finance.yahoo.com/x", requested_at="2026-10-26T15:00:00.000000Z",
                                      retrieved_at="2026-10-26T15:00:00.000000Z", http_status=200, parse_status="ok")
        s = yahoo.ingest_chart(fixture_db, "DEMO.A", parsed, snap, "2026-10-26T15:00:00.000000Z", 60, "2026-10-01")
    assert s["stored"] == 1 and s["last_final_bar"] == "2026-10-23"
    assert s["non_session"] == ["2026-10-24"]          # Saturday bar: reported, never stored
    assert s["skipped_partial"] == ["2026-10-26"]      # Monday still trading at 11:00 ET
    assert prices.bar_for(fixture_db, "DEMO.A", "2026-10-26") is None
    assert corporate_actions.covered_through(fixture_db, "DEMO.A") == "2026-10-23"


def _ingest(conn, payload, retrieved="2026-10-28T13:00:00.000000Z", start="2026-10-01"):
    with db.transaction(conn):
        snap = prices.record_snapshot(conn, source="test", url="https://query1.finance.yahoo.com/x", requested_at=retrieved,
                                      retrieved_at=retrieved, http_status=200, parse_status="ok")
        return yahoo.ingest_chart(conn, "DEMO.A", yahoo.parse_chart(payload), snap, retrieved, 60, start)


def test_unexplained_jump_is_flagged_but_split_jump_is_not(fixture_db):
    s = _ingest(fixture_db, chart_payload([("2026-10-23", 50, 51, 49, 50, 1000), ("2026-10-26", 30, 31, 29, 30, 1000)]))
    assert s["flags"] == {"2026-10-26": ["unexplained_jump"]}
    assert "unexplained_jump" in prices.flags_of(prices.bar_for(fixture_db, "DEMO.A", "2026-10-26"))


def test_split_day_drop_is_explained(fixture_db):
    # Yahoo would report the pre-split day already divided by 2; un-adjusting restores 100 -> stored as traded.
    s = _ingest(fixture_db, chart_payload([("2026-10-23", 50, 51, 49, 50, 2000), ("2026-10-26", 50.5, 51, 50, 50.6, 1000)],
                                          splits=[("2026-10-26", 2, 1)]))
    assert s["flags"] == {}
    assert prices.bar_for(fixture_db, "DEMO.A", "2026-10-23")["close"] == "100.0000"
    assert prices.bar_for(fixture_db, "DEMO.A", "2026-10-23")["volume"] == 1000


def test_refetch_within_float_noise_is_not_a_revision_but_real_change_is(fixture_db):
    _ingest(fixture_db, chart_payload([("2026-10-23", 50, 51, 49, 50.5, 1000)]))
    s = _ingest(fixture_db, chart_payload([("2026-10-23", 50.0000001, 51, 49, 50.50001, 1000)]))
    assert s["duplicates"] == 1 and s["revised"] == 0
    s = _ingest(fixture_db, chart_payload([("2026-10-23", 50, 51, 49, 49.0, 1000)]))
    assert s["revised"] == 1
    revs = prices.revisions(fixture_db, "DEMO.A", "2026-10-23")
    assert len(revs) == 2 and prices.bar_for(fixture_db, "DEMO.A", "2026-10-23")["close"] == "50.5000"  # first observed wins


def test_directory_parse_and_identity_verification(fixture_db):
    nas, nas_ts = nasdaq.parse_directory((REC / "nasdaqtrader_nasdaqlisted_excerpt_2026-10-23.txt").read_text(), "nasdaq")
    oth, oth_ts = nasdaq.parse_directory((REC / "nasdaqtrader_otherlisted_excerpt_2026-10-23.txt").read_text(), "other")
    assert nas_ts.startswith("1023202621") and oth["SPY"]["exchange"] == "NYSE_ARCA" and oth["BRK.B"]["exchange"] == "NYSE"
    spy_meta = yahoo.parse_chart((REC / "yahoo_chart_SPY_1mo_retrieved_2026-10-24.json").read_bytes())["meta"]
    inst = {i.symbol: i for i in universe.FORWARD_UNIVERSE}
    row = lambda i: {"symbol": i.symbol, "asset_type": i.asset_type, "expected_exchange": i.expected_exchange,
                     "expected_name_keywords": i.expected_name_keywords}
    status, det = universe.verify_against_directory(row(inst["SPY"]), oth["SPY"], spy_meta)
    assert status == "VERIFIED", det
    status, det = universe.verify_against_directory(row(inst["BRK.B"]), oth["BRK.B"], {"instrumentType": "EQUITY", "currency": "USD"})
    assert status == "VERIFIED", det
    # Wrong share class: GOOG is Class C, our universe expects Class A keywords.
    status, det = universe.verify_against_directory(row(inst["GOOGL"]), nas["GOOG"], {"instrumentType": "EQUITY", "currency": "USD"})
    assert status == "FAILED"
    lev = {"symbol": "TQQQ", "asset_type": "ETF", "expected_exchange": "NASDAQ", "expected_name_keywords": "ProShares"}
    status, det = universe.verify_against_directory(lev, nas["TQQQ"], {"instrumentType": "ETF", "currency": "USD"})
    assert status == "FAILED" and any("leverage" in p for p in det["problems"])


@pytest.mark.parametrize("url", [
    "http://query1.finance.yahoo.com/v8/finance/chart/SPY",       # not HTTPS
    "https://api.alpaca.markets/v2/orders",                       # brokerage
    "https://paper-api.alpaca.markets/v2/orders",                 # brokerage "paper" endpoint is still a broker
    "https://api.ibkr.com/v1/api/mcp",
    "https://example.com/data",
])
def test_fetcher_refuses_non_allowlisted_urls(url):
    with pytest.raises(http.FetchRefused):
        http.check_url(url)


def test_sec_requires_explicit_user_agent(monkeypatch):
    monkeypatch.delenv("PAPER_ADVISOR_SEC_USER_AGENT", raising=False)
    with pytest.raises(http.FetchRefused):
        http.check_url("https://data.sec.gov/submissions/CIK0000320193.json")
    monkeypatch.setenv("PAPER_ADVISOR_SEC_USER_AGENT", "Example Person example@example.org")
    assert http.check_url("https://data.sec.gov/submissions/CIK0000320193.json") == "data.sec.gov"


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, headers))
        for prefix, resp in self.responses.items():
            if url.startswith(prefix):
                return resp
        return 404, {}, b"not found"


def test_fetcher_caches_evidence_and_records_snapshots(fixture_db, app_paths):
    clock.set_override(lambda: datetime(2026, 10, 24, 5, 0, tzinfo=UTC))
    body = (REC / "yahoo_chart_SPY_1mo_retrieved_2026-10-24.json").read_bytes()
    t = FakeTransport({"https://query1.finance.yahoo.com/v8/finance/chart/SPY": (200, {}, body)})
    f = http.Fetcher(fixture_db, app_paths.evidence_dir, transport=t)
    r = f.get("yahoo_chart_v8", "https://query1.finance.yahoo.com/v8/finance/chart/SPY", {"range": "1mo"}, ext="json")
    assert r.ok
    snap = fixture_db.execute("SELECT * FROM source_snapshots WHERE id=?", (r.snapshot_id,)).fetchone()
    stored = gzip.decompress((app_paths.evidence_dir / snap["content_path"]).read_bytes())
    assert stored == body and snap["http_status"] == 200 and snap["retrieved_at"] == "2026-10-24T05:00:00.000000Z"
    assert t.calls[0][1]["User-Agent"].startswith("Mozilla/5.0") and "Authorization" not in t.calls[0][1]
    bad = f.get("yahoo_chart_v8", "https://query1.finance.yahoo.com/v8/finance/chart/NOPE", ext="json")
    assert not bad.ok and fixture_db.execute("SELECT parse_status FROM source_snapshots WHERE id=?", (bad.snapshot_id,)).fetchone()[0] == "error"


def test_rss_parsing_and_dtd_refusal():
    items = news.parse_rss((REC / "yahoo_rss_AAPL_retrieved_2026-10-24.xml").read_bytes())
    assert items and all(i["title"] for i in items)
    assert all(i["published_at"] is None or i["published_at"].endswith("Z") for i in items)
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    with pytest.raises(news.UnsafeFeed):
        news.parse_rss(evil)


def test_earnings_calendar_parsing():
    rows = nasdaq.parse_earnings((REC / "nasdaq_earnings_2026-10-28_retrieved_2026-10-24.json").read_bytes())
    by = {r["symbol"]: r for r in rows}
    assert by["MSFT"]["time"] == "time-after-hours" and by["GOOGL"]["time"] == "time-after-hours"
    assert rows[0]["as_of"] == "Wed, Oct 28, 2026"


def test_redirects_to_non_allowlisted_hosts_are_refused():
    import urllib.request
    h = http.AllowlistRedirectHandler()
    req = urllib.request.Request("https://news.google.com/rss/search?q=x")
    with pytest.raises(http.FetchRefused):
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.net/steal")
    with pytest.raises(http.FetchRefused):
        h.redirect_request(req, None, 302, "Found", {}, "https://paper-api.alpaca.markets/v2/orders")
    ok = h.redirect_request(req, None, 302, "Found", {}, "https://news.google.com/rss/search?q=y")
    assert ok.full_url == "https://news.google.com/rss/search?q=y"


def test_dtd_in_another_encoding_is_still_refused():
    doc = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    body = doc.encode("utf-16")
    assert b"<!doctype" not in body.lower()  # the naive byte check alone would miss it
    with pytest.raises(news.UnsafeFeed):
        news.parse_rss(body)
