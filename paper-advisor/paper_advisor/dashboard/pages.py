"""HTML for the local dashboard. Server-rendered, escaped, no external assets.

Design brief: this is a decision instrument for the experiment owner,
who must decide (1) is the advisor adding value vs cash and the benchmark, and is the evidence
meaningful yet, (2) is the record trustworthy (fresh data, reconciled, nothing provisional, stuck or
failed), (3) what happens next and what to do (there is no run button: the advisor is a Claude Code
session), (4) why each decision was made and what became of it, (5) whether to change prospective
settings or start a new experiment. Tiering: anything wrong is flagged in the always-visible status
row; performance, "what happens next" and a health summary sit above the fold; one table per record
type below; the full record of any row opens in the row-click drawer. Nothing requires reading raw
JSON, internal codes or check names.

Clocks: the forward test is judged against the real current time. The fixture demo is a frozen
historical scenario, judged against its own last activity (the "fixture clock"): judging 2025
synthetic data against today's date marked every symbol stale and every identity unverified.

SYNC WARNING: SETTING_LABELS/SETTING_RULES mirror config.EDITABLE_KEYS and config.normalize ranges;
CHECK_META mirrors the check names and observed/limit meanings emitted by submission.py;
RUN_STATUS_WORDS mirrors the runs.status CHECK; ORDER_REASON_WORDS and _EXIT_RE mirror the reason
strings written by broker_sim.py. The recovery text in next_steps (retry_words, takeover_words) mirrors morning.start
(a plain retry only reconciles and reports once ANY run submitted for the session it targets; --force-recover).
"""
from __future__ import annotations

import contextvars
import json
import re
import sqlite3
from datetime import date, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import accounting, clock, config, db, experiments, market_calendar as cal, metrics, orders, prices, quality, rules, universe
from .. import money as m
from . import charts

LA = ZoneInfo("America/Los_Angeles")
# Year of the clock the page is judged against (fixture: 2025). Dates in that year omit the year.
# A ContextVar, not a module global: the server renders pages on concurrent threads.
_REF_YEAR: contextvars.ContextVar[int | None] = contextvars.ContextVar("ref_year", default=None)

SETTING_LABELS = {
    "min_order_usd": ("Minimum order (USD)", "Smallest simulated order the validator accepts."),
    "max_position_pct": ("Max position (fraction of equity)", "Includes pending buys and the new order."),
    "min_cash_reserve_pct": ("Cash reserve after buys (fraction)", "Must remain in cash after all planned buys."),
    "max_open_positions": ("Max open positions", "Held + pending buys; a pending exit does not free a slot."),
    "max_new_orders_per_session": ("Max new advisor orders per session", "Counts every advisor order placed for a session, including canceled ones."),
    "commission_per_order_usd": ("Commission per order (USD)", "Simulation assumption, not verified broker pricing."),
    "commission_bps": ("Commission (basis points of value)", "Simulation assumption."),
    "slippage_bps": ("Adverse slippage per side (basis points)", "Simulation assumption: buys fill above the open, sells below."),
    "require_exit_rule_on_buy": ("Require an exit rule on every buy", "Each entry then needs a stop, target or holding limit."),
    "max_stop_loss_pct": ("Largest allowed stop distance (fraction)", "Sanity bound for stop-loss percentages."),
}
# How each value is echoed back while typing (app.js) and on render: what the number means.
_ECHO = {"max_position_pct": "pct", "min_cash_reserve_pct": "pct", "max_stop_loss_pct": "pct",
         "commission_bps": "bps", "slippage_bps": "bps", "min_order_usd": "usd", "commission_per_order_usd": "usd"}
# (low, low inclusive, high, high inclusive) for checking a value while it is typed, before the press.
# SYNC WARNING: mirrors the ranges enforced in config.normalize (the server stays the authority).
RANGES = {"max_position_pct": (0, False, 1, True), "min_cash_reserve_pct": (0, True, 1, False), "max_stop_loss_pct": (0, False, 1, False),
          "commission_bps": (0, True, 1000, True), "slippage_bps": (0, True, 1000, True), "min_order_usd": (0.01, True, None, True),
          "commission_per_order_usd": (0, True, None, True), "max_open_positions": (1, True, 100, True),
          "max_new_orders_per_session": (1, True, 50, True)}
# How to fix a rejected value, in the owner's terms (config.normalize's own messages name internal keys).
SETTING_RULES = {
    "min_order_usd": "Enter a dollar amount of at least 0.01 with at most 2 decimals, e.g. 5.00.",
    "max_position_pct": "Enter a fraction above 0 and at most 1, e.g. 0.25 for 25% of equity.",
    "min_cash_reserve_pct": "Enter a fraction from 0 up to but not including 1, e.g. 0.10 for 10%.",
    "max_open_positions": "Enter a whole number from 1 to 100.",
    "max_new_orders_per_session": "Enter a whole number from 1 to 50.",
    "commission_per_order_usd": "Enter a dollar amount of 0 or more with at most 2 decimals, e.g. 0.00.",
    "commission_bps": "Enter basis points from 0 to 1000 (10 bps = 0.10%).",
    "slippage_bps": "Enter basis points from 0 to 1000 (10 bps = 0.10%).",
    "require_exit_rule_on_buy": "Choose yes or no.",
    "max_stop_loss_pct": "Enter a fraction above 0 and below 1, e.g. 0.50 for 50%.",
}
# check name -> (label when passed, label when failed, what "observed" means, what "limit" means, dollars?).
# Failed checks are worded as the failure ("Over the max position size"), never as the rule they broke.
# A None meaning hides that value (the check is a yes/no condition).
CHECK_META = {
    "action_allowed": ("Action allowed", "Action not allowed", "action", "allowed", False),
    "no_position": ("Position held (no short sales)", "No position to sell (no short sales)", "shares held", "needed", False),
    "duplicate_symbol_in_batch": ("One decision per symbol per batch", "Symbol decided twice in one batch", None, None, False),
    "invalid_exit_rule": ("Exit rule within allowed ranges", "Exit rule outside allowed ranges", "problems found", "required", False),
    "missing_exit_rule": ("Exit rule and invalidation present", "Exit rule or invalidation missing", "exit rule", "required", False),
    "exit_already_pending": ("No exit already pending", "An exit is already pending", None, None, False),
    "entry_pending_for_symbol": ("No add-on entry already pending", "An add-on entry is still pending", "pending entries", "allowed", False),
    "stop_loosening_not_allowed": ("Stop not widened or removed", "Stop would be widened or removed", "new stop distance", "allowed", False),
    "max_new_orders_per_session": ("Within orders-per-session limit", "Too many orders for the session", "orders for the session incl. this one", "limit", False),
    "exceeds_available_quantity": ("Quantity available to sell", "More shares than held", "shares to sell", "shares available", False),
    "min_order": ("At least the minimum order", "Below the minimum order", "order value", "minimum", True),
    "data_quality_for_sell": ("Data quality (sells are allowed regardless)", "Data quality issue", "data", "preferred", False),
    "not_in_universe": ("In the research universe", "Not in the research universe", None, None, False),
    "instrument_unverified": ("Instrument identity verified", "Instrument identity not verified", "status", "required", False),
    "asset_type_not_allowed": ("Allowed asset type (stock or ETF, unleveraged)", "Asset type not allowed", "instrument", "allowed", False),
    "missing_data": ("Price data available", "No price data", "latest bar", "expected session", False),
    "stale_data": ("Fresh end-of-day data", "Price data is stale", "latest bar", "expected session", False),
    "suspect_data": ("Clean price data", "Price data flagged as suspect", "flags", "expected", False),
    "fresh_data": ("Fresh end-of-day data", "Price data is stale", "latest bar", "expected session", False),
    "conflicting_pending_exit": ("No exit pending for the symbol", "An exit is pending for the symbol", None, None, False),
    "max_position_pct": ("Within max position size", "Over the max position size", "position after the order", "largest allowed", True),
    "max_open_positions": ("Within max open positions", "Too many open positions", "positions incl. pending and this one", "limit", False),
    "insufficient_cash": ("Cash covers the order", "Not enough cash", "cash available", "order needs", True),
    "cash_reserve": ("Cash reserve kept after buys", "Cash reserve would be too low", "cash left after planned buys", "required reserve", True),
    "unknown_order": ("Order exists", "Order not found", "order id", None, False),
    "order_not_cancellable": ("Order still pending", "Order no longer pending", "order status", "required", False),
    "target_session_already_open": ("Before the target session's open", "Too late: the target session has opened", "decided", "deadline", False),
}
# What a passed check verified, for checks that store no values or message when they pass.
CHECK_PASS_NOTES = {"invalid_exit_rule": "the stop, target and holding limit are within their allowed ranges"}
ACTION_WORDS = {"NO_TRADE": "NO TRADE"}


def h(x) -> str:
    return "" if x is None else escape(str(x), quote=True)


def dash(x) -> str:
    return "—" if x is None or (isinstance(x, str) and not x.strip()) else h(x)


def num(x) -> str:
    """Decimal text without scientific notation (Decimal('100.0000').normalize() would print 1E+2)."""
    if x is None or x == "":
        return "—"
    return h(format(Decimal(str(x)).normalize(), "f"))


def price(x) -> str:
    """A price with at least 2 decimals and no trailing zeros beyond them: 100.00, 100.10, 45.7542."""
    if x is None or x == "":
        return "—"
    q = Decimal(str(x)).normalize()
    if q.as_tuple().exponent > -2:
        q = q.quantize(Decimal("0.01"))
    return h(format(q, "f"))


def fill_px(x) -> str:
    """A simulated fill price, always to 4 decimals (open x (1 +/- slippage) rarely ends in cents): '25.0250'."""
    return "—" if x in (None, "") else f"{Decimal(str(x)):.4f}"


def dollars(x) -> str:
    """A price as money for prose and drawers ('$45.7542'); tables keep bare numbers under a price header."""
    return "—" if x is None or x == "" else "$" + price(x)


def pt(iso: str | None) -> str:
    if not iso:
        return "—"
    return clock.parse_iso(iso).astimezone(LA).strftime("%a %b %-d, %-I:%M %p PT")


def pt_short(iso: str | None) -> str:
    if not iso:
        return "—"
    return clock.parse_iso(iso).astimezone(LA).strftime("%b %-d, %-I:%M %p")


def day(session_date: str | None) -> str:
    """'Tue Nov 25' (', 2025' only when the year differs from the page's clock): one date style everywhere."""
    if not session_date:
        return "—"
    try:
        dd = date.fromisoformat(session_date)
    except ValueError:
        return h(session_date)
    ref = _REF_YEAR.get()
    return dd.strftime("%a %b %-d") + ("" if ref in (None, dd.year) else dd.strftime(", %Y"))


def ago(iso: str, now) -> str:
    # SYNC WARNING: app.js agoText() words relative times the same way when it refreshes them.
    mins = int((now - clock.parse_iso(iso)).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"{mins // 60} h {mins % 60} min ago"


def iso_s(iso: str) -> str:
    """A UTC instant to whole seconds ('2026-10-24T10:09:26Z') for HTML attributes read by app.js (Safari's
    Date.parse rejects the stored microseconds)."""
    return clock.parse_iso(iso).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def when_html(iso: str, now) -> str:
    """'12 min ago (2:15 AM PT)': the absolute time never goes stale; app.js keeps the relative part current."""
    t = clock.parse_iso(iso).astimezone(LA).strftime("%-I:%M %p PT")
    return f'<time datetime="{iso_s(iso)}" data-ago>{h(ago(iso, now))}</time> ({t})'


def et_open(session_date: str | None) -> str:
    if not session_date:
        return "—"
    try:
        s = cal.session(date.fromisoformat(session_date))
    except ValueError:  # never let one malformed value take the whole dashboard down
        return h(session_date)
    if s is None:
        return day(session_date)  # not a trading day (e.g. a weekend run key): the date in words, not ISO
    return f"{day(session_date)} open (9:30 AM ET / {s.open_utc.astimezone(LA).strftime('%-I:%M %p')} PT)"


def open_short(session_date: str | None) -> str:
    """'Mon Nov 24 open' for table cells (the long ET/PT form wrapped every row onto two lines)."""
    if not session_date:
        return "—"
    try:
        s = cal.session(date.fromisoformat(session_date))
    except ValueError:
        return h(session_date)
    return day(session_date) if s is None else f"{day(session_date)} open"


def cutoff_pt(session_date: str, buffer_seconds: int) -> str:
    """When decisions must be submitted to target this session's open (the open minus the buffer)."""
    s = cal.session(date.fromisoformat(session_date))
    return (s.open_utc - timedelta(seconds=buffer_seconds)).astimezone(LA).strftime("%a %-I:%M %p PT")


def usd(c) -> str:
    return "—" if c is None else m.fmt_usd(int(c))


def signed_usd(c) -> str:
    # SYNC WARNING: reports._signed_usd writes P&L the same way in run reports ("+$14.41", "-$10.30", "$0.00").
    if c is None:
        return "—"
    c = int(c)
    cls = "pos" if c > 0 else ("neg" if c < 0 else "flat")
    return f'<span class="{cls}">{"+" if c > 0 else ""}{m.fmt_usd(c)}</span>'


def pct(x, signed=True) -> str:
    """Percent with an explicit sign, except zero, which is shown unsigned and neutral (no "+0.00%")."""
    if x is None:
        return '<span class="muted">n/a</span>'
    if abs(x) < 0.00005:
        return '<span class="flat">0.00%</span>'
    if not signed:
        return f"{x * 100:.1f}%"
    return f'<span class="{"pos" if x > 0 else "neg"}">{x * 100:+.2f}%</span>'


def pts(x) -> str:
    """A difference between two returns, in percentage points."""
    if x is None:
        return '<span class="muted">n/a</span>'
    if abs(x) < 0.00005:
        return '<span class="flat">0.00 pts</span>'
    return f'<span class="{"pos" if x > 0 else "neg"}">{x * 100:+.2f} pts</span>'


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def frac_pct(x) -> str:
    return "—" if x in (None, "") else f"{Decimal(str(x)) * 100:.1f}%"


def chip(text, kind="neutral", title=None) -> str:
    t = f' title="{h(title)}"' if title else ""
    return f'<span class="chip chip-{kind}"{t}>{h(text)}</span>'


def action_words(a: str) -> str:
    return ACTION_WORDS.get(a, a)


# SYNC WARNING: keys mirror the runs.status CHECK in migrations/0001_initial.sql.
RUN_STATUS_WORDS = {
    "started": ("starting", "info"),
    "awaiting_decisions": ("waiting for the advisor's decisions", "info"),
    "decisions_submitted": ("decisions submitted, finishing", "info"),
    "completed": ("completed", "good"),
    "failed": ("failed", "bad"),
    "interrupted": ("interrupted", "warn"),
}
IN_PROGRESS = ("started", "awaiting_decisions", "decisions_submitted")


def run_status_chip(status: str) -> str:
    words, kind = RUN_STATUS_WORDS.get(status, (status.replace("_", " "), "neutral"))
    return chip(words, kind)


def duration_words(secs: int) -> str:
    """A run's length as read next to its finish time ("after 0 min 0 s" read as broken for a scripted run)."""
    if secs < 1:
        return "in under a second"
    return f"after {secs} s" if secs < 60 else f"after {secs // 60} min {secs % 60} s"


def run_kind_words(run, several: bool = False) -> str:
    """several: this open had more than one attempt, so the first says "(attempt 1)" too (it read as unnumbered
    next to "attempt 3" in the same history)."""
    if run["run_kind"] != "morning":
        return f"{run['run_kind']} run"
    if run["mode"] == "reconcile_only":
        return f"morning rerun (reconcile only, attempt {run['version']})"  # it takes an attempt number too: no gap
    if run["supersedes_run_id"]:
        # /paper-morning new-analysis: versioned and linked to the analysis it follows (both keep their decisions).
        return f"morning analysis (attempt {run['version']}, a deliberate new analysis after run #{run['supersedes_run_id']})"
    return "morning analysis" + (f" (attempt {run['version']})" if run["version"] > 1 or several else "")


def run_kind_parts(run, several: bool = False) -> tuple[str, str]:
    """run_kind_words for the Runs table: the kind, then its attempt and link as a muted second line (one long phrase
    per row pushed the table wider than its card: capture, round 14). SYNC WARNING: same facts as run_kind_words."""
    if run["run_kind"] != "morning":
        return f"{run['run_kind'].capitalize()} run", ("reads the record; places no orders" if run["run_kind"] == "review" else "")
    if run["mode"] == "reconcile_only":
        return "Morning rerun", f"reconcile only · attempt {run['version']}"
    if run["supersedes_run_id"]:
        return "Morning analysis", f"attempt {run['version']} · new analysis after #{run['supersedes_run_id']}"
    return "Morning analysis", (f"attempt {run['version']}" if run["version"] > 1 or several else "")


def error_words(err: str | None) -> str:
    """Stored run errors are terse ('aborted: reason'); say them as sentences."""
    if not err:
        return ""
    if err.startswith("aborted: "):
        return "Aborted: " + err[len("aborted: "):]
    return err[:1].upper() + err[1:]


def data_status_words(st: str | None) -> str:
    if not st:
        return "—"
    if "FIXTURE" in st:
        return "synthetic"
    return {"OK": "complete", "PARTIAL": "partial", "DATA_UNAVAILABLE": "unavailable"}.get(st, st.lower())


def model_words(model_id: str | None, effort: str | None) -> str:
    """'claude-opus-5-5', 'max' -> 'Claude Opus 5.5 · max effort' (the raw id stays in a tooltip for provenance)."""
    mid = (model_id or "").strip()
    if mid in ("", "unknown"):
        name = "model not reported"
    elif mid == "fixture-script":
        return "Scripted fixture (no model)"
    elif mid.startswith("claude-"):
        parts = mid.split("-")[1:]
        fam = [x for x in parts if not x.isdigit()]
        ver = [x for x in parts if x.isdigit()]
        name = "Claude " + " ".join(x.capitalize() for x in fam) + (" " + ".".join(ver) if ver else "")
    else:
        name = mid
    eff = (effort or "").strip()
    return name + ("" if eff in ("", "n/a") else (" · effort not reported" if eff == "unknown" else f" · {eff} effort"))


def code_words(cv: str | None) -> str:
    """'1.0.0+8437787ddc3e' -> '1.0.0 · commit 8437787' (the full tree hash is in the cell's tooltip)."""
    if not cv or cv == "unknown":
        return "unknown"
    ver, _, sha = cv.partition("+")
    return ver if sha in ("", "nogit") else f"{ver} · commit {sha[:7]}"


def recon_label(name: str) -> str:
    """'ledger_replay:PORTFOLIO' -> 'ledger replay (portfolio)'."""
    base, _, acct = name.partition(":")
    words = base.replace("_", " ")
    return f"{words} ({'benchmark' if acct == 'BENCHMARK' else acct.lower()})" if acct else words


# Tools that check the run's own output rather than inform its decisions, listed apart in skills_summary. SYNC WARNING:
# skills_bridge marks data-quality-checker as the report lint (LINT), which morning.py runs on the report at finish.
CHECK_TOOLS = {"data-quality-checker"}
SKILL_WORDS = {"market-breadth-analyzer": "market breadth", "uptrend-analyzer": "uptrend ratios",
               "ibd-distribution-day-monitor": "distribution days", "exposure-coach": "exposure ceiling",
               "position-sizer": "position sizing", "stockbee-momentum-burst-screener": "momentum setups",
               "data-quality-checker": "report data check", "residual-edge-analyzer": "residual edge",
               "weekly-performance-digest": "weekly digest"}


def tool_words(name: str) -> str:
    return SKILL_WORDS.get(name, name)


def takeover_reasons(conn, exp_id) -> dict[int, tuple[str, str]]:
    """{interrupted run id: ("expired" | "forced", lock expiry)} from the lock the run that took it over replaced. After a
    takeover nothing said why the run was interrupted (its lock expiry was the whole story: capture, round 13).
    SYNC WARNING: morning.start logs {"recovered": [...], "previous_lock": <locks row>} in the new run's "started" event."""
    out = {}
    for ev in conn.execute("SELECT e.details_json, r.started_at FROM run_events e JOIN runs r ON r.id=e.run_id "
                           "WHERE e.kind='started' AND r.experiment_id=?", (exp_id,)):
        det = json.loads(ev["details_json"] or "{}")
        lock = det.get("previous_lock") or {}
        for rec in det.get("recovered") or []:
            if lock.get("run_id") == rec["run_id"] and lock.get("expires_at"):
                expired = clock.parse_iso(lock["expires_at"]) <= clock.parse_iso(ev["started_at"])
                out[rec["run_id"]] = ("expired" if expired else "forced", lock["expires_at"])
    return out


def taken_over_words(reason, by_run: int) -> str:
    """The end of an interrupted run's problem line, naming the run that took it over and why it could."""
    if reason is None:
        return f"taken over by run #{by_run}"
    kind, until = reason
    if kind == "expired":
        return f"its lock expired at {pt(until)} without being renewed, and run #{by_run} took it over"
    return f"run #{by_run} took it over with force-recover while its lock was still held (until {pt(until)})"


def _took_over_why(reason) -> str:
    """The "Took over" row's parenthesis for one taken-over run (see takeover_reasons)."""
    if reason is None:
        return " (it had stopped without finishing; this run marked it interrupted)"
    kind, until = reason
    if kind == "expired":
        return f" (its lock had expired at {pt(until)} without being renewed; this run marked it interrupted)"
    return f" (taken over with force-recover while its lock was held until {pt(until)}; this run marked it interrupted)"


def skills_summary(rows) -> tuple[str, str, str]:
    """(chip kind, chip text, detail). Skipped tools (e.g. live-data tools in fixture mode) are not failures."""
    ok = [s["skill_name"] for s in rows if s["status"] == "ok"]
    skipped = [s["skill_name"] for s in rows if s["status"] in ("skipped", "disabled")]
    bad = [s["skill_name"] for s in rows if s["status"] not in ("ok", "skipped", "disabled")]
    ran = len(ok) + len(bad)
    # The report data check runs at finish on the report itself: a check, not an input to the advisor's decisions.
    inputs, checks = [x for x in ok if x not in CHECK_TOOLS], [x for x in ok if x in CHECK_TOOLS]
    detail = ("; ".join(x for x in ("advisory inputs: " + ", ".join(tool_words(x) for x in inputs) if inputs else "",
                                     "checks: " + ", ".join(tool_words(x) for x in checks) if checks else "") if x)
              if ok else "no tool output")
    if bad:
        detail = "failed: " + ", ".join(tool_words(x) for x in bad)
    if skipped:
        detail += f"; {len(skipped)} skipped by design ({', '.join(tool_words(x) for x in skipped)})"
    return ("warn" if bad else "good"), f"Analysis tools {len(ok)}/{ran} ran", detail


def backup_reason_words(reason: str) -> str:
    """A backup's stored reason as the owner would say it (critic, round 9: the list showed "pre-morning run 16" and
    "dashboard backup"). SYNC WARNING: reasons written by morning.start, server._act_backup and cli's backup command."""
    m = re.match(r"^pre-morning run (\d+)$", reason or "")
    if m:
        return f"before morning run #{m.group(1)}"
    return {"dashboard backup": "made from this dashboard", "manual backup": "made with pa backup"}.get(reason, reason)


def clip_td(text, n: int = 180) -> str:
    """A one-line cell that ellipsizes; the full text stays available on hover."""
    t = text or ""
    return f'<td class="clip" title="{h(t)}">{h(t[:n])}</td>'


def check_label(name: str, passed: bool = True) -> str:
    meta = CHECK_META.get(name)
    if meta is None:
        return name.replace("_", " ")
    return meta[0] if passed else meta[1]


_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# Stored check values that are codes rather than words, per check.
# SYNC WARNING: limit/observed strings written by submission.py checks.
_VALUE_WORDS = {"missing_exit_rule": {"rule": "present", "none": "missing",
                                      "stop/target/max-holding + invalidation text": "a stop, target or holding limit, plus an invalidation statement"},
                "no_position": {"> 0": "more than 0"},
                "asset_type_not_allowed": {"COMMON_STOCK 1x": "common stock, unleveraged", "ETF 1x": "ETF, unleveraged",
                                           "COMMON_STOCK/ETF 1x": "common stock or ETF, unleveraged"},
                "data_quality_for_sell": {"FRESH preferred": "fresh preferred", "FRESH": "fresh", "STALE": "stale", "SUSPECT": "suspect",
                                          "MISSING": "missing"},
                "instrument_unverified": {"VERIFIED within 14 days": "verified within the last 14 days"},
                "missing_data": {"no stored price data": "none stored"}}


def _check_value(name: str, v, money: bool) -> str:
    s = str(v)
    if money:
        return "$" + s
    s = _VALUE_WORDS.get(name, {}).get(s, s)
    if name == "stop_loosening_not_allowed":
        # submission stores the new stop fraction and "<= <current fraction>": say both as percentages.
        mt = re.fullmatch(r"(<= )?([0-9.]+)", s)
        if mt:
            return ("at most " if mt.group(1) else "") + f"{Decimal(mt.group(2)) * 100:.1f}%"
    s = _TS_RE.sub(lambda mt: pt(mt.group(0)), s)  # UTC timestamps (whole or embedded) -> Pacific time
    s = _DATE_RE.sub(lambda mt: day(mt.group(1)), s)
    return s.replace("COMMON_STOCK", "common stock").replace("NO_TRADE", "NO TRADE")


def check_parts(name: str, observed, limit) -> list[tuple[str, str]]:
    """[('position after the order', '$200.00'), ('largest allowed', '$125.00')]; empty for yes/no checks."""
    if name == "invalid_exit_rule" and str(observed) in ("ok", "", "None"):
        return []  # passed: "problems found: none" adds nothing
    _, _, ol, ll, money = CHECK_META.get(name, (None, None, "observed", "limit", False))
    parts = []
    if ol is not None and observed not in (None, ""):
        parts.append((ol, _check_value(name, observed, money)))
    if ll is not None and limit not in (None, ""):
        parts.append((ll, _check_value(name, limit, money)))
    return parts


def check_values(name: str, observed, limit) -> str:
    return " · ".join(f"{h(a)} <strong>{h(b)}</strong>" for a, b in check_parts(name, observed, limit))


def check_values_text(name: str, observed, limit) -> str:
    return " · ".join(f"{a} {b}" for a, b in check_parts(name, observed, limit))


# SYNC WARNING: codes written by broker_sim.py (_expire / CANCELED at fill). Unknown codes show as-is.
ORDER_REASON_WORDS = {
    "no_bar_for_target_session": "the symbol had no daily bar for the target session (it did not trade, or data was missing)",
    "session_data_unavailable_timeout": "no market data for the target session arrived within the wait window",
    "missed_target_session": "its target session passed before it could be processed",
    "below_minimum_at_fill": "at the fill price it would have been below the minimum order",
    "insufficient_cash_at_fill": "cash at the open did not cover it (refused rather than overdraw)",
    "no_position_at_fill": "there was no position left to sell at the open",
}
# broker_sim writes rule exits as "<KIND>: close <price> on <date> vs rule [<rules.describe>]".
_EXIT_RE = re.compile(r"^(STOP_LOSS|TAKE_PROFIT|MAX_HOLDING): close ([0-9.]+) on (\d{4}-\d{2}-\d{2})")


def exit_words(note: str | None, tail: str | None = "sold") -> str:
    """'STOP_LOSS: close 46.5000 on 2025-11-25 vs rule [...]' -> 'Stop-loss: the Tue Nov 25 close ($46.50) ...'.
    tail says what became of the exit in the caller's tense: "sold" once filled, "sells" while still pending,
    None when the caller states the outcome itself (otherwise "sold at the next open; sold 2.5 shares ...")."""
    if not note:
        return ""
    mt = _EXIT_RE.match(note)
    if not mt:
        return note
    kind, close, sd = mt.groups()
    lvl = re.search(r"close (?:<=|>=) ([0-9.]+)", note)
    at = f" ({dollars(lvl.group(1))})" if lvl else ""
    end = "" if tail is None else f"; {tail} at the next open"
    if kind == "STOP_LOSS":
        return f"Stop-loss: the {day(sd)} close ({dollars(close)}) was at or below the stop{at} in force{end}"
    if kind == "TAKE_PROFIT":
        return f"Take-profit: the {day(sd)} close ({dollars(close)}) reached the target{at}{end}"
    return f"Holding-period limit reached at the {day(sd)} close{end}"


def rule_words(rule) -> str:
    """Exit rules as the owner would say them: 'exit on a close at or below $18.42 (8.0% stop) · after 30 sessions'."""
    if rule is None:
        return "none"
    parts = []
    ref = Decimal(rule["reference_price"]) if rule["reference_price"] not in (None, "") else None
    lvl = rules.stop_level(rule)
    if lvl is not None:
        how = f"{Decimal(rule['stop_loss_pct']) * 100:.1f}% stop" if rule["stop_loss_pct"] is not None else "stop floor"
        parts.append(f"exit on a close at or below {dollars(lvl.quantize(Decimal('0.01')))} ({how})")
    if rule["take_profit_pct"] is not None and ref is not None:
        tgt = (ref * (1 + Decimal(rule["take_profit_pct"]))).quantize(Decimal("0.01"))
        parts.append(f"take profit on a close at or above {dollars(tgt)} (+{Decimal(rule['take_profit_pct']) * 100:.1f}%)")
    if rule["max_holding_sessions"] is not None:
        parts.append(f"exit after {rule['max_holding_sessions']} sessions")
    return " · ".join(parts) or "none"


# SYNC WARNING: order_events reasons written by broker_sim._fill and submission (accepted orders).
_FILLED_RE = re.compile(r"^filled ([0-9.]+) @ ([0-9.]+) at (\d{4}-\d{2}-\d{2}) open$")
_ACCEPTED_RE = re.compile(r"^accepted for market-on-open (\d{4}-\d{2}-\d{2})$")


def _event_reason(to_status: str, reason: str) -> str:
    # A REJECTED event stores the failed check names joined by ";" (machine-readable); show labels.
    if to_status == "REJECTED" and reason and " " not in reason.strip():
        return "failed: " + ", ".join(check_label(x, passed=False) for x in reason.split(";") if x)
    if reason and _EXIT_RE.match(reason):
        # The event that queues a rule exit happens before the sale (its own later event records the fill).
        return exit_words(reason, tail="sells")
    mt = _FILLED_RE.match(reason or "")
    if mt:
        qty, px, sd = mt.groups()
        return f"filled {qty} shares at {dollars(px)} at the {day(sd)} open"
    mt = _ACCEPTED_RE.match(reason or "")
    if mt:
        return f"accepted for the {day(mt.group(1))} open"
    return ORDER_REASON_WORDS.get(reason, reason or "")


# SYNC WARNING: reason codes documented and written by valuation.py.
def provisional_words(reason: str) -> str:
    code, _, sym = reason.partition(":")
    return {"stale_mark": f"{sym} had no bar that session; its latest earlier close was used",
            "no_mark": f"{sym} had no price at all; it was valued at cost",
            "corporate_actions_unverified": f"splits and dividends for {sym} were not confirmed through that session",
            "data_quality": f"the {sym} bar used carries data-quality flags",
            "calendar_unverified": "the session is outside the official NYSE calendar table",
            "no_data_for_session": "the data feed never delivered that session"}.get(code, reason)


def _detail(did: str, title: str, body: str, wide: bool = False) -> str:
    # wide: the drawer opens wider for a record that is a wide table (the valued-sessions table needed sideways
    # scrolling in the standard 640px drawer). SYNC WARNING: app.js openDetail reads data-wide.
    return f'<template id="{did}"{" data-wide" if wide else ""}><h2>{h(title)}</h2>{body}</template>'


def _kv(rows) -> str:
    return '<dl class="kv">' + "".join(f"<dt>{h(k)}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def view_now(conn, kind: str):
    """Real time for the forward test; the frozen scenario clock (last run) for the fixture demo."""
    if kind != "fixture":
        return clock.now_utc()
    r = conn.execute("SELECT MAX(COALESCE(finished_at, started_at)) FROM runs").fetchone()[0]
    return clock.parse_iso(r) if r else clock.now_utc()


def load(conn, exp_id: int, now, db_kind: str) -> dict:
    exp = experiments.get(conn, exp_id)
    cfg_row = config.current_version(conn, exp_id, clock.iso(now))
    _, cfg = config.current(conn, exp_id, clock.iso(now))
    mt = metrics.compute(conn, exp_id)
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    today_ny = now.astimezone(cal.NY).date().isoformat()
    marks = {}
    for s in st.positions:
        b = prices.latest_bar(conn, s, on_or_before=today_ny)
        if b is not None:
            marks[s] = (Decimal(b["close"]), b["session_date"])
    val = accounting.valuation(st, {k: v[0] for k, v in marks.items()})
    last = cal.last_completed_session(now, cfg.bar_finalization_minutes)
    syms = universe.symbols(conn)
    dq = quality.assess(conn, syms, now, cfg.bar_finalization_minutes)
    ident = {s: universe.is_verified(conn, s, clock.iso(now)) for s in syms}
    recon = conn.execute("SELECT * FROM reconciliation_results WHERE experiment_id=? AND at=(SELECT MAX(at) FROM reconciliation_results WHERE experiment_id=?)",
                         (exp_id, exp_id)).fetchall()
    last_run = conn.execute("SELECT * FROM runs WHERE experiment_id=? ORDER BY id DESC LIMIT 1", (exp_id,)).fetchone()
    last_morning = conn.execute("SELECT * FROM runs WHERE experiment_id=? AND run_kind='morning' ORDER BY id DESC LIMIT 1", (exp_id,)).fetchone()
    skills = conn.execute("SELECT skill_name, status FROM skill_runs WHERE run_id=?", (last_run["id"],)).fetchall() if last_run else []
    failed_run = last_morning if last_morning is not None and last_morning["status"] in ("failed", "interrupted") else None
    return {"exp": exp, "cfg_row": cfg_row, "cfg": cfg, "mt": mt, "state": st, "marks": marks, "val": val, "last": last,
            "asof_session": mt["as_of_session"], "dq": dq, "ident": ident, "recon": recon,
            "lock": _live_lock(conn, exp_id, now),
            # An archived view needs the ACTIVE experiment's run (its own lock is never live), e.g. to lock its switch.
            "active_lock": _live_lock(conn, experiments.active_experiment_id(conn), now),
            "now": now, "db_kind": db_kind, "fixture": db_kind == "fixture", "bench": exp["benchmark_symbol"],
            "next": cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds)),
            "experiments": experiments.all_experiments(conn), "active_id": experiments.active_experiment_id(conn),
            "reserved": orders.reserved_cash_cents(conn, exp_id), "pending": orders.pending(conn, exp_id),
            "last_run": last_run, "failed_run": failed_run, "skills": skills,
            # A run still marked in progress after its lock expired: the session running it ended without finishing. Without
            # this the page said nothing about it once the lock had expired (reviewer, round 8: the lock-expired screen).
            # SYNC WARNING: morning.start takes such a run over and marks it interrupted.
            "dangling_run": last_run if (last_run is not None and last_run["status"] in IN_PROGRESS
                                         and _live_lock(conn, exp_id, now) is None) else None,
            "expired_lock": conn.execute("SELECT * FROM locks WHERE name=?", (f"experiment:{exp_id}",)).fetchone(),
            # The latest run that stored a submission for the session the next /paper-morning targets: with one, a plain
            # /paper-morning only reconciles and reports (the failed run's own submission was the old test, and it said "starts a
            # fresh analysis" after runs #10 and #13 had submitted: capture, round 13). SYNC WARNING: mirrors morning.start's
            # mode rule (any run_submissions row for session_key -> reconcile_only unless new_analysis) and its target session.
            "next_submitted_run": conn.execute(
                "SELECT r.* FROM runs r JOIN run_submissions s ON s.run_id=r.id WHERE r.experiment_id=? AND r.run_kind='morning' "
                "AND r.analysis_session_date=? ORDER BY r.version DESC LIMIT 1",
                (exp_id, cal.next_session_after(now + timedelta(seconds=cfg.decision_open_buffer_seconds)).date.isoformat())).fetchone(),
            # Any benchmark buy filled: after a re-queue (broker_sim._requeue_benchmark) the FIRST order is the expired one.
            "bench_filled": conn.execute("SELECT 1 FROM fills WHERE experiment_id=? AND account='BENCHMARK' AND side='BUY' LIMIT 1",
                                         (exp_id,)).fetchone() is not None,
            "archived": exp_id != experiments.active_experiment_id(conn),
            "archive_consequence": "" if db_kind == "fixture" else archive_consequence(conn, experiments.active_experiment_id(conn)),
            "provisional_rows": conn.execute("SELECT session_date, provisional_reasons_json FROM valuation_snapshots WHERE experiment_id=? "
                                             "AND account='PORTFOLIO' AND provisional=1 ORDER BY session_date DESC, revision DESC",
                                             (exp_id,)).fetchall(),
            "counts": {k: conn.execute(q, (exp_id,)).fetchone()[0] for k, q in {
                "morning": "SELECT COUNT(*) FROM runs WHERE experiment_id=? AND run_kind='morning'",
                "review": "SELECT COUNT(*) FROM runs WHERE experiment_id=? AND run_kind='review'",
                "fills": "SELECT COUNT(*) FROM fills WHERE experiment_id=? AND account='PORTFOLIO'",
                "rejected": "SELECT COUNT(*) FROM orders WHERE experiment_id=? AND status='REJECTED'",
                "splits": "SELECT COUNT(*) FROM ledger_events WHERE experiment_id=? AND account='PORTFOLIO' AND event_type='SPLIT'",
                "dividends": "SELECT COUNT(*) FROM ledger_events WHERE experiment_id=? AND account='PORTFOLIO' AND event_type='CASH_DIVIDEND'"}.items()},
            "latest_close": max((q["last_bar_date"] for q in dq.values() if q["last_bar_date"]), default=None),
            # Held symbols whose newest price is older than the last completed session: every number that
            # uses them is provisional and must say so where it is shown.
            "stale_marks": {sym: md for sym, (_, md) in marks.items() if last is not None and md < last.date.isoformat()},
            "typical_run_min": _median_run_minutes(conn, exp_id),
            "code_versions": conn.execute("SELECT COUNT(DISTINCT code_tree_sha256) FROM runs WHERE experiment_id=? AND code_tree_sha256 IS NOT NULL",
                                          (exp_id,)).fetchone()[0],
            "config_versions": conn.execute("SELECT COUNT(*) FROM config_versions WHERE experiment_id=?", (exp_id,)).fetchone()[0],
            "costs": _costs(conn, exp_id),
            # When the benchmark really bought, or the open its pending order targets: broker_sim moves its order to
            # the next open when an open cannot fill it, so benchmark_start_session is only the FIRST eligible open.
            "bench_fill_session": (lambda r: r[0] if r else None)(conn.execute(
                "SELECT session_date FROM fills WHERE experiment_id=? AND account='BENCHMARK' AND side='BUY' ORDER BY id LIMIT 1",
                (exp_id,)).fetchone()),
            "bench_target": next((o["target_session_date"] for o in orders.pending(conn, exp_id) if o["account"] == "BENCHMARK"),
                                 exp["benchmark_start_session"])}


