"""Report generation engine — Jinja2 templates + structured project reports.

Two modes:
  1. **Template mode** (``generate_report``): renders a Jinja2 ``.md.j2``
     template from ``configs/templates/`` and registers the output as an
     artifact. Used by compliance/test-summary reports.
  2. **Project report mode** (``generate_project_report``): assembles a
     three-section structured report from a workflow run (B3/REPORT-01):
       - Section 1 (Spec): ParsedSpec + clarifications + input sources
       - Section 2 (Execution): workflow steps + decisions + retries
       - Section 3 (Outcome): deploy URL + smoke test + open findings

Optional PDF export via ``weasyprint`` + ``markdown``.
Signed-URL helper for time-limited read-only sharing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jinja2

from backend import db
from backend.routers.artifacts import get_artifacts_root

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "configs" / "templates"
_REPORT_VERSION = "1.0.0"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=jinja2.ChainableUndefined,
    autoescape=False,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Jinja2 template mode (pre-existing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def list_templates() -> list[str]:
    """List available report template names."""
    if not _TEMPLATES_DIR.is_dir():
        return []
    return sorted(f.stem.replace(".md", "") for f in _TEMPLATES_DIR.glob("*.md.j2"))


async def generate_report(
    template_name: str,
    context: dict,
    output_name: str = "",
    task_id: str = "",
    agent_id: str = "",
) -> dict:
    """Render a Jinja2 template to a markdown file and register as artifact."""
    template_file = f"{template_name}.md.j2"
    try:
        template = _jinja_env.get_template(template_file)
    except jinja2.TemplateNotFound:
        return {"error": f"Template not found: {template_file}. Available: {list_templates()}"}

    context.setdefault("date", datetime.now().strftime("%Y-%m-%d %H:%M"))
    context.setdefault("project_name", "OmniSight")

    try:
        content = template.render(**context)
    except Exception as exc:
        return {"error": f"Template render failed: {exc}"}

    artifact_id = f"art-{uuid.uuid4().hex[:8]}"
    name = output_name or f"{template_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

    task_dir = get_artifacts_root() / (task_id or "general")
    task_dir.mkdir(parents=True, exist_ok=True)
    file_path = task_dir / name
    file_path.write_text(content, encoding="utf-8")

    size = file_path.stat().st_size
    artifact_data = {
        "id": artifact_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "name": name,
        "type": "markdown",
        "file_path": str(file_path),
        "size": size,
        "created_at": datetime.now().isoformat(),
    }
    try:
        await db.insert_artifact(artifact_data)
    except Exception as exc:
        logger.warning("Failed to register artifact in DB: %s", exc)

    try:
        from backend.events import bus
        bus.publish("artifact_created", {
            "id": artifact_id, "name": name, "type": "markdown",
            "task_id": task_id, "agent_id": agent_id, "size": size,
        })
    except Exception:
        pass

    logger.info("Artifact generated: %s (%d bytes) → %s", name, size, file_path)
    return artifact_data


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  B3/REPORT-01 — Structured project report
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class SpecSection:
    """Section 1: what was requested."""
    parsed_spec: dict[str, Any] = field(default_factory=dict)
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    input_sources: list[str] = field(default_factory=list)


@dataclass
class ExecutionSection:
    """Section 2: how it was executed."""
    workflow_run: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class OutcomeSection:
    """Section 3: what was the result."""
    deploy_url: str = ""
    smoke_test_results: list[dict[str, Any]] = field(default_factory=list)
    open_findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReportData:
    """Aggregate container for all three report sections."""
    report_id: str = ""
    title: str = "OmniSight Project Report"
    generated_at: str = ""
    version: str = _REPORT_VERSION
    spec: SpecSection = field(default_factory=SpecSection)
    execution: ExecutionSection = field(default_factory=ExecutionSection)
    outcome: OutcomeSection = field(default_factory=OutcomeSection)


# ── Section builders (async — hit DB) ──


async def build_spec_section(
    run_id: str,
    parsed_spec_dict: dict[str, Any] | None = None,
) -> SpecSection:
    """Build Section 1 from a workflow run's metadata + decision history."""
    section = SpecSection()

    from backend import workflow as _wf
    run = await _wf.get_run(run_id)

    if parsed_spec_dict:
        section.parsed_spec = parsed_spec_dict
    elif run and run.metadata.get("parsed_spec"):
        section.parsed_spec = run.metadata["parsed_spec"]

    if run and run.metadata.get("input_sources"):
        section.input_sources = run.metadata["input_sources"]
    elif run and run.metadata.get("repo_url"):
        section.input_sources = [run.metadata["repo_url"]]

    from backend import decision_engine as _de
    for d in _de.list_history(limit=200):
        if d.source.get("run_id") == run_id:
            section.clarifications.append({
                "id": d.id,
                "title": d.title,
                "chosen": d.chosen_option_id,
                "resolver": d.resolver,
                "severity": d.severity.value if hasattr(d.severity, "value") else str(d.severity),
            })

    return section


