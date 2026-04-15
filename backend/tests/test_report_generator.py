"""Tests for B3/REPORT-01 — report_generator module.

Covers:
  - ReportData dataclass construction
  - Markdown rendering (golden file comparison)
  - Signed URL generation + verification
  - Section builders with fixture workflow data
  - PDF render path (import-error handling)
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.report_generator import (
    ExecutionSection,
    OutcomeSection,
    ReportData,
    SpecSection,
    _fmt_ts,
    _spec_field_row,
    generate_signed_url,
    render_markdown,
    verify_signed_url,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture — canonical report for golden comparison
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FIXTURE_SPEC = {
    "project_type": {"value": "web_app", "confidence": 0.95},
    "runtime_model": {"value": "ssr", "confidence": 0.85},
    "target_arch": {"value": "x86_64", "confidence": 0.9},
    "target_os": {"value": "linux", "confidence": 0.8},
    "framework": {"value": "nextjs", "confidence": 0.92},
    "persistence": {"value": "postgres", "confidence": 0.75},
    "deploy_target": {"value": "cloud", "confidence": 0.88},
    "hardware_required": {"value": "no", "confidence": 0.99},
    "raw_text": "Build a Next.js SSR app with Postgres on cloud",
    "conflicts": [],
}


def _make_fixture_report() -> ReportData:
    return ReportData(
        report_id="rpt-test000001",
        title="Test Project Report",
        generated_at="2026-04-15T12:00:00+00:00",
        version="1.0.0",
        spec=SpecSection(
            parsed_spec=FIXTURE_SPEC,
            clarifications=[
                {
                    "id": "dec-001",
                    "title": "Runtime model ambiguity",
                    "chosen": "ssr",
                    "resolver": "user",
                    "severity": "routine",
                },
            ],
            input_sources=["https://github.com/example/my-app"],
        ),
        execution=ExecutionSection(
            workflow_run={
                "id": "wf-abc123",
                "kind": "invoke",
                "status": "completed",
                "started_at": 1744718400.0,
                "completed_at": 1744718700.0,
                "metadata": {},
            },
            steps=[
                {
                    "id": "step-001",
                    "key": "parse_intent",
                    "started_at": 1744718400.0,
                    "completed_at": 1744718420.0,
                    "is_done": True,
                    "error": None,
                },
                {
                    "id": "step-002",
                    "key": "draft_dag",
                    "started_at": 1744718420.0,
                    "completed_at": 1744718500.0,
                    "is_done": True,
                    "error": None,
                },
                {
                    "id": "step-003",
                    "key": "compile",
                    "started_at": 1744718500.0,
                    "completed_at": 1744718600.0,
                    "is_done": False,
                    "error": "TimeoutError: build exceeded 60s",
                },
                {
                    "id": "step-004",
                    "key": "compile_retry",
                    "started_at": 1744718610.0,
                    "completed_at": 1744718700.0,
                    "is_done": True,
                    "error": None,
                },
            ],
            retries=[
                {
                    "id": "step-003",
                    "key": "compile",
                    "started_at": 1744718500.0,
                    "completed_at": 1744718600.0,
                    "is_done": False,
                    "error": "TimeoutError: build exceeded 60s",
                },
            ],
            decisions=[
                {
                    "id": "dec-002",
                    "title": "Use SSR over SSG",
                    "severity": "routine",
                    "status": "approved",
                },
            ],
        ),
        outcome=OutcomeSection(
            deploy_url="https://my-app.omnisight.dev",
            smoke_test_results=[
                {"label": "health-check", "status": "pass"},
                {"label": "api-smoke", "status": "pass"},
            ],
            open_findings=[
                {
                    "id": "dbg-finding-001",
                    "finding_type": "error_repeated",
                    "severity": "warn",
                    "content": "Intermittent connection timeout to DB on cold start",
                    "agent_id": "software-alpha",
                    "status": "open",
                },
            ],
        ),
    )


GOLDEN_FILE = Path(__file__).parent / "golden" / "project_report_golden.md"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests — Markdown rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderMarkdown:
    def test_contains_title(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "# Test Project Report" in md

    def test_contains_report_id(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "`rpt-test000001`" in md

    def test_section_headers(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "## 1. Specification" in md
        assert "## 2. Execution" in md
        assert "## 3. Outcome" in md

    def test_spec_fields_table(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "| project_type |" in md
        assert "web_app" in md
        assert "95%" in md

    def test_raw_text_block(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "Build a Next.js SSR app" in md

    def test_clarifications(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "Runtime model ambiguity" in md
        assert "chosen: `ssr`" in md

    def test_input_sources(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "https://github.com/example/my-app" in md

    def test_workflow_run(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "`wf-abc123`" in md
        assert "invoke" in md
        assert "completed" in md

    def test_steps_table(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "`parse_intent`" in md
        assert "`compile`" in md

    def test_retries(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "Retries / Errors" in md
        assert "TimeoutError" in md

    def test_decisions(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "Use SSR over SSG" in md

    def test_deploy_url(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "https://my-app.omnisight.dev" in md

    def test_smoke_test_results(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "health-check" in md
        assert "api-smoke" in md

    def test_open_findings(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "error_repeated" in md
        assert "software-alpha" in md

    def test_footer(self):
        report = _make_fixture_report()
        md = render_markdown(report)
        assert "OmniSight Report Engine v1.0.0" in md

    def test_golden_file_match(self):
        """The fixture report must match the golden file exactly."""
        report = _make_fixture_report()
        md = render_markdown(report)
        if not GOLDEN_FILE.exists():
            GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_FILE.write_text(md, encoding="utf-8")
            pytest.skip("Golden file created — run test again to validate")
        golden = GOLDEN_FILE.read_text(encoding="utf-8")
        assert md == golden, (
            "Report output differs from golden file. "
            "If the change is intentional, delete the golden file and re-run."
        )

    def test_empty_report(self):
        report = ReportData(
            report_id="rpt-empty",
            title="Empty Report",
            generated_at="2026-04-15T00:00:00+00:00",
        )
        md = render_markdown(report)
        assert "# Empty Report" in md
        assert "No outcome data available yet" in md


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests — helper functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFormatTimestamp:
    def test_unix_float(self):
        assert "2025-04-15" in _fmt_ts(1744675200.0)

    def test_none(self):
        assert _fmt_ts(None) == "—"

    def test_string_passthrough(self):
        assert _fmt_ts("2026-04-15T00:00:00") == "2026-04-15T00:00:00"


class TestSpecFieldRow:
    def test_high_confidence(self):
        row = _spec_field_row("framework", {"value": "nextjs", "confidence": 0.92})
        assert "nextjs" in row
        assert "92%" in row
        assert "✅" in row

    def test_low_confidence(self):
        row = _spec_field_row("persistence", {"value": "unknown", "confidence": 0.1})
        assert "❓" in row

    def test_medium_confidence(self):
        row = _spec_field_row("target_os", {"value": "linux", "confidence": 0.5})
        assert "⚠️" in row


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests — Signed URL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSignedUrl:
    def test_generate_and_verify(self):
        url = generate_signed_url("https://example.com", "rpt-123", expires_in=3600)
        assert "/report/share/rpt-123?" in url
        assert "expires=" in url
        assert "sig=" in url

        parts = url.split("?")[1]
        params = dict(p.split("=") for p in parts.split("&"))
        assert verify_signed_url("rpt-123", int(params["expires"]), params["sig"])

    def test_expired_url(self):
        assert not verify_signed_url("rpt-123", int(time.time()) - 100, "fakesig")

    def test_tampered_sig(self):
        url = generate_signed_url("https://example.com", "rpt-456", expires_in=3600)
        parts = url.split("?")[1]
        params = dict(p.split("=") for p in parts.split("&"))
        assert not verify_signed_url("rpt-456", int(params["expires"]), "tampered")

    def test_wrong_report_id(self):
        url = generate_signed_url("https://example.com", "rpt-789", expires_in=3600)
        parts = url.split("?")[1]
        params = dict(p.split("=") for p in parts.split("&"))
        assert not verify_signed_url("rpt-WRONG", int(params["expires"]), params["sig"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests — Section builders (mocked DB)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildSpecSection:
    @pytest.mark.asyncio
    async def test_with_explicit_spec(self):
        from backend.report_generator import build_spec_section

        mock_run = MagicMock()
        mock_run.metadata = {}

        with patch("backend.workflow.get_run", new_callable=AsyncMock, return_value=mock_run), \
             patch("backend.decision_engine.list_history", return_value=[]):
            section = await build_spec_section("wf-test", parsed_spec_dict=FIXTURE_SPEC)

        assert section.parsed_spec["framework"]["value"] == "nextjs"

    @pytest.mark.asyncio
    async def test_from_run_metadata(self):
        from backend.report_generator import build_spec_section

        mock_run = MagicMock()
        mock_run.metadata = {
            "parsed_spec": FIXTURE_SPEC,
            "input_sources": ["file://local"],
        }

        with patch("backend.workflow.get_run", new_callable=AsyncMock, return_value=mock_run), \
             patch("backend.decision_engine.list_history", return_value=[]):
            section = await build_spec_section("wf-test")

        assert section.parsed_spec["framework"]["value"] == "nextjs"
        assert "file://local" in section.input_sources


class TestBuildExecutionSection:
    @pytest.mark.asyncio
    async def test_steps_and_retries(self):
        from backend.report_generator import build_execution_section
        from backend.workflow import StepRecord, WorkflowRun

        mock_run = WorkflowRun(
            id="wf-exec", kind="invoke", started_at=1000.0,
            status="completed", completed_at=2000.0,
        )
        mock_steps = [
            StepRecord(id="s1", run_id="wf-exec", idempotency_key="step_a",
                       started_at=1000.0, completed_at=1100.0, output={"ok": True}),
            StepRecord(id="s2", run_id="wf-exec", idempotency_key="step_b",
                       started_at=1100.0, completed_at=1200.0, output=None,
                       error="ValueError: bad input"),
        ]

        with patch("backend.workflow.get_run", new_callable=AsyncMock, return_value=mock_run), \
             patch("backend.workflow.list_steps", new_callable=AsyncMock, return_value=mock_steps), \
             patch("backend.decision_engine.list_history", return_value=[]):
            section = await build_execution_section("wf-exec")

        assert section.workflow_run["status"] == "completed"
        assert len(section.steps) == 2
        assert len(section.retries) == 1
        assert "ValueError" in section.retries[0]["error"]


class TestBuildOutcomeSection:
    @pytest.mark.asyncio
    async def test_deploy_url_and_findings(self):
        from backend.report_generator import build_outcome_section
        from backend.workflow import StepRecord, WorkflowRun

        mock_run = WorkflowRun(
            id="wf-out", kind="invoke", started_at=1000.0,
            status="completed", metadata={"deploy_url": "https://app.test"},
        )
        mock_steps = [
            StepRecord(id="s1", run_id="wf-out", idempotency_key="smoke",
                       started_at=1000.0, completed_at=1100.0,
                       output={"smoke_test": {"label": "health", "status": "pass"}}),
        ]
        mock_findings = [
            {"id": "f1", "finding_type": "timeout", "severity": "warn",
             "content": "Slow query", "agent_id": "sw-a", "status": "open"},
        ]

        with patch("backend.workflow.get_run", new_callable=AsyncMock, return_value=mock_run), \
             patch("backend.workflow.list_steps", new_callable=AsyncMock, return_value=mock_steps), \
             patch("backend.db.list_debug_findings", new_callable=AsyncMock, return_value=mock_findings):
            section = await build_outcome_section("wf-out")

        assert section.deploy_url == "https://app.test"
        assert len(section.smoke_test_results) == 1
        assert section.smoke_test_results[0]["label"] == "health"
        assert len(section.open_findings) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests — PDF export error handling
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPdfExport:
    def test_missing_markdown_raises(self):
        from backend.report_generator import render_pdf

        with patch.dict("sys.modules", {"markdown": None}):
            with pytest.raises(ImportError, match="markdown"):
                render_pdf("# Hello")

    def test_missing_weasyprint_raises(self):
        from backend.report_generator import render_pdf

        with patch.dict("sys.modules", {"weasyprint": None}):
            with pytest.raises(ImportError, match="weasyprint"):
                render_pdf("# Hello")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests — Full generate_project_report
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGenerateProjectReport:
    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        from backend.report_generator import generate_project_report
        from backend.workflow import StepRecord, WorkflowRun

        mock_run = WorkflowRun(
            id="wf-full", kind="invoke", started_at=1000.0,
            status="completed", completed_at=2000.0,
            metadata={"parsed_spec": FIXTURE_SPEC, "deploy_url": "https://test.dev"},
        )

        with patch("backend.workflow.get_run", new_callable=AsyncMock, return_value=mock_run), \
             patch("backend.workflow.list_steps", new_callable=AsyncMock, return_value=[]), \
             patch("backend.decision_engine.list_history", return_value=[]), \
             patch("backend.db.list_debug_findings", new_callable=AsyncMock, return_value=[]):
            report = await generate_project_report("wf-full")

        assert report.report_id.startswith("rpt-")
        assert report.spec.parsed_spec["framework"]["value"] == "nextjs"
        assert report.outcome.deploy_url == "https://test.dev"

        md = render_markdown(report)
        assert "## 1. Specification" in md
        assert "## 2. Execution" in md
        assert "## 3. Outcome" in md
