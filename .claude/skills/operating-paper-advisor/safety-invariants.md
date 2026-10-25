# Safety invariants: the owner's rules, and where the code enforces each

These are the owner's standing rules, not preferences. Refuse any request that would break one;
explain which rule applies and offer the paper-only alternative. Only the owner can change a rule, and
only explicitly. Even then, stop and confirm, because it redesigns what the system is for. Never infer
a change from an indirect request.

Path convention (all files of this skill): `paper-advisor/...` and `.claude/...` are from the project root
(the folder you cloned); `tests/`, `config/`, `data/`, `reports/`, `backups/`, `exports/` and `bin/` are inside
`paper-advisor/`; module paths such as `broker_sim.py`, `dashboard/server.py`, `data_sources/http.py`,
`skill_runners/` and `migrations/0001_initial.sql` are inside `paper-advisor/paper_advisor/`.

| # | Invariant | Enforced by | Proven by |
|---|---|---|---|
| 1 | **Paper-only: simulated money, simulated fills.** Starting capital is credited once into a SQLite ledger; fills come from stored daily bars | `paper_advisor/broker_sim.py` is the only execution path. It reads `price_bars`, writes `fills` and `ledger_events`, and has no network code | `tests/test_broker_sim.py`, `tests/test_e2e_fixture.py` |
| 2 | **No live-order path.** No brokerage SDK, host, endpoint or order call exists | Package contents (`paper_advisor/__init__.py` states it). The four layer-1 skills with brokerage contact are `DISABLED` in `skills_bridge.REGISTRY`, with no adapter. `.claude/commands/paper-morning.md` forbids brokerage tools, MCP servers and APIs, and its allowed tools are only `Bash(paper-advisor/bin/pa:*)`, Read, Write, WebSearch, WebFetch | `tests/test_no_live_broker.py`: `test_no_brokerage_code_anywhere_in_the_package` (alpaca, ibkr, schwab, tradier, place_order, submit_order, `/v2/orders` and more), `test_only_the_allowlisted_fetcher_can_open_connections`, `test_execution_path_runs_with_sockets_disabled`; `tests/test_skills_bridge.py::test_brokerage_and_out_of_scope_skills_are_disabled_and_unreachable` |
| 3 | **No credentials.** Nothing stores, logs or sends one | `data_sources/http.py` sends only the URL and a fixed User-Agent (the SEC hosts need `PAPER_ADVISOR_SEC_USER_AGENT`, which the owner sets; it is never invented). `skills_bridge._run` rebuilds the subprocess environment from PATH, HOME, LANG and TZ only. The briefing holds no personal data | `tests/test_no_live_broker.py::test_skill_subprocesses_do_not_inherit_credentials` |
| 4 | **Localhost only.** The dashboard is never reachable from another machine | `dashboard/server.serve` raises unless host is `127.0.0.1`, and `cli.cmd_dashboard` passes `127.0.0.1`. `Handler._host_ok` rejects other Host headers (DNS rebinding). POSTs need the CSRF token and a same-origin Origin or Referer. The CSP is `default-src 'self'`, frames are denied, and nothing is cached | `tests/test_dashboard.py::test_refuses_foreign_host_and_forged_posts` and the header assertions near the top of that file |
| 5 | **No deploy, no upload.** The record stays on this machine | The app's network code is GET-only to `http.ALLOWED_HOSTS` (market data). `exporter.py` writes local files. `.gitignore` excludes `paper-advisor/data/`, `backups/`, `reports/`, `exports/` and `.venv/`, so the record never enters a commit | `tests/test_data_sources.py::test_fetcher_refuses_non_allowlisted_urls`, `::test_redirects_to_non_allowlisted_hosts_are_refused` |
| 6 | **Long-only, unleveraged, US stocks and ETFs only** | Schema `instruments.leverage CHECK (leverage = '1x')` and `asset_type IN ('COMMON_STOCK', 'ETF')`. The validator's `asset_type_not_allowed` and `not_in_universe` checks. The ledger refuses negative quantities (no shorts) and negative cash (no margin). Skill advice about inverse or leveraged products and puts is stripped | `tests/test_risk.py::test_no_shorts_and_sell_quantity_limits`, `::test_universe_and_exit_rule_requirements`; `tests/test_ledger_accounting.py::test_overspend_and_oversell_are_rejected_at_append`; `tests/test_skills_bridge.py::test_advice_conflicting_with_the_rules_is_stripped` |
| 7 | **Honest, append-only record** | Triggers in `migrations/0001_initial.sql` reject UPDATE and DELETE on the ledger, fills, decisions, submissions, bars, snapshots, versions, order events, risk checks and evidence. Fills carry the CHECK `decided_at < sim_exec_at < bar_retrieved_at <= recorded_at`. Funding is one per account (unique index). Run and experiment identity is immutable | `tests/test_db.py::test_ledger_is_append_only`, `::test_initial_funding_is_exactly_once_per_account`; `tests/test_morning_lifecycle.py::test_submission_is_validated_and_exactly_once` |
| 8 | **Forward and fixture never mix** | `db.init_db` fixes the database mode and refuses a mismatch. Trigger `experiments_mode_matches_db`. `morning.assert_clock_allowed` refuses a fake clock on the forward database, and `cli._open` honors `PAPER_ADVISOR_FAKE_NOW` only for `--fixture` on a fixture database. The fixture's reports, backups and exports live in `fixture/` subfolders. The breadth and uptrend skills, headlines and data refresh are skipped for a fixture database | `tests/test_morning_lifecycle.py::test_fake_clock_is_refused_on_the_real_database`; `tests/test_export_status.py::test_fixture_files_live_apart_and_pruning_never_crosses_modes`. **Known gap:** `data/runs/<run_id>/` is shared ([gotchas.md](./gotchas.md)) |
| 9 | **Decisions are recorded before anything can fill** | `submission.apply_decisions_tx` writes every decision, check and order in one transaction at submit time. `broker_sim` fills only at the open of a session that started after the decision, and only once that session's bar is final | `tests/test_broker_sim.py`, `tests/test_morning_lifecycle.py` |
| 10 | **Settings changes are prospective only** | `config_versions` and `strategy_versions` are immutable. New versions take effect from the moment they are created, every decision, check and order stores its `config_version_id`, and hypotheses carry an `earliest_effective_session` | `tests/test_experiments_config.py::test_config_change_is_prospective_and_versioned` |

