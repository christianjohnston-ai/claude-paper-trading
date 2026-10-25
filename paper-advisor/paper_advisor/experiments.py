"""Experiments: creation (exactly once for the default $500 account), activation, benchmark setup.

An experiment fixes its starting capital, mode (forward/fixture) and inception instant forever
(schema triggers forbid editing them). Changing capital means creating a NEW experiment; the old
one is archived, never reset or deleted, so its history stays comparable and auditable.

Creating an experiment atomically (one transaction):
  - experiment row + config version 1 + strategy version 1
  - INITIAL_FUNDING of the PORTFOLIO account and of the BENCHMARK (passive SPY) shadow account
  - the benchmark's single BUY order for its whole capital at the first regular-session open after
    inception: the "explicitly matched investable start time" from the brief. The advisor cannot
    trade before that same open either, so both start investable at the same instant.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from . import clock, config, db, ledger, market_calendar as cal, orders, provenance
from . import money as m

DEFAULT_CAPITAL_CENTS = 50000
DEFAULT_NAME = "Forward paper experiment ($500)"

STRATEGY_V1 = {
    "title": "Evidence-gated discretionary advisor v1",
    "description": (
        "Each morning Claude reviews the account, a small verified universe of liquid US large caps and "
        "unleveraged ETFs, recorded price/regime evidence from audited skills, and dated news. It may propose "
        "long-only market-on-open orders with a written thesis, counterargument, horizon, size, uncertainty and "
        "precommitted exit rules (stop-loss, take-profit and/or maximum holding period, evaluated on completed "
        "daily closes). Holding cash and making no trade are always acceptable outcomes. Deterministic code "
        "validates every proposal against the risk limits; Claude cannot bypass them."),
    "rules": {"universe": "universe.FORWARD_UNIVERSE v2026-10-24.v1", "order_type": "MOO_NEXT_ELIGIBLE_SESSION",
              "exit_rules_required_on_buy": True, "may_hold_cash": True, "activity_target": None},
}


MIN_CAPITAL_CENTS = 1000  # a sanity floor for any limits; the tradable floor below is usually higher


class CapitalTooSmall(ValueError):
    """Raised before anything is written: the experiment could never place a buy under its limits."""

    def __init__(self, minimum_cents: int, params: dict):
        self.minimum_cents = minimum_cents
        super().__init__(f"starting capital must be at least {m.fmt_usd(minimum_cents)}: {capital_floor_reason(params)}")


def capital_floor_reason(params: dict) -> str:
    """Why the minimum is what it is, naming the limit that binds (shown by the CLI and next to the dashboard field):
    'below it a $5.00 buy (the minimum order) could not fit within the 25% max position, so ...'."""
    p = config.normalize(params)
    tradable = config.min_tradable_capital_cents(p)
    if tradable <= MIN_CAPITAL_CENTS:
        return "the smallest starting capital accepted"
    n_star = config.min_fillable_order_cents(p)
    what = ("(the minimum order)" if n_star == m.parse_usd(p["min_order_usd"])
            else "(the smallest that still fills after commission)")
    by_position = config.min_tradable_capital_cents({**p, "min_cash_reserve_pct": "0"})
    if by_position >= tradable:
        limit = f"fit within the {format((Decimal(p['max_position_pct']) * 100).normalize(), 'f')}% max position"
    else:
        limit = f"leave the {format((Decimal(p['min_cash_reserve_pct']) * 100).normalize(), 'f')}% cash reserve"
    return f"below it a {m.fmt_usd(n_star)} buy {what} could not {limit}, so the experiment could never place a buy"


def minimum_capital_cents(params: dict) -> int:
    return max(MIN_CAPITAL_CENTS, config.min_tradable_capital_cents(params))


def _create_tx(conn, *, name: str, starting_capital_cents: int, now: datetime, created_by: str,
               config_params: dict | None, notes: str | None, set_active: bool) -> int:
    if starting_capital_cents <= 0:
        raise ValueError("starting capital must be positive")
    mode = db.db_mode(conn)
    now_iso = clock.iso(now)
    params = config.normalize(config_params or {})
    # An experiment that can never buy would compare "held cash by force" against an invested benchmark;
    # refuse it here so the CLI, the dashboard and init share one rule (Rule 19: the dashboard also checks
    # the same floor while the amount is typed, from the limits the experiment would start with).
    minimum = minimum_capital_cents(params)
    if starting_capital_cents < minimum:
        raise CapitalTooSmall(minimum, params)
    if set_active:
        refuse_if_run_in_progress(conn)  # it would archive the experiment a run is working on
    ident = provenance.code_identity()  # which code created this experiment (immutable)
    cur = conn.execute(
        "INSERT INTO experiments(name, mode, starting_capital_cents, created_at, inception_at, status, benchmark_symbol, created_by, notes,"
        " code_version, code_tree_sha256) VALUES (?,?,?,?,?, 'active', ?, ?, ?, ?, ?)",
        (name, mode, starting_capital_cents, now_iso, now_iso, params["benchmark_symbol"], created_by, notes,
         ident["code_version"], ident["code_tree_sha256"]))
    exp_id = int(cur.lastrowid)
    params = {**params}
    vid = config.insert_version(conn, exp_id, params, effective_from=now_iso, created_by=created_by,
                                reason="initial configuration", now=now_iso)
    conn.execute(
        "INSERT INTO strategy_versions(experiment_id, version, title, description, rules_json, effective_from, created_at, created_by, reason)"
        " VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'initial strategy')",
        (exp_id, STRATEGY_V1["title"], STRATEGY_V1["description"], json.dumps(STRATEGY_V1["rules"], sort_keys=True),
         now_iso, now_iso, created_by))
    for account in ("PORTFOLIO", "BENCHMARK"):
        ledger.append_event(conn, exp_id, account, "INITIAL_FUNDING", effective_at=now_iso, recorded_at=now_iso,
                            cash_delta_cents=starting_capital_cents, idempotency_key=f"funding:{exp_id}:{account}",
                            memo=f"initial funding of {account.lower()} account")
    start = cal.next_session_after(now)
    orders.create(conn, experiment_id=exp_id, account="BENCHMARK", origin="BENCHMARK", symbol=params["benchmark_symbol"],
                  side="BUY", notional_cents=starting_capital_cents, decided_at=now_iso, target_session_date=start.date.isoformat(),
                  client_order_id=f"bench:{exp_id}:initial", now=now_iso, reserved_cash_cents=starting_capital_cents,
                  config_version_id=vid, status="PENDING",
                  status_reason="passive benchmark: invest all capital at the first regular-session open after inception",
                  origin_note="passive benchmark: invest all capital at the first regular-session open after inception")
    conn.execute("UPDATE experiments SET benchmark_start_session=? WHERE id=?", (start.date.isoformat(), exp_id))
    if set_active:
        conn.execute("UPDATE experiments SET status='archived' WHERE id != ? AND status='active'", (exp_id,))
        db.set_state(conn, "active_experiment_id", str(exp_id))
    return exp_id


def create_experiment(conn, *, name: str, starting_capital_cents: int, now: datetime, created_by: str = "cli",
                      config_params: dict | None = None, notes: str | None = None, set_active: bool = True) -> int:
    with db.transaction(conn):
        return _create_tx(conn, name=name, starting_capital_cents=starting_capital_cents, now=now, created_by=created_by,
                          config_params=config_params, notes=notes, set_active=set_active)


def ensure_default_experiment(conn, now: datetime) -> tuple[int, bool]:
    """Return the active experiment, creating the default $500 one if (and only if) the database has
    no experiment at all. The existence check and the creation happen inside one BEGIN IMMEDIATE
    transaction, so two concurrent `init` calls cannot both create (and fund) an experiment."""
    with db.transaction(conn):
        n = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        if n:
            return active_experiment_id(conn), False
        exp_id = _create_tx(conn, name=DEFAULT_NAME, starting_capital_cents=DEFAULT_CAPITAL_CENTS, now=now,
                            created_by="init", config_params=None, notes="Default experiment created on first run.",
                            set_active=True)
        return exp_id, True


def active_experiment_id(conn) -> int | None:
    v = db.get_state(conn, "active_experiment_id")
    if v is not None:
        return int(v)
    r = conn.execute("SELECT id FROM experiments ORDER BY id DESC LIMIT 1").fetchone()
    return None if r is None else int(r["id"])


class RunInProgress(ValueError):
    """A morning run holds the active experiment's lock. Switching the active experiment, or creating one (which
    archives the active one), would let that run's decisions land on an experiment that is no longer active."""


