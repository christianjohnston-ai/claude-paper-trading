---
name: operating-paper-advisor
description: Use when a request touches paper-advisor, the local PAPER-ONLY trading advisor in this repository, or has to be routed between the two skill layers in that project - the 74 third-party trading-analysis skills in .claude/skills (what to analyze in the market) and this trading system (how the advisor runs, decides, validates, simulates, records and shows it). Covers /paper-morning, /paper-status, /paper-review, the paper-advisor/bin/pa CLI, the localhost dashboard with its seven pages and its guided tour (tour.js, data-tour anchors), run locks, expired-lock takeover and force-recover, reconcile-only reruns, why a BUY was rejected, simulated next-open fills, the append-only ledger, the SPY benchmark, sample-size thresholds, data sources and what reaches the model service, new experiments and capital, config versions, backups, exports, the fixture demo, the paper-only safety invariants, and tests and release on Python 3.13 and 3.10.
---

# Operating paper-advisor (this trading system's skill)

Project root: the folder this repository was cloned into (it holds `paper-advisor/` and `.claude/`). Every
command here runs from that folder unless it starts with `cd`. Paths starting `paper-advisor/` or `.claude/` are from the root. Everything is simulated money:
there is no brokerage connection and no code path that can place a real order.

## When to Use

- Any trading request in this project: decide first whether layer 1 or layer 2 answers it (below).
- Running or recovering the morning run: `/paper-morning`, its lock, reconcile-only reruns, a stuck or
  failed run.
- Explaining what the account did: a rejected BUY, an expired order, a fill, the P&L, the result vs SPY
  and cash, provisional valuations.
- Changing the setup: risk limits, a new experiment or capital, the fixture demo, backups, exports.
- Reading or changing the code, the dashboard, the tests or the skills bridge, and committing in this repo.
- Any request that touches real money, a brokerage, credentials or deployment, so it can be refused
  correctly.

## There are two skills here. Pick the layer before doing anything.

| | Layer 1: the trading skills | Layer 2: this trading system's skill (this file) |
|---|---|---|
| Answers | **What to analyze in the market**: breadth, trend, setups, news method, sizing math | **How the advisor runs, decides, validates, simulates, records and shows it** |
| What it is | 74 third-party skills from `tradermonty/claude-trading-skills` at commit `cb0f3aa`, one folder each in `.claude/skills/`, installed by `scripts/install_trading_skills.py`, git-ignored on purpose and byte-identical to upstream | The app in `paper-advisor/`, the three `.claude/commands/paper-*.md` procedures, and this skill |
| Entry point | `trading-skills-navigator` (always with `--no-api`) | `/paper-morning`, `/paper-status`, `/paper-review`, `paper-advisor/bin/pa`, the dashboard |
| Verdict per skill | `paper-advisor/SKILL_AUDIT.md` section 4, or `paper-advisor/bin/pa skills` | n/a |
| May it decide, size or execute for this account? | **Never.** Its output is advisory evidence, nothing more. | Yes, and only here: Claude proposes, `submission.py` accepts or rejects, `broker_sim.py` fills at a later open |

Layer 1 produces analysis. Layer 2 is the only thing that turns analysis into a recorded, validated,
simulated order for this account. Nothing a layer-1 skill prints can become an order by itself.

## Routing rules

Apply in order; the first match wins.
1. **Real money**, a brokerage, a live order, an API key for a broker: refuse. The system is paper-only
   by construction ([safety-invariants.md](./safety-invariants.md)). Never install or call a brokerage
   tool, MCP server or SDK.
2. **This account** (its cash, positions, orders, decisions, rejections, fills, runs, locks, limits,
   experiments, dashboard, data freshness, reports, code or tests): layer 2.
3. **Trading, sizing or executing** anything: layer 2's `/paper-morning` flow. Never run a layer-1 skill
   and act on its output directly.
4. **What to analyze**, which skill fits a goal, what an indicator means: layer 1. Find the skill with
   the navigator and read its audit verdict before trusting it.
5. **Mixed** ("research X and buy it if it looks good"): layer-1 research becomes evidence inside a
   layer-2 morning run, and the validator decides.

