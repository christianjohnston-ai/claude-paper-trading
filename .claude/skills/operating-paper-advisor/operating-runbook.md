# Operating runbook

How to run, inspect and recover paper-advisor. All commands run from the project root unless they
start with `cd`. `bin/pa` means `paper-advisor/bin/pa`. Paths starting `paper-advisor/` or `.claude/`
are from the root. Never run a writing command on the owner's real account unless the owner asked for
it, or it is a step of a `/paper-*` command they invoked.

**Who starts a run.** The owner types `/paper-morning` (or `/paper-review`). When the owner asks in words
("run this morning's analysis"), that request is the go-ahead: follow the command file exactly, with the
same tools. Never start one on your own initiative.

## 1. `/paper-morning`: the morning run

Procedure: `.claude/commands/paper-morning.md`. Its allowed tools are `Bash(paper-advisor/bin/pa:*)`,
Read, Write, WebSearch and WebFetch; never a brokerage tool. The owner has authorized autonomous
SIMULATED orders within the rules each time they run it: do not ask for approval of individual paper
trades, and do not ask clarifying questions; report blockers instead. Arguments: `new-analysis` adds
`--new-analysis`; `force-recover` adds `--force-recover`.

**Step 1: start**

```bash
paper-advisor/bin/pa init
paper-advisor/bin/pa morning start --model-id <exact model id from your context> --effort <stated effort, else unknown> [--new-analysis] [--force-recover]
```

Never guess the model id or the effort. `--model-id` is the exact id in your system context. `--effort`
is the reasoning effort only if your context states it explicitly, otherwise `unknown`. Without the
flags both are recorded as `unknown`. Start can also fail with `MorningError` (exit 2): "no experiment
exists; run `bin/pa init` first", or a refusal of an injected clock on the real database. Start then:
1. Takes the lock, or fails with `LockBusy` (exit 2), or takes over an expired lock.
2. Marks earlier unfinished runs `interrupted`.
3. Backs up the database and prunes old backups.
4. Refreshes market data (forward only).
5. Settles every completed session and reconciles.
6. Fetches headlines and runs the adapted skills.
7. Writes `paper-advisor/data/runs/<run_id>/briefing.json`.

It prints JSON:

| Field | Meaning |
|---|---|
| `error: MorningError` (exit 2) | No experiment exists, or a fake clock on the forward database. Report it and stop |
| `error: LockBusy` (exit 2) | Another run holds a live lock. Report it and stop. Use `--force-recover` only if the owner says that session is gone |
| `mode: reconcile_only` | A run already submitted decisions for this target session. This rerun already settled, reconciled and wrote a report (`report_path`, `note`). Do not decide again. Summarize, mention `/paper-morning new-analysis`, and stop |
| `mode: analysis` | Keep `run_id`, `lock_token` and `briefing_path`, then continue |
| `orders_will_target_session` | The open that orders decided now will target |
| `settled` | Sessions processed, fills, expired orders, rule exits and corporate actions this run, plus `blocked_session` and `blocked_reason` |
| `reconciliation_failures`, `recovered_runs`, `supersedes_run_id`, `data_status`, `market_state`, `version` | As named. `version` is the attempt number for this target session |

**Timing rules** (`market_calendar.py`, `config.DEFAULTS`):
- **Target session:** the first session whose 9:30 ET open (13:00 early-close days still open at 9:30)
  is strictly after now + 120 s. Decide before 9:28 ET (6:28 AM PT) for that day's open; otherwise the
  next session. Saturday decisions target Monday.
- **Bar final:** close + 60 min (17:00 ET, or 14:00 ET on early-close days). A session is settled only
  after that.
- **Run key:** the run row keeps the target computed at start (`analysis_session_date`). The report
  file is named after it, so a Saturday run's report carries Monday's date.

