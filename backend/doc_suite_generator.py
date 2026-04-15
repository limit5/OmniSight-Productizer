"""L4-CORE-06 — Document suite generator (#215).

Extends REPORT-01 (``report_generator.py``) with per-product-class document
templates.  Each ``ProjectClass`` maps to a tailored set of documents drawn
from seven base templates:

  datasheet / user_manual / compliance / api_doc / sbom / eula / security

Compliance-cert fields are merged from L4-CORE-09 (safety), L4-CORE-10
(radio), and L4-CORE-18 (payment/PCI) when those modules are available.
PDF export reuses the WeasyPrint pipeline from ``report_generator``.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jinja2

from backend.hardware_profile import HardwareProfile
from backend.models import ProjectClass

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "configs" / "templates"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=jinja2.ChainableUndefined,
    autoescape=False,
)

SUITE_VERSION = "1.0.0"

ALL_TEMPLATE_NAMES = (
    "datasheet",
    "user_manual",
    "compliance",
    "api_doc",
    "sbom",
    "eula",
    "security",
)

PRODUCT_CLASS_TEMPLATES: dict[str, tuple[str, ...]] = {
    ProjectClass.embedded_product.value: (
        "datasheet", "user_manual", "compliance", "api_doc",
        "sbom", "eula", "security",
    ),
    ProjectClass.algo_sim.value: (
        "api_doc", "user_manual", "sbom", "eula",
    ),
    ProjectClass.optical_sim.value: (
        "api_doc", "user_manual", "sbom", "eula",
    ),
    ProjectClass.iso_standard.value: (
        "compliance", "api_doc", "user_manual", "sbom", "eula", "security",
    ),
    ProjectClass.test_tool.value: (
        "api_doc", "user_manual", "sbom", "eula",
    ),
    ProjectClass.factory_tool.value: (
        "datasheet", "user_manual", "compliance", "api_doc",
        "sbom", "eula", "security",
    ),
    ProjectClass.enterprise_web.value: (
        "api_doc", "user_manual", "sbom", "eula", "security",
    ),
}


def _template_filename(name: str) -> str:
    if name == "sbom":
        return "sbom.json.j2"
    if name == "compliance":
        return "compliance_report.md.j2"
    return f"{name}.md.j2"


def templates_for_class(project_class: str) -> tuple[str, ...]:
    return PRODUCT_CLASS_TEMPLATES.get(
        project_class,
        PRODUCT_CLASS_TEMPLATES[ProjectClass.embedded_product.value],
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Compliance-cert field merging (CORE-09/10/18 stubs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ComplianceCert:
    standard: str
    status: str = "Pending"
    cert_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _try_safety_certs() -> list[ComplianceCert]:
    """Merge from L4-CORE-09 safety & compliance framework (when available)."""
    try:
        from backend.safety_compliance import get_safety_certs  # type: ignore[import-not-found]
        return [ComplianceCert(**c) for c in get_safety_certs()]
    except (ImportError, Exception):
        return []


def _try_radio_certs() -> list[ComplianceCert]:
    """Merge from L4-CORE-10 radio certification (when available)."""
    try:
        from backend.radio_compliance import get_radio_certs  # type: ignore[import-not-found]
        return [ComplianceCert(**c) for c in get_radio_certs()]
    except (ImportError, Exception):
        return []


def _try_payment_certs() -> list[ComplianceCert]:
    """Merge from L4-CORE-18 payment/PCI compliance (when available)."""
    try:
        from backend.payment_compliance import get_payment_certs  # type: ignore[import-not-found]
        return [ComplianceCert(**c) for c in get_payment_certs()]
    except (ImportError, Exception):
        return []


def _try_rt_certs() -> list[ComplianceCert]:
    """Merge from L4-CORE-12 real-time / determinism track (when available)."""
    try:
        from backend.realtime_determinism import get_rt_certs  # type: ignore[import-not-found]
        return [ComplianceCert(**c) for c in get_rt_certs()]
    except (ImportError, Exception):
        return []


def _try_connectivity_certs() -> list[ComplianceCert]:
    """Merge from L4-CORE-13 connectivity sub-skill library (when available)."""
    try:
        from backend.connectivity import get_connectivity_certs  # type: ignore[import-not-found]
        return [ComplianceCert(**c) for c in get_connectivity_certs()]
    except (ImportError, Exception):
        return []


def _try_sensor_fusion_certs() -> list[ComplianceCert]:
    """Merge from L4-CORE-14 sensor fusion library (when available)."""
    try:
        from backend.sensor_fusion import get_sensor_fusion_certs  # type: ignore[import-not-found]
        return [ComplianceCert(**c) for c in get_sensor_fusion_certs()]
    except (ImportError, Exception):
        return []


def collect_compliance_certs(
    extra: list[dict[str, Any]] | None = None,
) -> list[ComplianceCert]:
    certs: list[ComplianceCert] = []
    certs.extend(_try_safety_certs())
    certs.extend(_try_radio_certs())
    certs.extend(_try_payment_certs())
    certs.extend(_try_rt_certs())
    certs.extend(_try_connectivity_certs())
    certs.extend(_try_sensor_fusion_certs())
    if extra:
        for c in extra:
            certs.append(ComplianceCert(
                standard=c.get("standard", "Unknown"),
                status=c.get("status", "Pending"),
                cert_id=c.get("cert_id", ""),
                details=c.get("details", {}),
            ))
    return certs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Document context builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class DocSuiteContext:
    product_name: str = "OmniSight Product"
    product_version: str = "1.0.0"
    product_description: str = ""
    project_class: str = ProjectClass.embedded_product.value
    date: str = ""
    hardware_profile: dict[str, Any] | None = None
    parsed_spec: dict[str, Any] | None = None
    compliance_certs: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.date:
            self.date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def to_template_vars(self) -> dict[str, Any]:
        certs = collect_compliance_certs(self.compliance_certs)
        cert_dicts = [
            {"standard": c.standard, "status": c.status, "cert_id": c.cert_id}
            for c in certs
        ]

        base: dict[str, Any] = {
            "product_name": self.product_name,
            "product_version": self.product_version,
            "product_description": self.product_description,
            "project_class": self.project_class,
            "date": self.date,
            "project_name": self.product_name,
            "compliance_certs": cert_dicts,
            "sbom_uuid": str(uuid.uuid4()),
            "timestamp": self.date,
        }

        if self.hardware_profile:
            base["hardware_profile"] = self.hardware_profile
            hw_spec = {}
            for k in ("soc", "mcu", "dsp", "npu", "display"):
                v = self.hardware_profile.get(k, "")
                if v:
                    hw_spec[k] = v
            for k in ("sensor", "codec", "usb"):
                vals = self.hardware_profile.get(k, [])
                if vals:
                    hw_spec[k] = ", ".join(vals)
            base["hardware_spec"] = hw_spec

        if self.parsed_spec:
            flat: dict[str, str] = {}
            for fname in (
                "project_type", "project_class", "runtime_model",
                "target_arch", "target_os", "framework",
                "persistence", "deploy_target",
            ):
                fval = self.parsed_spec.get(fname)
                if isinstance(fval, dict):
                    flat[fname] = fval.get("value", "unknown")
                elif isinstance(fval, str):
                    flat[fname] = fval
            base["parsed_spec"] = flat

        base.update(self.extra)
        return base


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Suite generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class GeneratedDoc:
    name: str
    template: str
    content: str
    format: str = "markdown"


def render_single(template_name: str, context: dict[str, Any]) -> GeneratedDoc:
    filename = _template_filename(template_name)
    try:
        tmpl = _jinja_env.get_template(filename)
    except jinja2.TemplateNotFound:
        raise FileNotFoundError(f"Template not found: {filename}")

    content = tmpl.render(**context)
    fmt = "json" if template_name == "sbom" else "markdown"
    return GeneratedDoc(
        name=template_name,
        template=filename,
        content=content,
        format=fmt,
    )


def generate_suite(
    ctx: DocSuiteContext,
    *,
    templates: tuple[str, ...] | None = None,
) -> list[GeneratedDoc]:
    if templates is None:
        templates = templates_for_class(ctx.project_class)

    template_vars = ctx.to_template_vars()
    docs: list[GeneratedDoc] = []

    for tname in templates:
        doc = render_single(tname, template_vars)
        docs.append(doc)

    logger.info(
        "Generated document suite: %d docs for class=%s",
        len(docs), ctx.project_class,
    )
    return docs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PDF export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_doc_pdf(doc: GeneratedDoc) -> bytes:
    """Convert a single generated doc to PDF. JSON docs get wrapped in <pre>."""
    from backend.report_generator import render_pdf

    if doc.format == "json":
        try:
            parsed = json.loads(doc.content)
            pretty = json.dumps(parsed, indent=2)
        except (json.JSONDecodeError, TypeError):
            pretty = doc.content
        md = f"# SBOM — Software Bill of Materials\n\n```json\n{pretty}\n```"
        return render_pdf(md)

    return render_pdf(doc.content)


def export_suite_to_dir(
    docs: list[GeneratedDoc],
    output_dir: Path,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    for doc in docs:
        ext = ".json" if doc.format == "json" else ".md"
        md_path = output_dir / f"{doc.name}{ext}"
        md_path.write_text(doc.content, encoding="utf-8")

        entry: dict[str, Any] = {
            "name": doc.name,
            "format": doc.format,
            "markdown_path": str(md_path),
        }

        try:
            pdf_bytes = render_doc_pdf(doc)
            pdf_path = output_dir / f"{doc.name}.pdf"
            pdf_path.write_bytes(pdf_bytes)
            entry["pdf_path"] = str(pdf_path)
            entry["pdf_size"] = len(pdf_bytes)
        except ImportError:
            entry["pdf_path"] = None
            entry["pdf_error"] = "weasyprint or markdown not installed"
        except Exception as exc:
            entry["pdf_path"] = None
            entry["pdf_error"] = str(exc)

        manifest.append(entry)

    return manifest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Convenience: from ParsedSpec + HardwareProfile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def from_parsed_spec(
    parsed_spec_dict: dict[str, Any],
    *,
    hardware_profile: HardwareProfile | None = None,
    product_name: str = "OmniSight Product",
    product_version: str = "1.0.0",
    extra: dict[str, Any] | None = None,
) -> DocSuiteContext:
    pc_field = parsed_spec_dict.get("project_class", {})
    if isinstance(pc_field, dict):
        project_class = pc_field.get("value", "embedded_product")
    else:
        project_class = str(pc_field) if pc_field else "embedded_product"

    if project_class == "unknown":
        project_class = "embedded_product"

    hw_dict = None
    if hardware_profile:
        hw_dict = hardware_profile.model_dump()
    elif parsed_spec_dict.get("hardware_profile"):
        hw_dict = parsed_spec_dict["hardware_profile"]

    return DocSuiteContext(
        product_name=product_name,
        product_version=product_version,
        project_class=project_class,
        hardware_profile=hw_dict,
        parsed_spec=parsed_spec_dict,
        extra=extra or {},
    )
