"""The /paper-morning lifecycle: start -> (Claude researches and decides) -> submit -> finish.

Guarantees (each covered by tests/test_morning_lifecycle.py):
- One analysis per market session by default. The run key is the session that orders would target
  (the first open after now + buffer). A rerun for a session that already has submitted decisions
  is RECONCILE-ONLY: it refreshes data, settles, reconciles, values and reports, but cannot accept
  decisions. A deliberate new analysis (--new-analysis) gets version N+1 and links to the run it
  supersedes; its orders still count against the per-session order limit.
- Exactly one decision submission per run (run_submissions primary key), validated against a strict
  schema before anything is written, and refused if a market session completed after the briefing
  was built (the advisor would be deciding on stale data).
- A single experiment lock (runs.locks) spans start..finish. A second concurrent `start` fails fast.
  An interrupted run (crash, closed laptop, expired lock) is marked 'interrupted' by the next start;
  anything it committed (e.g. decisions) stays, nothing is silently redone.
- A fake clock is refused on the real (forward) database.
"""
from __future__ import annotations

import json
from datetime import timedelta

from . import broker_sim, briefing, clock, config, db, decisions as dec_schema, experiments, ingest
from . import market_calendar as cal, orders, quality, reconcile, reports, runs, submission, universe


class MorningError(RuntimeError):
    pass


def assert_clock_allowed(conn) -> None:
    if clock.override_active() and db.db_mode(conn) == "forward":
        raise MorningError("refusing to run the REAL experiment with an injected clock; fake clocks are for fixture databases only")


def _news_symbols(conn, exp_id, now, cfg, refresh_rep) -> list[str]:
    from . import accounting
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    syms = set(st.positions) | {o["symbol"] for o in orders.pending(conn, exp_id, account="PORTFOLIO")}
    if refresh_rep:
        syms |= set((refresh_rep.get("earnings") or {}).get("by_symbol", {}).keys())
    from . import analytics
    last = cal.last_completed_session(now, cfg.bar_finalization_minutes)
    movers = []
    if last:
        for s in universe.symbols(conn):
            ind = analytics.indicators(analytics.adjusted_series(conn, s, last.date.isoformat(), limit=30))
            if ind.get("ret_5d") is not None:
                movers.append((abs(ind["ret_5d"]), s))
    syms |= {s for _, s in sorted(movers, reverse=True)[:5]}
    return sorted(s for s in syms if universe.get(conn, s) is not None)


def _default_refresher(conn, paths, exp_id, now, run_id, transport=None):
    if db.db_mode(conn) != "forward":
        return {"data_status": "FIXTURE", "failures": [], "note": "fixture database: market data comes from the fixture dataset"}
    rep = ingest.refresh(conn, paths, exp_id, now, run_id, transport=transport, include_news=False)
    return rep.as_dict()


def _default_skills(conn, paths, exp_id, run_id, now):
    try:
        from . import skills_bridge
    except ImportError:
        return {"note": "skill adapters unavailable"}
    return skills_bridge.run_morning(conn, paths, exp_id, run_id, now)


