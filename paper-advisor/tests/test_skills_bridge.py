"""Skill integration safety: registry completeness, audited integrity, advice sanitization, and
the guarantee that only adapted skills can ever be invoked (no brokerage skill has a code path)."""
import inspect

import pytest

from paper_advisor import skills_bridge
from paper_advisor.paths import SKILLS_DIR

# This repository's own skills live in .claude/skills beside the third-party library but are not part of it: they are
# written for this project and tracked in git, so the audit, REGISTRY and the manifest do not cover them. Approved on
# 2026-10-25, when adding operating-paper-advisor there made the exact-equality test below fail.
# SYNC WARNING: adding any first-party skill under .claude/skills means listing its exact folder name here (never a
# pattern or prefix), and re-including it in .gitignore with a "!.claude/skills/<name>/" line under ".claude/skills/*".
# Third-party skills never go into this set: they enter skills_bridge.REGISTRY through the audit in SKILL_AUDIT.md.
PROJECT_SKILLS = {"operating-paper-advisor"}


def _third_party_dirs() -> set[str]:
    if not SKILLS_DIR.is_dir():
        return set()
    return {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()} - PROJECT_SKILLS


# "Installed" means the third-party library is present. A clean checkout, or an export of the tracked files, holds only
# the project's own skills there and must skip the library tests, as it did before any project skill existed.
installed = bool(_third_party_dirs())


@pytest.mark.skipif(not installed, reason="skills not installed on this machine")
def test_registry_covers_every_installed_skill_exactly():
    # The library guarantee is unchanged: every third-party folder is classified in REGISTRY, so an unaudited skill
    # cannot appear unnoticed. A project skill may not reuse a third-party name, or excluding it would hide that skill.
    assert not PROJECT_SKILLS & set(skills_bridge.REGISTRY), PROJECT_SKILLS & set(skills_bridge.REGISTRY)
    dirs = _third_party_dirs()
    assert dirs == set(skills_bridge.REGISTRY), (dirs ^ set(skills_bridge.REGISTRY))


@pytest.mark.skipif(not installed, reason="skills not installed on this machine")
def test_adopted_skills_match_the_audited_manifest():
    adopted = [n for n, (s, _) in skills_bridge.REGISTRY.items() if s in (skills_bridge.ADAPTED, skills_bridge.REVIEW, skills_bridge.LINT)]
    assert len(adopted) == 9
    for n in adopted:
        ok, why = skills_bridge.integrity(n)
        assert ok, f"{n}: {why}"


def test_brokerage_and_out_of_scope_skills_are_disabled_and_unreachable():
    for n in ("portfolio-manager", "parabolic-short-trade-planner", "breakout-trade-planner", "mt5-robot-tester",
              "options-strategy-advisor", "futures-position-sizer", "crypto-regime-analyzer", "pair-trade-screener"):
        assert skills_bridge.REGISTRY[n][0] == skills_bridge.DISABLED
    src = inspect.getsource(skills_bridge)
    body = src.split("ADVICE_KEYS")[1]  # everything after the registry table
    for n in ("portfolio-manager", "parabolic-short", "breakout-trade", "mt5-robot", "alpaca"):
        assert n not in body.lower(), f"skills_bridge code references {n}"


def test_advice_conflicting_with_the_rules_is_stripped():
    raw = {"composite": {"composite_score": 30, "actions": ["buy puts"], "exposure_guidance": "0-20%",
                         "note": "Consider inverse ETFs such as SQQQ", "zone": "Critical"}}
    stripped = []
    clean = skills_bridge._strip_advice(raw, stripped)
    assert "actions" not in clean["composite"] and "exposure_guidance" not in clean["composite"]
    assert clean["composite"]["note"].startswith("[removed") and clean["composite"]["zone"] == "Critical"
    assert "composite.actions" in stripped


def test_freshness_counts_sessions_not_calendar_days():
    assert skills_bridge._freshness("2026-10-23", "2026-10-23") == (True, 0)
    assert skills_bridge._freshness("2026-10-22", "2026-10-23") == (False, 1)
    assert skills_bridge._freshness("2026-11-25", "2026-11-27") == (False, 1)  # Thanksgiving is not a session
    assert skills_bridge._freshness(None, "2026-10-23") == (False, None)


def test_a_skill_subprocess_can_never_wait_for_input(tmp_path):
    """A child inherits the parent's stdin by default; if that stdin is an open pipe (an interactive
    or harnessed session) a child that reads it blocks at 0% CPU until the 60-180 s timeout. Simulate
    that parent exactly: fd 0 becomes a pipe whose write end stays open."""
    import os
    import sys
    import time
    r, w = os.pipe()
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        t0 = time.monotonic()
        rc, out, err = skills_bridge._run([sys.executable, "-I", "-c", "import sys; sys.stdin.read(); print('done')"], tmp_path, timeout=5)
        elapsed = time.monotonic() - t0
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(r)
        os.close(w)
    assert rc == 0 and out.strip() == "done" and elapsed < 3, (rc, out, err, elapsed)
