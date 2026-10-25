"""The research universe: a small, documented list of verified US-listed instruments.

Why small and fixed: the experiment measures decision quality, not breadth. A fixed universe of
liquid large caps and unleveraged ETFs keeps data requests modest, makes survivorship explicit (the
list is dated, see UNIVERSE_VERSION), and lets every instrument's identity be verified on every run.

Identity verification (forward mode) cross-checks two independent sources:
  1. Nasdaq Trader official symbol directories (nasdaqlisted.txt / otherlisted.txt): the symbol is
     listed, not a test issue, on the expected exchange, the ETF flag matches the asset type, and
     the security name contains the expected issuer keywords.
  2. Yahoo chart metadata: instrumentType (EQUITY/ETF) and currency USD for the mapped Yahoo symbol.
A BUY is only allowed for an instrument with a VERIFIED result in the last 14 days.

Share-class mapping (the classic ticker-mapping trap): canonical symbols follow the listing
directory ("BRK.B"); Yahoo uses dashes ("BRK-B"). GOOGL is Alphabet Class A; Class C (GOOG) is a
different security and is deliberately not in the universe.

Leveraged/inverse products cannot be added at all: instruments.leverage has CHECK(leverage = '1x').

SYNC WARNING: UNIVERSE_VERSION and FORWARD_UNIVERSE are documented in README.md ("Research
universe"); change both together, and add a new experiment if the universe changes materially,
because changing the opportunity set mid-experiment changes what results mean.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import clock, db

UNIVERSE_VERSION = "2026-10-24.v1"
VERIFICATION_MAX_AGE_DAYS = 14


@dataclass(frozen=True)
class Instrument:
    symbol: str
    yahoo_symbol: str
    name: str
    asset_type: str  # COMMON_STOCK | ETF
    expected_exchange: str  # NASDAQ | NYSE | NYSE_ARCA | FIXTURE
    expected_name_keywords: str
    is_benchmark: bool = False
    share_class_note: str | None = None
    notes: str | None = None


FORWARD_UNIVERSE: list[Instrument] = [
    Instrument("SPY", "SPY", "SPDR S&P 500 ETF Trust", "ETF", "NYSE_ARCA", "SPDR S&P 500 ETF", is_benchmark=True,
               notes="Benchmark; also tradeable"),
    Instrument("QQQ", "QQQ", "Invesco QQQ Trust, Series 1", "ETF", "NASDAQ", "Invesco QQQ Trust"),
    Instrument("IWM", "IWM", "iShares Russell 2000 ETF", "ETF", "NYSE_ARCA", "iShares Russell 2000"),
    Instrument("DIA", "DIA", "SPDR Dow Jones Industrial Average ETF Trust", "ETF", "NYSE_ARCA", "SPDR Dow Jones Industrial Average"),
    Instrument("XLK", "XLK", "Technology Select Sector SPDR ETF", "ETF", "NYSE_ARCA", "Technology Select Sector SPDR"),
    Instrument("XLF", "XLF", "Financial Select Sector SPDR ETF", "ETF", "NYSE_ARCA", "Financial Select Sector SPDR"),
    Instrument("XLE", "XLE", "Energy Select Sector SPDR ETF", "ETF", "NYSE_ARCA", "Energy Select Sector SPDR"),
    Instrument("XLV", "XLV", "Health Care Select Sector SPDR ETF", "ETF", "NYSE_ARCA", "Health Care Select Sector SPDR"),
    Instrument("XLP", "XLP", "Consumer Staples Select Sector SPDR ETF", "ETF", "NYSE_ARCA", "Consumer Staples Select Sector SPDR"),
    Instrument("TLT", "TLT", "iShares 20+ Year Treasury Bond ETF", "ETF", "NASDAQ", "iShares 20+ Year Treasury Bond"),
    Instrument("AAPL", "AAPL", "Apple Inc.", "COMMON_STOCK", "NASDAQ", "Apple Inc."),
    Instrument("MSFT", "MSFT", "Microsoft Corporation", "COMMON_STOCK", "NASDAQ", "Microsoft Corporation"),
    Instrument("NVDA", "NVDA", "NVIDIA Corporation", "COMMON_STOCK", "NASDAQ", "NVIDIA Corporation"),
    Instrument("AMZN", "AMZN", "Amazon.com, Inc.", "COMMON_STOCK", "NASDAQ", "Amazon.com"),
    Instrument("GOOGL", "GOOGL", "Alphabet Inc. Class A", "COMMON_STOCK", "NASDAQ", "Alphabet Inc. - Class A",
               share_class_note="Class A (GOOGL); Class C (GOOG) is a separate security and not in the universe"),
    Instrument("META", "META", "Meta Platforms, Inc. Class A", "COMMON_STOCK", "NASDAQ", "Meta Platforms"),
    Instrument("BRK.B", "BRK-B", "Berkshire Hathaway Inc. Class B", "COMMON_STOCK", "NYSE", "Berkshire Hathaway",
               share_class_note="Class B. Listing symbol BRK.B; Yahoo symbol BRK-B. Class A (BRK.A) is not in the universe"),
    Instrument("JPM", "JPM", "JPMorgan Chase & Co.", "COMMON_STOCK", "NYSE", "JP Morgan Chase"),
    Instrument("XOM", "XOM", "Exxon Mobil", "COMMON_STOCK", "NYSE", "Exxon",
               notes="Listed as 'ExxonMobil Holdings Corporation' in the 2026-10-23 Nasdaq Trader directory"),
    Instrument("UNH", "UNH", "UnitedHealth Group Incorporated", "COMMON_STOCK", "NYSE", "UnitedHealth Group"),
    Instrument("JNJ", "JNJ", "Johnson & Johnson", "COMMON_STOCK", "NYSE", "Johnson & Johnson"),
    Instrument("PG", "PG", "Procter & Gamble Company", "COMMON_STOCK", "NYSE", "Procter & Gamble"),
    Instrument("COST", "COST", "Costco Wholesale Corporation", "COMMON_STOCK", "NASDAQ", "Costco Wholesale"),
    Instrument("LLY", "LLY", "Eli Lilly and Company", "COMMON_STOCK", "NYSE", "Eli Lilly"),
]

ALLOWED_ASSET_TYPES = ("COMMON_STOCK", "ETF")
# Exchange codes used by the Nasdaq Trader otherlisted.txt "Exchange" column.
OTHERLISTED_EXCHANGE_CODES = {"N": "NYSE", "P": "NYSE_ARCA", "A": "NYSE_AMERICAN", "Z": "CBOE_BZX", "V": "IEX"}


def install(conn, instruments, now: str, verification_source: str | None = None) -> None:
    """Insert instruments that are not yet present. verification_source='fixture' marks synthetic
    instruments as verified; that shortcut is refused outside fixture databases."""
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if verification_source == "fixture" and db.db_mode(conn) != "fixture":
            raise RuntimeError("fixture verification is only allowed in a fixture database")
        for inst in instruments:
            conn.execute(
                "INSERT OR IGNORE INTO instruments(symbol, yahoo_symbol, name, asset_type, expected_exchange,"
                " expected_name_keywords, share_class_note, leverage, is_benchmark, in_universe, added_at, notes)"
                " VALUES (?,?,?,?,?,?,?, '1x', ?, 1, ?, ?)",
                (inst.symbol, inst.yahoo_symbol, inst.name, inst.asset_type, inst.expected_exchange,
                 inst.expected_name_keywords, inst.share_class_note, int(inst.is_benchmark), now, inst.notes))
            if verification_source == "fixture":
                record_verification(conn, inst.symbol, "VERIFIED", {"source": "fixture", "note": "synthetic instrument"}, now)
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise


def record_verification(conn, symbol: str, status: str, details: dict, now: str, run_id: int | None = None) -> None:
    import json
    conn.execute("INSERT INTO instrument_verifications(symbol, verified_at, status, details_json, run_id) VALUES (?,?,?,?,?)",
                 (symbol, now, status, json.dumps(details, sort_keys=True), run_id))


def get(conn, symbol: str):
    return conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()


def symbols(conn, include_benchmark: bool = True) -> list[str]:
    sql = "SELECT symbol FROM instruments WHERE in_universe=1" + ("" if include_benchmark else " AND is_benchmark=0")
    return [r["symbol"] for r in conn.execute(sql + " ORDER BY symbol")]


def latest_verification(conn, symbol: str):
    return conn.execute("SELECT * FROM instrument_verifications WHERE symbol=? ORDER BY verified_at DESC, id DESC LIMIT 1",
                        (symbol,)).fetchone()


def verified_as_fixture(conn, symbol: str) -> bool:
    """True when the latest verification is the fixture scenario's own (synthetic instrument), not a listing check."""
    v = latest_verification(conn, symbol)
    try:
        return v is not None and json.loads(v["details_json"] or "{}").get("source") == "fixture"
    except (TypeError, ValueError):
        return False


