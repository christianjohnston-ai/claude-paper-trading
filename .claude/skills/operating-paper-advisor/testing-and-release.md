# Testing and release

How a change to paper-advisor gets tested, recorded and committed in this repository. Commands run from
the project root unless they start with `cd`; pytest runs from `paper-advisor/`, where `pytest.ini`
lives.

Path convention (all files of this skill): `paper-advisor/...` and `.claude/...` are from the project root
(the folder you cloned); `tests/`, `config/`, `data/`, `reports/`, `backups/`, `exports/` and `bin/` are inside
`paper-advisor/`; module paths such as `broker_sim.py`, `dashboard/server.py`, `data_sources/http.py`,
`skill_runners/` and `migrations/0001_initial.sql` are inside `paper-advisor/paper_advisor/`.

## 1. The test suite

- **Config:** `paper-advisor/pytest.ini` sets `pythonpath = .`, `testpaths = tests` and
  `addopts = -q -p no:cacheprovider`. Run pytest from `paper-advisor/`.
- **Command:** `cd paper-advisor && .venv/bin/python -m pytest`, or one file with
  `.venv/bin/python -m pytest tests/test_risk.py`.
- **Isolation:** every test gets its own temporary app home (`tests/conftest.py`: the `app_paths`,
  `fixture_db` and `forward_db` fixtures). No test touches `paper-advisor/data/`, and the dashboard
  tests bind port 0, so two suites can run at once.
- **Size and time:** about 295 tests. A full suite has taken from about 75 s to about 6.5 minutes per
  interpreter, depending on what else the machine is doing. Run both interpreters in parallel.
- **Expected result:** `295 passed` with the third-party skills installed (`scripts/install_trading_skills.py`),
  or `293 passed, 2 skipped` without them. The 2 skips are the registry tests in `tests/test_skills_bridge.py`,
  which need the third-party skills (they are downloaded, not tracked).
- **The venv:** `paper-advisor/.venv` is Python 3.13, built with uv
  from `requirements-dev.txt` (pytest 9.1.1) and `requirements-skills.txt` (the skills' subprocess
  dependencies). To recreate it:
  ```bash
  cd paper-advisor && uv venv .venv --python 3.13
  uv pip install --python .venv/bin/python -r requirements-dev.txt -r requirements-skills.txt
  ```

## 2. The real Python 3.10 check

The app promises Python 3.10 or later, and 3.13 accepts syntax that 3.10 rejects. The classic case is
an f-string that reuses its own quote type inside a replacement field (PEP 701 allowed it in 3.12). For
example, `f"{x["a"]}"` raises `SyntaxError: f-string: unmatched '['` on 3.10, while 3.13 prints the value.
So every release also runs the suite on a real 3.10.

- **Interpreter:** a uv venv on CPython 3.10 with pytest only, in any scratch folder (the `-temp` suffix
  marks it as disposable). uv keeps the interpreter itself in `~/.local/share/uv/python/`.
- **To make it** (verified 2026-10-25 with uv 0.9.21):
  ```bash
  P310=<scratch dir>/py310-temp      # any scratch folder; keep the -temp suffix
  uv venv "$P310" --python 3.10
  uv pip install --python "$P310/bin/python" -r paper-advisor/requirements-dev.txt
  "$P310/bin/python" -c "import sys, sqlite3; print(sys.version, sqlite3.sqlite_version)"
  ```
- **No skill dependencies needed:** the 3.10 venv needs no `requirements-skills.txt`. The skill tests
  that would need them are skipped in an export.

## 3. Release check: both suites on an export of exactly the staged files

The working tree can hold uncommitted edits, untracked files and other sessions' work, so tests there
prove little. The repo's practice is to export exactly what the commit will contain and test that copy
with both interpreters:

```bash
S=<scratch dir>                      # for example this session's scratchpad
X="$S/commit_check-temp"; rm -rf "$X"; mkdir -p "$X"
ROOT="$(git rev-parse --show-toplevel)"
git -C "$ROOT" checkout-index -a --prefix="$X/"    # the index = the next commit's tree
cd "$X/paper-advisor"
PYTHONDONTWRITEBYTECODE=1 "$ROOT/paper-advisor/.venv/bin/python" -m pytest > "$S/commit_check_313-temp.log" 2>&1 &
PYTHONDONTWRITEBYTECODE=1 "$S/py310-temp/bin/python" -m pytest > "$S/commit_check_310-temp.log" 2>&1 &
wait; tail -2 "$S/commit_check_313-temp.log" "$S/commit_check_310-temp.log"
```

