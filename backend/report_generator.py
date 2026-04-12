"""Report generation engine using Jinja2 templates.

Generates markdown reports from templates in ``configs/templates/``.
Artifacts are stored in ``.artifacts/{task_id}/`` and tracked in the DB.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

import jinja2

from backend import db
from backend.routers.artifacts import get_artifacts_root

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "configs" / "templates"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=jinja2.Undefined,
    autoescape=False,
)


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
    """Render a Jinja2 template to a markdown file and register as artifact.

    Args:
        template_name: Template filename (without .md.j2) e.g. "compliance_report"
        context: Template variables dict
        output_name: Output filename (auto-generated if empty)
        task_id: Associated task ID
        agent_id: Associated agent ID

    Returns:
        Artifact metadata dict with id, name, file_path, size.
    """
    # Find template
    template_file = f"{template_name}.md.j2"
    try:
        template = _jinja_env.get_template(template_file)
    except jinja2.TemplateNotFound:
        return {"error": f"Template not found: {template_file}. Available: {list_templates()}"}

    # Inject defaults
    context.setdefault("date", datetime.now().strftime("%Y-%m-%d %H:%M"))
    context.setdefault("project_name", "OmniSight")

    # Render
    try:
        content = template.render(**context)
    except Exception as exc:
        return {"error": f"Template render failed: {exc}"}

    # Write to artifact storage
    artifact_id = f"art-{uuid.uuid4().hex[:8]}"
    name = output_name or f"{template_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

    task_dir = get_artifacts_root() / (task_id or "general")
    task_dir.mkdir(parents=True, exist_ok=True)
    file_path = task_dir / name
    file_path.write_text(content, encoding="utf-8")

    size = file_path.stat().st_size

    # Register in DB
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

    # Emit event
    try:
        from backend.events import bus
        bus.publish("artifact_created", {
            "id": artifact_id,
            "name": name,
            "type": "markdown",
            "task_id": task_id,
            "agent_id": agent_id,
            "size": size,
        })
    except Exception:
        pass

    logger.info("Artifact generated: %s (%d bytes) → %s", name, size, file_path)
    return artifact_data
