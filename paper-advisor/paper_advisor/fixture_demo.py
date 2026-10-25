"""Isolated FIXTURE demo: synthetic prices, synthetic tickers, a scripted (non-AI) advisor.

Everything here is fake on purpose and kept out of the real experiment:
  - its own database (data/fixture_demo.sqlite, db_mode='fixture'; the schema refuses fixture
    experiments in the forward database and vice versa),
  - DEMO.* tickers that cannot be mistaken for real symbols,
  - an injected clock (refused by the forward database),
  - decisions written by this script, recorded with model_id 'fixture-script' (not a model).

Scenario (Thanksgiving week 2025, chosen for its holiday and early close):
  Sat 11-22  run 1: BUY A $120, B $100, C $60 accepted; BUY D $200 rejected (25% cap, 4th order)
  Sun 11-23  rerun: reconcile-only (no duplicate decisions)
  Mon 11-24  A, B, C and the benchmark fill at the open (recorded Tuesday morning)
  Tue 11-25  run 2 (09:00 ET, pre-open): HOLD A tightening its stop to 6%; BUY E $50 fills at Tue open.
             B splits 2:1 at the open. A closes at 46.50, below its stop -> exit queued for Wednesday.
  Wed 11-26  C goes ex-dividend $0.30; A's exit fills at a gapped-down 45.80 open. (No morning run.)
  Thu 11-27  Thanksgiving: no session.   Fri 11-28: 1 p.m. early close.
  Mon 12-01  run 3 at 10:00 ET (after the open): missed sessions reconcile in order; BUY F $80 targets
             TUESDAY's open, not Monday's.
  Wed 12-03  run 4 settles F; a review proposes a dated, prospective hypothesis.
"""
from __future__ import annotations

import random
import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import clock, config, corporate_actions, db, experiments, market_calendar as cal, metrics, morning, prices, reconcile, review, universe

