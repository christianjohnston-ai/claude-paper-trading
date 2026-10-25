"""Headline RSS adapters (Yahoo Finance per-symbol feed, Google News search).

Headlines are evidence of what was publicly reported and WHEN (publication time from the feed,
retrieval time from us). They are not verified facts: the morning command requires the advisor to
treat a single headline as unconfirmed and to cite the URL and timestamps of anything it relies on.

Parsing safety: feeds are untrusted XML. We refuse documents that declare a DTD or entities
(entity-expansion attacks) and cap the size before handing them to ElementTree.
"""
from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from xml.parsers import expat
from datetime import timezone
from email.utils import parsedate_to_datetime

from .. import clock

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"


class UnsafeFeed(ValueError):
    pass


def _deny(*_args):
    raise UnsafeFeed("feed declares a DTD/entities; refusing to parse")


def _reject_dtd(body: bytes) -> None:
    """Validation pass with expat on the DECODED document (a byte search alone can be bypassed with
    another encoding such as UTF-16). Any doctype or entity declaration aborts before ElementTree
    ever sees the document. defusedxml would do the same, but the app stays stdlib-only."""
    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = _deny
    p.EntityDeclHandler = _deny
    p.UnparsedEntityDeclHandler = _deny
    p.ExternalEntityRefHandler = _deny
    try:
        p.Parse(body, True)
    except expat.ExpatError as exc:
        raise UnsafeFeed(f"malformed XML: {exc}") from exc


def parse_rss(body: bytes, max_items: int = 15) -> list[dict]:
    if len(body) > 4 * 1024 * 1024:
        raise UnsafeFeed("feed too large")
    if b"<!doctype" in body[:4096].lower() or b"<!entity" in body.lower():
        raise UnsafeFeed("feed declares a DTD/entities; refusing to parse")
    _reject_dtd(body)
    root = ET.fromstring(body)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        src = it.find("source")
        published = None
        if pub:
            try:
                published = clock.iso(parsedate_to_datetime(pub.strip()).astimezone(timezone.utc))
            except (TypeError, ValueError, IndexError):
                published = None
        items.append({"title": title, "link": link, "published_at": published,
                      "publisher": (src.text.strip() if src is not None and src.text else None),
                      "summary": (it.findtext("description") or "").strip()[:500]})
        if len(items) >= max_items:
            break
    return items


def google_news_query(symbol: str, name: str) -> dict:
    return {"q": f'"{name}" OR {symbol} stock when:3d', "hl": "en-US", "gl": "US", "ceid": "US:en"}


def yahoo_params(symbol: str) -> dict:
    return {"s": symbol, "region": "US", "lang": "en-US"}


def safe_link(link: str) -> str | None:
    parts = urllib.parse.urlsplit(link)
    return link if parts.scheme in ("http", "https") else None