def _costs(conn, exp_id: int) -> dict:
    """What the advisor's fills paid beyond the official opens: slippage (|fill - open| x shares, to the nearest cent;
    the stored cost itself is exact and already in every P&L figure) and commission. Benchmark fills are excluded."""
    slip = Decimal(0)
    comm = 0
    for f in conn.execute("SELECT fill_price, reference_open, quantity_micro, commission_cents FROM fills "
                          "WHERE experiment_id=? AND account='PORTFOLIO'", (exp_id,)):
        slip += abs(Decimal(f["fill_price"]) - Decimal(f["reference_open"])) * Decimal(f["quantity_micro"]) / Decimal(1_000_000)
        comm += int(f["commission_cents"])
    return {"slippage_cents": int((slip * 100).quantize(Decimal("1"))), "commission_cents": comm}


def archive_consequence(conn, aid) -> str:
    """What archiving the active experiment does, said at every control that archives it: Create experiment, Make active
    in the Experiments table and in an archived experiment's panel (Make active alone said "nothing is deleted": reviewer,
    round 8). SYNC WARNING: mirrors broker_sim.settle (only the active experiment settles; catch-up resumes from
    settled_through on reactivation; tested in test_experiments_config)."""
    if aid is None:
        return ""
    npend = len(orders.pending(conn, aid))
    npos = len(accounting.load_state(conn, aid, "PORTFOLIO").positions)
    paused = [x for x in (f"its {plural(npend, 'pending order')} will not fill" if npend else "",
                          f"its {plural(npos, 'open position')} will not be valued or checked against exit rules" if npos else "") if x]
    what = (": " + " and ".join(paused) + " while it is archived." if paused
            else " (it has no pending orders or open positions right now).")
    # The archived stretch is not erased from #aid's record: catch-up values every missed session, and (per the brief)
    # research decisions are never invented for days the advisor did not run.
    return (f"Archiving #{aid} pauses its settlement{what} If it is made active again, the next morning run catches it up "
            "session by session (orders fill at their original opens; exit rules apply to each missed close). The archived "
            f"sessions still count in #{aid}'s record, with no advisor decisions in them.")


def _live_lock(conn, exp_id, now):
    """The experiment's run lock while a run holds it (an expired lock is taken over by the next run, so it does not count).
    SYNC WARNING: lock name as runs.lock_name; expiry rule as experiments.refuse_if_run_in_progress."""
    if exp_id is None:
        return None
    lk = conn.execute("SELECT * FROM locks WHERE name=?", (f"experiment:{exp_id}",)).fetchone()
    return lk if lk is not None and lk["expires_at"] > clock.iso(now) else None


def live_state(conn, now) -> dict:
    """What an open tab compares with the server to learn that a morning run started or ended, or that the active
    experiment changed: the page is rendered with it (body data-state) and app.js polls server /api/state for it.
    A tab opened before a run started would otherwise offer Save as if nothing had changed (Rule 19 #4).
    SYNC WARNING: app.js compares these four fields; the lock rule is _live_lock's. static/tour.js reads last_run_id
    (null before the active experiment's first run) to say why the phone strip names nothing yet."""
    aid = experiments.active_experiment_id(conn)
    lk = _live_lock(conn, aid, now)
    last = conn.execute("SELECT MAX(id) FROM runs WHERE experiment_id=?", (aid,)).fetchone()[0] if aid is not None else None
    return {"active_id": aid, "locked": lk is not None, "lock_run_id": lk["run_id"] if lk is not None else None, "last_run_id": last}


def _median_run_minutes(conn, exp_id: int) -> float | None:
    """Typical length of a completed ANALYSIS run (a reconcile-only rerun takes seconds and made the median "0 min")."""
    rows = conn.execute("SELECT started_at, finished_at FROM runs WHERE experiment_id=? AND run_kind='morning' AND mode='analysis' "
                        "AND status='completed' AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 20", (exp_id,)).fetchall()
    mins = sorted((clock.parse_iso(r["finished_at"]) - clock.parse_iso(r["started_at"])).total_seconds() / 60 for r in rows)
    return mins[len(mins) // 2] if mins else None


# Line icons for the app shell, as inline SVG (the CSP blocks inline styles and scripts, not markup). 24x24, stroked;
# colour and size come from app.css (.icon).
ICONS = {
    "logo": '<path d="M4 16l5-5 4 3 7-7"/><path d="M15 7h5v5"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    "portfolio": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M3 13h18"/>',
    "decisions": ('<path d="M3 6l1.5 1.5L7 5"/><path d="M3 12l1.5 1.5L7 11"/><path d="M3 18l1.5 1.5L7 17"/><path d="M11 6h10"/>'
                  '<path d="M11 12h10"/><path d="M11 18h10"/>'),
    "activity": '<path d="M7 4 3 8l4 4"/><path d="M3 8h14"/><path d="m17 12 4 4-4 4"/><path d="M21 16H7"/>',
    "runs": '<circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l5.5-3.5z"/>',
    "health": '<path d="M12 3l8 3v6c0 4.5-3.4 8.4-8 9-4.6-.6-8-4.5-8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    "settings": ('<path d="M4 6h9"/><path d="M17 6h3"/><circle cx="15" cy="6" r="2"/><path d="M4 12h3"/><path d="M11 12h9"/>'
                 '<circle cx="9" cy="12" r="2"/><path d="M4 18h11"/><path d="M19 18h1"/><circle cx="17" cy="18" r="2"/>'),
    "paper": '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/><path d="M9 12h6"/><path d="M9 16h4"/>',
    "updown": '<path d="m8 9 4-4 4 4"/><path d="m8 15 4 4 4-4"/>',
    "terminal": '<path d="m4 17 6-5-6-5"/><path d="M12 19h8"/>',
    "check": '<path d="m5 12 5 5 9-10"/>',
    "chev": '<path d="m9 6 6 6-6 6"/>',
    "alert": '<path d="M12 3 2 20h20z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5"/><path d="M12 8h.01"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "minus": '<path d="M6 12h12"/>',
    "tour": '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/>',
}


def icon(name: str, cls: str = "") -> str:
    return f'<svg class="icon {cls}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


# The sidebar's seven pages, in order. One page shows at a time (app.js); every section keeps its id, so any existing
# link (#journal, #risk, #runs...) opens the page that holds it. The owner found the single 6,800 px page of twelve
# sections behind an anchor bar "not intuitive" (after review round 13).
# SYNC WARNING: app.js (router), tests/test_dashboard.py PAGES, and the section-to-page map in _page().
PAGE_NAV = (("home", "Home"), ("portfolio", "Portfolio"), ("decisions", "Decisions"), ("activity", "Activity"),
            ("runs", "Runs"), ("health", "Data & health"), ("settings", "Settings"))
# Pages the guided tour lights whole, from the title down (a page taller than the screen is lit from its top): their
# content changes with the record (empty, a few rows, sixty runs), and the page is the one thing every state renders.
# Home, Data & health and Settings are toured card by card instead, so they have no page anchor (an anchor no step
# uses is an orphan).
# SYNC WARNING: static/tour.js targets these names; tests/test_tour.py renders every database state and checks them.
TOUR_PAGE_ANCHORS = {"portfolio": "page-portfolio", "decisions": "page-decisions", "activity": "page-activity",
                     "runs": "page-runs"}
# (Data & health is toured panel by panel: lit whole, it was taller than a screen and its card named panels it could
# not show. See health().)


def page_nav(d) -> str:
    """Sidebar entries, each with the one signal that says whether it needs a look (a count or a coloured dot)."""
    meta = {}
    npos, npend = len(d["val"].positions), len(d["pending"])
    if npos or npend:
        meta["portfolio"] = f'<span class="nav-meta">{h(plural(npos, "position") if npos else f"{npend} pending")}</span>'
    live = d["lock"] is not None and not d["fixture"]
    stopped = d["dangling_run"] is not None and not d["archived"]
    failed = d["failed_run"] is not None and not d["archived"]
    if live or stopped or failed:
        kind, what = (("live", "run in progress") if live else ("bad", "stopped run" if stopped else "failed run"))
        meta["runs"] = f'<span class="nav-dot {kind}" role="img" aria-label="{what}"></span>'
    stale = any(q["status"] != "FRESH" for q in d["dq"].values())
    recon_bad = any(not r["passed"] for r in d["recon"])
    if recon_bad or stale or d["mt"]["provisional_sessions"]:
        kind, what = ("bad", "reconciliation failed") if recon_bad else ("warn", "stale data or provisional valuations")
        meta["health"] = f'<span class="nav-dot {kind}" role="img" aria-label="{what}"></span>'
    items = "".join(f'<a class="nav-item" href="#{p}" data-view="{p}">{icon(p)}<span>{h(label)}</span>{meta.get(p, "")}</a>'
                    for p, label in PAGE_NAV)
    return f'<nav class="nav" aria-label="Pages">{items}</nav>'


def tabbar(d, db_kind: str) -> str:
    """Phones: the four most-used pages as bottom tabs, and More for the rest (Activity, Data & health, Settings), the
    fixture switch and the slash commands, so every page is one tap away (a faded, sideways-scrolling strip hid three:
    critic, round 13). app.css shows it below 760 px; the router marks the tab of the page in view.
    SYNC WARNING: PAGE_NAV (the same pages) and the router in app.js ([data-view], [data-views])."""
    tabs = (("home", "Home"), ("portfolio", "Portfolio"), ("decisions", "Decisions"), ("runs", "Runs"))
    dots = {}
    if d["lock"] is not None and not d["fixture"]:
        dots["runs"] = "live"
    elif (d["dangling_run"] is not None or d["failed_run"] is not None) and not d["archived"]:
        dots["runs"] = "bad"
    if any(not r["passed"] for r in d["recon"]):
        dots["more"] = "bad"
    elif any(q["status"] != "FRESH" for q in d["dq"].values()) or d["mt"]["provisional_sessions"]:
        dots["more"] = "warn"
    dot = lambda k: f'<span class="pdot {dots[k]}" aria-hidden="true"></span>' if k in dots else ""
    links = "".join(f'<a class="tab-item" href="#{p_}" data-view="{p_}">{icon(p_)}<span>{h(label)}</span>{dot(p_)}</a>' for p_, label in tabs)
    other = "forward" if d["fixture"] else "fixture"
    more_rows = "".join(f'<a class="menu-row" href="#{p_}" data-view="{p_}"><span class="switcher-avatar">{icon(p_)}</span>'
                        f'<span>{h(label)}</span></a>' for p_, label in (("activity", "Activity"), ("health", "Data & health"),
                                                                          ("settings", "Settings")))
    # SYNC WARNING: static/tour.js. "tabs" is the phone's pages step; "commands" (the menu) and "commands-button" (the More
    # tab it opens from) are the phone half of the commands step, which opens this More menu; the top bar's Commands
    # menu and button carry the same two anchors.
    more = (f'<details class="menu-host tab-more"><summary class="tab-item" data-views="activity health settings" data-tour="commands-button">'
            f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/>'
            f'<circle cx="19" cy="12" r="1.2"/></svg><span>More</span>{dot("more")}</summary><div class="menu" role="menu" data-tour="commands">{more_rows}'
            f'<a class="menu-row" href="/?db={other}"><span class="switcher-avatar">{icon("paper")}</span><span>'
            f'{"Leave the fixture demo" if d["fixture"] else "Open the fixture demo"}</span></a>'
            f'<div class="menu-head">Type these in Claude Code</div>{_cmd_list(d)}</div></details>')
    return f'<nav class="tabbar" aria-label="Pages" data-tour="tabs">{links}{more}</nav>'


def decision_index(conn, d) -> str:
    """Every decision of this experiment, newest first, one line each, opening its drawer. The journal below tells them run
    by run, and shows only its latest runs expanded: a BUY decided before several runs that decided nothing sat inside
    the collapsed "Older runs" (capture, round 14)."""
    rows = conn.execute("SELECT * FROM decisions WHERE experiment_id=? ORDER BY id DESC LIMIT 50", (d["exp"]["id"],)).fetchall()
    if not rows:
        return ""
    kinds = {"PENDING": "info", "FILLED": "good", "REJECTED": "bad", "EXPIRED": "warn", "CANCELED": "neutral"}
    out = []
    for dec in rows:
        o = conn.execute("SELECT * FROM orders WHERE decision_id=? AND origin='ADVISOR'", (dec["id"],)).fetchone()
        if o is None:
            nfail = conn.execute("SELECT COUNT(*) FROM risk_checks WHERE decision_id=? AND passed=0", (dec["id"],)).fetchone()[0]
            st, kind = ("REJECTED", "bad") if nfail else ("RECORDED", "neutral")
        else:
            st, kind = o["status"], kinds.get(o["status"], "neutral")
        size = (usd(dec["notional_cents"]) if dec["notional_cents"] else "ALL" if dec["sell_all"]
                else (h(m.qty_to_str(dec["quantity_micro"])) + " sh") if dec["quantity_micro"] else "—")
        # data-ordered: the decision placed an order (pending, filled, expired or canceled; a rejected one placed none).
        # static/tour.js opens the newest such decision's sheet, which shows a whole decision start to finish.
        ordered = " data-ordered" if o is not None and o["status"] != "REJECTED" else ""
        out.append(f'<tr data-detail="dec-{dec["id"]}"{ordered} tabindex="0"><td>{h(pt(dec["decided_at"]))}</td>'
                   f'<td>{h(action_words(dec["action"]))}</td><th scope="row">{h(dec["symbol"] or "—")}</th><td class="num">{size}</td>'
                   f'<td>{chip(st, kind)}</td><td class="num">#{dec["run_id"]}</td></tr>')
    # SYNC WARNING: static/tour.js opens the side sheet of the first row under this anchor (the newest decision), so
    # rows stay newest first and keep data-detail pointing at a rendered <template>.
    return ('<div class="table-wrap" data-tour="decision-index"><table class="grid-table decision-index"><thead><tr><th>Decided</th><th>Action</th><th>Symbol</th>'
            '<th class="num">Size</th><th>Outcome</th><th class="num">Run</th></tr></thead><tbody>' + "".join(out) + "</tbody></table></div>"
            '<p class="table-note">Newest first. Open one for its thesis, the validator\'s checks and what became of it; the journal '
            'below tells them run by run, with each run\'s market view.</p>')


def health_pill(d) -> str:
    """The top bar's one status: the worst current condition in words, linked to what explains or fixes it (the run's
    drawer, or Data and health). It is what keeps a stopped or failed run, a failed reconciliation, stale data and
    provisional valuations impossible to miss on every page (redesign: one pill instead of six chips).
    SYNC WARNING: the same states are named by health_summary(), next_steps() and page_nav() (its dots)."""
    recon_fail = [r for r in d["recon"] if not r["passed"]]
    fresh, total = sum(1 for q in d["dq"].values() if q["status"] == "FRESH"), len(d["dq"])
    prov = d["mt"]["provisional_sessions"]
    lr = d["last_run"]
    opens = ""
    if d["lock"] and lr is not None:
        # An absolute start time: "started just now" stayed in the always-visible bar of a tab left open.
        started = clock.parse_iso(lr["started_at"]).astimezone(LA).strftime("%-I:%M %p PT")
        kind, text, href, opens = "live", f"Run #{d['lock']['run_id']} in progress, started {started}", "#runs", f"run-{lr['id']}"
    elif d["dangling_run"] is not None and not d["archived"]:
        dr = d["dangling_run"]
        kind, text, href, opens = "bad", f"Run #{dr['id']} stopped (lock expired)", "#runs", f"run-{dr['id']}"
    elif d["failed_run"] is not None and not d["archived"]:
        fr, sub = d["failed_run"], d["next_submitted_run"]
        # The same rule as retry_words: once a run's decisions for the open are recorded, a failed re-analysis lost
        # nothing and a plain retry only reconciles (capture, round 14: "run /paper-morning again" here contradicted the
        # card beside it, which says so).
        tail = ("run /paper-morning again" if sub is None else
                "its decisions stand" if sub["id"] == fr["id"] else f"run #{sub['id']}'s decisions stand")
        kind, text, href, opens = "bad", f"Run #{fr['id']} {fr['status']}: {tail}", "#runs", f"run-{fr['id']}"
    elif d["failed_run"] is not None:
        # /paper-morning acts on the ACTIVE experiment: an archived one's failure is history, not an action.
        fr = d["failed_run"]
        kind, text, href, opens = "neutral", f"Last run #{fr['id']} {fr['status']}", "#runs", f"run-{fr['id']}"
    elif recon_fail:
        kind, text, href = "bad", f"Reconciliation FAILED ({len(recon_fail)})", "#health"
    elif lr is None:
        # Nothing has run: stale or missing prices only mean no run has fetched them yet. An archived experiment that
        # never ran has nothing pending ("Not reconciled yet" implied a check was due).
        kind, href = "neutral", "#health"
        text = "Never reconciled: archived before any run" if d["archived"] else "No runs yet: type /paper-morning in Claude Code"
    elif prov or (total and fresh < total and not d["archived"]):
        # Market-data freshness is the active experiment's concern (an archived one does not settle).
        stale = total and fresh < total and not d["archived"]
        parts = ([plural(prov, "provisional valuation")] if prov else []) + ([f"data {fresh}/{total} fresh"] if stale else [])
        text = " · ".join(parts)
        kind, text, href = "warn", text[:1].upper() + text[1:], "#health"
    elif not d["recon"]:
        kind, text, href = "neutral", "Not reconciled yet: every morning run reconciles first", "#health"
    else:
        kind, text, href = "ok", "All checks pass", "#health"
    data_open = f' data-open="{opens}"' if opens else ""
    return f'<a class="health-pill {kind}" href="{href}"{data_open}><span class="dot"></span>{h(text)}</a>'


