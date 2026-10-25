# Gotchas and footguns: paper-advisor

Non-obvious failure modes, each verified against the code or a read-only run on 2026-10-25 (times are
Pacific, PDT). Where a document and the code disagree, the code wins; those cases are collected in the
last section.

Path convention (all files of this skill): `paper-advisor/...` and `.claude/...` are from the project root
(the folder you cloned); `tests/`, `config/`, `data/`, `reports/`, `backups/`, `exports/` and `bin/` are inside
`paper-advisor/`; module paths such as `broker_sim.py`, `dashboard/server.py`, `data_sources/http.py`,
`skill_runners/` and `migrations/0001_initial.sql` are inside `paper-advisor/paper_advisor/`.

---

## Records and files

### The fixture demo and the real account share `data/runs/<run_id>/`

**Symptom:** `paper-advisor/data/runs/1/briefing.json` belongs to the forward account, but the fixture
database's run 1 points at the same path. A `pa demo --rebuild` would overwrite forward run folders.
**Why it happens:** both databases number their runs from 1, and `briefing.write`,
`review.start` and `skills_bridge` all write to `paths.runs_dir / str(run_id)`. Only reports, backups
and exports have `fixture/` subfolders. Verified 2026-10-25 on the author's machine: forward run #1
overwrote the fixture's run-1 briefing; folders 2 to 6 still held fixture files. The database
rows (submissions, evidence) are unaffected, but briefing files and skill outputs live only on disk.
**Fix / what to do instead:** do not run a first `pa demo` or any `pa demo --rebuild` on this machine
while the forward account has runs. Before trusting a briefing file, check which database wrote it: its
`experiment.mode` field says `forward` or `fixture`. The app fix is a separate runs folder for the fixture
(`data/runs/fixture/`), as reports, backups and exports already have. That is an app change, so it is
the owner's decision.

### Editing `migrations/0001_initial.sql` locks every writing command out of the real database

**Symptom:** after a schema edit, `init`, `morning start` and every other writing command fail with
`MigrationError: migration 0001_initial was edited after it was applied (checksum mismatch)`.
**Why it happens:** `db.migrate` stores each applied migration's sha256 and refuses a changed file. The
real database applies 0001 at its first `init`.
**Fix / what to do instead:** never edit an applied migration. Add `0002_<name>.sql`. Migrations run in
order and are immutable. Read-only `status` still works (it skips migrations).

### Restoring a backup over a database with leftover `-wal` / `-shm` files

**Symptom:** after copying a backup over `data/paper_advisor.sqlite`, the data looks wrong or the
database reports corruption.
**Why it happens:** README's restore line ("copy a backup file over `data/paper_advisor.sqlite`") omits
the WAL sidecars. SQLite replays a leftover `-wal` next to the file. `fixture_demo.run_demo` deletes
both sidecars when it replaces a database, for this reason.
**Fix / what to do instead:**
1. Stop every process that has the database open (the dashboard, a running session).
2. Back up the current state.
3. Move `paper_advisor.sqlite-wal` and `-shm` aside.
4. Copy the chosen backup in, then run `bin/pa reconcile`.

Restoring is the owner's call only.

### `pa demo --rebuild` leaves the replaced fixture database in the forward backups folder

**Symptom:** `paper-advisor/backups/fixture_demo-replaced-<stamp>.sqlite` appears next to the forward
backups, and pruning never removes it.
**Why it happens:** `run_demo` moves the old file to `paths.backups_dir` itself, not `backups/fixture/`.
`prune_backups` only globs `paper_advisor-<mode>-*.sqlite`.
**Fix / what to do instead:** it is harmless, but it is not a forward backup. Delete it by hand if you
want, after checking the name.

---

## Runs and locks

### `/paper-status` says "in progress" for a run whose lock already expired

