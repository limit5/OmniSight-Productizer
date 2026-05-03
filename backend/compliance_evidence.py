"""SC.11.2-SC.11.3 -- compliance evidence mapping and collection helpers.

This module deliberately owns only the SOC 2 and ISO 27001 mapping rows.
Zip/signature export remains for SC.11.4.

Module-global / cross-worker state audit: the compliance mappings are
immutable tuple data and every worker derives the same evidence plan from
the same repository files plus PG rows.  No singleton cache or mutable
in-memory state is used.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOC2_MAPPING_VERSION = "2026-05-03.sc11.2"
ISO27001_MAPPING_VERSION = "2026-05-03.sc11.3"


@dataclass(frozen=True)
class SOC2Control:
    control_id: str
    trust_service_category: str
    title: str
    description: str
    policy_paths: tuple[str, ...] = ()
    audit_entity_kinds: tuple[str, ...] = ()
    audit_actions: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()

    def to_mapping_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "trust_service_category": self.trust_service_category,
            "title": self.title,
            "description": self.description,
            "evidence_sources": {
                "policy_paths": list(self.policy_paths),
                "audit_entity_kinds": list(self.audit_entity_kinds),
                "audit_actions": list(self.audit_actions),
                "event_types": list(self.event_types),
            },
        }


@dataclass(frozen=True)
class ISO27001Control:
    control_id: str
    domain: str
    title: str
    description: str
    policy_paths: tuple[str, ...] = ()
    audit_entity_kinds: tuple[str, ...] = ()
    audit_actions: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()

    def to_mapping_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "domain": self.domain,
            "title": self.title,
            "description": self.description,
            "evidence_sources": {
                "policy_paths": list(self.policy_paths),
                "audit_entity_kinds": list(self.audit_entity_kinds),
                "audit_actions": list(self.audit_actions),
                "event_types": list(self.event_types),
            },
        }


@dataclass
class EvidencePointer:
    source_id: str
    source_type: str
    description: str
    available: bool = False
    path: str = ""
    sha256: str = ""
    bytes: int = 0
    table: str = ""
    row_count: int = 0
    latest_id: int = 0
    latest_at: str | float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ControlEvidence:
    control_id: str
    title: str
    policy_evidence: list[EvidencePointer] = field(default_factory=list)
    log_evidence: list[EvidencePointer] = field(default_factory=list)

    @property
    def has_policy(self) -> bool:
        return any(item.available for item in self.policy_evidence)

    @property
    def has_logs(self) -> bool:
        return any(item.available for item in self.log_evidence)

    @property
    def status(self) -> str:
        if self.has_policy and self.has_logs:
            return "collected"
        if self.has_policy or self.has_logs:
            return "partial"
        return "missing"

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "title": self.title,
            "status": self.status,
            "policy_evidence": [item.to_dict() for item in self.policy_evidence],
            "log_evidence": [item.to_dict() for item in self.log_evidence],
        }


SOC2_CONTROLS: tuple[SOC2Control, ...] = (
    SOC2Control(
        control_id="CC1.1",
        trust_service_category="common_criteria",
        title="Control environment and security ownership",
        description="Policies define security responsibilities and operating baseline.",
        policy_paths=(
            "docs/ops/security_baseline.md",
            "docs/ops/o10_security_hardening.md",
        ),
        audit_entity_kinds=("tenant", "user", "api_key"),
        audit_actions=("tenant.update", "user.update", "api_key.create"),
    ),
    SOC2Control(
        control_id="CC2.1",
        trust_service_category="common_criteria",
        title="Security communication and policy availability",
        description="Security and compliance policies are documented for operators.",
        policy_paths=(
            "docs/ops/security_baseline.md",
            "docs/security/as_0_1_auth_surface_inventory.md",
        ),
        event_types=("cross_agent/observation", "debug.finding"),
    ),
    SOC2Control(
        control_id="CC6.1",
        trust_service_category="common_criteria",
        title="Logical access controls",
        description="Authentication, API key, and session changes are auditable.",
        policy_paths=(
            "docs/security/as_0_1_auth_surface_inventory.md",
            "docs/design/as-auth-security-shared-library.md",
        ),
        audit_entity_kinds=("user", "session", "api_key", "oauth_token"),
        audit_actions=("login", "logout", "api_key.create", "api_key.revoke"),
    ),
    SOC2Control(
        control_id="CC6.6",
        trust_service_category="common_criteria",
        title="Confidentiality and secret handling",
        description="Secret storage and credential boundaries are documented and logged.",
        policy_paths=(
            "docs/security/ks-multi-tenant-secret-management.md",
            "docs/security/as_0_4_credential_refactor_migration_plan.md",
        ),
        audit_entity_kinds=("tenant_secret", "llm_credential", "oauth_token"),
        audit_actions=("secret.rotate", "credential.update", "oauth_token.refresh"),
    ),
    SOC2Control(
        control_id="CC7.2",
        trust_service_category="common_criteria",
        title="Monitoring, detection, and findings",
        description="Security findings and scanner results are retained for review.",
        policy_paths=(
            "docs/ops/security_baseline.md",
            "docs/design/sandbox-tier-audit.md",
        ),
        event_types=(
            "debug.finding",
            "security.sast",
            "security.sca",
            "security.secrets",
            "security.container",
        ),
    ),
    SOC2Control(
        control_id="CC8.1",
        trust_service_category="common_criteria",
        title="Change management",
        description="Dependency and deployment change policy is documented.",
        policy_paths=(
            "docs/ops/dependency_upgrade_policy.md",
            "docs/ops/renovate_policy.md",
        ),
        audit_entity_kinds=("task", "project", "workflow_run"),
        audit_actions=("task.update", "project.update", "workflow_run.complete"),
        event_types=("turn.complete", "workflow.step.complete"),
    ),
    SOC2Control(
        control_id="CC9.2",
        trust_service_category="common_criteria",
        title="Vendor and risk management",
        description="Third-party dependency and OAuth risks are tracked.",
        policy_paths=(
            "docs/ops/dependency_upgrade_policy.md",
            "docs/security/as_0_3_account_linking.md",
        ),
        audit_entity_kinds=("oauth_provider", "git_account", "integration"),
        audit_actions=("integration.update", "oauth_provider.update"),
        event_types=("dependency.upgrade", "security.sca"),
    ),
)


ISO27001_CONTROLS: tuple[ISO27001Control, ...] = (
    ISO27001Control(
        control_id="A.5.1",
        domain="organizational_controls",
        title="Policies for information security",
        description="Information security policies are documented and available.",
        policy_paths=(
            "docs/ops/security_baseline.md",
            "docs/ops/o10_security_hardening.md",
        ),
        event_types=("debug.finding", "cross_agent/observation"),
    ),
    ISO27001Control(
        control_id="A.5.15",
        domain="organizational_controls",
        title="Access control",
        description="Logical access changes are auditable and policy-backed.",
        policy_paths=(
            "docs/security/as_0_1_auth_surface_inventory.md",
            "docs/design/as-auth-security-shared-library.md",
        ),
        audit_entity_kinds=("user", "session", "api_key", "oauth_token"),
        audit_actions=("login", "logout", "api_key.create", "api_key.revoke"),
    ),
    ISO27001Control(
        control_id="A.5.16",
        domain="organizational_controls",
        title="Identity management",
        description="Tenant, user, and OAuth identity lifecycle changes are logged.",
        policy_paths=(
            "docs/security/as_0_3_account_linking.md",
            "docs/security/as_0_1_auth_surface_inventory.md",
        ),
        audit_entity_kinds=("tenant", "user", "oauth_provider", "git_account"),
        audit_actions=("tenant.update", "user.update", "oauth_provider.update"),
    ),
    ISO27001Control(
        control_id="A.5.23",
        domain="organizational_controls",
        title="Information security for cloud services",
        description="Cloud service, sandbox, and integration boundaries are documented.",
        policy_paths=(
            "docs/design/sandbox-tier-audit.md",
            "docs/operations/sandbox.md",
        ),
        audit_entity_kinds=("integration", "workflow_run", "project"),
        audit_actions=("integration.update", "workflow_run.complete"),
        event_types=("security.container", "workflow.step.complete"),
    ),
    ISO27001Control(
        control_id="A.5.30",
        domain="organizational_controls",
        title="ICT readiness for business continuity",
        description="Disaster recovery and failover procedures are retained.",
        policy_paths=(
            "docs/ops/dr_runbook.md",
            "docs/ops/db_failover.md",
            "docs/ops/dr_rto_rpo.md",
        ),
        audit_entity_kinds=("workflow_run", "task"),
        audit_actions=("workflow_run.complete", "task.update"),
        event_types=("workflow.step.complete", "turn.complete"),
    ),
    ISO27001Control(
        control_id="A.8.15",
        domain="technological_controls",
        title="Logging",
        description="Audit and event logs retain security-relevant activity.",
        policy_paths=(
            "docs/ops/observability_runbook.md",
            "docs/design/sandbox-tier-audit.md",
        ),
        audit_entity_kinds=("tenant", "user", "api_key", "tenant_secret"),
        audit_actions=("login", "logout", "secret.rotate", "api_key.revoke"),
        event_types=("debug.finding", "security.sast", "security.sca"),
    ),
    ISO27001Control(
        control_id="A.8.16",
        domain="technological_controls",
        title="Monitoring activities",
        description="Security findings and scanner events are captured for review.",
        policy_paths=(
            "docs/ops/security_baseline.md",
            "docs/ops/observability_runbook.md",
        ),
        event_types=(
            "debug.finding",
            "security.sast",
            "security.sca",
            "security.secrets",
            "security.container",
        ),
    ),
    ISO27001Control(
        control_id="A.8.24",
        domain="technological_controls",
        title="Use of cryptography",
        description="Key management and credential protection are documented.",
        policy_paths=(
            "docs/security/ks-multi-tenant-secret-management.md",
            "docs/operations/key-management.md",
        ),
        audit_entity_kinds=("tenant_secret", "llm_credential", "oauth_token"),
        audit_actions=("secret.rotate", "credential.update", "oauth_token.refresh"),
    ),
)


def list_soc2_controls() -> list[SOC2Control]:
    return list(SOC2_CONTROLS)


def list_iso27001_controls() -> list[ISO27001Control]:
    return list(ISO27001_CONTROLS)


def soc2_control_mapping() -> dict[str, Any]:
    return {
        "standard": "soc2",
        "version": SOC2_MAPPING_VERSION,
        "controls": [control.to_mapping_dict() for control in SOC2_CONTROLS],
    }


def iso27001_control_mapping() -> dict[str, Any]:
    return {
        "standard": "iso27001",
        "version": ISO27001_MAPPING_VERSION,
        "controls": [control.to_mapping_dict() for control in ISO27001_CONTROLS],
    }


def _policy_pointer(root: Path, rel_path: str) -> EvidencePointer:
    path = root / rel_path
    if not path.is_file():
        return EvidencePointer(
            source_id=rel_path,
            source_type="policy",
            description=f"Policy file {rel_path}",
            available=False,
            path=rel_path,
        )
    body = path.read_bytes()
    return EvidencePointer(
        source_id=rel_path,
        source_type="policy",
        description=f"Policy file {rel_path}",
        available=True,
        path=rel_path,
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
    )


async def _collect_audit_evidence(
    conn,
    tenant_id: str,
    control: SOC2Control | ISO27001Control,
    *,
    limit: int,
) -> EvidencePointer | None:
    if not control.audit_entity_kinds and not control.audit_actions:
        return None
    rows = await conn.fetch(
        "SELECT id, ts, action, entity_kind, entity_id "
        "FROM audit_log "
        "WHERE tenant_id = $1 "
        "AND (entity_kind = ANY($2::text[]) OR action = ANY($3::text[])) "
        "ORDER BY id DESC LIMIT $4",
        tenant_id,
        list(control.audit_entity_kinds),
        list(control.audit_actions),
        limit,
    )
    latest = rows[0] if rows else None
    return EvidencePointer(
        source_id=f"{control.control_id}.audit_log",
        source_type="log",
        description="Tenant audit_log rows matching compliance control predicates",
        available=bool(rows),
        table="audit_log",
        row_count=len(rows),
        latest_id=int(latest["id"]) if latest else 0,
        latest_at=latest["ts"] if latest else None,
    )


async def _collect_event_evidence(
    conn,
    tenant_id: str,
    control: SOC2Control | ISO27001Control,
    *,
    limit: int,
) -> EvidencePointer | None:
    if not control.event_types:
        return None
    rows = await conn.fetch(
        "SELECT id, created_at, event_type "
        "FROM event_log "
        "WHERE tenant_id = $1 AND event_type = ANY($2::text[]) "
        "ORDER BY id DESC LIMIT $3",
        tenant_id,
        list(control.event_types),
        limit,
    )
    latest = rows[0] if rows else None
    return EvidencePointer(
        source_id=f"{control.control_id}.event_log",
        source_type="log",
        description="Tenant event_log rows matching compliance control predicates",
        available=bool(rows),
        table="event_log",
        row_count=len(rows),
        latest_id=int(latest["id"]) if latest else 0,
        latest_at=latest["created_at"] if latest else None,
    )


async def collect_soc2_evidence(
    conn,
    tenant_id: str,
    *,
    root: Path | str = PROJECT_ROOT,
    limit_per_source: int = 50,
) -> dict[str, Any]:
    root_path = Path(root)
    controls: list[ControlEvidence] = []
    for control in SOC2_CONTROLS:
        item = ControlEvidence(
            control_id=control.control_id,
            title=control.title,
            policy_evidence=[
                _policy_pointer(root_path, rel_path)
                for rel_path in control.policy_paths
            ],
        )
        audit_pointer = await _collect_audit_evidence(
            conn, tenant_id, control, limit=limit_per_source,
        )
        event_pointer = await _collect_event_evidence(
            conn, tenant_id, control, limit=limit_per_source,
        )
        for pointer in (audit_pointer, event_pointer):
            if pointer is not None:
                item.log_evidence.append(pointer)
        controls.append(item)

    return {
        "standard": "soc2",
        "version": SOC2_MAPPING_VERSION,
        "tenant_id": tenant_id,
        "collected_at": time.time(),
        "controls": [control.to_dict() for control in controls],
        "summary": {
            "controls_total": len(controls),
            "controls_collected": sum(
                1 for control in controls if control.status == "collected"
            ),
            "controls_partial": sum(
                1 for control in controls if control.status == "partial"
            ),
            "controls_missing": sum(
                1 for control in controls if control.status == "missing"
            ),
            "controls_with_policy": sum(
                1 for control in controls if control.has_policy
            ),
            "controls_with_logs": sum(
                1 for control in controls if control.has_logs
            ),
        },
    }


async def collect_iso27001_evidence(
    conn,
    tenant_id: str,
    *,
    root: Path | str = PROJECT_ROOT,
    limit_per_source: int = 50,
) -> dict[str, Any]:
    root_path = Path(root)
    controls: list[ControlEvidence] = []
    for control in ISO27001_CONTROLS:
        item = ControlEvidence(
            control_id=control.control_id,
            title=control.title,
            policy_evidence=[
                _policy_pointer(root_path, rel_path)
                for rel_path in control.policy_paths
            ],
        )
        audit_pointer = await _collect_audit_evidence(
            conn, tenant_id, control, limit=limit_per_source,
        )
        event_pointer = await _collect_event_evidence(
            conn, tenant_id, control, limit=limit_per_source,
        )
        for pointer in (audit_pointer, event_pointer):
            if pointer is not None:
                item.log_evidence.append(pointer)
        controls.append(item)

    return {
        "standard": "iso27001",
        "version": ISO27001_MAPPING_VERSION,
        "tenant_id": tenant_id,
        "collected_at": time.time(),
        "controls": [control.to_dict() for control in controls],
        "summary": {
            "controls_total": len(controls),
            "controls_collected": sum(
                1 for control in controls if control.status == "collected"
            ),
            "controls_partial": sum(
                1 for control in controls if control.status == "partial"
            ),
            "controls_missing": sum(
                1 for control in controls if control.status == "missing"
            ),
            "controls_with_policy": sum(
                1 for control in controls if control.has_policy
            ),
            "controls_with_logs": sum(
                1 for control in controls if control.has_logs
            ),
        },
    }


async def collect_soc2_evidence_for_bundle(
    conn,
    bundle_id: str,
    *,
    root: Path | str = PROJECT_ROOT,
    limit_per_source: int = 50,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT id, tenant_id, standard "
        "FROM compliance_evidence_bundles WHERE id = $1",
        bundle_id,
    )
    if row is None:
        raise ValueError(f"Compliance evidence bundle not found: {bundle_id}")
    if row["standard"] != "soc2":
        raise ValueError(
            f"SC.11.2 only collects soc2 bundles, got {row['standard']!r}"
        )

    mapping = soc2_control_mapping()
    manifest = await collect_soc2_evidence(
        conn,
        row["tenant_id"],
        root=root,
        limit_per_source=limit_per_source,
    )
    await conn.execute(
        "UPDATE compliance_evidence_bundles "
        "SET status = 'collecting', "
        "control_mapping_json = $2, "
        "evidence_manifest_json = $3, "
        "error = '', "
        "version = version + 1 "
        "WHERE id = $1",
        bundle_id,
        json.dumps(mapping, sort_keys=True),
        json.dumps(manifest, sort_keys=True),
    )
    return manifest


async def collect_iso27001_evidence_for_bundle(
    conn,
    bundle_id: str,
    *,
    root: Path | str = PROJECT_ROOT,
    limit_per_source: int = 50,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT id, tenant_id, standard "
        "FROM compliance_evidence_bundles WHERE id = $1",
        bundle_id,
    )
    if row is None:
        raise ValueError(f"Compliance evidence bundle not found: {bundle_id}")
    if row["standard"] != "iso27001":
        raise ValueError(
            f"SC.11.3 only collects iso27001 bundles, got {row['standard']!r}"
        )

    mapping = iso27001_control_mapping()
    manifest = await collect_iso27001_evidence(
        conn,
        row["tenant_id"],
        root=root,
        limit_per_source=limit_per_source,
    )
    await conn.execute(
        "UPDATE compliance_evidence_bundles "
        "SET status = 'collecting', "
        "control_mapping_json = $2, "
        "evidence_manifest_json = $3, "
        "error = '', "
        "version = version + 1 "
        "WHERE id = $1",
        bundle_id,
        json.dumps(mapping, sort_keys=True),
        json.dumps(manifest, sort_keys=True),
    )
    return manifest
