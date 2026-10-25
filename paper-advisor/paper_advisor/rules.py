"""Precommitted exit rules for open positions.

Execution policy (README "Exits"): rules are evaluated only on COMPLETED daily closes. If a close
breaches a rule, a market-on-open SELL ALL is queued for the next session (decided_at = that close).
We deliberately do NOT simulate intraday stop orders from daily bars: a daily bar cannot tell us
whether the low came before the high, and a stop order is not guaranteed to fill at its stop price.
Close-confirmation avoids both problems at the cost of reacting one session later; gaps at the next
open are realized as they happened (the fill uses the actual open, not the rule level).

Lifecycle: AWAITING_ENTRY (BUY order pending) -> ACTIVE (after the fill; reference price = average
cost per share) -> TRIGGERED (exit queued) -> CLOSED (position flat). An exit that expires unfilled
re-arms the rule (TRIGGERED -> ACTIVE) so it can fire again on the next close. A newer decision on
the same symbol SUPERSEDES the active rule prospectively; nothing is edited retroactively.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from . import market_calendar as cal

REF_QUANT = Decimal("0.000001")


def _dec(x):
    return None if x is None else Decimal(str(x))


def create_awaiting(conn, experiment_id: int, symbol: str, decision_id: int, stop, tp, max_hold, invalidation: str, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO position_rules(experiment_id, symbol, decision_id, stop_loss_pct, take_profit_pct, max_holding_sessions,"
        " invalidation, effective_from, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'AWAITING_ENTRY', ?, ?)",
        (experiment_id, symbol, decision_id, None if stop is None else str(stop), None if tp is None else str(tp),
         max_hold, invalidation or "", now, now, now))
    return int(cur.lastrowid)


def _set(conn, rule_id, status, reason, now, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE position_rules SET status=?, status_reason=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
                 (status, reason, now, *cols.values(), rule_id))


def active_for(conn, experiment_id: int, symbol: str):
    return conn.execute("SELECT * FROM position_rules WHERE experiment_id=? AND symbol=? AND status IN ('ACTIVE','TRIGGERED') ORDER BY id DESC",
                        (experiment_id, symbol)).fetchall()


def stop_level(rule) -> Decimal | None:
    """Effective stop price: the higher of the percentage stop and the carried floor."""
    ref = _dec(rule["reference_price"])
    pct_level = ref * (1 - Decimal(rule["stop_loss_pct"])) if ref is not None and rule["stop_loss_pct"] is not None else None
    floor = _dec(rule["stop_floor_price"])
    levels = [x for x in (pct_level, floor) if x is not None]
    return max(levels) if levels else None


def _carried_floor(existing_rules) -> Decimal | None:
    levels = [lvl for lvl in (stop_level(r) for r in existing_rules) if lvl is not None]
    return max(levels) if levels else None


def on_buy_fill(conn, experiment_id: int, symbol: str, decision_id: int | None, avg_cost: Decimal, session_date: str, now: str) -> None:
    if decision_id is None:
        return
    awaiting = conn.execute("SELECT * FROM position_rules WHERE decision_id=? AND status='AWAITING_ENTRY'", (decision_id,)).fetchall()
    if not awaiting:
        return
    existing = active_for(conn, experiment_id, symbol)
    # Adding to a position (especially averaging down) lowers the average cost, which would lower a
    # percentage stop. Carry the old effective stop level forward as a floor so it can only rise.
    floor = _carried_floor(existing)
    for old in existing:
        _set(conn, old["id"], "SUPERSEDED", f"superseded by rules of decision {decision_id}", now)
    ref = avg_cost.quantize(REF_QUANT, rounding=ROUND_HALF_EVEN)
    for r in awaiting:
        _set(conn, r["id"], "ACTIVE", "entry filled", now, reference_price=str(ref), entry_session_date=session_date,
             stop_floor_price=None if floor is None else str(floor.quantize(REF_QUANT, rounding=ROUND_HALF_EVEN)))


def on_entry_not_filled(conn, decision_id: int | None, reason: str, now: str) -> None:
    if decision_id is None:
        return
    for r in conn.execute("SELECT * FROM position_rules WHERE decision_id=? AND status='AWAITING_ENTRY'", (decision_id,)).fetchall():
        _set(conn, r["id"], "CLOSED", reason, now)


def on_position_closed(conn, experiment_id: int, symbol: str, now: str) -> None:
    for r in active_for(conn, experiment_id, symbol):
        _set(conn, r["id"], "CLOSED", "position closed", now)


def on_exit_expired(conn, rule_id: int | None, now: str) -> None:
    if rule_id is None:
        return
    r = conn.execute("SELECT * FROM position_rules WHERE id=?", (rule_id,)).fetchone()
    if r is not None and r["status"] == "TRIGGERED":
        _set(conn, rule_id, "ACTIVE", "exit order expired unfilled; rule re-armed", now)


def on_split(conn, experiment_id: int, symbol: str, num: int, den: int, now: str) -> None:
    for r in active_for(conn, experiment_id, symbol):
        cols = {}
        for col in ("reference_price", "stop_floor_price"):
            if r[col] is not None:
                cols[col] = str((Decimal(r[col]) * den / num).quantize(REF_QUANT, rounding=ROUND_HALF_EVEN))
        if cols:
            _set(conn, r["id"], r["status"], f"price levels adjusted for {num}:{den} split", now, **cols)


def replace_from_hold(conn, experiment_id: int, symbol: str, decision_id: int, stop, tp, max_hold, invalidation: str, now: str) -> int | None:
    """A HOLD decision may restate exit rules for a held position, effective from now on. The old
    effective stop level is carried as a floor (see stop_level) so a restatement cannot lower it."""
    current = active_for(conn, experiment_id, symbol)
    ref = current[0]["reference_price"] if current else None
    entry = current[0]["entry_session_date"] if current else None
    floor = _carried_floor(current)
    for old in current:
        _set(conn, old["id"], "SUPERSEDED", f"restated by HOLD decision {decision_id}", now)
    cur = conn.execute(
        "INSERT INTO position_rules(experiment_id, symbol, decision_id, stop_loss_pct, take_profit_pct, max_holding_sessions,"
        " invalidation, reference_price, stop_floor_price, entry_session_date, effective_from, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?, 'ACTIVE', ?, ?)",
        (experiment_id, symbol, decision_id, None if stop is None else str(stop), None if tp is None else str(tp), max_hold,
         invalidation or "", ref, None if floor is None else str(floor.quantize(REF_QUANT, rounding=ROUND_HALF_EVEN)),
         entry, now, now, now))
    return int(cur.lastrowid)


def evaluate(rule, close: Decimal, session_date: str) -> list[str]:
    """Pure: which rules a completed close triggers. Ordered by severity."""
    hits = []
    ref = _dec(rule["reference_price"])
    lvl = stop_level(rule)
    if lvl is not None and close <= lvl:
        hits.append("STOP_LOSS")
    if rule["max_holding_sessions"] is not None and rule["entry_session_date"]:
        from datetime import date
        held = cal.count_sessions_after(date.fromisoformat(rule["entry_session_date"]), date.fromisoformat(session_date))
        if held >= int(rule["max_holding_sessions"]):
            hits.append("MAX_HOLDING")
    if ref is not None and rule["take_profit_pct"] is not None:
        if close >= ref * (1 + Decimal(rule["take_profit_pct"])):
            hits.append("TAKE_PROFIT")
    return hits


def describe(rule) -> str:
    parts = []
    ref = _dec(rule["reference_price"])
    eff = stop_level(rule)
    if rule["stop_loss_pct"] is not None or eff is not None:
        pct = f"stop {Decimal(rule['stop_loss_pct']) * 100:.1f}%" if rule["stop_loss_pct"] is not None else "stop floor"
        lvl = f" (close <= {eff.quantize(Decimal('0.01'))})" if eff is not None else ""
        parts.append(f"{pct}{lvl}")
    if rule["take_profit_pct"] is not None:
        lvl = f" (close >= {(ref * (1 + Decimal(rule['take_profit_pct']))).quantize(Decimal('0.01'))})" if ref else ""
        parts.append(f"target +{Decimal(rule['take_profit_pct']) * 100:.1f}%{lvl}")
    if rule["max_holding_sessions"] is not None:
        parts.append(f"max {rule['max_holding_sessions']} sessions")
    return "; ".join(parts) or "none"
