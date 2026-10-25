#!/usr/bin/env python3
"""Install the third-party research skills paper-advisor was audited against, exactly as audited.

Why this script exists: paper-advisor runs 9 skills from tradermonty/claude-trading-skills as isolated
subprocesses, and its test suite expects all 74 of that library's folders, each in .claude/skills/<name>/.
They are not stored in this repository (they are someone else's MIT-licensed work, and git-ignored on
purpose), so a fresh clone has none of them. Without them the morning run still works, but the audited
skills are skipped and the briefing has less evidence.

What it does, and why each step:
- Downloads the library's source archive for ONE commit, the one in paper-advisor/config/skills_manifest.json.
  A pinned commit is the only version SKILL_AUDIT.md describes; "latest" would run code nobody audited.
- Copies only the archive's skills/<name>/ folders into .claude/skills/. Nothing from the download is ever
  imported or run here; archive entries that are links, devices, absolute paths or ".." are refused.
- Never overwrites a skill folder that differs from the download: it stops and names it, because a changed
  folder is either local edits or a different version, and replacing either silently would hide that.
- Checks the result: the 74 folder names must equal paper-advisor's REGISTRY, and the 9 adopted skills must
  match the audited tree hashes in the manifest (the same check skills_bridge runs before every use).

Run it with the project's venv Python (3.10 or newer):  paper-advisor/.venv/bin/python scripts/install_trading_skills.py
Exit status 0 means every check passed. Anything else prints the reason and changes nothing further.
"""
from __future__ import annotations

import json
import shutil
import ssl
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is needed. Run it with the project's venv: paper-advisor/.venv/bin/python "
             "scripts/install_trading_skills.py")

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "paper-advisor"
sys.path.insert(0, str(APP))
from paper_advisor import skills_bridge  # noqa: E402  (this project's own code: the hash function and REGISTRY)

MANIFEST = json.loads((APP / "config" / "skills_manifest.json").read_text(encoding="utf-8"))
COMMIT = MANIFEST["commit"]
UPSTREAM = MANIFEST["upstream"].rstrip("/")
ARCHIVE_URL = f"https://codeload.github.com/{UPSTREAM.split('github.com/', 1)[1]}/tar.gz/{COMMIT}"
MAX_BYTES = 200 * 1024 * 1024  # the archive is about 19 MB; anything far larger is not what was audited

# The skills in the library that can contact a real brokerage (SKILL_AUDIT.md section 3). paper-advisor never
# runs them: they are DISABLED in skills_bridge.REGISTRY, with no adapter and no code path. Said after every
# install because the folders are now on disk, where a Claude Code session could still be asked to use them.
# SYNC WARNING: mirrors SKILL_AUDIT.md section 3, paper-advisor/README.md "Safety" and the brokerage list in
# tests/test_skills_bridge.py; the root README.md says the same in one line.
BROKER_CAPABLE = {
    "portfolio-manager": "reads an Alpaca brokerage account",
    "parabolic-short-trade-planner": "calls Alpaca's trading host",
    "breakout-trade-planner": "builds Alpaca order requests",
    "mt5-robot-tester": "launches a MetaTrader broker terminal",
}


def _ssl_context() -> ssl.SSLContext:
    # Some Python builds on macOS ship without CA certificates (the python.org installer needs a separate step).
    # The project venv holds certifi (requirements-skills.txt), so use it when it is there.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def download(dest: Path) -> None:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "paper-advisor skill installer"})
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r, dest.open("wb") as f:
                total = 0
                while chunk := r.read(1 << 20):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise RuntimeError(f"archive larger than {MAX_BYTES} bytes; refusing it")
                    f.write(chunk)
            return
        except Exception as exc:  # network errors, timeouts, HTTP errors: retry, then report
            last = exc
            time.sleep(2 * (attempt + 1))
    sys.exit(f"Could not download {ARCHIVE_URL}: {last}")


def extract_skills(archive: Path, staging: Path) -> set[str]:
    """Copy <top>/skills/<name>/... members into staging/<name>/..., refusing anything that is not a plain file
    or folder inside that tree. Files are written from their content (extractfile), never with tar's extract."""
    names = set()
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if len(parts) < 3 or parts[1] != "skills":
                continue
            rel = Path(*parts[2:])
            if m.name.startswith("/") or ".." in parts or rel.is_absolute():
                sys.exit(f"Refusing an archive entry outside the skills folder: {m.name}")
            if m.isdir():
                (staging / rel).mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                out = staging / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                out.write_bytes(src.read() if src else b"")
                out.chmod(0o755 if m.mode & 0o111 else 0o644)
            else:
                sys.exit(f"Refusing a link or special file in the archive: {m.name}")
            names.add(parts[2])
    return names


def main() -> int:
    skills_dir = skills_bridge.SKILLS_DIR
    if skills_dir.resolve() != (ROOT / ".claude" / "skills").resolve():
        sys.exit(f"paper-advisor looks for skills in {skills_dir}, not under this repository; is the layout intact?")
    with tempfile.TemporaryDirectory(prefix="trading-skills-") as tmp:
        archive, staging = Path(tmp) / "skills.tar.gz", Path(tmp) / "skills"
        print(f"Downloading tradermonty/claude-trading-skills at commit {COMMIT[:7]} ...")
        download(archive)
        names = extract_skills(archive, staging)
        expected = set(skills_bridge.REGISTRY)
        if names != expected:
            sys.exit(f"The download's skill folders do not match paper-advisor's audited list: "
                     f"missing {sorted(expected - names)}, unexpected {sorted(names - expected)}")
        # Check every folder that already exists before touching anything, so a refusal leaves no half install.
        clash = [n for n in sorted(names) if (skills_dir / n).exists()
                 and skills_bridge.tree_hash(skills_dir / n) != skills_bridge.tree_hash(staging / n)]
        if clash:
            sys.exit("These skill folders already exist and differ from the audited version, so nothing was changed: "
                     + ", ".join(clash) + ". Move them aside (or restore them) and run this again.")
        skills_dir.mkdir(parents=True, exist_ok=True)
        new = 0
        for n in sorted(names):
            if not (skills_dir / n).exists():
                shutil.copytree(staging / n, skills_dir / n)
                new += 1
    audited = MANIFEST["skills"]
    bad = [n for n in audited if not skills_bridge.integrity(n)[0]]
    if bad:
        sys.exit(f"Installed, but these audited skills do not match their fingerprints: {', '.join(bad)}")
    print(f"({new} copied now, {len(names) - new} already in place)")
    print(f"{len(names)} skills installed; {len(audited)} of {len(audited)} audited skills match {COMMIT[:7]}.")
    print("Four of them can contact a real brokerage: "
          + "; ".join(f"{n} ({why})" for n, why in BROKER_CAPABLE.items())
          + ". paper-advisor never runs them (they are disabled, with no code path), and they need broker keys "
            "you should never give them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
