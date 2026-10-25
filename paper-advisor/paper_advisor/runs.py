"""Run records and the experiment lock (full morning lifecycle lives in morning.py).

A run row is created for every morning/review invocation. The (experiment, kind, analysis session,
version) uniqueness constraint is what prevents a rerun of the same morning from creating a second
analysis: a deliberate new analysis must take version N+1 and link to the run it supersedes.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
from datetime import timedelta

from . import clock, db, provenance


class LockBusy(RuntimeError):
    pass


def create_run_row(conn, experiment_id: int, run_kind: str, analysis_session_date: str, *, version: int, mode: str, now: str,
                   supersedes_run_id: int | None = None, model_provider: str = "unknown", model_id: str = "unknown",
                   model_effort: str = "unknown", config_version_id: int | None = None, strategy_version_id: int | None = None) -> int:
    ident = provenance.code_identity()  # every run records the code that ran, whoever creates it
    cur = conn.execute(
        "INSERT INTO runs(experiment_id, run_kind, analysis_session_date, version, supersedes_run_id, mode, status, started_at,"
        " model_provider, model_id, model_effort, config_version_id, strategy_version_id, code_version, code_tree_sha256)"
        " VALUES (?,?,?,?,?,?, 'started', ?,?,?,?,?,?,?,?)",
        (experiment_id, run_kind, analysis_session_date, version, supersedes_run_id, mode, now, model_provider, model_id,
         model_effort, config_version_id, strategy_version_id, ident["code_version"], ident["code_tree_sha256"]))
    return int(cur.lastrowid)


def log(conn, run_id: int, kind: str, message: str, details: dict | None = None, now: str | None = None) -> None:
    conn.execute("INSERT INTO run_events(run_id, at, kind, message, details_json) VALUES (?,?,?,?,?)",
                 (run_id, now or clock.now_iso(), kind, message, json.dumps(details or {}, default=str, sort_keys=True)))


def set_status(conn, run_id: int, status: str, now: str, error: str | None = None, **cols) -> None:
    sets = "".join(f", {k}=?" for k in cols)
    finished = now if status in ("completed", "failed", "interrupted") else None
    conn.execute(f"UPDATE runs SET status=?, error=COALESCE(?, error), finished_at=COALESCE(?, finished_at){sets} WHERE id=?",
                 (status, error, finished, *cols.values(), run_id))


def get(conn, run_id: int):
    return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


# ---------------------------------------------------------------------------------------------
# Lock. One row per experiment; acquisition happens inside BEGIN IMMEDIATE, so two processes can
# never both believe they hold it. A holder must present its token on every later step.
# ---------------------------------------------------------------------------------------------

def lock_name(experiment_id: int) -> str:
    return f"experiment:{experiment_id}"


def acquire_lock_tx(conn, experiment_id: int, run_id: int | None, now, ttl_minutes: int, force: bool = False) -> tuple[str, dict | None]:
    """Must be called inside a transaction. Returns (token, previous_holder_if_taken_over)."""
    name = lock_name(experiment_id)
    row = conn.execute("SELECT * FROM locks WHERE name=?", (name,)).fetchone()
    now_iso = clock.iso(now)
    prev = None
    if row is not None:
        alive = row["expires_at"] > now_iso
        if alive and not force:
            raise LockBusy(f"experiment {experiment_id} is locked by run {row['run_id']} (pid {row['owner_pid']} on "
                           f"{row['owner_host']}) until {row['expires_at']}")
        prev = dict(row)
        conn.execute("DELETE FROM locks WHERE name=?", (name,))
    token = secrets.token_hex(16)
    conn.execute("INSERT INTO locks(name, run_id, token, acquired_at, heartbeat_at, expires_at, owner_pid, owner_host) VALUES (?,?,?,?,?,?,?,?)",
                 (name, run_id, token, now_iso, now_iso, clock.iso(now + timedelta(minutes=ttl_minutes)), os.getpid(), platform.node()))
    return token, prev


def set_lock_run(conn, experiment_id: int, token: str, run_id: int) -> None:
    conn.execute("UPDATE locks SET run_id=? WHERE name=? AND token=?", (run_id, lock_name(experiment_id), token))


def check_lock(conn, experiment_id: int, token: str, now, ttl_minutes: int, run_id: int | None = None) -> None:
    """Verify the caller holds the lock and extend it (heartbeat). Must be inside a transaction."""
    row = conn.execute("SELECT * FROM locks WHERE name=?", (lock_name(experiment_id),)).fetchone()
    if row is None or row["token"] != token:
        raise LockBusy("this run does not hold the experiment lock (it expired or was taken over); start a new run")
    if row["expires_at"] <= clock.iso(now):
        raise LockBusy("the experiment lock expired; start a new run")
    if run_id is not None and row["run_id"] not in (None, run_id):
        raise LockBusy(f"lock belongs to run {row['run_id']}, not run {run_id}")
    conn.execute("UPDATE locks SET heartbeat_at=?, expires_at=? WHERE name=?",
                 (clock.iso(now), clock.iso(now + timedelta(minutes=ttl_minutes)), lock_name(experiment_id)))


def release_lock(conn, experiment_id: int, token: str) -> None:
    conn.execute("DELETE FROM locks WHERE name=? AND token=?", (lock_name(experiment_id), token))


def current_lock(conn, experiment_id: int):
    return conn.execute("SELECT * FROM locks WHERE name=?", (lock_name(experiment_id),)).fetchone()
