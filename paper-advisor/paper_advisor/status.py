"""/paper-status: show state WITHOUT creating decisions, orders, fills or snapshots.

Opens the database READ-ONLY (SQLite mode=ro), so this command cannot write even by accident. It
does not settle pending orders: settlement creates fills, and fills are trades. It reports what the
next morning run will settle instead.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from . import accounting, clock, config, db, experiments, market_calendar as cal, metrics, orders, prices, quality, reports, universe
from . import money as m


def gather(conn, now=None) -> dict:
    now = now or clock.now_utc()
    exp_id = experiments.active_experiment_id(conn)
    if exp_id is None:
        return {"initialized": False}
    exp = experiments.get(conn, exp_id)
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    marks = {s: Decimal(b["close"]) for s in st.positions if (b := prices.latest_bar(conn, s)) is not None}
    v = accounting.valuation(st, marks)
    mt = metrics.compute(conn, exp_id)
    last = cal.last_completed_session(now, cfg.bar_finalization_minutes)
    pend = orders.pending(conn, exp_id)
    settle_ready = [o for o in pend if last and o["target_session_date"] <= last.date.isoformat()]
    dq = quality.assess(conn, universe.symbols(conn), now, cfg.bar_finalization_minutes)
    lock = conn.execute("SELECT * FROM locks WHERE name=?", (f"experiment:{exp_id}",)).fetchone()
    runs_ = conn.execute("SELECT id, run_kind, analysis_session_date, version, mode, status, started_at, finished_at, model_id, error"
                         " FROM runs WHERE experiment_id=? ORDER BY id DESC LIMIT 5", (exp_id,)).fetchall()
    return {
        "initialized": True, "PAPER_TRADING": True, "experiment": dict(exp), "market": cal.market_status(now),
        "next_orders_target": cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds)).date.isoformat(),
        "live_marks_note": "marked at the latest stored end-of-day closes, not live quotes",
        "cash_cents": st.cash_cents, "market_value_cents": v.market_value_cents, "equity_cents": v.equity_cents,
        "unrealized_cents": v.unrealized_cents, "realized_cents": st.realized_cents, "dividends_cents": st.dividends_cents,
        "positions": [{"symbol": p.symbol, "qty": m.qty_to_str(p.qty_micro), "last_close": None if p.price is None else str(p.price),
                       "value": m.fmt_usd(p.market_value_cents), "unrealized": m.fmt_usd(p.unrealized_cents)} for p in v.positions],
        "pending_orders": [{"id": o["id"], "account": o["account"], "origin": o["origin"], "side": o["side"], "symbol": o["symbol"],
                            "notional": None if o["notional_cents"] is None else m.fmt_usd(o["notional_cents"]),
                            "quantity": "ALL" if o["sell_all"] else (None if o["quantity_micro"] is None else m.qty_to_str(o["quantity_micro"])),
                            "target": o["target_session_date"], "decided_at": o["decided_at"]} for o in pend],
        "settleable_on_next_morning_run": [o["id"] for o in settle_ready],
        "metrics": {k: mt[k] for k in ("total_return", "spy_benchmark_return", "excess_vs_spy", "sessions_valued", "closed_trades",
                                         "max_drawdown", "provisional_sessions", "as_of_session")},
        "data_freshness": {s: {"status": q["status"], "last_bar": q["last_bar_date"]} for s, q in dq.items()},
        "lock": None if lock is None else dict(lock), "recent_runs": [dict(r) for r in runs_],
    }


def _day(iso_date: str | None) -> str:
    if not iso_date:
        return "n/a"
    x = date.fromisoformat(iso_date)
    return f"{x.strftime('%a %b')} {x.day}, {x.year}"


def render(g: dict) -> str:
    """The /paper-status text, which the owner reads (relayed by Claude): states, times and dates in words, like the
    dashboard; order and run numbers stay because the dashboard shows the same numbers."""
    if not g.get("initialized"):
        return "paper-advisor is not initialized. Run: paper-advisor/bin/pa init"
    e = g["experiment"]
    mt = g["metrics"]
    tz = cal.ZoneInfo("America/Los_Angeles")
    bench = e["benchmark_symbol"]
    pct = lambda x: "n/a" if x is None else f"{x * 100:+.2f}%"
    fresh = sum(1 for x in g["data_freshness"].values() if x["status"] == "FRESH")
    ny = clock.parse_iso(g["market"]["now_utc"]).astimezone(cal.NY)
    lines = ["PAPER TRADING (simulated, fake money)",
             f"Experiment #{e['id']} {e['name']} ({'fixture demo, synthetic data' if e['mode'] == 'fixture' else 'forward paper test'}), "
             f"started {reports.when(e['inception_at'], tz)} with {m.fmt_usd(e['starting_capital_cents'])}",
             f"Market: {cal.state_words(g['market'])} ({ny.strftime('%a %b')} {ny.day}, {ny.strftime('%-I:%M %p')} ET); "
             f"orders decided now target the {_day(g['next_orders_target'])} open",
             f"Equity {m.fmt_usd(g['equity_cents'])} | cash {m.fmt_usd(g['cash_cents'])} | positions {m.fmt_usd(g['market_value_cents'])} | "
             f"unrealized {m.fmt_usd(g['unrealized_cents'])} | realized {m.fmt_usd(g['realized_cents'])}; {g['live_marks_note']}",
             f"Since inception: {pct(mt['total_return'])} vs {bench} {pct(mt['spy_benchmark_return'])} vs keeping cash +0.00% "
             f"({mt['sessions_valued']} session{'s' if mt['sessions_valued'] != 1 else ''} valued, {mt['closed_trades']} closed "
             f"trade{'s' if mt['closed_trades'] != 1 else ''}; provisional valuations: {mt['provisional_sessions']})",
             f"Data: {fresh}/{len(g['data_freshness'])} symbols fresh",
             f"Database: {db.engine_words(short=True)}"]
    lines.append("Positions: " + (", ".join(f"{p['symbol']} {p['qty']} shares ({p['value']}, unrealized {p['unrealized']})" for p in g["positions"]) or "none"))
    lines.append("Pending orders: " + ("; ".join(f"#{o['id']} {'passive benchmark' if o['origin'] == 'BENCHMARK' else o['origin'].lower()} "
                                                 f"{o['side']} {o['symbol']} {o['notional'] or o['quantity']} at the {_day(o['target'])} open"
                                                 for o in g["pending_orders"]) or "none"))
    if g["settleable_on_next_morning_run"]:
        lines.append(f"Orders {g['settleable_on_next_morning_run']} have target sessions that are complete: the next /paper-morning will settle them.")
    if g["lock"]:
        lines.append(f"Run #{g['lock']['run_id']} is in progress (its lock lasts until {reports.when(g['lock']['expires_at'], tz)})")
    for r in g["recent_runs"][:3]:
        kind = "review" if r["run_kind"] == "review" else ("morning rerun (reconcile only)" if r["mode"] == "reconcile_only" else "morning analysis")
        lines.append(f"Run #{r['id']}: {kind} for the {_day(r['analysis_session_date'])} open"
                     + (f", attempt {r['version']}" if r["version"] > 1 else "") + f": {r['status'].replace('_', ' ')}"
                     + (f" ({r['error']})" if r["error"] else ""))
    return "\n".join(lines)
