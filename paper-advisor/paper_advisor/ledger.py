"""Append-only accounting ledger.

Every change to cash or shares in an account is one row in ledger_events, in a per-(experiment,
account) sequence. Holdings, cost basis, realized P&L and cash are never stored as mutable state;
they are recomputed by replaying this ledger (accounting.replay). That is what makes the record
auditable: you cannot "fix" a balance without leaving a corrective event behind.

Accounts: 'PORTFOLIO' is the advisor's simulated account; 'BENCHMARK' is the passive SPY shadow
account funded with the same capital. They never share cash or positions.

Invariants enforced here at append time (and again by the schema and by replay):
- cash never goes negative (no margin), share quantities never go negative (no short sales);
- an idempotency key identifies an event uniquely; re-appending the same key with the same payload
  is a no-op that returns the original id (safe reruns), with a different payload it is an error.
"""
from __future__ import annotations

import hashlib
import json

from . import db

REVERSIBLE_TYPES = ("CASH_DIVIDEND", "ADJUSTMENT")


class LedgerError(RuntimeError):
    pass


def tx(conn):
    return db.transaction(conn)


def _payload_sha(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def append_event(conn, experiment_id: int, account: str, event_type: str, *, effective_at: str, recorded_at: str,
                 idempotency_key: str, session_date: str | None = None, symbol: str | None = None,
                 quantity_delta_micro: int = 0, cash_delta_cents: int = 0, fee_cents: int = 0, price=None,
                 split_num: int | None = None, split_den: int | None = None, fill_id: int | None = None,
                 order_id: int | None = None, corporate_action_id: int | None = None,
                 reverses_event_id: int | None = None, memo: str | None = None, run_id: int | None = None) -> int:
    if not conn.in_transaction:
        raise LedgerError("append_event must run inside the caller's transaction so the event and the "
                          "row that justifies it (fill, corporate action, funding) commit atomically")
    if account not in ("PORTFOLIO", "BENCHMARK"):
        raise LedgerError(f"unknown account {account!r}")
    payload = {
        "experiment_id": experiment_id, "account": account, "event_type": event_type, "effective_at": effective_at,
        "session_date": session_date, "symbol": symbol, "quantity_delta_micro": int(quantity_delta_micro),
        "cash_delta_cents": int(cash_delta_cents), "fee_cents": int(fee_cents),
        "price": None if price is None else str(price), "split_num": split_num, "split_den": split_den,
        "fill_id": fill_id, "order_id": order_id, "corporate_action_id": corporate_action_id,
        "reverses_event_id": reverses_event_id,
    }
    sha = _payload_sha(payload)
    existing = conn.execute("SELECT id, payload_sha256 FROM ledger_events WHERE idempotency_key=?",
                            (idempotency_key,)).fetchone()
    if existing is not None:
        if existing["payload_sha256"] != sha:
            raise LedgerError(f"idempotency key {idempotency_key!r} was already used for a different event")
        return int(existing["id"])

    cash_now = conn.execute(
        "SELECT COALESCE(SUM(cash_delta_cents), 0) FROM ledger_events WHERE experiment_id=? AND account=?",
        (experiment_id, account)).fetchone()[0]
    if cash_now + int(cash_delta_cents) < 0:
        raise LedgerError(f"event would make {account} cash negative ({cash_now} + {cash_delta_cents} cents); "
                          "margin is not allowed")
    if symbol is not None and int(quantity_delta_micro) != 0:
        qty_now = conn.execute(
            "SELECT COALESCE(SUM(quantity_delta_micro), 0) FROM ledger_events WHERE experiment_id=? AND account=? AND symbol=?",
            (experiment_id, account, symbol)).fetchone()[0]
        if qty_now + int(quantity_delta_micro) < 0:
            raise LedgerError(f"event would make {symbol} quantity negative ({qty_now} + {quantity_delta_micro}); "
                              "short sales are not allowed")
    if event_type == "ADJUSTMENT" and int(quantity_delta_micro) != 0:
        raise LedgerError("v1 ADJUSTMENT events are cash-only; share corrections need a dedicated, reviewed event type")

    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM ledger_events WHERE experiment_id=? AND account=?",
                       (experiment_id, account)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO ledger_events(experiment_id, account, seq, event_type, effective_at, session_date, recorded_at,"
        " symbol, quantity_delta_micro, cash_delta_cents, fee_cents, price, split_num, split_den, fill_id, order_id,"
        " corporate_action_id, reverses_event_id, idempotency_key, payload_sha256, memo, run_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (experiment_id, account, seq, event_type, effective_at, session_date, recorded_at, symbol,
         int(quantity_delta_micro), int(cash_delta_cents), int(fee_cents), None if price is None else str(price),
         split_num, split_den, fill_id, order_id, corporate_action_id, reverses_event_id, idempotency_key, sha, memo, run_id))
    return int(cur.lastrowid)


def reverse_event(conn, experiment_id: int, account: str, event_id: int, *, recorded_at: str, memo: str,
                  run_id: int | None = None) -> int:
    """Record an explicit corrective REVERSAL of a cash-only event. The original row stays in place;
    the reversal is effective when it is recorded (history before that instant is not rewritten,
    the correction is visible as of the moment it was made)."""
    row = conn.execute("SELECT * FROM ledger_events WHERE id=? AND experiment_id=? AND account=?",
                       (event_id, experiment_id, account)).fetchone()
    if row is None:
        raise LedgerError(f"event {event_id} not found")
    if row["event_type"] not in REVERSIBLE_TYPES:
        raise LedgerError(f"{row['event_type']} events are not reversible in v1; record an explicit ADJUSTMENT "
                          "with a memo and mark the affected valuations provisional")
    return append_event(conn, experiment_id, account, "REVERSAL", effective_at=recorded_at, recorded_at=recorded_at,
                        idempotency_key=f"reversal:{event_id}", session_date=None, symbol=row["symbol"],
                        quantity_delta_micro=-int(row["quantity_delta_micro"]),
                        cash_delta_cents=-int(row["cash_delta_cents"]), reverses_event_id=event_id,
                        memo=memo, run_id=run_id)


def events(conn, experiment_id: int, account: str, through_seq: int | None = None):
    sql = "SELECT * FROM ledger_events WHERE experiment_id=? AND account=?"
    args: list = [experiment_id, account]
    if through_seq is not None:
        sql += " AND seq <= ?"
        args.append(through_seq)
    return conn.execute(sql + " ORDER BY seq", args).fetchall()
