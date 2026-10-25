-- paper-advisor schema v1.
--
-- Design rules encoded here (not just in Python), because the experiment record must stay honest
-- even if application code has a bug:
--   * Append-only evidence: ledger_events, fills, decisions, price_bars, source_snapshots,
--     config/strategy versions, valuation snapshots and order history reject UPDATE/DELETE via
--     triggers. Corrections are new rows (REVERSAL/ADJUSTMENT events, new revisions).
--   * Exactly-once: one INITIAL_FUNDING per (experiment, account); one fill per order; one decision
--     submission per run; idempotency keys on every ledger event.
--   * Time: every instant is TEXT 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (UTC). SYNC WARNING: clock.iso() emits
--     exactly this shape and string comparison relies on it (fills.sim_exec_at > fills.decided_at).
--   * Money in integer cents, quantities in integer micro-shares (see money.py).
--   * A database is either 'forward' (the real experiment) or 'fixture' (synthetic demo), fixed at
--     creation; an experiment whose mode differs from the database mode cannot be inserted.

CREATE TABLE app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE experiments (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT NOT NULL,
    mode                   TEXT NOT NULL CHECK (mode IN ('forward', 'fixture')),
    starting_capital_cents INTEGER NOT NULL CHECK (starting_capital_cents > 0),
    currency               TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    created_at             TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    inception_at           TEXT NOT NULL CHECK (inception_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    status                 TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    benchmark_symbol       TEXT NOT NULL,
    benchmark_start_session TEXT,
    created_by             TEXT NOT NULL DEFAULT 'cli',
    notes                  TEXT,
    -- Which code created the experiment (provenance.py): "<version>+<git HEAD>" and a hash of the exact bytes.
    code_version           TEXT NOT NULL DEFAULT 'unknown',
    code_tree_sha256       TEXT
);

CREATE TRIGGER experiments_mode_matches_db BEFORE INSERT ON experiments
WHEN NEW.mode IS NOT (SELECT value FROM app_state WHERE key = 'db_mode')
BEGIN
    SELECT RAISE(ABORT, 'experiment mode must match database mode (fixture data never enters the real experiment database)');
END;

CREATE TRIGGER experiments_immutable_core BEFORE UPDATE OF starting_capital_cents, mode, inception_at, created_at,
    code_version, code_tree_sha256 ON experiments
BEGIN
    SELECT RAISE(ABORT, 'starting capital, mode, inception and code identity are immutable; create a new experiment instead');
END;

CREATE TRIGGER experiments_no_delete BEFORE DELETE ON experiments
BEGIN
    SELECT RAISE(ABORT, 'experiments are never deleted; archive instead');
END;

-- Prospective configuration (risk limits, costs). Never edited; a change is a new version whose
-- effective_from is "now", so earlier decisions keep the limits that applied to them.
CREATE TABLE config_versions (
    id             INTEGER PRIMARY KEY,
    experiment_id  INTEGER NOT NULL REFERENCES experiments(id),
    version        INTEGER NOT NULL,
    params_json    TEXT NOT NULL,
    params_sha256  TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    reason         TEXT NOT NULL,
    UNIQUE (experiment_id, version)
);

CREATE TABLE strategy_versions (
    id             INTEGER PRIMARY KEY,
    experiment_id  INTEGER NOT NULL REFERENCES experiments(id),
    version        INTEGER NOT NULL,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL,
    rules_json     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    reason         TEXT NOT NULL,
    hypothesis_id  INTEGER,
    UNIQUE (experiment_id, version)
);

-- Dated, prospective hypotheses from /paper-review. Status may change (proposed -> adopted/rejected);
-- the hypothesis text itself is append-only by convention and the adoption creates a new
-- strategy/config version that is only effective for future sessions.
CREATE TABLE strategy_hypotheses (
    id                         INTEGER PRIMARY KEY,
    experiment_id              INTEGER NOT NULL REFERENCES experiments(id),
    created_at                 TEXT NOT NULL,
    review_run_id              INTEGER,
    title                      TEXT NOT NULL,
    hypothesis                 TEXT NOT NULL,
    rationale                  TEXT NOT NULL,
    evidence_json              TEXT NOT NULL DEFAULT '[]',
    proposed_change_json       TEXT NOT NULL DEFAULT '{}',
    falsification_test         TEXT NOT NULL,
    minimum_sample             TEXT,
    status                     TEXT NOT NULL CHECK (status IN ('proposed', 'adopted', 'rejected', 'retired')),
    status_changed_at          TEXT NOT NULL,
    earliest_effective_session TEXT NOT NULL,
    adopted_config_version_id  INTEGER,
    adopted_strategy_version_id INTEGER
);

-- Provenance of every third-party dependency adopted (skill library commits, data sources).
CREATE TABLE dependencies (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    commit_sha   TEXT,
    version      TEXT,
    verified_how TEXT NOT NULL,
    local_path   TEXT,
    recorded_at  TEXT NOT NULL,
    notes        TEXT,
    UNIQUE (name, kind, commit_sha, version)
);

CREATE TABLE instruments (
    symbol                 TEXT PRIMARY KEY,
    yahoo_symbol           TEXT NOT NULL,
    name                   TEXT NOT NULL,
    asset_type             TEXT NOT NULL CHECK (asset_type IN ('COMMON_STOCK', 'ETF')),
    expected_exchange      TEXT NOT NULL,
    expected_name_keywords TEXT NOT NULL,
    share_class_note       TEXT,
    leverage               TEXT NOT NULL DEFAULT '1x' CHECK (leverage = '1x'),
    is_benchmark           INTEGER NOT NULL DEFAULT 0,
    in_universe            INTEGER NOT NULL DEFAULT 1,
    added_at               TEXT NOT NULL,
    notes                  TEXT
);

CREATE TABLE instrument_verifications (
    id           INTEGER PRIMARY KEY,
    symbol       TEXT NOT NULL REFERENCES instruments(symbol),
    verified_at  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('VERIFIED', 'FAILED', 'UNAVAILABLE')),
    details_json TEXT NOT NULL,
    run_id       INTEGER
);
CREATE INDEX instrument_verifications_symbol ON instrument_verifications(symbol, verified_at);

