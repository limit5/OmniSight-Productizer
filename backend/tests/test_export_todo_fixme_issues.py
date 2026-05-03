"""FX.8.1 - tests for ``scripts/export_todo_fixme_issues.py``.

The exporter is intentionally stdlib-only and network-free under test.
These tests cover the scan/export layers and the resumable batch-create
logic without touching the live GitHub API.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "export_todo_fixme_issues.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import export_todo_fixme_issues as exporter  # noqa: E402


class TestScanMarkers:
    def test_scans_todo_and_fixme_markers(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "demo.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "def f():",
                "    # TODO: wire this to real provider",
                "    pass  # FIXME remove fallback",
            ]),
            encoding="utf-8",
        )

        issues = exporter.scan_markers(tmp_path)

        assert [issue.kind for issue in issues] == ["TODO", "FIXME"]
        assert issues[0].path == "backend/demo.py"
        assert issues[0].line == 2
        assert issues[0].text == "wire this to real provider"
        assert "backend/demo.py:2" in issues[0].body
        assert issues[1].text == "remove fallback"

    def test_skips_excluded_dirs_and_binary_files(self, tmp_path: Path) -> None:
        keep = tmp_path / "src.py"
        keep.write_text("# TODO: keep\n", encoding="utf-8")
        ignored = tmp_path / "test_assets" / "fixture.py"
        ignored.parent.mkdir()
        ignored.write_text("# TODO: ignored ground truth\n", encoding="utf-8")
        binary = tmp_path / "blob.py"
        binary.write_bytes(b"\0 TODO: ignored")

        issues = exporter.scan_markers(tmp_path)

        assert [issue.path for issue in issues] == ["src.py"]

    def test_marker_ids_are_stable(self, tmp_path: Path) -> None:
        src = tmp_path / "a.py"
        src.write_text("# TODO: stable\n", encoding="utf-8")

        first = exporter.scan_markers(tmp_path)[0].marker_id
        second = exporter.scan_markers(tmp_path)[0].marker_id

        assert first == second
        assert len(first) == 12

    def test_multiple_markers_on_one_line_keep_their_own_text(self, tmp_path: Path) -> None:
        src = tmp_path / "a.py"
        src.write_text("# TODO: first; FIXME: second\n", encoding="utf-8")

        issues = exporter.scan_markers(tmp_path)

        assert [issue.text for issue in issues] == ["first;", "second"]


class TestExportFormats:
    def test_jsonl_round_trip(self, tmp_path: Path) -> None:
        issue = exporter.build_issue(
            path="a.py",
            line=1,
            column=3,
            kind="TODO",
            text="ship",
            excerpt="# TODO: ship",
        )
        out = tmp_path / "issues.jsonl"

        exporter.write_jsonl(out, [issue])
        records = exporter.load_jsonl(out)

        assert records[0]["marker_id"] == issue.marker_id
        assert records[0]["title"].startswith("[TODO] a.py:1")

    def test_markdown_contains_summary_and_payload_table(self) -> None:
        issue = exporter.build_issue(
            path="a.py",
            line=1,
            column=3,
            kind="FIXME",
            text="repair",
            excerpt="# FIXME: repair",
        )

        body = exporter.render_markdown([issue])

        assert "Total markers: **1**" in body
        assert "`FIXME`: 1" in body
        assert "| ID | Marker | Source | Title |" in body


class TestBatchCreate:
    def test_dry_run_prints_payload_without_token(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        issue = exporter.build_issue(
            path="a.py",
            line=1,
            column=3,
            kind="TODO",
            text="ship",
            excerpt="# TODO: ship",
        )

        counts = exporter.batch_create_issues(
            [json.loads(json.dumps(asdict(issue)))],
            repo="owner/repo",
            token=None,
            dry_run=True,
        )

        assert counts == {"created": 0, "dry_run": 1, "skipped": 0}
        assert issue.marker_id in capsys.readouterr().out

    def test_real_create_requires_token(self) -> None:
        with pytest.raises(ValueError, match="token required"):
            exporter.batch_create_issues(
                [{"marker_id": "m", "title": "t", "body": "b"}],
                repo="owner/repo",
                token=None,
                dry_run=False,
            )

    def test_created_log_skips_existing_and_appends_new(self, tmp_path: Path) -> None:
        created_log = tmp_path / "created.jsonl"
        created_log.write_text(
            json.dumps({"marker_id": "old", "number": 1}) + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []

        def fake_create(repo: str, token: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append(payload)
            return {"number": 2, "html_url": "https://example/issues/2"}

        counts = exporter.batch_create_issues(
            [
                {"marker_id": "old", "title": "old", "body": "old"},
                {"marker_id": "new", "title": "new", "body": "new"},
            ],
            repo="owner/repo",
            token="token",
            dry_run=False,
            created_log=created_log,
            create_issue=fake_create,
        )

        assert counts == {"created": 1, "dry_run": 0, "skipped": 1}
        assert calls == [{"title": "new", "body": "new", "labels": ["todo-fixme", "technical-debt"]}]
        assert "https://example/issues/2" in created_log.read_text(encoding="utf-8")


class TestCli:
    def test_export_cli_writes_jsonl_and_markdown(self, tmp_path: Path) -> None:
        src = tmp_path / "a.py"
        src.write_text("# TODO: from cli\n", encoding="utf-8")
        out = tmp_path / "issues.jsonl"
        md = tmp_path / "issues.md"

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "export",
                "--root",
                str(tmp_path),
                "--out",
                str(out),
                "--markdown",
                str(md),
            ],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr
        assert "exported 1 markers" in proc.stdout
        assert json.loads(out.read_text(encoding="utf-8"))["text"] == "from cli"
        assert "Total markers: **1**" in md.read_text(encoding="utf-8")

    def test_batch_create_cli_dry_run(self, tmp_path: Path) -> None:
        issue = exporter.build_issue(
            path="a.py",
            line=1,
            column=3,
            kind="TODO",
            text="ship",
            excerpt="# TODO: ship",
        )
        src = tmp_path / "issues.jsonl"
        exporter.write_jsonl(src, [issue])

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "batch-create",
                "--input",
                str(src),
                "--repo",
                "owner/repo",
                "--dry-run",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr
        assert issue.marker_id in proc.stdout
        assert '"dry_run": 1' in proc.stdout


class TestStdlibOnly:
    def test_no_third_party_imports(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "import requests",
            "import httpx",
            "import yaml",
            "from pydantic",
        )
        for needle in forbidden:
            assert needle not in src, (
                f"scripts/export_todo_fixme_issues.py must stay stdlib-only, "
                f"found {needle!r}"
            )
