# File map

Where everything lives. Root: the repository root (the folder you cloned).
"Tracked" means in git; runtime folders are git-ignored and exist only on this machine.

## Project root

| Path | What it is |
|---|---|
| `.gitignore` | Ignores the third-party skills (`.claude/skills/*`, with a `!` exception per project skill), `paper-advisor/data/`, `backups/`, `reports/`, `exports/`, `.venv/`, caches, `.playwright-mcp/`, the Claude Code hook cache `.claude/tsc-cache/` and `Local/` |
| `README.md` | The front door: what this is, the safety lines, the install steps for people and for the installing AI, credits |
| `LICENSE` | MIT |
| `scripts/install_trading_skills.py` | Downloads the 74 third-party skills at the audited commit into `.claude/skills/` and checks the 9 adopted ones |
| `.claude/commands/paper-morning.md` | `/paper-morning`: the morning procedure Claude follows (part of the build hash) |
| `.claude/commands/paper-status.md` | `/paper-status`: read-only snapshot (part of the build hash) |
| `.claude/commands/paper-review.md` | `/paper-review`: evaluation and hypotheses (part of the build hash) |
| `.claude/skills/<74 third-party skills>/` | Layer 1, installed by `scripts/install_trading_skills.py`, git-ignored, byte-identical to upstream `cb0f3aa`; never edit |
| `.claude/skills/operating-paper-advisor/` | This skill (layer 2), tracked |
| `Local/` | Planning documents and mockups; git-ignored, never committed |
| `.playwright-mcp/` | Browser-automation captures; git-ignored |
| `paper-advisor/` | The app |

## `paper-advisor/` (the app)

| Path | What it is |
|---|---|
| `bin/pa` | The CLI wrapper (bash). Uses `.venv/bin/python` if present, else `python3`; runs `pa.py` |
| `pa.py` | Launcher: puts the app folder on `sys.path` and calls `paper_advisor.cli.main` |
| `README.md` | The owner's manual: execution policy, defaults, data sources, accounting, dashboard, safety, limitations |
| `SKILL_AUDIT.md` | The audit of all 74 layer-1 skills: provenance, method, capability map (section 4), findings (5), isolation (7) |
| `CHANGELOG.md` | One entry per change, newest at the end ([testing-and-release.md](./testing-and-release.md), section 4) |
| `config/skills_manifest.json` | Upstream commit plus the audited tree hash of the 9 adopted skills |
| `pytest.ini`, `requirements-dev.txt`, `requirements-skills.txt` | Test config; pinned pytest; pinned skill-subprocess dependencies |
| `tests/` | The suite (`conftest.py` gives every test a temp app home); `tests/fixtures/recorded/` holds real recorded responses; `tests/test_tour.py` is the guided tour's tripwire |
| `.venv/` | Python 3.13 venv (git-ignored) |

## `paper-advisor/paper_advisor/` (the package)

| Module | Responsibility |
|---|---|
| `cli.py` | Every `bin/pa` subcommand and flag ([operating-runbook.md](./operating-runbook.md), section 4) |
| `morning.py` | `start`, `submit`, `finish`, `abort`, `heartbeat`, plus `assert_clock_allowed` |
| `runs.py` | Run rows, run events and the experiment lock (`acquire_lock_tx`, `check_lock`, `release_lock`) |
| `briefing.py` | Builds and writes `data/runs/<id>/briefing.json` (what the model service sees) |
| `decisions.py` | The strict decision schema (`DECISION_SCHEMA_DOC`, `validate`, `SchemaError`) |
| `submission.py` | The deterministic risk gate (`apply_decisions_tx`, every check name) |
| `orders.py`, `rules.py`, `quality.py` | The order lifecycle and reservations; exit rules; per-symbol data quality |
| `broker_sim.py` | Settlement: corporate actions, next-open fills, expiry, exit rules, snapshots, benchmark re-queue |
| `ledger.py`, `accounting.py` | The append-only ledger; replay into FIFO lots, cash and P&L |
| `valuation.py`, `metrics.py`, `reconcile.py` | Per-session snapshots with provisional reasons; performance metrics and thresholds; reconciliation checks |
| `experiments.py`, `config.py` | Experiments (create, activate, capital floor, run guard); versioned prospective config, editable keys, tradability math |
| `review.py` | `/paper-review` start, submit and adopt |
| `reports.py`, `status.py`, `exporter.py` | Markdown reports; the read-only status; CSV, JSON and zip export |
| `fixture_demo.py` | The isolated synthetic demo (Thanksgiving 2025 scenario, `DEMO.*` tickers) |
| `skills_bridge.py`, `skill_runners/ibd_runner.py`, `skill_runners/sizer_runner.py` | Layer 1 to layer 2: registry, integrity, isolated adapters, sanitization, freshness |
| `ingest.py`, `prices.py`, `corporate_actions.py`, `universe.py`, `analytics.py` | Refresh and storage of bars, splits and dividends, identity checks, the 24-symbol universe, indicators |
| `data_sources/http.py`, `yahoo.py`, `nasdaq.py`, `news.py` | The only network code (GET-only allowlist) and the source adapters |
| `market_calendar.py`, `clock.py`, `money.py` | The NYSE calendar and targeting; the single time source; integer money and rounding |
| `db.py`, `migrations/0001_initial.sql`, `paths.py`, `provenance.py` | SQLite access, the open/close lock, backups; the schema and triggers; the folder layout; code identity |
| `dashboard/server.py`, `pages.py`, `charts.py` | The localhost server (security, routes, actions); server-rendered HTML for the seven pages and the guided tour's `data-tour` anchors; SVG charts |
| `dashboard/static/app.js`, `app.css`, `tour.js` | The only files the exact-name static whitelist serves: the dashboard script (with the tour's read-only bridge `window.paperAdvisorTour`), the stylesheet, and the 20-step guided tour. All are read from disk on every request |

## Runtime folders (git-ignored; created by `init`, runs and tools)

| Path | Contents |
|---|---|
| `paper-advisor/data/paper_advisor.sqlite` (+ `-wal`, `-shm`) | **The owner's real forward experiment.** Never write to it outside the documented flows |
| `paper-advisor/data/fixture_demo.sqlite` | The fixture demo database |
| `paper-advisor/data/evidence/<YYYY>/<MM>/<DD>/<sha256>.<ext>.gz` | Every fetched response, content-addressed |
| `paper-advisor/data/runs/<run_id>/` | `briefing.json`, `decisions.json` (written by Claude), `review_briefing.json`, `review.json`, and `skills/<skill>/` outputs. **Shared by the forward and fixture databases** (see gotchas) |
| `paper-advisor/reports/<YYYY>/<MM>/<target date>_<morning\|review>_run<N>.md` | Run reports (the fixture's are in `reports/fixture/`, named `_fixture_`) |
| `paper-advisor/backups/paper_advisor-forward-<stamp>.sqlite` | Backups (the fixture's are in `backups/fixture/`). `pa demo --rebuild` leaves `fixture_demo-replaced-<stamp>.sqlite` here |
| `paper-advisor/exports/export_<forward\|fixture>_<stamp>/` + `.zip` | Exports (the fixture's are in `exports/fixture/`) |
