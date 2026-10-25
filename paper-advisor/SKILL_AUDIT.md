# SKILL_AUDIT.md — what the installed trading skills really do, and how paper-advisor uses them

Audit date: 2026-10-24. Auditor: Claude Code session (claude-opus-5-5) with seven parallel read-only
code audits, lead spot-checks of the material claims, and full test runs. Paths below are relative
to `.claude/skills/` in the project root unless stated.

## 1. Provenance

| Item | Value |
|---|---|
| Installed library | `tradermonty/claude-trading-skills`, 74 skills in `.claude/skills/` (git-ignored, local only) |
| Exact version | commit **cb0f3aadffed6d1089db4b224162c964368baa46** (2026-10-19 HEAD) |
| How verified | cloned upstream and compared git blob hashes: **1141 of 1141 installed files byte-identical** to that commit |
| Integrity at runtime | the 9 adopted skills are tree-hashed in `config/skills_manifest.json`; an adapter refuses to run a skill whose files changed since this audit |
| Original repo discussed | `Jakeschincariol/trader-skill`, commit **5189e79b9a480755a3733ac9713f5353981dbe18** (single commit, v1.0, 11 skills). **Not installed. Not adopted.** Read for reference only (section 6) |

The two projects are different: the installed library is large and mostly depends on paid APIs (FMP,
Finviz Elite); the original is small, keyless (SEC EDGAR, House Clerk, Yahoo) and includes a skill
that can send orders you approve to a real brokerage (paper by default). Nothing from the original was silently substituted.

## 2. Method

1. **Static scan** of all 74 skills (SKILL.md, scripts, configs) for credentials, hosts, imports and
   order-placement code (no code executed).
2. **Tests**: every skill's own suite run in isolation (one pytest process per skill) inside a
   dedicated venv built from upstream `uv.lock`, with a scrubbed environment (no tokens or keys).
   **Result: 74/74 suites pass, 8,306 tests.** Passing tests prove the code matches its authors'
   expectations (mostly offline, mocked); they are **not** evidence of data correctness or of an
   investment edge, and several suites pin known-wrong behavior (noted below).
