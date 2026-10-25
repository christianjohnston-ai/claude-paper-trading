"""Nasdaq Trader symbol directories (official listing data) and the Nasdaq earnings calendar.

Directory files are pipe-delimited with a footer "File Creation Time: MMDDYYYYHH:MM". We parse:
  nasdaqlisted.txt: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
  otherlisted.txt:  ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol

Earnings calendar rows include a "time" of time-pre-market / time-after-hours / time-not-supplied.
Dates on this calendar can be estimates until companies confirm them; the briefing says so. A date
whose calendar request fails is reported as UNKNOWN rather than treated as "no earnings" (the audited
earnings-calendar skill silently dropped events on fetch failures; we fail loudly instead).
"""
from __future__ import annotations

import json

from .. import universe

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"


def parse_directory(text: str, kind: str) -> tuple[dict, str | None]:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return {}, None
    header = lines[0].split("|")
    footer = None
    rows = {}
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            footer = line.split(":", 1)[1].split("|")[0].strip()
            continue
        parts = line.split("|")
        rec = dict(zip(header, parts))
        if kind == "nasdaq":
            sym = rec.get("Symbol", "").strip()
            row = {"symbol": sym, "security_name": rec.get("Security Name", ""), "exchange": "NASDAQ",
                   "etf": rec.get("ETF", ""), "test_issue": rec.get("Test Issue", ""), "source": "nasdaqlisted.txt"}
        else:
            sym = rec.get("ACT Symbol", "").strip()
            code = rec.get("Exchange", "")
            row = {"symbol": sym, "security_name": rec.get("Security Name", ""),
                   "exchange": universe.OTHERLISTED_EXCHANGE_CODES.get(code, f"UNKNOWN:{code}"), "etf": rec.get("ETF", ""),
                   "test_issue": rec.get("Test Issue", ""), "cqs_symbol": rec.get("CQS Symbol", ""), "source": "otherlisted.txt"}
        if sym:
            rows[sym] = row
    return rows, footer


def parse_earnings(body: bytes) -> list[dict]:
    d = json.loads(body)
    data = d.get("data") or {}
    rows = data.get("rows") or []
    out = []
    for r in rows:
        out.append({"symbol": (r.get("symbol") or "").strip().upper(), "time": r.get("time") or "time-not-supplied",
                    "name": r.get("name"), "fiscal_quarter_ending": r.get("fiscalQuarterEnding"),
                    "eps_forecast": r.get("epsForecast"), "as_of": data.get("asOf")})
    return out


TIME_LABEL = {"time-pre-market": "before the open", "time-after-hours": "after the close", "time-not-supplied": "time not supplied"}