**Symptom:** status prints "Run #N is in progress (its lock lasts until <weekday, date, time PT>)" with
a time that has already passed, while the dashboard says "stopped (lock expired)".
**Why it happens:** `status.render` prints any lock row it finds and never compares `expires_at` with
now. An expired lock row stays until the next start replaces it.
**Fix / what to do instead:** compare the stated time with now. If it has passed, the run is stopped.
A plain `/paper-morning` takes it over (no `force-recover`).

### A rerun cannot decide, and a re-analysis still uses up the order limit

**Symptom:** the second `/paper-morning` of a morning returns `mode: reconcile_only`, and Claude cannot
submit decisions.
**Why it happens:** `morning.start` keys runs by the session that orders would target. Once any morning
run for that session has a submission, every later run is reconcile-only unless `--new-analysis` is
passed. A new analysis is attempt N+1, and its orders count against `max_new_orders_per_session`
(canceled orders count; rejected ones do not).
**Fix / what to do instead:** summarize the reconcile-only report and stop. Use
`/paper-morning new-analysis` only when the owner wants a deliberate re-decision.

### Orders target the next day although the run started before the open

**Symptom:** a run started at 9:20 ET reports `analysis_session` = today, but its accepted orders
target tomorrow's open.
**Why it happens:** the run key is computed at start, but `submit` recomputes the target from the submit
time (`next_session_after(now + 120 s)`). Submitting at or after 9:28 ET misses today's open.
Separately, submit refuses outright if a session completed (close + 60 min) after the briefing was
built.
**Fix / what to do instead:** before the open, finish research by 9:28 ET (6:28 AM PT). Report the
`target_session` from the submit output, not the run key. On the stale-briefing refusal, start a new run.

### An unfinished `/paper-review` is killed by the next morning start

**Symptom:** `bin/pa review submit` fails with "run N is not an open review" (exit 3).
**Why it happens:** reviews take no lock. `morning.start` marks every run of the experiment that is
still `started`, `awaiting_decisions` or `decisions_submitted` as `interrupted`, and that includes
review runs.
**Fix / what to do instead:** finish a review (start, then submit) in one sitting. If it was
interrupted, run `bin/pa review start` again.

### A takeover leaves the old session holding a dead token