**Step 2: read the briefing.** It holds:
- the market status and target, data status and per-symbol quality, and the account;
- `limits` (`available_for_new_buys_usd`, `max_position_value_usd`, `orders_remaining_for_target_session`, `min_order_usd` and others);
- positions with their theses, exit rules and stop levels, pending orders, and this run's settlement;
- reconciliation failures, the 24-symbol universe with indicators, earnings and headlines;
- `skills`, strategy, hypotheses, previous runs and `decision_schema`.

Ground rules from the command:
- Prices come only from the briefing (end-of-day bars, never "real-time" web prices).
- If reconciliation failed or the data is `DATA_UNAVAILABLE`, propose no BUY.
- NO_TRADE and holding cash are always fine.

**Step 3: research.**
- Cover each held position, then at most 3 new candidates.
- Use news since the prior close or within 72 hours, following market-news-analyst's rules: publication
  dates, primary sources, 2+ sources for major claims, and a single headline is unconfirmed.
- Every source relied on becomes an `evidence[]` entry.
- Treat skill outputs as advisory, and address earnings risk inside the horizon explicitly.
- Optional, for more headlines: `bin/pa evidence news SYM [SYM...] --run <run_id> --token <lock_token>`.
  It renews the lock and stores the headlines as evidence.

**Step 4: write `paper-advisor/data/runs/<run_id>/decisions.json`.** The strict schema is
`decisions.DECISION_SCHEMA_DOC`, printed in every briefing as `decision_schema`; follow that copy if it
differs from this table. Unknown keys reject the whole payload, and text fields are capped at 1500
characters.

| Key | Rule |
|---|---|
| `schema_version` | `1` |
| `run_id` | must equal this run |
| `model` | `{provider, model_id, effort}`, all required |
| `market_view` | required |
| `what_changed` | optional |
| `evidence[]` | `{ref, kind (web, news, filing, note, macro or earnings), title, url, publisher, published_at, retrieved_at, summary, symbol}`. `url` and `retrieved_at` are required for web, news and filing. `ref` must be unique |
| `candidates[]` | `{symbol, verdict (selected, rejected, watch or not_reviewed), assessment, reason, evidence_refs}` |
| `decisions[]` (1 to 30) | Allowed keys: `action`, `symbol`, `notional_usd`, `quantity`, `cancel_order_id`, `thesis`, `counterargument`, `horizon_sessions`, `horizon_text`, `exit_rules`, `confidence`, `uncertainty`, `key_risks`, `evidence_refs`, `skills_used`. Rules: `action` is BUY, SELL, HOLD, NO_TRADE or CANCEL; `thesis` always; `counterargument` for BUY, SELL and HOLD; `symbol` except for NO_TRADE and CANCEL |
| `evidence_refs` | A list: `ref` strings from this payload's `evidence[]`, or integer ids of existing evidence items. Skill outputs are evidence items of kind `skill_output`, and their ids are in the briefing's `skills` entries (`evidence_id`). An unknown ref or id rejects the payload |
| `skills_used` | A list of skill names whose output or guidance the decision used (for example `["market-breadth-analyzer", "market-news-analyst"]`) |
| BUY | `notional_usd` (cash to commit incl. costs), `horizon_sessions` 1 to 1260, `confidence` 0 to 1, `uncertainty`, `exit_rules{stop_loss_pct, take_profit_pct, max_holding_sessions, invalidation}` with `invalidation` required (plus at least one rule while `require_exit_rule_on_buy` is on) |
| SELL | `quantity`: `"ALL"` or shares with at most 6 decimals |
| CANCEL | `cancel_order_id` (only a PENDING advisor order, before its open) |
| HOLD | may restate `exit_rules` prospectively (tighten only) |

Doing nothing is one NO_TRADE whose thesis says why cash is better today.

`--file` is read relative to the current directory: run from the project root, as above.

**Step 5: submit and finish.**

```bash
paper-advisor/bin/pa morning submit --run <run_id> --token <lock_token> --file paper-advisor/data/runs/<run_id>/decisions.json
paper-advisor/bin/pa morning finish --run <run_id> --token <lock_token>
```

- **Submit output:** `target_session`, plus one outcome per decision (`ACCEPTED`, `REJECTED` with the
  failed check names, or `RECORDED` for NO_TRADE), `order_id`.
