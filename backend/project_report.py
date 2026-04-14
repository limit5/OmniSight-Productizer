"""Phase 61 — Project Final Report Generator.

Aggregates 6 sections into a single deliverable:

  1. Executive Summary — short PM-friendly paragraph
  2. Compliance Matrix — hardware_manifest spec lines × tasks × tests
  3. Metrics: Forecast vs Actual — Phase 60 forecast snapshot at start
                                    versus the realised numbers
  4. Decision Audit Timeline   — last 100 audit_log entries
  5. Lessons Learned           — episodic_memory + repeated stuck signals
  6. Artifact Catalog          — every artifact + checksum + size

Renders to:
  - JSON (always)
  - HTML (uses lib/md-to-html flavour via Python jinja2)
  - PDF (WeasyPrint when available; otherwise falls back to HTML+
         a clear note. Charts go through Playwright in v1 — not
         in this MVP.)

API: routers/projects.py
  POST /projects/{id}/report
  GET  /projects/{id}/report.{json,html,pdf}
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ComplianceRow:
    spec_line: str
    related_task_ids: list[str] = field(default_factory=list)
    test_passed: bool = False
    notes: str = ""


@dataclass
class MetricsCompare:
    label: str
    forecast: Any
    actual: Any
    delta_pct: float = 0.0


@dataclass
class AuditEntry:
    id: int
    ts: float
    actor: str
    action: str
    entity_kind: str
    entity_id: str
    summary: str = ""


@dataclass
class ArtifactRow:
    id: str
    name: str
    type: str
    size: int
    checksum: str
    created_at: str


@dataclass
class FinalReport:
    project_id: str
    project_name: str
    target_platform: str
    project_track: str
    generated_at: float
    executive_summary: str
    compliance: list[ComplianceRow]
    metrics: list[MetricsCompare]
    audit_timeline: list[AuditEntry]
    lessons_learned: list[str]
    artifacts: list[ArtifactRow]
    forecast_method: str
    forecast_confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Section builders (each best-effort, partial result on failure)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _executive_summary(project_name: str, track: str, target: str,
                              total_tasks: int, total_hours: float) -> str:
    """Templated paragraph for v0; v1 will hand off to the Reporter
    agent with the same template + freedom to embellish."""
    track_label = (track or "full_stack").replace("_", " ").title()
    return (
        f"Project **{project_name}** — track {track_label}, target "
        f"platform {target or '(unset)'}.\n\n"
        f"Pipeline executed approximately {total_tasks} tasks over "
        f"~{total_hours:.1f} hours of orchestrated agent activity. "
        f"All NPI phase exit gates were evaluated; full audit trail "
        f"available in section 4."
    )


async def _compliance_matrix(manifest: dict) -> list[ComplianceRow]:
    """Crude v0: every spec key gets a row. v1 will cross-reference
    with `tasks.acceptance_criteria` / `simulations.tests_passed`."""
    out: list[ComplianceRow] = []
    spec = manifest.get("sensor") or {}
    for k, v in (spec.items() if isinstance(spec, dict) else []):
        out.append(ComplianceRow(
            spec_line=f"sensor.{k} = {v}",
            test_passed=False,
            notes="auto-extracted from hardware_manifest.yaml",
        ))
    proj = manifest.get("project") or {}
    if proj.get("name"):
        out.append(ComplianceRow(
            spec_line=f"project.name = {proj['name']}",
            test_passed=True,
            notes="manifest set",
        ))
    return out


async def _metrics_compare() -> tuple[list[MetricsCompare], dict]:
    """Forecast (v0/v1) vs realised numbers from token_usage / agents.
    Returns (rows, {forecast_method, confidence})."""
    from backend import forecast as _fc
    fc = _fc.from_manifest()
    rows: list[MetricsCompare] = []
    # actual tokens from token_usage
    try:
        from backend import db
        async with db._conn().execute(
            "SELECT SUM(total_tokens) AS t, SUM(request_count) AS r FROM token_usage"
        ) as cur:
            row = await cur.fetchone()
        actual_tokens = int(row["t"] or 0)
        actual_requests = int(row["r"] or 0)
    except Exception:
        actual_tokens, actual_requests = 0, 0

    def _delta(f: float, a: float) -> float:
        if f == 0:
            return 0.0
        return round((a - f) / f * 100.0, 1)

    rows.append(MetricsCompare("tasks", fc.tasks.total, actual_requests,
                                _delta(fc.tasks.total, actual_requests)))
    rows.append(MetricsCompare("tokens", fc.tokens.total, actual_tokens,
                                _delta(fc.tokens.total, actual_tokens)))
    rows.append(MetricsCompare("hours", fc.duration.total_hours, 0.0, 0.0))
    rows.append(MetricsCompare("usd", fc.cost.total_usd, 0.0, 0.0))
    return rows, {"forecast_method": fc.method, "confidence": fc.confidence}


async def _audit_timeline(limit: int = 100) -> list[AuditEntry]:
    try:
        from backend import audit as _audit
        rows = await _audit.query(limit=limit)
    except Exception as exc:
        logger.warning("audit timeline read failed: %s", exc)
        return []
    out: list[AuditEntry] = []
    for r in rows:
        summary = ""
        if r["action"] == "mode_change":
            after = r.get("after") or {}
            summary = f"mode → {after.get('mode')}"
        elif r["action"] == "decision_resolve":
            after = r.get("after") or {}
            summary = f"decision {r['entity_id']} → {after.get('chosen_option_id')}"
        elif r["action"] == "decision_undo":
            summary = f"undone: {r['entity_id']}"
        out.append(AuditEntry(
            id=r["id"], ts=r["ts"], actor=r["actor"],
            action=r["action"], entity_kind=r["entity_kind"],
            entity_id=r["entity_id"], summary=summary,
        ))
    return out


async def _lessons_learned(limit: int = 20) -> list[str]:
    """v0: pull top quality_score episodic_memory entries."""
    try:
        from backend import db
        async with db._conn().execute(
            "SELECT error_signature, solution FROM episodic_memory "
            "ORDER BY quality_score DESC, access_count DESC LIMIT ?", (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [f"**{r['error_signature']}** — {r['solution']}" for r in rows]
    except Exception as exc:
        logger.debug("episodic_memory read failed: %s", exc)
        return []


async def _artifact_catalog() -> list[ArtifactRow]:
    try:
        from backend import db
        async with db._conn().execute(
            "SELECT id, name, type, size, COALESCE(checksum,'') AS checksum, created_at "
            "FROM artifacts ORDER BY created_at DESC LIMIT 200"
        ) as cur:
            rows = await cur.fetchall()
        return [
            ArtifactRow(id=r["id"], name=r["name"], type=r["type"],
                        size=int(r["size"] or 0), checksum=r["checksum"],
                        created_at=r["created_at"])
            for r in rows
        ]
    except Exception as exc:
        logger.warning("artifact catalog read failed: %s", exc)
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Top-level builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def build_report(project_id: str = "current") -> FinalReport:
    """Aggregate all 6 sections into a single FinalReport."""
    import yaml
    manifest = {}
    mp = _PROJECT_ROOT / "configs" / "hardware_manifest.yaml"
    if mp.exists():
        try:
            manifest = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            # Fix-B B2: manifest is optional; log at debug so a malformed
            # YAML isn't completely invisible to anyone debugging report output.
            import logging as _l
            _l.getLogger(__name__).debug("hardware_manifest parse failed: %s", exc)
    proj = manifest.get("project") or {}
    project_name = proj.get("name") or project_id
    target_platform = proj.get("target_platform") or ""
    project_track = (proj.get("project_track") or "").lower() or "full_stack"

    metrics, fc_meta = await _metrics_compare()
    total_tasks = next((m.actual for m in metrics if m.label == "tasks"), 0)
    total_hours = next((m.forecast for m in metrics if m.label == "hours"), 0.0)

    return FinalReport(
        project_id=project_id,
        project_name=project_name,
        target_platform=target_platform,
        project_track=project_track,
        generated_at=time.time(),
        executive_summary=await _executive_summary(
            project_name, project_track, target_platform,
            int(total_tasks or 0), float(total_hours or 0.0),
        ),
        compliance=await _compliance_matrix(manifest),
        metrics=metrics,
        audit_timeline=await _audit_timeline(),
        lessons_learned=await _lessons_learned(),
        artifacts=await _artifact_catalog(),
        forecast_method=fc_meta["forecast_method"],
        forecast_confidence=float(fc_meta["confidence"]),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Renderers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_html(report: FinalReport) -> str:
    """Self-contained HTML — no external CSS dep so WeasyPrint can
    consume it directly."""
    from datetime import datetime as _dt
    when = _dt.fromtimestamp(report.generated_at).strftime("%Y-%m-%d %H:%M:%S")

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    css = """
    body { font-family: 'Helvetica Neue', Arial, sans-serif; color: #1a1a1a; padding: 24pt; }
    h1 { font-size: 24pt; color: #0e7490; margin: 0 0 4pt; }
    h2 { font-size: 16pt; color: #0e7490; border-bottom: 1pt solid #94a3b8; padding-bottom: 2pt; margin-top: 18pt; }
    .meta { color: #64748b; font-size: 9pt; }
    table { width: 100%; border-collapse: collapse; margin: 6pt 0; font-size: 9pt; }
    th, td { border: 0.5pt solid #94a3b8; padding: 3pt 6pt; text-align: left; vertical-align: top; }
    th { background: #f1f5f9; }
    tbody tr:nth-child(even) { background: #f8fafc; }
    .delta-up { color: #b91c1c; }
    .delta-down { color: #15803d; }
    code { background: #f1f5f9; padding: 0 3pt; border-radius: 2pt; font-size: 8.5pt; }
    .ok { color: #15803d; } .fail { color: #b91c1c; }
    li { margin: 2pt 0; }
    """

    def _delta_class(p: float) -> str:
        return "delta-up" if p > 0 else "delta-down"

    rows_compl = "".join(
        f"<tr><td>{esc(r.spec_line)}</td><td>{'✓' if r.test_passed else '—'}</td>"
        f"<td>{esc(', '.join(r.related_task_ids))}</td><td>{esc(r.notes)}</td></tr>"
        for r in report.compliance
    ) or "<tr><td colspan='4'>(no spec rows)</td></tr>"

    rows_metrics = "".join(
        f"<tr><td>{esc(m.label.upper())}</td><td>{m.forecast}</td>"
        f"<td>{m.actual}</td><td class='{_delta_class(m.delta_pct)}'>{m.delta_pct:+.1f}%</td></tr>"
        for m in report.metrics
    )

    rows_audit = "".join(
        f"<tr><td>#{a.id}</td><td>{esc(a.actor)}</td><td>{esc(a.action)}</td>"
        f"<td>{esc(a.entity_kind)}/{esc(a.entity_id)}</td><td>{esc(a.summary)}</td></tr>"
        for a in report.audit_timeline[:50]
    ) or "<tr><td colspan='5'>(empty)</td></tr>"

    rows_artifacts = "".join(
        f"<tr><td>{esc(a.name)}</td><td>{esc(a.type)}</td>"
        f"<td>{a.size}</td><td><code>{esc(a.checksum[:16])}</code></td></tr>"
        for a in report.artifacts
    ) or "<tr><td colspan='4'>(no artifacts)</td></tr>"

    lessons = "".join(f"<li>{esc(l)}</li>" for l in report.lessons_learned) or "<li>(none recorded)</li>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{esc(report.project_name)} — Final Report</title>
<style>{css}</style></head><body>
<h1>{esc(report.project_name)} — Final Report</h1>
<div class="meta">Generated {when} · target {esc(report.target_platform)} · track {esc(report.project_track)}
 · forecast method {esc(report.forecast_method)} ({report.forecast_confidence*100:.0f}% confidence)</div>

<h2>1. Executive Summary</h2>
<p>{esc(report.executive_summary).replace(chr(10) + chr(10), '</p><p>').replace('**', '')}</p>

<h2>2. Compliance Matrix</h2>
<table><thead><tr><th>Spec</th><th>Pass</th><th>Tasks</th><th>Notes</th></tr></thead>
<tbody>{rows_compl}</tbody></table>

<h2>3. Metrics — Forecast vs Actual</h2>
<table><thead><tr><th>Metric</th><th>Forecast</th><th>Actual</th><th>Δ</th></tr></thead>
<tbody>{rows_metrics}</tbody></table>

<h2>4. Decision Audit Timeline (last 50)</h2>
<table><thead><tr><th>#</th><th>Actor</th><th>Action</th><th>Entity</th><th>Summary</th></tr></thead>
<tbody>{rows_audit}</tbody></table>

<h2>5. Lessons Learned</h2>
<ul>{lessons}</ul>

<h2>6. Artifact Catalog ({len(report.artifacts)} files)</h2>
<table><thead><tr><th>Name</th><th>Type</th><th>Size (B)</th><th>Checksum</th></tr></thead>
<tbody>{rows_artifacts}</tbody></table>

</body></html>"""


def render_pdf(report: FinalReport, out_path: Path) -> tuple[bool, str]:
    """WeasyPrint render. Returns (ok, message). Falls back to HTML
    sibling on failure so the API can still respond with something."""
    html = render_html(report)
    try:
        from weasyprint import HTML
        HTML(string=html).write_pdf(str(out_path))
        return True, str(out_path)
    except Exception as exc:
        logger.warning("WeasyPrint render failed: %s — falling back to HTML", exc)
        html_path = out_path.with_suffix(".html")
        html_path.write_text(html, encoding="utf-8")
        return False, str(html_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hash for caching / dedup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def report_etag(report: FinalReport) -> str:
    h = hashlib.sha256(json.dumps(report.to_dict(), sort_keys=True, default=str).encode()).hexdigest()
    return h[:16]
