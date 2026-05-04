"""BP.I.3 -- SecOps intel preflight hook integration.

This module wires the BP.I.1 helpers and BP.I.2 guild scaffold into two
passive hook entry points:

* ``integration_engineer_pre_install_hook`` -- run before dependency or
  catalog-entry install decisions.
* ``architect_pre_blueprint_hook`` -- run before blueprint generation.

The hooks deliberately return a structured brief instead of mutating
installer or Architect state. The caller can persist the brief or feed it
into the next prompt/template without this module owning orchestration.

Module-global state audit (SOP Step 1, 2026-04-21 rule)
-------------------------------------------------------
Only immutable constants and template paths live at module scope. Each
hook call invokes BP.I.1 helpers with caller-injected clients when tests
need them. Cross-worker consistency is moot because no mutable
module-level cache, singleton, or in-memory registry is read or written.

Read-after-write audit (SOP Step 1, 2026-04-21 rule)
---------------------------------------------------
N/A -- these hooks perform outbound reads plus deterministic template
rendering and do not write to PG, Redis, filesystem state, or module
globals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import jinja2

from backend import secops_intel as intel


HookName = Literal[
    "integration_engineer_pre_install",
    "architect_pre_blueprint",
]

HOOK_STATUS_CLEAN = "clean"
HOOK_STATUS_FINDINGS = "findings"

_INTEL_GUILD_DIR = Path(__file__).resolve().parent.parent / "configs" / "guilds" / "intel"
_BRIEF_TEMPLATE_PATH = _INTEL_GUILD_DIR / "scaffolds" / "threat_intel_brief.md.j2"


@dataclass(frozen=True)
class IntelHookResult:
    """Structured output shared by the BP.I.3 preflight hooks."""

    hook: HookName
    guild: str = "intel"
    status: str = HOOK_STATUS_CLEAN
    product_name: str = ""
    query: str = ""
    blocking: bool = False
    recommended_action: str = ""
    reports: list[dict[str, Any]] = field(default_factory=list)
    brief: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _join_terms(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def _has_findings(reports: list[dict[str, Any]]) -> bool:
    return any(report.get("items") for report in reports)


def _render_brief(
    *,
    product_name: str,
    cve_query: str,
    zero_day_query: str,
    best_practice_topic: str,
    reports: list[dict[str, Any]],
    recommended_action: str,
    now: datetime | None,
) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_BRIEF_TEMPLATE_PATH.parent)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(_BRIEF_TEMPLATE_PATH.name)
    return template.render(
        product_name=product_name or "Unnamed Product",
        generation_date=_utc_stamp(now),
        cve_query=cve_query,
        zero_day_query=zero_day_query,
        best_practice_topic=best_practice_topic,
        reports=reports,
        recommended_action=recommended_action,
    )


def _result(
    *,
    hook: HookName,
    product_name: str,
    query: str,
    best_practice_topic: str,
    reports: list[dict[str, Any]],
    now: datetime | None,
) -> dict[str, Any]:
    has_findings = _has_findings(reports)
    status = HOOK_STATUS_FINDINGS if has_findings else HOOK_STATUS_CLEAN
    recommended_action = (
        "Record the Intel guild brief and route findings into the next "
        "installer or blueprint decision; BP.I.3 does not block automatically."
    )
    brief = _render_brief(
        product_name=product_name,
        cve_query=query,
        zero_day_query=query,
        best_practice_topic=best_practice_topic,
        reports=reports,
        recommended_action=recommended_action,
        now=now,
    )
    return IntelHookResult(
        hook=hook,
        status=status,
        product_name=product_name,
        query=query,
        blocking=False,
        recommended_action=recommended_action,
        reports=reports,
        brief=brief,
    ).to_dict()


def integration_engineer_pre_install_hook(
    *,
    product_name: str = "",
    install_targets: list[str] | tuple[str, ...] = (),
    limit: int = 5,
    client_factory: intel.HttpClientFactory | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the Intel guild preflight before install decisions.

    ``install_targets`` should contain dependency names, package names,
    catalog entry ids, or install methods that the Integration Engineer
    is about to introduce. The hook returns a passive brief; it does not
    enqueue/cancel installer jobs.
    """
    query = _join_terms([product_name, *list(install_targets)])
    best_practice_topic = _join_terms(["dependency install", product_name])
    reports = [
        intel.search_latest_cve(
            query,
            limit=limit,
            client_factory=client_factory,
            now=now,
        ),
        intel.query_zero_day_feeds(
            query,
            limit=limit,
            client_factory=client_factory,
            now=now,
        ),
        intel.fetch_latest_best_practices(
            best_practice_topic,
            limit=limit,
            now=now,
        ),
    ]
    return _result(
        hook="integration_engineer_pre_install",
        product_name=product_name,
        query=query,
        best_practice_topic=best_practice_topic,
        reports=reports,
        now=now,
    )


def architect_pre_blueprint_hook(
    *,
    product_name: str = "",
    blueprint_keywords: list[str] | tuple[str, ...] = (),
    limit: int = 5,
    client_factory: intel.HttpClientFactory | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the Intel guild preflight before blueprint generation.

    ``blueprint_keywords`` should carry platform, framework, SoC, and
    security-domain terms the Architect is considering. The hook returns
    source-backed inputs for the blueprint prompt/template.
    """
    query = _join_terms([product_name, *list(blueprint_keywords)])
    best_practice_topic = _join_terms(["secure architecture", product_name, *list(blueprint_keywords)])
    reports = [
        intel.search_latest_cve(
            query,
            limit=limit,
            client_factory=client_factory,
            now=now,
        ),
        intel.query_zero_day_feeds(
            query,
            limit=limit,
            client_factory=client_factory,
            now=now,
        ),
        intel.fetch_latest_best_practices(
            best_practice_topic,
            limit=limit,
            now=now,
        ),
    ]
    return _result(
        hook="architect_pre_blueprint",
        product_name=product_name,
        query=query,
        best_practice_topic=best_practice_topic,
        reports=reports,
        now=now,
    )


__all__ = [
    "HOOK_STATUS_CLEAN",
    "HOOK_STATUS_FINDINGS",
    "IntelHookResult",
    "architect_pre_blueprint_hook",
    "integration_engineer_pre_install_hook",
]
