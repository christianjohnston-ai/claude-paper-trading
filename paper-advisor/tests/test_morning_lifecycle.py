"""Run lifecycle failure modes that could falsify the experiment: duplicate runs, concurrency,
restart recovery, exactly-once submission, stale briefings, fake clocks on the real database,
lock tokens, and persistence after restart."""
import multiprocessing as mp
from datetime import datetime, timedelta, timezone

import pytest

from paper_advisor import clock, db, decisions as dec_schema, experiments, metrics, morning, runs, universe

from helpers import World
from test_risk import FRESH

UTC = timezone.utc
NO_IO = dict(refresher=False, with_news=False, with_skills=False)


def pd(action, symbol=None, **kw):
    """Schema-conformant decision."""
    d = {"action": action, "thesis": f"thesis for {symbol or 'cash'}"}
    if symbol:
        d["symbol"] = symbol
    if action in ("BUY", "SELL", "HOLD"):
        d["counterargument"] = "the market could fall"
    if action == "BUY":
        d.update({"notional_usd": "100.00", "horizon_sessions": 20, "horizon_text": "about a month", "confidence": "0.55",
                  "uncertainty": "earnings and macro", "key_risks": "drawdown",
                  "exit_rules": {"stop_loss_pct": "0.08", "take_profit_pct": None, "max_holding_sessions": 30,
                                 "invalidation": "close below the 50-day average"}})
    d.update(kw)
    return d


def payload(run_id, decisions, evidence=None):
    return {"schema_version": 1, "run_id": run_id, "model": {"provider": "test", "model_id": "test-model", "effort": "unknown"},
            "market_view": "test market view", "what_changed": "", "evidence": evidence or [], "candidates": [],
            "decisions": decisions}


def world(fixture_db):
    w = World(fixture_db, start="2026-10-23T23:00:00")
    w.bars_all("2026-10-23", FRESH)
    w.cover_corporate_actions("2026-12-31")
    w.at("2026-10-24T05:00:00")
    return w


def full_run(conn, app_paths, decisions, **kw):
    s = morning.start(conn, app_paths, model_id="test-model", **NO_IO, **kw)
    if s["mode"] != "analysis":
        return s, None
    sub = morning.submit(conn, app_paths, run_id=s["run_id"], token=s["lock_token"], payload=payload(s["run_id"], decisions))
    morning.finish(conn, app_paths, run_id=s["run_id"], token=s["lock_token"], linter=False)
    return s, sub


def counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {"decisions": q("SELECT COUNT(*) FROM decisions"), "advisor_orders": q("SELECT COUNT(*) FROM orders WHERE origin='ADVISOR'"),
            "fills": q("SELECT COUNT(*) FROM fills"), "snapshots": q("SELECT COUNT(*) FROM valuation_snapshots"),
            "funding": q("SELECT COUNT(*) FROM ledger_events WHERE event_type='INITIAL_FUNDING'")}


def test_same_session_rerun_is_reconcile_only(fixture_db, app_paths):
    w = world(fixture_db)
    s1, sub = full_run(fixture_db, app_paths, [pd("BUY", "DEMO.A")])
    assert s1["mode"] == "analysis" and sub["outcomes"][0]["status"] == "ACCEPTED"
    before = counts(fixture_db)
    w.at("2026-10-24T18:00:00")  # later the same weekend: same target session (Monday)
    s2 = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    assert s2["mode"] == "reconcile_only" and s2["version"] == 2 and "report_path" in s2
    with pytest.raises(morning.MorningError):
        morning.submit(fixture_db, app_paths, run_id=s2["run_id"], token=s2["lock_token"], payload=payload(s2["run_id"], [pd("NO_TRADE")]))
    assert counts(fixture_db) == before
    assert runs.current_lock(fixture_db, w.exp_id) is None  # reconcile-only run released the lock


def test_new_analysis_is_versioned_linked_and_shares_the_order_limit(fixture_db, app_paths):
    w = world(fixture_db)
    s1, _ = full_run(fixture_db, app_paths, [pd("BUY", "DEMO.A"), pd("BUY", "DEMO.B")])
    s2, sub2 = full_run(fixture_db, app_paths, [pd("BUY", "DEMO.C"), pd("BUY", "DEMO.D")], new_analysis=True)
    assert s2["version"] == 2 and s2["supersedes_run_id"] == s1["run_id"] and s2["mode"] == "analysis"
    st = [o["status"] for o in sub2["outcomes"]]
    assert st == ["ACCEPTED", "REJECTED"]  # 3 advisor orders max for the Monday open across both versions
    assert "max_new_orders_per_session" in sub2["outcomes"][1]["reasons"]


def test_second_concurrent_start_is_refused(fixture_db, app_paths):
    world(fixture_db)
    s = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    with pytest.raises(runs.LockBusy):
        morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    morning.abort(fixture_db, run_id=s["run_id"], token=s["lock_token"], reason="test")


