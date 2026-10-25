"""Command-line interface: `paper-advisor/bin/pa <command>` (or `python paper-advisor/pa.py ...`).

Machine-facing steps (morning start/submit/finish, review) print JSON so the Claude session can read
them reliably; human-facing commands print text. Every command resolves the database from the app
directory, never from the current working directory.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, clock, config, db, experiments, market_calendar as cal, morning, review, runs, status, universe
from . import money as m
from .paths import DEFAULT_PATHS, AppPaths, PROJECT_ROOT

DEPENDENCIES = [
    {"name": "tradermonty/claude-trading-skills", "kind": "skill_library", "source_url": "https://github.com/tradermonty/claude-trading-skills",
     "commit_sha": "cb0f3aadffed6d1089db4b224162c964368baa46", "version": "2026-10-19 HEAD",
     "verified_how": "all 1141 installed files in .claude/skills byte-identical (git blob hash) to upstream HEAD on 2026-10-24; "
                     "adopted skills additionally tree-hashed in config/skills_manifest.json",
     "local_path": ".claude/skills", "notes": "9 skills adopted via isolated adapters; others guidance-only or disabled (SKILL_AUDIT.md)"},
    {"name": "Jakeschincariol/trader-skill", "kind": "reference_only", "source_url": "https://github.com/Jakeschincariol/trader-skill",
     "commit_sha": "5189e79b9a480755a3733ac9713f5353981dbe18", "version": "v1.0 (single commit 2026-10-14)",
     "verified_how": "cloned and audited read-only on 2026-10-24; NOT installed and NOT adopted (trader-broker connects to real brokers)",
     "local_path": None, "notes": "audit findings recorded in SKILL_AUDIT.md"},
    {"name": "Yahoo Finance chart API v8", "kind": "data_source", "source_url": "https://query1.finance.yahoo.com/v8/finance/chart/",
     "commit_sha": None, "version": "unofficial, keyless", "verified_how": "probed 2026-10-24; split back-adjustment verified on NVDA 2024-06",
     "local_path": None, "notes": "daily bars, dividends, splits, instrument metadata"},
    {"name": "Nasdaq Trader symbol directories", "kind": "data_source", "source_url": "https://www.nasdaqtrader.com/dynamic/SymDir/",
     "commit_sha": None, "version": "official files", "verified_how": "probed 2026-10-24 (file creation 10/23/2026 21:31)", "local_path": None,
     "notes": "instrument identity verification"},
    {"name": "Nasdaq earnings calendar", "kind": "data_source", "source_url": "https://api.nasdaq.com/api/calendar/earnings",
     "commit_sha": None, "version": "keyless JSON", "verified_how": "probed 2026-10-24", "local_path": None, "notes": "dates may be estimated"},
    {"name": "Yahoo Finance headline RSS", "kind": "data_source", "source_url": "https://feeds.finance.yahoo.com/rss/2.0/headline",
     "commit_sha": None, "version": "keyless RSS", "verified_how": "probed 2026-10-24", "local_path": None, "notes": "headlines are unverified leads"},
    {"name": "NYSE holidays and trading hours", "kind": "calendar_source", "source_url": "https://www.nyse.com/markets/hours-calendars",
     "commit_sha": None, "version": "2025-2027 table", "verified_how": "transcribed 2026-10-24; rule engine reproduces it (tests)",
     "local_path": None, "notes": "other years rule-derived and flagged unverified"},
]


def _paths(args) -> AppPaths:
    return AppPaths(Path(args.app_dir)) if getattr(args, "app_dir", None) else DEFAULT_PATHS


def _db_path(args, paths) -> Path:
    return paths.fixture_db_path if getattr(args, "fixture", False) else paths.db_path


def _open(args, paths, readonly=False):
    p = _db_path(args, paths)
    conn = db.open_existing(p, readonly=readonly)
    _load_calendar_overrides(conn)
    if getattr(args, "fixture", False) and os.environ.get("PAPER_ADVISOR_FAKE_NOW"):
        if db.db_mode(conn) != "fixture":
            raise SystemExit("PAPER_ADVISOR_FAKE_NOW is only honored for the fixture database")
        fixed = clock.parse_iso(os.environ["PAPER_ADVISOR_FAKE_NOW"])
        clock.set_override(lambda: fixed)
    return conn


def _load_calendar_overrides(conn) -> None:
    try:
        rows = conn.execute("SELECT session_date, reason FROM calendar_overrides").fetchall()
    except Exception:  # noqa: BLE001 - table missing on a brand-new DB before migration
        return
    cal.set_extra_closures({datetime.fromisoformat(r["session_date"]).date(): r["reason"] for r in rows})


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def cmd_init(args) -> int:
    paths = _paths(args)
    paths.ensure_dirs()
    conn = db.init_db(paths.db_path, db_mode="forward")
    now = clock.now_utc()
    universe.install(conn, universe.FORWARD_UNIVERSE, now=clock.iso(now))
    exp_id, created = experiments.ensure_default_experiment(conn, now)
    with db.transaction(conn):
        for dep in DEPENDENCIES:
            conn.execute("INSERT OR IGNORE INTO dependencies(name, kind, source_url, commit_sha, version, verified_how, local_path, recorded_at, notes)"
                         " VALUES (?,?,?,?,?,?,?,?,?)", (dep["name"], dep["kind"], dep["source_url"], dep["commit_sha"], dep["version"],
                                                         dep["verified_how"], dep["local_path"], clock.iso(now), dep["notes"]))
    exp = experiments.get(conn, exp_id)
    _print({"database": str(paths.db_path), "experiment_id": exp_id, "created_now": created, "name": exp["name"],
            "starting_capital": m.fmt_usd(exp["starting_capital_cents"]), "inception_at": exp["inception_at"],
            "first_investable_session": exp["benchmark_start_session"], "universe": universe.symbols(conn),
            "note": "Existing experiment reused; nothing was re-funded." if not created else "New $500 paper experiment funded exactly once."})
    return 0


def cmd_morning(args) -> int:
    paths = _paths(args)
    conn = _open(args, paths)
    try:
        if args.step == "start":
            res = morning.start(conn, paths, model_provider=args.provider, model_id=args.model_id, model_effort=args.effort,
                                new_analysis=args.new_analysis, force_recover=args.force_recover, with_news=not args.no_news,
                                with_skills=not args.no_skills)
        elif args.step == "submit":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            res = morning.submit(conn, paths, run_id=args.run, token=args.token, payload=payload)
        elif args.step == "finish":
            res = morning.finish(conn, paths, run_id=args.run, token=args.token)
        elif args.step == "abort":
            res = morning.abort(conn, run_id=args.run, token=args.token, reason=args.reason or "aborted by operator")
        elif args.step == "heartbeat":
            morning.heartbeat(conn, run_id=args.run, token=args.token)
            res = {"run_id": args.run, "heartbeat": "ok"}
        else:
            raise SystemExit(f"unknown step {args.step}")
    except (morning.MorningError, runs.LockBusy) as exc:
        _print({"error": type(exc).__name__, "message": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001
        from .decisions import SchemaError
        if isinstance(exc, SchemaError):
            _print({"error": "SchemaError", "errors": exc.errors, "note": "nothing was persisted; fix and resubmit"})
            return 3
        raise
    _print(res)
    return 0


def cmd_evidence(args) -> int:
    """Fetch extra headlines during research and attach them to the open run (needs its lock token)."""
    from . import ingest
    from .data_sources import http
    paths = _paths(args)
    conn = _open(args, paths)
    morning.heartbeat(conn, run_id=args.run, token=args.token)
    run = runs.get(conn, args.run)
    news, fails = ingest.fetch_news(conn, http.Fetcher(conn, paths.evidence_dir, run_id=args.run), run["experiment_id"], args.run,
                                    [s.upper() for s in args.symbols])
    _print({"news": news, "failures": fails})
    return 0


def cmd_status(args) -> int:
    paths = _paths(args)
    p = _db_path(args, paths)
    if not p.exists():
        print("paper-advisor is not initialized. Run: paper-advisor/bin/pa init")
        return 1
    conn = _open(args, paths, readonly=True)
    g = status.gather(conn)
    if args.json:
        _print(g)
    else:
        print(status.render(g))
    return 0


def cmd_review(args) -> int:
    paths = _paths(args)
    conn = _open(args, paths)
    if args.step == "start":
        _print(review.start(conn, paths, model_id=args.model_id, model_effort=args.effort, model_provider=args.provider))
    elif args.step == "submit":
        try:
            _print(review.submit(conn, paths, run_id=args.run, payload=json.loads(Path(args.file).read_text(encoding="utf-8"))))
        except ValueError as exc:
            _print({"error": "ValueError", "message": str(exc), "note": "nothing was persisted"})
            return 3
    elif args.step == "adopt":
        _print(review.adopt(conn, hypothesis_id=args.hypothesis, reason=args.reason))
    return 0


def cmd_settle(args) -> int:
    """Settle + reconcile without decisions (also run by every morning start)."""
    from . import broker_sim, reconcile
    paths = _paths(args)
    conn = _open(args, paths)
    exp_id = experiments.active_experiment_id(conn)
    morning.assert_clock_allowed(conn)
    _, cfg = config.current(conn, exp_id)
    with db.transaction(conn):
        lock = runs.current_lock(conn, exp_id)
        if lock is not None and lock["expires_at"] > clock.now_iso():
            _print({"error": "LockBusy", "message": f"run {lock['run_id']} holds the lock; settle runs inside it"})
            return 2
        token, _ = runs.acquire_lock_tx(conn, exp_id, None, clock.now_utc(), 10, force=True)
    try:
        rep = broker_sim.settle(conn, exp_id, clock.now_utc())
        with db.transaction(conn):
            rec = reconcile.run_checks(conn, exp_id)
    finally:
        with db.transaction(conn):
            runs.release_lock(conn, exp_id, token)
    _print({"settlement": rep.as_dict(), "reconciliation_failures": [r for r in rec if not r["passed"]]})
    return 0


def cmd_reconcile(args) -> int:
    from . import reconcile
    paths = _paths(args)
    conn = _open(args, paths)
    exp_id = experiments.active_experiment_id(conn)
    with db.transaction(conn):
        rec = reconcile.run_checks(conn, exp_id)
    _print({"passed": all(r["passed"] for r in rec), "checks": rec})
    return 0 if all(r["passed"] for r in rec) else 4


def cmd_backup(args) -> int:
    paths = _paths(args)
    conn = _open(args, paths)
    _print(db.backup(conn, paths.backups_dir, reason=args.reason or "manual backup"))
    return 0


def cmd_export(args) -> int:
    from . import exporter
    paths = _paths(args)
    conn = _open(args, paths, readonly=True)
    _print(exporter.export_all(conn, paths, experiments.active_experiment_id(conn)))
    return 0


def cmd_experiment(args) -> int:
    paths = _paths(args)
    conn = _open(args, paths)
    if args.step == "list":
        _print([dict(r) for r in experiments.all_experiments(conn)])
    elif args.step == "new":
        try:
            cents = m.parse_usd(args.capital)
        except ValueError as exc:
            raise SystemExit(f"starting capital must be a dollar amount with at most 2 decimals, e.g. 1000.00 ({exc})") from exc
        cur = experiments.active_experiment_id(conn)
        params = None
        if cur is not None and not args.default_config:
            import json as _j
            params = _j.loads(config.current_version(conn, cur)["params_json"])
        # The minimum (at least $10, and enough for one buy under the limits) is enforced by experiments.
        try:
            exp_id = experiments.create_experiment(conn, name=args.name or f"Forward paper experiment ({m.fmt_usd(cents)})",
                                                   starting_capital_cents=cents, now=clock.now_utc(), created_by="cli",
                                                   config_params=params, notes=args.notes)
        except (experiments.CapitalTooSmall, experiments.RunInProgress) as exc:
            raise SystemExit(str(exc)) from exc
        _print({"created_experiment_id": exp_id, "starting_capital": m.fmt_usd(cents), "previous_experiment_archived": cur})
    elif args.step == "activate":
        try:
            experiments.set_active(conn, args.id)
        except experiments.RunInProgress as exc:
            raise SystemExit(str(exc)) from exc
        _print({"active_experiment_id": args.id})
    return 0


def cmd_config(args) -> int:
    paths = _paths(args)
    conn = _open(args, paths)
    exp_id = experiments.active_experiment_id(conn)
    if args.step == "show":
        row = config.current_version(conn, exp_id)
        _print({"version": row["version"], "effective_from": row["effective_from"], "params": json.loads(row["params_json"])})
    else:
        overrides = dict(kv.split("=", 1) for kv in args.pairs)
        with db.transaction(conn):
            vid = config.create_version(conn, exp_id, overrides, created_by="cli", reason=args.reason, now=clock.now_iso())
        _print({"new_config_version_id": vid, "effective_from": clock.now_iso(), "overrides": overrides,
                "note": "prospective only: earlier decisions keep the limits they were validated under"})
    return 0


def cmd_calendar(args) -> int:
    paths = _paths(args)
    conn = _open(args, paths)
    if args.step == "add-closure":
        with db.transaction(conn):
            conn.execute("INSERT INTO calendar_overrides(session_date, kind, reason, created_at, created_by) VALUES (?, 'CLOSED', ?, ?, 'cli')",
                         (args.date, args.reason, clock.now_iso()))
        _print({"closed": args.date, "reason": args.reason})
    else:
        _print([dict(r) for r in conn.execute("SELECT * FROM calendar_overrides ORDER BY session_date")])
    return 0


def cmd_skills(args) -> int:
    from . import skills_bridge
    rows = skills_bridge.registry_rows()
    for r in rows:
        ok = skills_bridge.integrity(r["skill"])[1] if r["status"].startswith("ADAPTED") else ""
        print(f"{r['status']:<20} {r['skill']:<36} {r['reason']}" + (f"  [{ok}]" if ok else ""))
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import server
    paths = _paths(args)
    return server.serve(paths, fixture=args.fixture, host="127.0.0.1", port=args.port)


def cmd_demo(args) -> int:
    from . import fixture_demo
    paths = _paths(args)
    _print(fixture_demo.run_demo(paths, rebuild=args.rebuild))
    return 0


def cmd_rebuild_check(args) -> int:
    from . import accounting, reconcile
    paths = _paths(args)
    conn = _open(args, paths, readonly=True)
    exp_id = experiments.active_experiment_id(conn)
    out = {}
    for acct in ("PORTFOLIO", "BENCHMARK"):
        st = accounting.load_state(conn, exp_id, acct)
        out[acct] = {"cash": m.fmt_usd(st.cash_cents), "positions": {k: m.qty_to_str(v) for k, v in st.positions.items()},
                     "realized": m.fmt_usd(st.realized_cents), "dividends": m.fmt_usd(st.dividends_cents), "events_replayed": st.last_seq}
    _print({"rebuilt_from_ledger": out, "note": "holdings are always derived from the append-only ledger; run `pa reconcile` to "
                                                "compare against fills, orders and snapshots"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pa", description="paper-advisor: local PAPER-ONLY trading advisor (simulated, fake money)")
    p.add_argument("--version", action="version", version=f"paper-advisor {__version__}")
    p.add_argument("--app-dir", help=argparse.SUPPRESS)
    p.add_argument("--fixture", action="store_true", help="use the isolated fixture demo database")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the database and the default $500 experiment (idempotent)").set_defaults(fn=cmd_init)

    mo = sub.add_parser("morning", help="morning run steps (used by /paper-morning)")
    mo.add_argument("step", choices=["start", "submit", "finish", "abort", "heartbeat"])
    mo.add_argument("--run", type=int)
    mo.add_argument("--token")
    mo.add_argument("--file")
    mo.add_argument("--reason")
    mo.add_argument("--provider", default="anthropic/claude-code")
    mo.add_argument("--model-id", default="unknown")
    mo.add_argument("--effort", default="unknown")
    mo.add_argument("--new-analysis", action="store_true")
    mo.add_argument("--force-recover", action="store_true")
    mo.add_argument("--no-news", action="store_true")
    mo.add_argument("--no-skills", action="store_true")
    mo.set_defaults(fn=cmd_morning)

    ev = sub.add_parser("evidence", help="fetch extra headlines into the open run")
    ev.add_argument("step", choices=["news"])
    ev.add_argument("symbols", nargs="+")
    ev.add_argument("--run", type=int, required=True)
    ev.add_argument("--token", required=True)
    ev.set_defaults(fn=cmd_evidence)

    st = sub.add_parser("status", help="show state (read-only; never trades)")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)

    rv = sub.add_parser("review", help="review steps (used by /paper-review)")
    rv.add_argument("step", choices=["start", "submit", "adopt"])
    rv.add_argument("--run", type=int)
    rv.add_argument("--file")
    rv.add_argument("--hypothesis", type=int)
    rv.add_argument("--reason")
    rv.add_argument("--provider", default="anthropic/claude-code")
    rv.add_argument("--model-id", default="unknown")
    rv.add_argument("--effort", default="unknown")
    rv.set_defaults(fn=cmd_review)

    sub.add_parser("settle", help="settle pending orders/rules with available data (no decisions)").set_defaults(fn=cmd_settle)
    sub.add_parser("reconcile", help="run reconciliation checks").set_defaults(fn=cmd_reconcile)
    bk = sub.add_parser("backup", help="consistent SQLite backup")
    bk.add_argument("--reason")
    bk.set_defaults(fn=cmd_backup)
    sub.add_parser("export", help="CSV/JSON export of the record").set_defaults(fn=cmd_export)
    ex = sub.add_parser("experiment", help="list/new/activate experiments (new capital = new experiment)")
    ex.add_argument("step", choices=["list", "new", "activate"])
    ex.add_argument("--capital")
    ex.add_argument("--name")
    ex.add_argument("--notes")
    ex.add_argument("--id", type=int)
    ex.add_argument("--default-config", action="store_true", help="start the new experiment with default limits")
    ex.set_defaults(fn=cmd_experiment)
    cf = sub.add_parser("config", help="show or change risk settings (new prospective version)")
    cf.add_argument("step", choices=["show", "set"])
    cf.add_argument("pairs", nargs="*")
    cf.add_argument("--reason")
    cf.set_defaults(fn=cmd_config)
    ca = sub.add_parser("calendar", help="record an unscheduled market closure")
    ca.add_argument("step", choices=["list", "add-closure"])
    ca.add_argument("--date")
    ca.add_argument("--reason")
    ca.set_defaults(fn=cmd_calendar)
    sub.add_parser("skills", help="list the audited skills and how each is used").set_defaults(fn=cmd_skills)
    da = sub.add_parser("dashboard", help="serve the local dashboard on 127.0.0.1")
    da.add_argument("--port", type=int, default=8765)
    # README and `pa demo` print "pa dashboard --fixture", so accept it here as well as globally.
    # SUPPRESS: when absent here it must not override a global --fixture given before the subcommand.
    da.add_argument("--fixture", action="store_true", default=argparse.SUPPRESS, help="open the fixture demo database")
    da.set_defaults(fn=cmd_dashboard)
    de = sub.add_parser("demo", help="build and run the isolated fixture demo (synthetic data, separate database)")
    de.add_argument("--rebuild", action="store_true")
    de.set_defaults(fn=cmd_demo)
    sub.add_parser("rebuild-check", help="rebuild holdings from the ledger").set_defaults(fn=cmd_rebuild_check)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "morning" and args.step in ("submit", "finish", "abort", "heartbeat") and (args.run is None or not args.token):
        raise SystemExit("--run and --token are required")
    if args.cmd == "config" and args.step == "set" and not args.reason:
        raise SystemExit("--reason is required for configuration changes")
    return int(args.fn(args) or 0)
