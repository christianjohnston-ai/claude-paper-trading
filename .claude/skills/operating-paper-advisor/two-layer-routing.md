# Two-layer routing in depth

Layer 1 is the trading-analysis skill library (what to analyze in the market). Layer 2 is paper-advisor
(how the advisor runs, decides, validates, simulates, records and shows it). This file is the full map
of where they meet.

Path convention (all files of this skill): `paper-advisor/...` and `.claude/...` are from the project root
(the folder you cloned); `tests/`, `config/`, `data/`, `reports/`, `backups/`, `exports/` and `bin/` are inside
`paper-advisor/`; module paths such as `broker_sim.py`, `dashboard/server.py`, `data_sources/http.py`,
`skill_runners/` and `migrations/0001_initial.sql` are inside `paper-advisor/paper_advisor/`.

## 1. Layer 1 at a glance

- **Source.** `tradermonty/claude-trading-skills` at commit `cb0f3aadffed6d1089db4b224162c964368baa46`.
  All 1,141 installed files were verified byte-identical to that commit on 2026-10-24
  (`paper-advisor/SKILL_AUDIT.md` section 1).
- **Location.** One folder per skill in `.claude/skills/`, installed by `scripts/install_trading_skills.py`. `.gitignore` ignores the folder's children
  (`.claude/skills/*`) so the upstream sources stay untouched and untracked. It re-includes each
  project-owned skill (`!.claude/skills/operating-paper-advisor/`), the only tracked folder there.
- **Verdicts.** `skills_bridge.REGISTRY` holds the status and a one-line reason for each skill. The
  evidence, with `file:line` citations, is in `SKILL_AUDIT.md` section 4. `paper-advisor/bin/pa skills`
  prints the registry; for the adopted skills it adds whether the folder still matches the audited hash.