- **`SchemaError` (exit 3):** nothing was persisted. Fix the file and submit again; it is the same run.
- **One submission per run:** after a successful submit, the run no longer accepts decisions.
- **`MorningError` (exit 2)** "a market session (...) completed after this run's briefing was built":
  start a new run.
- **Any other failure after the lock is taken:**
  `bin/pa morning abort --run <run_id> --token <lock_token> --reason "<why>"` (status `failed`, lock
  released).
- **Research longer than an hour** (the command's rule; the lock lasts 180 min):
  `bin/pa morning heartbeat --run <run_id> --token <lock_token>`.
- **Finish** settles and reconciles again, writes the report, runs the date lint, marks the run
  `completed` and releases the lock. It refuses unless the run is `decisions_submitted`.

**Step 6: report to the owner.** Lead with **PAPER TRADING (simulated)**. Then give:
- the market state and the target open, and that fills are recorded only after that session closes (on a later run);
- what was settled now;
- each decision with ACCEPTED or REJECTED and the failed checks, plus its thesis and invalidation in one line;
- equity vs keeping cash vs SPY, with the sample-size caveat;
- data, skill or reconciliation problems;
- the report path and `paper-advisor/bin/pa dashboard` (http://127.0.0.1:8765/).

Never call results profitable or skillful before fills and later valuations exist.

**Run statuses:** `started`, then `awaiting_decisions`, then `decisions_submitted`, then `completed`.
Other outcomes are `failed` (an error or an abort) and `interrupted` (recovered by a later start). A
reconcile-only rerun goes from `started` to `completed`. Reports are written only by the finish step,
which a reconcile-only start runs itself. An interrupted run never gets one. Its decisions and orders stand, and the dashboard shows them.

## 2. `/paper-status`: read-only snapshot

Runs exactly `paper-advisor/bin/pa status` (`--json` for machine output). It opens the database read-only, never
refreshes, settles or trades, and takes no lock. It prints:
- the experiment, the market state in words and the open that orders decided now would target;
- equity, cash, positions, unrealized and realized P&L (at stored closes, not live quotes);
- the return since inception vs the benchmark vs keeping cash, with sessions valued, closed trades and
  provisional valuations;
- data freshness, the SQLite version, positions, pending orders (and which the next morning run will
  settle), any held lock and the last 3 runs.

If it says "not initialized", the owner runs `/paper-morning`, which inits once, or `bin/pa init`.

## 3. `/paper-review`: honest evaluation and prospective hypotheses

Procedure: `.claude/commands/paper-review.md` (allowed tools: `Bash(paper-advisor/bin/pa:*)`, Read,
Write).

```bash
paper-advisor/bin/pa review start --model-id <id> --effort <effort or unknown>   # writes paper-advisor/data/runs/<run_id>/review_briefing.json
# evaluate from the briefing only, then write paper-advisor/data/runs/<run_id>/review.json
paper-advisor/bin/pa review submit --run <run_id> --file paper-advisor/data/runs/<run_id>/review.json
```

- **What start does:** it creates a review run (no lock), computes metrics and decision outcomes, and
  runs residual-edge-analyzer and weekly-performance-digest.
- **The submit payload:** `{schema_version 1, run_id, model, observations (required, at most 3000
  chars), hypotheses[]}`. Each hypothesis has `title`, `hypothesis`, `rationale`, `falsification_test`
  (all required), `minimum_sample`, `proposed_change{type: none, config or strategy; config_overrides
  (editable keys only); strategy_text}` and `evidence_refs`.
- **What submit stores:** the hypotheses as `proposed`, with an `earliest_effective_session` (the next
  session after now + 5 min), and the review report. An invalid payload exits 3 with nothing persisted.
- **Always submit.** The `observations` are required; only the hypotheses are optional. Submitting is
  what records the evaluation, writes the review report and completes the run.
- **Limits:** the command allows at most 3 hypotheses (the code does not enforce that). Never adopt one
  yourself. The owner adopts with `bin/pa review adopt --hypothesis <id> --reason "<why>"`, which
  creates a new config or strategy version effective from that moment.
- **Finish in one session:** the next `bin/pa morning start` marks any unfinished review run
  `interrupted`, and its submit then fails ("not an open review").

## 4. The `bin/pa` CLI (every subcommand, from `cli.py`)

Global flags: `--fixture` (use `data/fixture_demo.sqlite`; put it before the subcommand) and `--version`.
`--app-dir DIR` is hidden; it points the app at another data folder, which QA copies use.

| Command | Flags | Writes? | Exit codes and notes |
|---|---|---|---|
| `init` | - | yes, once | Creates the database and the $500 experiment only if none exists; prints the experiment |
| `morning start` | `--model-id`, `--effort`, `--provider` (default `anthropic/claude-code`), `--new-analysis`, `--force-recover`, `--no-news`, `--no-skills` | yes | 0, or 2 (`LockBusy` or `MorningError` as JSON) |
| `morning submit` | `--run`, `--token`, `--file` | yes | 0, 2, or 3 (`SchemaError`, nothing persisted) |
| `morning finish` | `--run`, `--token` | yes | 0 or 2 |
| `morning abort` | `--run`, `--token`, `--reason` | yes | 2 if the token does not hold the lock |
| `morning heartbeat` | `--run`, `--token` | yes (renews the lock) | 2 if the lock was lost |
| `evidence news SYM...` | `--run`, `--token` (both required) | yes | Extra headlines into an open run |
| `status` | `--json` | no | 1 if not initialized |
| `review start` | `--model-id`, `--effort`, `--provider` | yes | Review briefing |
| `review submit` | `--run`, `--file` | yes | 3 on an invalid payload |
| `review adopt` | `--hypothesis`, `--reason` | yes | Owner only |
| `settle` | - | yes (fills) | Settles and reconciles outside a run; refused (2) while a run holds the lock |
| `reconcile` | - | yes (result rows) | 4 if any check failed |
| `backup` | `--reason` | yes (a file plus a row) | Consistent online backup with an integrity check |
| `export` | - | yes (files) | CSV, metrics and manifest zip under `exports/` |
| `experiment list` / `new` / `activate` | `new`: `--capital` (required: without it the CLI exits "starting capital must be a dollar amount"), `--name`, `--notes`, `--default-config`; `activate`: `--id` | `new`/`activate` yes; `list` reads through a writable connection | `new` archives the active experiment and makes the new one active. Refused during a live run (`RunInProgress`) or below the capital floor (`CapitalTooSmall`), with the reason on stderr and exit 1 |
| `config show` / `set k=v [k=v...]` | `--reason` (required for `set`) | `set` yes; `show` reads through a writable connection | New prospective version; editable keys only |
| `calendar list` / `add-closure` | `--date YYYY-MM-DD`, `--reason` | `add-closure` yes; `list` reads through a writable connection | Records an unscheduled NYSE closure |
| `skills` | - | no | Registry with integrity for the adopted skills |
| `dashboard` | `--port` (default 8765), `--fixture` | GET no, POST yes | Serves http://127.0.0.1:PORT/ |
| `demo` | `--rebuild` | yes | Builds the fixture database. See the gotchas before running it |
| `rebuild-check` | - | no | Holdings rebuilt from the ledger, both accounts |

`PAPER_ADVISOR_FAKE_NOW=<ISO time>` is honored only together with `--fixture` on a fixture database.
`PAPER_ADVISOR_SEC_USER_AGENT` only unlocks the SEC hosts in the fetcher; no feature uses them in v1.

## 5. The dashboard

- **Launch:** `paper-advisor/bin/pa dashboard`, then http://127.0.0.1:8765/. Use `--port 8766` if the
  port is busy (it prints that hint); stop with Ctrl+C. Starting or stopping never resets data.
- **Fixture:** `paper-advisor/bin/pa dashboard --fixture`, or `/?db=fixture` on any running dashboard.
  Other experiments: `/?exp=<id>`.
- **Localhost only:** `server.serve` refuses any host but 127.0.0.1.
  - Requests whose `Host` is not `127.0.0.1:PORT` or `localhost:PORT` get 403 (DNS-rebinding guard).
  - POSTs need the per-process CSRF token and a same-origin Origin or Referer.
  - It sends a strict CSP, `X-Frame-Options: DENY` and no-store.
- **Never deploy it or bind it elsewhere.**
- **GET routes:** `/` (the app), `/report?run=N` (a run's report as a labelled snapshot),
  `/api/state` (run state, polled every 15 s by open forward tabs, which reload when a run starts or
  ends), `/healthz`, `/static/app.css`, `/static/app.js`, `/static/tour.js` and `/download/export/<zip>`.
- **POST actions** (all on the Settings page): risk settings (`/actions/config`), a new experiment
  (`/actions/new-experiment`, backs up first), make active (`/actions/activate`), backup and export.
  - Config, new-experiment and activate are refused while a run holds a live lock.
  - The fixture's settings are read-only.
  - There is no "Run morning" button: the Commands menu lists the slash commands to copy.

The seven pages (sidebar on desktop; Home, Portfolio, Decisions, Runs and More tabs on a phone):

| Page | For | Sections (anchor ids) |
|---|---|---|
| Home | Is it adding value vs cash and the benchmark, and is the evidence meaningful yet? What happens next? | Performance vs benchmark and cash; evidence meters (valued sessions of 20, closed trades of 10); equity and drawdown charts; cash, invested, total P&L and drawdown cards that open their rows; recent activity; What happens next (the cutoff, what the next `/paper-morning` will do, recovery commands); Health at a glance |
| Portfolio | What it holds, and what fills at the next open | `#positions`, `#pending` |
| Decisions | Why each decision was made and what became of it | `#all-decisions`, `#journal` (advisor journal by run), `#rejected` (rejected proposals, each failed check in words) |
| Activity | Every fill, round trip, split and dividend | `#trades` |
| Runs | Every morning run and review: status, why it stopped, build, model, report | `#runs` |
| Data & health | Can the record be trusted? | `#health`: per-symbol data, reconciliation, provisional valuations, calendar, assumptions, the SQLite advisory |
| Settings | Prospective limits and experiments | `#settings`: risk settings, new experiment, experiments, export and backup, and the history of settings, strategy and hypotheses |

Row clicks open a drawer with the full record (thesis, counterargument, evidence with timestamps, every
check with observed vs limit, the order lifecycle). **Take the tour** (sidebar), or **Tour** in the phone's
app bar, starts a read-only 20-step walk through all of it ([SKILL.md](./SKILL.md), "Dashboard guided
tour"). The top bar always says PAPER TRADING and carries
one health pill naming the worst current condition. Fixture pages carry an orange FIXTURE DEMO band and
are judged against the scenario's frozen clock.

## 6. Playbooks

**A BUY was rejected.** Find out why, read-only:
1. **Identify the run.** `paper-advisor/bin/pa status` lists the last 3 runs ("Run #N: morning
   analysis for the ... open: completed"); the dashboard's Runs page shows them all. Nothing was
   recorded, so there is no rejection to explain, if the run's submit failed (`SchemaError`, or
   `MorningError` because a session completed after the briefing), or if it was aborted or
   interrupted before submitting. Say so. If no run happened this morning, say that too.
2. **Read the report.** `paper-advisor/reports/<YYYY>/<MM>/<target date>_morning_run<N>.md` lists each
   decision as ACCEPTED or REJECTED, with the failed checks in words. Or use the dashboard's Decisions
   page, `#rejected`.
3. **For the observed vs limit values,** query that run's failed checks with the app's own read-only
   connection (set `RUN` to the run id):

```bash
RUN=<run id> paper-advisor/.venv/bin/python -B -c "
import os, sys; sys.path.insert(0, 'paper-advisor')
from paper_advisor import db
c = db.connect('paper-advisor/data/paper_advisor.sqlite', readonly=True)
q = '''SELECT d.seq, d.action, d.symbol, c.check_name, c.observed, c.limit_value, c.message
       FROM decisions d JOIN risk_checks c ON c.decision_id = d.id
       WHERE c.passed = 0 AND d.run_id = ? ORDER BY d.seq'''
for r in c.execute(q, (int(os.environ['RUN']),)): print(dict(r))
q2 = '''SELECT o.id, o.side, o.symbol, o.status, o.status_reason, o.target_session_date
        FROM orders o WHERE o.run_id = ? ORDER BY o.id'''
for r in c.execute(q2, (int(os.environ['RUN']),)): print(dict(r))
"
```

The first query lists the failed checks; no rows means nothing failed in that run. The second lists the
run's orders with their status and `status_reason`. That covers ACCEPTED-then-EXPIRED orders too
(`no_bar_for_target_session`, `below_minimum_at_fill` and so on). Run numbers are per database, so the
fixture's run 1 is not the forward account's run 1.
Interpret each check name with the table in [architecture-and-data-flow.md](./architecture-and-data-flow.md),
section 3. Explain the fix in the owner's terms: a smaller notional, wait for fresh data, an exit rule
was missing, the per-open order limit was used up. Do not change limits to make a rejected trade pass
unless the owner decides to (`config set` is prospective and needs a reason).

**A run shows "stopped (lock expired)" or is stuck.** First make sure the view is the forward test, not
the fixture demo (orange band), which is a frozen scenario to leave alone.
- The lock has expired: suggest a plain `/paper-morning`, and run the procedure only on the owner's go.
  It takes over and marks the old run `interrupted`. An expired lock already refuses the old session's
  submit and heartbeat.
- The lock is still live, but the owner says the session is gone: run `/paper-morning force-recover`.
- What the new run then does: if any run already submitted for the open it targets, it is
  reconcile-only (`/paper-morning new-analysis` re-decides deliberately). Otherwise it is a fresh
  analysis.
- Do not try to `abort` someone else's run: abort needs that run's token.

**A run failed.** Its lock was released. Read its error (dashboard Runs, or `bin/pa status`). The next
`/paper-morning` follows the same reconcile-only-or-analysis rule.

**Start over, or use different capital.** Confirm with the owner, then run
`paper-advisor/bin/pa backup --reason "before new experiment"` and
`paper-advisor/bin/pa experiment new --capital <amount> [--name "..."] [--default-config]`.
- What happens:
  - The old experiment is archived with its history.
  - The new one is funded once and gets its own benchmark buy at its first open.
  - It is refused during a run or below the capital floor.
- **Switching back:** `bin/pa experiment activate --id <id>`. The reactivated experiment catches up
  session by session.
- **No top-up:** there is no way to add money to an existing experiment, by design.

**Change a limit.** `paper-advisor/bin/pa config set max_position_pct=0.20 --reason "<why>"`. Use the
editable keys only, and do it when no run is in progress: the CLI does not check the lock, though the
dashboard does. It applies to decisions from now on.

**Unscheduled market closure** (a national day of mourning, for example):
`paper-advisor/bin/pa calendar add-closure --date YYYY-MM-DD --reason "..."`. Until it is recorded, an
order for that day stays pending because no bar exists, which is the safe failure.

**Data problems.** `data_status` PARTIAL means the stale symbols cannot be bought; DATA_UNAVAILABLE means
no BUY at all. Sells still work. A later run's refresh clears it. Provisional valuations name their
reason, and most clear when later data arrives; check Data & health.

**Backups, exports and restore.**
- Backups and exports: `bin/pa backup`, `bin/pa export`, or the dashboard's Settings tools.
- Restoring replaces the record, so it is the owner's call only:
  1. Stop every process using the database: the dashboard (8765 and any QA ports) and any running
     session.
  2. Take a fresh `bin/pa backup` of the current state.
  3. Move aside `data/paper_advisor.sqlite-wal` and `-shm` if they exist.
  4. Copy the chosen backup over `data/paper_advisor.sqlite`.
  5. Run `bin/pa reconcile`.
