# Architecture and data flow

How paper-advisor works inside, layer by layer, as the code does it (the code wins over any doc).

Path convention (all files of this skill): `paper-advisor/...` and `.claude/...` are from the project root
(the folder you cloned); `tests/`, `config/`, `data/`, `reports/`, `backups/`, `exports/` and `bin/` are inside
`paper-advisor/`; module paths such as `broker_sim.py`, `dashboard/server.py`, `data_sources/http.py`,
`skill_runners/` and `migrations/0001_initial.sql` are inside `paper-advisor/paper_advisor/`.

## 1. Modules by layer

| Layer | Modules (`paper_advisor/`) | Job |
|---|---|---|
| Entry | `paper-advisor/bin/pa`, `paper-advisor/pa.py`, `cli.py` | CLI. `bin/pa` uses `.venv/bin/python` if present, else `python3`. The app's own paths (database, data, reports) resolve from the app folder, never the cwd; file arguments such as `--file` resolve from the cwd |
| Ingestion and provenance | `data_sources/http.py`, `yahoo.py`, `nasdaq.py`, `news.py`, `ingest.py`, `prices.py`, `corporate_actions.py`, `universe.py`, `analytics.py` | GET-only allowlisted fetches, as-traded bars, identity checks, earnings, headlines |
| Skills (layer 1 pipe) | `skills_bridge.py`, `skill_runners/` | Audited adapters ([two-layer-routing.md](./two-layer-routing.md)) |
| Research and decision interface | `briefing.py`, `morning.py`, `decisions.py`, `review.py`, `runs.py` | The run lifecycle, the briefing JSON, the strict decision schema, the lock |
| Validation and risk | `submission.py`, `quality.py`, `rules.py`, `orders.py`, `config.py` | The deterministic gate |
| Simulated execution and accounting | `broker_sim.py`, `ledger.py`, `accounting.py` | Next-open fills, the append-only ledger, FIFO lots |
| Evaluation and output | `valuation.py`, `metrics.py`, `reconcile.py`, `reports.py`, `status.py`, `exporter.py`, `dashboard/` | Snapshots, metrics, checks, reports, UI |
| Foundations | `market_calendar.py`, `money.py`, `clock.py`, `db.py`, `migrations/0001_initial.sql`, `paths.py`, `provenance.py`, `experiments.py` | Calendar, integer money, the time source, SQLite, layout, code identity |

The app is stdlib-only (`requirements-dev.txt` pins only pytest; `requirements-skills.txt` serves the
skill subprocesses).

## 2. One morning, end to end

```
/paper-morning (Claude) -> bin/pa init -> bin/pa morning start
   recover runs + take lock -> backup + prune -> ingest.refresh (forward only) -> broker_sim.settle
   -> reconcile.run_checks -> headlines (forward only) -> skills_bridge.run_morning -> briefing.json
Claude reads briefing.json, researches (WebSearch/WebFetch), writes decisions.json
bin/pa morning submit  -> decisions.validate (schema) -> submission.apply_decisions_tx (risk gate, one transaction)
bin/pa morning finish  -> settle again -> reconcile again -> report (+ data-quality lint) -> release lock
next morning start     -> settle: fills at the target open from its final daily bar -> exit rules -> valuations
```

## 3. Validation and risk (`submission.py`, `orders.py`, `rules.py`, `quality.py`)

- **The batch.** Decisions are processed CANCEL, then SELL, then HOLD and NO_TRADE, then BUY (risk-reducing
  first), and returned in submission order. Every decision, risk check (passed or failed, with observed
  and limit) and order is written in ONE transaction (`apply_decisions_tx`).
- **Equity reference:** cash + the sum of held quantity x last stored close. A position without a close
  counts at cost.
- **Limits are cumulative** across the batch and everything already pending. Pending SELL proceeds never
  count as cash, and a pending exit does not free a position slot.
- **Order lifecycle** (trigger `orders_legal_transition`): PROPOSED to REJECTED, or PROPOSED to PENDING;
  then PENDING to FILLED, CANCELED or EXPIRED. Order terms are immutable (trigger `orders_core_immutable`).
  A PENDING BUY reserves its whole notional.