def refuse_if_run_in_progress(conn) -> None:
    """SYNC WARNING: the lock name and expiry mirror runs.lock_name/acquire_lock_tx; an expired lock does not
    count (a later run takes it over). The dashboard also disables the controls while a run holds the lock."""
    aid = active_experiment_id(conn)
    if aid is None:
        return
    lock = conn.execute("SELECT run_id, expires_at FROM locks WHERE name=?", (f"experiment:{aid}",)).fetchone()
    if lock is not None and lock["expires_at"] > clock.now_iso():
        raise RunInProgress(f"morning run #{lock['run_id']} is in progress on experiment #{aid}; change experiments after it finishes")


def set_active(conn, experiment_id: int) -> None:
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM experiments WHERE id=?", (experiment_id,)).fetchone() is None:
            raise ValueError(f"no experiment {experiment_id}")
        refuse_if_run_in_progress(conn)
        db.set_state(conn, "active_experiment_id", str(experiment_id))
        # Keep statuses in step with the pointer (as _create_tx does): exactly one experiment is 'active'.
        # Only the status flag changes; capital, inception and history stay immutable.
        conn.execute("UPDATE experiments SET status = CASE WHEN id = ? THEN 'active' ELSE 'archived' END", (experiment_id,))


def get(conn, experiment_id: int):
    return conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()


def all_experiments(conn):
    return conn.execute("SELECT * FROM experiments ORDER BY id").fetchall()


def current_strategy(conn, experiment_id: int, at_iso: str | None = None):
    at_iso = at_iso or clock.now_iso()
    return conn.execute("SELECT * FROM strategy_versions WHERE experiment_id=? AND effective_from<=? ORDER BY version DESC LIMIT 1",
                        (experiment_id, at_iso)).fetchone() or conn.execute(
        "SELECT * FROM strategy_versions WHERE experiment_id=? ORDER BY version LIMIT 1", (experiment_id,)).fetchone()
