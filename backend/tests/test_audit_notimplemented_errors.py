"""FX.8.2 - tests for ``scripts/audit_notimplemented_errors.py``.

The audit script is stdlib-only and does not touch production services.
These tests cover the conservative classifier, export formats, and CLI
without relying on live repo state.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "audit_notimplemented_errors.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import audit_notimplemented_errors as auditor  # noqa: E402


class TestScanNotImplemented:
    def test_abstract_method_is_reasonable_contract(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "adapter.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "from abc import ABC, abstractmethod",
                "",
                "class Adapter(ABC):",
                "    @abstractmethod",
                "    def send(self):",
                "        raise NotImplementedError",
            ]),
            encoding="utf-8",
        )

        findings = auditor.scan_notimplemented(tmp_path)

        assert len(findings) == 1
        assert findings[0].verdict == "reasonable"
        assert findings[0].category == "abstract-contract"
        assert findings[0].symbol == "Adapter.send"

    def test_concrete_bare_raise_is_stub(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "worker.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "def run_job():",
                "    raise NotImplementedError",
            ]),
            encoding="utf-8",
        )

        findings = auditor.scan_notimplemented(tmp_path)

        assert len(findings) == 1
        assert findings[0].verdict == "stub"
        assert findings[0].category == "concrete-bare-raise"
        assert findings[0].symbol == "run_job"

    def test_explicit_unsupported_path_is_reasonable(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "queue.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "def select_backend(name):",
                "    raise NotImplementedError(f'unsupported backend: {name}')",
            ]),
            encoding="utf-8",
        )

        findings = auditor.scan_notimplemented(tmp_path)

        assert len(findings) == 1
        assert findings[0].verdict == "reasonable"
        assert findings[0].category == "explicit-unsupported"
        assert findings[0].symbol == "select_backend"

    def test_placeholder_message_is_stub(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "feature.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "def ship_later():",
                "    raise NotImplementedError('TODO: implement later')",
            ]),
            encoding="utf-8",
        )

        findings = auditor.scan_notimplemented(tmp_path)

        assert len(findings) == 1
        assert findings[0].verdict == "stub"
        assert findings[0].category == "concrete-placeholder"

    def test_except_handler_is_reasonable(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "fallback.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "def invoke(fn):",
                "    try:",
                "        return fn()",
                "    except NotImplementedError:",
                "        return None",
            ]),
            encoding="utf-8",
        )

        findings = auditor.scan_notimplemented(tmp_path)

        assert len(findings) == 1
        assert findings[0].verdict == "reasonable"
        assert findings[0].category == "caught-optional"

    def test_tuple_except_handler_is_reasonable(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "fallback.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "def invoke(fn):",
                "    try:",
                "        return fn()",
                "    except (AttributeError, NotImplementedError):",
                "        return None",
            ]),
            encoding="utf-8",
        )

        findings = auditor.scan_notimplemented(tmp_path)

        assert len(findings) == 1
        assert findings[0].verdict == "reasonable"
        assert findings[0].category == "caught-optional"

    def test_skips_tests_and_excluded_dirs_by_default(self, tmp_path: Path) -> None:
        keep = tmp_path / "backend" / "prod.py"
        keep.parent.mkdir()
        keep.write_text("raise NotImplementedError\n", encoding="utf-8")
        ignored_test = tmp_path / "backend" / "tests" / "test_prod.py"
        ignored_test.parent.mkdir()
        ignored_test.write_text("raise NotImplementedError\n", encoding="utf-8")
        ignored_asset = tmp_path / "test_assets" / "fixture.py"
        ignored_asset.parent.mkdir()
        ignored_asset.write_text("raise NotImplementedError\n", encoding="utf-8")

        findings = auditor.scan_notimplemented(tmp_path)

        assert [finding.path for finding in findings] == ["backend/prod.py"]


class TestExportFormats:
    def test_jsonl_round_trip(self, tmp_path: Path) -> None:
        finding = auditor.AuditFinding(
            finding_id="abc123",
            verdict="stub",
            category="concrete-bare-raise",
            path="a.py",
            line=1,
            column=7,
            symbol="run",
            message="",
            excerpt="raise NotImplementedError",
            rationale="Concrete bare raise.",
        )
        out = tmp_path / "audit.jsonl"

        auditor.write_jsonl(out, [finding])
        records = auditor.load_jsonl(out)

        assert records[0]["finding_id"] == "abc123"
        assert records[0]["verdict"] == "stub"

    def test_markdown_contains_summary_and_findings_table(self) -> None:
        finding = auditor.AuditFinding(
            finding_id="abc123",
            verdict="reasonable",
            category="abstract-contract",
            path="a.py",
            line=1,
            column=7,
            symbol="Adapter.run",
            message="",
            excerpt="raise NotImplementedError",
            rationale="Abstract contract.",
        )

        body = auditor.render_markdown([finding])

        assert "Total occurrences: **1**" in body
        assert "`reasonable`: 1" in body
        assert "| ID | Verdict | Category | Source | Symbol | Rationale |" in body


class TestCli:
    def test_audit_cli_writes_jsonl_and_markdown(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "demo.py"
        src.parent.mkdir()
        src.write_text(
            "\n".join([
                "def demo():",
                "    raise NotImplementedError",
            ]),
            encoding="utf-8",
        )
        out = tmp_path / "audit.jsonl"
        md = tmp_path / "audit.md"

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "audit",
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
        assert "audited 1 NotImplementedError occurrences" in proc.stdout
        assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "stub"
        assert "Total occurrences: **1**" in md.read_text(encoding="utf-8")

    def test_fail_on_stub_returns_distinct_exit_code(self, tmp_path: Path) -> None:
        src = tmp_path / "backend" / "demo.py"
        src.parent.mkdir()
        src.write_text("raise NotImplementedError\n", encoding="utf-8")
        out = tmp_path / "audit.jsonl"

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "audit",
                "--root",
                str(tmp_path),
                "--out",
                str(out),
                "--fail-on-stub",
            ],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 2
        assert "1 reasonable, 1 stub" not in proc.stdout
        assert "0 reasonable, 1 stub" in proc.stdout


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
                "scripts/audit_notimplemented_errors.py must stay stdlib-only, "
                f"found {needle!r}"
            )