- **Shared index:** `checkout-index -a` exports the index. If another session has staged its own files,
  they are in the export too. Check `git diff --cached --name-only` first. To test an exact set when the
  index is shared, export `HEAD` with `git archive HEAD | tar -x -C "$X"`, then copy your changed files
  over it.
- **Record the counts:** put both results in the commit message as
  `Tests: N passed, M skipped on Python 3.13 and on 3.10, run on an export of exactly these staged files.`
- **UI changes** also need screenshots of every state, a dashboard started from a QA app dir
  (`--app-dir`), never the owner's real data for anything that writes.

## 4. CHANGELOG convention (`paper-advisor/CHANGELOG.md`)

Append one entry at the END of the file; the entries run oldest to newest. Format:

```markdown
## 2026-10-25 - Type: Config
- Change: <what changed and why, in sentences>
- Files: <paths relative to paper-advisor/; ../ for files outside it, e.g. ../.claude/commands/paper-morning.md>
- State impact: None | Transition added | Guard modified | ...
- Field mutations: None | table.column (what)
- Performance: None measured | <measurement>
```

The types in use are Pipeline, State, Field, Config, UI and Performance. Every codebase change gets an
entry, written before the commit.

## 5. Commit convention (read `git log` for examples)

- Commit on `main`.
- **Stage only your own paths** (`git add <path> ...`). Never `git add .` or `-A`: several sessions can
  work in this repo at once. Never stage `.claude/scheduled_tasks.lock` (Claude Code's scheduler).
- If git reports `index.lock`, wait 30 s and retry. Never delete the lock.
- **Message shape:**
  1. `<type>: <subject>`. Types in use: `feat`, `fix`, `security`, `config`; `docs` and other conventional
     types are allowed.
  2. A WHY paragraph. Fixes add the root cause.
  3. Optional "Not changed" and "Risk" paragraphs.
  4. The `Tests:` line.
  5. A `Files:` line with repo-relative paths.
  6. Last line: the attribution line the harness gives for the session's model. Every commit so far
     ends `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

  Wrap at about 120 characters, and write paragraphs, not bullets.
- **Verify after committing:** `git show --stat HEAD` must list only the intended files, and
  `git status --short` must be clean apart from other sessions' work.

## 6. What a change does to the record's identity

- **`code_tree_sha256` ("build N"):** covers `paper-advisor/paper_advisor/**` and `paper-advisor/config/**`
  (files ending `.py .sql .css .js .json`) plus `.claude/commands/paper-morning.md`, `paper-status.md`
  and `paper-review.md`. Changing any of them makes the next run a new build, which the dashboard flags
  when an experiment mixes builds. Tests, README, CHANGELOG, SKILL_AUDIT and `.claude/skills/` are
  outside the hash.
- **`code_version`** is `1.0.0+<HEAD, 12 chars>`, read from `.git` without running git. Every commit
  changes it.
- **Command files:** `.claude/commands/paper-*.md` are both instructions and code identity. Edit them
  only together with their twins: `decisions.DECISION_SCHEMA_DOC` and the morning text in
  `dashboard/pages.py` (see their SYNC WARNING comments).

## 7. Tests that encode cross-system rules (keep them true)

| Test | Rule |
|---|---|
| `tests/test_skills_bridge.py::test_registry_covers_every_installed_skill_exactly` | The third-party folders in `.claude/skills` (all folders minus `PROJECT_SKILLS`) equal `skills_bridge.REGISTRY`. A new project skill must be added to `PROJECT_SKILLS`, plus a `.gitignore` exception |
| `tests/test_skills_bridge.py::test_adopted_skills_match_the_audited_manifest` | Exactly 9 adopted skills, each matching its manifest hash |
| `tests/test_no_live_broker.py` (4 tests) | No brokerage code; only `http.py` opens connections; execution runs with sockets off; no credentials in skill subprocesses |
| `tests/test_db.py::test_threads_opening_and_closing_connections_at_once_never_deadlock` | The SQLite 3.51.1 thread deadlock stays avoided (`db._OPEN_CLOSE`) |
| `tests/test_dashboard.py` (its `PAGES` list) | The seven pages and their section ids |
| `tests/test_market_calendar.py` | The rule engine reproduces the official 2025-2027 NYSE table |
| `tests/test_tour.py` (11 tests, 7 database states) | The guided tour's tripwire: every step's `data-tour` anchor renders in every state and no anchor lacks a step; the launch control exists on desktop and phone; `tour.js` holds no call from `FORBIDDEN_IN_TOUR` and uses only the 3-call bridge; it is served with the CSP unchanged |
