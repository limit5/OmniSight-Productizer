#!/usr/bin/env python3
"""Compatibility no-op for the retired generated lessons index."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from backend import docs_site_lessons

LESSONS_DIR = docs_site_lessons.LESSONS_DIR
REPO_ROOT = docs_site_lessons.REPO_ROOT
build_lessons_index = docs_site_lessons.build_lessons_index

INDEX_PATH = REPO_ROOT / "docs" / "sop" / "lessons-learned.md"


def main() -> int:
    build_lessons_index()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
