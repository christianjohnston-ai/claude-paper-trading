"""Proof obligations for "no execution path can submit a live brokerage order".

1. No brokerage SDK, host or order endpoint appears anywhere in the application package.
2. The only module that can open network connections is data_sources/http.py, and it is GET-only
   with an allowlist that contains no brokerage host (tested in test_data_sources.py too).
3. The execution path (decision submission + settlement) runs with all sockets disabled.
4. Skill subprocesses never inherit brokerage credentials from the environment.
"""
import json
import os
import re
import socket
from pathlib import Path

import pytest

from paper_advisor import broker_sim, skills_bridge, submission
from paper_advisor.data_sources import http

PKG = Path(__file__).resolve().parent.parent / "paper_advisor"
BROKER_PATTERNS = [r"\balpaca\b", r"alpaca\.markets", r"\bib_insync\b", r"\bibapi\b", r"api\.ibkr\.com", r"\btradier\b",
                   r"robin_stocks", r"\bwebull\b", r"schwab", r"tdameritrade", r"MetaTrader5", r"order_send", r"submit_order",
                   r"place_order", r"/v2/orders", r"tastytrade", r"oanda", r"etrade"]
NETWORK_IMPORTS = [r"^\s*import (urllib\.request|http\.client|socket|requests|httpx|aiohttp)", r"^\s*from (urllib\.request|http\.client|requests|httpx) import"]


def _sources():
    for p in PKG.rglob("*.py"):
        yield p, p.read_text(encoding="utf-8")


def test_no_brokerage_code_anywhere_in_the_package():
    hits = []
    for p, src in _sources():
        if p.name == "skills_bridge.py":
            # The registry text names disabled brokerage skills to explain WHY they are disabled; code below it must not.
            src = src.split("ADVICE_KEYS")[1]
        for pat in BROKER_PATTERNS:
            for m in re.finditer(pat, src, flags=re.I):
                hits.append(f"{p.relative_to(PKG)}: {pat}")
    assert hits == []


def test_only_the_allowlisted_fetcher_can_open_connections():
    offenders = []
    for p, src in _sources():
        for pat in NETWORK_IMPORTS:
            if re.search(pat, src, flags=re.M) and p.relative_to(PKG).as_posix() != "data_sources/http.py":
                offenders.append(str(p.relative_to(PKG)))
    assert offenders == []
    http_src = (PKG / "data_sources" / "http.py").read_text()
    assert 'method="GET"' in http_src and "POST" not in http_src.replace("POSTs", "")
    assert not any(re.search(b, " ".join(http.ALLOWED_HOSTS), re.I) for b in ("alpaca", "ibkr", "schwab", "tradier", "broker"))


def test_execution_path_runs_with_sockets_disabled(fixture_db, monkeypatch):
    from helpers import World
    from test_risk import FRESH, d
    w = World(fixture_db, start="2026-10-23T23:00:00")
    w.bars_all("2026-10-23", FRESH)
    w.cover_corporate_actions("2026-12-31")
    w.at("2026-10-24T05:00:00")

    def no_network(*a, **k):
        raise AssertionError("the execution path attempted a network connection")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    out = submission.apply_decisions(fixture_db, w.exp_id, w.new_run(), [d("BUY", "DEMO.A", notional_usd="100")], now=w.clock())
    assert out[0].status == "ACCEPTED"
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", FRESH)
    w.at("2026-10-27T13:00:00")
    rep = broker_sim.settle(fixture_db, w.exp_id, now=w.clock())
    assert len(rep.fills) == 2  # advisor buy + benchmark buy, simulated locally


def test_skill_subprocesses_do_not_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake-key-should-not-leak")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("FMP_API_KEY", "fake-fmp")
    rc, out, err = skills_bridge._run([skills_bridge._python(), "-I", "-c", "import os,json;print(json.dumps(sorted(os.environ)))"], tmp_path)
    assert rc == 0
    names = set(json.loads(out))
    assert not {n for n in names if "ALPACA" in n or "API_KEY" in n or "TOKEN" in n or "SECRET" in n}
