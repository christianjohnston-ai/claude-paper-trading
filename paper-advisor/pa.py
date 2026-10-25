#!/usr/bin/env python3
"""Launcher: puts the app directory on sys.path (resolved from this file, not the cwd) and runs the CLI."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from paper_advisor.cli import main  # noqa: E402

sys.exit(main())
