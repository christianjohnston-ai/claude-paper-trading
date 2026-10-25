"""Yahoo Finance chart API adapter (unofficial, keyless): daily bars, dividends, splits, metadata.

Known behaviors this adapter defends against (verified 2026-10-24, see tests/fixtures/recorded):
- Yahoo's "close"/"open"/... are BACK-ADJUSTED FOR SPLITS that happened after the bar date (NVDA's
  2024-06-07 close is returned as 120.888, but $1,208.88 actually traded). We multiply by the
  cumulative ratio of later splits to restore as-traded prices. Because Yahoo adjusts for every split
  up to the retrieval moment, we ALWAYS request through "now" so all such splits are in the
  response's events. Dividend amounts are split-adjusted the same way and are un-adjusted too.
- "adjclose" is also dividend-adjusted; we never use it (dividends are explicit cash events).
- Values are float32-noisy (765.8599853515625); we round to 4 dp.
- The last bar can be the CURRENT, still-trading session. We drop any bar whose session has not
  closed plus the finalization buffer at retrieval time.
- A bar on a date our NYSE calendar says was closed is not stored; it is reported, because either the
  calendar or the feed is wrong and a human should look.
- Missing values (nulls) appear for halted days; such bars are skipped and reported.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .. import clock, corporate_actions, market_calendar as cal, prices

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
JUMP_THRESHOLD = Decimal("0.35")  # >35% close-to-close move without a split is flagged for review
REVISION_TOLERANCE = Decimal("0.0005")  # re-fetched values within 5 bps are the same bar (float noise)
UTC = timezone.utc


def chart_url(symbol: str) -> str:
    """Index symbols contain '^' (e.g. ^VIX) which must be percent-encoded in the path."""
    return CHART_URL.format(symbol=urllib.parse.quote(symbol, safe=""))


def parse_chart(body: bytes) -> dict:
    d = json.loads(body)
    chart = d.get("chart") or {}
    if chart.get("error"):
        return {"error": str(chart["error"]), "bars": [], "dividends": [], "splits": [], "meta": {}}
    results = chart.get("result") or []
    if not results:
        return {"error": "empty result", "bars": [], "dividends": [], "splits": [], "meta": {}}
    r = results[0]
    meta = r.get("meta") or {}
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    bars, nulls = [], []
    for i, t in enumerate(ts):
        vals = [q.get(k, [None] * len(ts))[i] if i < len(q.get(k) or []) else None for k in ("open", "high", "low", "close", "volume")]
        d_ny = datetime.fromtimestamp(t, tz=UTC).astimezone(cal.NY).date()
        if any(v is None for v in vals[:4]):
            nulls.append(d_ny.isoformat())
            continue
        bars.append({"date": d_ny.isoformat(), "ts": clock.iso(datetime.fromtimestamp(t, tz=UTC)),
                     "open": Decimal(repr(vals[0])), "high": Decimal(repr(vals[1])), "low": Decimal(repr(vals[2])),
                     "close": Decimal(repr(vals[3])), "volume": int(vals[4] or 0)})
    ev = r.get("events") or {}
    divs = [{"ex_date": datetime.fromtimestamp(int(x["date"]), tz=UTC).astimezone(cal.NY).date().isoformat(),
             "amount": Decimal(repr(x["amount"]))} for x in (ev.get("dividends") or {}).values()]
    splits = [{"ex_date": datetime.fromtimestamp(int(x["date"]), tz=UTC).astimezone(cal.NY).date().isoformat(),
               "num": int(Decimal(repr(x["numerator"]))), "den": int(Decimal(repr(x["denominator"])))}
              for x in (ev.get("splits") or {}).values()]
    return {"error": None, "meta": meta, "bars": sorted(bars, key=lambda b: b["date"]), "null_bars": nulls,
            "dividends": sorted(divs, key=lambda x: x["ex_date"]), "splits": sorted(splits, key=lambda x: x["ex_date"])}


def split_factor_after(splits: list[dict], d: str) -> Decimal:
    f = Decimal(1)
    for s in splits:
        if s["ex_date"] > d:
            f *= Decimal(s["num"]) / Decimal(s["den"])
    return f


def unadjust(parsed: dict) -> dict:
    """Convert Yahoo's split-back-adjusted values to as-traded values (prices x factor, volume / factor)."""
    out_bars = []
    for b in parsed["bars"]:
        f = split_factor_after(parsed["splits"], b["date"])
        out_bars.append({**b, "open": (b["open"] * f).quantize(prices.PRICE_PLACES), "high": (b["high"] * f).quantize(prices.PRICE_PLACES),
                         "low": (b["low"] * f).quantize(prices.PRICE_PLACES), "close": (b["close"] * f).quantize(prices.PRICE_PLACES),
                         "volume": int((Decimal(b["volume"]) / f).to_integral_value()), "split_factor": format(f.normalize(), "f")})
    divs = [{**x, "amount": (x["amount"] * split_factor_after(parsed["splits"], x["ex_date"])).quantize(Decimal("0.000001")).normalize()}
            for x in parsed["dividends"]]
    return {**parsed, "bars": out_bars, "dividends": divs}