def start(conn, paths, *, now=None, model_provider="anthropic/claude-code", model_id="unknown", model_effort="unknown",
          new_analysis=False, force_recover=False, refresher=None, skills_runner=None, news_fetcher=None,
          transport=None, with_news=True, with_skills=True) -> dict:
    now = now or clock.now_utc()
    assert_clock_allowed(conn)
    exp_id = experiments.active_experiment_id(conn)
    if exp_id is None:
        raise MorningError("no experiment exists; run `bin/pa init` first")
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    now_iso = clock.iso(now)
    target = cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds))
    session_key = target.date.isoformat()
    recovered = []
    with db.transaction(conn):
        lock = runs.current_lock(conn, exp_id)
        if lock is not None and lock["expires_at"] > now_iso and not force_recover:
            raise runs.LockBusy(f"another run (#{lock['run_id']}) holds the experiment lock until {lock['expires_at']}. "
                                "If that session is truly gone, rerun with --force-recover.")
        for r in conn.execute("SELECT * FROM runs WHERE experiment_id=? AND status IN ('started','awaiting_decisions','decisions_submitted')",
                              (exp_id,)).fetchall():
            had = conn.execute("SELECT 1 FROM run_submissions WHERE run_id=?", (r["id"],)).fetchone() is not None
            runs.set_status(conn, r["id"], "interrupted", now_iso,
                            error=f"interrupted before finish ({'after' if had else 'before'} decisions were submitted); recovered by a later run")
            runs.log(conn, r["id"], "recovered", "marked interrupted by a later run", {"had_submission": had}, now_iso)
            recovered.append({"run_id": r["id"], "had_submission": had})
        token, prev = runs.acquire_lock_tx(conn, exp_id, None, now, cfg.lock_ttl_minutes, force=True)
        prior = conn.execute("SELECT * FROM runs WHERE experiment_id=? AND run_kind='morning' AND analysis_session_date=? ORDER BY version",
                             (exp_id, session_key)).fetchall()
        submitted = [r for r in prior if conn.execute("SELECT 1 FROM run_submissions WHERE run_id=?", (r["id"],)).fetchone()]
        version = (prior[-1]["version"] + 1) if prior else 1
        supersedes = None
        if new_analysis:
            mode = "analysis"
            supersedes = submitted[-1]["id"] if submitted else None
        elif submitted:
            mode = "reconcile_only"
        else:
            mode = "analysis"
        cfg_vid, _ = config.current(conn, exp_id, now_iso)
        strat = experiments.current_strategy(conn, exp_id, now_iso)
        run_id = runs.create_run_row(conn, exp_id, "morning", session_key, version=version, mode=mode, now=now_iso,
                                     supersedes_run_id=supersedes, model_provider=model_provider, model_id=model_id,
                                     model_effort=model_effort, config_version_id=cfg_vid, strategy_version_id=strat["id"])
        runs.set_lock_run(conn, exp_id, token, run_id)
        runs.log(conn, run_id, "started", f"morning run v{version} ({mode}) for session {session_key}",
                 {"recovered": recovered, "previous_lock": prev}, now_iso)
    try:
        backup_info = db.backup(conn, paths.backups_dir, reason=f"pre-morning run {run_id}")
        db.prune_backups(paths.backups_dir, cfg.backup_keep, db.db_mode(conn))
        refresh_rep = (refresher or _default_refresher)(conn, paths, exp_id, now, run_id, transport) if refresher is not False else None
        settle_rep = broker_sim.settle(conn, exp_id, now, run_id=run_id).as_dict()
        with db.transaction(conn):
            recon = reconcile.run_checks(conn, exp_id, run_id, now)
        news = {}
        if with_news and db.db_mode(conn) == "forward":
            syms = _news_symbols(conn, exp_id, now, cfg, refresh_rep)
            if news_fetcher is not None:
                news = news_fetcher(conn, paths, exp_id, run_id, syms)
            else:
                from .data_sources import http
                news, fails = ingest.fetch_news(conn, http.Fetcher(conn, paths.evidence_dir, transport=transport, run_id=run_id),
                                                exp_id, run_id, syms)
                if refresh_rep is not None:
                    refresh_rep.setdefault("failures", []).extend(fails)
        skill_out = (skills_runner or _default_skills)(conn, paths, exp_id, run_id, now) if with_skills else {"note": "skills disabled"}
        run = runs.get(conn, run_id)
        data = briefing.build(conn, exp_id, run, now, refresh_rep, settle_rep, recon, skill_out, news)
        data["recovered_runs"] = recovered
        data["backup"] = {"path": backup_info["path"], "integrity_ok": backup_info["integrity_ok"]}
        bpath = briefing.write(paths, run_id, data)
        with db.transaction(conn):
            runs.set_status(conn, run_id, "awaiting_decisions" if mode == "analysis" else "started", now_iso,
                            briefing_path=bpath, market_state=data["market"]["state"], data_status=data["data"]["status"])
            runs.log(conn, run_id, "briefing", "briefing written", {"path": bpath, "data_status": data["data"]["status"]}, now_iso)
        result = {"run_id": run_id, "version": version, "mode": mode, "analysis_session": session_key, "lock_token": token,
                  "briefing_path": bpath, "data_status": data["data"]["status"], "market_state": data["market"]["state"],
                  "orders_will_target_session": data["market"]["orders_will_target_session"],
                  "settled": {k: settle_rep[k] for k in ("sessions_processed", "fills", "expired", "rule_orders", "corporate_actions",
                                                         "blocked_session", "blocked_reason")},
                  "reconciliation_failures": [r["check"] for r in recon if not r["passed"]], "recovered_runs": recovered,
                  "supersedes_run_id": supersedes}
        if mode == "reconcile_only":
            fin = _finish(conn, paths, exp_id, run_id, token, now, cfg)
            result["report_path"] = fin["report_path"]
            result["note"] = ("An analysis for this session already exists (run "
                              f"{submitted[-1]['id']}). This rerun only reconciled and reported. Use --new-analysis for a "
                              "deliberate, versioned re-analysis.")
        return result
    except BaseException as exc:
        with db.transaction(conn):
            runs.set_status(conn, run_id, "failed", clock.now_iso(), error=f"{type(exc).__name__}: {exc}")
            runs.log(conn, run_id, "failed", str(exc))
            runs.release_lock(conn, exp_id, token)
        raise


