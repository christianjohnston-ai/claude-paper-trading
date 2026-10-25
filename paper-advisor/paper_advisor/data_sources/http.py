"""GET-only, host-allowlisted HTTP fetcher that caches every response as evidence.

Why so restrictive: this is the only code in the app that talks to the network. Keeping it GET-only
and allowlisted makes "no execution path can reach a broker" a property you can check by reading
one list (ALLOWED_HOSTS), and every response that a decision or valuation relies on is saved
(gzip, named by sha256) and recorded in source_snapshots with request and retrieval times, so a
later reader can reproduce what the advisor saw.

What is sent to these hosts: only the URL (ticker symbols, dates) and a User-Agent. No account data,
no credentials, no portfolio information. SEC EDGAR requires a contact in the User-Agent; we never
invent one or use the user's email: set PAPER_ADVISOR_SEC_USER_AGENT yourself to enable SEC hosts.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import clock, db, prices

# SYNC WARNING: README.md "External data requests" lists these hosts; keep them identical.
ALLOWED_HOSTS = {
    "query1.finance.yahoo.com": "Yahoo Finance chart API (daily bars, dividends, splits, instrument metadata)",
    "query2.finance.yahoo.com": "Yahoo Finance chart API (fallback host)",
    "feeds.finance.yahoo.com": "Yahoo Finance headline RSS",
    "news.google.com": "Google News RSS search",
    "www.nasdaqtrader.com": "Nasdaq Trader official symbol directories",
    "api.nasdaq.com": "Nasdaq earnings calendar",
    "www.sec.gov": "SEC EDGAR (only when PAPER_ADVISOR_SEC_USER_AGENT is set)",
    "data.sec.gov": "SEC EDGAR JSON APIs (only when PAPER_ADVISOR_SEC_USER_AGENT is set)",
}
SEC_HOSTS = {"www.sec.gov", "data.sec.gov"}
DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) paper-advisor/1.0 (local paper-trading research tool)"
MAX_BYTES = 8 * 1024 * 1024


class FetchRefused(RuntimeError):
    pass


@dataclass
class HttpResult:
    url: str
    status: int | None
    body: bytes | None
    requested_at: str
    retrieved_at: str
    snapshot_id: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and self.body is not None


class AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib follows redirects automatically; without this, an allowlisted host could bounce a request
    to ANY host and the allowlist guarantee would be fiction. Every redirect target is re-checked."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # raises FetchRefused for non-HTTPS or non-allowlisted targets
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(AllowlistRedirectHandler)


class UrllibTransport:
    def get(self, url: str, headers: dict, timeout: float):
        check_url(url)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with _OPENER.open(req, timeout=timeout) as r:  # noqa: S310 - host allowlisted, redirects re-checked
                final = r.geturl()
                check_url(final)
                hdrs = dict(r.headers)
                hdrs["X-Final-URL"] = final
                return r.status, hdrs, r.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read(MAX_BYTES + 1) if e.fp else b""


def check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise FetchRefused(f"refusing non-HTTPS URL {url}")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise FetchRefused(f"host {host!r} is not on the market-data allowlist")
    if host in SEC_HOSTS and not os.environ.get("PAPER_ADVISOR_SEC_USER_AGENT"):
        raise FetchRefused("SEC EDGAR requires a contact User-Agent; set PAPER_ADVISOR_SEC_USER_AGENT to enable it")
    return host


class Fetcher:
    def __init__(self, conn, evidence_dir, transport=None, run_id: int | None = None, min_interval: float = 0.35,
                 timeout: float = 20.0, retries: int = 2):
        self.conn = conn
        self.evidence_dir = Path(evidence_dir)
        self.transport = transport or UrllibTransport()
        self.run_id = run_id
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last_hit: dict[str, float] = {}

    def _store(self, body: bytes, ext: str) -> tuple[str, str]:
        sha = hashlib.sha256(body).hexdigest()
        day = clock.now_utc().strftime("%Y/%m/%d")
        rel = Path(day) / f"{sha}.{ext}.gz"
        dest = self.evidence_dir / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            with gzip.open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, dest)
        return sha, str(rel)

    def get(self, source: str, url: str, params: dict | None = None, accept: str = "*/*", ext: str = "bin") -> HttpResult:
        full = url if not params else f"{url}?{urllib.parse.urlencode(params)}"
        host = check_url(full)
        headers = {"User-Agent": os.environ.get("PAPER_ADVISOR_SEC_USER_AGENT") if host in SEC_HOSTS else DEFAULT_UA,
                   "Accept": accept}
        requested_at = clock.now_iso()
        status, body, err = None, None, None
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_hit.get(host, 0.0))
            if wait > 0 and not clock.override_active():
                time.sleep(wait)
            try:
                status, _hdrs, body = self.transport.get(full, headers, self.timeout)
                self._last_hit[host] = time.monotonic()
                if body is not None and len(body) > MAX_BYTES:
                    status, body, err = None, None, f"response larger than {MAX_BYTES} bytes"
                    break
                if status is not None and status >= 500 and attempt < self.retries:
                    if not clock.override_active():
                        time.sleep(1.5 * (attempt + 1))
                    continue
                err = None if status is not None and 200 <= status < 300 else f"HTTP {status}"
                break
            except Exception as exc:  # network errors, timeouts
                status, body, err = None, None, f"{type(exc).__name__}: {exc}"
                if attempt < self.retries and not clock.override_active():
                    time.sleep(1.5 * (attempt + 1))
        retrieved_at = clock.now_iso()
        sha = rel = None
        if body:
            sha, rel = self._store(body, ext)
        with db.transaction(self.conn):
            sid = prices.record_snapshot(self.conn, source=source, url=full, requested_at=requested_at, retrieved_at=retrieved_at,
                                         http_status=status, parse_status="fetched" if err is None else "error",
                                         content_sha256=sha, content_path=rel, bytes_=len(body) if body else None,
                                         error=err, run_id=self.run_id)
        return HttpResult(full, status, body if err is None else body, requested_at, retrieved_at, sid, err)
