"""CSV/JSON export of the experiment record (local files only; nothing is uploaded)."""
from __future__ import annotations

import csv
import hashlib
import json
import zipfile

from . import clock, db, metrics

TABLES = ("experiments", "config_versions", "strategy_versions", "strategy_hypotheses", "dependencies", "instruments",
          "instrument_verifications", "runs", "run_events", "run_submissions", "decisions", "candidate_assessments", "risk_checks",
          "orders", "order_events", "fills", "ledger_events", "position_rules", "corporate_actions", "valuation_snapshots",
          "reconciliation_results", "evidence_items", "skill_runs", "source_snapshots", "reports", "backups")


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(v):
    """Neutralize spreadsheet formula injection: exported text includes untrusted headlines and skill
    output, and a cell like '=HYPERLINK(...)' would execute when the CSV is opened in a spreadsheet.
    Numbers are left as numbers; only text cells starting with a formula trigger get a leading quote."""
    if isinstance(v, str) and v.startswith(FORMULA_PREFIXES):
        return "'" + v
    return v


def export_all(conn, paths, experiment_id: int | None = None) -> dict:
    stamp = clock.now_utc().strftime("%Y%m%dT%H%M%SZ")
    # The file name says which database it came from, so a fixture export is never mistaken for the
    # forward test's. SYNC WARNING: dashboard/server.py EXPORT_RE accepts exactly this pattern.
    mode = db.db_mode(conn)
    # The fixture demo's exports live in their own folder (dashboard/server.py serves both).
    out = paths.exports_dir / ("fixture" if mode == "fixture" else "") / f"export_{mode}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"created_at": clock.now_iso(), "paper_trading": True, "files": {}}
    for t in TABLES:
        cur = conn.execute(f"SELECT * FROM {t}")  # noqa: S608 - fixed table list
        cols = [c[0] for c in cur.description]  # known even when the table is empty
        rows = cur.fetchall()
        p = out / f"{t}.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # Header always: an empty table still documents its columns (it used to be a 0-byte file).
            w.writerow(cols)
            w.writerows([tuple(safe_cell(x) for x in r) for r in rows])
        manifest["files"][p.name] = {"rows": len(rows), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    if experiment_id is not None:
        mt = metrics.compute(conn, experiment_id)
        (out / "metrics.json").write_text(json.dumps(mt, indent=1, default=str))
        manifest["files"]["metrics.json"] = {"sha256": hashlib.sha256((out / "metrics.json").read_bytes()).hexdigest()}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    zp = out.with_suffix(".zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.iterdir()):
            z.write(p, arcname=f"{out.name}/{p.name}")
    # "files" counts what the zip actually holds (the data files plus MANIFEST.json itself).
    return {"dir": str(out), "zip": str(zp), "tables": len(TABLES), "files": len(manifest["files"]) + 1}