-- Every external response we rely on, cached by content hash under data/evidence/.
CREATE TABLE source_snapshots (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    url            TEXT NOT NULL,
    params_json    TEXT NOT NULL DEFAULT '{}',
    requested_at   TEXT NOT NULL,
    retrieved_at   TEXT NOT NULL,
    http_status    INTEGER,
    content_sha256 TEXT,
    content_path   TEXT,
    bytes          INTEGER,
    market_asof    TEXT,
    parse_status   TEXT NOT NULL,
    error          TEXT,
    run_id         INTEGER
);

-- As-traded daily bars. A bar is stored only once it is FINAL (retrieved after the session close
-- plus a buffer). Values are un-adjusted for splits that occurred after the bar date (Yahoo
-- back-adjusts its 'close' for splits), so stored values are what actually traded that day.
CREATE TABLE price_bars (
    id           INTEGER PRIMARY KEY,
    symbol       TEXT NOT NULL,
    session_date TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    open         TEXT NOT NULL,
    high         TEXT NOT NULL,
    low          TEXT NOT NULL,
    close        TEXT NOT NULL,
    volume       INTEGER NOT NULL CHECK (volume >= 0),
    source       TEXT NOT NULL,
    snapshot_id  INTEGER REFERENCES source_snapshots(id),
    retrieved_at TEXT NOT NULL,
    market_ts    TEXT,
    split_factor TEXT NOT NULL DEFAULT '1',
    revision     INTEGER NOT NULL DEFAULT 1,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    UNIQUE (symbol, session_date, revision)
);

CREATE TABLE corporate_actions (
    id            INTEGER PRIMARY KEY,
    symbol        TEXT NOT NULL,
    action_type   TEXT NOT NULL CHECK (action_type IN ('SPLIT', 'CASH_DIVIDEND')),
    ex_date       TEXT NOT NULL,
    ratio_num     INTEGER,
    ratio_den     INTEGER,
    amount        TEXT,
    source        TEXT NOT NULL,
    snapshot_id   INTEGER REFERENCES source_snapshots(id),
    first_seen_at TEXT NOT NULL,
    notes         TEXT,
    UNIQUE (symbol, action_type, ex_date),
    CHECK ((action_type = 'SPLIT' AND ratio_num > 0 AND ratio_den > 0) OR (action_type = 'CASH_DIVIDEND' AND amount IS NOT NULL))
);