def is_verified(conn, symbol: str, now_iso: str) -> tuple[bool, str]:
    v = latest_verification(conn, symbol)
    if v is None:
        return False, "never verified"
    if v["status"] != "VERIFIED":
        return False, f"latest verification {v['status']} at {v['verified_at']}"
    age_days = (clock.parse_iso(now_iso) - clock.parse_iso(v["verified_at"])).days
    if age_days > VERIFICATION_MAX_AGE_DAYS:
        return False, f"verification is {age_days} days old"
    return True, f"verified at {v['verified_at']}"


def verify_against_directory(inst_row, directory_row: dict | None, yahoo_meta: dict | None) -> tuple[str, dict]:
    """Pure identity check. directory_row is a parsed Nasdaq Trader row (see data_sources.nasdaq);
    yahoo_meta is the 'meta' object of a Yahoo chart response. Returns (status, details)."""
    problems, details = [], {"symbol": inst_row["symbol"]}
    if directory_row is None:
        problems.append("not found in Nasdaq Trader symbol directories")
    else:
        details["directory"] = directory_row
        if directory_row.get("test_issue") != "N":
            problems.append("listed as a test issue")
        if directory_row.get("exchange") != inst_row["expected_exchange"]:
            problems.append(f"exchange {directory_row.get('exchange')} != expected {inst_row['expected_exchange']}")
        want_etf = "Y" if inst_row["asset_type"] == "ETF" else "N"
        if directory_row.get("etf") != want_etf:
            problems.append(f"ETF flag {directory_row.get('etf')} does not match asset type {inst_row['asset_type']}")
        name = directory_row.get("security_name", "")
        if inst_row["expected_name_keywords"].lower() not in name.lower():
            problems.append(f"security name {name!r} lacks {inst_row['expected_name_keywords']!r}")
        for bad in ("ultra", "2x", "3x", "-1x", "inverse", "short ", "bear ", "leveraged"):
            if bad in name.lower():
                problems.append(f"name suggests leverage/inverse exposure ({bad.strip()})")
    if yahoo_meta is None:
        problems.append("no Yahoo metadata")
    else:
        details["yahoo"] = {k: yahoo_meta.get(k) for k in ("symbol", "instrumentType", "currency", "fullExchangeName", "longName")}
        want = "ETF" if inst_row["asset_type"] == "ETF" else "EQUITY"
        if yahoo_meta.get("instrumentType") != want:
            problems.append(f"Yahoo instrumentType {yahoo_meta.get('instrumentType')} != {want}")
        if yahoo_meta.get("currency") != "USD":
            problems.append(f"currency {yahoo_meta.get('currency')} != USD")
    details["problems"] = problems
    return ("VERIFIED" if not problems else "FAILED"), details
