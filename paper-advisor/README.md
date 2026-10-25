# paper-advisor — a local, PAPER-ONLY trading advisor with an honest record

**Everything here is simulated with fake money.** There is no brokerage connection, no brokerage
credential handling, and no code path that can place a real order (see "Safety" below).

Each morning you type `/paper-morning` in Claude Code. The app reconciles a simulated $500 account,
refreshes end-of-day market data, settles earlier simulated orders, runs a set of audited trading
skills, and hands Claude a briefing. Claude researches, then proposes BUY / SELL / HOLD / NO_TRADE
decisions with evidence, a thesis, a counterargument, a horizon, a size, an uncertainty statement
and precommitted exit rules. Deterministic code validates every proposal, records it **before**
anything can execute, and fills accepted orders only at the open of a later session, using that
session's official daily bar. The local dashboard shows whether any of it beat keeping cash or
buying SPY.

---

## Quick start

From the project root (the folder you cloned; to install it, see the README.md there):

```bash
paper-advisor/bin/pa init          # creates the database and the $500 experiment ONCE (idempotent)
paper-advisor/bin/pa status        # read-only snapshot
paper-advisor/bin/pa dashboard     # http://127.0.0.1:8765/  (Ctrl+C to stop)
paper-advisor/bin/pa demo          # isolated synthetic fixture demo -> view with: bin/pa dashboard --fixture
```

In Claude Code (project-local commands in `.claude/commands/`):

| Command | What it does |
|---|---|
| `/paper-morning` | Recover/lock, back up, refresh data, settle, reconcile, run skills, research, decide, validate, reconcile again, report |
| `/paper-morning new-analysis` | A deliberate, versioned re-analysis of a session that already has one |
| `/paper-status` | Read-only state: never refreshes, settles or trades |
| `/paper-review` | Honest evaluation + dated, prospective hypotheses (never rewrites history) |

Requirements: macOS/Linux with Python ≥ 3.10. The app itself is **stdlib-only**. A project venv
(`paper-advisor/.venv`, created with `uv`) holds pytest and the pinned runtime dependencies of the
audited skills (`requirements-dev.txt`, `requirements-skills.txt`):

```bash
cd paper-advisor && uv venv .venv --python python3.13
uv pip install --python .venv/bin/python -r requirements-dev.txt -r requirements-skills.txt
.venv/bin/python -m pytest          # full test suite
```

## Your morning

1. Open Claude Code in the project root and type `/paper-morning`.
2. Read the summary: what was settled (fills recorded now for opens that happened earlier), each
   decision with ACCEPTED/REJECTED and why, and the account vs cash vs SPY.
3. Optional: `paper-advisor/bin/pa dashboard` and open http://127.0.0.1:8765/.

If a run is stuck because the Claude Code session running it has ended (the dashboard says so and shows
how long ago the lock was renewed), type `/paper-morning force-recover` in a new session: it takes over
the lock and marks the old run interrupted. Once its lock has expired (the dashboard then shows "stopped (lock
expired)"), no force is needed: the next `/paper-morning` takes it over by itself. Like any run for an open that
already has submitted decisions, it then only reconciles and reports; `/paper-morning new-analysis` re-analyzes
deliberately.

Running it twice for the same session is safe: the second run is **reconcile-only** (it settles,
values and reports, but cannot decide again). Missing days is safe: the next run settles every
completed session in order, using the bars of those sessions, and labels those events as recorded
later. It never invents decisions for mornings you missed.

## When do fills happen? (execution policy)

The only price data is end-of-day daily bars, so:

- **Order type:** market-on-open (MOO) for the **first regular session whose open is strictly after
  the decision was committed** (plus a 120-second safety buffer for the commit itself).
  - Decide on Saturday → fills at Monday's 9:30 ET open.
  - Decide Monday at 6:15 AM PT (9:15 ET, before the open) → fills at Monday's open.
  - Decide Monday at 7:00 AM PT (10:00 ET, after the open) → fills at **Tuesday's** open.
- **Fill price:** that session's official open from its daily bar, with adverse slippage
  (default 10 bps per side: buys at open × 1.001 rounded up, sells at open × 0.999 rounded down).
  Gaps are realized as they happened.