def submit(conn, paths, *, run_id: int, token: str, payload: dict, now=None) -> dict:
    now = now or clock.now_utc()
    assert_clock_allowed(conn)
    run = runs.get(conn, run_id)
    if run is None:
        raise MorningError(f"run {run_id} not found")
    if run["mode"] != "analysis":
        raise MorningError(f"run {run_id} is {run['mode']}; it cannot accept decisions")
    if run["status"] != "awaiting_decisions":
        raise MorningError(f"run {run_id} is '{run['status']}', not awaiting decisions (each run accepts exactly one submission)")
    exp_id = run["experiment_id"]
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    errors = dec_schema.validate(payload, run_id)
    if errors:
        raise dec_schema.SchemaError(errors)
    now_iso = clock.iso(now)
    with db.transaction(conn):
        runs.check_lock(conn, exp_id, token, now, cfg.lock_ttl_minutes, run_id)
        a = cal.last_completed_session(clock.parse_iso(run["started_at"]), cfg.bar_finalization_minutes)
        b = cal.last_completed_session(now, cfg.bar_finalization_minutes)
        if (a.date if a else None) != (b.date if b else None):
            raise MorningError(f"a market session ({b.date}) completed after this run's briefing was built; start a new run so "
                               "decisions are made on current data")
        target_now = cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds)).date.isoformat()
        model = payload["model"]
        conn.execute("INSERT INTO run_submissions(run_id, submitted_at, payload_json, payload_sha256, market_view, what_changed, model_id, model_effort)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (run_id, now_iso, json.dumps(payload, sort_keys=True), _sha(payload), payload["market_view"],
                      payload.get("what_changed") or "", model["model_id"], model["effort"]))
        ref_ids = {}
        for e in payload.get("evidence") or []:
            cur = conn.execute(
                "INSERT INTO evidence_items(experiment_id, run_id, kind, symbol, title, url, publisher, published_at, retrieved_at, summary,"
                " quality_flags, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'advisor', ?)",
                (exp_id, run_id, e["kind"], (e.get("symbol") or None), e["title"], e.get("url"), e.get("publisher"),
                 e.get("published_at"), e.get("retrieved_at"), e["summary"],
                 json.dumps(["advisor_supplied"] + ([] if e.get("published_at") else ["no_publication_time"])), now_iso))
            ref_ids[e["ref"]] = int(cur.lastrowid)
        valid_ids = {r[0] for r in conn.execute("SELECT id FROM evidence_items WHERE experiment_id=?", (exp_id,))}

        def resolve(refs):
            out = []
            for r in refs or []:
                if isinstance(r, str):
                    out.append(ref_ids[r])
                elif r in valid_ids:
                    out.append(int(r))
                else:
                    raise dec_schema.SchemaError([f"evidence id {r} does not exist in this experiment"])
            return out
        for c in payload.get("candidates") or []:
            conn.execute("INSERT OR REPLACE INTO candidate_assessments(run_id, symbol, verdict, assessment, reason, evidence_ids_json, created_at)"
                         " VALUES (?,?,?,?,?,?,?)", (run_id, c["symbol"].upper(), c["verdict"], c["assessment"], c["reason"],
                                                     json.dumps(resolve(c.get("evidence_refs"))), now_iso))
        decs = []
        for d in payload["decisions"]:
            dd = dict(d)
            dd["evidence_ids"] = resolve(d.get("evidence_refs"))
            dd.pop("evidence_refs", None)
            decs.append(dd)
        outcomes = submission.apply_decisions_tx(conn, exp_id, run_id, decs, now)
        if model["model_id"] != run["model_id"] or model["effort"] != run["model_effort"]:
            runs.log(conn, run_id, "model_mismatch", "model identity in the payload differs from the one recorded at start",
                     {"start": [run["model_id"], run["model_effort"]], "payload": [model["model_id"], model["effort"]]}, now_iso)
        runs.set_status(conn, run_id, "decisions_submitted", now_iso)
        runs.log(conn, run_id, "submitted", f"{len(outcomes)} decisions", {"target_session": target_now}, now_iso)
    return {"run_id": run_id, "target_session": target_now,
            "outcomes": [{"seq": o.seq, "action": o.action, "symbol": o.symbol, "status": o.status, "reasons": o.reasons,
                          "order_id": o.order_id, "target_session": o.target_session} for o in outcomes]}