- **Data quality** (`quality.assess`): FRESH means the latest bar is the last completed session with no
  flags; STALE is older; SUSPECT carries flags; MISSING has no bar. Buys need FRESH; sells are allowed on
  any data.
- **Exit rules** (`rules.py`) are evaluated only on completed closes. A breach queues a SELL ALL for the
  next open; no intraday stop fill is ever simulated. Lifecycle: AWAITING_ENTRY, ACTIVE, TRIGGERED,
  CLOSED, plus SUPERSEDED. An exit that expires unfilled re-arms the rule. A stop may be tightened, never
  widened or removed, and adding to a position carries the old stop level forward as a floor.
- **Exit-rule ranges** (`_parse_exit_rules`): stop `0 < x <= max_stop_loss_pct` (0.50 by default),
  take-profit `0 < x <= 10`, max holding 1 to 1260 sessions.
- **Exit-rule requirement.** For a BUY, the schema requires an `exit_rules` object with `invalidation`
  text (a `SchemaError` otherwise). The validator also requires at least one of stop, target or holding
  limit while `require_exit_rule_on_buy` is true (a `missing_exit_rule` rejection). While that setting
  is on, a HOLD that restates rules also needs a mechanical rule and invalidation text
  (`_hold_restatement_checks`).

Every check name the validator writes to `risk_checks` (the dashboard and reports show these in words):

| Check | Fails when | `observed` / `limit` |
|---|---|---|
| `invalid_amount` | BUY `notional_usd` is not a positive amount with at most 2 decimals | - |
| `not_in_universe` | the symbol is not in the 24-symbol research universe | symbol / "research universe" |
| `instrument_unverified` | no VERIFIED identity result within 14 days | why / "VERIFIED within 14 days" |
| `asset_type_not_allowed` | not COMMON_STOCK or ETF at 1x | type and leverage |
| `missing_data`, `stale_data`, `suspect_data` | the latest bar is absent, older than the last completed session, or flagged | last bar date / expected session |
| `missing_exit_rule` | no stop, target or holding limit, or no invalidation text | "rule" or "none" |
| `invalid_exit_rule` | a rule value is outside the ranges above | the problems |
| `duplicate_symbol_in_batch` | a second decision for the same symbol in one batch | symbol |
| `conflicting_pending_exit` | BUY while an exit for that symbol is pending | symbol |
| `stop_loosening_not_allowed` | an add-on or HOLD would widen or drop the stop | new stop / "<= current" |
| `max_new_orders_per_session` | advisor orders for the target open, canceled ones included and rejected ones not, would exceed 3 (exit-rule and benchmark orders do not count) | count / max |
| `min_order` | the buy (minus commission, if any) is under $5, or a partial sell is worth under $5 | dollars / dollars |
| `max_position_pct` | held + pending + batch + this buy > 25% of the equity reference | dollars / cap dollars |
| `max_open_positions` | held + pending-buy + batch symbols > 5 | count / max |
| `insufficient_cash` | cash minus all reservations cannot cover the buy | cash available / notional |
| `cash_reserve` | cash after all planned buys < 10% of the equity reference | dollars left / reserve dollars |
| `no_position` | SELL or HOLD of a symbol not held (long-only) | shares / "> 0" |
| `exit_already_pending` | SELL, or a HOLD restating rules, while an exit is pending or triggered | symbol |
| `exceeds_available_quantity` | SELL more than held minus pending sells | shares / shares |
| `entry_pending_for_symbol` | a HOLD restatement while an add-on entry is pending | count / 0 |
| `unknown_order`, `order_not_cancellable`, `target_session_already_open` | a CANCEL of a foreign, non-PENDING or already-open order | - |
| `action_allowed` | an unknown action (NO_TRADE records it passed) | - |
| `fresh_data`, `data_quality_for_sell` | informational passes | - |

Defaults above come from `config.DEFAULTS` and can differ per config version (`bin/pa config show`).

## 4. Simulated execution (`broker_sim.py`)

- **When it runs:** `settle()` runs at morning start, at morning finish and in `bin/pa settle`. It walks
  every completed session (close + `bar_finalization_minutes` = 60) after `app_state`
  `settled_through:<exp>`. The first one is the experiment's first investable session. Each session is
  its own transaction, so a crash leaves a session fully applied or untouched.