- **When it is recorded:** a daily bar exists only after its session closed (we wait 60 minutes for
  it to be final), so a fill is always recorded on a later run than the open it simulates. Every
  fill stores, separately: decision time, simulated execution time (the open), the bar's retrieval
  time and the recording time (database-enforced: decided < executed < retrieved ≤ recorded).
- **Expiry:** an MOO order is good for its target session only. If the symbol has no bar that day
  while the rest of the market does (halt, delisting, data gap) it **expires**. If no data at all
  arrives for a completed session (feed outage), orders stay **pending** for up to 5 days, then
  expire. The passive benchmark's buy is the one exception: its rule ("invest everything at the first
  open after inception") was committed at inception, so if that open cannot fill it, it moves to the
  next open with the same decision time instead of leaving the benchmark in cash for good.
- **Exit rules** (stop-loss, take-profit, max holding sessions) are evaluated on **completed daily
  closes**. A breach queues a SELL ALL for the next open. We never claim an intraday stop fill:
  a daily bar cannot tell whether the low came before the high, and a stop price is not a
  guaranteed fill. Stops can be tightened later, never widened or removed; adding to a position
  carries the old stop level forward as a floor.
- **Non-trading days:** the NYSE calendar (official 2025–2027 holidays and 1 p.m. early closes from
  nyse.com; rule-derived and flagged unverified for other years). Unscheduled closures can be added
  with `bin/pa calendar add-closure --date YYYY-MM-DD --reason "..."`.
- **No continuous monitoring.** Nothing runs while your computer sleeps. Pending orders and exit
  rules are reconciled on your next invocation using historical bars, labeled as recorded later.

## Default experiment configuration

| Setting | Default | Notes |
|---|---|---|
| Starting equity | $500.00 | credited once (database-enforced) |
| Instruments | long-only US-listed stocks and unleveraged ETFs | no margin, shorts, options, leveraged/inverse products, crypto |
| Fractional shares | 6 decimal places (micro-shares) | buy quantity rounds down; cost rounds up to the cent |
| Minimum order | $5 | applies to what buys shares: with a commission configured, the order minus its commission (a smaller fill would expire at the open) |
| Max position | 25% of equity | includes pending buys and the new order |
| Cash reserve after planned buys | 10% of equity | pending sell proceeds are not counted |
| Max open positions | 5 | a pending exit does not free a slot until it fills |
| Max new advisor orders per session | 3 | canceled orders count; exit-rule and benchmark orders do not |
| Commission | $0 | **simulation assumption**, not verified broker pricing |
| Slippage | 10 bps adverse per side | **simulation assumption** |
| Timezones | display America/Los_Angeles; sessions America/New_York; storage UTC | |

Change limits with `bin/pa config set max_position_pct=0.20 --reason "..."` or the dashboard's
Settings form. Every change is a new version effective from that moment; earlier decisions keep the
limits they were validated under.

## Research universe (UNIVERSE_VERSION 2026-10-24.v1)

ETFs: SPY (also the benchmark), QQQ, IWM, DIA, XLK, XLF, XLE, XLV, XLP, TLT.
Stocks: AAPL, MSFT, NVDA, AMZN, GOOGL (Class A), META, BRK.B (Class B; Yahoo `BRK-B`), JPM, XOM,
UNH, JNJ, PG, COST, LLY.

Identity is verified (at most every 20 hours) against the official Nasdaq Trader symbol directories
(listed, not a test issue, expected exchange, ETF flag, issuer keywords, no leverage keywords) and
Yahoo metadata (instrument type, USD). A BUY requires a verification from the last 14 days.
Leveraged/inverse products cannot be added (schema CHECK). Changing the universe materially should
be a new experiment.

## Data sources and what leaves your computer

| Destination | What is sent | Why |
|---|---|---|
| query1/query2.finance.yahoo.com | ticker + date range | daily bars, dividends, splits, metadata (unofficial, keyless) |
| www.nasdaqtrader.com | nothing but the file request | official symbol directories |
| api.nasdaq.com | a date | earnings calendar (dates may be estimated) |
| feeds.finance.yahoo.com | ticker | headline RSS |
| tradermonty.github.io, raw.githubusercontent.com | nothing but the file request | public breadth/uptrend CSVs fetched by two audited skills inside their own subprocess |
| www.sec.gov, data.sec.gov | only if you set `PAPER_ADVISOR_SEC_USER_AGENT` | disabled by default; SEC requires a contact, and the app will not invent one or use yours |
| **Anthropic (the model service)** | **the briefing Claude reads** (account, positions, prices, headlines, skill outputs) and anything Claude searches for | Claude Code runs the model remotely; using Claude does not mean inference is local |

