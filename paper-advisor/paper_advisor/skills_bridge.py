"""Bridge to the installed, AUDITED third-party trading skills (tradermonty/claude-trading-skills).

Source and version: <project root>/.claude/skills (downloaded by scripts/install_trading_skills.py), verified
byte-identical to upstream commit cb0f3aadffed6d1089db4b224162c964368baa46 (see config/skills_manifest.json and SKILL_AUDIT.md).

How skills are used (and why so defensively):
- Every adopted skill runs UNMODIFIED as an isolated subprocess (`python -I -B`, scrubbed environment
  with no credentials, timeout, its own output directory under data/runs/<run>/skills/). Upstream
  sources are never edited; -B keeps bytecode out of the skill folders.
- Before running, the skill folder's tree hash must match the audited manifest; a changed skill is
  skipped, because the audit no longer describes the code that would run.
- Outputs are VALIDATED (data as-of must be the last completed session) and SANITIZED: advice that
  conflicts with this experiment (puts, inverse/leveraged ETFs, TQQQ, exposure "guidance") is stripped
  and logged. Skill outputs are advisory context for the Claude session; only paper-advisor's
  deterministic validator can approve an order.
- Every invocation is recorded in skill_runs with exit code, output hash and stderr tail, and its
  summary becomes an evidence item the advisor can cite.
SYNC WARNING: REGISTRY must list every directory in .claude/skills (tests/test_skills_bridge.py
enforces it) and must agree with the capability map in SKILL_AUDIT.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import analytics, clock, config, db, market_calendar as cal, metrics, universe
from .paths import APP_DIR, SKILLS_DIR

UPSTREAM_COMMIT = "cb0f3aadffed6d1089db4b224162c964368baa46"
MANIFEST_PATH = APP_DIR / "config" / "skills_manifest.json"
RUNNERS = Path(__file__).resolve().parent / "skill_runners"

ADAPTED, REVIEW, LINT, GUIDANCE, NOT_INTEGRATED, DISABLED = (
    "ADAPTED_MORNING", "ADAPTED_REVIEW", "ADAPTED_REPORT_LINT", "GUIDANCE_ONLY", "NOT_INTEGRATED_V1", "DISABLED")

# status, short reason. Full evidence with file:line references is in SKILL_AUDIT.md.
REGISTRY: dict[str, tuple[str, str]] = {
    "market-breadth-analyzer": (ADAPTED, "keyless public CSV; as-of checked against last session; trade advice stripped"),
    "uptrend-analyzer": (ADAPTED, "keyless public CSV; no internal freshness check, so we enforce as-of; advice stripped"),
    "ibd-distribution-day-monitor": (ADAPTED, "pure analyze_index() on our finished SPY/QQQ bars; TQQQ portfolio_action discarded"),
    "exposure-coach": (ADAPTED, "advisory exposure ceiling from fresh breadth/uptrend/distribution-day inputs; not enforced"),
    "position-sizer": (ADAPTED, "advisory sizing with caps always passed (upstream defaults are uncapped)"),
    "stockbee-momentum-burst-screener": (ADAPTED, "offline --prices-json path on our split-adjusted finished bars"),
    "data-quality-checker": (LINT, "warning-level lint of the morning report's dates; always exits 0 so findings are read from JSON"),
    "residual-edge-analyzer": (REVIEW, "alpha vs SPY with HAC errors; refuses thin samples (INSUFFICIENT_EVIDENCE is the honest answer early)"),
    "weekly-performance-digest": (REVIEW, "closed-trade metrics from adapter-written thesis files"),
    "market-news-analyst": (GUIDANCE, "research procedure adopted in /paper-morning; its impact score and unsourced % claims are not"),
    "us-stock-analysis": (GUIDANCE, "fundamental/news framework only; it asks for real-time web prices, which we forbid"),
    "backtest-expert": (GUIDANCE, "its NOT_EVALUABLE (<30 trades) gate informs /paper-review; scores are self-declared"),
    "trade-hypothesis-ideator": (GUIDANCE, "hypothesis card format (kill criteria, minimum viable experiment) used by /paper-review"),
    "trading-skills-navigator": (GUIDANCE, "routing reference only; must run with --no-api (otherwise routes to Alpaca)"),
    "market-environment-analysis": (GUIDANCE, "web-search report prompt without as-of capture; calendar helper not needed (own NYSE calendar)"),
    "us-market-bubble-detector": (GUIDANCE, "scores are model-assigned without underlying values; long-horizon context at most"),
    "technical-analyst": (NOT_INTEGRATED, "weekly_price_action is usable but drops the just-closed week on weekend runs; deferred"),
    "vcp-screener": (NOT_INTEGRATED, "analyze_stock needs >=222 bars and a date-joined SPY series (upstream aligns by index)"),
    "ftd-detector": (NOT_INTEGRATED, "needs index (not ETF) volume; SPY/QQQ proxies would change its semantics"),
    "macro-regime-detector": (NOT_INTEGRATED, "counts the partial current month as a bar; needs yfinance; missing components score 0"),
    "sector-analyst": (NOT_INTEGRATED, "same data as uptrend-analyzer's sector components; freshness checks the wrong file"),
    "breadth-chart-analyst": (NOT_INTEGRATED, "same CSVs as market-breadth-analyzer with weaker checks; image analysis deprecated upstream"),
    "trade-performance-coach": (NOT_INTEGRATED, "single-trade process checks need flags we do not record yet; monthly mode reads nothing"),
    "drawdown-circuit-breaker": (NOT_INTEGRATED, "realized-only, fixed account size, fails open on a missing state dir"),
    "pre-trade-discipline-gate": (NOT_INTEGRATED, "self-reported checklist; superseded by the deterministic validator"),
    "trader-memory-core": (NOT_INTEGRATED, "our SQLite ledger is the system of record; a second thesis store would diverge"),
    "kanchi-dividend-review-monitor": (NOT_INTEGRATED, "missing data reports OK (fails open)"),
    "stockbee-exhaustion-hammer-screener": (NOT_INTEGRATED, "designed around provisional pre-close bars; offline path possible later"),
    "stockbee-episodic-pivot-analyzer": (DISABLED, "silently scores the latest bar when the event bar is missing; ignores earnings timing"),
    "stockbee-20pct-study": (DISABLED, "research study; live universe is the first N tickers alphabetically"),
    "stockbee-setup-fluency-trainer": (DISABLED, "study store; wins from intraday highs; not a decision input"),
    "portfolio-manager": (DISABLED, "BROKERAGE: Alpaca account connector; non-'true' ALPACA_PAPER selects the live host"),
    "parabolic-short-trade-planner": (DISABLED, "SHORT SELLING and Alpaca trading-host reads"),
    "breakout-trade-planner": (DISABLED, "builds Alpaca order bodies; no aggregate cash cap; needs FMP-backed VCP output"),
    "mt5-robot-tester": (DISABLED, "launches a MetaTrader broker terminal; forex/CFD; Windows only"),
    "options-strategy-advisor": (DISABLED, "OPTIONS are outside the experiment"),
    "futures-position-sizer": (DISABLED, "FUTURES are outside the experiment"),
    "cot-contrarian-detector": (DISABLED, "futures COT data; requires paid FMP tier"),
    "news-reaction-failure-analyzer": (DISABLED, "part of the futures COT contrarian chain; requires FMP"),
    "contrarian-setup-gate": (DISABLED, "consumes the COT chain and emits SHORT setups"),
    "pair-trade-screener": (DISABLED, "market-neutral pairs need SHORT legs; requires FMP"),
    "crypto-regime-analyzer": (DISABLED, "CRYPTO is outside the experiment"),
    "fxmacrodata-calendar": (DISABLED, "FX macro vendor; keyless access unverified"),
    "kanchi-dividend-us-tax-accounting": (DISABLED, "tax planning; not applicable to a paper account"),
    "kanchi-dividend-sop": (DISABLED, "requires FMP_API_KEY"),
    "canslim-screener": (DISABLED, "requires FMP_API_KEY; fundamentals are latest-restated, not point-in-time"),
    "value-dividend-screener": (DISABLED, "requires FMP_API_KEY (and FINVIZ for its prescreen)"),
    "dividend-growth-pullback-screener": (DISABLED, "requires FMP_API_KEY; future-dated dividends distort growth"),
    "earnings-calendar": (DISABLED, "requires FMP_API_KEY; drops events silently on fetch failures (we use our own keyless calendar)"),
    "economic-calendar-fetcher": (DISABLED, "requires FMP_API_KEY"),
    "earnings-trade-analyzer": (DISABLED, "requires FMP_API_KEY; --as-of does not limit price data"),
    "pead-screener": (DISABLED, "requires FMP_API_KEY; can use a partial current week"),
    "institutional-flow-tracker": (DISABLED, "requires FMP_API_KEY; 13F keyed on quarter end with no filing-date gate"),
    "us-undervalued-growth-screener": (DISABLED, "requires FMP_API_KEY; discover stage has no as-of guard"),
    "market-top-detector": (DISABLED, "requires FMP_API_KEY; mixes live quotes with model-gathered numbers"),
    "downtrend-duration-analyzer": (DISABLED, "requires FMP_API_KEY; centered peak windows look ahead"),
    "theme-detector": (DISABLED, "scrapes FINVIZ (undated) and writes into .claude/ by default"),
    "finviz-screener": (DISABLED, "only builds and opens browser URLs; returns no data"),
    "manifoldbt-backtester": (DISABLED, "external manifoldbt engine not installed; fills at next bar close, not next open"),
    "edge-candidate-agent": (DISABLED, "research ticket generator; export forces full-sample (in-sample) validation"),
    "edge-concept-synthesizer": (DISABLED, "canned text grouping; a single hint counts as support"),
    "edge-hint-extractor": (DISABLED, "rules over regime labels/news; undated news bypasses as-of"),
    "edge-pipeline-orchestrator": (DISABLED, "revision step rounds thresholds (-0.07 -> -0) until drafts pass"),
    "edge-signal-aggregator": (DISABLED, "uncalibrated weights; wall-clock recency makes replays irreproducible"),
    "edge-strategy-designer": (DISABLED, "templates tuned to pass the reviewer"),
    "edge-strategy-reviewer": (DISABLED, "PASS is a keyword/structure lint, not validation"),
    "strategy-pivot-designer": (DISABLED, "iterates on the same data until plateau; no multiple-testing control"),
    "signal-postmortem": (DISABLED, "without FMP every outcome is NEUTRAL; manual exit prices ignored"),
    "stanley-druckenmiller-investment": (DISABLED, "two required inputs need FMP; malformed input becomes the most bullish score"),
    "scenario-analyzer": (DISABLED, "requires scenario-analyst/strategy-reviewer agents that are not installed"),
    "dual-axis-skill-reviewer": (DISABLED, "developer tooling (META)"),
    "skill-designer": (DISABLED, "developer tooling (META)"),
    "skill-idea-miner": (DISABLED, "META; reads Claude session logs and sends snippets to `claude -p` (privacy, spend)"),
    "skill-integration-tester": (DISABLED, "developer tooling (META); needs a workflows CLAUDE.md that does not exist here"),
}

ADVICE_KEYS = {"actions", "exposure_guidance", "guidance", "recommended_actions", "action_items", "portfolio_action",
               "recommendations", "exposure_recommendation"}
CONFLICT_WORDS = ("inverse", " puts", "put option", "short ", "tqqq", "sqqq", "leverage", "3x", "2x")


def _manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text())["skills"]
    except (OSError, KeyError, ValueError):
        return {}


def tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in d.rglob("*") if x.is_file() and "__pycache__" not in x.parts and x.name != ".DS_Store"):
        h.update(str(p.relative_to(d)).encode())
        h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def integrity(name: str) -> tuple[bool, str]:
    d = SKILLS_DIR / name
    if not d.is_dir():
        return False, "skill not installed"
    want = _manifest().get(name)
    if want is None:
        return False, "skill not in the audited manifest"
    got = tree_hash(d)
    return (got == want), ("matches audited manifest" if got == want else f"tree hash {got[:12]} != audited {want[:12]}")


def _python() -> str:
    v = APP_DIR / ".venv" / "bin" / "python"
    return str(v) if v.exists() else sys.executable


def _run(cmd: list[str], cwd: Path, timeout: int = 180) -> tuple[int | None, str, str]:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(cwd), "LANG": "en_US.UTF-8", "TZ": "America/New_York"}
    try:
        # stdin=DEVNULL: a child otherwise inherits our stdin, and under an interactive or harnessed
        # session (an open pipe) any upstream code that reads input blocks silently until the timeout.
        p = subprocess.run(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        return None, exc.stdout or "", f"timeout after {timeout}s"


def _record(conn, run_id, name, entrypoint, args, started, rc, status, out_path=None, out_text=None, stdout="", stderr="",
            detail="", evidence=None, exp_id=None) -> int:
    sha = hashlib.sha256(out_text.encode()).hexdigest() if out_text else None
    with db.transaction(conn):
        eid = None
        if evidence is not None and exp_id is not None:
            cur = conn.execute(
                "INSERT INTO evidence_items(experiment_id, run_id, kind, symbol, title, url, publisher, published_at, retrieved_at,"
                " summary, content_sha256, quality_flags, created_by, created_at) VALUES (?,?, 'skill_output', NULL, ?, NULL, ?, NULL, ?, ?, ?, ?, 'system', ?)",
                (exp_id, run_id, evidence["title"], f"skill {name} @ {UPSTREAM_COMMIT[:7]}", clock.now_iso(),
                 json.dumps(evidence["summary"], default=str)[:4000], sha, json.dumps(evidence.get("flags", [])), clock.now_iso()))
            eid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO skill_runs(run_id, skill_name, upstream_commit, entrypoint, args_json, started_at, finished_at, exit_code, status,"
            " output_path, output_sha256, stdout_tail, stderr_tail, detail, evidence_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, name, UPSTREAM_COMMIT, entrypoint, json.dumps(args, default=str), started, clock.now_iso(), rc, status,
             None if out_path is None else str(out_path), sha, (stdout or "")[-1500:], (stderr or "")[-1500:], detail, eid))
    return eid


def _strip_advice(obj, stripped: list, path=""):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ADVICE_KEYS:
                stripped.append(f"{path}{k}")
                continue
            out[k] = _strip_advice(v, stripped, f"{path}{k}.")
        return out
    if isinstance(obj, list):
        return [_strip_advice(x, stripped, path) for x in obj]
    if isinstance(obj, str) and any(w in obj.lower() for w in CONFLICT_WORDS):
        stripped.append(f"{path}(text)")
        return "[removed: advice conflicting with long-only unleveraged rules]"
    return obj


def _freshness(as_of: str | None, last_session: str | None) -> tuple[bool, int | None]:
    if not as_of or not last_session:
        return False, None
    try:
        behind = cal.count_sessions_after(date.fromisoformat(as_of[:10]), date.fromisoformat(last_session))
    except ValueError:
        return False, None
    return behind == 0, behind


def _newest_json(d: Path, prefix: str) -> Path | None:
    files = sorted(p for p in d.glob(f"{prefix}*.json") if "history" not in p.name)
    return files[-1] if files else None


def _csv_skill(conn, run_id, exp_id, name, script, prefix, out_dir, last_session, summarize):
    started = clock.now_iso()
    ok, why = integrity(name)
    if not ok:
        _record(conn, run_id, name, script, {}, started, None, "skipped", detail=why)
        return {"status": "skipped", "reason": why}
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [_python(), "-I", "-B", str(SKILLS_DIR / name / "scripts" / script), "--output-dir", str(out_dir)]
    rc, so, se = _run(cmd, out_dir)
    f = _newest_json(out_dir, prefix)
    if rc != 0 or f is None:
        _record(conn, run_id, name, script, {"cmd": cmd[2:]}, started, rc, "failed", stdout=so, stderr=se, detail="no JSON output")
        return {"status": "failed", "exit_code": rc, "stderr_tail": (se or "")[-300:]}
    raw = f.read_text()
    data = json.loads(raw)
    stripped: list = []
    clean = _strip_advice(data, stripped)
    as_of = (data.get("data_provenance") or {}).get("as_of")
    fresh, behind = _freshness(as_of, last_session)
    summ = {"status": "ok" if fresh else "stale", "as_of": as_of, "sessions_behind_last_completed": behind,
            "source": (data.get("data_provenance") or {}).get("endpoint"), **summarize(clean),
            "stripped_advice_fields": sorted(set(stripped)),
            "caveats": ["heuristic composite score, not a probability", "data provider flags: not point-in-time, not survivorship-free"]}
    flags = [] if fresh else ["stale_skill_data"]
    eid = _record(conn, run_id, name, script, {"cmd": cmd[2:]}, started, rc, "ok", out_path=f, out_text=raw, stdout=so, stderr=se,
                  detail=f"as_of={as_of} fresh={fresh}", evidence={"title": f"{name}: {summ.get('headline', '')}", "summary": summ, "flags": flags},
                  exp_id=exp_id)
    summ["evidence_id"] = eid
    summ["output_path"] = str(f)
    return summ


def _breadth_summary(d):
    c = d.get("composite") or {}
    comps = {k: (v.get("score") if isinstance(v, dict) else v) for k, v in (c.get("component_scores") or {}).items()}
    return {"composite_score": c.get("composite_score"), "zone": c.get("zone"), "data_quality": c.get("data_quality"),
            "component_scores": comps, "headline": f"breadth {c.get('composite_score')} ({c.get('zone')})",
            "note": "the cycle component uses peak/trough markers that can be revised after the fact"}


def _uptrend_summary(d):
    c = d.get("composite") or {}
    return {"composite_score": c.get("composite_score"), "composite_score_raw": c.get("composite_score_raw"),
            "warning_penalty": c.get("warning_penalty"), "zone": c.get("zone"), "active_warnings": c.get("active_warnings"),
            "data_quality": c.get("data_quality"), "headline": f"uptrend {c.get('composite_score')} ({c.get('zone')})",
            "note": "sector components come from an undated summary file upstream"}


def _bars_payload(conn, symbols, last_session, limit=260, newest_first=True):
    out = {}
    for s in symbols:
        series = analytics.adjusted_series(conn, s, last_session, limit=limit)
        rows = [{"date": x["date"], "open": round(x["open"], 4), "high": round(x["high"], 4), "low": round(x["low"], 4),
                 "close": round(x["close"], 4), "volume": int(x["volume"])} for x in series]
        out[s] = list(reversed(rows)) if newest_first else rows
    return out


def run_morning(conn, paths, exp_id: int, run_id: int, now) -> dict:
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    last = cal.last_completed_session(now, cfg.bar_finalization_minutes)
    last_s = last.date.isoformat() if last else None
    base = paths.runs_dir / str(run_id) / "skills"
    results: dict = {"upstream": f"tradermonty/claude-trading-skills@{UPSTREAM_COMMIT[:12]}",
                     "advisory_only": "skill outputs inform judgment; only the deterministic validator approves orders"}
    forward = db.db_mode(conn) == "forward"
    if forward:
        results["market-breadth-analyzer"] = _csv_skill(conn, run_id, exp_id, "market-breadth-analyzer", "market_breadth_analyzer.py",
                                                        "market_breadth_", base / "market-breadth-analyzer", last_s, _breadth_summary)
        results["uptrend-analyzer"] = _csv_skill(conn, run_id, exp_id, "uptrend-analyzer", "uptrend_analyzer.py",
                                                 "uptrend_analysis_", base / "uptrend-analyzer", last_s, _uptrend_summary)
    else:
        for n in ("market-breadth-analyzer", "uptrend-analyzer"):
            results[n] = {"status": "skipped", "reason": "fixture database: these skills fetch real market-wide CSVs, which must not be mixed into a synthetic demo"}
    bench = cfg.benchmark_symbol
    idx = [s for s in (bench, "QQQ") if universe.get(conn, s) is not None] or [bench]
    results["ibd-distribution-day-monitor"] = _ibd(conn, run_id, exp_id, base / "ibd-distribution-day-monitor", idx, last_s)
    results["exposure-coach"] = _exposure(conn, run_id, exp_id, base / "exposure-coach", results, now)
    results["position-sizer"] = _sizer(conn, run_id, exp_id, base / "position-sizer", last_s, cfg, now)
    results["stockbee-momentum-burst-screener"] = _momentum(conn, run_id, exp_id, base / "stockbee-momentum-burst-screener", last_s)
    return results


def _ibd(conn, run_id, exp_id, out_dir, symbols, last_s):
    name, started = "ibd-distribution-day-monitor", clock.now_iso()
    ok, why = integrity(name)
    if not ok or not last_s:
        _record(conn, run_id, name, "ibd_runner.py", {}, started, None, "skipped", detail=why if not ok else "no completed session")
        return {"status": "skipped", "reason": why if not ok else "no completed session"}
    out_dir.mkdir(parents=True, exist_ok=True)
    inp = out_dir / "input.json"
    inp.write_text(json.dumps({"as_of": last_s, "histories": _bars_payload(conn, symbols, last_s, limit=120)}))
    rc, so, se = _run([_python(), "-I", "-B", str(RUNNERS / "ibd_runner.py"), str(SKILLS_DIR / name / "scripts"), str(inp)], out_dir)
    if rc != 0:
        _record(conn, run_id, name, "ibd_runner.py", {"symbols": symbols}, started, rc, "failed", stdout=so, stderr=se)
        return {"status": "failed", "stderr_tail": se[-300:]}
    data = json.loads(so)
    (out_dir / "output.json").write_text(so)
    summ = {"status": "ok", "as_of": last_s, "by_index": {s: {k: v for k, v in d.items() if k in
                                                            ("risk_level", "d5_count", "d15_count", "d25_count", "is_distribution_day_today", "error")}
                                                        for s, d in data.items()},
            "method": "IBD distribution day: close down >= 0.2% on higher volume; 25-session expiry; 5% invalidation",
            "discarded": "upstream portfolio_action (recommends TQQQ exposure; leveraged products are excluded)"}
    summ["headline"] = ", ".join(f"{s} {d.get('risk_level')} d25={d.get('d25_count')}" for s, d in summ["by_index"].items())
    eid = _record(conn, run_id, name, "ibd_runner.py", {"symbols": symbols}, started, rc, "ok", out_path=out_dir / "output.json", out_text=so,
                  stderr=se, evidence={"title": f"distribution days: {summ['headline']}", "summary": summ}, exp_id=exp_id)
    summ["evidence_id"] = eid
    return summ


def _exposure(conn, run_id, exp_id, out_dir, results, now):
    name, started = "exposure-coach", clock.now_iso()
    ok, why = integrity(name)
    if not ok:
        _record(conn, run_id, name, "calculate_exposure.py", {}, started, None, "skipped", detail=why)
        return {"status": "skipped", "reason": why}
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [_python(), "-I", "-B", str(SKILLS_DIR / name / "scripts" / "calculate_exposure.py"), "--output-dir", str(out_dir), "--json-only"]
    used, excluded = [], []
    for key, flag in (("market-breadth-analyzer", "--breadth"), ("uptrend-analyzer", "--uptrend")):
        r = results.get(key) or {}
        if r.get("status") == "ok" and r.get("output_path"):
            args += [flag, r["output_path"]]
            used.append(key)
        else:
            excluded.append(f"{key} ({r.get('status', 'missing')})")
    ibd = results.get("ibd-distribution-day-monitor") or {}
    if ibd.get("status") == "ok":
        counts = [d.get("d25_count") for d in ibd["by_index"].values() if d.get("d25_count") is not None]
        if counts:
            tr = out_dir / "top_risk_input.json"
            tr.write_text(json.dumps({"distribution_days": max(counts)}))
            args += ["--top-risk", str(tr)]
            used.append("distribution days (as top-risk proxy)")
    rc, so, se = _run(args, out_dir)
    f = _newest_json(out_dir, "exposure")
    if rc != 0 or f is None:
        _record(conn, run_id, name, "calculate_exposure.py", {"inputs": used}, started, rc, "failed", stdout=so, stderr=se)
        return {"status": "failed", "stderr_tail": (se or "")[-300:]}
    raw = f.read_text()
    d = json.loads(raw)
    summ = {"status": "ok", "inputs_used": used, "inputs_excluded": excluded + ["macro regime (disabled skill)"],
            "exposure_ceiling_pct": d.get("exposure_ceiling_pct"), "recommendation": d.get("recommendation"),
            "confidence": d.get("confidence"), "composite_score": d.get("composite_score"), "inputs_missing": d.get("inputs_missing"),
            "note": "ADVISORY ONLY: not enforced by the validator. Missing critical inputs reduce its score by design."}
    summ["headline"] = f"ceiling {summ['exposure_ceiling_pct']}% {summ['recommendation']} (confidence {summ['confidence']})"
    eid = _record(conn, run_id, name, "calculate_exposure.py", {"inputs": used}, started, rc, "ok", out_path=f, out_text=raw, stderr=se,
                  evidence={"title": f"exposure coach: {summ['headline']}", "summary": summ}, exp_id=exp_id)
    summ["evidence_id"] = eid
    return summ


def _sizer(conn, run_id, exp_id, out_dir, last_s, cfg, now):
    name, started = "position-sizer", clock.now_iso()
    ok, why = integrity(name)
    if not ok or not last_s:
        _record(conn, run_id, name, "sizer_runner.py", {}, started, None, "skipped", detail=why if not ok else "no completed session")
        return {"status": "skipped", "reason": why if not ok else "no completed session"}
    m = metrics.compute(conn, exp_id)
    equity = m["equity_cents"] / 100
    cands = []
    for s in universe.symbols(conn):
        ind = analytics.indicators(analytics.adjusted_series(conn, s, last_s, limit=60))
        if ind.get("atr14_pct") and ind.get("last_close"):
            cands.append({"symbol": s, "entry": ind["last_close"], "atr": round(ind["atr14_pct"] * ind["last_close"], 4)})
    out_dir.mkdir(parents=True, exist_ok=True)
    inp = out_dir / "input.json"
    params = {"equity": equity, "risk_pct": 1.0, "atr_multiplier": 2.0, "max_position_pct": float(cfg.max_position_pct), "candidates": cands}
    inp.write_text(json.dumps(params))
    rc, so, se = _run([_python(), "-I", "-B", str(RUNNERS / "sizer_runner.py"), str(SKILLS_DIR / name / "scripts"), str(inp)], out_dir)
    if rc != 0:
        _record(conn, run_id, name, "sizer_runner.py", params, started, rc, "failed", stdout=so, stderr=se)
        return {"status": "failed", "stderr_tail": se[-300:]}
    rows = json.loads(so)
    (out_dir / "output.json").write_text(so)
    summ = {"status": "ok", "assumptions": "risk 1% of equity per trade, stop = entry - 2 x ATR(14), max position "
                                            f"{cfg.max_position_pct * 100}% of equity, fractional shares; ADVISORY only",
            "equity_usd": round(equity, 2), "rows": rows, "headline": f"{len(rows)} sizing rows at 1% risk / 2xATR stop"}
    eid = _record(conn, run_id, name, "sizer_runner.py", {"risk_pct": 1.0, "atr_multiplier": 2.0}, started, rc, "ok",
                  out_path=out_dir / "output.json", out_text=so, stderr=se,
                  evidence={"title": "position-sizer advisory table", "summary": {"assumptions": summ["assumptions"], "rows": rows[:30]}},
                  exp_id=exp_id)
    summ["evidence_id"] = eid
    return summ


def _momentum(conn, run_id, exp_id, out_dir, last_s):
    name, started = "stockbee-momentum-burst-screener", clock.now_iso()
    ok, why = integrity(name)
    if not ok or not last_s:
        _record(conn, run_id, name, "screen_momentum_burst.py", {}, started, None, "skipped", detail=why if not ok else "no completed session")
        return {"status": "skipped", "reason": why if not ok else "no completed session"}
    syms = [s for s in universe.symbols(conn) if universe.get(conn, s)["asset_type"] == "COMMON_STOCK"]
    out_dir.mkdir(parents=True, exist_ok=True)
    inp = out_dir / "prices.json"
    inp.write_text(json.dumps(_bars_payload(conn, syms, last_s, limit=80)))
    cmd = [_python(), "-I", "-B", str(SKILLS_DIR / name / "scripts" / "screen_momentum_burst.py"), "--prices-json", str(inp),
           "--symbols", *syms, "--output-dir", str(out_dir)]
    rc, so, se = _run(cmd, out_dir)
    f = _newest_json(out_dir, "stockbee_momentum_burst_")
    if rc != 0 or f is None:
        _record(conn, run_id, name, "screen_momentum_burst.py", {"symbols": syms}, started, rc, "failed", stdout=so, stderr=se)
        return {"status": "failed", "exit_code": rc, "stderr_tail": (se or "")[-300:]}
    raw = f.read_text()
    d = json.loads(raw)
    cands = [{k: c.get(k) for k in ("symbol", "state", "setup_score", "trigger_types", "risk_pct_to_stop", "close", "notes") if k in c}
             for c in d.get("candidates", [])]
    live = [c for c in cands if c.get("state") != "REJECTED"]
    summ = {"status": "ok", "as_of": last_s, "scanned": len(syms), "non_rejected": live[:10],
            "rejected_count": len(cands) - len(live), "headline": f"{len(live)} momentum-burst setups among {len(syms)} stocks",
            "caveat": "signal bar is the last COMPLETED session; any entry would be the next open, not the signal-day close"}
    eid = _record(conn, run_id, name, "screen_momentum_burst.py", {"symbols": len(syms)}, started, rc, "ok", out_path=f, out_text=raw,
                  stderr=se, evidence={"title": f"momentum burst: {summ['headline']}", "summary": summ}, exp_id=exp_id)
    summ["evidence_id"] = eid
    return summ


def lint_report(paths, conn, run_id: int, text: str, now) -> dict:
    """data-quality-checker as a WARNING-level lint (it exits 0 even with errors, so we read its JSON)."""
    name, started = "data-quality-checker", clock.now_iso()
    ok, why = integrity(name)
    if not ok:
        _record(conn, run_id, name, "check_data_quality.py", {}, started, None, "skipped", detail=why)
        return {"status": "skipped", "findings": []}
    out_dir = paths.runs_dir / str(run_id) / "skills" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    rp = out_dir / "report.md"
    rp.write_text(text, encoding="utf-8")
    as_of = now.astimezone(cal.NY).date().isoformat()
    cmd = [_python(), "-I", "-B", str(SKILLS_DIR / name / "scripts" / "check_data_quality.py"), "--file", str(rp), "--checks", "dates",
           "--as-of", as_of, "--output-dir", str(out_dir)]
    rc, so, se = _run(cmd, out_dir, timeout=60)
    f = _newest_json(out_dir, "data_quality_")
    findings = []
    if f is not None:
        d = json.loads(f.read_text())
        # Upstream writes a bare JSON list of findings ({severity, category, message, line_number}).
        items = d if isinstance(d, list) else (d.get("findings") or d.get("issues") or [])
        for it in items:
            if isinstance(it, dict):
                findings.append(f"{it.get('severity', '?')}: {it.get('message') or it.get('description') or it}")
    _record(conn, run_id, name, "check_data_quality.py", {"checks": "dates", "as_of": as_of}, started, rc,
            "ok" if f is not None else "failed", out_path=f, out_text=f.read_text() if f else None, stdout=so, stderr=se,
            detail=f"{len(findings)} findings")
    return {"status": "ok" if f is not None else "failed", "findings": findings}


def residual_edge(conn, paths, exp_id: int, run_id: int) -> dict:
    name, started = "residual-edge-analyzer", clock.now_iso()
    ok, why = integrity(name)
    if not ok:
        _record(conn, run_id, name, "analyze_residual_edge.py", {}, started, None, "skipped", detail=why)
        return {"status": "skipped", "reason": why}
    mt = metrics.compute(conn, exp_id)
    port = {p["date"]: p["daily_return"] for p in mt["series"]}
    bench = {p["date"]: p["daily_return"] for p in mt["benchmark_series"]}
    dates = sorted(d for d in port if d in bench and port[d] is not None and bench[d] is not None)
    out_dir = paths.runs_dir / str(run_id) / "skills" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    csv = out_dir / "returns.csv"
    csv.write_text("date,strategy_return,spy_return\n" + "".join(f"{d},{port[d]:.10f},{bench[d]:.10f}\n" for d in dates))
    spec = {"schema_version": "1.0", "date_column": "date", "strategy_column": "strategy_return", "return_unit": "decimal",
            "frequency": "daily", "primary_model": {"name": "spy", "baseline_columns": ["spy_return"]}, "sensitivity_models": [],
            "regime_columns": [], "rolling_window": 63, "minimum_observations": 60, "minimum_regime_observations": 20,
            "minimum_rolling_windows": 12, "hac_lags": "auto", "include_series": False,
            "data_declarations": {"baseline_selection": "predeclared", "strategy_return_basis": "net", "baseline_return_basis": "net",
                                  "analysis_scope": "out_of_sample", "universe_data": "point_in_time"}}
    sp = out_dir / "spec.json"
    sp.write_text(json.dumps(spec))
    outj = out_dir / "residual_edge.json"
    cmd = [_python(), "-I", "-B", str(SKILLS_DIR / name / "scripts" / "analyze_residual_edge.py"), "--input", str(csv), "--config", str(sp),
           "--output-json", str(outj)]
    rc, so, se = _run(cmd, out_dir, timeout=120)
    if not outj.exists():
        _record(conn, run_id, name, "analyze_residual_edge.py", {"observations": len(dates)}, started, rc, "failed", stdout=so, stderr=se)
        return {"status": "failed", "observations": len(dates), "exit_code": rc, "stderr_tail": (se or so or "")[-400:]}
    raw = outj.read_text()
    d = json.loads(raw)
    summ = {"status": "ok", "observations": len(dates), "verdict": d.get("status") or d.get("verdict"),
            "summary": {k: d.get(k) for k in ("status", "verdict", "reasons", "warnings", "primary_model") if k in d}}
    _record(conn, run_id, name, "analyze_residual_edge.py", {"observations": len(dates)}, started, rc, "ok", out_path=outj, out_text=raw,
            stdout=so, stderr=se, evidence={"title": f"residual edge vs SPY: {summ['verdict']}", "summary": summ}, exp_id=exp_id)
    return summ


def weekly_digest(conn, paths, exp_id: int, run_id: int, from_date: str, to_date: str) -> dict:
    """Adapter: write one JSON-formatted (YAML-compatible) thesis file per CLOSED round trip, using the
    INITIAL stop (the audit found the digest computes R from exit.stop_loss and breaks on trailed stops)."""
    name, started = "weekly-performance-digest", clock.now_iso()
    ok, why = integrity(name)
    if not ok:
        _record(conn, run_id, name, "generate_weekly_digest.py", {}, started, None, "skipped", detail=why)
        return {"status": "skipped", "reason": why}
    mt = metrics.compute(conn, exp_id)
    out_dir = paths.runs_dir / str(run_id) / "skills" / name
    state = out_dir / "theses"
    state.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, t in enumerate(mt["trades"], start=1):
        if not t["closed_at"]:
            continue
        fill = conn.execute("SELECT f.*, o.decision_id FROM fills f JOIN orders o ON o.id=f.order_id WHERE f.id=?", (t["buy_fill_ids"][0],)).fetchone()
        rule = conn.execute("SELECT * FROM position_rules WHERE decision_id=? ORDER BY id LIMIT 1", (fill["decision_id"],)).fetchone() if fill else None
        entry_px = float(fill["fill_price"]) if fill else None
        stop = float(Decimal(rule["reference_price"]) * (1 - Decimal(rule["stop_loss_pct"]))) if rule and rule["stop_loss_pct"] and rule["reference_price"] else None
        shares = fill["quantity_micro"] / 1e6 if fill else None
        pnl = (t["realized_cents"] + t["dividends_cents"]) / 100
        thesis = {"thesis_id": f"th_{exp_id}_{i}", "ticker": t["symbol"], "status": "CLOSED",
                  "entry": {"actual_date": t["opened_at"], "actual_price": entry_px},
                  "exit": {"actual_date": t["closed_at"], "stop_loss": stop}, "position": {"shares": shares},
                  "outcome": {"pnl_dollars": round(pnl, 2), "pnl_pct": round(pnl / (t["cost_cents"] / 100), 6) if t["cost_cents"] else None}}
        (state / f"th_{exp_id}_{i}.yaml").write_text(json.dumps(thesis))
        n += 1
    cmd = [_python(), "-I", "-B", str(SKILLS_DIR / name / "scripts" / "generate_weekly_digest.py"), "--state-dir", str(state),
           "--from-date", from_date, "--to-date", to_date, "--output-dir", str(out_dir)]
    rc, so, se = _run(cmd, out_dir, timeout=60)
    f = _newest_json(out_dir, "weekly")
    if rc != 0 or f is None:
        _record(conn, run_id, name, "generate_weekly_digest.py", {"theses": n}, started, rc, "failed", stdout=so, stderr=se)
        return {"status": "failed", "theses": n, "stdout": so[-300:], "stderr_tail": (se or "")[-300:]}
    raw = f.read_text()
    d = json.loads(raw)
    summ = {"status": "ok", "theses": n, "window": [from_date, to_date], "summary": d.get("summary")}
    _record(conn, run_id, name, "generate_weekly_digest.py", {"theses": n}, started, rc, "ok", out_path=f, out_text=raw, stdout=so,
            stderr=se, evidence={"title": f"weekly digest {from_date}..{to_date}", "summary": summ}, exp_id=exp_id)
    return summ


def registry_rows() -> list[dict]:
    installed = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()} if SKILLS_DIR.is_dir() else set()
    return [{"skill": n, "status": s, "reason": r, "installed": n in installed} for n, (s, r) in sorted(REGISTRY.items())]