def _sha(payload) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _finish(conn, paths, exp_id, run_id, token, now, cfg, linter=None) -> dict:
    now_iso = clock.iso(now)
    settle_rep = broker_sim.settle(conn, exp_id, now, run_id=run_id)
    # Reconcile again after this run's own writes (decisions, orders, reservations): the start-of-run check alone
    # left "Reconciled" describing the state BEFORE the run changed it. Failures are recorded and reported, like at start.
    with db.transaction(conn):
        reconcile.run_checks(conn, exp_id, run_id, now)
    run = runs.get(conn, run_id)
    notes = []
    if settle_rep.blocked_reason:
        notes.append(f"Session {settle_rep.blocked_session} could not be settled yet: {settle_rep.blocked_reason}")
    text = reports.morning_report(conn, exp_id, run, now, notes)
    if linter is False:
        lint = None
    elif linter is not None:
        lint = linter(text, run["analysis_session_date"])
    else:
        from . import skills_bridge
        lint = skills_bridge.lint_report(paths, conn, run_id, text, now)
    if lint and lint.get("findings"):
        text += "\n## Report lint (data-quality-checker skill, warnings only)\n\n" + "\n".join(f"- {f}" for f in lint["findings"]) + "\n"
    with db.transaction(conn):
        path = reports.save(conn, paths, run_id, "morning", text, run["analysis_session_date"])
        runs.set_status(conn, run_id, "completed", now_iso, report_path=path)
        runs.log(conn, run_id, "completed", "report written", {"path": path}, now_iso)
        runs.release_lock(conn, exp_id, token)
    return {"run_id": run_id, "report_path": path}


def finish(conn, paths, *, run_id: int, token: str, now=None, linter=None) -> dict:
    now = now or clock.now_utc()
    assert_clock_allowed(conn)
    run = runs.get(conn, run_id)
    if run is None or run["status"] != "decisions_submitted":
        raise MorningError(f"run {run_id} must be 'decisions_submitted' to finish (submit a NO_TRADE decision if you choose not to act)")
    exp_id = run["experiment_id"]
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    with db.transaction(conn):
        runs.check_lock(conn, exp_id, token, now, cfg.lock_ttl_minutes, run_id)
    return _finish(conn, paths, exp_id, run_id, token, now, cfg, linter)


def abort(conn, *, run_id: int, token: str, reason: str, now=None) -> dict:
    now = now or clock.now_utc()
    run = runs.get(conn, run_id)
    if run is None:
        raise MorningError(f"run {run_id} not found")
    exp_id = run["experiment_id"]
    with db.transaction(conn):
        lock = runs.current_lock(conn, exp_id)
        if lock is None or lock["token"] != token:
            raise runs.LockBusy("token does not hold the lock")
        runs.set_status(conn, run_id, "failed", clock.iso(now), error=f"aborted: {reason}")
        runs.log(conn, run_id, "aborted", reason)
        runs.release_lock(conn, exp_id, token)
    return {"run_id": run_id, "status": "failed"}


def heartbeat(conn, *, run_id: int, token: str, now=None) -> None:
    now = now or clock.now_utc()
    run = runs.get(conn, run_id)
    _, cfg = config.current(conn, run["experiment_id"], clock.iso(now))
    with db.transaction(conn):
        runs.check_lock(conn, run["experiment_id"], token, now, cfg.lock_ttl_minutes, run_id)