All network access in the app goes through one GET-only fetcher with this allowlist (redirects are
re-checked). Every response is cached under `data/evidence/` (gzip, named by SHA-256) and recorded
with request and retrieval times. The database, reports, exports and backups never leave the machine.
No credentials are stored or logged.

## Prices, splits and dividends

- Bars are stored **as traded**. Yahoo back-adjusts history for later splits (NVDA's 2024-06-07 close
  comes back as 120.888; $1,208.88 traded), so we multiply by the cumulative later-split ratio, and
  always request through "now" so every such split is visible. Dividend-adjusted closes are never
  used for accounting.
- Splits are ledger events at the ex-date open (shares × ratio, basis unchanged; pending sell
  quantities and rule price levels adjust too). Cash dividends are credited on the ex-date to
  holders of record at the prior close. The SPY benchmark gets identical treatment, so nothing is
  double counted. (Simplification: pay dates are not available keylessly, so dividend cash is
  spendable a few weeks early; amounts are cents on a $500 account.)
- If corporate actions were not checked through a valuation date, or a mark is stale or missing,
  that day's valuation is **PROVISIONAL** with machine-readable reasons, shown on the dashboard.

## Accounting

- Append-only ledger (database triggers reject UPDATE/DELETE); holdings, FIFO tax lots, cash and P&L
  are always rebuilt by replaying it. Corrections are explicit REVERSAL/ADJUSTMENT events.
- Integers: cash in cents, quantities in micro-shares. Rounding never favors the account.
- Reconciliation runs every morning: ledger replay, funding exactly once, fills ↔ ledger ↔ orders,
  fill timing, reservations ≤ cash, snapshot consistency, and the identity
  `equity − funded = realized + unrealized + dividends + adjustments`.

**Hand-calculated example** (also `tests/test_ledger_accounting.py`):

| Step | Calculation | Result |
|---|---|---|
| Start | | cash $500.00 |
| BUY $100 at a $50.00 open | fill 50.00 × 1.001 = 50.05; qty floor₆(100 / 50.05) = 1.998001; cost ceil(99.99995005) | $100.00; cash $400.00 |
| Close at $52.00 | 1.998001 × 52 = 103.896052 | value $103.90; unrealized +$3.90; equity $503.90 |
| SELL ALL at a $51.00 open | fill 51 × 0.999 = 50.949; proceeds floor(101.796152949) | $101.79; realized +$1.79 |
| End | | cash $501.79, return +0.358% |

## Measuring whether it helped

The dashboard and `/paper-review` show: starting capital, cash, invested value, equity; realized
and unrealized P&L; return since inception net of modeled costs; daily equity, drawdown, exposure
and turnover; closed-trade win rate, average win/loss and profit factor (shown with their sample size,
"n/a" without losing trades); and every decision with its fill dates, prices, evidence and reasons.

Comparisons: (1) keeping $500 in cash (0%); (2) a passive SPY account funded with the same $500 that
buys at the **same first eligible open** as the advisor could (the first session after inception),
with the same slippage, commission and dividend conventions (cash on ex-date, not reinvested).
Deposits/withdrawals are disabled, so total return is also the time-weighted return. Nothing is
annualized before 252 sessions, and every metric carries an insufficient-sample warning until there
are at least 20 sessions and 10 closed trades. `residual-edge-analyzer` reports INSUFFICIENT_EVIDENCE
below 60 daily observations, which is the honest result early on.

The primary experiment is **forward paper trading**. Historical replay is not implemented in v1;
if added, it must be a separate mode with separate records (and an LLM's prior knowledge can
contaminate retrospective tests).

## Reviews and strategy changes

`/paper-review` evaluates results and may store up to three dated, falsifiable hypotheses with a
minimum sample and an earliest effective session strictly in the future. Nothing changes until you
run `bin/pa review adopt --hypothesis <id> --reason "..."`, which creates a new config or strategy
version effective from that moment only. Past decisions, orders, fills and valuations are
append-only and cannot be revised.

