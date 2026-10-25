"""Database guarantees that the experiment record depends on: migrations, append-only tables,
exactly-once funding, and strict separation of fixture data from the real experiment."""
import sqlite3

import pytest

from paper_advisor import clock, db


def test_migrations_apply_once_and_are_idempotent(app_paths):
    conn = db.init_db(app_paths.db_path, db_mode="forward")
    v1 = db.schema_version(conn)
    assert v1 >= 1
    conn.close()
    conn = db.connect(app_paths.db_path)
    db.migrate(conn)  # re-running is a no-op
    assert db.schema_version(conn) == v1
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == v1


def test_edited_migration_is_detected(app_paths, monkeypatch):
    conn = db.init_db(app_paths.db_path, db_mode="forward")
    conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
    with pytest.raises(db.MigrationError):
        db.migrate(conn)


def test_db_mode_is_fixed_at_creation(app_paths):
    conn = db.init_db(app_paths.db_path, db_mode="forward")
    assert db.db_mode(conn) == "forward"
    conn.close()
    with pytest.raises(db.MigrationError):
        db.init_db(app_paths.db_path, db_mode="fixture")  # cannot flip an existing DB's mode


def test_fixture_experiment_cannot_enter_forward_db(forward_db):
    now = clock.iso(clock.now_utc())
    with pytest.raises(sqlite3.IntegrityError):
        forward_db.execute(
            "INSERT INTO experiments(name, mode, starting_capital_cents, created_at, inception_at, status, benchmark_symbol)"
            " VALUES ('x','fixture',50000,?,?,'active','SPY')", (now, now))


def _make_experiment(conn, mode="forward"):
    now = clock.iso(clock.now_utc())
    cur = conn.execute(
        "INSERT INTO experiments(name, mode, starting_capital_cents, created_at, inception_at, status, benchmark_symbol)"
        " VALUES ('t',?,50000,?,?,'active','SPY')", (mode, now, now))
    return cur.lastrowid, now


def _funding(conn, exp_id, now, key):
    conn.execute(
        "INSERT INTO ledger_events(experiment_id, account, seq, event_type, effective_at, recorded_at,"
        " cash_delta_cents, idempotency_key, payload_sha256) VALUES (?, 'PORTFOLIO', ?, 'INITIAL_FUNDING', ?, ?, 50000, ?, 'raw-sql-test')",
        (exp_id, 1 if key == "a" else 2, now, now, key))


def test_initial_funding_is_exactly_once_per_account(forward_db):
    exp_id, now = _make_experiment(forward_db)
    _funding(forward_db, exp_id, now, "a")
    with pytest.raises(sqlite3.IntegrityError):
        _funding(forward_db, exp_id, now, "b")  # different key, still rejected by the partial unique index


def test_ledger_is_append_only(forward_db):
    exp_id, now = _make_experiment(forward_db)
    _funding(forward_db, exp_id, now, "a")
    with pytest.raises(sqlite3.IntegrityError):
        forward_db.execute("UPDATE ledger_events SET cash_delta_cents = 99999999")
    with pytest.raises(sqlite3.IntegrityError):
        forward_db.execute("DELETE FROM ledger_events")


def test_fill_cannot_precede_decision(forward_db):
    exp_id, now = _make_experiment(forward_db)
    with pytest.raises(sqlite3.IntegrityError):
        forward_db.execute(
            "INSERT INTO fills(order_id, experiment_id, account, symbol, side, session_date, reference_open, slippage_bps,"
            " fill_price, quantity_micro, gross_cents, commission_cents, net_cash_cents, decided_at, sim_exec_at,"
            " bar_retrieved_at, recorded_at) VALUES (1, ?, 'PORTFOLIO', 'SPY', 'BUY', '2026-10-26', '700', '10', '700.7',"
            " 1000000, 70070, 0, -70070, '2026-10-26T14:00:00.000000Z', '2026-10-26T13:30:00.000000Z',"
            " '2026-10-26T22:00:00.000000Z', '2026-10-27T13:00:00.000000Z')", (exp_id,))


def test_timestamps_must_be_canonical_utc(forward_db):
    with pytest.raises(sqlite3.IntegrityError):
        forward_db.execute(
            "INSERT INTO experiments(name, mode, starting_capital_cents, created_at, inception_at, status, benchmark_symbol)"
            " VALUES ('t','forward',50000,'2026-10-24 04:55:00','2026-10-24 04:55:00','active','SPY')")


def test_backup_is_consistent_and_verified(forward_db, app_paths):
    exp_id, now = _make_experiment(forward_db)
    _funding(forward_db, exp_id, now, "a")
    info = db.backup(forward_db, app_paths.backups_dir, reason="test")
    assert info["integrity_ok"] is True
    restored = db.connect(info["path"], readonly=True)
    assert restored.execute("SELECT SUM(cash_delta_cents) FROM ledger_events").fetchone()[0] == 50000


def test_threads_opening_and_closing_connections_at_once_never_deadlock(tmp_path):
    """SQLite 3.51.0-3.51.1 can deadlock when threads of one process open and close connections to the same file at
    the same time ("an obscure deadlock in the new broken-posix-lock detection logic", fixed upstream in 3.51.2). The
    dashboard's handler threads do exactly that; this suite hung on it once (Homebrew Python 3.13, SQLite 3.51.1),
    and the same pattern through plain sqlite3.connect froze 5 of 6 runs. Run in a subprocess: a deadlocked
    interpreter holds its GIL and cannot be interrupted, so only a timeout from outside can report it."""
    import subprocess
    import sys
    import textwrap
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(f"""
        import sys, threading
        sys.path.insert(0, {str(root)!r})
        from paper_advisor import db
        path = {str(tmp_path / "s.sqlite")!r}
        c = db.connect(path); c.execute("CREATE TABLE t(x)"); c.execute("INSERT INTO t VALUES (1)"); c.close()
        def worker(i):
            for _ in range(600):
                conn = db.connect(path, readonly=(i % 2 == 0))
                conn.execute("SELECT COUNT(*) FROM t").fetchone()
                conn.close()
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in ts: t.start()
        for t in ts: t.join()
        print("OK")
    """)
    p = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=45)
    assert p.stdout.strip() == "OK", p.stderr[-500:]


def test_the_database_engine_version_and_its_known_issues_are_named():
    # The record's integrity rests on SQLite: versions before 3.51.3 (except the 3.44.6 and 3.50.7 backports) carry the
    # WAL-reset corruption bug (rare; two connections writing or checkpointing at the same instant). Said, not hidden.
    assert db.engine_issues((3, 51, 1)) and "3.51.3" in db.engine_issues((3, 51, 1))[0]
    assert db.engine_issues((3, 50, 4)) and db.engine_issues((3, 44, 5))
    assert db.engine_issues((3, 51, 3)) == [] and db.engine_issues((3, 52, 0)) == []
    assert db.engine_issues((3, 50, 7)) == [] and db.engine_issues((3, 44, 6)) == []  # upstream backports
    assert db.engine_words().startswith(f"SQLite {sqlite3.sqlite_version}")
    if db.engine_issues():
        assert "brew upgrade sqlite" in db.engine_words() and "Data and health" in db.engine_words(short=True)