async def build_execution_section(run_id: str) -> ExecutionSection:
    """Build Section 2 from workflow steps + decisions."""
    section = ExecutionSection()

    from backend import workflow as _wf
    run = await _wf.get_run(run_id)
    if not run:
        return section

    section.workflow_run = {
        "id": run.id,
        "kind": run.kind,
        "status": run.status,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "metadata": run.metadata,
    }

    steps = await _wf.list_steps(run_id)
    for s in steps:
        step_dict: dict[str, Any] = {
            "id": s.id,
            "key": s.idempotency_key,
            "started_at": s.started_at,
            "completed_at": s.completed_at,
            "is_done": s.is_done,
            "error": s.error,
        }
        if s.error:
            section.retries.append(step_dict)
        section.steps.append(step_dict)

    from backend import decision_engine as _de
    for d in _de.list_history(limit=200):
        if d.source.get("run_id") == run_id:
            section.decisions.append(d.to_dict())

    return section


async def build_outcome_section(run_id: str) -> OutcomeSection:
    """Build Section 3 from deploy metadata + debug findings."""
    section = OutcomeSection()

    from backend import workflow as _wf
    run = await _wf.get_run(run_id)
    if run and run.metadata.get("deploy_url"):
        section.deploy_url = run.metadata["deploy_url"]

    findings = await db.list_debug_findings(status="open", limit=100)
    for f in findings:
        section.open_findings.append({
            "id": f.get("id", ""),
            "finding_type": f.get("finding_type", ""),
            "severity": f.get("severity", "info"),
            "content": f.get("content", ""),
            "agent_id": f.get("agent_id", ""),
            "status": f.get("status", "open"),
        })

    steps = await _wf.list_steps(run_id)
    for s in steps:
        if s.output and isinstance(s.output, dict) and s.output.get("smoke_test"):
            section.smoke_test_results.append(s.output["smoke_test"])

    return section


# ── Full report assembly ──


async def generate_project_report(
    run_id: str,
    *,
    title: str = "OmniSight Project Report",
    parsed_spec_dict: dict[str, Any] | None = None,
) -> ReportData:
    """Assemble a full ReportData from a workflow run."""
    report = ReportData(
        report_id=f"rpt-{uuid.uuid4().hex[:10]}",
        title=title,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    report.spec = await build_spec_section(run_id, parsed_spec_dict)
    report.execution = await build_execution_section(run_id)
    report.outcome = await build_outcome_section(run_id)
    return report


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Markdown renderer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _fmt_ts(ts: float | str | None) -> str:
    if ts is None:
        return "—"
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts)