## New experiment / different capital

Changing starting capital creates a **new experiment**; the current one is archived, never reset:

```bash
paper-advisor/bin/pa experiment new --capital 1000 --name "Forward paper experiment ($1,000)"
paper-advisor/bin/pa experiment list
paper-advisor/bin/pa experiment activate --id 1
```

(or the dashboard's "Start a new experiment" form). The new experiment is funded once and gets its
own SPY benchmark starting at its own first eligible open. While a morning run is in progress, creating
an experiment or making another one active is refused (it would move the run's decisions to an
experiment that is no longer active); do it after the run finishes.

The smallest starting capital is the larger of $10.00 and the smallest account that could place any
buy under the limits the experiment starts with: the smallest buy that can fill (the minimum order, plus
commission if one is configured) must fit within the max position and leave the cash reserve. With the
defaults that is $5 / 25% = **$20.00**; below it an experiment could never trade while its benchmark
invests. The CLI, the dashboard and `experiments.create_experiment` all refuse less, and the dashboard
checks the amount while you type it. Likewise, the risk settings form warns (while typing, and again
when saved) if new limits would leave this account too small to place any buy.

## Backups and exports

- An automatic consistent backup (SQLite online backup API + integrity check) is taken at the start
  of every morning run into `paper-advisor/backups/` (newest 60 kept). Manual: `bin/pa backup` or the
  dashboard button.
- `bin/pa export` (or the dashboard) writes CSVs of every table plus metrics and a SHA-256 manifest
  to `paper-advisor/exports/` and a zip. Text cells that could be spreadsheet formulas are neutralized.
- Restoring: stop the dashboard, copy a backup file over `data/paper_advisor.sqlite`.
- The fixture demo's backups, exports and reports go to `fixture/` subfolders (`backups/fixture/`,
  `exports/fixture/`, `reports/fixture/`), with "fixture" in each file name and "FIXTURE DEMO" in each
  report's title, so a file found on disk is never taken for the forward test's; pruning old backups
  only ever counts and deletes the forward test's own.

## Dashboard

`paper-advisor/bin/pa dashboard [--port 8765] [--fixture]` serves http://127.0.0.1:8765/ (localhost
only, never deployed). It is laid out like a Stripe dashboard: a sidebar of seven pages (Home, Portfolio,
Decisions, Activity, Runs, Data & health, Settings) with the experiment switcher at its top, and a top bar
that always says PAPER TRADING and carries one health pill naming the worst current condition in words (a
run in progress, a stopped or failed run, a failed reconciliation, stale data or provisional valuations, or
"All checks pass"), which opens the run or Data & health. On a phone the pages become bottom tabs (Home,
Portfolio, Decisions, Runs, and More for the rest, the fixture switch and the slash commands), and a sticky
PAPER TRADING strip names any abnormal state.

