"""Simulated order lifecycle.

States (the schema trigger orders_legal_transition enforces the same graph):
    PROPOSED -> REJECTED                       failed deterministic validation, never eligible to fill
    PROPOSED -> PENDING                        accepted; waiting for its target session's open
    PENDING  -> FILLED | CANCELED | EXPIRED    terminal
A recommendation is not a fill: an order only becomes FILLED when broker_sim finds the official
daily bar of its target session.

Cash reservation: a PENDING BUY reserves its full notional (which already includes the room for
commission, see broker_sim). New proposals are validated against cash minus all reservations.
"""
from __future__ import annotations

import json

TERMINAL = ("REJECTED", "FILLED", "CANCELED", "EXPIRED")


class OrderError(RuntimeError):
    pass


def create(conn, *, experiment_id: int, account: str, origin: str, symbol: str, side: str, decided_at: str,
           target_session_date: str, client_order_id: str, now: str, notional_cents: int | None = None,
           quantity_micro: int | None = None, sell_all: bool = False, reserved_cash_cents: int = 0,
           run_id: int | None = None, decision_id: int | None = None, rule_id: int | None = None,
           config_version_id: int | None = None, status: str = "PROPOSED", status_reason: str | None = None,
           origin_note: str | None = None) -> int:
    existing = conn.execute("SELECT id FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
    if existing is not None:
        return int(existing["id"])
    cur = conn.execute(
        "INSERT INTO orders(experiment_id, account, run_id, decision_id, rule_id, origin, client_order_id, symbol, side,"
        " notional_cents, quantity_micro, sell_all, decided_at, target_session_date, reserved_cash_cents, status,"
        " status_reason, origin_note, config_version_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (experiment_id, account, run_id, decision_id, rule_id, origin, client_order_id, symbol, side, notional_cents,
         quantity_micro, int(bool(sell_all)), decided_at, target_session_date, reserved_cash_cents if side == "BUY" else 0,
         status, status_reason, origin_note, config_version_id, now, now))
    oid = int(cur.lastrowid)
    _event(conn, oid, None, status, status_reason or "created", now, None, {})
    return oid


def _event(conn, order_id, from_status, to_status, reason, now, effective_at, details):
    conn.execute("INSERT INTO order_events(order_id, recorded_at, effective_at, from_status, to_status, reason, details_json)"
                 " VALUES (?,?,?,?,?,?,?)", (order_id, now, effective_at, from_status, to_status, reason,
                                              json.dumps(details or {}, sort_keys=True, default=str)))


def transition(conn, order_id: int, to_status: str, reason: str, now: str, effective_at: str | None = None,
               details: dict | None = None) -> None:
    o = get(conn, order_id)
    if o is None:
        raise OrderError(f"order {order_id} not found")
    if o["status"] == to_status and to_status in TERMINAL:
        return  # idempotent re-application (e.g. settlement rerun)
    conn.execute("UPDATE orders SET status=?, status_reason=?, updated_at=? WHERE id=?", (to_status, reason, now, order_id))
    _event(conn, order_id, o["status"], to_status, reason, now, effective_at, details)


def set_quantity(conn, order_id: int, new_qty_micro: int, reason: str, now: str, details: dict | None = None) -> None:
    o = get(conn, order_id)
    if o["status"] != "PENDING" or o["side"] != "SELL" or o["sell_all"]:
        raise OrderError("only pending quantity SELL orders can be quantity-adjusted")
    conn.execute("UPDATE orders SET quantity_micro=?, updated_at=? WHERE id=?", (new_qty_micro, now, order_id))
    _event(conn, order_id, "PENDING", "PENDING", reason, now, None,
           {**(details or {}), "old_quantity_micro": o["quantity_micro"], "new_quantity_micro": new_qty_micro})


def get(conn, order_id: int):
    return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def pending(conn, experiment_id: int, account: str | None = None, symbol: str | None = None,
            target_session: str | None = None):
    sql = "SELECT * FROM orders WHERE experiment_id=? AND status='PENDING'"
    args: list = [experiment_id]
    for col, val in (("account", account), ("symbol", symbol), ("target_session_date", target_session)):
        if val is not None:
            sql += f" AND {col}=?"
            args.append(val)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def reserved_cash_cents(conn, experiment_id: int, account: str = "PORTFOLIO") -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(reserved_cash_cents), 0) FROM orders WHERE experiment_id=? AND account=? AND status='PENDING' AND side='BUY'",
        (experiment_id, account)).fetchone()[0])


def pending_sell_qty(conn, experiment_id: int, symbol: str, held_qty: int) -> int:
    total = 0
    for o in pending(conn, experiment_id, account="PORTFOLIO", symbol=symbol):
        if o["side"] == "SELL":
            total += held_qty if o["sell_all"] else int(o["quantity_micro"])
    return min(total, held_qty) if total > held_qty else total


def advisor_orders_for_session(conn, experiment_id: int, target_session: str) -> int:
    """Advisor-originated orders already created for a session, excluding rejected proposals.
    Canceled orders still count: the limit is on new orders placed, so place/cancel/place churn
    cannot exceed it. Precommitted RULE exits and the BENCHMARK order are not advisor decisions."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM orders WHERE experiment_id=? AND account='PORTFOLIO' AND origin='ADVISOR'"
        " AND target_session_date=? AND status != 'REJECTED'", (experiment_id, target_session)).fetchone()[0])


def all_for_experiment(conn, experiment_id: int, account: str | None = "PORTFOLIO"):
    if account is None:
        return conn.execute("SELECT * FROM orders WHERE experiment_id=? ORDER BY id", (experiment_id,)).fetchall()
    return conn.execute("SELECT * FROM orders WHERE experiment_id=? AND account=? ORDER BY id", (experiment_id, account)).fetchall()
