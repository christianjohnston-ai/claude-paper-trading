"""Exports are local and injection-safe; /paper-status is strictly read-only."""
import csv
import sqlite3

import pytest

from paper_advisor import clock, db, exporter, status

from helpers import World
from test_risk import FRESH


def test_csv_export_neutralizes_formula_injection(fixture_db, app_paths):
    w = World(fixture_db, start="2026-10-23T23:00:00")
    with db.transaction(fixture_db):
        fixture_db.execute("INSERT INTO evidence_items(experiment_id, kind, title, summary, created_by, created_at) VALUES (?, 'news', ?, ?, 'system', ?)",
                           (w.exp_id, '=HYPERLINK("http://evil.example","click")', "+cmd|' /C calc'!A0", clock.now_iso()))
    out = exporter.export_all(fixture_db, app_paths, w.exp_id)
    rows = list(csv.DictReader(open(f"{out['dir']}/evidence_items.csv", encoding="utf-8")))
    assert rows[0]["title"].startswith("'=") and rows[0]["summary"].startswith("'+")
    ledger = list(csv.DictReader(open(f"{out['dir']}/ledger_events.csv", encoding="utf-8")))
    assert ledger[0]["cash_delta_cents"] == "50000"  # numbers untouched


def test_empty_tables_export_their_header_and_counts_match_the_zip(fixture_db, app_paths):
    import zipfile
    w = World(fixture_db, start="2026-10-23T23:00:00")  # funded, but no decisions or fills yet
    out = exporter.export_all(fixture_db, app_paths, w.exp_id)
    with open(f"{out['dir']}/fills.csv", encoding="utf-8") as f:
        header = next(csv.reader(f), None)
    # An empty table still documents its columns (it exported as a 0-byte file before).
    assert header is not None and "fill_price" in header and "sim_exec_at" in header
    with zipfile.ZipFile(out["zip"]) as z:
        assert out["files"] == len(z.namelist())  # the reported count is what the owner will find in the zip
    assert out["tables"] == len(exporter.TABLES)


def test_status_is_read_only(fixture_db, app_paths):
    w = World(fixture_db, start="2026-10-23T23:00:00")
    w.bars_all("2026-10-23", FRESH)
    fixture_db.close()
    ro = db.connect(app_paths.fixture_db_path, readonly=True)
    before = {t: ro.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("fills", "orders", "decisions", "valuation_snapshots", "runs")}
    g = status.gather(ro)
    text = status.render(g)
    assert "PAPER TRADING" in text and g["equity_cents"] == 50000
    # Read by the owner through /paper-status: the market state, the benchmark and the start time in words, not
    # stored codes ("after_close"), a hard-coded "SPY" for the fixture's DEMO.SPY, or a raw UTC stamp.
    assert "Market: after the close" in text and "vs DEMO.SPY" in text and "started Fri Oct 23, 2026, 4:00 PM PT" in text
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO app_state(key, value) VALUES ('x', 'y')")
    after = {t: ro.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after


def test_fixture_files_live_apart_and_pruning_never_crosses_modes(app_paths):
    """Fixture backups, exports and reports sat in the forward test's folders, told apart only by a name prefix, and
    the forward prune (glob paper_advisor-*.sqlite) also deleted fixture backups."""
    from pathlib import Path
    from paper_advisor import db, exporter, fixture_demo
    fixture_demo.run_demo(app_paths, rebuild=True)
    fx = db.open_existing(app_paths.fixture_db_path)
    fwd = db.init_db(app_paths.db_path, db_mode="forward")
    a = db.backup(fx, app_paths.backups_dir, reason="t")
    b = db.backup(fx, app_paths.backups_dir, reason="t")
    c = db.backup(fwd, app_paths.backups_dir, reason="t")
    assert Path(a["path"]).parent == app_paths.backups_dir / "fixture" and Path(c["path"]).parent == app_paths.backups_dir
    db.prune_backups(app_paths.backups_dir, 1)
    assert Path(a["path"]).exists() and Path(b["path"]).exists() and Path(c["path"]).exists()
    out = exporter.export_all(fx, app_paths, 1)
    assert Path(out["zip"]).parent == app_paths.exports_dir / "fixture"
    paths = [r[0] for r in fx.execute("SELECT path FROM reports")]
    assert paths and all("/fixture/" in p and "_fixture_" in Path(p).name for p in paths)
    assert all("FIXTURE DEMO" in Path(p).read_text().splitlines()[0] for p in paths)