def _proc_start(db_path, app_base, q):
    from pathlib import Path
    from paper_advisor import db as _db, morning as _m, runs as _r
    from paper_advisor.paths import AppPaths
    conn = _db.open_existing(db_path)
    try:
        s = _m.start(conn, AppPaths(Path(app_base)), model_id="proc", refresher=False, with_news=False, with_skills=False)
        q.put(("ok", s["run_id"]))
    except _r.LockBusy:
        q.put(("busy", None))
    except Exception as exc:  # noqa: BLE001
        q.put(("error", repr(exc)))


def test_concurrent_processes_get_exactly_one_lock(forward_db, app_paths):
    """Two OS processes race to start the REAL-mode experiment (real clock, no injected time)."""
    universe.install(forward_db, [i for i in universe.FORWARD_UNIVERSE if i.symbol == "SPY"], now=clock.now_iso())
    experiments.ensure_default_experiment(forward_db, clock.now_utc())
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_proc_start, args=(str(app_paths.db_path), str(app_paths.base), q)) for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
    results = sorted(q.get(timeout=5)[0] for _ in ps)
    assert results == ["busy", "ok"], results


def test_restart_recovery_before_and_after_submission(fixture_db, app_paths):
    w = world(fixture_db)
    s1 = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)  # crash: never submitted
    w.at("2026-10-24T08:10:00")  # > 180-minute lock TTL later
    s2 = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    assert runs.get(fixture_db, s1["run_id"])["status"] == "interrupted"
    assert s2["mode"] == "analysis" and s2["recovered_runs"] == [{"run_id": s1["run_id"], "had_submission": False}]
    morning.submit(fixture_db, app_paths, run_id=s2["run_id"], token=s2["lock_token"], payload=payload(s2["run_id"], [pd("BUY", "DEMO.A")]))
    # crash after submit, before finish
    w.at("2026-10-24T11:20:00")
    s3 = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    assert runs.get(fixture_db, s2["run_id"])["status"] == "interrupted"
    assert s3["mode"] == "reconcile_only"  # the interrupted run's decisions stand; nothing is redone
    assert counts(fixture_db)["decisions"] == 1


def test_submission_is_validated_and_exactly_once(fixture_db, app_paths):
    world(fixture_db)
    s = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    bad = payload(s["run_id"], [{"action": "BUY", "symbol": "DEMO.A", "notional_usd": "50"}])
    with pytest.raises(dec_schema.SchemaError) as e:
        morning.submit(fixture_db, app_paths, run_id=s["run_id"], token=s["lock_token"], payload=bad)
    assert any("thesis" in x for x in e.value.errors) and any("exit_rules" in x for x in e.value.errors)
    assert counts(fixture_db)["decisions"] == 0
    smuggle = payload(s["run_id"], [pd("NO_TRADE")])
    smuggle["chain_of_thought"] = "hidden reasoning"
    with pytest.raises(dec_schema.SchemaError):
        morning.submit(fixture_db, app_paths, run_id=s["run_id"], token=s["lock_token"], payload=smuggle)
    with pytest.raises(runs.LockBusy):
        morning.submit(fixture_db, app_paths, run_id=s["run_id"], token="wrong", payload=payload(s["run_id"], [pd("NO_TRADE")]))
    morning.submit(fixture_db, app_paths, run_id=s["run_id"], token=s["lock_token"], payload=payload(s["run_id"], [pd("NO_TRADE")]))
    with pytest.raises(morning.MorningError):
        morning.submit(fixture_db, app_paths, run_id=s["run_id"], token=s["lock_token"], payload=payload(s["run_id"], [pd("NO_TRADE")]))
    assert counts(fixture_db)["decisions"] == 1


def test_stale_briefing_is_refused(fixture_db, app_paths):
    w = world(fixture_db)
    w.at("2026-10-26T19:30:00")  # Monday 15:30 ET, market open; briefing built on Friday's completed data
    s = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    w.at("2026-10-26T21:05:00")  # within the lock TTL, but Monday has now closed and finalized
    with pytest.raises(morning.MorningError, match="completed after this run's briefing"):
        morning.submit(fixture_db, app_paths, run_id=s["run_id"], token=s["lock_token"], payload=payload(s["run_id"], [pd("NO_TRADE")]))


def test_fake_clock_is_refused_on_the_real_database(forward_db, app_paths):
    experiments.ensure_default_experiment(forward_db, clock.now_utc())
    clock.set_override(lambda: datetime(2026, 10, 26, 13, 0, tzinfo=UTC))
    with pytest.raises(morning.MorningError, match="injected clock"):
        morning.start(forward_db, app_paths, model_id="x", **NO_IO)