def chart_params(start: date, end_utc: datetime) -> dict:
    p1 = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp())
    return {"period1": p1, "period2": int(end_utc.timestamp()) + 1, "interval": "1d", "events": "div,splits",
            "includeAdjustedClose": "true"}


def _close_enough(a: Decimal, b: Decimal) -> bool:
    return b != 0 and abs(a - b) / abs(b) <= REVISION_TOLERANCE


def ingest_chart(conn, symbol: str, parsed: dict, snapshot_id: int, retrieved_at: str, finalization_minutes: int,
                 requested_start: str) -> dict:
    """Store final bars, corporate actions and coverage for one symbol. Must be inside a transaction."""
    summary = {"symbol": symbol, "stored": 0, "duplicates": 0, "revised": 0, "skipped_partial": [], "non_session": [],
               "null_bars": parsed.get("null_bars", []), "flags": {}, "corporate_actions": [], "last_final_bar": None,
               "conflicts": []}
    if parsed.get("error"):
        summary["error"] = parsed["error"]
        return summary
    data = unadjust(parsed)
    retrieved = clock.parse_iso(retrieved_at)
    for s in data["splits"]:
        _, st = corporate_actions.record(conn, symbol, "SPLIT", s["ex_date"], ratio=(s["num"], s["den"]), source="yahoo_chart_v8",
                                         snapshot_id=snapshot_id, now=retrieved_at)
        summary["corporate_actions"].append(f"split {s['num']}:{s['den']} ex {s['ex_date']} ({st})")
        if st == "conflict":
            summary["conflicts"].append(f"split {s['ex_date']}")
    for dv in data["dividends"]:
        _, st = corporate_actions.record(conn, symbol, "CASH_DIVIDEND", dv["ex_date"], amount=dv["amount"], source="yahoo_chart_v8",
                                         snapshot_id=snapshot_id, now=retrieved_at)
        summary["corporate_actions"].append(f"dividend {dv['amount']} ex {dv['ex_date']} ({st})")
        if st == "conflict":
            summary["conflicts"].append(f"dividend {dv['ex_date']}")
    prev_close = None
    prev_date = None
    prev_bar = None
    for b in data["bars"]:
        sess = cal.session(date.fromisoformat(b["date"]))
        if sess is None:
            summary["non_session"].append(b["date"])
            continue
        if sess.close_utc + timedelta(minutes=finalization_minutes) > retrieved:
            summary["skipped_partial"].append(b["date"])
            continue
        flags = []
        if prev_bar is None:
            prev_bar = prices.latest_bar(conn, symbol, on_or_before=(date.fromisoformat(b["date"]) - timedelta(days=1)).isoformat())
            if prev_bar is not None:
                prev_close, prev_date = Decimal(prev_bar["close"]), prev_bar["session_date"]
        if prev_close is not None and prev_close > 0:
            ratio = Decimal(1)
            for s in data["splits"]:
                if prev_date < s["ex_date"] <= b["date"]:
                    ratio *= Decimal(s["den"]) / Decimal(s["num"])
            move = b["close"] / (prev_close * ratio) - 1
            if abs(move) > JUMP_THRESHOLD:
                flags.append("unexplained_jump")
        existing = prices.bar_for(conn, symbol, b["date"])
        if existing is not None and all(_close_enough(b[k], Decimal(existing[k])) for k in ("open", "high", "low", "close")):
            summary["duplicates"] += 1
        else:
            _, st = prices.insert_bar(conn, symbol, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"],
                                      source="yahoo_chart_v8", snapshot_id=snapshot_id, retrieved_at=retrieved_at,
                                      market_ts=b["ts"], split_factor=b["split_factor"], quality_flags=flags)
            summary["stored" if st == "inserted" else ("revised" if st == "revised" else "duplicates")] += 1
            if flags or st == "revised":
                summary["flags"][b["date"]] = flags + (["revised"] if st == "revised" else [])
        prev_close, prev_date = b["close"], b["date"]
        summary["last_final_bar"] = b["date"]
    if summary["last_final_bar"]:
        corporate_actions.set_coverage(conn, symbol, requested_start, summary["last_final_bar"], retrieved_at, snapshot_id)
    return summary


def meta_summary(parsed: dict) -> dict:
    m = parsed.get("meta") or {}
    return {k: m.get(k) for k in ("symbol", "instrumentType", "currency", "exchangeName", "fullExchangeName", "longName",
                                  "regularMarketTime", "exchangeTimezoneName")}
