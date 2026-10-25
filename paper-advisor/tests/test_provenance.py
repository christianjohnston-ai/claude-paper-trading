"""Which code produced a record: app version + git HEAD (read from .git files) + a SHA-256 tree hash
of the exact bytes that ran, stored on every run and experiment and immutable afterwards."""
import re
import sqlite3

import pytest

from paper_advisor import __version__, experiments, provenance, runs

from helpers import World


def _tree(tmp_path):
    app = tmp_path / "paper-advisor"
    (app / "paper_advisor").mkdir(parents=True)
    (app / "paper_advisor" / "a.py").write_text("x = 1\n")
    (app / "config").mkdir()
    (app / "config" / "skills_manifest.json").write_text("{}")
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "paper-morning.md").write_text("procedure v1")
    return app


def test_tree_hash_is_stable_and_changes_with_code_or_procedure(tmp_path):
    app = _tree(tmp_path)
    h1 = provenance.code_tree_sha256(app, tmp_path)
    assert re.fullmatch(r"[0-9a-f]{64}", h1) and provenance.code_tree_sha256(app, tmp_path) == h1
    (app / "paper_advisor" / "__pycache__").mkdir()
    (app / "paper_advisor" / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"junk")
    assert provenance.code_tree_sha256(app, tmp_path) == h1  # bytecode caches are not code
    (tmp_path / ".claude" / "commands" / "paper-morning.md").write_text("procedure v2")
    h2 = provenance.code_tree_sha256(app, tmp_path)
    assert h2 != h1  # the advisor's written procedure is part of what ran
    (app / "paper_advisor" / "a.py").write_text("x = 2\n")
    assert provenance.code_tree_sha256(app, tmp_path) not in (h1, h2)


def test_git_head_from_loose_ref_packed_refs_detached_or_absent(tmp_path):
    assert provenance.git_head(tmp_path) is None  # not a repository
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "packed-refs").write_text("# pack-refs with: peeled fully-peeled sorted\n" + "a" * 40 + " refs/heads/main\n")
    assert provenance.git_head(tmp_path) == "a" * 40
    (g / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    assert provenance.git_head(tmp_path) == "b" * 40  # a loose ref is newer than the packed one
    (g / "HEAD").write_text("c" * 40 + "\n")
    assert provenance.git_head(tmp_path) == "c" * 40  # detached HEAD


def test_runs_and_experiments_record_the_code_that_ran(fixture_db):
    w = World(fixture_db, start="2026-10-23T23:00:00")
    ident = provenance.code_identity()
    assert ident["code_version"].startswith(__version__ + "+") and re.fullmatch(r"[0-9a-f]{64}", ident["code_tree_sha256"])
    exp = experiments.get(fixture_db, w.exp_id)
    assert (exp["code_version"], exp["code_tree_sha256"]) == (ident["code_version"], ident["code_tree_sha256"])
    r = runs.get(fixture_db, w.new_run())  # every run, whoever creates it, records the code identity
    assert (r["code_version"], r["code_tree_sha256"]) == (ident["code_version"], ident["code_tree_sha256"])


def test_run_and_experiment_identity_cannot_be_edited(fixture_db):
    w = World(fixture_db, start="2026-10-23T23:00:00")
    rid = w.new_run()
    for sql, args in (("UPDATE runs SET code_version='edited' WHERE id=?", (rid,)),
                      ("UPDATE runs SET model_id='someone-else' WHERE id=?", (rid,)),
                      ("UPDATE experiments SET code_tree_sha256='0' WHERE id=?", (w.exp_id,))):
        with pytest.raises(sqlite3.DatabaseError):
            fixture_db.execute(sql, args)