| Example request | Layer | What to do |
|---|---|---|
| "Which skill should I use for swing trading?" / "What works without API keys?" | 1 | Run the navigator (below), then read the verdict |
| "Screen tech stocks for VCP setups." | 1 | The skill is `vcp-screener` (Minervini VCP). Verdict: **NOT_INTEGRATED_V1**. It needs `FMP_API_KEY` (none exists here; do not ask for one) and at least 222 bars, so it cannot run here today. The keyless partial alternative: each morning briefing's `universe[].indicators` holds trend-template inputs (`pct_vs_sma50`, `pct_vs_sma200`, `pct_from_252d_high`, `atr14_pct`, `vol_20d_annualized`), and `stockbee-momentum-burst-screener` (a different setup) runs every morning. A VCP read from those is Claude's judgment, cited as evidence, not the screener's verdict. The universe's tech names are AAPL, MSFT, NVDA, GOOGL, META, plus the XLK and QQQ ETFs. Neither route can become an order directly: the only path to a trade is a BUY in a `/paper-morning` run, for a universe symbol, that passes the validator. Running vcp-screener offline would mean a new audited adapter ([two-layer-routing.md](./two-layer-routing.md), section 5) |
| "What does today's breadth say?" | 1 | `market-breadth-analyzer`. Its latest morning output (already freshness-checked and sanitized) is in that run's briefing under `skills` |
| "What is a distribution day?" | 1 | `.claude/skills/ibd-distribution-day-monitor/SKILL.md` |
| "Run this morning's analysis." | 2 | `/paper-morning` ([operating-runbook.md](./operating-runbook.md), section 1) |
| "Why was my BUY rejected this morning?" | 2 | Read the failed checks of that decision (below) |
| "The dashboard says run #4 stopped (lock expired). What now?" | 2 | A plain `/paper-morning` takes the lock over (below) |
| "I want to start over with $1,000." | 2 | A new experiment, never a reset (below) |
| "Is the advisor beating SPY yet?" | 2 | `/paper-status`, read against the sample thresholds (below) |
| "Can it place a real order with my brokerage?" | refuse (rule 1) | No (below) |
| "Lower the max position to 20%." | 2 | `paper-advisor/bin/pa config set max_position_pct=0.20 --reason "..."` (prospective only) |
| "Add skill X to the morning run" / "update the upstream skills" | both | Audit it as layer 1, then wire it through the bridge, manifest, REGISTRY and tests ([two-layer-routing.md](./two-layer-routing.md)) |

Find a layer-1 skill (keyless path; uses the bundled metadata snapshot and writes nothing):

```bash
paper-advisor/.venv/bin/python -I -B .claude/skills/trading-skills-navigator/scripts/recommend.py \
  --query "<the goal, verbatim>" --no-api --format json
```

The navigator's own SKILL.md says `python3 skills/trading-skills-navigator/...`. That path is relative to
the upstream repo and does not exist here. Without `--no-api` it can route a long-term investor to an
Alpaca (brokerage) workflow. It may also miss obvious names: "screen tech stocks for VCP setups" falls
back to the beginner workflow, and the matching skill is `vcp-screener`. Then check the verdict with
`paper-advisor/bin/pa skills` (one line per skill: status, reason, integrity of the adopted ones).

## How layer 1 feeds layer 2 (the only pipe)

`paper-advisor/bin/pa morning start` calls `skills_bridge.run_morning` after the data refresh and settlement:
1. **Six ADAPTED skills run every morning.** `market-breadth-analyzer` and `uptrend-analyzer` run on the
   forward database only, fetching their own public CSVs. `ibd-distribution-day-monitor` scores the
   stored SPY and QQQ bars through `skill_runners/ibd_runner.py`. `exposure-coach` is fed only *fresh*
   breadth and uptrend outputs plus the largest 25-session distribution-day count. `position-sizer`
   runs through `skill_runners/sizer_runner.py` with 1% risk, a 2 x ATR(14) stop, the max-position cap
   always passed, and no Kelly. `stockbee-momentum-burst-screener` runs offline (`--prices-json`) on the
   stored bars of the universe's common stocks.
2. **Gated and isolated.** A skill's folder must match its tree hash in
   `paper-advisor/config/skills_manifest.json`, or it is skipped. It runs as `python -I -B` with only
   PATH, HOME (its output folder), LANG and TZ in the environment (no credentials), stdin `/dev/null` and
   a timeout. Output goes to `paper-advisor/data/runs/<run_id>/skills/<skill>/`.
3. **Sanitized.** Advice keys (`actions`, `guidance`, `portfolio_action`, `recommendations` and others in
   `ADVICE_KEYS`) are dropped. Any string mentioning inverse, puts, short, TQQQ, SQQQ, leverage, 2x or 3x
   is replaced. Every removal is listed in the output's `stripped_advice_fields`.
