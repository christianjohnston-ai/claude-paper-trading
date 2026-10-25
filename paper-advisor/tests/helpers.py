"""Test world builder: a FIXTURE-mode experiment with a synthetic universe, an injected clock and
helpers to drop in daily bars. Synthetic symbols use a DEMO. prefix so nobody can mistake them for
real tickers. Everything here writes only to the temporary fixture database of the test."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from paper_advisor import clock, corporate_actions, db, experiments, market_calendar as cal, prices, runs, universe

UTC = timezone.utc

FIXTURE_UNIVERSE = [
    universe.Instrument("DEMO.SPY", "DEMO.SPY", "Synthetic broad-market ETF (fixture)", "ETF", "FIXTURE", "fixture", is_benchmark=True),
    universe.Instrument("DEMO.A", "DEMO.A", "Synthetic stock A (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.B", "DEMO.B", "Synthetic stock B (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.C", "DEMO.C", "Synthetic stock C (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.D", "DEMO.D", "Synthetic stock D (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.E", "DEMO.E", "Synthetic stock E (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
    universe.Instrument("DEMO.F", "DEMO.F", "Synthetic stock F (fixture)", "COMMON_STOCK", "FIXTURE", "fixture"),
]


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


class World:
    def __init__(self, conn, start: str = "2026-10-24T05:00:00", capital_cents: int = 50000, config_params=None):
        self.conn = conn
        self.clock = clock.SteppableClock(utc(start))
        clock.set_override(self.clock)
        universe.install(conn, FIXTURE_UNIVERSE, now=clock.now_iso(), verification_source="fixture")
        params = {"benchmark_symbol": "DEMO.SPY"}
        params.update(config_params or {})
        self.exp_id = experiments.create_experiment(conn, name="test experiment", starting_capital_cents=capital_cents,
                                                    now=clock.now_utc(), created_by="test", config_params=params)
        self._run_seq = 0

    def at(self, s: str) -> None:
        self.clock.set(utc(s))

    def now_iso(self) -> str:
        return clock.now_iso()

    def bar(self, symbol: str, date: str, o, h=None, l=None, c=None, v: int = 1_000_000, retrieved_at: str | None = None,
            flags=()):
        s = cal.session(datetime.fromisoformat(date).date())
        assert s is not None, f"{date} is not a session"
        c = o if c is None else c
        h = max(Decimal(str(o)), Decimal(str(c))) if h is None else h
        l = min(Decimal(str(o)), Decimal(str(c))) if l is None else l
        when = retrieved_at or clock.iso(s.close_utc + timedelta(minutes=61))
        with db.transaction(self.conn):
            snap = prices.record_snapshot(self.conn, source="fixture", url="fixture://bars", requested_at=when,
                                          retrieved_at=when, http_status=200, parse_status="ok")
            prices.insert_bar(self.conn, symbol, date, Decimal(str(o)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)),
                              v, source="fixture", snapshot_id=snap, retrieved_at=when, quality_flags=list(flags))

    def bars_all(self, date: str, price_by_symbol: dict) -> None:
        for sym, px in price_by_symbol.items():
            if isinstance(px, tuple):
                self.bar(sym, date, *px)
            else:
                self.bar(sym, date, px)

    def split(self, symbol: str, ex_date: str, num: int, den: int) -> None:
        with db.transaction(self.conn):
            corporate_actions.record(self.conn, symbol, "SPLIT", ex_date, ratio=(num, den), source="fixture",
                                     snapshot_id=None, now=clock.now_iso())

    def dividend(self, symbol: str, ex_date: str, amount: str) -> None:
        with db.transaction(self.conn):
            corporate_actions.record(self.conn, symbol, "CASH_DIVIDEND", ex_date, amount=Decimal(amount), source="fixture",
                                     snapshot_id=None, now=clock.now_iso())

    def cover_corporate_actions(self, through: str, symbols=None) -> None:
        with db.transaction(self.conn):
            for inst in FIXTURE_UNIVERSE:
                if symbols is None or inst.symbol in symbols:
                    corporate_actions.set_coverage(self.conn, inst.symbol, "2026-01-01", through, clock.now_iso(), None)

    def new_run(self) -> int:
        """A bare morning run row (no lock) so decisions have a run to belong to."""
        self._run_seq += 1
        # Keyed by the session its orders would target, as morning.start does (the UTC calendar date made QA
        # scenarios show "run dated Sat Oct 24" instead of "orders target the Mon Oct 26 open").
        key = cal.next_session_after(clock.now_utc() + timedelta(seconds=120)).date.isoformat()
        with db.transaction(self.conn):
            # The version counts runs for THAT key, as morning.start does: a global counter made QA scenarios
            # show "attempt 3" for a session's first and only run.
            prior = self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM runs WHERE experiment_id=? AND run_kind='morning' "
                                      "AND analysis_session_date=?", (self.exp_id, key)).fetchone()[0]
            # Scripted runs say so ("Scripted fixture (no model)"), like the fixture demo's, not "model not reported".
            return runs.create_run_row(self.conn, self.exp_id, "morning", key, version=prior + 1, mode="analysis",
                                       now=clock.now_iso(), model_provider="fixture", model_id="fixture-script", model_effort="n/a")
