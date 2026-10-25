"""Concise Markdown reports for each morning run (and a review report).

The report is generated from the database at finish time, so it reflects what was actually
persisted (decisions, rejections, fills recorded later), not what the session intended.
Dates are written as "October 23, 2026 (Friday)" so the audited data-quality-checker skill can
verify weekday/date consistency as a lint pass.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

from . import clock, config, db, market_calendar as cal, metrics
from . import money as m


def long_date(d: str | None) -> str:
    if not d:
        return "n/a"
    x = date.fromisoformat(d)
    return f"{x.strftime('%B')} {x.day}, {x.year} ({x.strftime('%A')})"


def _signed_usd(cents: int) -> str:
    """A gain or loss with its sign ("+$14.41", "-$10.30", "$0.00"): the account row read "-$10.30" realized next to a plain
    "$14.41" unrealized (capture, round 13). SYNC WARNING: the dashboard shows P&L the same way (pages.signed_usd)."""
    return ("+" if cents > 0 else "") + m.fmt_usd(int(cents))


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _pts(x):
    # A gap between two returns is in percentage points, as the dashboard and the line under the table say.
    return "n/a" if x is None else f"{x * 100:+.2f} pts"


def when(iso: str | None, tz) -> str:
    """'Tue Dec 2, 2025, 6:30 AM PT': stored UTC instants as the owner reads them (the dashboard renders this
    report too, and ISO stamps there read as a debug dump). The weekday keeps dates checkable by the audited
    data-quality-checker skill."""
    if not iso:
        return "n/a"
    t = clock.parse_iso(iso).astimezone(tz)
    zone = "PT" if str(tz) == "America/Los_Angeles" else t.strftime("%Z")
    return f"{t.strftime('%a %b')} {t.day}, {t.year}, {t.strftime('%-I:%M %p')} {zone}"


def words(code: str | None) -> str:
    """Stored reason codes as words: 'no_bar_for_target_session' -> 'no bar for target session'."""
    return (code or "").replace(";", ", ").replace("_", " ").replace(":", " ")


def morning_report(conn, exp_id: int, run, now, extra_notes: list[str] | None = None) -> str:
    mt = metrics.compute(conn, exp_id)
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    exp = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    status = cal.market_status(now)
    tz = cal.ZoneInfo(cfg.display_timezone)
    # "attempt 1" on a first analysis read as noise; later attempts say which one they are.
    kind = "reconcile-only rerun" if run["mode"] == "reconcile_only" else (f"attempt {run['version']}" if run["version"] > 1 else "")
    title = "PAPER TRADING · FIXTURE DEMO (synthetic data)" if exp["mode"] == "fixture" else "PAPER TRADING"
    # The benchmark is named as the experiment records it: the fixture's is the synthetic DEMO.SPY, never "SPY".
    bench = exp["benchmark_symbol"]
    lines = [f"# {title} · Morning report, run {run['id']}" + (f" ({kind})" if kind else ""), "",
             "> Simulated trading with fake money. No real orders exist. Prices are end-of-day daily bars, not live quotes.", "",
             f"- Experiment: **{exp['name']}** (#{exp_id}, {'fixture demo' if exp['mode'] == 'fixture' else 'forward paper test'}), "
             f"starting capital {m.fmt_usd(exp['starting_capital_cents'])}",
             f"- Generated: {when(clock.iso(now), tz)} ({now.astimezone(cal.NY).strftime('%-I:%M %p')} ET)",
             f"- Market: {cal.state_words(status)}; orders from this run target the "
             f"**{long_date(run['analysis_session_date'])}** open", ""]
    lines += ["## Account", "",
              # Short headers: the dashboard renders this table in a fixed-width column (nine long headers overflowed it).
              f"| Equity | Cash | Invested | Realized | Unrealized | Dividends | Return | {bench} | Gap vs {bench} |",
              "|---|---|---|---|---|---|---|---|---|",
              f"| {m.fmt_usd(mt['equity_cents'])} | {m.fmt_usd(mt['cash_cents'])} | {m.fmt_usd(mt['invested_cents'])} | "
              f"{_signed_usd(mt['realized_pnl_cents'])} | {_signed_usd(mt['unrealized_pnl_cents'])} | {_signed_usd(mt['dividends_cents'])} | "
              f"{_pct(mt['total_return'])} | {_pct(mt['spy_benchmark_return'])} | {_pts(mt['excess_vs_spy'])} |", "",
              f"Return is since inception and net of costs; {bench} is the passive benchmark's return over the same period; "
              f"Gap vs {bench} is Return minus {bench}, in percentage points.", "",
              " ".join(x for x in ((f"Valued through {long_date(mt['as_of_session'])}." if mt["as_of_session"] else "No session has been valued yet."),
                                   f"{mt['sample_note']}.",
                                   f"{mt['annualization_note']}." if mt["annualization_note"] else "",
                                   f"Keeping the {m.fmt_usd(exp['starting_capital_cents'])} in cash would show {_pct(0.0)}.") if x), ""]
    if mt["provisional_sessions"]:
        lines += [f"**{mt['provisional_sessions']} valuation(s) are PROVISIONAL** (missing marks or unverified corporate actions); see the dashboard.", ""]
    fills = conn.execute("SELECT * FROM fills WHERE experiment_id=? AND run_id=? ORDER BY id", (exp_id, run["id"])).fetchall()
    lines += ["## Settled this run (simulated events recorded later)", ""]
    if fills:
        lines += ["| Account | Symbol | Side | Qty | Fill price | Session open (simulated) | Decided | Recorded |", "|---|---|---|---|---|---|---|---|"]
        for f in fills:
            lines.append(f"| {f['account'].title()} | {f['symbol']} | {f['side']} | {m.qty_to_str(f['quantity_micro'])} | {Decimal(f['fill_price']):.4f} | "
                         f"{when(f['sim_exec_at'], tz)} | {when(f['decided_at'], tz)} | {when(f['recorded_at'], tz)} |")
    else:
        lines.append("No fills were recorded in this run.")
    exp_orders = conn.execute("SELECT o.* FROM orders o JOIN order_events e ON e.order_id=o.id WHERE o.experiment_id=? AND e.to_status='EXPIRED'"
                              " AND e.recorded_at>=? ORDER BY o.id", (exp_id, run["started_at"])).fetchall()
    for o in exp_orders:
        lines.append(f"- {o['symbol']} {o['side']} order for the {long_date(o['target_session_date'])} open EXPIRED: {words(o['status_reason'])}")
    lines.append("")
    decs = conn.execute("SELECT * FROM decisions WHERE run_id=? ORDER BY seq", (run["id"],)).fetchall()
    lines += ["## Decisions", ""]
    if run["mode"] == "reconcile_only":
        lines.append("Reconcile-only rerun: an analysis for this session already exists, so no new decisions were taken.")
    elif not decs:
        lines.append("No decisions were submitted.")
    else:
        lines += ["| # | Action | Symbol | Size | Status | Target open | Why (thesis) |", "|---|---|---|---|---|---|---|"]
        for d in decs:
            o = conn.execute("SELECT * FROM orders WHERE decision_id=? AND origin='ADVISOR'", (d["id"],)).fetchone()
            size = m.fmt_usd(d["notional_cents"]) if d["notional_cents"] else ("ALL" if d["sell_all"] else
                                                                               (m.qty_to_str(d["quantity_micro"]) if d["quantity_micro"] else ""))
            st = o["status"] if o else ("RECORDED" if d["action"] in ("HOLD", "NO_TRADE") else "")
            if o and o["status"] == "REJECTED":
                # The dashboard's words for each failed check ("Over the max position size"), not the stored names.
                from .dashboard.pages import check_label  # lazy: the CLI imports reports without the dashboard
                st = "REJECTED: " + ", ".join(check_label(x.strip(), passed=False) for x in (o["status_reason"] or "").split(";") if x.strip())
            # A no-break space keeps "NO TRADE" on one line in the narrow table.
            lines.append(f"| {d['seq']} | {d['action'].replace('_', chr(160))} | {d['symbol'] or ''} | {size} | {st} | "
                         f"{long_date(o['target_session_date']) if o else ''} | "
                         f"{d['thesis'][:220].replace('|', '/')} |")
        lines.append("")
        for d in decs:
            if d["action"] in ("BUY", "SELL", "HOLD"):
                # As a percentage, like the dashboard ("0.55" read as a raw stored value). Computed outside the f-string:
                # nesting the same quotes inside one is a SyntaxError before Python 3.12.
                conf = f"{Decimal(str(d['confidence'])) * 100:.0f}%" if d["confidence"] else "n/a"
                end = lambda x: (str(x).rstrip() + ("" if str(x).rstrip().endswith((".", "!", "?")) else "."))  # each part a sentence
                lines += [f"**{d['action']} {d['symbol']}.** Counterargument: {end(d['counterargument'])}",
                          f"Invalidation: {end(d['invalidation'] or 'n/a')} Horizon: {end(d['horizon_text'] or d['horizon_sessions'])} "
                          f"Confidence: {conf}. Uncertainty: {end(d['uncertainty'] or 'n/a')}", ""]
    sub = conn.execute("SELECT * FROM run_submissions WHERE run_id=?", (run["id"],)).fetchone()
    if sub:
        lines += ["## Market view", "", sub["market_view"], ""]
        if sub["what_changed"]:
            lines += ["## What changed", "", sub["what_changed"], ""]
    # Both accounts: the benchmark's pending buy is part of the comparison the report makes (the dashboard lists it too).
    pend = conn.execute("SELECT * FROM orders WHERE experiment_id=? AND status='PENDING' ORDER BY id", (exp_id,)).fetchall()
    lines += ["## Pending orders", ""]
    lines += [f"- {'passive benchmark' if o['account'] == 'BENCHMARK' else o['origin'].lower()} {o['side']} {o['symbol']} "
              f"{m.fmt_usd(o['notional_cents']) if o['notional_cents'] else ('ALL' if o['sell_all'] else m.qty_to_str(o['quantity_micro']))}"
              f" for the {long_date(o['target_session_date'])} open (decided {when(o['decided_at'], tz)})" for o in pend] or ["None."]
    lines.append("")
    rec = conn.execute("SELECT * FROM reconciliation_results WHERE run_id=? AND passed=0", (run["id"],)).fetchall()
    lines += ["## Reconciliation", "", "All checks passed." if not rec else "FAILED: " + ", ".join(words(r["check_name"]) for r in rec), ""]
    for n in extra_notes or []:
        lines.append(f"- {n}")
    lines += ["", "_Reports never imply skill from a small sample. Commission/slippage are simulation assumptions._", ""]
    # Raw identifiers last, for audit; the dashboard collapses this section.
    # SYNC WARNING: dashboard/pages.report_html collapses the section whose heading starts "Provenance".
    lines += ["## Provenance (raw identifiers)", "",
              f"- Model: {run['model_provider']} / {run['model_id']} (effort: {run['model_effort']})",
              f"- Code: {run['code_version']} (tree sha256 {run['code_tree_sha256'] or 'unknown'})", ""]
    return "\n".join(lines)


def save(conn, paths, run_id: int, kind: str, text: str, session_date: str) -> str:
    # Fixture reports live in their own folder and say so in their name, so a file found on disk is never taken for
    # the forward test's. The dashboard only serves files under reports_dir.
    mode = db.db_mode(conn)
    d = paths.reports_dir / ("fixture" if mode == "fixture" else "") / session_date[:4] / session_date[5:7]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_date}_{'fixture_' if mode == 'fixture' else ''}{kind}_run{run_id}.md"
    p.write_text(text, encoding="utf-8")
    conn.execute("INSERT INTO reports(run_id, kind, path, sha256, created_at) VALUES (?,?,?,?,?)",
                 (run_id, kind, str(p), hashlib.sha256(text.encode()).hexdigest(), clock.now_iso()))
    return str(p)