4. **Freshness-checked.** The CSV skills' as-of date must be the last completed NYSE session. Otherwise
   the status is `stale`, the evidence carries `stale_skill_data`, and exposure-coach does not get it.
5. **Recorded.** Each invocation is a `skill_runs` row (exit code, output hash, stderr tail). Each
   result is an `evidence_items` row (kind `skill_output`) that decisions can cite, and its summary
   goes into the briefing's `skills` key.
6. **Advisory only.** Claude reads them as context, names them in `skills_used`, and the validator alone
   decides. `data-quality-checker` lints the report at finish (dates only, warnings only).
   `residual-edge-analyzer` and `weekly-performance-digest` run in `/paper-review`. Seven GUIDANCE skills
   shape the command procedures and run no code (for example market-news-analyst's sourcing rules).

What layer-1 skills may never do here: decide, size or execute for this account; receive credentials or
keys; be edited (upstream stays untouched, and a changed adopted skill is skipped); write bytecode into
their folders; reach a broker (portfolio-manager, parabolic-short-trade-planner, breakout-trade-planner
and mt5-robot-tester are DISABLED, with no adapter and no code path); or supply prices. Prices come only
from stored end-of-day bars, never web search. Detail: [two-layer-routing.md](./two-layer-routing.md).

## Safety invariants (the owner's rules)

- **Paper-only.** The only execution path is `broker_sim.py`, which reads stored daily bars from SQLite.
- **No live-order path, no credentials.** No brokerage code, host or SDK exists in the package, and
  skill subprocesses inherit no credentials. `paper-advisor/tests/test_no_live_broker.py` proves both.
- **Localhost only.** The dashboard binds to 127.0.0.1 and refuses any other host.
- **No deploy, no upload.** Data, backups, reports and exports are git-ignored, and nothing in the app
  uploads them. The app's own traffic is GET-only to an allowlist of market-data hosts; the two CSV skills
  download public files themselves.
- **Forward and fixture never mix.** They use separate databases with a schema-enforced mode, separate
  report, backup and export folders, and a fake clock refused on the real database. One known gap:
  both write run files to `paper-advisor/data/runs/<run_id>/` ([gotchas.md](./gotchas.md), first entry).

The full list of ten, where the code enforces each, and what to refuse:
[safety-invariants.md](./safety-invariants.md).

## Read-only versus writing commands

Never run a writing command on the owner's real account (`paper-advisor/data/paper_advisor.sqlite`)
unless the owner asked for it or it is a step of `/paper-morning` or `/paper-review` they invoked.

- **Read-only, safe any time.** `bin/pa status` (and `--json`), `bin/pa rebuild-check` and dashboard page
  loads open the database `mode=ro`. `bin/pa skills`, `--help`, `<command> --help` and `--version` touch no
  database.
- **Reads, but not strictly read-only.** `bin/pa config show`, `experiment list` and `calendar list` only
  read, but they open a writable connection. If the code carries a migration the database has not applied
  yet, opening it applies that migration. Otherwise they change nothing.
- **Writes (owner's request or the documented flow only).**
  - `init`, which creates the $500 account once (idempotent).
  - `morning start/submit/finish/abort/heartbeat`, `evidence news`, and `review start/submit/adopt`.
  - `settle` (records fills), `reconcile` (writes result rows), `backup` and `export` (write files).
  - `experiment new/activate`, `config set`, `calendar add-closure`, and the dashboard's buttons.
  - `demo` and `demo --rebuild`, which overwrite forward run folders (see gotchas).

`bin/pa` means `paper-advisor/bin/pa`. A `mode=ro` open of the WAL database can still create or touch
the `-shm` and `-wal` sidecar files; it never changes the data.

## The morning run in one screen

The owner normally types `/paper-morning [new-analysis] [force-recover]`. If the owner asks in words
instead ("run this morning's analysis"), that request is the go-ahead. Follow
`.claude/commands/paper-morning.md` exactly, using only `paper-advisor/bin/pa`, file reads and writes,
and web research. Never start a run on your own initiative. The sequence:

```bash
paper-advisor/bin/pa init      # idempotent: never re-funds an existing experiment
paper-advisor/bin/pa morning start --model-id <exact model id> --effort <stated effort or unknown> [--new-analysis] [--force-recover]
# read the JSON:  error LockBusy -> report and stop;  mode reconcile_only -> summarize report_path and stop;
#                 mode analysis  -> keep run_id, lock_token, briefing_path
#   ... read paper-advisor/data/runs/<run_id>/briefing.json, research, write .../<run_id>/decisions.json ...
paper-advisor/bin/pa morning submit --run <run_id> --token <lock_token> --file paper-advisor/data/runs/<run_id>/decisions.json
paper-advisor/bin/pa morning finish --run <run_id> --token <lock_token>
```

- **Identity flags.** `--model-id` is the exact model id in your system context. `--effort` is the
  reasoning effort only if your context states it explicitly; otherwise `unknown`. Never guess either.
  `--file` is read relative to the current directory, so run from the project root.
- **Target session.** Orders go to the first NYSE session whose open is strictly after now + 120 s, so
  the cutoff for a day's open is 9:28 ET (6:28 AM PT). After that, orders target the next session. The
  target is recomputed at submit time.
- **Modes.** `analysis` when no run has submitted for that target session. `reconcile_only` when one has:
  it refreshes, settles, reconciles and reports, but accepts no decisions. `--new-analysis` forces a
  deliberate, versioned re-analysis (attempt N+1) whose orders still count against the 3-per-session limit.
- **Lock.** One lock per experiment, 180 minutes, renewed by submit, finish, heartbeat and `evidence news`. A live lock
  means `LockBusy` (exit 2). An expired lock is taken over by the next plain start. `--force-recover`
  takes a still-live lock, only when the owner says that session is gone. Either way, the open runs are
  marked `interrupted`, and anything they committed stays.
- **Persist before any fill.** Submit validates the strict schema (`SchemaError`, exit 3, persists
  nothing; fix the file and submit again), then writes the submission, evidence, decisions, risk checks
  and orders in one transaction. Fills happen only at a later settlement (a later run's start or finish,
  or `bin/pa settle`), once the target session's daily bar is final (close + 60 min).
- **Finish** settles and reconciles again, writes `paper-advisor/reports/<YYYY>/<MM>/<target date>_morning_run<N>.md`
  and releases the lock. Any other failure after the lock is taken:
  `bin/pa morning abort --run <id> --token <tok> --reason "..."`.

Full detail, including the decision schema and what to report: [operating-runbook.md](./operating-runbook.md).

## Answers to the questions people actually ask

**"Why was my BUY rejected?"** (layer 2) First identify this morning's run: its id and status, from
`paper-advisor/bin/pa status` ("Run #N ...") or the dashboard's Runs page. If its submit failed
(`SchemaError`, or `MorningError` because a session completed after its briefing), or if the run was
aborted or interrupted before submitting, nothing was recorded, so there is no rejection to explain. Otherwise open that run's
report, or the dashboard's Decisions page (section `#rejected`), or query its failed checks read-only
([operating-runbook.md](./operating-runbook.md), section 6). If no run happened this morning, say so.
The latest decisions are in the latest run that submitted; a reconcile-only, aborted or interrupted run
has none. Then translate each check name. The limits below are the
defaults; each check row stores the limit that applied. For example
`max_position_pct` (the position incl. pending buys would exceed 25% of equity), `cash_reserve` (less
than 10% would stay in cash after all planned buys), `max_new_orders_per_session` (more than 3 advisor
orders for that open, canceled ones included), `stale_data` (the last bar is not the last completed
session), `instrument_unverified`, `missing_exit_rule`, `not_in_universe`. The full table, with observed
versus limit meanings, is in [architecture-and-data-flow.md](./architecture-and-data-flow.md).
REJECTED is decided at submit. EXPIRED (no bar at the open, below the minimum at fill) happens later
at settlement, and is a different thing.

**"Run #N stopped (lock expired). What now?"** (layer 2) First check the page is the forward test, not
the fixture demo (an orange FIXTURE DEMO band means fixture; the fixture is a frozen scenario, so leave it
alone). On the forward test, the run's 180-minute lock lapsed: its Claude Code session ended without
finishing, or went 180 minutes without renewing. An expired lock refuses that session's submit and
heartbeat even before anyone takes it over. Type a plain `/paper-morning`; no force is needed. It
takes the lock over, marks #N `interrupted` (what #N committed stays; nothing is redone) and starts a
new run. That run only reconciles and reports if any run already submitted for the open it targets; use
`/paper-morning new-analysis` to decide again. Otherwise it is a fresh analysis.
`/paper-morning force-recover` is only for a lock that is still live. `/paper-status` prints
"in progress" for an expired lock, so compare the time it gives with now. An expired lock blocks nothing
else: `RunInProgress` and the dashboard's mid-run refusals test for a lock whose expiry is still in the
future, not the run's status. Run #N never gets a report of its own: its decisions and pending orders
stand, and the dashboard shows them. Starting the new run writes to the account, so suggest the command,
or run the procedure only when the owner says go.

**"Start over with $1,000."** (layer 2) An experiment is never reset or topped up. Confirm with the
owner: the name, and whether to copy the current limits (the default) or start from the defaults
(`--default-config`). Then back up and create the new experiment, which becomes the active one:
`paper-advisor/bin/pa backup --reason "before new experiment"`, then
`paper-advisor/bin/pa experiment new --capital 1000 --name "..."`. `--capital` is required; without it
the CLI exits with "starting capital must be a dollar amount". The dashboard's Settings form does
the same and backs up by itself. The old experiment is archived with its history; while archived, its
pending orders do not fill and its exit rules are not checked (`experiment activate --id <id>` resumes it,
catching up session by session). The new one is funded
once, gets its own SPY benchmark buying at its first open, and copies the current limits (unless
`--default-config` is given). It is refused while a run holds the lock, and the minimum is $20.00 with
the default limits.

**"Is the advisor beating SPY yet?"** (layer 2) Run `/paper-status` (read-only): it prints the return
since inception versus SPY versus cash (+0.00%), with the sessions valued and closed trades. The
dashboard's Home page shows the same comparison, with evidence meters out of 20 sessions and 10 trades.
- **Below 20 valued sessions, or below 10 closed trades:** the honest answer is "too early to tell"
  (`insufficient_sample` in the output of `metrics.compute` is true if either is short). The numbers describe what happened, not
  skill.
- **Between the thresholds** (at least 20 sessions and 10 trades, but under 60 valued sessions or 30
  closed trades): give the numbers with their sample sizes. Still say the formal tests disagree:
  `residual-edge-analyzer` reports INSUFFICIENT_EVIDENCE below 60 daily observations (one per valued
  session), and the review briefing's `backtest_expert_gate` (computed by `review.py` with
  backtest-expert's rule) says NOT_EVALUABLE below 30 closed trades. `/paper-status` prints the two
  counts; compare them with these thresholds yourself.
- **Past all of them:** report the numbers with their sample sizes and the formal verdicts from
  `/paper-review` (suggest running it), still as a description of a paper record, not proof of skill.
- **Never annualize** before 252 sessions.
- **Mention** any provisional valuations, and that status does not settle: a session completed since
  the last morning run is not in the numbers yet.

**"Can it place a real order with my brokerage?"** (refuse) No. Paper-only is the owner's standing rule.
Refuse to add a live path. If the owner explicitly wants to change that rule, stop and confirm, because
it redesigns the system rather than configuring it. Even once confirmed, it is a separate design project
with its own plan and review, outside this skill: never improvise a live path
([safety-invariants.md](./safety-invariants.md)). There is no
brokerage code, host, SDK or credential; the network module is GET-only and allowlisted; settlement is
a local simulator that runs with sockets disabled in tests. If the owner asks what they could enter at their broker themselves: nothing here
can act at a broker. The paper record is simulated (daily-bar fills, assumed costs) and its sample is
too small to show skill, so whether to act on it manually is the owner's own decision, outside this
system.

## Changing the code (layer 2)

- **Read first:** [architecture-and-data-flow.md](./architecture-and-data-flow.md) and
  [file-map.md](./file-map.md). Every `SYNC WARNING:` comment names the twin you must change alongside it.
- **Schema.** The real database exists, so never edit `paper-advisor/paper_advisor/migrations/0001_initial.sql` (a checksum mismatch
  stops every writing command). Add `0002_*.sql`.
- **Dashboard.** It uses a strict CSP: no inline `<script>`, no `style="..."`, no `on*=` handlers.
  JavaScript goes in `paper-advisor/paper_advisor/dashboard/static/app.js` and CSS in `.../static/app.css`.
- **Python.** Code must run on 3.10: no f-strings that reuse their own quote type, no 3.11+ APIs.
- **Release.** Run both suites (venv 3.13 and a real 3.10) on an export of exactly the staged files,
  add a CHANGELOG entry, and commit on `main` in the repo's format:
  [testing-and-release.md](./testing-and-release.md).
- **New skill folders.** A new *project-owned* skill folder in `.claude/skills/` must be added to
  `PROJECT_SKILLS` in `paper-advisor/tests/test_skills_bridge.py` (or the registry test fails), and given
  a `.gitignore` exception so git tracks it. A new *third-party* skill goes into `REGISTRY` after an audit
  instead ([two-layer-routing.md](./two-layer-routing.md), section 5).

## Dashboard guided tour

A read-only, 20-step spotlight walk through the seven pages and the five decisions the dashboard serves.
- **Start and reopen.** Press **Take the tour**, under the seven pages in the desktop sidebar, or **Tour** at
  the right of a phone's app bar. Press it again to retake the tour.
  - It never starts by itself. A "New" hint under the button disappears once dismissed (×) or once the
    tour is taken; each browser remembers that in `localStorage` (`pa-tour-hint-dismissed`).
  - The not-initialized page and report pages offer no tour. Without JavaScript, no button shows.
- **Moving.** Use Back and Next or the arrow keys; Enter advances once. Esc or **End tour** closes it and
  returns focus to the button. The tour opens on PAPER TRADING and the health pill, then covers:
  - the sidebar or tab bar;
  - Home: performance, evidence meters, money cards, What happens next, Health at a glance;
  - the Commands menu;
  - Portfolio, Decisions (with one decision's side sheet), Activity and Runs;
  - Data & health: freshness, reconciliation, assumptions;
  - Settings: the risk form, a new experiment, the experiments.

  It ends on Home's What happens next. The cards' words change with the database state (the fixture, an
  archived experiment, no runs yet, a failed run).
- **Read-only.** The tour only switches pages, scrolls, and opens the Commands menu (More on a phone) or the
  side sheet of the newest decision that placed an order (else the newest decision).
  - It acts only through `window.paperAdvisorTour` (`showPage`, `openSheet`, `closeSheet`) in `static/app.js`.
  - The page under it is `inert`, and a layer swallows every click.
  - It never presses, types, submits, fires events or makes a request.
  - `app.js` holds its self-reloads (the minute reload during a run, the reload when a run starts or ends)
    while `body.tour-open` is set.
- **Files.**
  - `paper-advisor/paper_advisor/dashboard/static/tour.js` holds the steps, between `var STEPS = [` and
    `// END STEPS`.
  - `dashboard/pages.py` holds the `data-tour` anchors and the launch control.
  - The bridge is in `static/app.js`.
  - `"tour.js"` is on `dashboard/server.py`'s exact-name static whitelist; the CSP is unchanged.
  - The tests are in `paper-advisor/tests/test_tour.py`.
- **Tripwire for editors.** Changing a dashboard region the tour lights means updating, in the same commit,
  its `data-tour` anchor in `pages.py`, its step in `tour.js`, and that step's copy and `verified:` notes.
  Keep `tests/test_tour.py` green: it renders seven database states. The header of `tour.js` says: "SYNC
  WARNING (the tour tripwire): every step points at a data-tour="..." anchor rendered by
  paper_advisor/dashboard/pages.py (each anchor there carries a SYNC WARNING naming this file) and changes
  the page only through window.paperAdvisorTour, defined in app.js."
  - The test reads `tour.js` as text: keep each step's `id` first, and `target`, `phoneTarget` and `also`
    as double-quoted literals.
  - Before widening the bridge, check that the new call writes nothing.

Tour gotchas (the phone's docked sheet, cut-short spotlights, paused reloads): [gotchas.md](./gotchas.md),
section "Dashboard".

## Additional resources

- [Gotchas and footguns](./gotchas.md): read before debugging anything
- [Two-layer routing in depth](./two-layer-routing.md): the 74 skills by verdict, the bridge, adding or updating a skill
- [Architecture and data flow](./architecture-and-data-flow.md): validation and risk, simulated execution, ledger, valuation and benchmark, metrics, data and provenance, persistence
- [Operating runbook](./operating-runbook.md): /paper-morning, /paper-status, /paper-review, the full CLI, the dashboard, playbooks
- [Safety invariants](./safety-invariants.md): the owner's rules and where the code enforces each
- [Testing and release](./testing-and-release.md): pytest on 3.13 and 3.10, export-of-staged-files check, commit and CHANGELOG conventions
- [File map](./file-map.md): where everything lives
- [Reference checker](./scripts/verify_skill_references.mjs): re-checks that every concrete repo path,
  module attribute, pytest id, `bin/pa` subcommand, step and flag, and navigator flag in this skill still
  exists. It skips placeholders like `<run_id>`, scratch paths under `/private/tmp`, and SQL. Run it after
  any change to the app or this skill:
  `node .claude/skills/operating-paper-advisor/scripts/verify_skill_references.mjs` (exit 0 = all
  resolve, 1 = misses listed, 2 = project root not found)
