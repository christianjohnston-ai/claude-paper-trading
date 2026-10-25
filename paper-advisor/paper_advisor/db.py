"""SQLite access: connections, migrations, transactions, consistent backups.

Why these pragmas: WAL lets the dashboard read while a morning run writes; synchronous=FULL makes a
committed ledger event survive a power loss; foreign_keys enforces references; busy_timeout makes a
second process wait briefly instead of failing on a momentary lock. Writers always use
BEGIN IMMEDIATE (see transaction()) so two processes cannot interleave a read-check-write sequence.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import clock

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class MigrationError(RuntimeError):
    pass


# SQLite 3.51.0-3.51.1 can deadlock when threads of one process open and close connections to the same database file at
# the same moment ("an obscure deadlock in the new broken-posix-lock detection logic", fixed upstream in 3.51.2). The
# dashboard's handler threads do exactly that, Homebrew's Python 3.13 links 3.51.1, and the frozen interpreter cannot
# even be interrupted (the closing thread holds the GIL). So opening and closing are serialized within this process;
# queries still run in parallel, and an open or a close takes well under a millisecond. Processes are unaffected (the
# deadlock is between threads). tests/test_db.py reproduces the hang through plain sqlite3.connect.
_OPEN_CLOSE = threading.RLock()


class _Connection(sqlite3.Connection):
    """A connection whose close(), explicit or when it is dropped unclosed, takes the open/close lock."""

    def close(self) -> None:
        with _OPEN_CLOSE:
            super().close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # dropped by a thread other than its own (or at exit): SQLite's own cleanup closes it
            pass


def engine_issues(version: tuple[int, int, int] = sqlite3.sqlite_version_info) -> list[str]:
    """Known SQLite defects that bear on this record, for the version Python links (stated on the dashboard and by
    /paper-status; paper-advisor cannot fix the library). Source: sqlite.org/wal.html#walresetbug and the 3.51.2/3.51.3
    release notes. The 3.51.0-3.51.1 thread deadlock is avoided in-process (see _OPEN_CLOSE), so it is not listed."""
    backported = (3, 44, 6) <= version < (3, 45, 0) or (3, 50, 7) <= version < (3, 51, 0)
    if version < (3, 51, 3) and not backported:
        return ["the WAL-reset bug (fixed in SQLite 3.51.3, backported to 3.50.7 and 3.44.6): if two connections write or "
                "checkpoint at the same instant, a WAL database can be corrupted. SQLite rates it unlikely in common use, and "
                "every backup's integrity is checked. Upgrading the SQLite library Python uses removes it"]
    return []


# The upgrade for the usual install here (Homebrew Python links Homebrew's SQLite); the dashboard offers it to copy.
ENGINE_UPGRADE_HOMEBREW = "brew upgrade sqlite"


def engine_words(short: bool = False) -> str:
    """'SQLite 3.51.1; known issue: ...' (short: the issue's name only, for /paper-status)."""
    issues = engine_issues()
    if not issues:
        return f"SQLite {sqlite3.sqlite_version}; no known issue affects this record"
    if short:
        return f"SQLite {sqlite3.sqlite_version}; known issue: the WAL-reset bug (see the dashboard's Data and health)"
    return f"SQLite {sqlite3.sqlite_version}; known issue: {issues[0]} (Homebrew Python: {ENGINE_UPGRADE_HOMEBREW})."


def connect(path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    with _OPEN_CLOSE:
        if readonly:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=10, factory=_Connection)
        else:
            conn = sqlite3.connect(str(path), isolation_level=None, timeout=10, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception. Nested use is not supported on
    purpose: a nested call would silently widen or split atomicity boundaries."""
    if conn.in_transaction:
        raise RuntimeError("nested transaction; restructure the caller so one transaction owns the unit of work")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migration_files() -> list[tuple[int, str, str, str]]:
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        sql = p.read_text(encoding="utf-8")
        out.append((int(p.name[:4]), p.stem, sql, hashlib.sha256(sql.encode()).hexdigest()))
    return out


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r["version"]: r for r in conn.execute("SELECT * FROM schema_migrations")}
    for version, name, sql, checksum in _migration_files():
        if version in applied:
            if applied[version]["checksum"] != checksum:
                raise MigrationError(
                    f"migration {name} was edited after it was applied (checksum mismatch). Migrations are "
                    "immutable: add a new migration file instead.")
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                         (version, name, checksum, clock.now_iso()))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements without breaking CREATE TRIGGER ... BEGIN ... END; bodies.
    sqlite3.complete_statement tells us when the accumulated text is a full statement."""
    stmts, buf = [], ""
    for line in sql.splitlines(keepends=True):
        if not buf and (line.strip() == "" or line.strip().startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        raise MigrationError("migration ends with an incomplete statement")
    return stmts


def schema_version(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(r[0] or 0)


def db_mode(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT value FROM app_state WHERE key='db_mode'").fetchone()
    if r is None:
        raise MigrationError("database has no db_mode; it was not initialized by paper-advisor")
    return r["value"]


def init_db(path, db_mode: str) -> sqlite3.Connection:
    """Create or open a database, apply migrations, and fix its mode ('forward' or 'fixture') the
    first time. Re-opening with a different mode is refused, so a fixture run can never be pointed
    at the real experiment database (or vice versa)."""
    if db_mode not in ("forward", "fixture"):
        raise ValueError("db_mode must be 'forward' or 'fixture'")
    conn = connect(path)
    migrate(conn)
    existing = conn.execute("SELECT value FROM app_state WHERE key='db_mode'").fetchone()
    if existing is None:
        with transaction(conn):
            conn.execute("INSERT INTO app_state(key, value) VALUES ('db_mode', ?)", (db_mode,))
            conn.execute("INSERT INTO app_state(key, value) VALUES ('created_at', ?)", (clock.now_iso(),))
    elif existing["value"] != db_mode:
        conn.close()
        raise MigrationError(f"database {path} is a '{existing['value']}' database, not '{db_mode}'")
    return conn


def open_existing(path, readonly: bool = False) -> sqlite3.Connection:
    if not Path(path).exists():
        raise FileNotFoundError(f"database not found: {path}. Run `bin/pa init` first.")
    conn = connect(path, readonly=readonly)
    if not readonly:
        migrate(conn)
    return conn


def get_state(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return default if r is None else r["value"]


def set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(conn: sqlite3.Connection, backups_dir, reason: str) -> dict:
    """Consistent online backup via the SQLite backup API (safe while other readers are open), then
    an integrity_check on the copy. The backup is recorded in the source database."""
    mode = db_mode(conn)
    # The fixture demo's backups live in their own folder: next to the forward test's they were told apart only by a
    # name prefix, and the forward prune counted them. SYNC WARNING: prune_backups and dashboard/server.App.dirs.
    backups_dir = Path(backups_dir) / ("fixture" if mode == "fixture" else "")
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock.now_utc().strftime("%Y%m%dT%H%M%S%fZ")
    dest_path = backups_dir / f"paper_advisor-{mode}-{stamp}.sqlite"
    with _OPEN_CLOSE:  # see _OPEN_CLOSE: every open and close in this process is serialized
        dest = sqlite3.connect(str(dest_path), factory=_Connection)
    try:
        conn.backup(dest)
        ok = dest.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        dest.close()
    info = {"path": str(dest_path), "sha256": sha256_file(dest_path), "bytes": os.path.getsize(dest_path),
            "integrity_ok": ok, "reason": reason, "created_at": clock.now_iso()}
    with transaction(conn):
        conn.execute("INSERT INTO backups(created_at, path, sha256, bytes, integrity_ok, reason) VALUES (?,?,?,?,?,?)",
                     (info["created_at"], info["path"], info["sha256"], info["bytes"], int(ok), reason))
    return info


def prune_backups(backups_dir, keep: int, mode: str = "forward") -> list[str]:
    """Keep the newest `keep` automatic backups of ONE database (files are never the only copy of anything: the live
    database is the source of truth). Each mode has its own folder and name prefix, so a prune never counts or
    deletes the other database's backups. SYNC WARNING: mirrors backup()'s folder and file name."""
    folder = Path(backups_dir) / ("fixture" if mode == "fixture" else "")
    files = sorted(folder.glob(f"paper_advisor-{mode}-*.sqlite"))
    removed = []
    for p in files[:-keep] if keep > 0 else []:
        p.unlink()
        removed.append(str(p))
    return removed