-- Coverage of corporate-action data: which symbol/date ranges we have actually checked. Valuations
-- that rely on unchecked ranges are flagged provisional.
CREATE TABLE corporate_action_coverage (
    symbol        TEXT PRIMARY KEY,
    checked_from  TEXT NOT NULL,
    checked_through TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    snapshot_id   INTEGER
);

CREATE TABLE calendar_overrides (
    id           INTEGER PRIMARY KEY,
    session_date TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind = 'CLOSED'),
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL
);

CREATE TABLE runs (
    id                    INTEGER PRIMARY KEY,
    experiment_id         INTEGER NOT NULL REFERENCES experiments(id),
    run_kind              TEXT NOT NULL CHECK (run_kind IN ('morning', 'review', 'settle')),
    analysis_session_date TEXT NOT NULL CHECK (analysis_session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    version               INTEGER NOT NULL,
    supersedes_run_id     INTEGER REFERENCES runs(id),
    mode                  TEXT NOT NULL CHECK (mode IN ('analysis', 'reconcile_only')),
    status                TEXT NOT NULL CHECK (status IN ('started', 'awaiting_decisions', 'decisions_submitted', 'completed', 'failed', 'interrupted')),
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    model_provider        TEXT NOT NULL DEFAULT 'unknown',
    model_id              TEXT NOT NULL DEFAULT 'unknown',
    model_effort          TEXT NOT NULL DEFAULT 'unknown',
    model_notes           TEXT,
    config_version_id     INTEGER REFERENCES config_versions(id),
    strategy_version_id   INTEGER REFERENCES strategy_versions(id),
    market_state          TEXT,
    data_status           TEXT,
    error                 TEXT,
    briefing_path         TEXT,
    report_path           TEXT,
    -- Which code ran (provenance.py); set at insert, never edited (runs_identity_immutable).
    code_version          TEXT NOT NULL DEFAULT 'unknown',
    code_tree_sha256      TEXT,
    UNIQUE (experiment_id, run_kind, analysis_session_date, version)
);

-- A run's identity (what, when, which model and which code) is a fact about the past; status, paths,
-- data status and errors are the only columns a run may update.
CREATE TRIGGER runs_identity_immutable BEFORE UPDATE OF experiment_id, run_kind, analysis_session_date, version,
    supersedes_run_id, started_at, model_provider, model_id, model_effort, config_version_id, strategy_version_id,
    code_version, code_tree_sha256 ON runs
BEGIN
    SELECT RAISE(ABORT, 'run identity (session, version, start, model, config, strategy, code) is immutable');
END;

CREATE TABLE run_events (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    at           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

-- One row per lock name. Acquired inside BEGIN IMMEDIATE, so two processes can never both hold it.
CREATE TABLE locks (
    name         TEXT PRIMARY KEY,
    run_id       INTEGER,
    token        TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    owner_pid    INTEGER,
    owner_host   TEXT
);

CREATE TABLE evidence_items (
    id             INTEGER PRIMARY KEY,
    experiment_id  INTEGER NOT NULL REFERENCES experiments(id),
    run_id         INTEGER REFERENCES runs(id),
    kind           TEXT NOT NULL CHECK (kind IN ('price', 'news', 'filing', 'earnings', 'skill_output', 'macro', 'web', 'note', 'calendar', 'identity')),
    symbol         TEXT,
    title          TEXT NOT NULL,
    url            TEXT,
    publisher      TEXT,
    published_at   TEXT,
    retrieved_at   TEXT,
    snapshot_id    INTEGER REFERENCES source_snapshots(id),
    summary        TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT,
    quality_flags  TEXT NOT NULL DEFAULT '[]',
    created_by     TEXT NOT NULL CHECK (created_by IN ('system', 'advisor')),
    created_at     TEXT NOT NULL
);
CREATE INDEX evidence_items_run ON evidence_items(run_id);

CREATE TABLE skill_runs (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER REFERENCES runs(id),
    skill_name      TEXT NOT NULL,
    upstream_commit TEXT NOT NULL,
    entrypoint      TEXT NOT NULL,
    args_json       TEXT NOT NULL DEFAULT '{}',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    exit_code       INTEGER,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'disabled', 'skipped')),
    output_path     TEXT,
    output_sha256   TEXT,
    stdout_tail     TEXT,
    stderr_tail     TEXT,
    detail          TEXT,
    evidence_id     INTEGER REFERENCES evidence_items(id)
);