UTC = timezone.utc
FIXTURE_NOTE = "(fixture)"
FIXTURE_UNIVERSE = [
    universe.Instrument("DEMO.SPY", "DEMO.SPY", "Synthetic broad-market ETF (fixture)", "ETF", "FIXTURE", "fixture", is_benchmark=True),
    universe.Instrument("DEMO.A", "DEMO.A", "Synthetic stock A (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.B", "DEMO.B", "Synthetic stock B (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.C", "DEMO.C", "Synthetic stock C (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.D", "DEMO.D", "Synthetic stock D (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.E", "DEMO.E", "Synthetic stock E (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.F", "DEMO.F", "Synthetic stock F (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
]

# Hand-written bars for the scenario window (o, h, l, c). Everything earlier is a seeded random walk
# that only feeds the briefing's descriptive indicators.
SCENARIO = {
    "2025-11-21": {"DEMO.SPY": (99.0, 100.2, 98.8, 100.0), "DEMO.A": (49.5, 50.3, 49.2, 50.0), "DEMO.B": (39.6, 40.3, 39.4, 40.0),
                   "DEMO.C": (29.8, 30.2, 29.6, 30.0), "DEMO.D": (19.9, 20.2, 19.8, 20.0), "DEMO.E": (9.9, 10.1, 9.8, 10.0),
                   "DEMO.F": (24.8, 25.2, 24.6, 25.0)},
    "2025-11-24": {"DEMO.SPY": (100.0, 101.6, 99.8, 101.0), "DEMO.A": (50.0, 50.4, 48.9, 49.0), "DEMO.B": (40.0, 41.2, 39.9, 41.0),
                   "DEMO.C": (30.0, 30.6, 29.9, 30.4), "DEMO.D": (20.0, 20.3, 19.9, 20.1), "DEMO.E": (10.0, 10.2, 9.9, 10.1),
                   "DEMO.F": (25.0, 25.3, 24.9, 25.1)},
    "2025-11-25": {"DEMO.SPY": (101.2, 101.9, 100.6, 101.5), "DEMO.A": (48.6, 48.8, 46.2, 46.5), "DEMO.B": (20.6, 21.0, 20.4, 20.9),
                   "DEMO.C": (30.4, 30.7, 30.2, 30.5), "DEMO.D": (20.1, 20.4, 20.0, 20.3), "DEMO.E": (10.0, 10.3, 9.95, 10.2),
                   "DEMO.F": (25.1, 25.4, 25.0, 25.2)},
    "2025-11-26": {"DEMO.SPY": (101.6, 102.4, 101.4, 102.2), "DEMO.A": (45.8, 46.4, 45.5, 46.1), "DEMO.B": (20.9, 21.3, 20.8, 21.2),
                   "DEMO.C": (30.2, 30.5, 30.0, 30.3), "DEMO.D": (20.3, 20.5, 20.2, 20.4), "DEMO.E": (10.2, 10.4, 10.1, 10.35),
                   "DEMO.F": (25.2, 25.5, 25.1, 25.4)},
    "2025-11-28": {"DEMO.SPY": (102.3, 102.8, 102.1, 102.6), "DEMO.A": (46.2, 46.6, 46.0, 46.4), "DEMO.B": (21.2, 21.5, 21.1, 21.4),
                   "DEMO.C": (30.3, 30.6, 30.2, 30.5), "DEMO.D": (20.4, 20.6, 20.3, 20.5), "DEMO.E": (10.35, 10.5, 10.3, 10.45),
                   "DEMO.F": (25.4, 25.6, 25.3, 25.5)},
    "2025-12-01": {"DEMO.SPY": (102.4, 102.7, 101.2, 101.6), "DEMO.A": (46.3, 46.5, 45.6, 45.9), "DEMO.B": (21.3, 21.4, 20.9, 21.0),
                   "DEMO.C": (30.4, 30.5, 30.0, 30.1), "DEMO.D": (20.4, 20.5, 20.1, 20.2), "DEMO.E": (10.4, 10.45, 10.1, 10.2),
                   "DEMO.F": (25.5, 25.6, 25.0, 25.2)},
    "2025-12-02": {"DEMO.SPY": (101.8, 102.9, 101.6, 102.7), "DEMO.A": (46.0, 46.8, 45.9, 46.6), "DEMO.B": (21.1, 21.7, 21.0, 21.6),
                   "DEMO.C": (30.2, 30.8, 30.1, 30.7), "DEMO.D": (20.3, 20.7, 20.2, 20.6), "DEMO.E": (10.25, 10.6, 10.2, 10.55),
                   "DEMO.F": (25.0, 25.9, 24.9, 25.8)},
}
CORPORATE_ACTIONS = [  # (symbol, type, ex_date, ratio/amount, announced_on)
    ("DEMO.B", "SPLIT", "2025-11-25", (2, 1), "2025-11-10"),
    ("DEMO.C", "CASH_DIVIDEND", "2025-11-26", Decimal("0.30"), "2025-11-10"),
]
HISTORY_START = date(2025, 8, 20)
SCENARIO_START = "2025-11-21"


def synthetic_history() -> dict:
    """Seeded random walk ending exactly at each symbol's 2025-11-21 open (continuous series)."""
    rng = random.Random(20251121)
    sessions = [s.date for s in cal.sessions_between(HISTORY_START, date(2025, 11, 20))]
    out = {}
    for inst in FIXTURE_UNIVERSE:
        end = SCENARIO["2025-11-21"][inst.symbol][0]
        rets = [rng.gauss(0.0004, 0.012) for _ in sessions]
        closes = [end]
        for r in reversed(rets):
            closes.append(closes[-1] / (1 + r))
        closes = list(reversed(closes))[:-1]
        bars = {}
        prev = closes[0]
        for d, c in zip(sessions, closes):
            o = prev * (1 + rng.gauss(0, 0.003))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
            bars[d.isoformat()] = tuple(round(x, 2) for x in (o, h, l, c))
            prev = c
        out[inst.symbol] = bars
    return out


class FixtureFeed:
    """Simulates a daily-bar data feed: a bar becomes available only after its session closed plus
    the finalization buffer, and is 'retrieved' when the feed is polled (so missed days arrive late)."""

    def __init__(self, conn):
        self.conn = conn
        self.history = synthetic_history()

    def __call__(self, conn, paths, exp_id, now, run_id, transport=None):
        _, cfg = config.current(conn, exp_id, clock.iso(now))
        stored = 0
        with db.transaction(conn):
            snap = prices.record_snapshot(conn, source="fixture_feed", url="fixture://synthetic-bars", requested_at=clock.iso(now),
                                          retrieved_at=clock.iso(now), http_status=200, parse_status="ok",
                                          params={"note": "synthetic fixture data, not market data"}, run_id=run_id)
            for sym in [i.symbol for i in FIXTURE_UNIVERSE]:
                rows = {**self.history[sym], **{d: v[sym] for d, v in SCENARIO.items()}}
                for d, (o, h, l, c) in sorted(rows.items()):
                    s = cal.session(date.fromisoformat(d))
                    if s.close_utc + timedelta(minutes=cfg.bar_finalization_minutes) > now or prices.bar_for(conn, sym, d):
                        continue
                    prices.insert_bar(conn, sym, d, o, h, l, c, 1_000_000, source="fixture_feed", snapshot_id=snap,
                                      retrieved_at=clock.iso(now))
                    stored += 1
                corporate_actions.set_coverage(conn, sym, HISTORY_START.isoformat(), cal.last_completed_session(now, cfg.bar_finalization_minutes).date.isoformat(),
                                               clock.iso(now), snap)
            for sym, kind, ex, val, announced in CORPORATE_ACTIONS:
                if now.date().isoformat() >= announced:
                    if kind == "SPLIT":
                        corporate_actions.record(conn, sym, kind, ex, ratio=val, source="fixture_feed", snapshot_id=snap, now=clock.iso(now))
                    else:
                        corporate_actions.record(conn, sym, kind, ex, amount=val, source="fixture_feed", snapshot_id=snap, now=clock.iso(now))
        return {"data_status": "OK (FIXTURE)", "failures": [], "bars_stored": stored, "note": "synthetic fixture data"}


def _buy(symbol, notional, thesis, stop="0.08", tp=None, mh=30, conf="0.55"):
    return {"action": "BUY", "symbol": symbol, "notional_usd": notional, "thesis": thesis + " " + FIXTURE_NOTE,
            "counterargument": "synthetic scenario: the move could reverse " + FIXTURE_NOTE, "horizon_sessions": 20,
            "horizon_text": "about four weeks", "confidence": conf, "uncertainty": "synthetic data; illustrative only",
            "key_risks": "gap risk", "exit_rules": {"stop_loss_pct": stop, "take_profit_pct": tp, "max_holding_sessions": mh,
                                                    "invalidation": "close below the precommitted stop"},
            "skills_used": ["position-sizer"]}


def _payload(run_id, decisions, view):
    return {"schema_version": 1, "run_id": run_id, "model": {"provider": "fixture", "model_id": "fixture-script", "effort": "n/a"},
            "market_view": view + " " + FIXTURE_NOTE, "what_changed": "scripted fixture step " + FIXTURE_NOTE,
            "evidence": [{"ref": "N1", "kind": "note", "title": "Fixture scenario note", "summary": "Synthetic data for the isolated demo."}],
            "candidates": [{"symbol": "DEMO.D", "verdict": "rejected", "assessment": "oversized on purpose to show the validator",
                            "reason": "demonstrates a rejection"}],
            "decisions": decisions}


def _at(clk, s: str):
    clk.set(datetime.fromisoformat(s).replace(tzinfo=UTC))


def run_demo(paths, rebuild: bool = False) -> dict:
    paths.ensure_dirs()
    dbp = paths.fixture_db_path
    if dbp.exists():
        if not rebuild:
            conn = db.open_existing(dbp)
            if db.db_mode(conn) != "fixture":
                raise RuntimeError("fixture path holds a non-fixture database; refusing to touch it")
            return {"fixture_db": str(dbp), "note": "fixture demo already built; pass --rebuild to rebuild it",
                    "metrics": {k: v for k, v in metrics.compute(conn, experiments.active_experiment_id(conn)).items()
                                if k not in ("series", "benchmark_series", "trades")}}
        keep = paths.backups_dir / f"fixture_demo-replaced-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite"
        shutil.move(str(dbp), keep)
        for ext in ("-wal", "-shm"):
            side = dbp.with_name(dbp.name + ext)
            if side.exists():
                side.unlink()
    clk = clock.SteppableClock(datetime(2025, 11, 21, 22, 30, tzinfo=UTC))
    clock.set_override(clk)
    try:
        conn = db.init_db(dbp, db_mode="fixture")
        universe.install(conn, FIXTURE_UNIVERSE, now=clock.now_iso(), verification_source="fixture")
        exp_id = experiments.create_experiment(conn, name="FIXTURE DEMO ($500, synthetic data)", starting_capital_cents=50000,
                                               now=clock.now_utc(), created_by="fixture_demo", config_params={"benchmark_symbol": "DEMO.SPY"},
                                               notes="Synthetic fixture demo. Not market data. Not the real experiment.")
        feed = FixtureFeed(conn)
        log = []

        def run(when, decisions=None, view="", **kw):
            _at(clk, when)
            s = morning.start(conn, paths, model_provider="fixture", model_id="fixture-script", model_effort="n/a",
                              refresher=feed, with_news=False, **kw)
            entry = {"at": when, "run_id": s["run_id"], "mode": s["mode"], "settled": s["settled"]}
            if s["mode"] == "analysis":
                sub = morning.submit(conn, paths, run_id=s["run_id"], token=s["lock_token"], payload=_payload(s["run_id"], decisions, view))
                fin = morning.finish(conn, paths, run_id=s["run_id"], token=s["lock_token"])
                entry["outcomes"] = sub["outcomes"]
                entry["report"] = fin["report_path"]
            else:
                entry["report"] = s.get("report_path")
            log.append(entry)
            return entry

        run("2025-11-22T05:00:00", [
            _buy("DEMO.A", "120.00", "Synthetic uptrend continuation."),
            _buy("DEMO.B", "100.00", "Synthetic breakout; a 2:1 split is scheduled for Tuesday."),
            _buy("DEMO.C", "60.00", "Synthetic dividend payer going ex-dividend Wednesday."),
            _buy("DEMO.D", "200.00", "Deliberately oversized to show the validator rejecting it."),
        ], view="Scripted fixture view: neutral regime, three small long positions.")
        run("2025-11-23T18:00:00")  # same target session -> reconcile-only rerun
        run("2025-11-25T14:00:00", [
            {"action": "HOLD", "symbol": "DEMO.A", "thesis": "Holding A but tightening risk " + FIXTURE_NOTE,
             "counterargument": "Monday closed weak " + FIXTURE_NOTE,
             "exit_rules": {"stop_loss_pct": "0.06", "take_profit_pct": None, "max_holding_sessions": 30, "invalidation": "close below 6% stop"}},
            _buy("DEMO.E", "50.00", "Synthetic small-cap mean reversion.", stop="0.10"),
        ], view="Scripted fixture view: tighten A, add a small E position before the open.")
        # Wednesday's run is skipped on purpose (missed morning); Thursday is Thanksgiving.
        run("2025-12-01T15:00:00", [_buy("DEMO.F", "80.00", "Synthetic setup decided AFTER the open.")],
            view="Scripted fixture view: decision made at 10:00 ET, so it targets Tuesday's open.")
        run("2025-12-03T13:00:00", [{"action": "NO_TRADE", "thesis": "Nothing compelling in the synthetic data " + FIXTURE_NOTE}],
            view="Scripted fixture view: holding cash is an acceptable outcome.")
        _at(clk, "2025-12-03T13:30:00")
        rv = review.start(conn, paths, model_id="fixture-script", model_effort="n/a", model_provider="fixture")
        rsub = review.submit(conn, paths, run_id=rv["run_id"], payload={
            "schema_version": 1, "run_id": rv["run_id"], "model": {"provider": "fixture", "model_id": "fixture-script", "effort": "n/a"},
            "observations": "Fixture review: one stop-loss exit after a gap, one split and one dividend handled; sample far too small. " + FIXTURE_NOTE,
            "hypotheses": [{"title": "Avoid entries the session before a known gap catalyst " + FIXTURE_NOTE,
                            "hypothesis": "Entries made right before scheduled catalysts produce larger adverse gaps.",
                            "rationale": "DEMO.A gapped below its stop. Single observation; illustrative only.",
                            "falsification_test": "Compare next-open gaps for entries with and without a catalyst within 2 sessions over at least 30 entries.",
                            "minimum_sample": "30 entries", "proposed_change": {"type": "none"}}]})
        with db.transaction(conn):
            rec = reconcile.run_checks(conn, exp_id)
        mt = metrics.compute(conn, exp_id)
        return {"fixture_db": str(dbp), "experiment_id": exp_id, "runs": log, "review": rsub,
                "reconciliation_passed": all(r["passed"] for r in rec),
                "metrics": {k: v for k, v in mt.items() if k not in ("series", "benchmark_series")},
                "dashboard": "paper-advisor/bin/pa dashboard --fixture"}
    finally:
        clock.clear_override()