def header(d, db_kind: str, view_exp: int, nav_html: str = "") -> tuple[str, str, str]:
    # The top bar's run, data and reconciliation chips became one health pill (health_pill); the phone's strip below
    # names the same condition in short words.
    exp, mt = d["exp"], d["mt"]
    fresh = sum(1 for q in d["dq"].values() if q["status"] == "FRESH")
    total = len(d["dq"])
    recon_fail = [r for r in d["recon"] if not r["passed"]]
    archived_chip = (chip("ARCHIVED (read-only)", "warn", "An archived experiment's history is read-only; make it active in Settings to change it")
                     if exp["id"] != d["active_id"] else "")
    if d["fixture"]:
        # With the year: the scenario's yearless dates (Nov, Dec) otherwise read as upcoming next to today's date.
        clock_note = (f"Fixture clock frozen at {h(d['now'].astimezone(LA).strftime('%a %b %-d, %Y, %-I:%M %p PT'))}, "
                      "the scenario's last run.")
    else:
        close = f"latest stored close {h(day(d['latest_close']))}" if d["latest_close"] else "no prices stored yet"
        clock_note = f"End-of-day prices, never live quotes: {close}. Page loaded {h(d['now'].astimezone(LA).strftime('%-I:%M %p PT'))}."
    banner = ""
    if d["fixture"]:
        # One line on a desktop (the band's height is the sticky offset of everything below it): the tag and the switch
        # always show, the explanation ellipsizes before it would wrap.
        banner = ('<div class="fixture-banner fixture-band" role="note"><strong class="band-tag">FIXTURE DEMO — SYNTHETIC DATA.</strong>'
                  '<span class="band-text">Invented tickers, prices and decisions for an isolated demo: not market data, and not '
                  'your $500 forward paper test.</span>'
                  '<a href="/?db=forward">Switch to the forward paper test</a></div>')
    alert = (d["failed_run"] is not None and not d["archived"]) or bool(recon_fail) or (d["dangling_run"] is not None and not d["archived"])
    # The phone hides the top bar, so this strip names health_pill()'s condition in short words. The fixture's label is a
    # prefix, not a branch of its own (capture, round 14: on the states fixture the strip read only "PAPER TRADING ·
    # FIXTURE DEMO" while a provisional valuation and stale data showed nowhere at the top of a phone).
    # SYNC WARNING: the conditions and their order mirror health_pill().
    label = "PAPER TRADING · FIXTURE DEMO" if d["fixture"] else "PAPER TRADING"
    if d["lock"] and not alert:
        strip_alert = f'<span class="strip-info">run #{h(d["lock"]["run_id"])} in progress</span>'
    elif d["archived"] and not alert:
        strip_alert = f'<span class="strip-muted">archived #{h(exp["id"])}</span>'
    elif alert:
        what = (f"run #{d['dangling_run']['id']} stopped (lock expired)" if d["dangling_run"] is not None else
                f"run #{d['failed_run']['id']} {d['failed_run']['status']}" if d["failed_run"] is not None else "reconciliation failed")
        strip_alert = f'<span class="strip-alert">{h(what)}</span>'
    elif not d["archived"] and (mt["provisional_sessions"] or (total and fresh < total)) and d["last_run"] is not None:
        parts = ([plural(mt["provisional_sessions"], "provisional valuation")] if mt["provisional_sessions"] else []) + \
                ([f"data {fresh}/{total} fresh"] if total and fresh < total else [])
        strip_alert = f'<span class="strip-warn">{h(" · ".join(parts))}</span>'
    else:
        strip_alert = ""
    # The condition is a chip, with no "·" before it: wrapped onto a second line, a separator was left dangling (capture,
    # round 14). With nothing to report, the forward test's strip says what the money is.
    strip = label + ("" if strip_alert or d["fixture"] else " · simulated money")
    # The experiment switcher (Stripe's account switcher): the experiment in view with its status, and a menu of every
    # experiment. A <details> menu needs no script; each row is a plain link to that experiment's view.
    if d["fixture"]:
        # The badge alone: with "frozen" beside it the line was 147 px in a 121 px box (capture, round 14); the sidebar
        # foot says the fixture clock is frozen, and the badge's tooltip says it too.
        status_line = '<span class="badge fixture" title="A frozen synthetic scenario">Fixture demo</span>'
    elif d["archived"]:
        status_line = '<span class="status-dot neu"></span>Archived · read-only'
    else:
        # "Run #N running" alone: "Active · run #17 running" was 137 px in a 121 px box (capture, round 14); only the active
        # experiment ever runs, and the live dot says it.
        status_line = (f'<span class="status-dot live"></span>Run #{d["lock"]["run_id"]} running' if d["lock"]
                       else '<span class="status-dot ok"></span>Active experiment')
    rows = "".join(
        f'<a class="menu-row{" on" if e["id"] == view_exp else ""}" href="/?db={h(db_kind)}&amp;exp={e["id"]}">'
        f'<span class="switcher-avatar">#{e["id"]}</span><span>{h(e["name"])}<span class="sub">'
        f'{"Active" if e["id"] == d["active_id"] else "Archived"} · started with {usd(e["starting_capital_cents"])}</span></span>'
        f'{icon("check") if e["id"] == view_exp else ""}</a>' for e in d["experiments"])
    switcher = f"""<details class="menu-host switcher-host">
    <summary class="switcher" aria-label="Experiment in view: #{exp['id']} {h(exp['name'])}. Switch experiment">
      <span class="switcher-avatar">#{exp['id']}</span>
      <span class="switcher-text"><span class="switcher-name">{h(exp['name'])}</span><span class="switcher-sub">{status_line}</span></span>
      {icon("updown")}
    </summary>
    <div class="menu" role="menu"><div class="menu-head">Experiments</div>{rows}
      {'' if d['fixture'] else '<div class="menu-foot"><a href="#experiments">Manage experiments</a></div>'}</div>
  </details>"""
    # The guided tour's launch control: one element for both layouts. app.css shows it under the seven pages on a desktop
    # (the top bar has no room left at 1280 px) and as a compact "Tour" button at the right of the phone's app bar. It is
    # rendered hidden and static/tour.js reveals it, so no button sits there doing nothing without JavaScript; the hint
    # under it shows until it is dismissed or the tour is taken (tour.js remembers that in this browser).
    # SYNC WARNING: static/tour.js (#tour-launch, .tour-launch-wrap, #tour-hint, .tour-hint-x) and tests/test_tour.py (it
    # must stay a direct part of the sidebar, outside the brand, nav and foot that the phone hides).
    launch = (f'<div class="tour-launch-wrap" hidden><button type="button" class="tour-launch" id="tour-launch" aria-label="Take the tour">'
              f'{icon("tour")}<span class="tl-long">Take the tour</span><span class="tl-short" aria-hidden="true">Tour</span></button>'
              '<p class="tour-hint" id="tour-hint" role="note" hidden><span>New: take the tour to see what each page is for.</span>'
              '<button type="button" class="tour-hint-x" aria-label="Dismiss the tour hint">×</button></p></div>')
    # SYNC WARNING: static/tour.js. "pages" is the desktop's pages step (the whole sidebar: switcher, the seven pages, the
    # data clock); on a phone this element is the app bar, and the step uses the tab bar ("tabs") instead.
    sidebar = f"""<aside class="sidebar" aria-label="Pages" data-tour="pages">
  <div class="brand"><span class="brand-mark">{icon("logo")}</span>paper-advisor</div>
  {switcher}
  {nav_html}
  {launch}
  <div class="sidebar-foot"><span class="clock-note">{clock_note}</span></div>
</aside>"""
    # The top bar: PAPER TRADING, the one health pill (the worst current condition), the archived mark when viewing
    # history, the fixture switch and the slash commands. The record's detailed chips live in Health at a glance.
    other = "forward" if d["fixture"] else "fixture"
    # SYNC WARNING: static/tour.js lights the switch ("fixture-switch") with the health pill and says what it does.
    switch = (f'<a class="switch{" on" if d["fixture"] else ""}" href="/?db={other}" data-tour="fixture-switch" '
              f'aria-label="{"Fixture demo is on: switch to the forward paper test" if d["fixture"] else "Open the fixture demo"}">'
              '<span class="switch-track"></span>Fixture demo</a>')
    commands = (f'<details class="menu-host cmd-host"><summary class="btn sm" data-tour="commands-button">{icon("terminal")}Commands</summary>'
                f'<div class="menu" role="menu" data-tour="commands"><div class="menu-head">Type these in Claude Code</div>{_cmd_list(d)}</div></details>')
    # SYNC WARNING: static/tour.js. The tour's first steps light "paper" (the mark with its note here, the strip on a phone:
    # whichever is laid out) and "health" (the pill, with the archived chip when there is one); "commands" opens the menu.
    topbar = f"""<header class="topbar">
  <span class="paper-id" data-tour="paper"><span class="paper-mark" aria-label="Paper trading">{icon("paper")}PAPER TRADING</span>
  <span class="topbar-note">Simulated money, no real orders</span></span>
  <span class="spacer"></span>
  <div class="status-row" data-tour="health">{archived_chip}{health_pill(d)}</div>
  <span class="vr"></span>
  {switch}
  {commands}
</header>
<div class="paper-strip" data-tour="paper">{strip}{strip_alert}</div>"""
    return banner, sidebar, topbar


# --------------------------------------------------------------------------------------------------------------------
# Home: the three answers first (is it beating cash and the benchmark, and is that evidence yet; can the record be
# trusted; what happens next), then the four money cards and recent activity. Every figure opens its rows.

def badge(kind: str, text: str, ico: str | None = None) -> str:
    return f'<span class="badge {kind}">{icon(ico) if ico else ""}{h(text)}</span>'


def _pct_class(frac: float) -> str:
    """A width class in 1% steps (app.css .pct-0 ... .pct-100): the CSP forbids style attributes."""
    return f"pct-{max(0, min(100, round(frac * 100)))}"


def _compare(d) -> str:
    """Return since inception for the advisor, the benchmark and keeping cash as bars from one zero baseline, then the
    gap in points. A missing value says why in the bar's place. With any loss the baseline sits in the middle, so a
    loss extends left: a left-anchored bar would read a larger loss as "more"."""
    mt, bench, v = d["mt"], d["bench"], d["val"]
    start = d["exp"]["starting_capital_cents"]
    ss = mt["sessions_valued"]
    adv = (v.equity_cents - start) / start if ss else None
    bret = mt["spy_benchmark_return"] if (d["bench_filled"] and mt["spy_benchmark_return"] is not None) else None
    vals = [x for x in (adv, bret) if x is not None]
    scale = max([abs(x) for x in vals] + [0.0001])
    centred = any(x < 0 for x in vals)

    def bar(x, cls):
        frac = abs(x) / scale
        if centred:
            return (f'<span class="c-bar centred"><span class="c-mid"></span><span class="c-fill {cls} {"neg" if x < 0 else "pos"} '
                    f'{_pct_class(frac / 2)}"></span></span>')
        return f'<span class="c-bar"><span class="c-fill {cls} {_pct_class(frac)}"></span></span>'
    rows = ['<span class="c-name"><span class="c-key adv"></span>Advisor</span>'
            + (f'{bar(adv, "adv")}<span class="c-val">{pct(adv)}</span>' if adv is not None
               else '<span class="c-span"><b>n/a</b>no session valued yet</span>')]
    if bret is not None:
        rows.append(f'<span class="c-name"><span class="c-key bench"></span>{h(bench)}</span>{bar(bret, "bench")}<span class="c-val">{pct(bret)}</span>')
    else:
        why = ("archived before its first eligible open; it buys at that open if this experiment is made active again"
               if d["archived"] and not d["bench_filled"] else f"the benchmark starts at the {et_open(d['bench_target'])}")
        rows.append(f'<span class="c-name"><span class="c-key bench"></span>{h(bench)}</span><span class="c-span"><b>n/a</b>{h(why)}</span>')
    rows.append('<span class="c-name"><span class="c-key cash"></span>Keeping cash</span><span class="c-bar"><span class="c-fill zero"></span></span>'
                '<span class="c-val">0.00%</span>')
    if ss and d["bench_filled"] and mt["excess_vs_spy"] is not None:
        gap = f'<strong>{pts(mt["excess_vs_spy"])}</strong>'
    else:
        gap = '<span class="faint">n/a until both are valued</span>'
    return f'<div class="compare">{"".join(rows)}</div><div class="compare-gap"><span>Gap vs {h(bench)}</span>{gap}</div>'


def _evidence(d) -> str:
    """Is the evidence meaningful yet: metrics' own thresholds as two meters (sessions of 20, trades of 10), so progress
    toward "enough to describe anything" is visible; the full caveat is one click away (Learn more)."""
    mt = d["mt"]
    ss, ct = mt["sessions_valued"], mt["closed_trades"]
    need_s, need_t = metrics.MIN_SESSIONS_FOR_STATS, metrics.MIN_TRADES_FOR_STATS
    verdict = (badge("neutral", "Too early to judge") if mt["insufficient_sample"]
               else badge("plain", f"{ss} sessions, {ct} trades"))
    full = f"{plural(ss, 'valued session')} and {plural(ct, 'closed trade')}"
    if mt["insufficient_sample"]:
        full += (f": far too few to judge skill. Win rate and profit factor need at least {need_s} valued sessions and "
                 f"{need_t} closed trades before they describe anything, and even then a small sample proves little")
    notes = []
    if d["code_versions"] > 1:
        # "builds" are numbered in Run history in order of first use, so results can be attributed to one.
        notes.append(f'The advisor\'s code changed during this experiment (<a href="#runs">{d["code_versions"]} builds</a>), '
                     "so these results mix builds")
        full += f". The advisor's code changed during this experiment ({d['code_versions']} builds), so these results mix builds"
    if d["config_versions"] > 1:
        cr = d["cfg_row"]
        notes.append(h(f"risk settings v{cr['version']} since {pt(cr['effective_from'])}"))
        full += (f". Its risk settings changed during this experiment (version {cr['version']} is in force since "
                 f"{pt(cr['effective_from'])}); each decision was checked against the version in force when it was made")
    ann = (f"not annualized (that needs {metrics.ANNUALIZE_AFTER_SESSIONS} sessions)" if mt["annualization_note"] else "not annualized")
    notes.append(ann)
    notes.append(h(f"valued through {day(mt['as_of_session'])}" if mt["as_of_session"] else "no session valued yet"))
    full += ". Returns are since inception and " + ann + "."
    meter = lambda label, n, need: (f'<span class="meter"><span class="meter-label">{label} <b>{n} of {need}</b></span>'
                                    f'<span class="meter-track"><span class="meter-fill {_pct_class(min(1, n / need))}"></span></span></span>')
    # SYNC WARNING: static/tour.js "evidence" step (it quotes the 20 sessions and 10 trades: metrics' thresholds).
    return (f'<div class="evidence" data-tour="evidence"><span class="evidence-verdict">{verdict}</span>{meter("Valued sessions", ss, need_s)}'
            f'{meter("Closed trades", ct, need_t)}<details class="evidence-more"><summary class="link-more">Learn more</summary>'
            f'<p class="sample-note">{h(full)}</p></details><div class="evidence-note"><span>' + " · ".join(notes) + "</span></div></div>")


def _charts(d) -> str:
    """Equity (advisor vs benchmark vs keeping cash) and drawdown on one card, switched by a segmented control
    (app.js; without JavaScript both show). The headings stay for screen readers and name each chart."""
    mt, bench = d["mt"], d["bench"]
    dates = [p_["date"] for p_ in mt["series"]]
    port = [p_["equity_cents"] / 100 for p_ in mt["series"]]
    bench_by = {p_["date"]: p_["equity_cents"] / 100 for p_ in mt["benchmark_series"]}
    start = d["exp"]["starting_capital_cents"] / 100
    prov_idx = [i for i, pnt in enumerate(mt["series"]) if pnt["provisional"]]
    eq = charts.equity_chart(dates, {"Advisor": (port, "line-port"), bench: ([bench_by.get(x) for x in dates], "line-bench")}, start,
                             title=f"Equity of the advisor portfolio, the passive {bench} benchmark and keeping cash",
                             marks={"Advisor": prov_idx})
    dd = charts.drawdown_chart(dates, [p_["drawdown"] for p_ in mt["series"]], title="Advisor drawdown from peak")
    # Phones get their own narrower rendering: the 640-wide chart scaled to a phone made its labels about 6 px tall.
    eq_p = charts.equity_chart(dates, {"Advisor": (port, "line-port"), bench: ([bench_by.get(x) for x in dates], "line-bench")}, start,
                               width=340, height=220, pad_l=44, pad_r=104, marks={"Advisor": prov_idx},
                               title=f"Equity of the advisor portfolio, the passive {bench} benchmark and keeping cash")
    dd_p = charts.drawdown_chart(dates, [p_["drawdown"] for p_ in mt["series"]], width=340, height=140, pad_l=44, pad_r=60,
                                 title="Advisor drawdown from peak")
    prov = [p_["date"] for p_ in mt["series"] if p_["provisional"]]
    note = (f'<p class="chart-note warn-text">Hollow dots mark provisional valuations: {", ".join(day(x) for x in prov)} '
            '(see <a href="#health">Data and health</a>)</p>') if prov else ""
    bs = d["exp"]["benchmark_start_session"]
    if d["archived"] and not d["bench_filled"]:
        # Settlement runs only for the active experiment, so an experiment archived before its first eligible open has
        # not bought its benchmark yet; made active again, catch-up fills that order at its original open.
        bnote = (f"{h(bench)} benchmark: not bought yet. This experiment was archived before its first eligible open, and "
                 "settlement runs only for the active experiment; if it is made active again, the benchmark buys at that "
                 "original open.")
    else:
        when = d["bench_fill_session"] if d["bench_filled"] else d["bench_target"]
        verb = "bought" if d["bench_filled"] else "will buy"
        where = ("the first eligible open after inception" if when == bs else
                 f"because its first eligible open, the {open_short(bs)}, could not fill it (it moves to the next open until one does)")
        bnote = (f"{h(bench)} benchmark: the same starting capital {verb} {h(bench)} at the {et_open(when)}, {where}, with the same "
                 "slippage and dividend treatment (cash on the ex-date, not reinvested).")
    # SYNC WARNING: static/tour.js names "Each valued session" only when this link renders (it looks for
    # data-open="valuations" inside the "chart" anchor).
    each = (f'<a class="link-more" href="#" data-open="valuations">Each valued session{icon("chev")}</a>' if mt["series"] else "")
    legend = (f'<span class="legend" data-pane="eq"><span class="legend-item"><span class="legend-key adv"></span>Advisor</span>'
              f'<span class="legend-item"><span class="legend-key bench"></span>{h(bench)}</span>'
              '<span class="legend-item"><span class="legend-key cash"></span>Keeping cash</span></span>')
    seg = ('<span class="seg" role="group" aria-label="Chart"><button type="button" class="on" data-chart="eq" aria-pressed="true">Equity</button>'
           '<button type="button" data-chart="dd" aria-pressed="false">Drawdown</button></span>')
    return (f'<div class="chart-head">{seg}{legend}<span class="spacer"></span>{each}</div>'
            f'<div class="chart-pane" data-pane="eq"><h3>Equity: advisor vs {h(bench)} vs keeping cash</h3>'
            f'<div class="chart-wrap only-desk">{eq}</div><div class="chart-wrap only-phone">{eq_p}</div>{note}</div>'
            f'<div class="chart-pane" data-pane="dd"><h3>Drawdown from peak</h3>'
            f'<div class="chart-wrap only-desk">{dd}</div><div class="chart-wrap only-phone">{dd_p}</div></div>'
            # Each chart has its own caption (capture, round 14: the benchmark note stayed under the drawdown chart).
            # SYNC WARNING: the peak is metrics._series' (the starting capital or a higher close).
            f'<div class="chart-foot" data-pane="eq">{bnote}</div>'
            '<div class="chart-foot" data-pane="dd">Drawdown: how far the advisor\'s equity at each session\'s close is below its '
            "highest point so far, the starting capital or a higher close; 0% at a new high.</div>")


def perf_card(d) -> str:
    v, exp, mt = d["val"], d["exp"], d["mt"]
    start = exp["starting_capital_cents"]
    ss = mt["sessions_valued"]
    if ss:
        sub = f"{signed_usd(v.equity_cents - start)} since {h(pt(exp['inception_at']))} · started with {usd(start)}"
        # The equity figure opens each valued session, like every other headline number.
        label = f'<a class="hero-label" href="#" data-open="valuations">Equity{icon("chev")}</a>'
    else:
        sub = f"Started with {usd(start)} on {h(pt(exp['inception_at']))}"
        label = '<div class="hero-label">Equity</div>'
    flag = ""
    if d["stale_marks"]:
        # SYNC WARNING: the remedy mirrors morning.start (prices are downloaded before anything else).
        clears = (" In the forward test it clears when a newer bar arrives (the next /paper-morning run downloads prices "
                  "first); this fixture never changes." if d["fixture"] else
                  " It clears when a newer bar arrives: the next morning run downloads prices first, so nothing needs "
                  "doing unless it stays stale.")
        flag = (f'<div class="notice warn perf-flag">{icon("alert")}<div class="k-flag">Provisional: '
                + h("; ".join(f"{s_} is priced at its {day(md)} close (no newer bar)" for s_, md in sorted(d["stale_marks"].items())) + "." + clears)
                + ("" if d["fixture"] else " " + cmd_pill("/paper-morning")) + "</div></div>")
    # SYNC WARNING: static/tour.js "performance" step lights the hero and the comparison (decision 1) together with the
    # chart below them ("chart", a plain wrapper with no style of its own), and says what Drawdown shows.
    return (f'<div class="card perf"><div class="perf-top" data-tour="performance">'
            f'<div>{label}<div class="hero-value">{usd(v.equity_cents)}</div><div class="hero-sub">{sub}</div></div>'
            f'<div><div class="compare-title"><span>Return since inception, net of costs</span></div>{_compare(d)}</div></div>'
            f'{flag}{_evidence(d)}<div class="perf-chart" data-tour="chart">{_charts(d)}</div></div>')


def kpi_cards(d) -> str:
    """Cash, invested, total P&L and max drawdown; each whole card opens its rows (a stretched link), and a second link
    inside one (the reserved cash's pending buys) stays its own target."""
    v, mt = d["val"], d["mt"]
    start = d["exp"]["starting_capital_cents"]
    ss = mt["sessions_valued"]
    prov = " · provisional (see Equity)" if d["stale_marks"] else ""

    def card(label, opener, value, sub):
        return (f'<div class="card kpi"><a class="kpi-open" href="{opener[0]}"{opener[1]}><span class="kpi-label">{label}{icon("chev")}</span></a>'
                f'<div class="kpi-value">{value}</div><div class="kpi-sub">{sub}</div></div>')
    costs = (" · costs included: slippage about " + usd(d["costs"]["slippage_cents"]) + ", commission "
             + usd(d["costs"]["commission_cents"])) if d["counts"]["fills"] else ""
    if ss:
        dd = card("Max drawdown", ("#", ' data-open="valuations"'), pct(mt["max_drawdown"]),
                  f'current {pct(mt["current_drawdown"])} · benchmark max drawdown {pct(mt["spy_max_drawdown"])} · exposure '
                  f'{mt["avg_exposure"] * 100:.0f}% · turnover {mt["turnover"]:.2f}×{prov}')
    else:
        dd = ('<div class="card kpi kpi-static"><span class="kpi-label">Max drawdown</span><div class="kpi-value"><span class="faint">n/a</span></div>'
              '<div class="kpi-sub">drawdown, exposure and turnover start after the first valued session</div></div>')
    # SYNC WARNING: static/tour.js "money" step says what each card opens.
    return ('<div class="kpis" data-tour="money">'
            + card("Cash", ("#", ' data-open="cash"'), usd(v.cash_cents),
                   f'{usd(d["reserved"])} <a class="kpi-sublink" href="#pending">reserved by your pending buys</a>')
            + card("Invested", ("#positions", ""), usd(v.market_value_cents),
                   f'{plural(len(d["state"].positions), "position")} · at latest closes{prov}')
            + card("Total P&amp;L", ("#", ' data-open="pnl"'), signed_usd(v.equity_cents - start),
                   f'realized {signed_usd(d["state"].realized_cents)} · unrealized {signed_usd(v.unrealized_cents)} · '
                   f'dividends {signed_usd(d["state"].dividends_cents)}{costs}{prov}')
            + dd + "</div>")


def activity_card(conn, d) -> str:
    """The latest runs and fills, newest first; each opens its record."""
    exp_id = d["exp"]["id"]
    dangling = d["dangling_run"]["id"] if d["dangling_run"] is not None else None
    items = []
    for r in conn.execute("SELECT * FROM runs WHERE experiment_id=? ORDER BY id DESC LIMIT 6", (exp_id,)):
        words, kind = RUN_STATUS_WORDS.get(r["status"], (r["status"].replace("_", " "), "neutral"))
        if r["id"] == dangling:
            words, kind = "stopped (lock expired)", "bad"
        ico, cls = {"good": ("check", "ok"), "bad": ("x", "bad"), "warn": ("alert", "warn"), "info": ("runs", "info")}.get(kind, ("runs", ""))
        items.append((r["started_at"], f'<li><span class="when">{h(pt(r["started_at"]))}</span><span class="ico {cls}">{icon(ico)}</span>'
                      f'<a class="what" href="#runs" data-open="run-{r["id"]}"><b>Run #{r["id"]}</b> {h(words)} '
                      f'<span class="muted">{h(run_kind_words(r))}</span></a></li>'))
    for f in conn.execute("SELECT * FROM fills WHERE experiment_id=? AND account='PORTFOLIO' ORDER BY id DESC LIMIT 6", (exp_id,)):
        verb = "Bought" if f["side"] == "BUY" else "Sold"
        items.append((f["recorded_at"], f'<li><span class="when">{h(pt(f["recorded_at"]))}</span><span class="ico">{icon("activity")}</span>'
                      f'<a class="what" href="#trades" data-open="fill-{f["id"]}"><b>{verb} {h(f["symbol"])}</b> '
                      f'{h(m.qty_to_str(f["quantity_micro"]))} sh at {dollars(f["fill_price"])} '
                      f'<span class="muted">at the {open_short(f["session_date"])}</span></a></li>'))
    items.sort(key=lambda x: x[0], reverse=True)
    body = "".join(x[1] for x in items[:6]) or '<li class="feed-empty">Nothing yet: runs and fills appear here as they happen.</li>'
    return (f'<section class="card activity"><div class="card-head"><h3 class="card-title">Recent activity</h3>'
            f'<span class="spacer"></span><a class="link-more" href="#runs">All runs{icon("chev")}</a></div><ul class="feed">{body}</ul></section>')