- **Per session,** in this order:
  1. splits, then cash dividends, for positions held at the prior close (PORTFOLIO and BENCHMARK alike);
  2. fills at the open, SELLs before BUYs, oldest first;
  3. exit rules on the close;
  4. valuation snapshots for both accounts.
- **Fill rule:** market-on-open of the order's target session, from that session's first-observed final
  daily bar. BUY price = open x (1 + 10 bps), rounded up to 6 decimals (`money.PRICE_QUANT`; stored bars
  have 4). SELL price = open x (1 - 10 bps), rounded down. A BUY's quantity is
  floor((notional - commission) / price) at 6 decimals, so the cash
  spent never exceeds the reservation. SELL ALL sells what is held at the fill. Gaps are realized: the
  open is used, never a rule level.
- **Time proof:** `fills` CHECKs enforce `decided_at < sim_exec_at < bar_retrieved_at <= recorded_at`.
  A fill is therefore always recorded on a later run than the open it simulates, and reconciliation
  re-checks it (`fill_timing`).
- **Expiry and cancel reasons:**
  - `missed_target_session`;
  - `no_bar_for_target_session` (a halt or gap while others traded);
  - `session_data_unavailable_timeout` (no data for a whole session within `session_data_wait_days` = 5;
    until then the session blocks settlement, and the reason is `session_data_unavailable`);
  - `below_minimum_at_fill`;
  - `no_position_at_fill`;
  - CANCELED `insufficient_cash_at_fill` (a guard that should never fire).
- **Benchmark exception:** a BENCHMARK buy that cannot fill is re-queued for the next open with the
  original decision time (`_requeue_benchmark`, client id `bench:<exp>:retry:<session>`), so the
  benchmark never sits in cash for good.
- **Scope:** only the active experiment is settled. An archived experiment's orders and rules pause, and
  on reactivation it catches up session by session (the bars must still be downloadable).

## 5. Ledger and accounting (`ledger.py`, `accounting.py`, `reconcile.py`)

- **The ledger:** `ledger_events` is the only source of cash and shares, one sequence per experiment and
  account (`PORTFOLIO` = the advisor, `BENCHMARK` = the passive shadow account). Event types:
  `INITIAL_FUNDING`, `BUY_FILL`, `SELL_FILL`, `CASH_DIVIDEND`, `SPLIT`, `REVERSAL`, `ADJUSTMENT`.
- **Append-only:** triggers reject UPDATE and DELETE. Corrections are explicit: `REVERSAL` (only of
  `CASH_DIVIDEND` or `ADJUSTMENT`, one per event) or a cash-only `ADJUSTMENT` with a memo.
- **Exactly once:** a unique index allows one `INITIAL_FUNDING` per experiment and account; every event
  has an idempotency key, and reusing a key with a different payload is an error.
- **Guards at append time:** cash can never go negative (no margin) and quantities can never go
  negative (no shorts).
- **Holdings are never stored.** `accounting.replay` rebuilds cash, FIFO lots, realized P&L, dividends,
  fees and round trips from the events every time (`bin/pa rebuild-check` prints the result).
  - A buy's basis is its gross cost plus commission. Realized P&L = proceeds - commission - FIFO basis.
  - Splits multiply lot quantities and keep the basis. Dividends are cash income, credited on the
    ex-date to holders at the prior close.
- **Integers only:** cents and micro-shares (`money.py`). Rounding never favors the account.
- **Reconciliation** (`reconcile.run_checks`, at morning start and again at finish, or with
  `bin/pa reconcile`) runs these checks:
  - `ledger_replay:PORTFOLIO` and `:BENCHMARK`, `funding_exactly_once`, `fills_match_ledger`,
    `orders_match_fills`, `fill_timing`, `reservations_covered_by_cash`;
  - `snapshot_consistency:<account>`, which includes the identity
    `equity - funded = realized + unrealized + dividends + adjustments`.

  The results go to `reconciliation_results`.

## 6. Valuation and the SPY benchmark (`valuation.py`, `experiments.py`)

- **Snapshots:** one per session per account (`valuation_snapshots`, immutable; a correction would be a
  new revision). Marks are the session's as-traded close from the first-observed bar.
