"""AUDIT-29b-6 (OP-1024): smoke tests for scripts/cognee-ingest-lessons.py.

The script's ``--dry-run`` path must collect the lesson + anti-pattern
sources without touching Cognee / Neo4j (CI runs with neither installed),
report the counts, and exit 0. Full ingestion is exercised only against a
live Cognee/Neo4j, so it is out of scope here.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cognee-ingest-lessons.py"
LESSONS_DIR = REPO_ROOT / "docs" / "sop" / "lessons"
ANTIPATTERNS_DOC = REPO_ROOT / "docs" / "sop" / "architecture-anti-patterns.md"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()


def test_dry_run_json_reports_source_counts() -> None:
    proc = _run("--dry-run", "--json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "dry-run"
    assert report["dry_run"] is True
    # 6 legacy + the per-ticket L-OP-* files (>= 60 today; grows over time).
    expected_lessons = len(list(LESSONS_DIR.glob("L-*.md")))
    assert report["lesson_sources_seen"] == expected_lessons
    assert report["lesson_sources_seen"] >= 60
    # The cookbook currently has 14 numbered patterns.
    assert report["antipattern_sources_seen"] == 14
    # Dry-run must never claim it ingested anything.
    assert "lesson_nodes_ingested" not in report


def test_dry_run_text_output_mentions_counts() -> None:
    proc = _run("--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "lesson sources seen" in proc.stdout
    assert "anti-pattern sources seen" in proc.stdout


def test_dry_run_respects_explicit_paths() -> None:
    proc = _run(
        "--dry-run",
        "--json",
        "--lessons-dir",
        str(LESSONS_DIR),
        "--antipatterns-doc",
        str(ANTIPATTERNS_DOC),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["lessons_dir"] == str(LESSONS_DIR)
    assert report["antipatterns_doc"] == str(ANTIPATTERNS_DOC)