-- Exactly one decision submission per run; the raw payload is kept verbatim with its hash.
CREATE TABLE run_submissions (
    run_id         INTEGER PRIMARY KEY REFERENCES runs(id),
    submitted_at   TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    market_view    TEXT NOT NULL,
    what_changed   TEXT NOT NULL DEFAULT '',
    model_id       TEXT NOT NULL,
    model_effort   TEXT NOT NULL
);

CREATE TABLE decisions (
    id                   INTEGER PRIMARY KEY,
    run_id               INTEGER NOT NULL REFERENCES runs(id),
    experiment_id        INTEGER NOT NULL REFERENCES experiments(id),
    seq                  INTEGER NOT NULL,
    action               TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD', 'NO_TRADE', 'CANCEL')),
    symbol               TEXT,
    notional_cents       INTEGER,
    quantity_micro       INTEGER,
    sell_all             INTEGER NOT NULL DEFAULT 0,
    cancel_order_id      INTEGER,
    thesis               TEXT NOT NULL,
    counterargument      TEXT NOT NULL,
    horizon_sessions     INTEGER,
    horizon_text         TEXT NOT NULL DEFAULT '',
    stop_loss_pct        TEXT,
    take_profit_pct      TEXT,
    max_holding_sessions INTEGER,
    invalidation         TEXT NOT NULL DEFAULT '',
    confidence           TEXT,
    uncertainty          TEXT NOT NULL DEFAULT '',
    key_risks            TEXT NOT NULL DEFAULT '',
    skills_used_json     TEXT NOT NULL DEFAULT '[]',
    evidence_ids_json    TEXT NOT NULL DEFAULT '[]',
    decided_at           TEXT NOT NULL CHECK (decided_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    UNIQUE (run_id, seq)
);

CREATE TABLE candidate_assessments (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    symbol            TEXT NOT NULL,
    verdict           TEXT NOT NULL CHECK (verdict IN ('selected', 'rejected', 'watch', 'not_reviewed')),
    assessment        TEXT NOT NULL,
    reason            TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    UNIQUE (run_id, symbol)
);

CREATE TABLE orders (
    id                  INTEGER PRIMARY KEY,
    experiment_id       INTEGER NOT NULL REFERENCES experiments(id),
    account             TEXT NOT NULL CHECK (account IN ('PORTFOLIO', 'BENCHMARK')),
    run_id              INTEGER REFERENCES runs(id),
    decision_id         INTEGER REFERENCES decisions(id),
    rule_id             INTEGER,
    origin              TEXT NOT NULL CHECK (origin IN ('ADVISOR', 'RULE', 'BENCHMARK')),
    client_order_id     TEXT NOT NULL UNIQUE,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type          TEXT NOT NULL DEFAULT 'MOO' CHECK (order_type = 'MOO'),
    notional_cents      INTEGER,
    quantity_micro      INTEGER,
    sell_all            INTEGER NOT NULL DEFAULT 0,
    decided_at          TEXT NOT NULL CHECK (decided_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    target_session_date TEXT NOT NULL,
    reserved_cash_cents INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL CHECK (status IN ('PROPOSED', 'REJECTED', 'PENDING', 'FILLED', 'CANCELED', 'EXPIRED')),
    status_reason       TEXT,
    -- Why the order exists (e.g. which exit rule fired and on which close). Fixed at creation, unlike
    -- status_reason, which tracks the latest lifecycle transition.
    origin_note         TEXT,
    config_version_id   INTEGER REFERENCES config_versions(id),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK ((side = 'BUY' AND notional_cents > 0 AND quantity_micro IS NULL AND sell_all = 0)
        OR (side = 'SELL' AND notional_cents IS NULL AND ((sell_all = 1 AND quantity_micro IS NULL) OR (sell_all = 0 AND quantity_micro > 0))))
);
CREATE INDEX orders_status ON orders(experiment_id, account, status);

CREATE TABLE order_events (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    recorded_at  TEXT NOT NULL,
    effective_at TEXT,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    reason       TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE risk_checks (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id),
    decision_id INTEGER REFERENCES decisions(id),
    order_id    INTEGER REFERENCES orders(id),
    check_name  TEXT NOT NULL,
    passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
    observed    TEXT,
    limit_value TEXT,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE fills (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    experiment_id    INTEGER NOT NULL REFERENCES experiments(id),
    account          TEXT NOT NULL CHECK (account IN ('PORTFOLIO', 'BENCHMARK')),
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    session_date     TEXT NOT NULL,
    bar_id           INTEGER REFERENCES price_bars(id),
    reference_open   TEXT NOT NULL,
    slippage_bps     TEXT NOT NULL,
    fill_price       TEXT NOT NULL,
    quantity_micro   INTEGER NOT NULL CHECK (quantity_micro > 0),
    gross_cents      INTEGER NOT NULL CHECK (gross_cents >= 0),
    commission_cents INTEGER NOT NULL CHECK (commission_cents >= 0),
    net_cash_cents   INTEGER NOT NULL,
    decided_at       TEXT NOT NULL,
    sim_exec_at      TEXT NOT NULL,
    bar_retrieved_at TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    run_id           INTEGER,
    notes            TEXT,
    -- A fill can never precede its decision, and the bar it uses must have been retrieved after the
    -- simulated execution instant (we only ever use completed daily bars).
    CHECK (sim_exec_at > decided_at),
    CHECK (bar_retrieved_at > sim_exec_at),
    CHECK (recorded_at >= bar_retrieved_at)
);

CREATE TABLE ledger_events (
    id                   INTEGER PRIMARY KEY,
    experiment_id        INTEGER NOT NULL REFERENCES experiments(id),
    account              TEXT NOT NULL CHECK (account IN ('PORTFOLIO', 'BENCHMARK')),
    seq                  INTEGER NOT NULL,
    event_type           TEXT NOT NULL CHECK (event_type IN ('INITIAL_FUNDING', 'BUY_FILL', 'SELL_FILL', 'CASH_DIVIDEND', 'SPLIT', 'REVERSAL', 'ADJUSTMENT')),
    effective_at         TEXT NOT NULL CHECK (effective_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    session_date         TEXT,
    recorded_at          TEXT NOT NULL CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    symbol               TEXT,
    quantity_delta_micro INTEGER NOT NULL DEFAULT 0,
    cash_delta_cents     INTEGER NOT NULL DEFAULT 0,
    fee_cents            INTEGER NOT NULL DEFAULT 0 CHECK (fee_cents >= 0),
    price                TEXT,
    split_num            INTEGER,
    split_den            INTEGER,
    fill_id              INTEGER REFERENCES fills(id),
    order_id             INTEGER REFERENCES orders(id),
    corporate_action_id  INTEGER REFERENCES corporate_actions(id),
    reverses_event_id    INTEGER REFERENCES ledger_events(id),
    idempotency_key      TEXT NOT NULL UNIQUE,
    payload_sha256       TEXT NOT NULL,
    memo                 TEXT,
    run_id               INTEGER,
    UNIQUE (experiment_id, account, seq),
    CHECK (event_type != 'INITIAL_FUNDING' OR (cash_delta_cents > 0 AND quantity_delta_micro = 0 AND symbol IS NULL)),
    CHECK (event_type != 'BUY_FILL' OR (cash_delta_cents < 0 AND quantity_delta_micro > 0 AND symbol IS NOT NULL)),
    CHECK (event_type != 'SELL_FILL' OR (cash_delta_cents >= 0 AND quantity_delta_micro < 0 AND symbol IS NOT NULL)),
    CHECK (event_type != 'CASH_DIVIDEND' OR (quantity_delta_micro = 0 AND symbol IS NOT NULL)),
    CHECK (event_type != 'SPLIT' OR (cash_delta_cents = 0 AND symbol IS NOT NULL AND split_num > 0 AND split_den > 0)),
    CHECK (event_type != 'ADJUSTMENT' OR (quantity_delta_micro = 0 AND memo IS NOT NULL)),
    CHECK (event_type != 'REVERSAL' OR reverses_event_id IS NOT NULL)
);
CREATE UNIQUE INDEX ledger_one_initial_funding ON ledger_events(experiment_id, account) WHERE event_type = 'INITIAL_FUNDING';
CREATE UNIQUE INDEX ledger_one_reversal_per_event ON ledger_events(reverses_event_id) WHERE reverses_event_id IS NOT NULL;

-- Precommitted exit rules for an open position, evaluated on completed daily closes.
CREATE TABLE position_rules (
    id                   INTEGER PRIMARY KEY,
    experiment_id        INTEGER NOT NULL REFERENCES experiments(id),
    symbol               TEXT NOT NULL,
    decision_id          INTEGER REFERENCES decisions(id),
    stop_loss_pct        TEXT,
    take_profit_pct      TEXT,
    max_holding_sessions INTEGER,
    invalidation         TEXT NOT NULL DEFAULT '',
    reference_price      TEXT,
    -- Absolute stop level carried from earlier rules on the same position. A later decision (adding
    -- to the position, averaging down, restating rules) can raise the effective stop but never lower
    -- it: effective stop = max(reference_price * (1 - stop_loss_pct), stop_floor_price).
    stop_floor_price     TEXT,
    entry_session_date   TEXT,
    effective_from       TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('AWAITING_ENTRY', 'ACTIVE', 'SUPERSEDED', 'CLOSED', 'TRIGGERED')),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    status_reason        TEXT
);
CREATE INDEX position_rules_active ON position_rules(experiment_id, symbol, status);

-- End-of-session valuations. Derived from ledger + as-traded closes; a correction is a new revision.
CREATE TABLE valuation_snapshots (
    id                       INTEGER PRIMARY KEY,
    experiment_id            INTEGER NOT NULL REFERENCES experiments(id),
    account                  TEXT NOT NULL CHECK (account IN ('PORTFOLIO', 'BENCHMARK')),
    session_date             TEXT NOT NULL,
    revision                 INTEGER NOT NULL DEFAULT 1,
    as_of_utc                TEXT NOT NULL,
    computed_at              TEXT NOT NULL,
    cash_cents               INTEGER NOT NULL,
    invested_cents           INTEGER NOT NULL,
    equity_cents             INTEGER NOT NULL,
    realized_pnl_cents       INTEGER NOT NULL,
    unrealized_pnl_cents     INTEGER NOT NULL,
    dividends_cents          INTEGER NOT NULL,
    fees_cents               INTEGER NOT NULL,
    traded_notional_cents    INTEGER NOT NULL DEFAULT 0,
    positions_json           TEXT NOT NULL,
    provisional              INTEGER NOT NULL CHECK (provisional IN (0, 1)),
    provisional_reasons_json TEXT NOT NULL DEFAULT '[]',
    ledger_seq_through       INTEGER NOT NULL,
    run_id                   INTEGER,
    revision_reason          TEXT,
    UNIQUE (experiment_id, account, session_date, revision)
);

CREATE TABLE reconciliation_results (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER REFERENCES runs(id),
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    at            TEXT NOT NULL,
    check_name    TEXT NOT NULL,
    passed        INTEGER NOT NULL CHECK (passed IN (0, 1)),
    details_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE reports (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER REFERENCES runs(id),
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE backups (
    id           INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    integrity_ok INTEGER NOT NULL,
    reason       TEXT NOT NULL
);

-- Append-only enforcement.
CREATE TRIGGER ledger_events_no_update BEFORE UPDATE ON ledger_events BEGIN SELECT RAISE(ABORT, 'ledger_events is append-only; record a REVERSAL or ADJUSTMENT event instead'); END;
CREATE TRIGGER ledger_events_no_delete BEFORE DELETE ON ledger_events BEGIN SELECT RAISE(ABORT, 'ledger_events is append-only'); END;
CREATE TRIGGER fills_no_update BEFORE UPDATE ON fills BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER fills_no_delete BEFORE DELETE ON fills BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT, 'decisions are immutable; submit a new versioned analysis instead'); END;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER run_submissions_no_update BEFORE UPDATE ON run_submissions BEGIN SELECT RAISE(ABORT, 'run submissions are immutable'); END;
CREATE TRIGGER run_submissions_no_delete BEFORE DELETE ON run_submissions BEGIN SELECT RAISE(ABORT, 'run submissions are immutable'); END;
CREATE TRIGGER price_bars_no_update BEFORE UPDATE ON price_bars BEGIN SELECT RAISE(ABORT, 'price bars are immutable; store a new revision'); END;
CREATE TRIGGER price_bars_no_delete BEFORE DELETE ON price_bars BEGIN SELECT RAISE(ABORT, 'price bars are immutable'); END;
CREATE TRIGGER source_snapshots_no_update BEFORE UPDATE ON source_snapshots BEGIN SELECT RAISE(ABORT, 'source snapshots are immutable'); END;
CREATE TRIGGER source_snapshots_no_delete BEFORE DELETE ON source_snapshots BEGIN SELECT RAISE(ABORT, 'source snapshots are immutable'); END;
CREATE TRIGGER config_versions_no_update BEFORE UPDATE ON config_versions BEGIN SELECT RAISE(ABORT, 'config versions are immutable; create a new version'); END;
CREATE TRIGGER config_versions_no_delete BEFORE DELETE ON config_versions BEGIN SELECT RAISE(ABORT, 'config versions are immutable'); END;
CREATE TRIGGER strategy_versions_no_update BEFORE UPDATE ON strategy_versions BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable; create a new version'); END;
CREATE TRIGGER strategy_versions_no_delete BEFORE DELETE ON strategy_versions BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END;
CREATE TRIGGER valuation_snapshots_no_update BEFORE UPDATE ON valuation_snapshots BEGIN SELECT RAISE(ABORT, 'valuation snapshots are immutable; store a new revision'); END;
CREATE TRIGGER valuation_snapshots_no_delete BEFORE DELETE ON valuation_snapshots BEGIN SELECT RAISE(ABORT, 'valuation snapshots are immutable'); END;
CREATE TRIGGER order_events_no_update BEFORE UPDATE ON order_events BEGIN SELECT RAISE(ABORT, 'order history is append-only'); END;
CREATE TRIGGER order_events_no_delete BEFORE DELETE ON order_events BEGIN SELECT RAISE(ABORT, 'order history is append-only'); END;
CREATE TRIGGER risk_checks_no_update BEFORE UPDATE ON risk_checks BEGIN SELECT RAISE(ABORT, 'risk checks are append-only'); END;
CREATE TRIGGER risk_checks_no_delete BEFORE DELETE ON risk_checks BEGIN SELECT RAISE(ABORT, 'risk checks are append-only'); END;
CREATE TRIGGER evidence_items_no_update BEFORE UPDATE ON evidence_items BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER evidence_items_no_delete BEFORE DELETE ON evidence_items BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER orders_no_delete BEFORE DELETE ON orders BEGIN SELECT RAISE(ABORT, 'orders are never deleted; cancel or expire them'); END;
-- Order rows may only change status/reason/updated_at/target_session_date, and only along the legal
-- lifecycle. Everything else about an order is fixed when it is created.
CREATE TRIGGER orders_core_immutable BEFORE UPDATE OF experiment_id, account, run_id, decision_id, origin, client_order_id, symbol, side, notional_cents, sell_all, decided_at, reserved_cash_cents, origin_note, created_at ON orders
BEGIN SELECT RAISE(ABORT, 'order terms are immutable'); END;
CREATE TRIGGER orders_legal_transition BEFORE UPDATE OF status ON orders
WHEN NOT (
       (OLD.status = 'PROPOSED' AND NEW.status IN ('REJECTED', 'PENDING'))
    OR (OLD.status = 'PENDING' AND NEW.status IN ('FILLED', 'CANCELED', 'EXPIRED', 'PENDING'))
)
BEGIN SELECT RAISE(ABORT, 'illegal order status transition'); END;