def test_state_persists_identically_after_restart(fixture_db, app_paths):
    w = world(fixture_db)
    full_run(fixture_db, app_paths, [pd("BUY", "DEMO.A")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", {k: v * 1.01 for k, v in FRESH.items()})
    w.at("2026-10-27T13:00:00")
    s, _ = full_run(fixture_db, app_paths, [pd("NO_TRADE")])
    before = metrics.compute(fixture_db, w.exp_id)
    fixture_db.close()
    conn2 = db.open_existing(app_paths.fixture_db_path)
    after = metrics.compute(conn2, w.exp_id)
    assert {k: v for k, v in before.items()} == {k: v for k, v in after.items()}
    assert after["sessions_valued"] == 1 and after["invested_cents"] > 0
    conn2.close()


def test_finish_reconciles_again_so_reconciled_covers_the_runs_own_writes(fixture_db, app_paths):
    """Reconciliation ran only at the start of a run, so "Reconciled" always predated the run's own orders."""
    w = world(fixture_db)
    s = morning.start(fixture_db, app_paths, model_id="test-model", **NO_IO)
    morning.submit(fixture_db, app_paths, run_id=s["run_id"], token=s["lock_token"], payload=payload(s["run_id"], [pd("BUY", "DEMO.A")]))
    w.at("2026-10-24T05:02:00")
    morning.finish(fixture_db, app_paths, run_id=s["run_id"], token=s["lock_token"], linter=False)
    sub_at = fixture_db.execute("SELECT submitted_at FROM run_submissions WHERE run_id=?", (s["run_id"],)).fetchone()[0]
    last = fixture_db.execute("SELECT MAX(at) FROM reconciliation_results WHERE run_id=?", (s["run_id"],)).fetchone()[0]
    assert last > sub_at
    assert all(r[0] for r in fixture_db.execute("SELECT passed FROM reconciliation_results WHERE run_id=? AND at=?", (s["run_id"], last)))


def _late_download(w, finished_at):
    """Stands in for ingest.refresh. The real download finishes seconds after start() reads the clock, and the bars it
    stores carry that later retrieval time, so this one moves the clock forward before storing Monday's bars."""
    def refresh(conn, paths, exp_id, now, run_id, transport):
        w.at(finished_at)
        for sym, px in FRESH.items():
            w.bar(sym, "2026-10-26", px, retrieved_at=w.now_iso())
        return {}
    return refresh


def test_fills_settle_when_the_refresh_downloads_their_bars_after_the_run_started(fixture_db, app_paths):
    """A morning run used to crash with "CHECK constraint failed: recorded_at >= bar_retrieved_at": start() read the
    clock once, the refresh then stored Monday's bars stamped a few seconds later, and settlement recorded the fills
    at the earlier start time. Every morning with something to fill failed the same way."""
    w = world(fixture_db)
    full_run(fixture_db, app_paths, [pd("BUY", "DEMO.A")])  # orders for the Mon Oct 26 open, plus the benchmark's
    w.at("2026-10-27T12:15:00")  # Tue 5:15 AM PT; Monday's bars are not stored yet
    s = morning.start(fixture_db, app_paths, model_id="test-model", with_news=False, with_skills=False,
                      refresher=_late_download(w, "2026-10-27T12:15:05"))
    assert s["mode"] == "analysis" and len(s["settled"]["fills"]) == 2
    assert s["reconciliation_failures"] == []
    rows = fixture_db.execute("SELECT bar_retrieved_at, recorded_at FROM fills").fetchall()
    assert len(rows) == 2 and all(r["recorded_at"] >= r["bar_retrieved_at"] for r in rows)


def test_a_reconcile_only_rerun_settles_and_reconciles_late_downloaded_fills(fixture_db, app_paths):
    """The same crash in a reconcile-only rerun. Its finish also has to reconcile with the later time, or the timing
    check flags the fills it just recorded as recorded after "now"."""
    w = world(fixture_db)
    full_run(fixture_db, app_paths, [pd("BUY", "DEMO.A")])  # Sat: orders for the Mon Oct 26 open
    w.at("2026-10-26T21:30:00")  # Mon after the close, Monday's bars not stored: settlement waits for them
    full_run(fixture_db, app_paths, [pd("NO_TRADE")])  # decides the Tue Oct 27 open
    w.at("2026-10-27T12:15:00")
    s = morning.start(fixture_db, app_paths, model_id="test-model", with_news=False, with_skills=False,
                      refresher=_late_download(w, "2026-10-27T12:15:05"))
    assert s["mode"] == "reconcile_only" and len(s["settled"]["fills"]) == 2
    assert s["reconciliation_failures"] == []
    last = fixture_db.execute("SELECT MAX(at) FROM reconciliation_results WHERE run_id=?", (s["run_id"],)).fetchone()[0]
    assert all(r[0] for r in fixture_db.execute("SELECT passed FROM reconciliation_results WHERE run_id=? AND at=?", (s["run_id"], last)))
