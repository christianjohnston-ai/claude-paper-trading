"""Shared test fixtures. Every test gets its own temporary app home, so no test can touch the real
experiment database at paper-advisor/data/paper_advisor.sqlite."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from paper_advisor import clock, db, market_calendar
from paper_advisor.paths import AppPaths

UTC = timezone.utc


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    p = AppPaths(tmp_path / "app")
    p.ensure_dirs()
    return p


@pytest.fixture
def fixture_db(app_paths):
    """A migrated FIXTURE-mode database (synthetic data allowed, injected clock allowed)."""
    conn = db.init_db(app_paths.fixture_db_path, db_mode="fixture")
    yield conn
    conn.close()


@pytest.fixture
def forward_db(app_paths):
    """A migrated FORWARD-mode database (as used by the real experiment) in a temp directory."""
    conn = db.init_db(app_paths.db_path, db_mode="forward")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _reset_global_state():
    yield
    clock.clear_override()
    market_calendar.set_extra_closures({})


def ts(s: str) -> datetime:
    """'2026-10-26T13:00' (UTC) -> aware datetime."""
    return datetime.fromisoformat(s).replace(tzinfo=UTC)