def _spec_field_row(name: str, field_dict: dict[str, Any]) -> str:
    val = field_dict.get("value", "unknown")
    conf = field_dict.get("confidence", 0)
    indicator = "✅" if conf >= 0.7 else "⚠️" if conf >= 0.3 else "❓"
    return f"| {name} | {val} | {conf:.0%} | {indicator} |"


def render_markdown(report: ReportData) -> str:
    """Render a ReportData into a Markdown document."""
    lines: list[str] = []
    lines.append(f"# {report.title}")
    lines.append("")
    lines.append(f"> Report ID: `{report.report_id}`")
    lines.append(f"> Generated: {report.generated_at}")
    lines.append(f"> Version: {report.version}")
    lines.append("")

    # ── Section 1: Spec ──
    lines.append("## 1. Specification")
    lines.append("")
    spec = report.spec
    if spec.parsed_spec:
        lines.append("### Parsed Spec Fields")
        lines.append("")
        lines.append("| Field | Value | Confidence | Status |")
        lines.append("|-------|-------|------------|--------|")
        for fname in (
            "project_type", "runtime_model", "target_arch", "target_os",
            "framework", "persistence", "deploy_target", "hardware_required",
        ):
            if fname in spec.parsed_spec:
                lines.append(_spec_field_row(fname, spec.parsed_spec[fname]))
        lines.append("")
        if spec.parsed_spec.get("raw_text"):
            lines.append("### Raw Input")
            lines.append("")
            lines.append(f"```\n{spec.parsed_spec['raw_text']}\n```")
            lines.append("")
        if spec.parsed_spec.get("conflicts"):
            lines.append("### Conflicts Detected")
            lines.append("")
            for c in spec.parsed_spec["conflicts"]:
                lines.append(f"- **{c.get('id', '?')}**: {c.get('message', '')}")
            lines.append("")
    if spec.input_sources:
        lines.append("### Input Sources")
        lines.append("")
        for src in spec.input_sources:
            lines.append(f"- {src}")
        lines.append("")
    if spec.clarifications:
        lines.append("### Clarifications")
        lines.append("")
        for cl in spec.clarifications:
            lines.append(f"- **{cl.get('title', '?')}** → chosen: `{cl.get('chosen', '?')}` (by {cl.get('resolver', '?')})")
        lines.append("")

    # ── Section 2: Execution ──
    lines.append("## 2. Execution")
    lines.append("")
    ex = report.execution
    wr = ex.workflow_run
    if wr:
        lines.append("### Workflow Run")
        lines.append("")
        lines.append(f"- **ID**: `{wr.get('id', '?')}`")
        lines.append(f"- **Kind**: {wr.get('kind', '?')}")
        lines.append(f"- **Status**: {wr.get('status', '?')}")
        lines.append(f"- **Started**: {_fmt_ts(wr.get('started_at'))}")
        lines.append(f"- **Completed**: {_fmt_ts(wr.get('completed_at'))}")
        lines.append("")
    if ex.steps:
        lines.append("### Steps")
        lines.append("")
        lines.append("| # | Key | Started | Completed | Status |")
        lines.append("|---|-----|---------|-----------|--------|")
        for i, s in enumerate(ex.steps, 1):
            status = "✅" if s.get("is_done") else ("❌" if s.get("error") else "⏳")
            lines.append(
                f"| {i} | `{s.get('key', '?')}` "
                f"| {_fmt_ts(s.get('started_at'))} "
                f"| {_fmt_ts(s.get('completed_at'))} "
                f"| {status} |"
            )
        lines.append("")
    if ex.retries:
        lines.append("### Retries / Errors")
        lines.append("")
        for r in ex.retries:
            lines.append(f"- `{r.get('key', '?')}`: {r.get('error', 'unknown error')}")
        lines.append("")
    if ex.decisions:
        lines.append("### Decisions")
        lines.append("")
        for d in ex.decisions:
            lines.append(f"- **{d.get('title', '?')}** [{d.get('severity', '?')}] → {d.get('status', '?')}")
        lines.append("")

    # ── Section 3: Outcome ──
    lines.append("## 3. Outcome")
    lines.append("")
    out = report.outcome
    if out.deploy_url:
        lines.append(f"### Deploy URL\n\n{out.deploy_url}\n")
    if out.smoke_test_results:
        lines.append("### Smoke Test Results")
        lines.append("")
        for st in out.smoke_test_results:
            status = st.get("status", "unknown")
            label = st.get("label", st.get("dag_id", "test"))
            lines.append(f"- **{label}**: {status}")
        lines.append("")
    if out.open_findings:
        lines.append("### Open Debug Findings")
        lines.append("")
        lines.append("| ID | Type | Severity | Agent | Content |")
        lines.append("|----|------|----------|-------|---------|")
        for f in out.open_findings:
            lines.append(
                f"| `{f.get('id', '?')[:12]}` "
                f"| {f.get('finding_type', '?')} "
                f"| {f.get('severity', '?')} "
                f"| {f.get('agent_id', '?')} "
                f"| {f.get('content', '')[:60]} |"
            )
        lines.append("")
    elif not out.smoke_test_results and not out.deploy_url:
        lines.append("*No outcome data available yet.*\n")

    lines.append("---")
    lines.append(f"*Generated by OmniSight Report Engine v{report.version}*")
    lines.append("")

    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PDF export (optional — requires weasyprint + markdown)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_pdf(markdown_text: str) -> bytes:
    """Convert Markdown → HTML → PDF. Raises ImportError if deps missing."""
    try:
        import markdown as _md
    except ImportError:
        raise ImportError("'markdown' package required for PDF export: pip install markdown")
    try:
        from weasyprint import HTML
    except ImportError:
        raise ImportError("'weasyprint' package required for PDF export: pip install weasyprint")

    html_body = _md.markdown(markdown_text, extensions=["tables", "fenced_code"])
    html_doc = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<style>"
        "body{font-family:system-ui,sans-serif;margin:2cm;font-size:11pt;line-height:1.5}"
        "table{border-collapse:collapse;width:100%;margin:1em 0}"
        "th,td{border:1px solid #ccc;padding:6px 10px;text-align:left}"
        "th{background:#f5f5f5}"
        "code{background:#f0f0f0;padding:2px 4px;border-radius:3px;font-size:.9em}"
        "pre{background:#f0f0f0;padding:1em;border-radius:4px;overflow-x:auto}"
        "blockquote{border-left:3px solid #ccc;margin-left:0;padding-left:1em;color:#555}"
        "h1{color:#1a1a1a;border-bottom:2px solid #333;padding-bottom:.3em}"
        "h2{color:#2a2a2a;border-bottom:1px solid #ddd;padding-bottom:.2em}"
        "</style></head><body>"
        f"{html_body}</body></html>"
    )
    return HTML(string=html_doc).write_pdf()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signed URL helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _get_signing_secret() -> str:
    import os
    from backend.config import settings
    secret = getattr(settings, "report_signing_secret", "") or ""
    if not secret:
        secret = os.environ.get("OMNISIGHT_REPORT_SECRET", "omnisight-report-default-secret")
    return secret


def generate_signed_url(
    base_url: str,
    report_id: str,
    *,
    expires_in: int = 86400,
) -> str:
    """Create a time-limited signed URL for read-only report access."""
    expires_at = int(time.time()) + expires_in
    payload = f"{report_id}:{expires_at}"
    sig = hmac.new(
        _get_signing_secret().encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{base_url}/report/share/{report_id}?expires={expires_at}&sig={sig}"


def verify_signed_url(report_id: str, expires: int, sig: str) -> bool:
    """Verify a signed URL token. Returns False if expired or tampered."""
    if time.time() > expires:
        return False
    payload = f"{report_id}:{expires}"
    expected = hmac.new(
        _get_signing_secret().encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)