def home(conn, d) -> str:
    return (f'<div class="grid-home"><div class="stack"><section class="summary" aria-label="Account summary">{perf_card(d)}'
            f'{kpi_cards(d)}</section>{activity_card(conn, d)}</div>'
            f'<div class="stack">{next_steps(d)}{health_summary(d)}</div></div>')


def valuations_drawer(d) -> str:
    """Every valued session behind the headline figures (return, gap, drawdown, exposure): a number a reader may
    question drills down to its rows. Provisional sessions carry their reasons."""
    mt, start = d["mt"], d["exp"]["starting_capital_cents"]
    if not mt["series"]:
        return ""
    bench_by = {p_["date"]: p_ for p_ in mt["benchmark_series"]}
    rows, prev = [], start
    for p_ in mt["series"]:
        b = bench_by.get(p_["date"])
        ret = p_["equity_cents"] / start - 1
        bret = (b["equity_cents"] / start - 1) if b else None
        why = "; ".join(provisional_words(x) for x in p_["provisional_reasons"]) if p_["provisional"] else ""
        # Decision columns first (return vs benchmark, then risk), and "provisional" inside the session cell: on a phone
        # only the first columns fit, and the provisional column was the last one, off-screen (reviewer, round 12).
        rows.append(f'<tr><th scope="row">{day(p_["date"])}'
                    + (f' {chip("provisional", "warn")}<div class="small muted val-why">{h(why)}</div>' if why else "") + "</th>"
                    f'<td class="num">{pct(ret)}</td><td class="num">{pct(bret)}</td>'
                    f'<td class="num">{pts(ret - bret if bret is not None else None)}</td><td class="num">{pct(p_["drawdown"])}</td>'
                    f'<td class="num">{usd(p_["equity_cents"])}</td><td class="num">{signed_usd(p_["equity_cents"] - prev)}</td>'
                    f'<td class="num">{p_["exposure"] * 100:.0f}%</td></tr>')
        prev = p_["equity_cents"]
    nprov = sum(1 for p_ in mt["series"] if p_["provisional"])
    body = (f'<p class="small muted">Valued at each session\'s closes (since inception, net of costs). The {h(d["bench"])} column is the '
            "passive benchmark's return; Gap is the advisor's return minus it. "
            + (f"{plural(nprov, 'session')} marked provisional, with the reason under its date." if nprov else "No session is provisional.")
            + "</p>"
            '<div class="table-wrap"><table class="grid-table compact val-table"><thead><tr><th>Session</th><th class="num">Return</th>'
            '<th class="num">' + h(d["bench"]) + '</th><th class="num">Gap</th><th class="num">Drawdown</th><th class="num">Equity</th>'
            '<th class="num">Change</th><th class="num">Invested</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>"
            '<p class="small muted phone-only">Scroll the table sideways for drawdown, equity, change and invested.</p>')
    return _detail("valuations", "Each valued session", body, wide=True)


