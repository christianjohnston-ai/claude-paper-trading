"""Single source of "now" for the whole app, plus canonical UTC timestamp formatting.

Why an injectable clock: the isolated fixture demo and the tests must replay several market
sessions in seconds. Why it is dangerous: a fake clock on the REAL experiment would let a decision
appear to precede a price it had already seen. So the override is refused for forward-mode
databases (see runs.assert_clock_allowed); only fixture databases may use it.

Canonical timestamp format (SYNC WARNING: the SQLite schema has GLOB CHECK constraints on this exact
shape in migrations/0001_initial.sql, and string comparison of these values is used for
"fill strictly after decision"; never store another format):
    YYYY-MM-DDTHH:MM:SS.ffffffZ   (UTC, microseconds, literal Z)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

UTC = timezone.utc
_override: Callable[[], datetime] | None = None


def now_utc() -> datetime:
    if _override is not None:
        return _override().astimezone(UTC)
    return datetime.now(UTC)


def set_override(fn: Callable[[], datetime]) -> None:
    global _override
    _override = fn


def clear_override() -> None:
    global _override
    _override = None


def override_active() -> bool:
    return _override is not None


class SteppableClock:
    """Test/fixture clock that only moves when told to."""

    def __init__(self, start: datetime):
        self.current = start.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.current

    def set(self, when: datetime) -> None:
        self.current = when.astimezone(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("refusing to format a naive datetime; all timestamps must be timezone-aware")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {s!r}")
    return dt.astimezone(UTC)


def now_iso() -> str:
    return iso(now_utc())