- **PROVISIONAL** snapshots carry machine-readable reasons:
  - `stale_mark:SYM`: an earlier close was used;
  - `no_mark:SYM`: valued at cost;
  - `corporate_actions_unverified:SYM`: splits and dividends were not checked through that session;
  - `data_quality:SYM`;
  - `calendar_unverified`: a year outside the official 2025-2027 NYSE table;
  - `no_data_for_session`.
- **The benchmark:** creating an experiment funds BOTH accounts with the same capital and places one
  BENCHMARK BUY for all of it, at the first open after inception (`benchmark_start_session`). The
  advisor cannot trade before that open either. The benchmark symbol is the config key
  `benchmark_symbol`: SPY, or `DEMO.SPY` in the fixture. It gets the same slippage, commission and dividend treatment (cash on
  the ex-date, not reinvested) and never trades again.

## 7. Metrics (`metrics.py`, `review.py`)

- `metrics.compute` gives:
  - total return = equity / starting capital - 1 (also time-weighted, because deposits are disabled);
  - the SPY return, `excess_vs_spy` (over the sessions both accounts have valued), the cash return (0);
  - max and current drawdown, exposure, turnover;
  - win rate, average win and loss, and profit factor (`None` without a losing trade);
  - the count of provisional sessions.
- **Thresholds before statistics mean anything:**
  - `MIN_SESSIONS_FOR_STATS = 20` and `MIN_TRADES_FOR_STATS = 10`: below either, `insufficient_sample`
    is true and the sample note says to treat every number as a description, not an edge;
  - `ANNUALIZE_AFTER_SESSIONS = 252`: nothing is annualized before that.
- **`/paper-review` adds:**
  - residual-edge-analyzer, which reports INSUFFICIENT_EVIDENCE below 60 daily observations (one per
    session both accounts have valued);
  - `backtest_expert_gate`, computed by `review.py` with backtest-expert's rule: NOT_EVALUABLE below
    30 closed trades;
  - confidence calibration buckets (high at 0.65 and above, medium 0.5 to 0.65, low below 0.5), which
    say nothing below 20 decisions per bucket.

## 8. Data and provenance (`data_sources/`, `ingest.py`, `universe.py`)

Every network request in the app's own code goes through `data_sources/http.Fetcher`:
- GET only, HTTPS only, and the host must be in `ALLOWED_HOSTS`. Redirects are re-checked, there are at
  most 3 hops, and a response is capped at 8 MiB.
- Every response is cached as `data/evidence/<YYYY/MM/DD>/<sha256>.<ext>.gz` and recorded in
  `source_snapshots` with its request and retrieval times.
- Only the URL (tickers, dates) and a User-Agent are sent.

