"""/paper-review: evaluate results honestly and record PROSPECTIVE, dated strategy hypotheses.

A review can never change the past. It reads the immutable record, computes outcomes, and stores
hypotheses whose earliest effective session is strictly in the future. Adopting a hypothesis
creates a new config or strategy version effective from the adoption instant; earlier decisions
keep the rules they were made under. There is no code path that edits a past decision, order, fill
or valuation (those tables are append-only at the schema level).

Small-sample honesty: backtest-expert's own gate treats fewer than 30 trades as NOT_EVALUABLE, and
residual-edge-analyzer refuses fewer than 60 daily observations. Both verdicts are shown verbatim;
early in the experiment "insufficient evidence" IS the finding.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from . import clock, config, db, experiments, market_calendar as cal, metrics, prices, reports, runs, skills_bridge
from . import money as m

HYP_KEYS = {"title", "hypothesis", "rationale", "falsification_test", "minimum_sample", "proposed_change", "evidence_refs"}
CHANGE_TYPES = ("none", "config", "strategy")


def decision_outcomes(conn, exp_id: int) -> list[dict]:
    out = []
    last_spy = prices.latest_bar(conn, experiments.get(conn, exp_id)["benchmark_symbol"])
    for d in conn.execute("SELECT * FROM decisions WHERE experiment_id=? ORDER BY id", (exp_id,)).fetchall():
        o = conn.execute("SELECT * FROM orders WHERE decision_id=? AND origin='ADVISOR'", (d["id"],)).fetchone()
        row = {"decision_id": d["id"], "run_id": d["run_id"], "decided_at": d["decided_at"], "action": d["action"],
               "symbol": d["symbol"], "confidence": d["confidence"], "order_status": o["status"] if o else None,
               "order_reason": o["status_reason"] if o else None}
        if o and o["status"] == "FILLED" and o["side"] == "BUY":
            f = conn.execute("SELECT * FROM fills WHERE order_id=?", (o["id"],)).fetchone()
            exit_f = conn.execute("SELECT f.* FROM fills f JOIN orders x ON x.id=f.order_id WHERE x.experiment_id=? AND f.symbol=?"
                                  " AND f.side='SELL' AND f.id>? AND f.account='PORTFOLIO' ORDER BY f.id LIMIT 1",
                                  (exp_id, d["symbol"], f["id"])).fetchone()
            last = prices.latest_bar(conn, d["symbol"])
            end_px = Decimal(exit_f["fill_price"]) if exit_f else (Decimal(last["close"]) if last else None)
            end_date = exit_f["session_date"] if exit_f else (last["session_date"] if last else None)
            spy_start = prices.bar_for(conn, experiments.get(conn, exp_id)["benchmark_symbol"], f["session_date"])
            spy_end = prices.bar_for(conn, experiments.get(conn, exp_id)["benchmark_symbol"], end_date) if end_date else None
            ret = float(end_px / Decimal(f["fill_price"]) - 1) if end_px else None
            spy = float(Decimal(spy_end["close"]) / Decimal(spy_start["open"]) - 1) if spy_start and spy_end else None
            row.update({"entry_session": f["session_date"], "entry_price": f["fill_price"], "outcome_through": end_date,
                        "closed": bool(exit_f), "price_return_net_of_entry_slippage": ret, "spy_price_return_same_window": spy,
                        "excess_vs_spy": None if ret is None or spy is None else ret - spy})
        elif d["action"] == "NO_TRADE" and last_spy is not None:
            tgt = cal.next_session_after(clock.parse_iso(d["decided_at"]))
            b0 = prices.bar_for(conn, experiments.get(conn, exp_id)["benchmark_symbol"], tgt.date.isoformat())
            if b0 is not None:
                row.update({"spy_return_since_decision": float(Decimal(last_spy["close"]) / Decimal(b0["open"]) - 1),
                            "outcome_through": last_spy["session_date"]})
        out.append(row)
    return out


def start(conn, paths, *, now=None, model_id="unknown", model_effort="unknown", model_provider="anthropic/claude-code") -> dict:
    now = now or clock.now_utc()
    exp_id = experiments.active_experiment_id(conn)
    day = now.astimezone(cal.NY).date().isoformat()
    with db.transaction(conn):
        v = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM runs WHERE experiment_id=? AND run_kind='review' AND analysis_session_date=?",
                         (exp_id, day)).fetchone()[0]
        cfg_vid, _ = config.current(conn, exp_id, clock.iso(now))
        run_id = runs.create_run_row(conn, exp_id, "review", day, version=v, mode="analysis", now=clock.iso(now),
                                     model_provider=model_provider, model_id=model_id, model_effort=model_effort,
                                     config_version_id=cfg_vid)
    mt = metrics.compute(conn, exp_id)
    outcomes = decision_outcomes(conn, exp_id)
    exp = experiments.get(conn, exp_id)
    resid = skills_bridge.residual_edge(conn, paths, exp_id, run_id)
    digest = skills_bridge.weekly_digest(conn, paths, exp_id, run_id, exp["inception_at"][:10], day)
    filled = [o for o in outcomes if o.get("price_return_net_of_entry_slippage") is not None]
    conf = {}
    for o in filled:
        if o["confidence"] is None:
            continue
        b = "high (>=0.65)" if Decimal(o["confidence"]) >= Decimal("0.65") else ("medium (0.5-0.65)" if Decimal(o["confidence"]) >= Decimal("0.5") else "low (<0.5)")
        c = conf.setdefault(b, {"n": 0, "beat_spy": 0})
        c["n"] += 1
        c["beat_spy"] += int((o["excess_vs_spy"] or 0) > 0)
    data = {"PAPER_TRADING": True, "run_id": run_id, "generated_at": clock.iso(now),
            "metrics": {k: v for k, v in mt.items() if k not in ("series", "benchmark_series")},
            "equity_series": [{k: p[k] for k in ("date", "equity_cents", "drawdown", "exposure", "provisional")} for p in mt["series"]],
            "decision_outcomes": outcomes, "confidence_calibration": conf,
            "calibration_note": "buckets with fewer than 20 decisions say nothing about calibration",
            "skills": {"residual-edge-analyzer": resid, "weekly-performance-digest": digest},
            "backtest_expert_gate": "NOT_EVALUABLE" if mt["closed_trades"] < 30 else "evaluable",
            "existing_hypotheses": [dict(h) for h in conn.execute("SELECT * FROM strategy_hypotheses WHERE experiment_id=? ORDER BY id", (exp_id,))],
            "rules": ["Hypotheses must be dated, falsifiable and prospective; they never change past records.",
                      "Do not optimize rules to past trades and present the result as the original record.",
                      "Name the minimum sample and the date you will re-check before believing the hypothesis."],
            "payload_schema": {"schema_version": 1, "run_id": run_id, "model": {"provider": "...", "model_id": "...", "effort": "..."},
                               "observations": "<= 3000 chars", "hypotheses": [{"title": "...", "hypothesis": "...", "rationale": "...",
                                                                                "falsification_test": "...", "minimum_sample": "...",
                                                                                "proposed_change": {"type": "none|config|strategy",
                                                                                                    "config_overrides": {}, "strategy_text": ""},
                                                                                "evidence_refs": []}]}}
    d = paths.runs_dir / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "review_briefing.json"
    p.write_text(json.dumps(data, indent=1, default=str))
    with db.transaction(conn):
        runs.set_status(conn, run_id, "awaiting_decisions", clock.iso(now), briefing_path=str(p))
    return {"run_id": run_id, "briefing_path": str(p), "sessions_valued": mt["sessions_valued"], "closed_trades": mt["closed_trades"],
            "residual_edge": resid.get("verdict") or resid.get("status"), "digest": digest.get("status")}


def validate(payload, run_id) -> list[str]:
    e = []
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("run_id") != run_id:
        return [f"payload must be an object with schema_version 1 and run_id {run_id}"]
    extra = set(payload) - {"schema_version", "run_id", "model", "observations", "hypotheses"}
    if extra:
        e.append(f"unknown keys {sorted(extra)}")
    if not isinstance(payload.get("observations"), str) or not payload["observations"].strip() or len(payload["observations"]) > 3000:
        e.append("observations is required (<= 3000 chars)")
    for i, h in enumerate(payload.get("hypotheses") or []):
        if set(h) - HYP_KEYS:
            e.append(f"hypotheses[{i}] unknown keys {sorted(set(h) - HYP_KEYS)}")
        for k in ("title", "hypothesis", "rationale", "falsification_test"):
            if not isinstance(h.get(k), str) or not h[k].strip():
                e.append(f"hypotheses[{i}].{k} is required")
        pc = h.get("proposed_change") or {"type": "none"}
        if pc.get("type") not in CHANGE_TYPES:
            e.append(f"hypotheses[{i}].proposed_change.type must be one of {CHANGE_TYPES}")
        if pc.get("type") == "config":
            try:
                bad = set(pc.get("config_overrides") or {}) - set(config.EDITABLE_KEYS)
                if bad:
                    e.append(f"hypotheses[{i}] config keys not editable: {sorted(bad)}")
            except TypeError:
                e.append(f"hypotheses[{i}].proposed_change.config_overrides must be an object")
    return e


def submit(conn, paths, *, run_id: int, payload: dict, now=None) -> dict:
    now = now or clock.now_utc()
    run = runs.get(conn, run_id)
    if run is None or run["run_kind"] != "review" or run["status"] != "awaiting_decisions":
        raise ValueError(f"run {run_id} is not an open review")
    errs = validate(payload, run_id)
    if errs:
        raise ValueError("; ".join(errs))
    exp_id = run["experiment_id"]
    earliest = cal.next_session_after(now + timedelta(minutes=5)).date.isoformat()
    ids = []
    with db.transaction(conn):
        for h in payload.get("hypotheses") or []:
            cur = conn.execute(
                "INSERT INTO strategy_hypotheses(experiment_id, created_at, review_run_id, title, hypothesis, rationale, evidence_json,"
                " proposed_change_json, falsification_test, minimum_sample, status, status_changed_at, earliest_effective_session)"
                " VALUES (?,?,?,?,?,?,?,?,?,?, 'proposed', ?, ?)",
                (exp_id, clock.iso(now), run_id, h["title"], h["hypothesis"], h["rationale"], json.dumps(h.get("evidence_refs") or []),
                 json.dumps(h.get("proposed_change") or {"type": "none"}), h["falsification_test"], h.get("minimum_sample"),
                 clock.iso(now), earliest))
            ids.append(int(cur.lastrowid))
        runs.log(conn, run_id, "review_submitted", payload["observations"][:500], {"hypotheses": ids})
        text = review_report(conn, exp_id, run_id, payload, ids, now)
        path = reports.save(conn, paths, run_id, "review", text, now.astimezone(cal.NY).date().isoformat())
        runs.set_status(conn, run_id, "completed", clock.iso(now), report_path=path)
    return {"run_id": run_id, "hypothesis_ids": ids, "earliest_effective_session": earliest, "report_path": path}


def adopt(conn, *, hypothesis_id: int, reason: str, now=None) -> dict:
    """Adopt a proposed hypothesis prospectively (new config/strategy version effective now)."""
    now = now or clock.now_utc()
    h = conn.execute("SELECT * FROM strategy_hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
    if h is None or h["status"] != "proposed":
        raise ValueError("only a proposed hypothesis can be adopted")
    # Adoption takes effect from this instant; any decision made from now on targets a session at or after
    # earliest_effective_session (computed at submission as the next session), so no past session is affected.
    pc = json.loads(h["proposed_change_json"])
    exp_id = h["experiment_id"]
    with db.transaction(conn):
        cid = sid = None
        if pc.get("type") == "config":
            cid = config.create_version(conn, exp_id, pc.get("config_overrides") or {}, created_by=f"hypothesis {hypothesis_id}",
                                        reason=reason, now=clock.iso(now))
        elif pc.get("type") == "strategy":
            cur_s = experiments.current_strategy(conn, exp_id, clock.iso(now))
            v = conn.execute("SELECT MAX(version)+1 FROM strategy_versions WHERE experiment_id=?", (exp_id,)).fetchone()[0]
            cur = conn.execute("INSERT INTO strategy_versions(experiment_id, version, title, description, rules_json, effective_from, created_at,"
                               " created_by, reason, hypothesis_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (exp_id, v, h["title"], pc.get("strategy_text") or cur_s["description"], cur_s["rules_json"], clock.iso(now),
                                clock.iso(now), f"hypothesis {hypothesis_id}", reason, hypothesis_id))
            sid = int(cur.lastrowid)
        conn.execute("UPDATE strategy_hypotheses SET status='adopted', status_changed_at=?, adopted_config_version_id=?, adopted_strategy_version_id=? WHERE id=?",
                     (clock.iso(now), cid, sid, hypothesis_id))
    return {"hypothesis_id": hypothesis_id, "config_version_id": cid, "strategy_version_id": sid}


def _change_words(change: dict) -> str:
    """A proposed change in words (the report was showing it as a JSON dump).
    SYNC WARNING: dashboard/pages._change_words says the same for the hypothesis drawer."""
    t = change.get("type", "none")
    if t == "config":
        return "risk settings: " + ("; ".join(f"{str(k).replace('_', ' ')} -> {v}" for k, v in (change.get("config_overrides") or {}).items())
                                    or "none listed")
    if t == "strategy":
        return "strategy text: " + (change.get("strategy_text") or "not given")
    return "none (observation only)"


def review_report(conn, exp_id, run_id, payload, ids, now) -> str:
    mt = metrics.compute(conn, exp_id)
    pct = lambda x: "n/a" if x is None else f"{x * 100:+.2f}%"
    exp = conn.execute("SELECT mode, benchmark_symbol FROM experiments WHERE id=?", (exp_id,)).fetchone()
    fixture, bench = exp["mode"] == "fixture", exp["benchmark_symbol"]  # the fixture's benchmark is DEMO.SPY, never "SPY"
    title = "PAPER TRADING · FIXTURE DEMO (synthetic data)" if fixture else "PAPER TRADING"
    pts = lambda x: "n/a" if x is None else f"{x * 100:+.2f} pts"  # a gap between returns, as on the dashboard
    lines = [f"# {title} · Review, run {run_id}", "", "> Reviews propose prospective changes only. Past records are immutable.", "",
             f"- Sessions valued: {mt['sessions_valued']}; closed trades: {mt['closed_trades']} ({mt['sample_note']})",
             f"- Return {pct(mt['total_return'])} vs {bench} {pct(mt['spy_benchmark_return'])} vs keeping cash +0.00%; "
             f"gap vs {bench} {pts(mt['excess_vs_spy'])}",
             f"- Max drawdown {pct(mt['max_drawdown'])}; average exposure {mt['avg_exposure'] * 100:.1f}%; turnover {mt['turnover']:.2f}x", "",
             "## Observations", "", payload["observations"], "", "## Hypotheses (proposed, prospective)", ""]
    for hid, h in zip(ids, payload.get("hypotheses") or []):
        lines += [f"### #{hid} {h['title']}", "", h["hypothesis"], "", f"- Rationale: {h['rationale']}",
                  f"- Falsification test: {h['falsification_test']}", f"- Minimum sample: {h.get('minimum_sample') or 'n/a'}",
                  f"- Proposed change: {_change_words(h.get('proposed_change') or {'type': 'none'})}", ""]
    if not ids:
        lines.append("No hypotheses proposed.")
    return "\n".join(lines) + "\n"