3. **Code audit**: seven reviewers read the code by domain (broker/sizing/risk; filings/fundamentals;
   regime/breadth; technical screeners; backtest/edge/review; prompt-only/meta; the original repo),
   each finding cited to file:line. The lead re-read the highest-impact claims in source
   (portfolio-manager live-host selection, position-sizer caps, drawdown fail-open, 13F quarter
   selection, the original repo's broker configuration, episodic-pivot bar fallback): all
   confirmed.
4. **Live checks**: the adopted keyless skills were run against real data on 2026-10-24
   (breadth 42.5 "Neutral" and uptrend 51.3 "Neutral", both as of 2026-10-23; IBD distribution days
   SPY 7 and QQQ 7 in 25 sessions, "SEVERE"; exposure-coach advisory ceiling 15% CASH_PRIORITY).

## 3. Bottom line

- **No skill in the installed library can place an order by itself**: across all 74, the Python has
  no order submission, modification or cancellation, and all HTTP is GET. Brokerage *contact* exists:
  `portfolio-manager` reads Alpaca accounts/positions and selects the LIVE host for any
  `ALPACA_PAPER` value other than exactly "true" (`portfolio-manager/scripts/check_alpaca_connection.py:34,52-56`);
  `parabolic-short-trade-planner` calls Alpaca's trading host for inventory (`.../adapters/alpaca_inventory_adapter.py:34-35,65-66`);
  `mt5-robot-tester` launches a broker terminal (`mt5-robot-tester/scripts/mt5_batch_tester.py:466-470`);
  `breakout-trade-planner` builds Alpaca-shaped order bodies (`breakout-trade-planner/scripts/order_builder.py:30-85`).
  All four are disabled. The **original** repo's `trader-broker` can connect to a real brokerage (section 6).
- **34 skills need `FMP_API_KEY`** (plus Finviz Elite for some). No keys exist here and none were requested; those skills are disabled.
- **No skill refuses a partial (still-trading) daily bar** except `technical-analyst`'s weekly module.
  paper-advisor feeds skills only bars whose session closed plus a 60-minute buffer.
- **No skill simulates cash, holdings and fills** as a backtest; the only simulator (manifoldbt) is an
  external engine that is not installed.
- 9 skills are adopted through isolated adapters; 7 are used as research guidance; the rest are not
  integrated or are disabled, each with a reason (table below; also `bin/pa skills`).

## 4. Capability map (all 74)

Legend — **Use**: `ADAPTED` (runs each morning via an adapter) · `REVIEW` (runs in /paper-review) ·
`LINT` (report check) · `GUIDANCE` (SKILL.md procedure used by the Claude session, no code run) ·
`NOT-V1` (usable in principle, not integrated in v1, reason given) · `DISABLED`.
**Kind**: CODE / MIXED (code + LLM steps) / PROMPT / META.

| Skill | What the code actually does | Kind | Needs | Use | Why |
|---|---|---|---|---|---|
| market-breadth-analyzer | 6-component 0-100 breadth score from TraderMonty's public CSVs | CODE | keyless CSV | ADAPTED | fresh as-of verified; stale data only *warns* upstream (`scripts/market_breadth_analyzer.py:113-120`); advice text stripped |
| uptrend-analyzer | 5-component score from Monty's uptrend-ratio CSVs | CODE | keyless CSV | ADAPTED | no freshness check upstream (`scripts/uptrend_analyzer.py:75-106`); missing components score a neutral 50 at full weight (`scripts/scorer.py:91-93`) |
| ibd-distribution-day-monitor | IBD distribution days for SPY/QQQ, 25-session expiry | CODE | FMP as shipped | ADAPTED | pure `analyze_index()` run on our finished bars; its TQQQ (3x) `portfolio_action` discarded (`config/default.yaml:43-44`) |
| exposure-coach | weighted posture → exposure ceiling | CODE | none | ADAPTED | advisory only; freshness unchecked by default (`scripts/calculate_exposure.py:94-103`), so only fresh inputs are passed |
| position-sizer | fixed-fractional/ATR/Kelly share sizing | CODE | none | ADAPTED | caps apply only if flags passed (`scripts/position_sizer.py:154,172`); adapter always caps, never uses Kelly; advisory |
| stockbee-momentum-burst-screener | 4%/$0.90/range-expansion breakout scoring of the last bar | CODE | FMP optional | ADAPTED | offline `--prices-json` path on our finished, split-adjusted bars; no as-of guard upstream (`scripts/screen_momentum_burst.py:696`) |
| data-quality-checker | regex checks of a document (dates/weekdays, allocations, units) | CODE | none | LINT | exits 0 even on errors (`scripts/check_data_quality.py:765-803`); we read its JSON; dates check only (other checks misfire, see §5.4) |
| residual-edge-analyzer | OLS alpha vs baselines with Newey-West errors; refuses thin samples | CODE | none | REVIEW | honest floors (≥60 obs, `scripts/analyze_residual_edge.py:513,993-1005`) |
| weekly-performance-digest | closed-trade win rate, expectancy, profit factor, R | CODE | PyYAML | REVIEW | adapter writes initial stop (R uses `exit.stop_loss`, `scripts/generate_weekly_digest.py:175-190`) |
| market-news-analyst | 6-step news research procedure + impact score | PROMPT | web search | GUIDANCE | sourcing/date rules adopted; its "quantify every reaction" and estimated moves rejected (`SKILL.md:141,722`) |
| us-stock-analysis | fundamental/technical report framework | PROMPT | web search | GUIDANCE | asks for "real-time" prices from web search (`SKILL.md:10,14,17,53`): overridden, prices come from stored bars |
| backtest-expert | scores 8 user-typed summary numbers | MIXED | none | GUIDANCE | NOT_EVALUABLE below 30 trades used as the review gate; 20/100 points from a self-declared flag (`scripts/evaluate_backtest.py:194-200`) |
| trade-hypothesis-ideator | schema/guardrails for LLM hypothesis cards | MIXED | PyYAML | GUIDANCE | card format (kill criteria, minimum experiment) used; rank is the LLM's self-score (`pipeline/ranking.py:44-51`) |
| trading-skills-navigator | persona→workflow router | META | PyYAML | GUIDANCE | routes "long-term investor" to an Alpaca workflow unless `--no-api` (`references/intent_routing.md:51`) |
| market-environment-analysis | web-search report prompt + calendar helper | PROMPT | web | GUIDANCE | no as-of capture for numbers |
| us-market-bubble-detector | sums model-assigned indicator points | MIXED | none | GUIDANCE | scores without underlying values or dates (`scripts/bubble_scorer.py:323-360`) |
| technical-analyst | chart-image prompt + weekly reversal check | MIXED | FMP for CLI | NOT-V1 | pure module usable, but drops the just-closed week on weekend runs (`scripts/weekly_price_action.py:105-119`); CLI exits 1 without FMP despite SKILL.md (`scripts/check_weekly_price_action.py:686-689`) |
| vcp-screener | Minervini trend template + VCP detection | CODE | FMP | NOT-V1 | needs ≥222 bars; SPY aligned by index not date (`scripts/historical_scanner.py:109-110`) |
| ftd-detector | O'Neil follow-through-day state machine | CODE | FMP | NOT-V1 | needs index (not ETF) volume; requests data through today with no session-complete check (`scripts/fmp_client.py:86`) |
| macro-regime-detector | monthly cross-asset ratio regime | CODE | yfinance/FMP | NOT-V1 | counts the partial current month (`scripts/calculators/utils.py:26-40`); missing components score 0 (`scripts/scorer.py:64-67`) |
| sector-analyst | sector rotation from the uptrend CSVs | MIXED | keyless | NOT-V1 | freshness checks a different file than it scores (`scripts/analyze_sector_rotation.py:192-207`) |
| breadth-chart-analyst | chart-image prompt + latest CSV values | MIXED | keyless | NOT-V1 | same data as breadth-analyzer, newest row by file position (`scripts/fetch_breadth_csv.py:260,265`) |
| trade-performance-coach | rule checks on per-trade flags | CODE | jsonschema | NOT-V1 | multi-input monthly mode reads nothing (`scripts/review_trade_performance.py:802-820`) |
| drawdown-circuit-breaker | realized-P&L loss limits + cooldown | CODE | PyYAML | NOT-V1 | missing/empty state dir → TRADING_ALLOWED (`scripts/check_circuit_breaker.py:306-312`), default dir is relative (`:804`); ignores unrealized losses |
| pre-trade-discipline-gate | GO/NO_GO from a self-reported checklist | CODE | PyYAML | NOT-V1 | never age-checks upstream files (`scripts/check_pre_trade_discipline.py:279-293`); superseded by our deterministic validator |
| trader-memory-core | YAML thesis journal | CODE | jsonschema | NOT-V1 | a second system of record would diverge from our ledger; float money, no locking (`scripts/thesis_store.py:793-835`) |
| kanchi-dividend-review-monitor | OK/WARN/REVIEW triggers on user JSON | CODE | none | NOT-V1 | missing data reports OK (`scripts/build_review_queue.py:67-97,246-248`) |
| stockbee-exhaustion-hammer-screener | hammer/undercut scoring | CODE | FMP optional | NOT-V1 | built around provisional pre-close bars; stamps quotes with local today (`scripts/screen_exhaustion_hammer.py:284,1084-1085`) |
| stockbee-episodic-pivot-analyzer | catalyst + gap scoring | CODE | FMP optional | DISABLED | silently scores the latest bar if the event bar is missing (`scripts/analyze_ep.py:468-482`, verified) and ignores earnings timing (`:380`) |
| stockbee-20pct-study | ±20% mover study with forward outcomes | CODE | FMP optional | DISABLED | live universe = first N tickers alphabetically (`scripts/run_20pct_study.py:146-148`) |
| stockbee-setup-fluency-trainer | model book with 3/5-day outcomes | CODE | FMP optional | DISABLED | wins from intraday highs (`scripts/build_model_book.py:537-540`); study, not a decision input |
| portfolio-manager | Alpaca holdings analysis (LLM) + reconcile helper | MIXED | **Alpaca** | DISABLED | brokerage connector; live host unless ALPACA_PAPER=="true" (verified) |
| parabolic-short-trade-planner | short-sale plans + intraday FSM | CODE | FMP, **Alpaca** | DISABLED | short selling; Alpaca trading host |
| breakout-trade-planner | trade plans + Alpaca bracket order bodies | CODE | none | DISABLED | no aggregate cash cap or position count (`scripts/plan_breakout_trades.py:82-87,320`); missing exposure file = zero risk (fail-open) |
| mt5-robot-tester | MetaTrader 5 strategy-tester batch runner | CODE | Windows/MT5 | DISABLED | launches a broker terminal; forex/CFD |
| options-strategy-advisor | Black-Scholes demo | CODE | FMP | DISABLED | options; documented CLI flags do not exist (`scripts/black_scholes.py:449-549`) |
| futures-position-sizer | futures contract sizing | CODE | none | DISABLED | futures |
| cot-contrarian-detector | CFTC positioning index | CODE | FMP premium | DISABLED | futures |
| news-reaction-failure-analyzer | drift test vs a crowded position | CODE | FMP | DISABLED | futures COT chain |
| contrarian-setup-gate | fail-closed synthesis of the COT chain | META | none | DISABLED | emits SHORT setups (`scripts/gate_logic.py:99,468`) |
| pair-trade-screener | cointegrated pairs | CODE | FMP | DISABLED | needs short legs; full-sample OLS overstates cointegration (`scripts/find_pairs.py:259-291`) |
| crypto-regime-analyzer | crypto regime score | CODE | keyless | DISABLED | crypto |
| fxmacrodata-calendar | FX macro calendar | CODE | key (optional, unverified) | DISABLED | FX vendor; keyless access unverified |
| kanchi-dividend-us-tax-accounting | qualified/ordinary dividend labels | CODE | none | DISABLED | tax planning, not applicable |
| kanchi-dividend-sop | dividend SOP + entry signals | MIXED | FMP | DISABLED | requires key |
| canslim-screener | CANSLIM on a fixed 40-ticker list | CODE | FMP (+Finviz scrape) | DISABLED | latest-restated statements; neutral-50 fills (`scripts/screen_canslim.py:199-204`) |
| value-dividend-screener | value + dividend screen | CODE | FMP/Finviz | DISABLED | requires keys |
| dividend-growth-pullback-screener | dividend growth + RSI | CODE | FMP | DISABLED | keeps next-year declared dividends (`scripts/screen_dividend_growth_rsi.py:264-272`) |
| earnings-calendar | FMP earnings calendar | CODE | FMP | DISABLED | drops events silently on fetch failure, exits 0 (`scripts/fetch_earnings_fmp.py:145-166,436`); we use our own keyless calendar that reports gaps |
| economic-calendar-fetcher | FMP economic calendar | CODE | FMP | DISABLED | requires key (its fail-closed empty-response pattern is good) |
| earnings-trade-analyzer | post-earnings 5-factor score | CODE | FMP | DISABLED | `--as-of` does not limit prices (`scripts/fmp_client.py:75-86`) |
| pead-screener | post-earnings drift weekly candles | CODE | FMP | DISABLED | uses a possibly partial current week (`scripts/calculators/weekly_candle_calculator.py:163-168`) |
| institutional-flow-tracker | 13F ownership changes via FMP | CODE | FMP | DISABLED | quarter-end keyed, no filing-date gate (`scripts/track_institutional_flow.py:86-91`, verified) |
| us-undervalued-growth-screener | GARP pipeline + research contract | MIXED | FMP | DISABLED | `discover` stage has no as-of guard (`scripts/run_pipeline.py:1888-2063`) |
| market-top-detector | heuristic top-risk score | MIXED | FMP | DISABLED | mixes live quotes with model-supplied numbers |
| downtrend-duration-analyzer | decline-length histograms | CODE | FMP | DISABLED | centered peak windows look ahead (`scripts/analyze_downtrends.py:175-186`) |
| theme-detector | FINVIZ industry themes | CODE | Finviz scrape | DISABLED | undated scraped data; writes into `.claude/` by default (`scripts/theme_detector.py:489-496`) |
| finviz-screener | builds FinViz URLs, opens a browser | MIXED | none | DISABLED | returns no data |
| manifoldbt-backtester | adapter to the manifoldbt engine | CODE | manifoldbt (absent) | DISABLED | engine not installed; fills at next bar close (`scripts/run_backtest.py:150-151`) |
| edge-candidate-agent | heuristic research tickets | CODE | pandas | DISABLED | export forces full-sample validation (`scripts/candidate_contract.py:175-179`) |
| edge-concept-synthesizer | groups tickets into canned concepts | CODE | PyYAML | DISABLED | one hint counts as support |
| edge-hint-extractor | rule-based hints | CODE | PyYAML | DISABLED | undated news bypasses as-of (`scripts/build_hints.py:134-140`) |
| edge-pipeline-orchestrator | runs the edge chain | META | PyYAML | DISABLED | revision rounds thresholds until drafts pass (`scripts/orchestrate_edge_pipeline.py:248-258`) |
| edge-signal-aggregator | weighted conviction | META | PyYAML | DISABLED | wall-clock recency, uncalibrated weights (`scripts/aggregate_signals.py:400`) |
| edge-strategy-designer | strategy templates | CODE | PyYAML | DISABLED | tuned to pass the reviewer |
| edge-strategy-reviewer | keyword/structure lint called "review" | CODE | PyYAML | DISABLED | template draft scores ~81 PASS (`scripts/review_strategy_drafts.py:123-330`) |
| strategy-pivot-designer | stagnation triggers + pivot menu | CODE | PyYAML | DISABLED | iterates on the same data, no multiple-testing control |
| signal-postmortem | signal outcome recorder/analyzer | CODE | FMP | DISABLED | without FMP every outcome is NEUTRAL (`scripts/postmortem_recorder.py:239-250,342-363`); tests pin it |
| stanley-druckenmiller-investment | 8-input conviction synthesis | META | FMP upstream | DISABLED | malformed input → most bullish score (`scripts/report_loader.py:164-173`) |
| scenario-analyzer | orchestrates two subagents | PROMPT | agents | DISABLED | its required agents are not installed here |
| dual-axis-skill-reviewer | skill quality scoring | META | uv | DISABLED | dev tooling |
| skill-designer | prompt for generating skills | META | claude CLI | DISABLED | dev tooling |
| skill-idea-miner | mines `~/.claude/projects` session logs, sends snippets to `claude -p` | META | claude CLI | DISABLED | privacy and spend (`scripts/mine_session_logs.py:202-228,485-501`) |
| skill-integration-tester | checks workflow docs | META | none | DISABLED | needs a workflows CLAUDE.md that does not exist |

## 5. Material findings by concern (most severe first within each)

### 5.1 Filing publication dates vs transaction dates and reporting periods
- **institutional-flow-tracker** keys 13F data on report period (year, quarter) and takes the first
  non-empty quarter walking back from the calendar quarter; no `fillingDate`/`acceptedDate` anywhere
  (`scripts/track_institutional_flow.py:86-91,150-151`; `scripts/fmp_client.py:381-397`). 13Fs are due
  ~45 days after quarter end, so during filing season a partially filed quarter can be compared with a
  complete one. Same pattern in canslim's institutional component (`canslim-screener/scripts/fmp_client.py:477-493`).
- **us-undervalued-growth-screener** gates exactly one field on `acceptedDate` with time, read as Eastern
  (`scripts/run_pipeline.py:1213-1249`) — the right pattern — but TTM metrics and statements are not
  filing-date gated (`:1301-1335`; `scripts/fmp_client.py:608-634`).
- **paper-advisor**: no filing-based signal is used in v1 (SEC EDGAR returns 403 without a contact
  User-Agent; we will not invent one or send the user's email; set `PAPER_ADVISOR_SEC_USER_AGENT` to
  enable the allowlisted SEC hosts). Any filing the advisor cites must carry `published_at`.

### 5.2 Amended 13F filings, duplicate holdings, share classes, incomplete disclosures
- **institutional-flow-tracker** has no code for 13F-HR/A, confidential treatment, put/call rows or
  duplicate manager rows; holder rows map 1:1 (`scripts/data_quality.py:102-119`). Its share-class
  dedupe keeps the higher-market-cap class from a stale hard-coded list (RDS, DISCA/K, VIAC, FCAU)
  (`scripts/data_quality.py:26-37,152-184`). Its reference calls 13F-NT a "Notice of Confidential
  Treatment" (`references/13f_filings_guide.md:113`), which is wrong.
- **paper-advisor**: share-class identity is enforced per instrument (BRK.B listing symbol vs Yahoo
  BRK-B; GOOGL Class A verified by name; GOOG Class C fails verification by test).

### 5.3 Restated financial statements and historical lookahead
- Every FMP-based fundamentals skill reads latest-only statements with no as-of (e.g.
  `canslim-screener/scripts/fmp_client.py:349-383`; YoY compares list positions 0 and 4 without date
  matching, `canslim-screener/scripts/calculators/earnings_calculator.py:58-66`). Usable live, invalid
  for any historical scoring. Nothing detects restatements.
- `earnings-trade-analyzer`'s `--as-of` moves only the event window while prices run through today
  (`scripts/fmp_client.py:75-86`), so a past as-of sees the future.
- **paper-advisor**: forward-only evaluation; no historical replay in v1. Decisions record what
  evidence (with timestamps) existed at decision time.

### 5.4 Stale quotes, missing data, corporate actions, price adjustment
- Partial bars: no skill except `technical-analyst` refuses an in-progress session
  (`ibd-distribution-day-monitor/scripts/history_utils.py:40-42`; `ftd-detector/scripts/fmp_client.py:86`;
  `market-top-detector/scripts/calculators/distribution_day_calculator.py:107-120`;
  `theme-detector/scripts/etf_scanner.py:500-501`; `stockbee-momentum-burst-screener/scripts/screen_momentum_burst.py:200-205,696`).
- Stale data passes: breadth stale check only prints (`market-breadth-analyzer/scripts/market_breadth_analyzer.py:113-120`);
  a broken newest row silently falls back a day (`scripts/csv_client.py:61-66`); uptrend has no age check;
  exposure-coach strict mode skips regime/top-risk (`exposure-coach/scripts/provenance_contract.py:177-204`).
- Fail-open defaults: uptrend missing components = 50 at full weight; macro missing = 0; Druckenmiller
  malformed top-risk = most bullish; kanchi review missing = OK; drawdown breaker empty dir = allowed.
- Adjustment: no FMP skill uses `adjClose`/`adjDividend` (e.g. `value-dividend-screener/scripts/screen_dividend_stocks.py:410`);
  macro-regime uses dividend-adjusted closes (`scripts/fmp_client.py:377-378`), not point-in-time.
- data-quality-checker misfires: ratio constants contradict its own reference (SI/SLV 2.3 vs ~1.1,
  `scripts/check_data_quality.py:56-57`), unknown `--checks` names are skipped silently (`:697-699`).
- **paper-advisor**: Yahoo's split back-adjustment (verified on NVDA 2024-06) is undone to store
  as-traded prices; partial bars are never stored; bars on non-session days are reported; revisions
  are flagged and fills keep the first-observed bar; splits/dividends are explicit ledger events for
  both the portfolio and the SPY benchmark; missing corporate-action coverage or stale marks make a
  valuation provisional; every skill input is our finished bars and every skill output is
  freshness-checked against the last completed session.

### 5.5 Position sizing, portfolio-wide constraints, arithmetic precision, risk-check bypasses
- `position-sizer`: no position or cash cap unless flags are passed; risk % and Kelly unbounded
  (`scripts/position_sizer.py:47-48,130-134,154,172`, verified). With defaults a $500 account can be
  sized into a $1,000 position.
- `breakout-trade-planner`: caps risk, not money: no total-cost ≤ cash, no position count
  (`scripts/plan_breakout_trades.py:320`); missing exposure file = zero risk (`:82-87`).
- `pre-trade-discipline-gate`: "oversized" compares the trader's own numbers (`scripts/check_pre_trade_discipline.py:363-382`); upstream files never age-checked.
- `drawdown-circuit-breaker`: realized-only vs a fixed account size (`scripts/check_circuit_breaker.py:580-588`).
- Floats everywhere for prices and sizes in the screeners; `trader-memory-core` float money.
- **paper-advisor**: integer cents and micro-shares with conservative rounding; every limit evaluated
  across the whole batch plus pending reservations; deterministic rejection with recorded observed vs
  limit values; stops can only tighten (carried stop floor); the skills' sizing is advisory only.

### 5.6 Does any "backtest" simulate cash, holdings, fills and costs?
- No. `backtest-expert` scores numbers the user types (`scripts/evaluate_backtest.py:594-634`).
  `manifoldbt-backtester` defers to an engine that is not installed and fills at the next bar's close
  (`scripts/run_backtest.py:116-122,150-151`). The `edge-*` chain passes text and heuristic scores;
  `edge-strategy-reviewer`'s PASS is a keyword/structure lint. `stockbee-20pct-study` and
  `setup-fluency-trainer` count wins from intraday highs from signal-day closes.
- **paper-advisor** is forward paper trading with a real cash/holdings ledger, next-open fills,
  slippage and commissions; historical replay is not implemented in v1.

### 5.7 Order approval and brokerage reach
- Installed library: no order placement in code; disabled skills listed in §3.
- Original repo (reference only, §6): its `trader-broker` can send an order you approve to a real brokerage
  through Alpaca's MCP server or IBKR's connector.
- **paper-advisor**: no brokerage code exists; the only network module is GET-only with an allowlist
  and redirect re-checks; skill subprocesses inherit no credentials; tests prove the execution path
  runs with sockets disabled (`tests/test_no_live_broker.py`).

## 6. Jake Schincariol's trader-skill (reference only)

`Jakeschincariol/trader-skill` (MIT, commit 5189e79, 11 skills) is the original "Trader skill";
`Jakeschincariol/day-trader-skill` is its day-trading sequel. trader-skill was read for reference.
paper-advisor does not install, run or depend on either of them.

One fact matters if you install them yourself: both packs include a `trader-broker` skill that can send
an order you approve to a real brokerage through a brokerage connector (for example Alpaca's MCP server,
whose paper account is the default). Keep it on paper. paper-advisor has no brokerage connection of any
kind.

## 7. How defects affecting this app were fixed or isolated (upstream sources untouched)

| Problem | Isolation in paper-advisor |
|---|---|
| Partial/unfinished bars | adapters receive only bars whose session closed + 60 min |
| Stale skill data | as-of compared with the last completed NYSE session; stale outputs excluded from exposure inputs and flagged |
| Advice conflicting with rules (puts, inverse ETFs, TQQQ) | keys/text stripped and listed in `stripped_advice_fields` |
| Uncapped sizing | sizer adapter always passes caps, fixed risk, no Kelly; deterministic validator is the authority |
| Fail-open defaults | not integrated (drawdown breaker, pre-trade gate, kanchi monitor) |
| Brokerage reach | disabled; no code path; `tests/test_no_live_broker.py` |
| Upstream drift | tree-hash manifest; changed skills are skipped |
| Bytecode written into skill folders | skills run with `python -I -B` |
| Credentials in subprocess env | scrubbed environment; test asserts no keys inherited |
| Report lint exit code | JSON findings read; warnings only |

## 8. Limits of this audit
- FMP's server-side behavior (restatements, partial 13F aggregates, split adjustment) could not be
  verified without a key; marked UNVERIFIED by the auditors and irrelevant to v1 (FMP skills disabled).
- Large reference documents were keyword-searched, not read line by line; scripts relevant to
  verdicts were read. The manifoldbt engine source and the absent `trade-strategy-pipeline` repo
  were not available.
- TraderMonty's breadth/uptrend CSVs are produced by upstream jobs we cannot inspect; their flags
  say they are not point-in-time or survivorship-free, so they are context, never signals.