| What (forward database only) | From | When |
|---|---|---|
| Daily bars, splits and dividends (as-traded: Yahoo's split back-adjustment is undone; in-progress bars are dropped; non-session and revised bars are flagged) | `query1.finance.yahoo.com` chart v8 | each morning start, from the last stored bar minus 15 days (420 days for a new symbol) |
| Identity check (listed, not a test issue, exchange, ETF flag, issuer keywords) | `www.nasdaqtrader.com` symbol directories, plus Yahoo metadata | when any symbol's verification is older than 20 hours. A BUY needs one within 14 days |
| `^VIX` and `^TNX` context (not tradeable) | Yahoo chart | each morning start |
| Earnings dates for the next 10 sessions (may be estimates; a failed date is UNKNOWN) | `api.nasdaq.com` | each morning start |
| Headlines (unverified leads) | `feeds.finance.yahoo.com` RSS | each morning start, for held and pending symbols, earnings-window symbols and the 5 largest 5-day movers. Extra with `bin/pa evidence news SYM... --run N --token T` |
| Breadth and uptrend CSVs | `tradermonty.github.io` / `raw.githubusercontent.com`, fetched by the two skill subprocesses themselves, not through the Fetcher | each morning start |

`data_status`: OK when every universe symbol is FRESH; PARTIAL when some are not (stale, suspect or
missing; those cannot be bought); DATA_UNAVAILABLE when the benchmark is not fresh, or more than half
of the symbols are not (the command then proposes no BUY). The fixture database never fetches anything.

**What reaches the model service (Anthropic):** the briefing JSON that Claude reads (account,
positions, prices, headlines, skill outputs, limits), plus whatever Claude searches or fetches on the
web. The database, reports, exports and backups never leave the machine, and no credentials or personal
data are in the briefing.

The universe (`universe.FORWARD_UNIVERSE`, version 2026-10-24.v1, 24 symbols) has 10 ETFs (SPY, QQQ,
IWM, DIA, XLK, XLF, XLE, XLV, XLP, TLT) and 14 stocks (AAPL, MSFT, NVDA, AMZN, GOOGL, META, BRK.B, JPM,
XOM, UNH, JNJ, PG, COST, LLY). Leveraged products are impossible (schema `CHECK (leverage = '1x')`).

## 9. Persistence (`db.py`, migrations, experiments, versions)

- **Databases:**
  - `data/paper_advisor.sqlite` is the real forward experiment. `data/fixture_demo.sqlite` is the demo.
  - Each database's mode (`forward` or `fixture`) is fixed in `app_state` on creation; re-opening with
    another mode is refused, and a trigger refuses an experiment whose mode differs from the database's.
  - Settings: WAL, `synchronous=FULL`, foreign keys, a 10 s busy timeout, and every writer uses
    `BEGIN IMMEDIATE` (`db.transaction`, never nested).
- **Migrations:** `migrations/0001_initial.sql` is the only one. Applied migrations are checksummed, so
  editing one raises `MigrationError` on the next writing open. Schema changes need a new
  `0002_*.sql`.
- **Append-only tables:** ledger events, fills, decisions, run submissions, price bars, source snapshots,
  config and strategy versions, valuation snapshots, order events, risk checks and evidence. Orders and
  experiments are never deleted.
- **Backups:** `db.backup` uses the SQLite online-backup API plus `PRAGMA integrity_check`.
  - Files: `backups/paper_advisor-forward-<stamp>.sqlite`, or `backups/fixture/paper_advisor-fixture-<stamp>.sqlite`.
  - Taken at every morning start (and the newest 60 per mode are kept), before the dashboard creates an
    experiment, and by `bin/pa backup` or the dashboard button.
- **Exports:** `bin/pa export` writes `exports/export_<forward|fixture>_<stamp>/` (26 tables as CSV,
  `metrics.json`, `MANIFEST.json` with sha256s) and a zip of it. Text cells that start with a formula
  trigger get a leading quote.
- **Experiments and capital:**
  - `init` creates the default $500 experiment only if none exists.
  - A new capital means a NEW experiment (`bin/pa experiment new --capital N`). The old one is archived,
    never reset. Capital, mode, inception and code identity are immutable, and experiments are never
    deleted.
  - Funding happens exactly once. Deposits and withdrawals are disabled (`config.normalize` refuses
    them), so an experiment is never topped up.
  - Minimum capital: the larger of $10 and the smallest account that can place a buy under its limits
    ($20.00 with the defaults).
- **Config versions are prospective:** `bin/pa config set k=v --reason "..."`, or the dashboard, creates
  version N+1, effective now. Earlier decisions keep the version they were validated under.
  - Editable keys: `min_order_usd`, `max_position_pct`, `min_cash_reserve_pct`, `max_open_positions`,
    `max_new_orders_per_session`, `commission_per_order_usd`, `commission_bps`, `slippage_bps`,
    `require_exit_rule_on_buy`, `max_stop_loss_pct`.
  - Everything else (benchmark, timezones, `lock_ttl_minutes` 180, `backup_keep` 60 and others) is fixed
    for the experiment's life. Neither the CLI nor the dashboard can set it: a new experiment copies the
    current values or, with `--default-config`, takes the defaults.
- **Strategy versions:** change only by adopting a review hypothesis (`bin/pa review adopt`). Hypotheses
  get an `earliest_effective_session` after submission, so a change can never reach back in time.
- **Code identity:** every run and experiment records `code_version` (`1.0.0+<git HEAD, 12 chars>`) and
  `code_tree_sha256`, immutable by trigger.
  - The tree hash covers `paper_advisor/` and `config/` (`.py .sql .css .js .json`) plus the three
    `.claude/commands/paper-*.md` files.
  - The dashboard numbers each distinct tree hash as "build N", so tests, docs and skills do not change
    the build; any commit changes `code_version`.
