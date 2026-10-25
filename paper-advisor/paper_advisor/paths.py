"""Filesystem layout. Every path is resolved from this file's location, never from the process's
current working directory, so `bin/pa`, the slash commands, the dashboard and subprocesses all
agree on where the experiment database lives no matter where they were launched from.

Default layout (under <project root>/paper-advisor/):
    data/paper_advisor.sqlite      the REAL forward experiment (never synthetic data)
    data/fixture_demo.sqlite       the isolated fixture demo (synthetic data only)
    data/evidence/                 cached raw source responses (gzip), named by sha256
    data/runs/<run_id>/            briefing/decision files exchanged with the Claude session
    reports/                       morning and review reports (Markdown)
    backups/                       consistent SQLite backups
    exports/                       CSV/JSON exports
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_DIR.parent
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"


@dataclass(frozen=True)
class AppPaths:
    base: Path = APP_DIR

    @property
    def data_dir(self) -> Path:
        return self.base / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "paper_advisor.sqlite"

    @property
    def fixture_db_path(self) -> Path:
        return self.data_dir / "fixture_demo.sqlite"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.base / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.base / "backups"

    @property
    def exports_dir(self) -> Path:
        return self.base / "exports"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.evidence_dir, self.runs_dir, self.reports_dir, self.backups_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)


DEFAULT_PATHS = AppPaths()