**Symptom:** the old session's `morning submit` or `finish` fails with `LockBusy: this run does not hold
the experiment lock (it expired or was taken over); start a new run`.
**Why it happens:** `check_lock` requires the current token. A takeover (expired lock or
`--force-recover`) deleted the old lock row.
**Fix / what to do instead:** stop using the old run. Its committed rows stay. Continue in the new run,
or start one.

### Reports are named for the target session, not the day the run happened

**Symptom:** a Saturday run's report is `reports/2026/10/2026-10-26_morning_run1.md`, a Monday date.
**Why it happens:** `reports.save` uses the run's `analysis_session_date`: the open its orders would
target, computed when the run started. `morning start` prints it as `analysis_session`, and submit may
target a later open (see "Orders target the next day").
**Fix / what to do instead:** find "this morning's report" by run id, or in the dashboard's Runs page,
not by today's date.

---

## Validation and simulation

### A REJECTED decision and an EXPIRED order are different failures

**Symptom:** a BUY showed ACCEPTED in the morning report but never filled.
**Why it happens:** rejection happens at submit, in the deterministic checks (`risk_checks`). Expiry
happens later, at settlement:
- `no_bar_for_target_session`: a halt or gap;
- `below_minimum_at_fill`: the cost after rounding fell under the minimum;
- `missed_target_session`;
- `session_data_unavailable_timeout`: no data for a whole session within 5 days.
The benchmark's own buy never expires for good: it is re-queued with its original decision time.
**Fix / what to do instead:** read the order's `status_reason`, or open its drawer in the dashboard,
before explaining.

### Stale data blocks buys, never sells, and makes valuations provisional

**Symptom:** BUYs are rejected with `stale_data` or `missing_data`, the briefing says `PARTIAL` or
`DATA_UNAVAILABLE`, and valuations show PROVISIONAL.
**Why it happens:** buys need the latest bar to be the last completed session (`quality.assess`).
Sells are allowed on any data because they reduce risk. A snapshot is provisional when a mark is
stale or missing, corporate actions were not checked through that session, a bar is flagged, the
calendar year is unverified, or the feed delivered nothing.
**Fix / what to do instead:** a later run's refresh normally clears it. Never invent prices from the
web. Report the provisional reasons; the count is `provisional_sessions` in the output of
`metrics.compute`.

### The CLI does not guard settings or experiments the way the dashboard does

**Symptom:** `bin/pa config set ...` succeeds during a morning run, and `bin/pa experiment new` leaves
no backup behind.
**Why it happens:** the dashboard refuses config saves and experiment creation while a run holds a live
lock, and backs up before creating an experiment (`server._act_config`, `_act_new_experiment`). The
CLI's `config set` checks no lock, and `experiment new` takes no backup. `experiments.set_active` and
creation do refuse during a run (`RunInProgress`) on both paths. That guard tests for a lock whose
expiry is still in the future, so an expired lock does not count.
**Fix / what to do instead:** change limits only when no run is in progress, since a mid-run change
applies to that run's submit. Run `bin/pa backup --reason "..."` before `bin/pa experiment new`.

### "Read-only" commands that are not

**Symptom:** a supposedly harmless inspection leaves new rows or files.
**Why it happens:**
- `bin/pa reconcile` writes `reconciliation_results` rows.
- `bin/pa settle` records fills (simulated trades) and snapshots.
- `bin/pa export` and `bin/pa backup` write files (backup also writes a `backups` row).
- `bin/pa demo` writes a database and run folders.
- Even a true read-only open of a WAL database can create or touch the `-shm` and `-wal` sidecar files.

Truly read-only: `status` and `rebuild-check` (`mode=ro`), and `skills`, `--help` and `--version` (no
database). `config show`, `experiment list` and `calendar list` only read, but they open a writable
connection, so they would apply a pending migration.
**Fix / what to do instead:** use the read-only table in SKILL.md. For ad hoc queries, open the
database through `db.connect(path, readonly=True)` (see the playbook in operating-runbook.md).

### `sqlite3 -readonly` cannot open a WAL database copy without its sidecars

**Symptom:** `Error: in prepare, unable to open database file (14)` from `/usr/bin/sqlite3 -readonly`
on a copied `.sqlite` file.
**Why it happens:** read-only WAL access needs the `-shm` / `-wal` files. The macOS sqlite3 CLI
(3.43.2) with `-readonly` will not create them. Python's `mode=ro` (SQLite 3.51.1) opened the same copy,
and created the sidecars.
**Fix / what to do instead:** query through the app's `db.connect(..., readonly=True)` with the venv
Python, or copy the database together with its sidecars.

---

## Dashboard

### Inline script or style is silently blocked by the strict CSP

**Symptom:** a new `<script>`, an `onclick=` handler or a `style="..."` attribute does nothing in the
browser, with no error on the page.
**Why it happens:** `dashboard/server.py` sends
`default-src 'self'; script-src 'self'; style-src 'self'; ...` with no `unsafe-inline`. Only
`/static/app.js` and `/static/app.css` are served.
**Fix / what to do instead:**
- Put behavior in `dashboard/static/app.js` and styling in `dashboard/static/app.css`.
- Pass data with `data-*` attributes (as `pages.page` does with `data-state`).
- SVG presentation attributes (`fill=`) are fine; `style=` is not.

### The dashboard keeps showing old code

**Symptom:** an edit to `pages.py` or `server.py` does not appear on reload, but CSS and JS edits do.
Or nothing changes at all.
**Why it happens:** the server reads `app.css` and `app.js` from disk on every request, but loads the
Python once at start. A running server may also come from another code tree, such as an exported copy
started with `--app-dir` pointing at the real data folder.
**Fix / what to do instead:**
1. Find the served tree: `lsof -a -p $(lsof -tiTCP:8765 -sTCP:LISTEN) -d cwd -Fn` shows the code, and
   `ps -o args= -p <pid>` shows `--app-dir`, the data.
2. Restart that server for Python changes.
3. Never point a QA or test server at the owner's data with a writing action.

### The fixture demo is judged against its own frozen clock

**Symptom:** the fixture view shows 2025 dates and calls its data fresh, while the forward view uses
today.
**Why it happens:** `pages.view_now` uses the latest run time in the fixture database for the fixture
(the scenario ends in December 2025). Judging 2025 synthetic data against today would mark everything
stale and unverified. `PAPER_ADVISOR_FAKE_NOW` is honored only with `--fixture` on a fixture database,
and `morning.assert_clock_allowed` refuses any fake clock on the forward database.
**Fix / what to do instead:** never compare fixture freshness with today, and never try to run the
morning flow on the fixture with the real clock. Rebuild it instead (after reading the first gotcha).

### On a phone, a tour card docks at the screen's edge as a full-width sheet

**Symptom:** on a phone, a tour step's card is glued to the bottom (or top) edge, full width, with part of
the lit target cut off above it. It looks like a layout bug.
**Why it happens:** when nothing fits beside the whole target, `static/tour.js` docks the card at one end of
the window and stops the spotlight short of it, so the card never covers what it describes. On a phone the
card then runs flush to the window's edge (`.tour-card.tour-flush` or `.tour-flush-top` in `app.css`), so no
dimmed sliver of the target peeks out beside it. A pinned target (the More menu, the side sheet) keeps
whichever end leaves more of it lit.
**Fix / what to do instead:** expected; leave it. If a card covers what it names, look at that step's target
size and its `placement`, not at the sheet styling.

### A cut-short spotlight ends between blocks, never inside a row, and the card says "further down"

**Symptom:** the spotlight stops above part of a table or form, and the card says the rest is "further down".
Or, after a markup change, a cut slices through a new chip-like element.
**Why it happens:** `snapBottom` in `tour.js` only cuts at a block's bottom or a line break. It never cuts
through a line of text, a field, a button, a chip or a table row: the `SOLID` selector list (`tr`, inputs,
`.chip`, `.badge`, `.cmd-pill`, `.next-cmds` and more). When the spotlight is cut short, the card then says
the rest is further down: in its own words for steps marked `more: false` (they test `s.cut`), otherwise as
a separate line. A round-8 polish rule that put the ring on a row divider was reverted on purpose: rounded onto
the divider, a cut between table rows landed in the next row, and desktop Runs lit no rows.
**Fix / what to do instead:** when you add an element that must never be split, add its selector to `SOLID`
in `tour.js`. Do not reintroduce the row-divider rule.

### An open tour pauses the dashboard's self-reload

**Symptom:** a morning run started or ended, but a tab with the tour open did not refresh.
**Why it happens:** `app.js`'s `reloadAllowed()` returns false while `body.tour-open` is set, as it does for
an open drawer, an open `<details>` or a focused field. Reloading would drop the owner out of the tour.
**Fix / what to do instead:** close the tour (Esc or End tour). The next 15-second state poll (forward-test
tabs only) compares with the state the page rendered, so it still reloads; so does the minute reload during
a run.

### A harmless-looking refactor of `tour.js` breaks `tests/test_tour.py`

**Symptom:** after restructuring the tour's steps (computed targets, template literals, steps moved into a
helper), `tests/test_tour.py` fails or no longer sees the steps.
**Why it happens:** the test reads `tour.js` as text. It expects the steps between `var STEPS = [` and
`// END STEPS`, each step's `id` first, and `target`, `phoneTarget` and `also` as double-quoted literals.
Its `FORBIDDEN_IN_TOUR` list also bans `innerHTML`, `.value =`, `location.reload`, `cssText` and `fetch(`,
among others. The bridge may expose only `showPage`, `openSheet` and `closeSheet`.
**Fix / what to do instead:** keep that shape. Build the card's text with `createElement` and
`textContent`. Before adding a bridge call, confirm that it writes nothing; then update the test's `BRIDGE`
set and the SYNC WARNINGs in `app.js` and `tour.js`.

---

## Database engine and Python versions

### SQLite 3.51.0-3.51.1 deadlocks when threads open and close connections at once

**Symptom:** the test suite or the dashboard freezes at 0% CPU and cannot be interrupted. Native stacks
show `unixOpen` and `sqlite3WalClose` blocked on the same file.
**Why it happens:** it is an upstream SQLite bug, fixed in 3.51.2. Homebrew's Python 3.13 links 3.51.1,
and the dashboard's handler threads open and close connections concurrently. The closing thread
holds the GIL.
**Fix / what to do instead:** always open and close through `db.connect`, which serializes the open and
the close process-wide (`db._OPEN_CLOSE`, a connection subclass whose `close` takes the same lock).
Never call `sqlite3.connect` directly in app code. `tests/test_db.py` reproduces the hang without the
lock.

### The WAL-reset bug (SQLite before 3.51.3) is not fixable from the app

**Symptom:** `/paper-status` and Data & health name "the WAL-reset bug".
**Why it happens:** in SQLite before 3.51.3 (except the 3.50.7 and 3.44.6 backports), two connections
writing or checkpointing at the same instant can corrupt a WAL database. Both interpreters used here
are affected: 3.51.1 (venv 3.13) and 3.50.4 (py310).
**Fix / what to do instead:** the upgrade is `brew upgrade sqlite`, and it is the owner's decision,
because the library is shared with other software. Every backup is integrity-checked.

### f-strings that reuse their own quote type fail before Python 3.12

**Symptom:** the suite passes on 3.13 but the module will not import on 3.10:
`SyntaxError: f-string: unmatched '['`.
**Why it happens:** PEP 701 (Python 3.12) allowed `f"{x["a"]}"`. 3.10 and 3.11 reject it. The app
promises Python 3.10 or later.
**Fix / what to do instead:** use the other quote type inside the braces (`f"{x['a']}"`), or a
variable. Always run the 3.10 suite ([testing-and-release.md](./testing-and-release.md), section 2).

---

## Skills (layer 1 into layer 2)

### A new folder in `.claude/skills/` fails the registry test

**Symptom:** `tests/test_skills_bridge.py::test_registry_covers_every_installed_skill_exactly` fails
with "Extra items in the left set". Or, in an export of the tracked files, both registry tests fail
with "skill not installed".
**Why it happens:** before 2026-10-25 the test required the folders in `.claude/skills` to equal the
74-name `REGISTRY` exactly, and treated the folder's mere existence as "installed".
**Fix / what to do instead:** the test now subtracts `PROJECT_SKILLS` (this skill) and treats the
library as installed only when a third-party folder exists. A stray or missing third-party folder still
fails. Add every new first-party skill to `PROJECT_SKILLS`, by its exact folder name (never a pattern
or prefix), and to `.gitignore` as `!.claude/skills/<name>/`. A third-party skill never goes into
`PROJECT_SKILLS`: it enters `skills_bridge.REGISTRY` through the audit in `paper-advisor/SKILL_AUDIT.md`.
Keep `.claude/skills/*`, not `.claude/skills/`: git never looks inside an excluded directory, so the
exception would do nothing. This test edit (approved 2026-10-25) was the skill's one deliberate change
under `paper-advisor/` besides CHANGELOG.

### A changed upstream skill silently drops out of the morning

**Symptom:** the briefing shows a skill as `skipped` with
`tree hash <x> != audited <y>`, and the morning run continues without it.
**Why it happens:** `skills_bridge.integrity` compares each adopted skill's tree hash with
`config/skills_manifest.json`. Any edit, update, or stray file (other than `__pycache__` and
`.DS_Store`) changes the hash.
**Fix / what to do instead:** restore the upstream files, or re-audit and update the manifest, the
REGISTRY and SKILL_AUDIT.md ([two-layer-routing.md](./two-layer-routing.md), section 5). Run any upstream
script with `-B` from a scratch folder, so nothing is written into the skill folder.

### The navigator's documented command does not work here

**Symptom:** `python3 skills/trading-skills-navigator/scripts/recommend.py` fails with "No such file".
Without `--no-api`, a goal can be routed to an Alpaca brokerage workflow. A specific goal ("VCP
setups") can fall back to a generic beginner workflow.
**Why it happens:** the SKILL.md was written for the upstream repo layout, and the routing is
persona-based.
**Fix / what to do instead:** run
`paper-advisor/.venv/bin/python -I -B .claude/skills/trading-skills-navigator/scripts/recommend.py --query "..." --no-api --format json`,
which uses the bundled snapshot and writes nothing. Also look for skills by name.

### Skill output is never permission to trade

**Symptom:** a session wants to place the exposure-coach ceiling, a position-sizer row or a
momentum-burst setup as an order.
**Why it happens:** layer-1 skills describe the market. Their advice keys are stripped and their sizing
is advisory.
**Fix / what to do instead:** every order is a decision in `/paper-morning`'s `decisions.json`,
validated by `submission.py`. Cite the skill's evidence id and name it in `skills_used`.

---

## Documents that disagree with the code (the code wins)

### README's data-source table is not the fetcher's allowlist

**Symptom:** README lists `tradermonty.github.io` and `raw.githubusercontent.com` but not
`news.google.com`. `data_sources/http.ALLOWED_HOSTS` holds `news.google.com` and the SEC hosts, but not
the GitHub hosts. Its SYNC WARNING says the two are "identical", and names a README section ("External
data requests") that does not exist.
**Why it happens:**
- `news.google.com` is allowlisted, and `news.google_news_query` exists, but nothing calls it: headlines
  come only from Yahoo RSS.
- The SEC hosts are allowlisted, but no code fetches SEC data. `PAPER_ADVISOR_SEC_USER_AGENT` only
  unlocks them.
- The two GitHub hosts are reached by the breadth and uptrend skill subprocesses themselves, outside
  the Fetcher.
**Fix / what to do instead:** describe the traffic as it is: the app's Fetcher reaches Yahoo, Nasdaq
Trader and Nasdaq; two skill subprocesses fetch public CSVs; the model service receives the briefing.

### `clock.py` points to a clock guard that lives in `morning.py`

**Symptom:** `clock.py`'s docstring says the forward-database guard is runs.assert_clock_allowed, but
`runs.py` has no such function.
**Why it happens:** the guard is `morning.assert_clock_allowed`. It is called by `morning.start`,
`submit` and `finish`, and by `cli.cmd_settle`. The docstring is stale.
**Fix / what to do instead:** read `morning.py`. The guard refuses an injected clock on any `forward`
database.

### "At most 3 hypotheses" is a procedure, not a check

**Symptom:** a review payload with more than 3 hypotheses is accepted and stored.
**Why it happens:** README and `.claude/commands/paper-review.md` limit a review to 3 hypotheses, but
`review.validate` never counts them.
**Fix / what to do instead:** keep to 3 by procedure. Do not describe the limit as enforced.

### `skills_bridge.py` says REGISTRY lists every folder in `.claude/skills`

**Symptom:** its docstring's SYNC WARNING says REGISTRY "must list every directory in .claude/skills",
yet `operating-paper-advisor` is not in REGISTRY and the tests pass.
**Why it happens:** since 2026-10-25 the registry test subtracts `PROJECT_SKILLS`. The docstring was
left unchanged on purpose: editing `skills_bridge.py` changes the build hash of the owner's runs.
**Fix / what to do instead:** read it as "every third-party directory". Update the docstring together
with the next real change to `skills_bridge.py`.

### The dashboard allows backup and export during a run

**Symptom:** `dashboard/server.py`'s docstring says state-changing actions "never run while a morning
run holds the experiment lock", yet Backup and Export work mid-run.
**Why it happens:** only config, new-experiment and activate check the lock. Backup uses SQLite's online
backup API, and export only reads.
**Fix / what to do instead:** nothing. Both are safe mid-run, and the docstring overstates the rule.
README's restore line and its "fixture files apart" claim are covered by the restore and shared-runs
gotchas above.