Home answers the three questions first: equity with the return since inception for the advisor, the
benchmark and keeping cash on one baseline and the gap in points; how far the evidence is from meaning
anything (valued sessions of 20 and closed trades of 10, the thresholds below which win rate and profit
factor describe nothing), with the full caveat under Learn more; the equity and drawdown charts; then cash,
invested, total P&L and max drawdown as cards that each open their rows, and recent runs and fills. Beside
them, "What happens next" (the submission cutoff for the next open, what the next `/paper-morning` will
actually do, when the next analysis can run once the coming open is decided, pending orders, and the
recovery step for a failed or stuck run, with the command to copy) and "Health at a glance". Portfolio
holds positions (value, P&L, weight and exit rules first, with how far each last close is from its stop)
and pending orders; Decisions an index of every decision, the advisor journal run by run, and rejected
proposals with each failed check worded as the failure; Activity the fills, closed round trips with why
each exited, and splits and dividends; Runs the run history (each run's status with the reason a stopped run
stopped under it, its kind, start, code build, tools and report; what every run shares, such as the open it targets
and the advisor model, is said once above the table, and becomes a column when runs differ; a report opens as a
labelled snapshot of its run);
Data & health the per-symbol data, reconciliation, provisional valuations, calendar and assumptions;
Settings the risk settings, a new experiment, the experiments, export and backup, and the history of
settings, strategy and hypotheses. Every link to a section opens the page that holds it.
Each distinct code tree is "build N" in order of first use, and the results line says
when an experiment mixes builds or risk-settings versions. While a run is in progress, times are shown
as clock times, relative ones stay current, and the page reloads itself every minute (not while a
detail panel or an expanded section is open, or a field has focus), so a stuck run is visible on a tab
left open. An open tab also checks every 15 seconds whether a run has started or ended and reloads when
one has, so a tab opened before a run never offers Save as if nothing had changed (if a field has focus,
Save, Create and Make active turn off at once with the reason). Entries typed in the risk and
new-experiment forms are kept across these reloads, restored with a note saying why and a Discard
button; a save refused because a run was in progress keeps its entries until the run ends, when Save
works again. Every headline figure drills down: Cash to each movement with its running balance, Total P&L
to its realized, unrealized and dividend parts, Invested to the positions, Return and Drawdown to each
valued session. Pending orders and runs open drawers too (a run's log in words, with its raw
identifiers in a collapsed provenance block, and for a running or failed run what to do next with
the command to copy), and Settings ends with the history of risk-settings versions (what changed and the
reason given), strategy versions, and the dated hypotheses from `/paper-review`, each opening its
falsification test, minimum sample and proposed change. Click or tap any row for its full record (thesis, counterargument, evidence with
timestamps, every check with its observed value and limit, the order lifecycle); a fill links to the
decision behind it. Controls for risk settings (a typed fraction is echoed as a percentage, and the line
above Save says exactly which settings would change, from what to what), a new experiment and Make
active (both say what archiving the active experiment does before you press), export and backup all
work; the Experiments table shows each experiment's equity, return and gap; a refused value is explained next to its field with your
input kept and the cursor placed there, and a completed action confirms itself where you clicked.
There is no "Run morning" button: the dashboard shows each slash command with what it is for. Fixture
data carries an orange FIXTURE DEMO band (Stripe's test-data treatment; orange means nothing else on the
dashboard), an orange PAPER TRADING · FIXTURE DEMO marker (also inside every detail drawer) and its own
synthetic benchmark ticker, and can never be mixed into the forward paper
test; the fixture view is judged against its own frozen clock (the scenario's last run), not today's
date, so its freshness and identity checks are meaningful.

**Guided tour.** Press **Take the tour**, under the seven pages in the sidebar (on a phone, **Tour** at the right of
the app bar), for a spotlight walk through every page and the five decisions the dashboard serves. It never starts by
itself: a small "New" hint under the button goes away once dismissed or once the tour is taken (remembered in that
browser). It only looks: it switches pages, scrolls, and opens the Commands menu (More on a phone) and the newest
decision's side sheet; it never presses, types or submits anything and makes no request of its own. The arrow keys or
Back and Next move, Esc or **End tour** closes, and it ends on Home's What happens next, where each trading morning starts. Press
the same button to take it again. The not-initialized page offers no tour. Tripwire: each step points at a `data-tour`
anchor in `paper_advisor/dashboard/pages.py` and the tour lives in `paper_advisor/dashboard/static/tour.js`, so a change to
a page the tour describes updates its step, anchor and `verified:` note in the same commit; `tests/test_tour.py` fails
when an anchor is missing in any database state, an anchor has no step, or the tour gains a call that could change
something.

## Architecture

```
paper-advisor/
  bin/pa, pa.py                CLI entry (resolves paths from its own location)
  paper_advisor/
    data_sources/              1. ingestion + provenance (GET-only allowlisted fetcher, Yahoo, Nasdaq, RSS)
    ingest.py, analytics.py
    skills_bridge.py           2. audited skill adapters (isolated subprocesses) + registry
    skill_runners/
    briefing.py, morning.py,   2. Claude's research/decision layer interface (briefing -> decisions JSON)
    decisions.py, review.py
    submission.py, quality.py  3. deterministic validation and portfolio-risk checks
    broker_sim.py, orders.py,  4. simulated execution and accounting (append-only ledger, FIFO lots)
    ledger.py, accounting.py,
    rules.py, corporate_actions.py
    valuation.py, metrics.py,  5. evaluation, reports, dashboard
    reports.py, status.py, exporter.py, dashboard/
    market_calendar.py, money.py, clock.py, db.py, migrations/
  data/paper_advisor.sqlite    the real experiment (git-ignored)
  data/fixture_demo.sqlite     the isolated demo (git-ignored)
  data/evidence/, data/runs/   cached sources; briefings and decision files per run
  reports/, backups/, exports/ (git-ignored)
  SKILL_AUDIT.md               what each installed skill really does and how it is used
```

Model selection is replaceable: the decision layer is a JSON contract (`decisions.DECISION_SCHEMA_DOC`),
and each run records the model provider, exact model ID and effort level reported by the session
(or "unknown", never guessed). Each run and each experiment also records the code that produced it:
`code_version` ("1.0.0+" and the git HEAD commit, read from `.git` without running git) and
`code_tree_sha256`, a hash of the exact package, config and `/paper-*` procedure files that ran, which
also catches uncommitted edits. These identity columns cannot be edited afterwards (schema triggers).

## Safety: why this cannot trade real money

- No brokerage SDK, host, endpoint or credential exists in the package; `tests/test_no_live_broker.py`
  scans for them, requires that only the GET-only allowlisted fetcher can open connections, runs the
  whole submission + settlement path with sockets disabled, and checks that skill subprocesses
  inherit no credentials.
- The four skills with brokerage contact (portfolio-manager, parabolic-short-trade-planner,
  breakout-trade-planner, mt5-robot-tester) are disabled, have no adapter, and are never invoked.
- The `/paper-*` commands only allow `paper-advisor/bin/pa`, reading/writing files and web research.

## Tests

`cd paper-advisor && .venv/bin/python -m pytest` runs the suite: money/rounding, the NYSE calendar
against the official table, append-only and exactly-once database guarantees, ledger/lots/P&L with
the hand-calculated example, the batch risk gate (overspending, aggregate exposure, reservations,
order limits, precision, sells, stale/suspect data, universe, exit rules), the simulated broker
(fill timing, decisions after the open, costs, holidays/early closes, gaps, expiry, missing data,
splits, dividends, rules, missed days, benchmark, provisional marks), ingestion (split un-adjustment,
partial bars, revisions, identity, allowlist, redirects, DTDs), the morning lifecycle (duplicate
runs, two-process concurrency, crash recovery, exactly-once submission, stale briefings, fake clocks),
the skill bridge, the dashboard's HTTP security and in-place error handling, the isolated
multi-session end-to-end fixture scenario with persistence after restart, and the no-broker proofs.

## Known limitations

- Daily bars only: no intraday precision, exits react one session after a close-confirmed breach.
- Yahoo's chart endpoint is unofficial and may change or rate-limit; failures degrade to PARTIAL /
  DATA_UNAVAILABLE with buys blocked, never to invented data.
- Earnings dates come from Nasdaq's calendar and may be estimates; failed dates are reported UNKNOWN.
- Dividends are credited on the ex-date (pay dates unavailable); splits rely on Yahoo's events.
- No filings-based signals unless you enable SEC with your own contact User-Agent.
- Most skills in the installed library need paid API keys and are disabled (SKILL_AUDIT.md).
- The sample will be tiny for months. Treat results as a description of what happened, not proof of skill.
- Settlement runs only for the active experiment. While an experiment is archived its pending orders do
  not fill and its positions are neither valued nor checked against their exit rules. If it is made
  active again, the next morning run catches it up session by session from where it stopped, exactly
  like missed days: pending orders fill at their original target opens (they were decided before
  them), exit rules apply to each missed close, and every missed session gets its valuation (tested in
  tests/test_experiments_config.py). Prices for those sessions must still be available to download.
- The record lives in SQLite, so it inherits the SQLite library your Python links (`/paper-status` and the
  dashboard's Data and health name the version). SQLite before 3.51.3 (except the 3.50.7 and 3.44.6
  backports) has the WAL-reset bug: if two connections write or checkpoint at the same instant, a WAL
  database can be corrupted. SQLite rates it unlikely; every backup is integrity-checked, and upgrading
  removes it (Homebrew Python: `brew upgrade sqlite`). SQLite 3.51.0-3.51.1 can also deadlock when threads
  of one process open and close connections at once; the dashboard's threads do that, so paper-advisor
  serializes opening and closing within a process (tests/test_db.py reproduces the hang without it).
