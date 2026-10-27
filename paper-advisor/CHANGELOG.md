# Changelog

## 2026-10-25 - Type: Config
- Change: First public release of paper-advisor (1.0.0) as claude-paper-trading. The development history stays
  private; this repository starts here. Added an installer for the audited third-party skills
  (scripts/install_trading_skills.py), a root README with install steps for people and for the installing AI, and an
  MIT license. Documentation paths now say "the project root" instead of one machine's folder.
- Files: ../README.md, ../LICENSE, ../scripts/install_trading_skills.py, README.md, SKILL_AUDIT.md, CHANGELOG.md,
  paper_advisor/skills_bridge.py (docstring), paper_advisor/dashboard/pages.py, static/app.js, static/tour.js,
  static/app.css (comments only), tests/test_dashboard.py, tests/test_tour.py (comments only),
  ../.claude/skills/operating-paper-advisor/
- State impact: None
- Field mutations: None
- Performance: None measured

## 2026-10-27 - Type: State
- Change: morning.start() now settles fills with a clock reading taken after the data refresh, not the time the run
  started. A morning run with an order to fill crashed with "CHECK constraint failed: recorded_at >=
  bar_retrieved_at": the refresh stored the fill session's bars stamped with their real download time, seconds after
  the start time, and settlement recorded the fills at the earlier start time, which the fills table refuses. Every
  account hits it, on the first run after its first market session, because that run fills the SPY
  benchmark's opening buy. Running /paper-morning again got past it, since the bars were then already stored. The
  reconcile after settlement and a reconcile-only rerun's finish use the same reading, because reconcile's timing
  check requires recorded_at <= its time. Two regression tests reproduce the crash (an analysis run and a
  reconcile-only rerun) and failed before the fix.
- Files: paper_advisor/morning.py, tests/test_morning_lifecycle.py
- State impact: None to transitions. Fill and ledger rows recorded at a morning start now carry a time a few seconds
  after the run's start time instead of the start time itself. code_tree_sha256 covers morning.py, so the next run
  records a new build.
- Field mutations: None (fills.recorded_at and ledger_events.recorded_at keep their meaning: when the row was written)
- Performance: None measured (one extra clock read)