| Status (`REGISTRY`) | Count | Skills | What it means here |
|---|---|---|---|
| `ADAPTED_MORNING` | 6 | market-breadth-analyzer, uptrend-analyzer, ibd-distribution-day-monitor, exposure-coach, position-sizer, stockbee-momentum-burst-screener | Run by code in every morning start (breadth and uptrend on the forward database only) |
| `ADAPTED_REPORT_LINT` | 1 | data-quality-checker | Lints the morning report's dates at finish; warnings only |
| `ADAPTED_REVIEW` | 2 | residual-edge-analyzer, weekly-performance-digest | Run by code in `/paper-review` |
| `GUIDANCE_ONLY` | 7 | market-news-analyst, us-stock-analysis, backtest-expert, trade-hypothesis-ideator, trading-skills-navigator, market-environment-analysis, us-market-bubble-detector | Their procedures shaped the `/paper-*` command text; no code runs |
| `NOT_INTEGRATED_V1` | 12 | technical-analyst, vcp-screener, ftd-detector, macro-regime-detector, sector-analyst, breadth-chart-analyst, trade-performance-coach, drawdown-circuit-breaker, pre-trade-discipline-gate, trader-memory-core, kanchi-dividend-review-monitor, stockbee-exhaustion-hammer-screener | Usable in principle, but not wired in; the reason is in the registry |
| `DISABLED` | 46 | Most skills that need `FMP_API_KEY` or Finviz; every one that touches a broker or trades shorts, options, futures, crypto or FX; developer tooling. Some key-needing skills are NOT_INTEGRATED instead (vcp-screener, ftd-detector, technical-analyst's CLI) | Never invoked. The four with brokerage contact (portfolio-manager, parabolic-short-trade-planner, breakout-trade-planner, mt5-robot-tester) have no adapter and no code path |

`tests/test_skills_bridge.py` enforces that the registry names exactly the third-party folders that are
installed, that 9 skills are adopted, and that the brokerage and out-of-scope skills are DISABLED and
never referenced by the bridge code.

## 2. Finding the right analysis skill

1. Run the navigator from the project root:
   ```bash
   paper-advisor/.venv/bin/python -I -B .claude/skills/trading-skills-navigator/scripts/recommend.py \
     --query "<the goal, verbatim>" --no-api --format json
   ```
   Flags (from `recommend.py`): `--query` (required), `--no-api`, `--time-budget 15m|30m|60m|90m|any`,
   `--experience beginner|intermediate|advanced`, `--format text|json`, and `--project-root`. The root
   defaults to the cwd and holds no `skills-index.yaml` here, so the bundled
   `assets/metadata_snapshot.json` is used (stderr says `metadata source: snapshot`).
2. Always pass `--no-api`. Without it the navigator can route a long-term investor to an Alpaca
   brokerage workflow (the registry's reason for its GUIDANCE status).
3. Read `primary_workflow`, `routing_diagnostics.status`, `no_api_path` and `note`. A `fallback` status
   means no persona matched. "screen tech stocks for VCP setups" falls back to the beginner workflow
   `market-regime-daily`, so also search skill names directly (`ls .claude/skills`).
4. Before trusting a skill, read its verdict (`paper-advisor/bin/pa skills`, or SKILL_AUDIT.md section 4)
   and its section-5 findings. Many fail open, look ahead or need paid keys.

## 3. Using a layer-1 skill outside the morning run (ad hoc research)

Allowed as research only. Its output is never an order and never replaces the briefing's prices. The
navigator is the one exception to the scratch-folder rule below: it was verified to write nothing, so
run it in place, as in section 2. Run that command from the project root so its relative path resolves;
with no `skills-index.yaml` there, the navigator uses its bundled snapshot.
- **Symbols outside the 24-symbol universe** can be researched but never bought (`not_in_universe`).
  Changing the universe materially should be a new experiment (README).
- **For more history than the briefing gives,** read the stored, as-traded bars read-only from the
  `price_bars` table through `db.connect(..., readonly=True)` (revision 1 is the one fills and
  valuations use).
- Run its scripts with `-B`, ideally `-I -B`, from a scratch directory, with any `--output-dir` pointing
  there. Never run anything inside the skill folder: bytecode or reports written there change the
  upstream tree that the integrity check hashes.
- If it needs `FMP_API_KEY`, Finviz or Alpaca, stop. No keys exist here, none should be requested, and
  those skills are DISABLED or NOT_INTEGRATED for a reason.
- If its finding should influence a decision, cite it in the next `/paper-morning` run's `evidence[]`,
  for example as kind `note` with a summary and the time it was produced. Name the skill in
  `skills_used`. Then the validator decides as usual.

## 4. The bridge: exactly how layer 1 feeds layer 2

Code: `paper-advisor/paper_advisor/skills_bridge.py` and `paper-advisor/paper_advisor/skill_runners/`.

| Skill | When | Input | How it runs | What reaches the briefing |
|---|---|---|---|---|
| market-breadth-analyzer | morning start, forward database only | public CSVs it fetches itself | `scripts/market_breadth_analyzer.py --output-dir <run>/skills/<name>` | composite score, zone, components, as-of, `stripped_advice_fields`, caveats |
| uptrend-analyzer | morning start, forward database only | public CSVs it fetches itself | `scripts/uptrend_analyzer.py --output-dir ...` | composite, zone, warnings, as-of, caveats |
| ibd-distribution-day-monitor | morning start | stored, finished SPY and QQQ bars (120) | `skill_runners/ibd_runner.py` imports upstream `ibd_monitor.analyze_index()` and bypasses its FMP loader | risk level, d5/d15/d25 counts per index. Its TQQQ `portfolio_action` is discarded |
| exposure-coach | morning start, after the three above | breadth and uptrend outputs only if `status` is `ok` (fresh), plus `{"distribution_days": max d25}` as the top-risk input | `scripts/calculate_exposure.py --json-only` | exposure ceiling %, recommendation, confidence, inputs used and excluded. ADVISORY ONLY: the validator does not enforce it |
| position-sizer | morning start | equity, every universe symbol's last close and ATR(14) | `skill_runners/sizer_runner.py`: risk 1%, stop 2 x ATR, `max_position_pct` always passed, fractional shares, no Kelly | one sizing row per symbol. Advisory: the validator alone sizes |
| stockbee-momentum-burst-screener | morning start | stored, finished bars (80) of the universe's COMMON_STOCK symbols | `scripts/screen_momentum_burst.py --prices-json ... --symbols ...` (offline) | up to 10 non-rejected setups. The caveat: the signal bar is the last completed session, and any entry is the next open |
| data-quality-checker | morning finish | the report text | `scripts/check_data_quality.py --checks dates --as-of <today ET>` | findings appended to the report as warnings (it exits 0 even with errors, so JSON is read) |
| residual-edge-analyzer | `/paper-review` start | daily returns of the portfolio vs the benchmark | `scripts/analyze_residual_edge.py` with a predeclared spec (`minimum_observations` 60) | verdict, verbatim: INSUFFICIENT_EVIDENCE below 60 daily observations, the honest answer early on |
| weekly-performance-digest | `/paper-review` start | one thesis file per closed round trip, using the INITIAL stop | `scripts/generate_weekly_digest.py` | closed-trade summary |

Every invocation, in order:
1. **Integrity:** `integrity(name)` compares `tree_hash(folder)` (sha256 over sorted path and file-hash
   pairs, ignoring `__pycache__` and `.DS_Store`) with `paper-advisor/config/skills_manifest.json`. On a
   mismatch, the run is recorded as `skipped` with the reason `tree hash <x> != audited <y>`, and the
   morning continues without that skill.
2. **Isolation:** `_python()` is the venv interpreter (`paper-advisor/.venv/bin/python`, else
   `sys.executable`), run with `-I -B`. The environment is only PATH (`/usr/bin:/bin:/usr/sbin:/sbin`),
   HOME (the output folder), LANG and TZ (`America/New_York`). stdin is `/dev/null`. Timeouts: 180 s by
   default, 120 s for residual-edge, 60 s for the lint and the digest.
3. **Sanitization:** `_strip_advice` drops these keys anywhere in the JSON: `actions`, `action_items`,
   `exposure_guidance`, `exposure_recommendation`, `guidance`, `portfolio_action`, `recommendations`,
   `recommended_actions`. It also replaces any string containing `inverse`, ` puts`, `put option`,
   `short `, `tqqq`, `sqqq`, `leverage`, `3x` or `2x` (case-insensitive) with
   `[removed: advice conflicting with long-only unleveraged rules]`.
4. **Freshness:** `_freshness(as_of, last_completed_session)` counts NYSE sessions after the as-of date.
   Zero is fresh; anything else is `stale`, plus a `stale_skill_data` flag on the evidence item. The
   adapters that read our own bars are fresh by construction, because they use only sessions closed for
   at least 60 minutes.
5. **Record:** one `skill_runs` row (upstream commit, args, exit code, output sha256, stdout and stderr
   tails), and on success one `evidence_items` row of kind `skill_output`. The decisions cite it by id.

What a layer-1 skill can never do here, and why:
- **Decide, size or execute.** Only `submission.apply_decisions_tx` creates orders, and only from the
  decision file Claude submits.
- **See credentials.** The subprocess environment is rebuilt from four variables
  (`test_skill_subprocesses_do_not_inherit_credentials`).
- **Change itself unnoticed.** The tree-hash gate skips a changed skill.
- **Reach a broker.** The brokerage-contact skills are DISABLED and never named in the bridge code
  (`test_brokerage_and_out_of_scope_skills_are_disabled_and_unreachable`).
- **Supply prices.** The briefing's prices are stored end-of-day bars. `/paper-morning` forbids "current"
  prices from web search, overriding `us-stock-analysis`, which asks for them.

## 5. Changing the bridge

**Adopt a new skill into the morning run:**
1. Audit it as SKILL_AUDIT.md did: read the code, look for fail-open defaults, partial bars and lookahead,
   and run its own tests in isolation. Add its row to section 4 and any findings to section 5.
2. Change its `REGISTRY` status, and add its tree hash to `config/skills_manifest.json`, computed with
   `skills_bridge.tree_hash(Path(".claude/skills/<name>"))`.
3. Write an adapter in the style of the existing ones: integrity check, finished bars only, `_run`,
   sanitize, freshness, `_record` with evidence. Call it from `run_morning`.
4. Update `tests/test_skills_bridge.py`. `test_adopted_skills_match_the_audited_manifest` asserts
   exactly 9 adopted skills, so raise that number.
5. Update the README and SKILL_AUDIT. Note that the build id changes: `skills_bridge.py` and `config/`
   are part of `code_tree_sha256`. Then follow the release steps in
   [testing-and-release.md](./testing-and-release.md).

**Update the upstream library to a new commit:** this re-opens the audit. Update `UPSTREAM_COMMIT` in
`skills_bridge.py`, the manifest's `commit`, `verified` and hashes, the `DEPENDENCIES` entry in
`cli.py`, the `requirements-skills.txt` pins and SKILL_AUDIT.md (the install script reads the commit and the
hashes from the manifest). Until then, a changed adopted skill is
skipped, which is the safe failure.

**Add a project-owned skill** (like this one): put it in `.claude/skills/<gerund-name>/`, add its name
to `PROJECT_SKILLS` in `tests/test_skills_bridge.py`, and add a `.gitignore` exception line
`!.claude/skills/<name>/` below `.claude/skills/*`. The ignore line must be `.claude/skills/*` (the
folder's children), not `.claude/skills/`: git never looks inside an excluded directory, so an exception
after `.claude/skills/` does nothing. Check with `git status --porcelain -uall .claude/skills`, which
must list only project-owned files. The registry test then still proves that the third-party set equals
`REGISTRY`.