def money_drawers(conn, d) -> str:
    """The Cash and Total P&L tiles drill down to the rows that add up to them (reviewer, round 8: only Return and
    Drawdown did). Cash: every ledger movement of the portfolio with a running balance that ends at the tile. P&L:
    realized by round trip, unrealized by position, dividends by event; their sum is the tile.
    SYNC WARNING: ledger event types as migrations/0001_initial.sql; totals as accounting.load_state / valuation."""
    exp_id, v, st = d["exp"]["id"], d["val"], d["state"]
    evs = conn.execute("SELECT * FROM ledger_events WHERE experiment_id=? AND account='PORTFOLIO' ORDER BY seq", (exp_id,)).fetchall()
    bal, rows = 0, []
    for ev in evs:
        bal += ev["cash_delta_cents"]
        sym = h(ev["symbol"] or "")
        link = f' · <a href="#" data-open="fill-{ev["fill_id"]}">open the fill</a>' if ev["fill_id"] else ""
        what = {"INITIAL_FUNDING": "Starting capital, funded once",
                "BUY_FILL": f"Buy {sym}{link}", "SELL_FILL": f"Sell {sym}{link}",
                "CASH_DIVIDEND": f"Dividend from {sym}",
                "SPLIT": f"{ev['split_num']}-for-{ev['split_den']} split of {sym} (shares only, no cash)" if ev["split_num"] else f"Split of {sym}",
                }.get(ev["event_type"], f"Correction: {h(ev['memo'] or ev['event_type'].lower())}")
        fee = f" (fee {usd(ev['fee_cents'])})" if ev["fee_cents"] else ""
        # The c-* classes place each cell when a phone stacks the rows (app.css .cash-table).
        rows.append(f'<tr><td class="c-when">{h(pt(ev["effective_at"]))}</td><td class="wrap c-what">{what}{fee}</td>'
                    f'<td class="num c-chg">{signed_usd(ev["cash_delta_cents"]) if ev["cash_delta_cents"] else "—"}</td>'
                    f'<td class="num cash-run c-after">{usd(bal)}</td></tr>')
    reserved = d["reserved"]
    cash = _detail("cash", "Cash: every movement", wide=True, body=(
        f'<p class="small muted">The portfolio\'s cash after each recorded event (fills at their simulated open, dividends on '
        f'the ex-date). Cash now {usd(v.cash_cents)}; {usd(reserved)} of it is reserved by pending buys.</p>'
        '<div class="table-wrap"><table class="grid-table compact cash-table"><thead><tr><th>When</th><th>What</th><th class="num">Cash change</th>'
        '<th class="num">Cash after</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"))
    trips = d["mt"]["trades"]
    t_rows = "".join(f'<tr><td>{h(t["symbol"])}</td><td>{open_short(t["entry_session"])} → {open_short(t["exit_session"])}</td>'
                     f'<td class="num">{signed_usd(t["realized_cents"])}</td><td><a href="#" data-open="fill-{t["buy_fill_ids"][0]}">'
                     "open the entry fill</a></td></tr>" for t in trips if t["buy_fill_ids"])
    p_rows = "".join(f'<tr><td>{h(p_.symbol)}</td><td class="num">{usd(p_.basis_cents)}</td><td class="num">{usd(p_.market_value_cents)}</td>'
                     f'<td class="num">{signed_usd(p_.unrealized_cents)}</td><td><a href="#" data-open="pos-{h(p_.symbol)}">open the position</a></td></tr>'
                     for p_ in v.positions)
    divs = [ev for ev in evs if ev["event_type"] == "CASH_DIVIDEND"]
    d_rows = "".join(f'<tr><td>{h(pt(ev["effective_at"]))}</td><td>{h(ev["symbol"])}</td><td class="num">{signed_usd(ev["cash_delta_cents"])}</td></tr>'
                     for ev in divs)
    total = st.realized_cents + v.unrealized_cents + st.dividends_cents
    table = lambda head, body, empty: ('<div class="table-wrap"><table class="grid-table compact"><thead><tr>' + head + '</tr></thead><tbody>'
                                       + body + "</tbody></table></div>") if body else f"<p class='small'>{empty}</p>"
    costs = (f'<p class="small muted">Costs are inside these figures (paid in the fill prices): slippage about '
             f'{usd(d["costs"]["slippage_cents"])}, commission {usd(d["costs"]["commission_cents"])}.</p>' if d["counts"]["fills"] else "")
    pnl = _detail("pnl", "Total P&L: how it adds up", (
        f'<p>Realized <strong>{signed_usd(st.realized_cents)}</strong>, unrealized <strong>{signed_usd(v.unrealized_cents)}</strong> and '
        # Every part signed like the total (the report signs them too: reports._signed_usd).
        f'dividends <strong>{signed_usd(st.dividends_cents)}</strong> add up to <strong>{signed_usd(total)}</strong> since inception.</p>' + costs
        + "<h4>Realized, by closed round trip</h4>" + table("<th>Symbol</th><th>Entry → exit</th><th class='num'>Realized</th><th></th>", t_rows, "No closed round trips yet.")
        + "<h4>Unrealized, by open position (at latest closes)</h4>" + table("<th>Symbol</th><th class='num'>Cost basis</th><th class='num'>Value</th>"
                                                                             "<th class='num'>Unrealized</th><th></th>", p_rows, "No open positions.")
        + "<h4>Dividends</h4>" + table("<th>Ex-date</th><th>Symbol</th><th class='num'>Credited</th>", d_rows, "No dividends yet.")))
    return cash + pnl


COMMANDS = (("/paper-morning", "each trading morning: settle, research, decide (or hold cash)"),
            ("/paper-status", "any time: read-only snapshot, changes nothing"),
            ("/paper-review", "weekly: honest evaluation and dated hypotheses"))


def cmd_pill(cmd: str) -> str:
    """A command with a Copy button (app.js copies data-copy): the way every slash command is offered."""
    return (f'<span class="cmd-pill"><code>{h(cmd)}</code><button type="button" class="copy" data-copy="{h(cmd)}" '
            f'aria-label="Copy {h(cmd)}">Copy</button></span>')


def _cmd_list(d=None) -> str:
    busy = d["lock"] if d is not None and not d["fixture"] else None
    # Until the next open's cutoff, a plain /paper-morning only reconciles once a run has decided that open (retry_words):
    # the generic "research, decide" line contradicted the box above it that says so (capture, round 13).
    sub = d["next_submitted_run"] if d is not None and not d["fixture"] and not d["archived"] else None

    def why(c, text):
        if c == "/paper-morning" and busy:
            return f"{text} (wait: run #{busy['run_id']} is in progress; see above)"
        if c == "/paper-morning" and sub is not None:
            nxt = d["next"].date.isoformat()
            return (f"{text}; before {cutoff_pt(nxt, d['cfg'].decision_open_buffer_seconds)} it only reconciles and reports "
                    f"(run #{sub['id']}'s decisions for the {open_short(nxt)} are recorded)")
        return text
    return ("<ul class='cmds'>" + "".join(f'<li>{cmd_pill(c)}<span class="small muted">{h(why(c, t))}</span></li>' for c, t in COMMANDS)
            + "</ul>")


def retry_words(d, attempts: int | None = None) -> str:
    """What a plain /paper-morning does for the session it targets now. SYNC WARNING: morning.start reconciles and reports
    (mode reconcile_only) when any run for that session stored a submission, unless new-analysis; else it analyzes.
    attempts: earlier attempts at that open, said when nothing was submitted for it."""
    nxt, sub = d["next"].date.isoformat(), d["next_submitted_run"]
    if sub is not None:
        return (f"Run #{sub['id']}'s decisions for the {open_short(nxt)} are already recorded, so {cmd_pill('/paper-morning')} only "
                f"settles, reconciles and reports; {cmd_pill('/paper-morning new-analysis')} starts a deliberate, versioned "
                f'<span class="nowrap">re-analysis</span>.')
    return (f"Nothing is recorded for the {open_short(nxt)} yet, so {cmd_pill('/paper-morning')} settles, reconciles and analyzes it"
            + (f" ({plural(attempts, 'earlier attempt')} so far, none submitted)." if attempts else "."))


def takeover_words(d, run) -> str:
    """What the next /paper-morning does with a run whose lock expired: morning.start marks every run still in progress
    interrupted, then runs in the mode retry_words describes (it promised a fresh analysis when a submission existed)."""
    nxt, sub = d["next"].date.isoformat(), d["next_submitted_run"]
    head = f"The next {cmd_pill('/paper-morning')} takes it over: it marks run #{run['id']} interrupted, then settles, reconciles and "
    if sub is not None:
        return (head + f"reports. Run #{sub['id']}'s decisions for the {open_short(nxt)} are already recorded, so it does not analyze "
                f"again; {cmd_pill('/paper-morning new-analysis')} takes it over with a deliberate, versioned "
                f'<span class="nowrap">re-analysis</span> instead.')
    return head + f"analyzes the {open_short(nxt)}."


def next_steps(d) -> str:
    if d["archived"]:
        act = next((e for e in d["experiments"] if e["id"] == d["active_id"]), None)
        who = f"#{act['id']} {h(act['name'])}" if act is not None else "the active experiment"
        # The switch is offered right here (it submits the Experiments table's form), locked while a run is in progress.
        # SYNC WARNING: experiments.refuse_if_run_in_progress refuses it on the server too.
        busy = d.get("active_lock")
        # The reason beside the button is also its accessible description (the Experiments table's buttons point to theirs).
        button = (f'<button type="submit" form="activate-{d["exp"]["id"]}" class="secondary" aria-describedby="resume-why"'
                  f'{" disabled" if busy else ""}>Make #{d["exp"]["id"]} active</button>')
        resume = (f"{button} <span class='small muted' id='resume-why'>(locked while run #{busy['run_id']} is in progress on the active "
                  f"experiment; if its Claude Code session has ended, run {cmd_pill('/paper-morning force-recover')} in a new session)</span>"
                  if busy else f"{button} <span class='small muted' id='resume-why'>(archives {who}; nothing is deleted)</span>")
        # SYNC WARNING: static/tour.js "next" (and its closing step) lights whichever of the three cards renders here:
        # archived, fixture scenario or What happens next. Its copy has a variant for each.
        return ('<section class="card next-card" data-tour="next"><h3>This experiment is archived</h3><ul class="next-list">'
                f'<li>Its history is read-only. <code>/paper-morning</code>, <code>/paper-status</code> and <code>/paper-review</code> act on '
                f'the active experiment, {who}.</li>'
                f'<li>To resume this one: {resume}</li></ul>'
                + (f'<p class="archive-note">{d["archive_consequence"]}</p>' if d["archive_consequence"] else "")
                + '</section>')
    if d["fixture"]:
        c = d["counts"]
        parts = [plural(c["fills"], "portfolio fill") + " (plus the benchmark's)", plural(d["mt"]["closed_trades"], "closed trade"),
                 plural(c["rejected"], "rejected proposal")]
        if c["splits"]:
            parts.append(plural(c["splits"], "split"))
        if c["dividends"]:
            parts.append(plural(c["dividends"], "dividend"))
        runs_txt = plural(c["morning"], "morning run") + (f" and {plural(c['review'], 'review')}" if c["review"] else "")
        stale_line = ("<li>Its stale data and provisional valuations are part of the scenario; in the forward test they clear "
                      "when newer bars arrive (the next /paper-morning run downloads prices first).</li>"
                      if d["stale_marks"] or d["mt"]["provisional_sessions"] else "")
        return (f'<section class="card next-card" data-tour="next"><h3>Fixture scenario {badge("fixture", "Frozen")}</h3><ul class="next-list">'
                f'<li>A finished synthetic scenario: {runs_txt}, with {", ".join(parts)}. Nothing here will fill or change.</li>{stale_line}'
                f'<li>The demo is rebuilt with {cmd_pill("paper-advisor/bin/pa demo --rebuild")}</li>'  # no "." after a pill: it wrapped alone'
                '<li><a href="/?db=forward">Open the forward paper test</a> for live status and what happens next.</li></ul></section>')
    nxt, buf = d["next"], d["cfg"].decision_open_buffer_seconds
    lines = []
    if d["lock"]:
        # SYNC WARNING: mirrors morning.start: a live lock refuses a new start unless --force-recover;
        # an expired lock is taken over by a normal start, which marks the old run interrupted.
        lr, lk = d["last_run"], d["lock"]
        tm = d["typical_run_min"]
        typical = ((f"completed runs have taken about {round(tm)} min" if tm >= 1 else "completed runs have taken under a minute")
                   if tm is not None
                   else "no completed run yet to compare with, so there is no typical length")
        hb = clock.parse_iso(lk["heartbeat_at"])
        quiet_min = (d["now"] - hb).total_seconds() / 60
        # A tab left open must not keep saying "just now" (critic, round 5): app.js refreshes the relative times,
        # reloads the page every minute (data-refresh on <body>), and reveals these two warnings at their instant.
        stuck = (f'<span data-show-after="{iso_s(clock.iso(hb + timedelta(minutes=60)))}"{"" if quiet_min > 60 else " hidden"}>'
                 " It has not been renewed for over an hour, so it may be stuck.</span>")
        expired = (f'<span data-show-after="{iso_s(lk["expires_at"])}" hidden> Its lock has expired since this page was loaded: '
                   "reload this page for the current state.</span>")
        # SYNC WARNING: the force-recover argument is defined in .claude/commands/paper-morning.md.
        recover = cmd_pill("/paper-morning force-recover")
        # "renews if research runs past an hour" next to a 3-hour expiry read as a contradiction: say how long one renewal holds.
        held_min = int((clock.parse_iso(lk["expires_at"]) - clock.parse_iso(lk["heartbeat_at"])).total_seconds() // 60)
        hold = plural(held_min // 60, "hour") if held_min % 60 == 0 else plural(held_min, "minute")
        lines.append(f"<li class='attn-info'><strong>Run #{lk['run_id']} is in progress</strong> ({h(RUN_STATUS_WORDS.get(lr['status'], (lr['status'],))[0]) if lr else 'running'}): "
                     f"started {when_html(lr['started_at'], d['now']) if lr else ''}, lock last renewed {when_html(lk['heartbeat_at'], d['now'])}, "
                     f"expires {h(pt(lk['expires_at']))}; {h(typical)}. Each renewal holds the lock for {hold}, and the advisor renews "
                     "it if its research runs past an hour. "
                     f"This page reloads itself every minute while the run is in progress.{stuck}{expired} "
                     f"If the Claude Code session running it has ended, run {recover} in a new session (it tells the advisor that "
                     "session is gone, so it takes over the run's lock), or wait for the lock to expire. "
                     "Settings and new experiments wait until then.</li>")
    elif d["dangling_run"] is not None:
        dr, xl = d["dangling_run"], d["expired_lock"]
        # An expired lock shows only that nothing renewed it (each renewal holds 3 hours); "the session has ended" claimed
        # more than the record knows (capture, round 13).
        lines.append(f"<li class='attn'><strong>Run #{dr['id']} stopped without finishing.</strong> Its lock expired"
                     + (f" at {h(pt(xl['expires_at']))}" if xl is not None and xl["run_id"] == dr["id"] else "")
                     + f" without being renewed, so the Claude Code session running it is no longer working on it. {takeover_words(d, dr)}</li>")
    elif d["failed_run"] is not None:
        fr = d["failed_run"]
        # The stored reason is free text that may hold its own parentheses: say it as a sentence, not "(…)".
        what = (h(error_words(fr["error"]).rstrip(".")) + ".") if fr["error"] else "No reason was recorded."
        same_open = fr["analysis_session_date"] == nxt.date.isoformat()
        retry = retry_words(d, attempts=fr["version"] if same_open else None)
        lines.append(f"<li class='attn'><strong>Run #{fr['id']} {h(fr['status'])}.</strong> {what} {retry}</li>")
    if d["next_submitted_run"] is not None and not d["lock"] and d["dangling_run"] is None:
        # The coming open is already decided, so the next useful run is after that session closes (critic, round 13:
        # "the next useful run ... is left to inference"). SYNC WARNING: morning.start's target session and mode rule.
        following = cal.next_session_after(nxt.open_utc)
        lines.append(f"<li class='next-run'><strong>Next analysis:</strong> once the {open_short(nxt.date.isoformat())} session has "
                     f"closed, run <code>/paper-morning</code> before <strong>{h(cutoff_pt(following.date.isoformat(), buf))}</strong>: it "
                     f"records that session's fills, then decides for the {open_short(following.date.isoformat())}."
                     f"<div class='li-cmd'>{cmd_pill('/paper-morning')}</div></li>")
    lines.append(f"<li>Market: <strong>{h(cal.state_words(cal.market_status(d['now'])))}</strong>. Decisions submitted before "
                 f"<strong>{h(cutoff_pt(nxt.date.isoformat(), buf))}</strong> fill at the <strong>{et_open(nxt.date.isoformat())}</strong>"
                 f"{' (early close day)' if nxt.early_close else ''}; later ones at the following session's open.</li>")
    lines += [f"<li>{h(o['origin'].title())} {h(o['side'])} {h(o['symbol'])} {_order_size(o)} fills at the {et_open(o['target_session_date'])}; "
              f"it is recorded on the first morning run after that session closes.</li>" for o in d["pending"]]
    if not d["pending"]:
        lines.append("<li>No pending orders.</li>")
    return f"""<section class="card next-card" data-tour="next">
  <h3>What happens next</h3>
  <ul class="next-list">{''.join(lines)}</ul>
  <div class="next-cmds"><p class="help">Run the advisor in Claude Code (there is no run button here: the advisor is your Claude Code session).</p>
  {_cmd_list(d)}</div>
</section>"""


def health_summary(d) -> str:
    lr = d["last_run"]
    fails = [r for r in d["recon"] if not r["passed"]]
    bad = sorted(s for s, q in d["dq"].items() if q["status"] != "FRESH")
    unver = sorted(s for s, (ok, _) in d["ident"].items() if not ok)
    items = []
    if lr is None:
        items.append(("neutral", "No runs" if d["archived"] else "No runs yet",
                      "archived: the slash commands act on the active experiment" if d["archived"]
                      else "type /paper-morning in Claude Code to run the first one"))
    else:
        words, k = RUN_STATUS_WORDS.get(lr["status"], (lr["status"], "neutral"))
        if d["archived"] and k == "bad":
            k = "neutral"  # history of an archived experiment, not an alarm (the status-row chip says the same)
        which = "Current run" if lr["status"] in IN_PROGRESS else "Last run"
        if d["dangling_run"] is not None:
            which, words, k = "Last run", "stopped (lock expired)", "bad"
        items.append((k, f"{which} #{lr['id']}: {words}", f"{pt(lr['started_at'])} · {run_kind_words(lr)}"
                      + (f" · {error_words(lr['error'])[:140]}" if lr["error"] else "")))
    items.append(("good" if not bad else "warn", f"Data {len(d['dq']) - len(bad)}/{len(d['dq'])} fresh",
                  "every symbol has the last completed session" if not bad else "stale or missing: " + ", ".join(bad[:6]) + (" …" if len(bad) > 6 else "")
                  + (" · clears when newer bars arrive" + (" (forward test)" if d["fixture"] else " (the next /paper-morning run fetches them first)"))))
    items.append(("good" if not unver else "bad", f"Identity {len(d['ident']) - len(unver)}/{len(d['ident'])} verified",
                  ("synthetic instruments, verified as fixtures" if d["fixture"] else "checked against official listings within 14 days")
                  if not unver else "unverified: " + ", ".join(unver[:6])))
    items.append(("good" if d["recon"] and not fails else ("bad" if fails else "neutral"),
                  "Reconciled" if d["recon"] and not fails else (f"{plural(len(fails), 'reconciliation check')} failed" if fails else
                                                                ("Never reconciled" if d["archived"] and lr is None else "Not reconciled yet")),
                  ", ".join(recon_label(f["check_name"]) for f in fails) if fails else
                  (f"ledger, fills, orders and snapshots agree · checked {pt(d['recon'][0]['at'])}"
                   + (f" by run #{d['recon'][0]['run_id']}" if d["recon"][0]["run_id"] else "") if d["recon"] else
                   ("archived before any run" if d["archived"] else "every morning run reconciles first"))))
    if d["mt"]["provisional_sessions"]:
        latest = d["provisional_rows"][0] if d["provisional_rows"] else None
        why = ("latest " + day(latest["session_date"]) + ": " + "; ".join(provisional_words(x) for x in json.loads(latest["provisional_reasons_json"] or "[]"))
               if latest else "see Data and health")
        items.append(("warn", plural(d["mt"]["provisional_sessions"], "provisional valuation"), why))
    if d["skills"]:
        k_, t_, s_ = skills_summary(d["skills"])
        items.append((k_, t_ + (f" (run #{lr['id']})" if lr is not None else ""), s_))
    if db.engine_issues():
        # Advisory, not a failure (SQLite rates the bug unlikely): neutral, with where the upgrade is.
        items.append(("neutral", "SQLite update advised", f"SQLite {sqlite3.sqlite_version} has the WAL-reset bug (rare)",
                      '<a href="#health">the upgrade to copy ›</a>'))
    # Items are (kind, label, note[, link html]); the link is trusted markup built here (the note is escaped).
    glyph = {"good": ("ok", "check"), "bad": ("bad", "x"), "warn": ("warn", "alert"), "info": ("live", "info"), "neutral": ("neu", "minus")}
    rows = "".join(f'<li><span class="check {glyph.get(it[0], glyph["neutral"])[0]}">{icon(glyph.get(it[0], glyph["neutral"])[1])}</span>'
                   f'<span class="hk">{h(it[1])}</span><span class="hv">{h(it[2])}'
                   + (f" · {it[3]}" if len(it) > 3 else "") + "</span></li>" for it in items)
    # SYNC WARNING: static/tour.js "health-card" step names these rows and what their glyphs mean.
    return (f'<section class="card health-card" data-tour="health-card"><h3>Health at a glance</h3><ul class="health-list">{rows}</ul>'
            f'<a class="link-more card-link" href="#health">Data &amp; health details{icon("chev")}</a></section>')


_DIV_RE = re.compile(r"^cash dividend ([0-9.]+)/share on ([0-9.]+) shares \(ex (\d{4}-\d{2}-\d{2})\)")


def _corporate_actions(conn, exp_id: int, account: str | None = None, symbol: str | None = None):
    sql = ("SELECT * FROM ledger_events WHERE experiment_id=? AND event_type IN ('SPLIT','CASH_DIVIDEND')"
           + (" AND account=?" if account else "") + (" AND symbol=?" if symbol else "") + " ORDER BY effective_at, id")
    args = [exp_id] + ([account] if account else []) + ([symbol] if symbol else [])
    return conn.execute(sql, args).fetchall()


def _ca_words(ev) -> str:
    if ev["event_type"] == "SPLIT":
        num_, den = int(ev["split_num"]), int(ev["split_den"])
        after_delta = int(ev["quantity_delta_micro"])
        # The ledger stores the shares added; before = added x den / (num - den) for a forward split.
        before = after_delta * den // (num_ - den) if num_ != den else None
        if before is not None:
            return (f"{num_}-for-{den} split: {m.qty_to_str(before)} → {m.qty_to_str(before + after_delta)} shares "
                    "(average cost divided accordingly; value unchanged)")
        return f"{num_}-for-{den} split"
    # SYNC WARNING: broker_sim writes "cash dividend <amount>/share on <qty> shares (ex <date>)".
    mt = _DIV_RE.match(ev["memo"] or "")
    if mt:
        amt, qty, ex = mt.groups()
        exact = format((Decimal(amt) * Decimal(qty)).normalize(), "f")
        # broker_sim credits a dividend on its ex-date, and both places that list one show that date beside these words (a
        # position drawer's "Wed Nov 26:" prefix, the Ex-date column), so "ex-date Wed Nov 26" only repeated it (capture, round 13).
        when = "on the ex-date" if ex == ev["session_date"] else f"on {day(ev['session_date'])} for the {day(ex)} ex-date"
        return (f"Cash dividend: ${Decimal(amt):.2f} per share on {h(qty)} shares = ${exact}, credited {when}, rounded down to "
                "the cent (the rule for every cash credit)")
    return f"Cash dividend: {h(_DATE_RE.sub(lambda x: day(x.group(1)), ev['memo'] or ''))}"


def _stop_distance(rule, mark) -> str:
    """How far the last close is from the stop that would sell at the next open (decision 4: is it near its exit?)."""
    lvl = rules.stop_level(rule) if rule is not None else None
    if lvl is None or mark is None or Decimal(mark) <= 0:
        return ""
    gap = (Decimal(mark) - lvl) / Decimal(mark) * 100
    # Base = the close: "how far can it fall before the stop sells it". (close - stop) / stop would overstate it.
    if gap >= 0:
        return f' <span class="muted small">(stop {gap:.1f}% below the last close)</span>'
    return ' <span class="muted small">(last close is below the stop: it sells at the next open)</span>'


def two_line(main: str, sub: str = "") -> str:
    """A table cell's main line with a muted second line (both already escaped)."""
    return f'<span class="two"><span>{main}</span>' + (f'<span class="sub">{sub}</span>' if sub else "") + "</span>"


def exit_cell(rule, mark) -> str:
    """rule_words + _stop_distance in two short lines for the Positions table: the sentence wrapped to twelve lines in
    a 94 px column (capture, round 14). The drawer keeps the full words. SYNC WARNING: same levels as rule_words."""
    lvl = rules.stop_level(rule)
    ref = Decimal(rule["reference_price"]) if rule["reference_price"] not in (None, "") else None
    main, cls, rest = "", "", []
    if lvl is not None:
        rest.append(f"stop {dollars(lvl.quantize(Decimal('0.01')))}")
        if mark is not None and Decimal(mark) > 0:
            # Base = the close, as in _stop_distance: how far it can fall before the stop sells it.
            gap = (Decimal(mark) - lvl) / Decimal(mark) * 100
            main, cls = ((f"stop {gap:.1f}% below the last close", "") if gap >= 0
                         else ("the last close is below the stop", "warn-text"))
            if cls:
                rest.insert(0, "sells at the next open")
    if rule["take_profit_pct"] is not None and ref is not None:
        rest.append(f"take profit {dollars((ref * (1 + Decimal(rule['take_profit_pct']))).quantize(Decimal('0.01')))}")
    if rule["max_holding_sessions"] is not None:
        rest.append(f"exit after {rule['max_holding_sessions']} sessions")
    if not main:
        main, rest = (rest[0], rest[1:]) if rest else ("none", [])
    head = f'<span class="{cls}">{main}</span>' if cls else f"<span>{main}</span>"
    return f'<span class="two">{head}' + (f'<span class="sub">{" · ".join(rest)}</span>' if rest else "") + "</span>"


def positions_table(conn, d) -> tuple[str, str]:
    st, v = d["state"], d["val"]
    rows, tpl = [], []
    for p in v.positions:
        r = rules.active_for(conn, d["exp"]["id"], p.symbol)
        rule = r[0] if r else None
        dec = conn.execute("SELECT * FROM decisions WHERE id=?", (rule["decision_id"],)).fetchone() if rule and rule["decision_id"] else None
        held = None
        if rule and rule["entry_session_date"] and d["asof_session"]:
            held = cal.count_sessions_after(date.fromisoformat(rule["entry_session_date"]), date.fromisoformat(d["asof_session"]))
        mark, mark_date = d["marks"].get(p.symbol, (None, None))
        upct = (p.unrealized_cents / p.basis_cents) if p.basis_cents else None
        did = f"pos-{h(p.symbol)}"
        cas = _corporate_actions(conn, d["exp"]["id"], "PORTFOLIO", p.symbol)
        weight = f"{(p.market_value_cents / v.equity_cents * 100):.1f}%"
        stale = (" " + chip("stale", "warn", "no bar for the latest session; valuations using it are provisional")
                 if p.symbol in d["stale_marks"] else "")
        # Two short lines per cell keep the row inside the card (capture, round 14: the exit-rule sentence wrapped to
        # twelve lines and pushed Thesis off the right edge; weight and average cost now sit under value and shares, so
        # the table fits at 1280 too). The drawer has every value in full.
        rows.append(f'<tr data-detail="{did}" tabindex="0"><th scope="row">{h(p.symbol)}</th>'
                    f'<td class="num">{two_line(usd(p.market_value_cents), weight + " of equity")}</td>'
                    f'<td class="num">{two_line(signed_usd(p.unrealized_cents), pct(upct))}</td>'
                    + (f'<td title="{h(rule_words(rule))}">{exit_cell(rule, mark)}</td>' if rule else f'<td>{chip("no active rule", "warn")}</td>')
                    + f'<td class="num">{two_line(price(mark) + stale, day(mark_date))}</td>'
                    # The drawer's (and the fills') precision: the stop is computed from the entry price, and a cent-rounded
                    # 25.03 beside "$23.02 (8.0% stop)" from a 25.025 entry read as an arithmetic error (capture, round 13).
                    f'<td class="num">{two_line(m.qty_to_str(p.qty_micro), "avg cost " + dollars(st.avg_cost(p.symbol).quantize(Decimal("0.0001"))))}</td>'
                    f'<td class="num">{h(held) if held is not None else "—"}</td>'
                    f'<td class="thesis" title="{h(dec["thesis"] if dec else "")}"><span class="clamp2">{h((dec["thesis"] or "—") if dec else "—")}</span></td></tr>')
        decided = (f'{h(pt(dec["decided_at"]))} · Run #{dec["run_id"]} · <a href="#" data-open="dec-{dec["id"]}">open the decision</a>'
                   if dec else "—")
        conf = f"{Decimal(str(dec['confidence'])) * 100:.0f}%" if dec and dec["confidence"] else "—"
        # The drawer is the position's whole record: value, weight and the stop's distance were only in the row.
        body = _kv([("Value", f"{usd(p.market_value_cents)} at the last close ({(p.market_value_cents / v.equity_cents * 100):.1f}% of equity)"),
                    ("Shares", h(m.qty_to_str(p.qty_micro))), ("Cost basis", usd(p.basis_cents)),
                    ("Average cost", dollars(st.avg_cost(p.symbol).quantize(Decimal("0.0001")))),
                    ("Last close", f"{dollars(mark)} on {day(mark_date)}"
                     + (" " + chip("stale", "warn", "no bar for the latest session; valuations using it are provisional") if p.symbol in d["stale_marks"] else "")),
                    ("Unrealized", f"{signed_usd(p.unrealized_cents)} {pct(upct)}"),
                    ("Exit rules", (rule_words(rule) + _stop_distance(rule, mark)) if rule else chip("no active rule", "warn")),
                    ("Would invalidate it", (dash(rule["invalidation"]) + ' <span class="muted small">(judged by the advisor each morning; '
                                             'the mechanical exit is the rule above)</span>') if rule else "—"),
                    ("Entry session", day(rule["entry_session_date"]) if rule else "—"),
                    ("Sessions since entry", f"{dash(held)} (completed sessions after the entry session; holding limits count the same way)"),
                    ("Splits and dividends", "<br>".join(f"{day(ev['session_date'])}: {_ca_words(ev)}"
                                                         + (f" ({signed_usd(ev['cash_delta_cents'])})" if ev["event_type"] == "CASH_DIVIDEND" else "")
                                                         for ev in cas) or "none"),
                    ("Decided", decided), ("Thesis", dash(dec["thesis"]) if dec else "—"), ("Counterargument", dash(dec["counterargument"]) if dec else "—"),
                    ("Horizon", dash(dec["horizon_text"]) if dec else "—"), ("Confidence", h(conf)),
                    ("Key risks", dash(dec["key_risks"]) if dec else "—")])
        tpl.append(_detail(did, f"{p.symbol} position", body))
    if not rows:
        return ('<p class="empty">No open positions. The account is all cash, which is a valid outcome: the advisor is never required to trade.</p>', "")
    # SYNC WARNING: the p-* classes size the columns in app.css (.positions-table).
    head = ("<tr><th class='p-sym'>Symbol</th><th class='num p-val'>Value</th><th class='num p-unr'>Unrealized</th>"
            "<th class='p-exit'>Exit rules</th><th class='num p-close'>Last close</th><th class='num p-sh'>Shares</th>"
            "<th class='num p-held' title='Sessions since entry: completed sessions after the entry session'>Held</th><th>Thesis</th></tr>")
    return (f'<div class="table-wrap"><table class="grid-table positions-table"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="small muted">Click or tap a row for the thesis, counterargument, invalidation, splits and dividends.</p>', "".join(tpl))


def _order_size(o) -> str:
    if o["notional_cents"]:
        return usd(o["notional_cents"])
    return "ALL shares" if o["sell_all"] else (m.qty_to_str(o["quantity_micro"]) + " sh" if o["quantity_micro"] else "—")


ORIGIN_WORDS = {"ADVISOR": "advisor decision", "RULE": "exit rule", "BENCHMARK": "automatic"}


def _unfillable_words(o) -> str:
    """What happens to this order if its open cannot fill it.
    SYNC WARNING: mirrors broker_sim (_expire, below_minimum_at_fill applies to portfolio buys only) and
    rules.on_exit_expired (an expired exit re-arms its rule)."""
    base = "it is good for that open only: with no bar for it that session"
    if o["account"] == "BENCHMARK":
        # broker_sim._requeue_benchmark: its rule was committed at inception, so it moves on instead of expiring.
        return ("if that open cannot fill it (no bar for it, or no market data within the wait window), it moves to the next "
                "open with the same decision time, until an open fills it. Advisor orders expire instead; the benchmark's rule was "
                "fixed at inception")
    if o["side"] == "BUY":
        return (base + ", or a fill below the minimum order, it expires instead of filling later (the benchmark's order is the one "
                "exception: it moves to the next open)")
    if o["origin"] == "RULE":
        return base + ", it expires instead of filling later and the exit rule is re-armed (checked again at the next close)"
    return base + ", it expires instead of filling later"


def pending_table(conn, d) -> tuple[str, str]:
    pend = d["pending"]
    if not pend:
        return '<p class="empty">No pending orders.</p>', ""
    rows, tpl = [], []
    for o in pend:
        did = f"pend-{o['id']}"
        bench = o["account"] == "BENCHMARK"
        origin = ORIGIN_WORDS.get(o["origin"], o["origin"].lower())
        why = exit_words(o["origin_note"] or o["status_reason"], tail="sells")
        if o["origin"] == "ADVISOR" and o["decision_id"]:
            # An advisor order exists because of its decision: its thesis is the why (the stored status_reason
            # "accepted for market-on-open <ISO date>" is a status code, not a reason).
            t = conn.execute("SELECT thesis FROM decisions WHERE id=?", (o["decision_id"],)).fetchone()
            why = (t["thesis"] if t and t["thesis"] else "") or _event_reason("PENDING", o["status_reason"] or "")
        # One order reads as one cell (account, side and symbol over its origin) and every value with a qualifier takes it
        # as a second line, so the table fits its card (capture, round 14: nine columns ran 47 px past it at 1440 and cut
        # Why mid-word). SYNC WARNING: the o-* classes size the columns in app.css (.pending-table).
        dec_t = clock.parse_iso(o["decided_at"]).astimezone(LA)
        order_main = f'{chip(o["account"].title(), "info" if bench else "neutral")} {h(o["side"])} <span class="sym">{h(o["symbol"])}</span>'
        fills = f'<span class="nowrap">{open_short(o["target_session_date"])}</span>'
        reserved = two_line(usd(o["reserved_cash_cents"]), "the benchmark's own cash" if bench else "") if o["side"] == "BUY" else "—"
        rows.append(f'<tr data-detail="{did}" tabindex="0"><th scope="row">{two_line(order_main, h(origin))}</th>'
                    f'<td class="num">{_order_size(o)}</td><td>{two_line(day(dec_t.date().isoformat()), dec_t.strftime("%-I:%M %p PT"))}</td>'
                    f'<td>{two_line(fills, "paused while archived") if d["archived"] else fills}</td><td class="num">{reserved}</td>'
                    f'<td class="why" title="{h(why)}"><span class="clamp2">{h(why) or "—"}</span></td></tr>')
        if bench:
            why_html = (f"The passive {h(o['symbol'])} benchmark invests all {usd(o['notional_cents'])} of its own starting cash at the first "
                        "eligible open after inception, with the same slippage and dividend treatment as the advisor. It never uses "
                        "the advisor's cash.")
        else:
            why_html = h(why) or "—"
        moved = ""
        if bench:
            # Opens that could not fill the benchmark before it moved on (broker_sim._requeue_benchmark).
            gone = conn.execute("SELECT target_session_date, status_reason FROM orders WHERE experiment_id=? AND account='BENCHMARK' "
                                "AND status='EXPIRED' ORDER BY id", (o["experiment_id"],)).fetchall()
            moved = "<br>".join(f"the {open_short(x['target_session_date'])} could not fill it: "
                                f"{h(ORDER_REASON_WORDS.get(x['status_reason'], x['status_reason'] or ''))}" for x in gone)
        # SYNC WARNING: mirrors broker_sim (MOO from the session's daily bar; good for one session; expiry reasons).
        tpl.append(_detail(did, f"Pending order: {o['side']} {o['symbol']}", _kv([
            ("What", f"{h(o['side'])} {h(o['symbol'])} {_order_size(o)}, market-on-open"),
            ("Account", h(o["account"].title()) + (" (passive benchmark)" if bench else "")), ("Origin", h(origin)),
            ("Why", why_html), ("Decided", h(pt(o["decided_at"]))),
            ("Fills at", f"the {et_open(o['target_session_date'])}: the official open with {num(d['cfg'].slippage_bps)} bps "
                         f"adverse slippage ({'above' if o['side'] == 'BUY' else 'below'} the open), from that session's daily bar"
                         + (". It is paused while this experiment is archived: if the experiment is made active again, the next "
                            "morning run catches it up and it fills at that open" if d["archived"] else "")),
            ("Recorded", "on the first morning run after that session closes (the daily bar exists only after the close)"),
            ("If it cannot fill", _unfillable_words(o)),
            *([("Earlier opens", moved)] if moved else []),
            ("Cash reserved", (usd(o["reserved_cash_cents"]) + (" of the benchmark's own cash" if bench else "")) if o["side"] == "BUY" else "—"),
            ("Decision", f'{h(_decision_ref(conn, o["decision_id"]))} · <a href="#" data-open="dec-{o["decision_id"]}">open it</a>'
                         if o["decision_id"] else "none (automatic)")])))
    return ('<div class="table-wrap"><table class="grid-table pending-table"><thead><tr><th class="o-order">Order</th>'
            '<th class="num o-size">Size</th><th class="o-when">Decided</th><th class="o-fills">Fills at</th>'
            '<th class="num o-cash">Cash reserved</th><th>Why</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div><p class="small muted">A pending market-on-open order fills at the official open of its '
            "target session using that session's daily bar, which exists only after the close; it is recorded on the next morning run. "
            "Click or tap a row for its full record.</p>", "".join(tpl))


def _order_origin_html(f) -> str:
    """Why an order existed, linked to the decision behind it (rule exits keep the decision that set the rule)."""
    if f["account"] == "BENCHMARK":
        return "passive benchmark: all capital at the first eligible open"
    link = f' · <a href="#" data-open="dec-{f["decision_id"]}">open the decision</a>' if f["decision_id"] else ""
    if f["origin"] == "RULE":
        return h(exit_words(f["origin_note"])) + link
    return "advisor decision" + link


def fills_table(conn, d) -> tuple[str, str]:
    exp_id = d["exp"]["id"]
    fs = conn.execute("SELECT f.*, o.origin, o.decision_id, o.origin_note FROM fills f JOIN orders o ON o.id=f.order_id WHERE f.experiment_id=? ORDER BY f.id DESC",
                      (exp_id,)).fetchall()
    cas = _corporate_actions(conn, exp_id)
    ca_html = ""
    if cas:
        ca_rows = "".join(f'<tr><td>{day(ev["session_date"])}</td><td>{chip(ev["account"].title(), "info" if ev["account"] == "BENCHMARK" else "neutral")}</td>'
                          f'<th scope="row">{h(ev["symbol"])}</th><td>{_ca_words(ev)}</td>'
                          f'<td class="num">{signed_usd(ev["cash_delta_cents"]) if ev["event_type"] == "CASH_DIVIDEND" else "—"}</td></tr>' for ev in cas)
        ca_html = (f'<h4>Splits and dividends ({len(cas)})</h4><div class="table-wrap"><table class="grid-table"><thead><tr><th>Ex-date</th>'
                   f'<th>Account</th><th>Symbol</th><th>What happened</th><th class="num">Cash</th></tr></thead><tbody>{ca_rows}</tbody></table></div>'
                   '<p class="small muted">Why share counts and average costs in Positions can differ from the fills above.</p>')
    if not fs:
        return ('<p class="empty">No fills yet. A recommendation is not a fill: fills appear only after a target session has opened, closed '
                'and been settled by the next morning run.</p>' + ca_html, "")
    rows, tpl = [], []
    trades = d["mt"]["trades"]
    for f in fs:
        did = f"fill-{f['id']}"
        late = (clock.parse_iso(f["recorded_at"]) - clock.parse_iso(f["sim_exec_at"])).total_seconds() / 3600
        rows.append(f'<tr data-detail="{did}" tabindex="0"><td>{chip(f["account"].title(), "info" if f["account"] == "BENCHMARK" else "neutral")}</td>'
                    f'<th scope="row">{h(f["symbol"])}</th><td>{h(f["side"])}</td><td class="num">{m.qty_to_str(f["quantity_micro"])}</td>'
                    f'<td class="num">{fill_px(f["fill_price"])}</td><td class="num">{price(f["reference_open"])}</td>'
                    f'<td class="num">{signed_usd(f["net_cash_cents"])}</td><td>{open_short(f["session_date"])}</td><td>{h(pt(f["decided_at"]))}</td>'
                    f'<td>{h(pt(f["recorded_at"]))} <span class="muted small">({late:.0f} h after the open)</span></td></tr>')
        trip = next((t for t in trades if f["id"] in t["sell_fill_ids"]), None)
        # A buy is part of a closed round trip, or its position is still held ("—" said neither).
        entry_trip = next((t for t in trades if f["id"] in t["buy_fill_ids"]), None) if f["side"] == "BUY" else None
        if trip:
            trip_words = (f"realized {signed_usd(trip['realized_cents'])} (entered at the {open_short(trip['entry_session'])})"
                          + (f' · <a href="#" data-open="fill-{trip["buy_fill_ids"][0]}">open the entry fill</a>' if trip["buy_fill_ids"] else ""))
        elif entry_trip:
            trip_words = (f"closed at the {open_short(entry_trip['exit_session'])}: realized {signed_usd(entry_trip['realized_cents'])}"
                          + (f' · <a href="#" data-open="fill-{entry_trip["sell_fill_ids"][-1]}">open the exit fill</a>'
                             if entry_trip["sell_fill_ids"] else ""))
        elif f["side"] == "BUY" and f["account"] == "PORTFOLIO":
            # A link to the position's own drawer, like the other cross-references (plain "(see Positions)" was not one).
            held_now = any(p_.symbol == f["symbol"] for p_ in d["val"].positions)
            trip_words = ("still open: the position is held · " + f'<a href="#" data-open="pos-{h(f["symbol"])}">open the position</a>'
                          if held_now else "still open")
        elif f["account"] == "BENCHMARK":
            trip_words = "the passive benchmark holds until the experiment ends"
        else:
            trip_words = "—"
        tpl.append(_detail(did, f"Fill: {f['side']} {f['symbol']}", _kv([
            ("Why the order existed", _order_origin_html(f)),
            ("Exit rule triggered" if f["origin"] == "RULE" else "Decision committed",
             h(pt(f["decided_at"])) + (" (at that session's close)" if f["origin"] == "RULE" else "")),
            ("Simulated execution", h(pt(f["sim_exec_at"])) + " (the session's official open)"),
            ("Daily bar retrieved", h(pt(f["bar_retrieved_at"]))), ("Recorded", h(pt(f["recorded_at"])) + f" ({late:.0f} h after the open)"),
            ("Session", et_open(f["session_date"])), ("Official open", dollars(f["reference_open"])),
            ("Slippage", f"{num(f['slippage_bps'])} bps adverse"), ("Fill price", "$" + fill_px(f["fill_price"])),
            ("Shares", h(m.qty_to_str(f["quantity_micro"]))), ("Gross", usd(f["gross_cents"])), ("Commission", usd(f["commission_cents"])),
            ("Cash change", signed_usd(f["net_cash_cents"])),
            ("Round trip", trip_words),
            ("Recorded by", f"Run #{f['run_id']}" if f["run_id"] else "—"), ("Note", dash(f["notes"]))])))
    head = ("<tr><th>Account</th><th>Symbol</th><th>Side</th><th class='num'>Shares</th><th class='num'>Fill price</th><th class='num'>Open</th>"
            "<th class='num'>Cash</th><th>Simulated at</th><th>Decided</th><th>Recorded</th></tr>")
    trades = d["mt"]["trades"]

    def exit_reason(t) -> str:
        for fid in t["sell_fill_ids"]:
            o = conn.execute("SELECT o.origin, o.origin_note, o.decision_id FROM fills f JOIN orders o ON o.id=f.order_id WHERE f.id=?", (fid,)).fetchone()
            if o is not None:
                return exit_words(o["origin_note"]) if o["origin"] == "RULE" else "advisor SELL decision"
        return "—"
    def entry_decision(t):
        for fid in t["buy_fill_ids"]:
            r = conn.execute("SELECT o.decision_id FROM fills f JOIN orders o ON o.id=f.order_id WHERE f.id=?", (fid,)).fetchone()
            if r is not None and r["decision_id"]:
                return r["decision_id"]
        return None

    def trow_open(t):
        did = entry_decision(t)
        return f'<tr data-detail="dec-{did}" tabindex="0">' if did else "<tr>"
    trows = "".join(f'{trow_open(t)}<th scope="row">{h(t["symbol"])}</th><td>{day(t["entry_session"])}</td><td>{day(t["exit_session"])}</td>'
                    f'<td class="num">{usd(t["cost_cents"])}</td><td class="num">{usd(t["proceeds_cents"])}</td><td class="num">{signed_usd(t["realized_cents"])}</td>'
                    f'<td class="num">{usd(t["dividends_cents"])}</td><td class="wrap">{h(exit_reason(t))}</td></tr>' for t in trades)
    closed = (f'<h4>Closed round trips ({len(trades)})</h4><div class="table-wrap"><table class="grid-table"><thead><tr><th>Symbol</th><th>Entry</th>'
              f'<th>Exit</th><th class="num">Cost</th><th class="num">Proceeds</th><th class="num">Realized</th><th class="num">Dividends</th>'
              f'<th>Why it exited</th></tr></thead><tbody>{trows}</tbody></table></div>'
              '<p class="small muted">Click or tap a round trip for the decision that entered it.</p>' if trades
              else '<p class="small muted">No closed round trips yet.</p>')
    mt = d["mt"]
    win = "n/a" if mt["win_rate"] is None else "%.0f%%" % (mt["win_rate"] * 100)
    pf = ("n/a (no losing trades)" if mt["closed_trades"] else "n/a") if mt["profit_factor"] is None else "%.2f" % mt["profit_factor"]
    aw = usd(mt["avg_win_cents"]) if mt["avg_win_cents"] else "n/a"
    al = usd(mt["avg_loss_cents"]) if mt["avg_loss_cents"] else "n/a"
    stats = (f'<p class="small">Win rate {win} · average win {aw} · average loss {al} · profit factor {pf} '
             f'<span class="muted">({plural(mt["closed_trades"], "closed trade")}; not meaningful below {metrics.MIN_TRADES_FOR_STATS})</span></p>')
    return (f'<div class="table-wrap"><table class="grid-table"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>{stats}{closed}{ca_html}',
            "".join(tpl))


def _checks_html(conn, decision_id: int) -> str:
    # Failures first: the drawer answers "why was this refused?" before listing what passed.
    checks = conn.execute("SELECT * FROM risk_checks WHERE decision_id=? ORDER BY passed, id", (decision_id,)).fetchall()
    if not checks:
        return ""
    items = []
    for c in checks:
        vals = check_values(c["check_name"], c["observed"], c["limit_value"])
        items.append(f'<li class="{"ck-pass" if c["passed"] else "ck-fail"}">{chip("pass", "good") if c["passed"] else chip("FAIL", "bad")} '
                     f'<span class="ck-name">{h(check_label(c["check_name"], bool(c["passed"])))}</span>'
                     + (f'<div class="ck-vals">{vals}</div>' if vals else "")
                     + (f'<div class="ck-note">{h(c["message"] or (CHECK_PASS_NOTES.get(c["check_name"], "") if c["passed"] else ""))}</div>'
                        if (c["message"] or (c["passed"] and c["check_name"] in CHECK_PASS_NOTES)) else "") + "</li>")
    nfail = sum(1 for c in checks if not c["passed"])
    return (f"<h3>Deterministic checks <span class='small muted'>{nfail} failed of {len(checks)}</span></h3>"
            f"<ul class='checks'>{''.join(items)}</ul>")


def _decision_outcome(conn, dec, o) -> str:
    """What became of the decision, in one line: the first thing the drawer answers."""
    failed = [r[0] for r in conn.execute("SELECT check_name FROM risk_checks WHERE decision_id=? AND passed=0 ORDER BY id", (dec["id"],))]
    nfail = len(failed)
    which = f" Failed: {h(', '.join(check_label(c, passed=False) for c in failed))}." if failed else ""
    if o is None:
        if nfail:
            return f"{chip('REJECTED', 'bad')} Refused by the validator: {plural(nfail, 'check')} failed; nothing changed.{which}"
        return f"{chip('RECORDED', 'neutral')} Recorded; no order was needed."
    st = o["status"]
    if st == "REJECTED":
        return f"{chip('REJECTED', 'bad')} Rejected by the validator: {plural(nfail, 'check')} failed; no order was placed.{which}"
    if st == "PENDING":
        return (f"{chip('PENDING', 'info')} Accepted: a market-on-open order for the {et_open(o['target_session_date'])} · "
                f'<a href="#" data-open="pend-{o["id"]}">open the order</a>')
    if st == "FILLED":
        f = conn.execute("SELECT * FROM fills WHERE order_id=? ORDER BY id LIMIT 1", (o["id"],)).fetchone()
        if f is not None:
            return (f"{chip('FILLED', 'good')} Filled at the {open_short(f['session_date'])}: {h(m.qty_to_str(f['quantity_micro']))} shares "
                    f"at ${fill_px(f['fill_price'])} (cash {signed_usd(f['net_cash_cents'])}).")
        return f"{chip('FILLED', 'good')} Filled."
    why = ORDER_REASON_WORDS.get(o["status_reason"], o["status_reason"] or "").rstrip(".")
    verb = "Expired unfilled" if st == "EXPIRED" else ("Canceled" if st == "CANCELED" else st.title())
    if o["status_reason"] == "no_bar_for_target_session":
        sym, sd = o["symbol"], o["target_session_date"]
        prev = conn.execute("SELECT MAX(session_date) FROM price_bars WHERE symbol=? AND session_date<?", (sym, sd)).fetchone()[0]
        nxt = conn.execute("SELECT MIN(session_date) FROM price_bars WHERE symbol=? AND session_date>?", (sym, sd)).fetchone()[0]
        near = " and ".join(day(x) for x in (prev, nxt) if x)
        why = (f"No {sym} bar was stored for {day(sd)}" + (f" (bars exist for {near})" if near else "")
               + ". A missing bar means it did not trade that day (for example a halt) or the data feed skipped it; the stored data "
                 "cannot tell which")
    tail = (" Advisor orders are good for one open only, so it was not carried to a later open; the advisor can decide again."
            if st == "EXPIRED" and o["origin"] == "ADVISOR" else "")
    return f"{chip(st, 'warn' if st == 'EXPIRED' else 'neutral')} {verb}" + (f": {h(why)}." if why else ".") + tail


def _decision_ref(conn, decision_id: int) -> str:
    """'the BUY decided Fri Nov 21 in run #1': how the owner can recognize a decision; its database id is shown nowhere."""
    r = conn.execute("SELECT action, decided_at, run_id FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if r is None:
        return "an earlier decision"
    when = clock.parse_iso(r["decided_at"]).astimezone(LA).date().isoformat()
    return f"the {action_words(r['action'])} decided {day(when)} in run #{r['run_id']}"


# SYNC WARNING: evidence kinds as the decision schema (briefing.decision_schema: web|news|filing|note|macro|earnings).
EVIDENCE_KIND_WORDS = {"web": "web page", "news": "news", "filing": "filing", "note": "note", "macro": "macro data",
                       "earnings": "earnings"}


def _decision_detail(conn, dec, trades=()) -> str:
    ev_ids = json.loads(dec["evidence_ids_json"] or "[]")
    evs = [conn.execute("SELECT * FROM evidence_items WHERE id=?", (i,)).fetchone() for i in ev_ids]
    ev_html = "".join(f'<li><strong>{h(e["title"])}</strong> <span class="muted small">kind: {h(EVIDENCE_KIND_WORDS.get(e["kind"], e["kind"]))} · {dash(e["publisher"])} · published '
                      f'{h(pt(e["published_at"]) if e["published_at"] else "time unknown")} · retrieved {h(pt(e["retrieved_at"]))}</span>'
                      + (f' <a href="{h(e["url"])}" rel="noopener noreferrer" target="_blank">source</a>' if e["url"] else "")
                      + f'<div class="small">{h(e["summary"][:600])}</div></li>' for e in evs if e is not None) or "<li>No evidence cited.</li>"
    o = conn.execute("SELECT * FROM orders WHERE decision_id=? AND origin='ADVISOR'", (dec["id"],)).fetchone()
    life = ""
    if o is not None:
        evs2 = conn.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (o["id"],)).fetchall()
        life = "<h3>Order lifecycle</h3><ol class='small'>" + "".join(
            f"<li>{h(pt(e['recorded_at']))}: {h(e['from_status'] or 'new')} → <strong>{h(e['to_status'])}</strong> — "
            f"{h(_event_reason(e['to_status'], e['reason']))}</li>" for e in evs2) + "</ol>"
    tools = ", ".join(tool_words(x) for x in json.loads(dec["skills_used_json"] or "[]")) or "none recorded"
    size = (usd(dec["notional_cents"]) if dec["notional_cents"] else
            ("ALL shares" if dec["sell_all"] else (h(m.qty_to_str(dec["quantity_micro"])) + " sh" if dec["quantity_micro"] else "—")))
    exit_rules = ", ".join(x for x in [f"stop {frac_pct(dec['stop_loss_pct'])} below entry" if dec["stop_loss_pct"] else "",
                                         f"target +{frac_pct(dec['take_profit_pct'])}" if dec["take_profit_pct"] else "",
                                         f"exit after {dec['max_holding_sessions']} sessions" if dec["max_holding_sessions"] else ""] if x) or "—"
    conf = f"{Decimal(str(dec['confidence'])) * 100:.0f}%" if dec["confidence"] else "—"
    later = []
    for ro in conn.execute("SELECT * FROM orders WHERE decision_id=? AND origin='RULE' ORDER BY id", (dec["id"],)).fetchall():
        rf = conn.execute("SELECT * FROM fills WHERE order_id=? ORDER BY id LIMIT 1", (ro["id"],)).fetchone()
        if rf is not None:
            res = (f"sold {h(m.qty_to_str(rf['quantity_micro']))} shares at ${fill_px(rf['fill_price'])} at the {open_short(rf['session_date'])} "
                   f"(cash {signed_usd(rf['net_cash_cents'])})")
        elif ro["status"] == "PENDING":
            res = f"sell order pending for the {open_short(ro['target_session_date'])}"
        else:
            res = f"sell order {h(ro['status'].lower())}: {h(ORDER_REASON_WORDS.get(ro['status_reason'], ro['status_reason'] or ''))}"
        link = f' · <a href="#" data-open="fill-{rf["id"]}">open the exit fill</a>' if rf is not None else ""
        later.append(f"{h(exit_words(ro['origin_note'], tail=None))}; {res}{link}")
    # An entry (BUY) can be closed by a rule another decision restated (e.g. a later HOLD): follow the round trip.
    if o is not None and o["status"] == "FILLED":
        buy_ids = {r[0] for r in conn.execute("SELECT id FROM fills WHERE order_id=?", (o["id"],))}
        for t in trades:
            if not buy_ids & set(t["buy_fill_ids"]):
                continue
            sell_id = t["sell_fill_ids"][-1] if t["sell_fill_ids"] else None
            so = conn.execute("SELECT o.origin, o.origin_note, o.decision_id FROM fills f JOIN orders o ON o.id=f.order_id WHERE f.id=?",
                              (sell_id,)).fetchone() if sell_id else None
            reason = (exit_words(so["origin_note"], tail=None) if so is not None and so["origin"] == "RULE" else "advisor SELL decision")
            links = f' · <a href="#" data-open="fill-{sell_id}">open the exit fill</a>' if sell_id else ""
            if so is not None and so["decision_id"] and so["decision_id"] != dec["id"]:
                links += f' · <a href="#" data-open="dec-{so["decision_id"]}">open the decision that set the exit</a>'
            later.append(f"Round trip closed at the {open_short(t['exit_session'])}: {h(reason)}; realized {signed_usd(t['realized_cents'])}"
                         f" (dividends {signed_usd(t['dividends_cents'])}){links}")
    restated = []
    if dec["action"] == "HOLD":
        # SYNC WARNING: rules.replace_from_hold marks the old rule "restated by HOLD decision <id>".
        for old in conn.execute("SELECT * FROM position_rules WHERE experiment_id=? AND status='SUPERSEDED' AND status_reason=? ORDER BY id",
                                (dec["experiment_id"], f"restated by HOLD decision {dec['id']}")).fetchall():
            src = (f' (set by <a href="#" data-open="dec-{old["decision_id"]}">{h(_decision_ref(conn, old["decision_id"]))}</a>)'
                   if old["decision_id"] else "")
            restated.append(("Replaced", f"{rule_words(old)}{src}"))
        new_rule = conn.execute("SELECT * FROM position_rules WHERE decision_id=? ORDER BY id DESC LIMIT 1", (dec["id"],)).fetchone()
        if new_rule is not None:
            restated.append(("Rule in force after it", rule_words(new_rule)))
    return (_kv([("Outcome", _decision_outcome(conn, dec, o))]
                + ([("Later", "<br>".join(later))] if later else []) + restated + [
                 ("Decided", f"{h(pt(dec['decided_at']))} · Run #{dec['run_id']}"), ("Action", h(action_words(dec["action"]))),
                 ("Symbol", dash(dec["symbol"])), ("Size", size),
                 ("Thesis", dash(dec["thesis"])), ("Counterargument", dash(dec["counterargument"])),
                 ("Horizon", dash(dec["horizon_text"] or dec["horizon_sessions"])), ("Exit rules", h(exit_rules)),
                 ("Would invalidate it", dash(dec["invalidation"])), ("Confidence", h(conf)),
                 ("Uncertainty", dash(dec["uncertainty"])), ("Key risks", dash(dec["key_risks"])), ("Analysis tools used", h(tools))])
            + _checks_html(conn, dec["id"]) + f"<h3>Evidence</h3><ul class='evidence'>{ev_html}</ul>" + life)


JOURNAL_RUNS = 30  # morning runs shown in the journal; drawers for older linked decisions are added by _page


def journal(conn, d, limit_runs: int | None = None) -> tuple[str, str]:
    runs = conn.execute("SELECT * FROM runs WHERE experiment_id=? AND run_kind='morning' ORDER BY id DESC LIMIT ?",
                        (d["exp"]["id"], limit_runs or JOURNAL_RUNS)).fetchall()
    if not runs:
        if d["archived"]:
            return '<p class="empty">No morning runs. This experiment is archived; the slash commands act on the active one.</p>', ""
        return '<p class="empty">No morning runs yet. Type <code>/paper-morning</code> in Claude Code to run the first one.</p>', ""
    blocks, tpl = [], []
    # SYNC WARNING: as runs_table: the next start marks a run still in progress interrupted (morning.start), so the run
    # that took one over is the next run of the experiment.
    ids = [x[0] for x in conn.execute("SELECT id FROM runs WHERE experiment_id=? ORDER BY id", (d["exp"]["id"],))]
    reasons = takeover_reasons(conn, d["exp"]["id"])

    def err_line(r) -> str:
        if not r["error"]:
            return ""
        w, cls = error_words(r["error"]), "bad-text"
        if r["status"] == "interrupted":
            cls = "warn-text"  # amber like its chip: a recovered interruption, not a failure to act on
            later = next((i for i in ids if i > r["id"]), None)
            if later is not None:
                w = w.replace("recovered by a later run", taken_over_words(reasons.get(r["id"]), later))
        return f"<p class='small {cls}'>{h(w)}</p>"
    for r in runs:
        sub = conn.execute("SELECT * FROM run_submissions WHERE run_id=?", (r["id"],)).fetchone()
        decs = conn.execute("SELECT * FROM decisions WHERE run_id=? ORDER BY seq", (r["id"],)).fetchall()
        cands = conn.execute("SELECT * FROM candidate_assessments WHERE run_id=? ORDER BY symbol", (r["id"],)).fetchall()
        rows = []
        for dec in decs:
            o = conn.execute("SELECT * FROM orders WHERE decision_id=? AND origin='ADVISOR'", (dec["id"],)).fetchone()
            failed = conn.execute("SELECT COUNT(*) FROM risk_checks WHERE decision_id=? AND passed=0", (dec["id"],)).fetchone()[0]
            if o is not None:
                stx = o["status"]
            elif failed:
                stx = "REJECTED"  # e.g. a HOLD restatement or CANCEL refused before any order existed
            else:
                stx = "RECORDED" if dec["action"] in ("HOLD", "NO_TRADE") else "—"
            kind = {"FILLED": "good", "PENDING": "info", "REJECTED": "bad", "EXPIRED": "warn", "CANCELED": "neutral"}.get(stx, "neutral")
            did = f"dec-{dec['id']}"
            size = usd(dec["notional_cents"]) if dec["notional_cents"] else ("ALL" if dec["sell_all"] else "")
            conf = f"{Decimal(str(dec['confidence'])) * 100:.0f}%" if dec["confidence"] else ""
            rows.append(f'<tr data-detail="{did}" tabindex="0"><td>{chip(action_words(dec["action"]), "neutral")}</td><th scope="row">{dash(dec["symbol"])}</th>'
                        f'<td class="num">{size}</td><td>{chip(stx, kind)}</td>'
                        f'<td>{open_short(o["target_session_date"]) if o else "—"}</td>' + clip_td(dec["thesis"])
                        + clip_td(dec["invalidation"] or "—", 100) + f'<td class="num">{h(conf) or "—"}</td></tr>')
            tpl.append(_detail(did, f"Decision: {action_words(dec['action'])} {dec['symbol'] or ''}", _decision_detail(conn, dec, d["mt"]["trades"])))
        mode = chip("reconcile-only rerun", "neutral") if r["mode"] == "reconcile_only" else ""
        cand = ("<details class='small'><summary>Candidates considered (" + str(len(cands)) + ")</summary><ul>" +
                "".join(f"<li><strong>{h(c['symbol'])}</strong> {chip(c['verdict'], 'neutral')} {h(c['assessment'])} — {h(c['reason'])}</li>" for c in cands)
                + "</ul></details>") if cands else ""
        if rows:
            # One fixed column layout for every run's table, so the columns line up from run to run (reviewer, round 12).
            table = (f'<div class="table-wrap"><table class="grid-table journal-table"><colgroup><col class="jc-action"><col class="jc-symbol">'
                     '<col class="jc-size"><col class="jc-status"><col class="jc-target"><col class="jc-thesis"><col class="jc-inval">'
                     '<col class="jc-conf"></colgroup><thead><tr><th>Action</th><th>Symbol</th><th class="num">Size</th><th>Status</th>'
                     f'<th>Target open</th><th>Thesis</th><th>Would invalidate it</th><th class="num">Confidence</th></tr></thead>'
                     f'<tbody>{"".join(rows)}</tbody></table></div>')
        elif r["mode"] == "reconcile_only":
            table = '<p class="small muted">Reconcile-only rerun: this session already had an analysis, so no new decisions were taken.</p>'
        elif r["status"] in ("started", "awaiting_decisions"):
            table = ("<p class='small muted'>In progress: waiting for the advisor's decisions. Nothing is recorded until they are "
                     "submitted and validated.</p>")
        elif r["status"] in ("failed", "interrupted"):
            table = f"<p class='small muted'>No decisions were recorded: the run {'failed' if r['status'] == 'failed' else 'was interrupted'} before any were submitted.</p>"
        else:
            table = '<p class="small muted">No decisions in this run.</p>'
        report = f' · <a href="/report?run={r["id"]}&amp;db={h(d["db_kind"])}">report</a>' if r["report_path"] else ""
        # A rerun takes an attempt number too, as in Run history (run_kind_words): the journal skipped it (capture, round 13).
        version = f" · attempt {r['version']}" if r["version"] > 1 else ""
        try:
            is_session = cal.session(date.fromisoformat(r["analysis_session_date"])) is not None
        except ValueError:
            is_session = False
        target_txt = (f"orders target the {et_open(r['analysis_session_date'])}" if is_session
                      else f"run dated {day(r['analysis_session_date'])}")
        blocks.append(f"""<article class="run-block">
  <div class="run-head"><strong>Run #{r['id']}</strong><span>{h(pt(r['started_at']))}{version} · {target_txt}</span>
   {mode} {run_status_chip(r['status'])} <span class="muted small" title="{h(r['model_id'])}">{h(model_words(r['model_id'], r['model_effort']))}{report}</span></div>
  {f"<p class='view'><strong>Market view:</strong> {h(sub['market_view'])}</p>" if sub else ""}
  {f"<p class='small muted'><strong>What changed:</strong> {h(sub['what_changed'])}</p>" if sub and sub['what_changed'] else ""}
  {table}{cand}
  {err_line(r)}
</article>""")
    if len(blocks) > 5:
        return ("".join(blocks[:5]) + f'<details class="older-runs"><summary>Older runs ({len(blocks) - 5})</summary>'
                + "".join(blocks[5:]) + "</details>"), "".join(tpl)
    return "".join(blocks), "".join(tpl)


def rejected_table(conn, d) -> str:
    rej = conn.execute("SELECT o.*, dd.thesis, dd.id AS did FROM orders o JOIN decisions dd ON dd.id=o.decision_id "
                       "WHERE o.experiment_id=? AND o.status='REJECTED' ORDER BY o.id DESC", (d["exp"]["id"],)).fetchall()
    noorder = conn.execute("SELECT dd.* FROM decisions dd WHERE dd.experiment_id=? AND dd.action IN ('BUY','SELL','HOLD','CANCEL') AND NOT EXISTS "
                           "(SELECT 1 FROM orders o WHERE o.decision_id=dd.id) AND EXISTS (SELECT 1 FROM risk_checks c WHERE c.decision_id=dd.id AND c.passed=0)",
                           (d["exp"]["id"],)).fetchall()
    if not rej and not noorder:
        return '<p class="empty">No rejected proposals.</p>'

    def fail_chips(decision_id):
        fails = conn.execute("SELECT check_name, observed, limit_value FROM risk_checks WHERE decision_id=? AND passed=0 ORDER BY id",
                             (decision_id,)).fetchall()
        return "".join(chip(check_label(f["check_name"], passed=False), "bad", check_values_text(f["check_name"], f["observed"], f["limit_value"]))
                       for f in fails)
    rows = []
    for o in rej:
        rows.append(f'<tr data-detail="dec-{o["did"]}" tabindex="0"><td>{h(pt(o["decided_at"]))}</td><td>{h(o["side"])}</td><th scope="row">{h(o["symbol"])}</th>'
                    f'<td class="num">{_order_size(o)}</td><td class="chips">{fail_chips(o["did"])}</td>' + clip_td(o["thesis"], 160) + "</tr>")
    for dec in noorder:
        rows.append(f'<tr data-detail="dec-{dec["id"]}" tabindex="0"><td>{h(pt(dec["decided_at"]))}</td><td>{h(action_words(dec["action"]))}</td>'
                    f'<th scope="row">{dash(dec["symbol"])}</th><td class="num">—</td><td class="chips">{fail_chips(dec["id"])}</td>'
                    + clip_td(dec["thesis"], 160) + "</tr>")
    return ('<div class="table-wrap"><table class="grid-table"><thead><tr><th>Decided</th><th>Side</th><th>Symbol</th><th class="num">Size</th>'
            f'<th>Why it was rejected</th><th>Thesis</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="small muted">Click or tap a row for every check, with its observed value and limit where it has one.</p>')


# SYNC WARNING: event kinds written by morning.py and review.py (runs.log calls).
def _run_event_words(run, ev) -> str:
    k, msg = ev["kind"], _DATE_RE.sub(lambda x: day(x.group(1)), ev["message"] or "")
    return {"started": f"Started: {run_kind_words(run)}", "briefing": "Briefing written for the advisor",
            "submitted": "Decisions submitted (" + (plural(int(re.match(r"\d+", msg).group(0)), "decision") if re.match(r"\d+ decisions?$", msg) else msg) + ")",
            "completed": "Completed: report written",
            "aborted": f"Aborted: {msg}", "failed": f"Failed: {msg}", "recovered": "Marked interrupted by a later run",
            "model_mismatch": "The model named when decisions were submitted differs from the one recorded at the start",
            "review_submitted": f"Review submitted: {msg[:200]}"}.get(k, msg)


TOOL_STATUS_WORDS = {"ok": "ran", "skipped": "skipped by design", "disabled": "disabled"}


def _review_rows(conn, run) -> list:
    """A review run has no journal entry: its drawer shows the evaluation and the hypotheses it recorded.
    SYNC WARNING: review.submit logs the observations (first 500 characters) as the review_submitted event."""
    ev = conn.execute("SELECT message FROM run_events WHERE run_id=? AND kind='review_submitted' ORDER BY id DESC LIMIT 1",
                      (run["id"],)).fetchone()
    hyps = conn.execute("SELECT id, title, status FROM strategy_hypotheses WHERE review_run_id=? ORDER BY id", (run["id"],)).fetchall()
    links = "<br>".join(f'<a href="#" data-open="hyp-{x["id"]}">{h(x["title"])}</a> ({h(x["status"])})' for x in hyps) or "none"
    return [("Observations", dash(ev["message"] if ev else None)), ("Hypotheses recorded", links)]


def runs_table(conn, d) -> tuple[str, str]:
    rs = conn.execute("SELECT * FROM runs WHERE experiment_id=? ORDER BY id DESC LIMIT 60", (d["exp"]["id"],)).fetchall()
    if not rs:
        return '<p class="empty">No runs yet.</p>', ""
    build_no: dict[str, int] = {}
    prev_of: dict = {}
    # Whether the advisor's code changed between runs matters for judging results, so say it in words: each
    # distinct code tree is "build N" in order of first use in this experiment (the release string alone was the
    # same for every build). The version string and the exact tree hash stay in the cell's tooltip.
    # SYNC WARNING: kpis() counts the same distinct code_tree_sha256 values ("N builds").
    # Builds are numbered across the whole database, so "build 4" names the same code in every experiment (per
    # experiment, run #5 on #2 read "build 1" while #1 was on "build 4" with the same code: reviewer, round 8).
    for r in conn.execute("SELECT code_tree_sha256 FROM runs ORDER BY id"):
        if r["code_tree_sha256"] and r["code_tree_sha256"] not in build_no:
            build_no[r["code_tree_sha256"]] = len(build_no) + 1
    every = conn.execute("SELECT id, code_tree_sha256, run_kind, mode, analysis_session_date FROM runs WHERE experiment_id=? ORDER BY id",
                         (d["exp"]["id"],)).fetchall()
    last = None
    for r in every:
        prev_of[r["id"]] = last
        last = r
    others = conn.execute("SELECT COUNT(*) FROM runs WHERE experiment_id != ?", (d["exp"]["id"],)).fetchone()[0]
    tries: dict[str, int] = {}
    for r in every:
        if r["run_kind"] == "morning" and r["mode"] == "analysis":
            tries[r["analysis_session_date"]] = tries.get(r["analysis_session_date"], 0) + 1
    rel, build_cell = {}, {}
    several = lambda r: tries.get(r["analysis_session_date"], 0) > 1
    for r in rs:
        ver = "v" + (r["code_version"] or "unknown").partition("+")[0]
        b = f"build {build_no[r['code_tree_sha256']]}" if r["code_tree_sha256"] in build_no else "build unknown"
        prev = prev_of.get(r["id"])
        # The table says the same as the sheet's "Code" row in two short lines ("build 2" over "changed since #1").
        if prev is None:
            rel[r["id"]], build_cell[r["id"]] = f"{ver} · {b}", (b, "first run")
        elif r["code_tree_sha256"] and r["code_tree_sha256"] == prev["code_tree_sha256"]:
            rel[r["id"]], build_cell[r["id"]] = f"{ver} · {b}, same as run #{prev['id']}", (b, f"same as #{prev['id']}")
        else:
            rel[r["id"]], build_cell[r["id"]] = f"{ver} · {b}, changed since run #{prev['id']}", (b, f"changed since #{prev['id']}")
    rows, tpl = [], []
    dangling_id = d["dangling_run"]["id"] if d["dangling_run"] is not None else None

    def _log_line(r, ev) -> str:
        w = _run_event_words(r, ev)
        if ev["kind"] == "recovered" and taken_by.get(r["id"]):
            return f"Marked interrupted by run #{taken_by[r['id']]}, which took it over"
        if ev["kind"] == "started" and took_over.get(r["id"]):
            return w + " · took over " + ", ".join(f"run #{x}" for x in took_over[r["id"]])
        return w
    # SYNC WARNING: the same state is named in header(), next_steps() and health_summary() (d["dangling_run"]).
    status_html = lambda r: (chip("stopped (lock expired)", "bad") if r["id"] == dangling_id else run_status_chip(r["status"]))
    # SYNC WARNING: morning.start marks every run still in progress interrupted and lists them in its "started" event.
    taken_by = {r["id"]: next((x["id"] for x in every if x["id"] > r["id"]), None) for r in every}
    took_over: dict[int, list[int]] = {}
    reasons = takeover_reasons(conn, d["exp"]["id"])
    for ev in conn.execute("SELECT run_id, details_json FROM run_events WHERE kind='started' AND run_id IN "
                           "(SELECT id FROM runs WHERE experiment_id=?)", (d["exp"]["id"],)):
        rec = json.loads(ev["details_json"] or "{}").get("recovered") or []
        if rec:
            took_over[ev["run_id"]] = [x["run_id"] for x in rec]

    def problem_words(r) -> str:
        if r["id"] == dangling_id:
            xl = d["expired_lock"]
            return f"Its lock expired{(' at ' + pt(xl['expires_at'])) if xl is not None and xl['run_id'] == r['id'] else ''} before it finished"
        w = error_words(r["error"])
        if r["status"] == "interrupted" and taken_by.get(r["id"]):
            # name it (reviewer, round 13) and say why it could be taken over (capture, round 13)
            w = w.replace("recovered by a later run", taken_over_words(reasons.get(r["id"]), taken_by[r["id"]]))
        return w

    def when_after(iso, ref_iso) -> str:
        """A time in the table: the clock alone on the run's own day (the Started column has the date), else with its date."""
        t = clock.parse_iso(iso).astimezone(LA)
        # Non-breaking spaces: a phone broke "7:12 PM / PT" across lines (capture, round 14).
        out = t.strftime("%-I:%M %p PT" if t.date() == clock.parse_iso(ref_iso).astimezone(LA).date() else "%b %-d, %-I:%M %p PT")
        return out.replace(" ", "\u00a0")

    def problem_short(r) -> str:
        """The table's reason, cause first, in a line or two (capture, round 14: problem_words' sentence, clamped to two
        lines, lost "and run #N took it over"). The cell's tooltip and the run's sheet keep problem_words.
        SYNC WARNING: same cases as problem_words and taken_over_words."""
        if r["id"] == dangling_id:
            xl = d["expired_lock"]
            return (f"Lock expired {when_after(xl['expires_at'], r['started_at'])} before it finished"
                    if xl is not None and xl["run_id"] == r["id"] else "Lock expired before it finished")
        if r["status"] == "interrupted" and taken_by.get(r["id"]):
            by, reason = taken_by[r["id"]], reasons.get(r["id"])
            if reason is None:
                return f"Stopped without finishing; run #{by} took it over"
            kind, until = reason
            return (f"Lock expired {when_after(until, r['started_at'])} without renewal; run #{by} took it over" if kind == "expired"
                    else f"Run #{by} took it over with force-recover (lock held until {when_after(until, r['started_at'])})")
        return error_words(r["error"])
    def _is_session(r) -> bool:
        try:
            return cal.session(date.fromisoformat(r["analysis_session_date"])) is not None
        except ValueError:
            return False

    def target_words(r) -> str:
        if r["run_kind"] == "review":
            return f"ahead of {day(r['analysis_session_date'])}"
        return f"for the {day(r['analysis_session_date'])} open" if _is_session(r) else f"dated {day(r['analysis_session_date'])}"
    data_of = lambda r: "not refreshed" if r["run_kind"] == "review" else data_status_words(r["data_status"])
    # A strip above the table states what every listed run shares, and a column (or a cell's second line) returns only
    # when rows differ (designer, tiering 2.9). Eleven one-line columns made the table 1848 px wide in a 1142 px card,
    # so the reason a run stopped and its report were a sideways scroll away (capture, round 14). Every value is still
    # in the run's sheet.
    models = {model_words(r["model_id"], r["model_effort"]) for r in rs}
    datas = {data_of(r) for r in rs}
    show_model, show_data, show_target = len(models) > 1, len(datas) > 1, len({r["analysis_session_date"] for r in rs}) > 1
    for r in rs:
        sk = conn.execute("SELECT skill_name, status FROM skill_runs WHERE run_id=?", (r["id"],)).fetchall()
        if sk:
            sk_kind, sk_text, sk_detail = skills_summary(sk)
            sk_cell = (f'<td class="num{" warn-text" if sk_kind == "warn" else ""}" title="{h(sk_detail)}">'
                       f'{h(sk_text.replace("Analysis tools ", "").replace(" ran", "").replace("/", " of "))}</td>')
        else:
            sk_cell = '<td class="num">—</td>'
        report = (f'<a href="/report?run={r["id"]}&amp;db={h(d["db_kind"])}" title="The report run #{r["id"]} wrote">open</a>'
                  if r["report_path"] else "—")
        did = f"run-{r['id']}"
        kmain, ksub = run_kind_parts(r, several(r))
        bmain, bsub = build_cell[r["id"]]
        prob, prob_full = problem_short(r), problem_words(r)
        rows.append(f'<tr data-detail="{did}" tabindex="0"><td class="num">#{r["id"]}</td>'
                    # The reason sits under the badge: status and why it stopped are one reading (law of proximity).
                    # Amber for an interruption a later run took over, red for a failure (critic, round 13: "same
                    # meaning, two looks" when the interrupted row was red but its badge and journal line amber).
                    f'<td><span class="stack">{status_html(r)}'
                    + (f'<span class="prob{" warn" if r["status"] == "interrupted" else ""}" title="{h(prob_full)}">{h(prob)}</span>' if prob else "")
                    + f'</span></td><td>{two_line(h(kmain), h(ksub))}</td>'
                    f'<td>{two_line(h(pt(r["started_at"]).removesuffix(" PT")), h(target_words(r)) if show_target else "")}</td>'
                    f'<td title="{h(rel[r["id"]])} · {h(r["code_version"])} · tree sha256 {h(r["code_tree_sha256"] or "unknown")}">{two_line(h(bmain), h(bsub))}</td>'
                    + sk_cell
                    + (f'<td title="{h(r["model_provider"])} / {h(r["model_id"])} / {h(r["model_effort"])}">'
                       f'{h(model_words(r["model_id"], r["model_effort"]))}</td>' if show_model else "")
                    + (f'<td>{h(data_of(r))}</td>' if show_data else "")
                    + f'<td>{report}</td></tr>')
        is_session = _is_session(r)
        secs = int((clock.parse_iso(r["finished_at"]) - clock.parse_iso(r["started_at"])).total_seconds()) if r["finished_at"] else None
        ndec = conn.execute("SELECT COUNT(*) FROM decisions WHERE run_id=?", (r["id"],)).fetchone()[0]
        log = conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY id", (r["id"],)).fetchall()
        tools = "<br>".join(f"{h(tool_words(s['skill_name']))}: {h(TOOL_STATUS_WORDS.get(s['status'], s['status']))}" for s in sk) or "none recorded"
        # A run's drawer covers the panel that says what to do next, so it says it too (reviewer, round 8: a failed run's
        # drawer offered no re-run, a running one no lock expiry or recovery).
        what_now = None
        if r["status"] in IN_PROGRESS:
            lk = d["lock"]
            what_now = (f"In progress; its lock expires {h(pt(lk['expires_at']))}. If the Claude Code session running it has ended, run "
                        f"{cmd_pill('/paper-morning force-recover')} in a new session."
                        if lk is not None and lk["run_id"] == r["id"] else
                        takeover_words(d, r))
        elif r["status"] == "interrupted" and taken_by.get(r["id"]):
            what_now = f'Nothing: <a href="#" data-open="run-{taken_by[r["id"]]}">run #{taken_by[r["id"]]}</a> took it over.'
        elif r["status"] == "failed":
            later = next((x for x in every if x["id"] > r["id"]), None)
            if later is not None:
                what_now = f"Run #{later['id']} followed it."
            elif d["archived"]:
                what_now = "This experiment is archived; the panel at the top says how to make it active again."
            elif r["run_kind"] == "morning":
                what_now = retry_words(d) + " A failed run's decisions, if any, stay in the record."
        # A review places no orders, so "orders target the ... open" was untrue for it.
        for_words = (f"reviewing the record ahead of {day(r['analysis_session_date'])} (a review places no orders)" if r["run_kind"] == "review"
                     else f"orders target the {et_open(r['analysis_session_date'])}" if is_session else f"run dated {day(r['analysis_session_date'])}")
        body = _kv([
            ("Status", status_html(r) + (f" {h(problem_words(r))}" if (r["error"] or r["id"] == dangling_id) else "")),
            ("Kind", h(run_kind_words(r, several(r)))),
            ("For", for_words),
            ("Started", h(pt(r["started_at"]))),
            # Minutes and seconds: "after 6 min" between 4:13 and 4:20 read as a contradiction ("after 0 min 0 s" read as broken).
            ("Finished", (f"marked interrupted {h(pt(r['finished_at']))} by run #{taken_by[r['id']]}, which took it over"
                          if r["status"] == "interrupted" and taken_by.get(r["id"]) else
                          h(pt(r["finished_at"])) + f" ({duration_words(secs)})") if secs is not None else "not finished"),
            *([("Took over", ", ".join(f'<a href="#" data-open="run-{x}">run #{x}</a>' + h(_took_over_why(reasons.get(x)))
                                       for x in took_over[r["id"]]))] if r["id"] in took_over else []),
            ("Advisor model", h(model_words(r["model_id"], r["model_effort"]))), ("Code", h(rel[r["id"]])),
            ("Data", "not refreshed (a review reads the record)" if r["run_kind"] == "review" else h(data_status_words(r["data_status"]))),
            ("Analysis tools", tools),
            *([("What now", what_now)] if what_now else []),
            *(_review_rows(conn, r) if r["run_kind"] == "review" else
              [("Decisions", f'{plural(ndec, "decision")} recorded · <a href="#journal">see {"it" if ndec == 1 else "them"} in the Advisor journal</a>'
                if ndec else "none recorded")]),
            # Say why there is no report ("—" read as missing data): it is written when a run finishes.
            ("Report", report if r["report_path"] else ("none: the run stopped before writing one"
                                                        if r["status"] == "failed" or r["id"] == dangling_id
                                                        else "written when the run finishes" if r["status"] in IN_PROGRESS else "none")),
            ("Run log", "<ol class='small'>" + "".join(f"<li>{h(pt(ev['at']))}: {h(_log_line(r, ev))}</li>" for ev in log) + "</ol>"
             if log else "no events recorded")])
        # Raw identifiers stay one collapsed click away (provenance), never in the visible fields.
        raw = _kv([("Model provider / id / effort", h(f"{r['model_provider']} / {r['model_id']} / {r['model_effort']}")),
                   ("Code version", h(r["code_version"] or "unknown")), ("Code tree sha256", f"<code class='path'>{h(r['code_tree_sha256'] or 'unknown')}</code>"),
                   ("Risk settings version id", h(r["config_version_id"])), ("Strategy version id", h(r["strategy_version_id"]))])
        tpl.append(_detail(did, f"Run #{r['id']}", body + f"<details class='small provenance'><summary>Provenance (raw identifiers)</summary>{raw}</details>"))
    counts: dict[str, int] = {}
    for r in rs:
        k = ("stopped (lock expired)" if r["id"] == dangling_id else "in progress" if r["status"] in IN_PROGRESS
             else RUN_STATUS_WORDS.get(r["status"], (r["status"],))[0])
        counts[k] = counts.get(k, 0) + 1
    order = ("completed", "interrupted", "failed", "stopped (lock expired)", "in progress")
    if len(rs) == 1:
        head = f"<b>1 run</b>, {next(iter(counts))}"
    else:
        head = ((f"<b>{len(rs)} runs</b>" if len(rs) == len(every) else f"<b>Latest {len(rs)} of {len(every)} runs</b>") + ": "
                + " · ".join([f"{counts[k]} {k}" for k in order if k in counts] + [f"{n} {k}" for k, n in counts.items() if k not in order]))
    facts = []
    if not show_target:
        sd = rs[0]["analysis_session_date"]
        facts.append(f"for the <b>{day(sd)}</b> open" if _is_session(rs[0]) else f"dated <b>{day(sd)}</b>")
    if not show_model:
        facts.append(f"advisor <b>{h(next(iter(models)).replace(' · ', ', '))}</b>")
    if not show_data:
        dv = next(iter(datas))
        facts.append("no data recorded" if dv == "—" else f"data <b>{h(dv)}</b>")
    strip = (f'<div class="strip run-strip"><span>{head}</span>'
             + (f'<span class="run-shared">{"Every run: " if len(rs) > 1 else ""}{" · ".join(facts)}</span>' if facts else "") + "</div>")
    heads = ['<th class="c-run num">Run</th>', '<th class="c-status">Status</th>', '<th class="c-kind">Kind</th>',
             '<th class="c-started">Started (PT)</th>', '<th class="c-build">Build</th>', '<th class="c-tools num">Tools</th>',
             *(['<th class="c-model">Advisor</th>'] if show_model else []), *(['<th class="c-data">Data</th>'] if show_data else []),
             '<th class="c-rep">Report</th>']
    cls = "grid-table runs-table" + (" with-model" if show_model else "") + (" with-data" if show_data else "")
    foot = ("Build: each distinct version of the advisor's code, numbered in order of first use; changed means it differs from "
            "the run before. Tools: the analysis tools that ran. Open a run for its log, tools, data and provenance."
            + (" Runs and builds are numbered across all experiments in this database, so numbers used by other experiments "
               "are missing here." if others else ""))
    return (f'<div class="card runs-card">{strip}<div class="table-wrap"><table class="{cls}"><thead><tr>{"".join(heads)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div><p class="table-foot">{foot}</p></div>', "".join(tpl))


def engine_html() -> str:
    """The SQLite version and its known issue, with the upgrade to copy (a plain-text command was the one off-screen fix
    the owner could not copy: reviewer, round 8). SYNC WARNING: db.engine_issues / db.engine_words word the same issue."""
    issues = db.engine_issues()
    if not issues:
        return h(db.engine_words())
    return (f"SQLite {h(sqlite3.sqlite_version)}; known issue: {h(issues[0])} (with Homebrew Python: "
            f"{cmd_pill(db.ENGINE_UPGRADE_HOMEBREW)}).")


def health(conn, d) -> str:
    def row(s, q):
        ok, why = d["ident"][s]
        inst = universe.get(conn, s)
        k = {"FRESH": "good", "STALE": "warn", "SUSPECT": "warn", "MISSING": "bad"}[q["status"]]
        return (f'<tr><th scope="row">{h(s)}</th><td>{h(inst["name"])}</td><td>{chip(q["status"], k)}</td><td>{day(q["last_bar_date"])}</td>'
                f'<td>{day(q["expected_session"])}</td><td>{h(pt_short(q.get("retrieved_at")))}</td>'
                f'<td>{chip("verified", "good") if ok else chip("unverified", "bad", why)}</td><td>{h(", ".join(q["flags"])) or "—"}</td></tr>')
    head = ("<thead><tr><th>Symbol</th><th>Name</th><th>Data</th><th>Last bar</th><th>Expected</th><th>Retrieved (PT)</th><th>Identity</th>"
            "<th>Flags</th></tr></thead>")
    items = sorted(d["dq"].items())
    problems = [(s, q) for s, q in items if q["status"] != "FRESH" or not d["ident"][s][0]]
    fresh = sum(1 for _, q in items if q["status"] == "FRESH")
    verified = sum(1 for s, _ in items if d["ident"][s][0])
    clock_word = "the fixture clock's" if d["fixture"] else "the"
    summary = (f"<p><strong>{fresh}/{len(items)}</strong> symbols have a bar for {clock_word} last completed session "
               f"({day(d['last'].date.isoformat()) if d['last'] else 'n/a'}); <strong>{verified}/{len(items)}</strong> identities verified "
               + ("(synthetic instruments, verified as fixtures)." if d["fixture"] else "against the Nasdaq Trader directories and Yahoo metadata.") + "</p>")
    prob_html = (f'<h4>Needs attention ({len(problems)})</h4><div class="table-wrap"><table class="grid-table compact">{head}<tbody>'
                 + "".join(row(s, q) for s, q in problems) + "</tbody></table></div>") if problems else "<p class='small'>Nothing needs attention.</p>"
    if any(q["status"] in ("STALE", "MISSING") for _, q in problems):
        # SYNC WARNING: mirrors morning.start (refresh first), submission (fresh_data gate on buys only),
        # broker_sim (rules skipped without a bar; orders expire without a bar) and valuation (stale_mark).
        prob_html += ("<p class='small remedy'><strong>What happens next" + (" (in the forward paper test; this fixture never changes)" if d["fixture"] else "")
                      + ":</strong> The next /paper-morning run downloads prices again "
                      "before anything else, so nothing needs doing unless a symbol stays stale. Until a symbol has a bar for the latest "
                      "session, new buys of it are refused, sell decisions are still accepted, its exit rules are checked again when its next "
                      "bar arrives, an order for a session with no bar for it expires, and valuations use its latest earlier close and are "
                      "marked provisional. A missing bar means it did not trade (for example a halt) or the feed skipped it; if it persists "
                      "for several sessions, check the news for a halt, delisting or ticker change."
                      # The forward test offers the command it names (copyable), like every other off-screen fix.
                      + ("" if d["fixture"] else f" Next morning run: {cmd_pill('/paper-morning')}") + "</p>")
    full = (f'<details class="all-data"><summary>Show all {len(items)} symbols</summary><div class="table-wrap"><table class="grid-table compact">'
            f'{head}<tbody>{"".join(row(s, q) for s, q in items)}</tbody></table></div></details>')
    recon = "".join(f'<li>{chip("pass", "good") if r["passed"] else chip("FAIL", "bad")} {h(recon_label(r["check_name"]))}</li>'
                    for r in d["recon"]) or "<li>Not run yet.</li>"
    # When and by which run, as in the health card: a run reconciles at its start and again after its own writes.
    # A check without a run comes from `pa reconcile`, `pa settle` or the fixture demo's final check.
    recon_when_html = (f"<p class='small muted'>Checked {h(pt(d['recon'][0]['at']))} "
                       + (f"by run #{d['recon'][0]['run_id']}." if d["recon"][0]["run_id"] else "outside a morning run (a stand-alone check).")
                       + "</p>") if d["recon"] else ""
    if d["provisional_rows"]:
        lis = "".join(f"<li><strong>{day(r['session_date'])}</strong>: "
                      + "; ".join(h(provisional_words(x)) for x in json.loads(r["provisional_reasons_json"] or "[]")) + "</li>"
                      for r in d["provisional_rows"])
        prov_html = (f"<h4>Provisional valuations ({len(d['provisional_rows'])})</h4><p class='small muted'>A valuation is provisional when it "
                     f"could mislead; it is kept, flagged and counted, never edited.</p><ul class='small'>{lis}</ul>")
    elif d["mt"]["sessions_valued"] == 0:
        # "every session was valued from its own closes" read as a finding when nothing had been valued yet.
        prov_html = "<h4>Provisional valuations</h4><p class='small'>None yet: no session has been valued.</p>"
    else:
        prov_html = "<h4>Provisional valuations</h4><p class='small'>None: every session was valued from that session's own closes.</p>"
    nxt = d["next"]
    # SYNC WARNING: static/tour.js "data" lights the freshness panel ("health-data": it names Needs attention and What
    # happens next) and "recon" the reconciliation panel ("health-recon") with the assumptions panel
    # ("health-assumptions") beside or below it, each card naming what those panels hold.
    return f"""<div class="health-grid">
<div class="panel" data-tour="health-data"><h4 class="first">Data freshness and instrument identity</h4>{summary}{prob_html}{full}</div>
<div class="panel" data-tour="health-recon"><h4 class="first">Reconciliation (latest check)</h4>{recon_when_html}<ul class="plain">{recon}</ul>
{prov_html}
<h4>Calendar</h4><p class="small">{'Official NYSE schedule (nyse.com, 2025–2027).' if nxt.verified else 'Rule-derived NYSE calendar: not verified against an official publication for this year.'}</p></div>
<div class="panel" data-tour="health-assumptions"><h4 class="first">Execution and data assumptions</h4><ul class="small">
<li>Market-on-open fills at the first regular session that opens strictly after a decision (+{d['cfg'].decision_open_buffer_seconds}s buffer), from that session's official daily bar.</li>
<li>Slippage {num(d['cfg'].slippage_bps)} bps per side; commission {usd(d['cfg'].commission_per_order_cents)} + {num(d['cfg'].commission_bps)} bps. Simulation assumptions, not verified broker economics.</li>
<li>Exit rules are checked on completed closes; exits fill at the next open (no intraday stop fills are claimed).</li>
<li>Prices are as-traded end-of-day bars. Splits adjust share counts; cash dividends are credited on the ex-date (same for the benchmark).</li>
<li>Deposits and withdrawals are disabled (returns are not cash-flow-weighted).</li>
<li>Keeping cash earns 0% (no interest is simulated, for the account or for the "keeping cash" comparison).</li>
<li>Database engine: {engine_html()}</li></ul></div></div>"""


CREATED_BY_WORDS = {"dashboard": "dashboard", "cli": "command line", "init": "first setup", "fixture_demo": "fixture demo",
                    "test": "test", "qa": "QA scenario"}
HYP_STATUS = {"proposed": "info", "adopted": "good", "rejected": "neutral", "retired": "neutral"}


def _by_words(who: str | None) -> str:
    # review.adopt writes "hypothesis <id>" as the author of the version it creates.
    w = who or ""
    return f"adopted hypothesis #{w.split()[1]}" if w.startswith("hypothesis ") else CREATED_BY_WORDS.get(w, w)


def _setting_value(key: str, v) -> str:
    """A stored setting as the owner reads it: booleans as yes/no, fractions and basis points with their percentage."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    kind = _ECHO.get(key)
    if kind in ("pct", "bps"):
        return f"{v} ({_echo_text(kind, v)[2:]})"
    return str(v)


def _change_words(change: dict) -> str:
    # SYNC WARNING: proposed_change_json shapes written by review.submit/validate ({"type": none|config|strategy}).
    t = change.get("type", "none")
    if t == "config":
        return "risk settings: " + ("; ".join(f"{SETTING_LABELS.get(k, (k,))[0]} → {_setting_value(k, v)}"
                                             for k, v in (change.get("config_overrides") or {}).items()) or "none listed")
    if t == "strategy":
        return "strategy text: " + (change.get("strategy_text") or "not given")
    return "none (observation only)"


def _rule_words(conn, key: str, value) -> tuple[str, str]:
    """A strategy rule as the owner reads it (the stored keys and codes are for the engine).
    SYNC WARNING: keys written by experiments.STRATEGY_V1 and review.adopt."""
    yes_no = lambda v: "yes" if v is True else ("no" if v is False else str(v))
    if key == "order_type":
        return "Orders", {"MOO_NEXT_ELIGIBLE_SESSION": "market-on-open at the next eligible session"}.get(value, str(value))
    if key == "universe":
        n = len(universe.symbols(conn))
        if db.db_mode(conn) == "fixture":
            # The fixture's runs use its own synthetic symbols, not the dated forward list the stored rule names.
            return "Research universe", f"the fixture's synthetic universe ({n} DEMO symbols, verified as part of the scenario)"
        mt = re.search(r"v(\d{4}-\d{2}-\d{2})", str(value))
        return "Research universe", (f"the verified research universe ({n} symbols)"
                                     + (f", list dated {day(mt.group(1))}" if mt else ""))
    if key == "exit_rules_required_on_buy":
        return "Exit rule required on every buy", yes_no(value)
    if key == "may_hold_cash":
        return "Holding cash allowed", yes_no(value)
    if key == "activity_target":
        return "Activity target", "none (no trade is ever required)" if value is None else str(value)
    return key.replace("_", " ").capitalize(), yes_no(value) if isinstance(value, bool) else ("none" if value is None else str(value))


def history(conn, d) -> tuple[str, str]:
    """Risk-settings versions (what changed and why), strategy versions and the dated hypotheses from reviews.
    Every save requires a reason; before this the reason was stored and never shown."""
    exp_id = d["exp"]["id"]
    cvs = conn.execute("SELECT * FROM config_versions WHERE experiment_id=? ORDER BY version DESC", (exp_id,)).fetchall()
    params = {c["version"]: json.loads(c["params_json"]) for c in cvs}
    in_force = d["cfg_row"]["version"]

    def changes(ver: int) -> str:
        prev, cur = params.get(ver - 1), params[ver]
        if prev is None:  # the first version: say what the starting limits were (its reason already says "initial")
            return "starting limits: " + "; ".join(f"{SETTING_LABELS[k][0]} {_setting_value(k, cur.get(k))}" for k in SETTING_LABELS)
        diff = [f"{SETTING_LABELS.get(k, (k,))[0]}: {_setting_value(k, prev.get(k))} → {_setting_value(k, cur.get(k))}"
                for k in cur if cur.get(k) != prev.get(k)]
        return "; ".join(diff) or "no change"
    crow = "".join(f'<tr><td class="num">v{c["version"]}{" " + chip("in force", "good") if c["version"] == in_force else ""}</td>'
                   f'<td>{h(pt(c["effective_from"]))}</td><td>{h(_by_words(c["created_by"]))}</td><td class="wrap">{h(c["reason"])}</td>'
                   f'<td class="wrap">{h(changes(c["version"]))}</td></tr>' for c in cvs)
    svs = conn.execute("SELECT * FROM strategy_versions WHERE experiment_id=? ORDER BY version DESC", (exp_id,)).fetchall()
    srow = "".join(f'<tr data-detail="strat-{s["id"]}" tabindex="0"><td class="num">v{s["version"]}</td><td>{h(pt(s["effective_from"]))}</td>'
                   f'<td>{h(s["title"])}</td><td>{h(_by_words(s["created_by"]))}</td><td class="wrap">{h(s["reason"])}</td></tr>' for s in svs)
    tpl = []
    for sv in svs:
        rules_ = json.loads(sv["rules_json"] or "{}")
        rule_rows = "<br>".join(f"{h(a)}: {h(b)}" for a, b in (_rule_words(conn, k, v) for k, v in rules_.items())) or "none recorded"
        tpl.append(_detail(f"strat-{sv['id']}", f"Strategy v{sv['version']}: {sv['title']}", _kv([
            ("Effective", f"{h(pt(sv['effective_from']))} (prospective: earlier decisions keep the strategy they were made under)"),
            ("Set by", h(_by_words(sv["created_by"]))), ("Reason", dash(sv["reason"])),
            ("Description", dash(sv["description"])), ("Rules", rule_rows)])))
    hyps = conn.execute("SELECT * FROM strategy_hypotheses WHERE experiment_id=? ORDER BY id DESC", (exp_id,)).fetchall()
    if hyps:
        hrows = []
        for x in hyps:
            did = f"hyp-{x['id']}"
            hrows.append(f'<tr data-detail="{did}" tabindex="0"><td>{h(pt(x["created_at"]))}</td><th scope="row">{h(x["title"])}</th>'
                         f'<td>{chip(x["status"], HYP_STATUS.get(x["status"], "neutral"))}</td><td>{day(x["earliest_effective_session"])}</td>'
                         f'<td>{dash(x["minimum_sample"])}</td></tr>')
            adopted = ", ".join(t for t in (
                f"risk settings v{conn.execute('SELECT version FROM config_versions WHERE id=?', (x['adopted_config_version_id'],)).fetchone()[0]}"
                if x["adopted_config_version_id"] else "",
                f"strategy v{conn.execute('SELECT version FROM strategy_versions WHERE id=?', (x['adopted_strategy_version_id'],)).fetchone()[0]}"
                if x["adopted_strategy_version_id"] else "") if t)
            evid = json.loads(x["evidence_json"] or "[]")
            tpl.append(_detail(did, f"Hypothesis: {x['title']}", _kv([
                ("Status", f"{chip(x['status'], HYP_STATUS.get(x['status'], 'neutral'))} since {h(pt(x['status_changed_at']))}"
                           + (f" · adopted as {h(adopted)}" if adopted else "")),
                ("Recorded", f"{h(pt(x['created_at']))}" + (f" by review run #{x['review_run_id']}" if x["review_run_id"] else "")
                 + (f' · <a href="/report?run={x["review_run_id"]}&amp;db={h(d["db_kind"])}">open the review report</a>'
                    if x["review_run_id"] and conn.execute("SELECT report_path FROM runs WHERE id=?", (x["review_run_id"],)).fetchone()[0] else "")),
                ("Hypothesis", dash(x["hypothesis"])), ("Rationale", dash(x["rationale"])),
                ("Proposed change", h(_change_words(json.loads(x["proposed_change_json"] or "{}")))),
                ("Falsification test", dash(x["falsification_test"])), ("Minimum sample", dash(x["minimum_sample"])),
                ("Earliest effect", f"the {et_open(x['earliest_effective_session'])} or later: prospective only, never applied to past "
                                    "decisions or results"),
                ("Evidence cited", "<br>".join(h(str(e)) for e in evid) or "none")])))
        hyp_html = ('<div class="table-wrap"><table class="grid-table compact"><thead><tr><th>Recorded</th><th>Title</th><th>Status</th>'
                    f'<th>Earliest effect</th><th>Minimum sample</th></tr></thead><tbody>{"".join(hrows)}</tbody></table></div>'
                    '<p class="small muted">Click or tap a hypothesis for its falsification test and proposed change.</p>')
    else:
        hyp_html = '<p class="small muted">No hypotheses yet: /paper-review records dated, prospective hypotheses here.</p>'
    html_out = (f'<h4 id="history">History: risk settings, strategy and hypotheses</h4>'
                '<p class="small muted">Changes apply from the moment they are made and never rewrite earlier results; each decision '
                'records the versions it was checked under.</p>'
                f'<h5>Risk settings versions ({len(cvs)})</h5><div class="table-wrap"><table class="grid-table compact"><thead><tr>'
                f'<th class="num">Version</th><th>Effective</th><th>Set by</th><th>Reason</th><th>What changed</th></tr></thead><tbody>{crow}</tbody></table></div>'
                f'<h5>Strategy versions ({len(svs)})</h5><div class="table-wrap"><table class="grid-table compact"><thead><tr>'
                f'<th class="num">Version</th><th>Effective</th><th>Title</th><th>Set by</th><th>Reason</th></tr></thead><tbody>{srow}</tbody></table></div>'
                '<p class="small muted">Click or tap a strategy version for its full description and rules.</p>'
                f'<h5>Hypotheses from reviews ({len(hyps)})</h5>{hyp_html}')
    return html_out, "".join(tpl)


def current_equity_cents(conn, exp_id: int, now) -> int:
    """Equity at the latest stored closes (a position without any close at cost), as load() and the validator value it."""
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    today_ny = now.astimezone(cal.NY).date().isoformat()
    marks = {}
    for s in st.positions:
        b = prices.latest_bar(conn, s, on_or_before=today_ny)
        if b is not None:
            marks[s] = Decimal(b["close"])
    return accounting.valuation(st, marks).equity_cents


def freeze_words(need_cents: int, equity_cents: int) -> str:
    """Said only when equity < config.min_tradable_capital_cents: even an all-cash account this size could not buy,
    so this one cannot either (the converse is not claimed: cash tied up in positions can block a buy too).
    SYNC WARNING: app.js freezeText() words the same warning while the limits are typed."""
    return (f"With these limits no new buy can pass: an account needs at least {usd(need_cents)} to place one, and this one has "
            f"{usd(equity_cents)} of equity. Saving is still allowed (it pauses new buys); sells and exit rules keep working.")


# The limits that decide whether any buy can pass at an account's size (config.min_tradable_capital_cents).
# SYNC WARNING: app.js FREEZE_KEYS lists the same fields.
FREEZE_KEYS = ("min_order_usd", "commission_per_order_usd", "commission_bps", "max_position_pct", "min_cash_reserve_pct")


def fix_label(key: str, value: str) -> str:
    """SYNC WARNING: app.js fixLabel() words the one-press fix buttons the same way."""
    if key == "min_order_usd":
        return f"Use a ${value} minimum order"
    pct_ = f"{Decimal(value) * 100:.0f}%"
    return f"Use max position {value} ({pct_})" if key == "max_position_pct" else f"Use cash reserve {value} ({pct_})"


def freeze_html(need_cents: int, equity_cents: int, fixes: dict, disabled: bool) -> str:
    """The freeze warning with its one-press fixes (Rule 19: the screen computes the fix and offers it in place)."""
    buttons = "".join(f'<button type="button" class="fix-btn" data-set="cfg-{k}" data-value="{h(v)}"{" disabled" if disabled else ""}>'
                      f'{h(fix_label(k, v))}</button>' for k, v in fixes.items())
    return h(freeze_words(need_cents, equity_cents)) + (f' <span class="fix-lead">One-field fixes:</span> {buttons}' if buttons else "")


def _echo_text(kind: str, value) -> str:
    try:
        v = Decimal(str(value).strip())
    except Exception:
        return ""
    if not v.is_finite():
        return ""
    if kind == "pct":
        return f"= {format((v * 100).normalize(), 'f')}%"
    if kind == "bps":
        return f"= {format((v / 100).normalize(), 'f')}%"
    if kind == "usd":
        return f"= ${v:,.2f}" if v >= 0 else ""
    return ""


def settings(conn, d, csrf: str, db_kind: str, errors: dict | None = None, values: dict | None = None,
             dirs: tuple[str, str] = ("paper-advisor/exports/", "paper-advisor/backups/"),
             panel_notice: tuple[str, str] | None = None) -> str:
    """panel_notice = (panel id, notice html): the notice renders inside the panel whose control was used."""
    def pn(pid: str) -> str:
        return panel_notice[1] if panel_notice and panel_notice[0] == pid else ""
    cfg_row = d["cfg_row"]
    params = json.loads(cfg_row["params_json"])
    values = values or {}
    errors = errors or {}
    # Each lock says what unlocks it and offers that control right there (Rule 19), as HTML with escaped pieces.
    act = next((e for e in d["experiments"] if e["id"] == d["active_id"]), None)
    disabled_reason = None
    if db_kind == "fixture":
        disabled_reason = ("The fixture demo is a frozen synthetic scenario; its settings are not editable here. To change real limits, "
                           '<a href="/?db=forward#settings">open the forward paper test\'s settings</a>. To rebuild the demo, run '
                           f"{cmd_pill('paper-advisor/bin/pa demo --rebuild')}")
    elif d["lock"]:
        lk = d["lock"]
        disabled_reason = (f"Morning run #{lk['run_id']} is in progress (its lock expires {h(pt(lk['expires_at']))}). Settings can change "
                           "after it finishes; they apply prospectively. If the Claude Code session running it has ended, run "
                           f"{cmd_pill('/paper-morning force-recover')} in a new session.")
    elif d["exp"]["id"] != d["active_id"]:
        # The button submits the activate form in the Experiments table (form attribute: forms cannot nest).
        disabled_reason = (f"You are viewing archived #{d['exp']['id']}; only the active experiment's settings can change. "
                           f'<button type="submit" form="activate-{d["exp"]["id"]}" class="secondary">Make #{d["exp"]["id"]} active</button> '
                           f"to change them (it archives #{act['id'] if act else '?'}; nothing is deleted).")
    dis = " disabled" if disabled_reason else ""
    # Limits can contradict each other at THIS account's size (e.g. a minimum order above the max position): the
    # warning for the saved limits is rendered here, and app.js re-checks while new ones are typed.
    equity = d["val"].equity_cents
    need_now = config.min_tradable_capital_cents(params)
    frozen = equity < need_now
    fields = []
    for key, (label, help_) in SETTING_LABELS.items():
        v = values.get(key, params.get(key))
        err = errors.get(key)
        # The live echo (what the typed number means, or why it is out of range) sits on the line right under the
        # box, then any refusal from the server, then the static help: a problem is read next to what caused it.
        has_echo = key != "require_exit_rule_on_buy" and (_ECHO.get(key, "none") != "none" or key in RANGES)
        aria = (f' aria-describedby="{"echo-" + key + " " if has_echo else ""}help-{key}"' + (' aria-invalid="true"' if err else ""))
        if key == "require_exit_rule_on_buy":
            # A yes/no choice is a select, not a free-text "true".
            on = str(v).lower() in ("true", "1", "yes", "on")
            control = (f'<select id="cfg-{key}" name="{key}" data-initial="{"true" if on else "false"}"{dis}{aria}><option value="true"{" selected" if on else ""}>Yes, required</option>'
                       f'<option value="false"{"" if on else " selected"}>No</option></select>')
            echo = ""
        else:
            control = f'<input id="cfg-{key}" name="{key}" value="{h(v)}" data-initial="{h(params.get(key))}"{dis}{aria}>'
            kind = _ECHO.get(key, "none")
            rng = RANGES.get(key)
            rattr = ""
            if rng:
                lo, lo_incl, hi, hi_incl = rng
                rattr = (f' data-lo="{lo}" data-lo-incl="{int(lo_incl)}"' + (f' data-hi="{hi}" data-hi-incl="{int(hi_incl)}"' if hi is not None else "")
                         + (' data-int="1"' if key in ("max_open_positions", "max_new_orders_per_session") else ""))
            echo = (f'<div class="echo-line"><span class="echo" id="echo-{key}" aria-live="polite" data-echo="{kind}" '
                    f'data-for="cfg-{key}"{rattr}>{h(_echo_text(kind, v))}</span></div>' if has_echo else "")
        # The freeze warning is shown under the field that caused it: the one last edited (app.js), or Minimum order
        # when the saved limits are already frozen at this account's size.
        fz = ""
        if key in FREEZE_KEYS:
            show = frozen and key == "min_order_usd"
            body_ = freeze_html(need_now, equity, config.tradability_fixes(params, equity), bool(dis)) if show else ""
            fz = f'<div class="freeze-hint" data-freeze-for="cfg-{key}" role="status"{"" if show else " hidden"}>{body_}</div>'
        fields.append(f'<div class="field{" has-error" if err else ""}"><label for="cfg-{key}">{h(label)}</label>{control}{echo}{fz}'
                      + (f'<div class="error" role="alert">{h(err)}</div>' if err else "")
                      + f'<div class="help" id="help-{key}">{h(help_)}</div></div>')
    form_err = f'<div class="error-banner" role="alert">{h(errors["_form"])}</div>' if errors.get("_form") else ""
    # Outside the f-string: a backslash inside an f-string expression is a SyntaxError before Python 3.12.
    lock_attr = ' aria-describedby="exps-lock" disabled' if d["lock"] else ""

    def result_cells(e) -> str:
        # Each experiment's result beside its row, so experiments can be compared before switching (decision 5).
        mt_e = metrics.compute(conn, e["id"])
        if not mt_e["sessions_valued"]:
            return '<td class="num">' + usd(e["starting_capital_cents"]) + '</td><td class="num muted">n/a</td><td class="num muted">n/a</td>'
        return (f'<td class="num">{usd(mt_e["equity_cents"])}</td><td class="num">{pct(mt_e["total_return"])}</td>'
                f'<td class="num">{pts(mt_e["excess_vs_spy"])}</td>')
    exps = "".join(
        f'<tr><td class="num">#{e["id"]}</td><td>{h(e["name"])}</td><td>{"Fixture demo" if e["mode"] == "fixture" else "Forward paper test"}</td>'
        f'<td class="num">{usd(e["starting_capital_cents"])}</td>'
        f'<td>{h(pt(e["inception_at"]))}</td>{result_cells(e)}<td>{chip("active", "good") if e["id"] == d["active_id"] else chip(e["status"], "neutral")}</td>'
        f'<td>' + ("" if e["id"] == d["active_id"] or db_kind == "fixture" else
                   f'<form id="activate-{e["id"]}" method="post" action="/actions/activate?db={h(db_kind)}"><input type="hidden" name="csrf" value="{h(csrf)}"><input type="hidden" name="id" value="{e["id"]}">'
                   f'<button type="submit" class="secondary"{lock_attr}>Make active</button></form>')
        + "</td></tr>" for e in d["experiments"])
    # SYNC WARNING: experiments.refuse_if_run_in_progress refuses the switch on the server as well.
    exps_note = (f'<p class="disabled-reason" id="exps-lock">Run #{d["lock"]["run_id"]} is in progress on the active experiment; which '
                 "experiment is active can change after it finishes. If its Claude Code session has ended, run "
                 f"{cmd_pill('/paper-morning force-recover')} in a new session.</p>"
                 if d["lock"] and db_kind != "fixture" and len(d["experiments"]) > 1 else "")
    backups = conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 5").fetchall()
    n_backups = conn.execute("SELECT COUNT(*) FROM backups").fetchone()[0]
    def backup_li(b) -> str:
        # The file name stays in the tooltip, for finding the file in the backups folder.
        return (f"<li title=\"{h(Path(b['path']).name)}\">{h(pt(b['created_at']))} · {h(backup_reason_words(b['reason']))} · "
                f"{b['bytes'] / 1_048_576:.1f} MB · {'integrity ok' if b['integrity_ok'] else 'INTEGRITY FAILED'}</li>")
    backups_html = f'<ul class="small">{"".join(backup_li(b) for b in backups) or "<li>None yet.</li>"}</ul>'
    if n_backups > len(backups):
        older = conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT -1 OFFSET ?", (len(backups),)).fetchall()
        backups_html += (f"<details class='small'><summary>{plural(len(older), 'older backup')}</summary><ul class='small'>"
                         + "".join(backup_li(b) for b in older) + "</ul></details>")
    # SYNC WARNING: server POST actions act on the ACTIVE experiment (it is archived and its limits are copied), so
    # from an archived experiment's page this form would copy the active one's limits under a floor computed from
    # the viewed one's. It is locked there, like the risk form.
    viewing_archived = d["exp"]["id"] != d["active_id"]
    newexp_dis = " disabled" if db_kind == "fixture" or d["lock"] or viewing_archived else ""
    nx_reason = ("The fixture demo database cannot create experiments. New experiments belong to the forward paper test: "
                 '<a href="/?db=forward#newexp">start one there</a>.' if db_kind == "fixture" else
                 (f"Run #{d['lock']['run_id']} is in progress; you can create an experiment after it finishes (if its Claude Code "
                  f"session has ended, run {cmd_pill('/paper-morning force-recover')} in a new session)." if d["lock"] else
                  (f"You are viewing archived #{d['exp']['id']}. Switch to the active experiment, #{act['id']}, to start a new one: "
                   f'<a href="/?db={h(db_kind)}&amp;exp={act["id"]}#newexp">open #{act["id"]}</a> (a new experiment starts with the '
                   "active experiment's limits)." if viewing_archived and act else "")))
    cap_echo = _echo_text("usd", values.get("capital", "")) if values.get("capital") else ""
    # The new experiment starts with these same limits (server._act_new_experiment copies them), so its floor
    # comes from them; app.js checks the typed amount against data-lo and the server refuses below it.
    floor = experiments.minimum_capital_cents(params)
    floor_reason = experiments.capital_floor_reason(params)
    # SYNC WARNING: mirrors broker_sim.settle (only the active experiment settles; catch-up resumes from
    # settled_through on reactivation; tested in test_experiments_config).
    archive_note = f'<p class="small archive-note">{d["archive_consequence"]}</p>' if d["archive_consequence"] and db_kind != "fixture" else ""
    # SYNC WARNING: static/tour.js "risk" and "new-experiment" steps light these two forms (never press or type in them)
    # and quote what saving and creating do: prospective versions, a required reason, archive-never-delete; its
    # "experiments" step lights the Experiments block (.exp-block, a plain wrapper with no style of its own) and quotes
    # what Make active does.
    return f"""
<div class="settings-grid">
<form class="panel form" id="risk" method="post" action="/actions/config?db={h(db_kind)}" data-equity-cents="{equity}" data-tour="risk" novalidate>
  <h4 class="first">Risk settings <span class="muted small">version {cfg_row['version']}, effective {h(pt(cfg_row['effective_from']))}</span></h4>
  {pn("risk")}
  {'' if disabled_reason else f'<p class="small muted">Saving creates version {cfg_row["version"] + 1}, effective immediately and only for FUTURE decisions. Earlier decisions keep the limits they were checked against.</p>'}
  {f'<p class="disabled-reason">{disabled_reason}</p>' if disabled_reason else ''}
  {form_err}
  <input type="hidden" name="csrf" value="{h(csrf)}">
  <div class="fields">{''.join(fields)}</div>
  <div class="field reason{' has-error' if errors.get('reason') else ''}"><label for="cfg-reason">Reason for the change (required)</label>
    <textarea id="cfg-reason" name="reason" rows="2"{dis}{' aria-invalid="true"' if errors.get('reason') else ''}>{h(values.get('reason', ''))}</textarea>
    {f'<div class="error" role="alert">{h(errors["reason"])}</div>' if errors.get('reason') else ''}</div>
  <p class="small cfg-diff" id="cfg-diff" aria-live="polite" hidden></p>
  <button type="submit" class="primary" id="cfg-save" aria-describedby="cfg-diff cfg-hint"{dis}>Save as new version</button>
  <span class="small muted nx-hint" id="cfg-hint" hidden></span>
</form>
<div class="side-col">
<form class="panel form" id="newexp" method="post" action="/actions/new-experiment?db={h(db_kind)}" data-tour="new-experiment" novalidate>
  <h4 class="first">Start a new experiment</h4>
  {pn("newexp")}
  <p class="small muted">Different capital means a new experiment with its own ledger and benchmark, funded once with that amount; it starts
  with the current risk settings. The current experiment is archived (never reset or deleted) and can be made active again later.</p>
  {'' if newexp_dis else archive_note}
  {f'<p class="disabled-reason">{nx_reason}</p>' if nx_reason else ''}
  {f'<div class="error-banner" role="alert">{h(errors["_newexp"])}</div>' if errors.get("_newexp") else ''}
  <input type="hidden" name="csrf" value="{h(csrf)}">
  <div class="field{' has-error' if errors.get('capital') else ''}"><label for="nx-capital">Starting capital (USD)</label>
    <input id="nx-capital" name="capital" inputmode="decimal" placeholder="e.g. 1000.00" value="{h(values.get('capital', ''))}" aria-describedby="echo-capital help-capital"{newexp_dis}{' aria-invalid="true"' if errors.get('capital') else ''}>
    <div class="echo-line"><span class="echo" id="echo-capital" aria-live="polite" data-echo="usd" data-for="nx-capital" data-lo="{m.cents_to_str(floor)}" data-lo-incl="1">{h(cap_echo)}</span></div>
    {f'<div class="error" role="alert">{h(errors["capital"])}</div>' if errors.get('capital') else ''}
    <div class="help" id="help-capital">At least {usd(floor)}: {h(floor_reason)}. Funded once and never topped up.</div></div>
  <div class="field"><label for="nx-name">Name (optional)</label><input id="nx-name" name="name" value="{h(values.get('name', ''))}" aria-describedby="help-name"{newexp_dis}>
    <div class="help" id="help-name">Left empty, it is named after its capital, like “Forward paper experiment ($500)”.</div></div>
  <div class="field check{' has-error' if errors.get('confirm') else ''}"><label><input type="checkbox" name="confirm" value="yes"{newexp_dis}{' checked' if values.get('confirm') else ''}{' aria-invalid="true"' if errors.get('confirm') else ''}>
    Archive the current experiment and make the new one active</label>
    {f'<div class="error" role="alert">{h(errors["confirm"])}</div>' if errors.get('confirm') else ''}</div>
  <button type="submit" class="primary" id="nx-create" aria-describedby="nx-hint"{newexp_dis}>Create experiment</button>
  <span class="small muted nx-hint" id="nx-hint" hidden>Tick the box above to create it.</span>
</form>
<div class="panel" id="tools"><h4 class="first">Export and backup</h4>
  {pn("tools")}
  <form method="post" action="/actions/export?db={h(db_kind)}" class="inline"><input type="hidden" name="csrf" value="{h(csrf)}"><button type="submit" class="secondary">Export CSV + JSON (zip)</button></form>
  <form method="post" action="/actions/backup?db={h(db_kind)}" class="inline"><input type="hidden" name="csrf" value="{h(csrf)}"><button type="submit" class="secondary">Back up database now</button></form>
  <p class="small muted">Files stay on this computer, in <code class="path">{h(dirs[0])}</code> and <code class="path">{h(dirs[1])}</code>. Recent backups{f" ({len(backups)} of {n_backups})" if n_backups else ""}:</p>{backups_html}
</div></div></div>
<div class="exp-block" data-tour="experiments"><h4 id="experiments">Experiments</h4>{pn("experiments")}{exps_note}{(f'<p class="small archive-note">Make active switches the slash commands to the experiment you pick. {d["archive_consequence"]}</p>' if len(d["experiments"]) > 1 and db_kind != "fixture" else '')}<div class="table-wrap"><table class="grid-table compact"><thead><tr><th class="num">#</th><th>Name</th><th>Kind</th><th class="num">Capital</th><th>Inception</th><th class="num">Equity</th><th class="num">Return</th><th class="num">Gap vs benchmark</th><th>Status</th><th></th></tr></thead><tbody>{exps}</tbody></table></div></div>"""


def page(conn, exp_id: int, now, db_kind: str, csrf: str, notice: tuple | None = None, form_errors: dict | None = None,
         form_values: dict | None = None, form_target: str | None = None, notice_target: str | None = None,
         dirs: tuple[str, str] | None = None, notice_panel: str | None = None) -> str:
    """notice_target names the section the notice belongs to and notice_panel the panel inside it (where the
    action was taken), so "Done." or "Nothing was saved" is read next to the control it is about."""
    token = _REF_YEAR.set(now.astimezone(LA).year)
    try:
        return _page(conn, exp_id, now, db_kind, csrf, notice, form_errors, form_values, form_target, notice_target, dirs, notice_panel)
    finally:
        _REF_YEAR.reset(token)


_DEC_LINK_RE = re.compile(r'data-(?:open|detail)="dec-(\d+)"')
_DEC_TPL_RE = re.compile(r'<template id="dec-(\d+)">')


def _missing_decision_templates(conn, d, html_text: str) -> str:
    """Drawers for decisions that are linked (a position's entry, a pending exit's rule, a HOLD's replaced rule)
    but not rendered by the journal, which shows only the latest JOURNAL_RUNS runs: without this, a position held
    longer than that had a dead "open the decision" link. Repeated until closed, since a drawer links onward."""
    have = set(_DEC_TPL_RE.findall(html_text))
    extra = []
    todo = set(_DEC_LINK_RE.findall(html_text)) - have
    while todo:
        new_html = ""
        for i in sorted(todo, key=int):
            dec = conn.execute("SELECT * FROM decisions WHERE id=? AND experiment_id=?", (int(i), d["exp"]["id"])).fetchone()
            have.add(i)
            if dec is not None:
                t = _detail(f"dec-{i}", f"Decision: {action_words(dec['action'])} {dec['symbol'] or ''}",
                            _decision_detail(conn, dec, d["mt"]["trades"]))
                extra.append(t)
                new_html += t
        todo = set(_DEC_LINK_RE.findall(new_html)) - have
    return "".join(extra)


def _page(conn, exp_id, now, db_kind, csrf, notice, form_errors, form_values, form_target, notice_target, dirs, notice_panel) -> str:
    d = load(conn, exp_id, now, db_kind)
    pos_html, pos_tpl = positions_table(conn, d)
    fills_html, fills_tpl = fills_table(conn, d)
    jr_html, jr_tpl = journal(conn, d)
    pend_html, pend_tpl = pending_table(conn, d)
    runs_html, runs_tpl = runs_table(conn, d)
    hist_html, hist_tpl = history(conn, d)
    nt = ""
    if notice:
        kind, text = notice
        nt = f'<div id="notice" class="notice notice-{h(kind)}" role="status">{text}</div>'
    def block(sid, title, content, solo=False):
        # solo: the page's only section, whose heading would repeat the page title (kept for anchors and screen readers).
        return (f'<section class="block{" solo" if solo else ""}{" focus-target" if sid == form_target else ""}" id="{sid}"><h2>{h(title)}</h2>'
                f'{nt if (sid == notice_target and not notice_panel) else ""}{content}</section>')
    # Each sidebar page and the sections it holds (ids unchanged, so every link, refusal and test anchor still lands).
    # SYNC WARNING: PAGE_NAV order; app.js opens the page holding a linked id; tests/test_dashboard.py PAGES.
    page_body = {
        "home": home(conn, d),
        "portfolio": block("positions", "Positions", pos_html) + block("pending", "Pending orders", pend_html),
        "decisions": ((block("all-decisions", "All decisions", idx) if (idx := decision_index(conn, d)) else "")
                      + block("journal", "Advisor journal", jr_html) + block("rejected", "Rejected proposals", rejected_table(conn, d))),
        "activity": block("trades", "Fills, trades, splits and dividends", fills_html, solo=True),
        "runs": block("runs", "Run history", runs_html, solo=True),
        "health": block("health", "Data and health", health(conn, d), solo=True),
        "settings": block("settings", "Settings and tools",
                          settings(conn, d, csrf, db_kind, form_errors if form_target == "settings" else None,
                                   form_values if form_target == "settings" else None,
                                   panel_notice=(notice_panel, nt) if (nt and notice_panel and notice_target == "settings") else None,
                                   **({"dirs": dirs} if dirs else {})) + hist_html, solo=True),
    }
    # One line under each page title says what the page is for (the owner found the old single page hard to read).
    heads = {
        "portfolio": ("Portfolio", "What the advisor holds now, and the orders that fill at the next open."),
        "decisions": ("Decisions", "Why the advisor decided what it did, and what became of each decision. Open one for its "
                                   "thesis, the validator's checks and its history."),
        "activity": ("Activity", "Every fill, round trip, split and dividend, as recorded in the ledger."),
        "runs": ("Runs", "Every morning run and review: what it did, what it decided, and anything that went wrong."),
        "health": ("Data & health", "Whether the record can be trusted: price data, reconciliation, provisional valuations, "
                                    "the calendar and the simulation's assumptions."),
        "settings": ("Settings", "Prospective risk limits and experiments. A change applies from the moment it is saved; "
                                 "earlier decisions keep the versions they were checked against."),
    }
    for name, (title_, sub_) in heads.items():
        page_body[name] = (f'<div class="page-head"><div><h1 class="page-title">{h(title_)}</h1><p class="page-sub">{h(sub_)}</p>'
                           f'</div></div>' + page_body[name])
    # Pages without a section of their own take the page's name as id (#home, #portfolio...), so the sidebar links are
    # plain anchors that also work without JavaScript (every page then shows, stacked).
    # SYNC WARNING: static/tour.js lights the pages named in TOUR_PAGE_ANCHORS whole (data-tour="page-...").
    body = "".join(f'<section class="view"{f" id={chr(34)}{name}{chr(34)}" if name in ("home", "portfolio", "decisions", "activity") else ""}'
                   f' data-view="{name}"'
                   + (f' data-tour="{TOUR_PAGE_ANCHORS[name]}"' if name in TOUR_PAGE_ANCHORS else "")
                   + f'>{page_body[name]}</section>' for name, _ in PAGE_NAV)
    band, side, top = header(d, db_kind, exp_id, page_nav(d))
    templates = pos_tpl + fills_tpl + jr_tpl + pend_tpl + runs_tpl + hist_tpl + valuations_drawer(d) + money_drawers(conn, d)
    templates += _missing_decision_templates(conn, d, body + templates)
    title = f"PAPER TRADING · {d['exp']['name']}"
    mark = f"PAPER TRADING · {'FIXTURE DEMO' if d['fixture'] else 'simulated money'}"
    # SYNC WARNING: app.js reads these. data-state (the run lock and latest run as rendered) is compared with server
    # /api/state so an open tab reloads when a run starts or ends; data-self is this view's GET address, given to a
    # refused POST so no reload re-sends the form; data-kept names the form a refusal re-rendered with the owner's
    # entries, which app.js keeps across reloads (data-exp/data-active: only the active experiment's forms keep them).
    st = live_state(conn, now)
    attrs = (f' data-db="{h(db_kind)}" data-exp="{d["exp"]["id"]}" data-active="{st["active_id"] or ""}"'
             f' data-self="/?db={h(db_kind)}&amp;exp={d["exp"]["id"]}" data-state="{h(json.dumps(st))}"'
             + ("" if d["fixture"] else f' data-state-url="/api/state?db={h(db_kind)}"')
             + (f' data-kept="{h(notice_panel)}"' if form_values is not None and notice_panel else "")
             + (' data-refresh="60"' if d["lock"] and not d["fixture"] else ""))
    # SYNC WARNING: static/tour.js loads after app.js because it drives the page through window.paperAdvisorTour, which
    # app.js defines; the drawer's data-tour="sheet" is the tour's side-sheet step. simple_page() loads no tour.
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/static/app.css"></head>
<body class="db-{h(db_kind)}"{attrs}>
{band}
<div class="app">
{side}
<div class="main">
{top}
<main class="page">
{nt if notice_target is None else ""}
{body}
</main>
</div>
</div>
{tabbar(d, db_kind)}
<aside id="drawer" class="drawer" role="dialog" aria-modal="true" aria-hidden="true" aria-label="Details" data-tour="sheet"><div class="drawer-head"><span class="drawer-mark">{mark}</span><button type="button" class="drawer-back" hidden>← Back</button><span class="drawer-title" hidden></span><span class="spacer"></span><button type="button" class="drawer-close" aria-label="Close details">×</button></div><div class="drawer-body"></div></aside>
<div id="drawer-scrim" class="scrim" hidden></div>
{templates}
<script src="/static/app.js"></script><script src="/static/tour.js"></script>
</body></html>"""


def report_html(md: str) -> str:
    """The run reports' markdown subset (#/##/### headings, > quote, - lists, | tables, **bold**, _em_) as HTML.
    Every piece is escaped first. The section whose heading starts "Provenance" (raw model and code identifiers,
    for audit) is collapsed, as everywhere else on the dashboard.
    SYNC WARNING: reports.morning_report and review.review_report write this subset."""
    out, para, items, rows = [], [], [], []
    in_prov = False

    def inline(t: str) -> str:
        t = h(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return re.sub(r"(?<![\w/])_(.+?)_(?![\w/])", r"<em>\1</em>", t)

    def flush() -> None:
        if para:
            out.append("<p>" + " ".join(para) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            items.clear()
        if rows:
            head, *body = rows
            # report-table: cells wrap but size to their content (td.wrap's 240px minimum hid all but 4 columns).
            out.append('<div class="table-wrap"><table class="grid-table compact report-table"><thead><tr>' + "".join(f"<th>{c}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table></div>")
            rows.clear()

    for line in md.splitlines():
        st = line.strip()
        if st.startswith("#"):
            flush()
            level = len(st) - len(st.lstrip("#"))
            title = st[level:].strip()
            if level == 2 and title.startswith("Provenance"):
                out.append(f'<details class="small provenance"><summary>{inline(title)}</summary>')
                in_prov = True
                continue
            out.append(f"<h{min(level + 1, 4)}>{inline(title)}</h{min(level + 1, 4)}>")
        elif st.startswith("> "):
            flush()
            out.append(f'<p class="report-note">{inline(st[2:])}</p>')
        elif st.startswith("- "):
            if para or rows:
                flush()
            items.append(inline(st[2:]))
        elif st.startswith("|"):
            if para or items:
                flush()
            if not re.fullmatch(r"\|[-|: ]+\|", st):
                rows.append([inline(c.strip()) for c in st.strip("|").split("|")])
        elif not st:
            flush()
        else:
            para.append(inline(st))
    flush()
    if in_prov:
        out.append("</details>")
    return "".join(out)


def simple_page(title: str, body_html: str, db_kind: str = "forward", back: bool = True) -> str:
    """back=False where the dashboard itself is what is missing (a back link would lead to this same page)."""
    back_link = f'<a href="/?db={h(db_kind)}">← Back to dashboard</a>' if back else ""
    # The browser tab says PAPER TRADING (and FIXTURE DEMO) like the dashboard's ("Report run 5" alone did not).
    tab_title = "PAPER TRADING · " + ("FIXTURE DEMO · " if db_kind == "fixture" else "") + title
    # A fixture page says so in words, not only in the orange colour (a scrolled report was told apart by colour alone).
    fixture_chip = chip("FIXTURE DEMO", "fixture") if db_kind == "fixture" else ""
    banner = ('<div class="fixture-banner" role="note"><strong>FIXTURE DEMO — SYNTHETIC DATA.</strong> Invented for an isolated '
              'demonstration; not market data and not your forward paper test.</div>' if db_kind == "fixture" else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(tab_title)}</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/static/app.css"></head><body class="db-{h(db_kind)}">
<header class="top"><div class="top-row"><span class="paper-badge">PAPER TRADING</span>{fixture_chip}<div class="brand"><strong>paper-advisor</strong></div>
{back_link}</div></header>{banner}<main class="narrow">{body_html}</main><script src="/static/app.js"></script></body></html>"""