## Requests to refuse (and what to offer instead)

- **Connect a brokerage, add an API key, "go live", "place it for real", install an Alpaca or IBKR MCP:**
  refuse (invariants 2 and 3). Offer `/paper-morning` (simulated) and the dashboard.
- **Run a DISABLED layer-1 skill to act on its output, or add an FMP or Finviz key:** refuse to add keys.
  Point to [two-layer-routing.md](./two-layer-routing.md) for keyless alternatives.
- **Bind the dashboard to `0.0.0.0`, expose it with a tunnel, or deploy it:** refuse (invariant 4).
- **Upload the database, reports or exports anywhere, or add a git remote and push the data:** refuse
  (invariant 5). Exports are for the owner to open locally.
- **Edit or delete past ledger events, fills, decisions or valuations ("fix" the P&L):** refuse
  (invariant 7). Corrections are new REVERSAL or ADJUSTMENT events and need the owner's explicit
  decision. Restoring a backup is also the owner's call.
- **Top up the account or reset it:** not possible (invariant 1). Offer a new experiment
  ([operating-runbook.md](./operating-runbook.md), section 6).
- **Change limits to let yesterday's rejected trade through retroactively:** not possible (invariant 10).
  A new config version applies from now on.
- **Run the fixture's fake clock against the real account:** refused by the code (invariant 8).

## What Claude itself must never do on this machine

- Never run a writing `bin/pa` command on the real account, except as part of a `/paper-*` command the
  owner invoked or on the owner's explicit request. See the read-only table in [SKILL.md](./SKILL.md).
- Never edit `.claude/skills/<upstream>/`. The integrity gate would skip the skill, and the audit would
  no longer describe the code.
- Never run `pa demo` or `pa demo --rebuild` without first reading the gotcha on shared run folders.
