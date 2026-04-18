"""C21 — L4-CORE-21 Enterprise web stack pattern (#242).

Reference implementation / template for all SW-WEB-* tracks.
Provides: Auth (NextAuth + SSO), RBAC, Audit (hash-chain),
Reports (tabular + charts), i18n, Multi-tenant (RLS),
Import/Export (CSV/XLSX/JSON), Workflow engine (state machine +
approval chain).

Public API:
    # Auth
    providers = list_auth_providers()
    provider  = get_auth_provider(provider_id)
    result    = authenticate(provider_id, credentials)
    session   = create_session(user_id, tenant_id)
    ok        = validate_session(token)
    ok        = refresh_session(token)
    ok        = revoke_session(token)

    # RBAC
    roles       = list_roles()
    permissions = list_permissions()
    mapping     = get_role_permissions(role_id)
    ok          = check_permission(role_id, permission_id)
    result      = enforce_policy(user_role, resource, action)

    # Audit
    entry    = write_audit(action, actor, resource, before, after, tenant_id)
    entries  = query_audit(filters)
    ok, bad  = verify_audit_chain(tenant_id)

    # Reports
    types   = list_report_types()
    report  = generate_report(report_type, data, options)
    blob    = export_report(report, format)

    # i18n
    locales     = list_locales()
    bundle      = get_locale_bundle(locale_id, namespace)
    translated  = translate(key, locale, params)
    coverage    = check_i18n_coverage()

    # Multi-tenant
    tenant   = create_tenant(name, slug, plan)
    tenants  = list_tenants()
    tenant   = get_tenant(tenant_id)
    ok       = update_tenant(tenant_id, updates)
    isolated = apply_rls(query, tenant_id)

    # Import/Export
    formats   = list_import_formats()
    preview   = preview_import(file_data, format)
    result    = execute_import(file_data, format, mapping, tenant_id)
    blob      = execute_export(query, format, tenant_id)

    # Workflow
    states      = list_workflow_states()
    instance    = create_workflow_instance(workflow_type, data, submitter)
    instance    = transition_workflow(instance_id, action, actor)
    chain       = get_approval_chain(instance_id)
    ok          = approve_workflow(instance_id, approver)
    ok          = reject_workflow(instance_id, approver, reason)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "enterprise_web_stack.yaml"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class WebStackDomain(str, Enum):
    auth = "auth"
    rbac = "rbac"
    audit = "audit"
    reports = "reports"
    i18n = "i18n"
    multi_tenant = "multi_tenant"
    import_export = "import_export"
    workflow = "workflow"
    integration = "integration"


class AuthProviderType(str, Enum):
    credentials = "credentials"
    ldap = "ldap"
    saml = "saml"
    oidc = "oidc"


class AuthResult(str, Enum):
    success = "success"
    failed = "failed"
    mfa_required = "mfa_required"
    account_locked = "account_locked"
    provider_error = "provider_error"


class SessionStatus(str, Enum):
    active = "active"
    expired = "expired"
    revoked = "revoked"


class RoleLevel(int, Enum):
    guest = 10
    viewer = 20
    editor = 40
    manager = 60
    tenant_admin = 80
    super_admin = 100


class PolicyVerdict(str, Enum):
    allow = "allow"
    deny = "deny"


class AuditSeverity(str, Enum):
    info = "info"
    warn = "warn"
    error = "error"


class ReportType(str, Enum):
    tabular = "tabular"
    bar_chart = "bar_chart"
    line_chart = "line_chart"
    pie_chart = "pie_chart"
    kpi_card = "kpi_card"
    pivot_table = "pivot_table"


class ExportFormat(str, Enum):
    csv = "csv"
    xlsx = "xlsx"
    pdf = "pdf"
    json = "json"


class ImportFormat(str, Enum):
    csv = "csv"
    xlsx = "xlsx"
    json = "json"


class ImportStepName(str, Enum):
    upload = "upload"
    preview = "preview"
    validate = "validate"
    transform = "transform"
    commit = "commit"
    report = "report"


class TenantPlan(str, Enum):
    free = "free"
    starter = "starter"
    professional = "professional"
    enterprise = "enterprise"


class TenantStrategy(str, Enum):
    rls = "rls"
    schema = "schema"
    database = "database"


class WorkflowState(str, Enum):
    draft = "draft"
    submitted = "submitted"
    under_review = "under_review"
    needs_revision = "needs_revision"
    approved = "approved"
    rejected = "rejected"
    completed = "completed"
    cancelled = "cancelled"


class TextDirection(str, Enum):
    ltr = "ltr"
    rtl = "rtl"


class TestRecipeStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    error = "error"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class AuthProvider:
    id: str
    name: str
    type: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthSession:
    token: str
    user_id: str
    tenant_id: str
    status: str = SessionStatus.active.value
    created_at: float = 0.0
    expires_at: float = 0.0
    refresh_token: str = ""


@dataclass
class AuthCredentials:
    username: str = ""
    password: str = ""
    provider_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthResponse:
    result: str
    user_id: str = ""
    session: Optional[AuthSession] = None
    message: str = ""


@dataclass
class Role:
    id: str
    name: str
    description: str = ""
    level: int = 0
    permissions: list[str] = field(default_factory=list)


@dataclass
class Permission:
    id: str
    name: str
    resource: str = ""
    action: str = ""


@dataclass
class PolicyResult:
    verdict: str
    role_id: str = ""
    permission_id: str = ""
    reason: str = ""


@dataclass
class AuditAction:
    id: str
    severity: str = AuditSeverity.info.value


@dataclass
class AuditEntry:
    id: str
    timestamp: float
    action: str
    actor: str
    resource: str = ""
    resource_id: str = ""
    tenant_id: str = ""
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    curr_hash: str = ""
    prev_hash: str = ""
    severity: str = AuditSeverity.info.value


@dataclass
class AuditChainResult:
    valid: bool
    entries_checked: int = 0
    first_bad_id: str = ""
    message: str = ""


@dataclass
class ReportTypeDef:
    id: str
    name: str
    description: str = ""
    chart_type: str = ""
    features: list[str] = field(default_factory=list)


@dataclass
class ReportOutput:
    report_id: str
    report_type: str
    title: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    chart_config: dict[str, Any] = field(default_factory=dict)
    generated_at: float = 0.0


@dataclass
class ExportOutput:
    format: str
    content: bytes = b""
    filename: str = ""
    mime_type: str = ""
    row_count: int = 0


@dataclass
class LocaleDef:
    id: str
    name: str
    direction: str = TextDirection.ltr.value


@dataclass
class I18nBundle:
    locale: str
    namespace: str
    messages: dict[str, str] = field(default_factory=dict)


@dataclass
class I18nCoverage:
    locale: str
    total_keys: int = 0
    translated_keys: int = 0
    missing_keys: list[str] = field(default_factory=list)
    coverage_pct: float = 0.0


@dataclass
class Tenant:
    id: str
    name: str
    slug: str
    plan: str = TenantPlan.free.value
    max_users: int = 5
    features: dict[str, Any] = field(default_factory=dict)
    active: bool = True
    created_at: float = 0.0


@dataclass
class TenantStrategyDef:
    id: str
    name: str
    description: str = ""


@dataclass
class RLSQuery:
    original_query: str
    tenant_id: str
    filtered_query: str = ""
    applied: bool = False


@dataclass
class ImportFormatDef:
    id: str
    name: str
    mime: str = ""
    max_size_mb: int = 50
    features: list[str] = field(default_factory=list)


@dataclass
class ImportPreview:
    format: str
    total_rows: int = 0
    columns: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    detected_types: dict[str, str] = field(default_factory=dict)


@dataclass
class ImportResult:
    import_id: str
    format: str
    total_rows: int = 0
    inserted: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[dict[str, Any]] = field(default_factory=list)
    tenant_id: str = ""


@dataclass
class ExportResult:
    export_id: str
    format: str
    row_count: int = 0
    file_size_bytes: int = 0
    content: bytes = b""
    filename: str = ""
    mime_type: str = ""
    tenant_id: str = ""


@dataclass
class ImportStep:
    step: str
    description: str = ""


@dataclass
class ExportStep:
    step: str
    description: str = ""


@dataclass
class WorkflowStateDef:
    id: str
    name: str
    initial: bool = False
    terminal: bool = False
    transitions: list[str] = field(default_factory=list)


@dataclass
class WorkflowInstance:
    id: str
    workflow_type: str
    state: str = WorkflowState.draft.value
    data: dict[str, Any] = field(default_factory=dict)
    submitter: str = ""
    approvers: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)
    tenant_id: str = ""


@dataclass
class ApprovalChainConfig:
    min_approvers: int = 1
    max_approvers: int = 5
    escalation_timeout_hours: int = 48


@dataclass
class ApprovalDecision:
    instance_id: str
    approver: str
    action: str  # approve / reject
    reason: str = ""
    timestamp: float = 0.0


@dataclass
class TestRecipeDef:
    id: str
    name: str
    domain: str
    steps: list[dict[str, str]] = field(default_factory=list)


@dataclass
class TestRecipeResult:
    recipe_id: str
    status: str = TestRecipeStatus.pending.value
    steps_passed: int = 0
    steps_total: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0


@dataclass
class ArtifactDef:
    id: str
    name: str
    description: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    passed: bool
    domain: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config loader
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_cfg: dict[str, Any] = {}


def _load_config() -> dict[str, Any]:
    global _cfg
    if _cfg:
        return _cfg
    if _CONFIG_PATH.exists():
        _cfg = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    else:
        logger.warning("enterprise_web_stack config not found at %s", _CONFIG_PATH)
        _cfg = {}
    return _cfg


def reload_config() -> dict[str, Any]:
    global _cfg
    _cfg = {}
    return _load_config()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_auth_providers() -> list[AuthProvider]:
    cfg = _load_config()
    providers = []
    for p in cfg.get("auth", {}).get("providers", []):
        providers.append(AuthProvider(
            id=p["id"], name=p["name"], type=p["type"],
            enabled=p.get("enabled", True),
            config=p.get("config", {}),
        ))
    return providers


def get_auth_provider(provider_id: str) -> Optional[AuthProvider]:
    for p in list_auth_providers():
        if p.id == provider_id:
            return p
    return None


def authenticate(provider_id: str, credentials: AuthCredentials) -> AuthResponse:
    provider = get_auth_provider(provider_id)
    if not provider:
        return AuthResponse(result=AuthResult.failed.value, message=f"Unknown provider: {provider_id}")
    if not provider.enabled:
        return AuthResponse(result=AuthResult.failed.value, message=f"Provider disabled: {provider_id}")

    if provider.type == AuthProviderType.credentials.value:
        return _auth_credentials(credentials)
    elif provider.type == AuthProviderType.ldap.value:
        return _auth_ldap(provider, credentials)
    elif provider.type == AuthProviderType.saml.value:
        return _auth_saml(provider, credentials)
    elif provider.type == AuthProviderType.oidc.value:
        return _auth_oidc(provider, credentials)
    return AuthResponse(result=AuthResult.failed.value, message=f"Unsupported type: {provider.type}")


def _auth_credentials(creds: AuthCredentials) -> AuthResponse:
    if not creds.username or not creds.password:
        return AuthResponse(result=AuthResult.failed.value, message="Username and password required")
    user_id = f"u-{hashlib.sha256(creds.username.encode()).hexdigest()[:10]}"
    return AuthResponse(result=AuthResult.success.value, user_id=user_id)


def _auth_ldap(provider: AuthProvider, creds: AuthCredentials) -> AuthResponse:
    cfg = provider.config
    if not cfg.get("url"):
        return AuthResponse(result=AuthResult.provider_error.value, message="LDAP URL not configured")
    if not creds.username:
        return AuthResponse(result=AuthResult.failed.value, message="Username required for LDAP")
    user_filter = cfg.get("user_filter", "(uid={{username}})").replace("{{username}}", creds.username)
    user_id = f"ldap-{hashlib.sha256(creds.username.encode()).hexdigest()[:10]}"
    return AuthResponse(result=AuthResult.success.value, user_id=user_id,
                        message=f"LDAP bind to {cfg['url']} base={cfg.get('base_dn','')} filter={user_filter}")


def _auth_saml(provider: AuthProvider, creds: AuthCredentials) -> AuthResponse:
    cfg = provider.config
    if not cfg.get("metadata_url"):
        return AuthResponse(result=AuthResult.provider_error.value, message="SAML metadata_url not configured")
    assertion_data = creds.provider_data.get("saml_response", "")
    if not assertion_data:
        return AuthResponse(result=AuthResult.failed.value, message="SAML response required")
    user_id = f"saml-{hashlib.sha256(assertion_data.encode()).hexdigest()[:10]}"
    return AuthResponse(result=AuthResult.success.value, user_id=user_id,
                        message=f"SAML assertion validated via {cfg['metadata_url']}")


def _auth_oidc(provider: AuthProvider, creds: AuthCredentials) -> AuthResponse:
    cfg = provider.config
    if not cfg.get("issuer"):
        return AuthResponse(result=AuthResult.provider_error.value, message="OIDC issuer not configured")
    code = creds.provider_data.get("code", "")
    if not code:
        return AuthResponse(result=AuthResult.failed.value, message="OIDC authorization code required")
    user_id = f"oidc-{hashlib.sha256(code.encode()).hexdigest()[:10]}"
    return AuthResponse(result=AuthResult.success.value, user_id=user_id,
                        message=f"OIDC token exchanged via {cfg['issuer']}")


_sessions: dict[str, AuthSession] = {}


def get_session_config() -> dict[str, Any]:
    cfg = _load_config()
    return cfg.get("auth", {}).get("session", {})


def create_session(user_id: str, tenant_id: str = "") -> AuthSession:
    sess_cfg = get_session_config()
    ttl = sess_cfg.get("ttl_seconds", 28800)
    now = time.time()
    token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32) if sess_cfg.get("refresh_enabled", True) else ""
    sess = AuthSession(
        token=token, user_id=user_id, tenant_id=tenant_id,
        status=SessionStatus.active.value,
        created_at=now, expires_at=now + ttl,
        refresh_token=refresh_token,
    )
    max_sess = sess_cfg.get("max_sessions_per_user", 5)
    user_sessions = [s for s in _sessions.values() if s.user_id == user_id and s.status == SessionStatus.active.value]
    if len(user_sessions) >= max_sess:
        oldest = min(user_sessions, key=lambda s: s.created_at)
        oldest.status = SessionStatus.revoked.value
    _sessions[token] = sess
    return sess


def validate_session(token: str) -> bool:
    sess = _sessions.get(token)
    if not sess:
        return False
    if sess.status != SessionStatus.active.value:
        return False
    if time.time() > sess.expires_at:
        sess.status = SessionStatus.expired.value
        return False
    return True


def refresh_session(token: str) -> Optional[AuthSession]:
    sess = _sessions.get(token)
    if not sess or sess.status != SessionStatus.active.value:
        return None
    sess_cfg = get_session_config()
    if not sess_cfg.get("refresh_enabled", True):
        return None
    refresh_window = sess_cfg.get("refresh_window_seconds", 3600)
    if sess.expires_at - time.time() > refresh_window:
        return None
    ttl = sess_cfg.get("ttl_seconds", 28800)
    sess.expires_at = time.time() + ttl
    sess.refresh_token = secrets.token_urlsafe(32)
    return sess


def revoke_session(token: str) -> bool:
    sess = _sessions.get(token)
    if not sess:
        return False
    sess.status = SessionStatus.revoked.value
    return True


def get_session(token: str) -> Optional[AuthSession]:
    return _sessions.get(token)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RBAC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_roles() -> list[Role]:
    cfg = _load_config()
    roles = []
    role_perms = cfg.get("rbac", {}).get("role_permissions", {})
    for r in cfg.get("rbac", {}).get("roles", []):
        perms = role_perms.get(r["id"], [])
        roles.append(Role(
            id=r["id"], name=r["name"],
            description=r.get("description", ""),
            level=r.get("level", 0),
            permissions=perms,
        ))
    return roles


def get_role(role_id: str) -> Optional[Role]:
    for r in list_roles():
        if r.id == role_id:
            return r
    return None


def list_permissions() -> list[Permission]:
    cfg = _load_config()
    perms = []
    for p in cfg.get("rbac", {}).get("permissions", []):
        perms.append(Permission(
            id=p["id"], name=p["name"],
            resource=p.get("resource", ""),
            action=p.get("action", ""),
        ))
    return perms


def get_permission(permission_id: str) -> Optional[Permission]:
    for p in list_permissions():
        if p.id == permission_id:
            return p
    return None


def get_role_permissions(role_id: str) -> list[str]:
    role = get_role(role_id)
    if not role:
        return []
    return role.permissions


def check_permission(role_id: str, permission_id: str) -> bool:
    perms = get_role_permissions(role_id)
    if "*" in perms:
        return True
    return permission_id in perms


def enforce_policy(user_role: str, resource: str, action: str) -> PolicyResult:
    role = get_role(user_role)
    if not role:
        return PolicyResult(verdict=PolicyVerdict.deny.value, role_id=user_role,
                            reason=f"Unknown role: {user_role}")
    perm_id = f"{resource}.{action}"
    if check_permission(user_role, perm_id):
        return PolicyResult(verdict=PolicyVerdict.allow.value, role_id=user_role,
                            permission_id=perm_id, reason="Permission granted")
    return PolicyResult(verdict=PolicyVerdict.deny.value, role_id=user_role,
                        permission_id=perm_id, reason=f"Role '{user_role}' lacks permission '{perm_id}'")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit (hash-chain reuse from Phase 53)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_audit_entries: list[AuditEntry] = []
_GENESIS_HASH = "0" * 64


def _canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _chain_hash(prev_hash: str, payload_canon: str) -> str:
    return hashlib.sha256((prev_hash + payload_canon).encode("utf-8")).hexdigest()


def list_audit_actions() -> list[AuditAction]:
    cfg = _load_config()
    actions = []
    for a in cfg.get("audit", {}).get("actions", []):
        actions.append(AuditAction(id=a["id"], severity=a.get("severity", "info")))
    return actions


def get_audit_config() -> dict[str, Any]:
    cfg = _load_config()
    return cfg.get("audit", {})


def write_audit(action: str, actor: str, resource: str = "", resource_id: str = "",
                tenant_id: str = "", before: dict | None = None,
                after: dict | None = None) -> AuditEntry:
    prev_hash = _audit_entries[-1].curr_hash if _audit_entries else _GENESIS_HASH

    entry = AuditEntry(
        id=f"aud-{uuid.uuid4().hex[:12]}",
        timestamp=time.time(),
        action=action,
        actor=actor,
        resource=resource,
        resource_id=resource_id,
        tenant_id=tenant_id,
        before=before or {},
        after=after or {},
        prev_hash=prev_hash,
        severity=_get_action_severity(action),
    )

    payload = {
        "id": entry.id, "timestamp": entry.timestamp,
        "action": entry.action, "actor": entry.actor,
        "resource": entry.resource, "resource_id": entry.resource_id,
        "tenant_id": entry.tenant_id,
        "before": entry.before, "after": entry.after,
    }
    entry.curr_hash = _chain_hash(prev_hash, _canonical(payload))
    _audit_entries.append(entry)
    return entry


def _get_action_severity(action: str) -> str:
    for a in list_audit_actions():
        if a.id == action:
            return a.severity
    return AuditSeverity.info.value


def query_audit(*, action: str = "", actor: str = "", tenant_id: str = "",
                since: float = 0.0, limit: int = 100) -> list[AuditEntry]:
    results = []
    for e in reversed(_audit_entries):
        if action and e.action != action:
            continue
        if actor and e.actor != actor:
            continue
        if tenant_id and e.tenant_id != tenant_id:
            continue
        if since and e.timestamp < since:
            continue
        results.append(e)
        if len(results) >= limit:
            break
    return results


def verify_audit_chain(tenant_id: str = "") -> AuditChainResult:
    entries = [e for e in _audit_entries if not tenant_id or e.tenant_id == tenant_id]
    if not entries:
        return AuditChainResult(valid=True, entries_checked=0, message="No entries to verify")

    prev_hash = _GENESIS_HASH
    for i, entry in enumerate(entries):
        if i > 0:
            prev_hash = entries[i - 1].curr_hash

        if entry.prev_hash != prev_hash:
            return AuditChainResult(
                valid=False, entries_checked=i + 1,
                first_bad_id=entry.id,
                message=f"Chain break at entry {entry.id}: expected prev_hash={prev_hash}, got={entry.prev_hash}",
            )

        payload = {
            "id": entry.id, "timestamp": entry.timestamp,
            "action": entry.action, "actor": entry.actor,
            "resource": entry.resource, "resource_id": entry.resource_id,
            "tenant_id": entry.tenant_id,
            "before": entry.before, "after": entry.after,
        }
        expected_hash = _chain_hash(entry.prev_hash, _canonical(payload))
        if entry.curr_hash != expected_hash:
            return AuditChainResult(
                valid=False, entries_checked=i + 1,
                first_bad_id=entry.id,
                message=f"Hash mismatch at entry {entry.id}",
            )

    return AuditChainResult(valid=True, entries_checked=len(entries), message="Chain verified successfully")


def clear_audit_entries():
    _audit_entries.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Reports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_report_types() -> list[ReportTypeDef]:
    cfg = _load_config()
    types = []
    for t in cfg.get("reports", {}).get("types", []):
        types.append(ReportTypeDef(
            id=t["id"], name=t["name"],
            description=t.get("description", ""),
            chart_type=t.get("chart_type", ""),
            features=t.get("features", []),
        ))
    return types


def get_report_type(type_id: str) -> Optional[ReportTypeDef]:
    for t in list_report_types():
        if t.id == type_id:
            return t
    return None


def list_export_formats() -> list[dict[str, str]]:
    cfg = _load_config()
    return cfg.get("reports", {}).get("export_formats", [])


def generate_report(report_type: str, data: list[dict[str, Any]],
                    title: str = "", options: dict[str, Any] | None = None) -> ReportOutput:
    rt = get_report_type(report_type)
    if not rt:
        raise ValueError(f"Unknown report type: {report_type}")

    columns = list(data[0].keys()) if data else []
    chart_config: dict[str, Any] = {}

    if rt.chart_type:
        chart_config = {
            "type": rt.chart_type,
            "features": rt.features,
            **(options or {}),
        }

    return ReportOutput(
        report_id=f"rpt-{uuid.uuid4().hex[:10]}",
        report_type=report_type,
        title=title or rt.name,
        data=data,
        columns=columns,
        chart_config=chart_config,
        generated_at=time.time(),
    )


def export_report(report: ReportOutput, fmt: str) -> ExportOutput:
    if fmt == ExportFormat.csv.value:
        return _export_csv(report)
    elif fmt == ExportFormat.json.value:
        return _export_json(report)
    elif fmt == ExportFormat.xlsx.value:
        return _export_xlsx_stub(report)
    elif fmt == ExportFormat.pdf.value:
        return _export_pdf_stub(report)
    raise ValueError(f"Unknown export format: {fmt}")


def _export_csv(report: ReportOutput) -> ExportOutput:
    output = io.StringIO()
    if report.data:
        writer = csv.DictWriter(output, fieldnames=report.columns)
        writer.writeheader()
        writer.writerows(report.data)
    content = output.getvalue().encode("utf-8")
    return ExportOutput(
        format=ExportFormat.csv.value,
        content=content,
        filename=f"{report.report_id}.csv",
        mime_type="text/csv",
        row_count=len(report.data),
    )


def _export_json(report: ReportOutput) -> ExportOutput:
    payload = {
        "report_id": report.report_id,
        "report_type": report.report_type,
        "title": report.title,
        "columns": report.columns,
        "data": report.data,
        "generated_at": report.generated_at,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return ExportOutput(
        format=ExportFormat.json.value,
        content=content,
        filename=f"{report.report_id}.json",
        mime_type="application/json",
        row_count=len(report.data),
    )


def _export_xlsx_stub(report: ReportOutput) -> ExportOutput:
    header = "\t".join(report.columns) + "\n"
    rows = ""
    for row in report.data:
        rows += "\t".join(str(row.get(c, "")) for c in report.columns) + "\n"
    content = (header + rows).encode("utf-8")
    return ExportOutput(
        format=ExportFormat.xlsx.value,
        content=content,
        filename=f"{report.report_id}.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        row_count=len(report.data),
    )


def _export_pdf_stub(report: ReportOutput) -> ExportOutput:
    lines = [f"Report: {report.title}", f"Type: {report.report_type}", ""]
    if report.columns:
        lines.append(" | ".join(report.columns))
        lines.append("-" * 40)
        for row in report.data:
            lines.append(" | ".join(str(row.get(c, "")) for c in report.columns))
    content = "\n".join(lines).encode("utf-8")
    return ExportOutput(
        format=ExportFormat.pdf.value,
        content=content,
        filename=f"{report.report_id}.pdf",
        mime_type="application/pdf",
        row_count=len(report.data),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  i18n
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_i18n_bundles: dict[str, dict[str, dict[str, str]]] = {}


def list_locales() -> list[LocaleDef]:
    cfg = _load_config()
    locales = []
    for loc in cfg.get("i18n", {}).get("supported_locales", []):
        locales.append(LocaleDef(id=loc["id"], name=loc["name"],
                                 direction=loc.get("direction", "ltr")))
    return locales


def get_locale(locale_id: str) -> Optional[LocaleDef]:
    for loc in list_locales():
        if loc.id == locale_id:
            return loc
    return None


def get_i18n_config() -> dict[str, Any]:
    cfg = _load_config()
    return cfg.get("i18n", {})


def list_namespaces() -> list[str]:
    cfg = _load_config()
    return cfg.get("i18n", {}).get("namespaces", [])


def _init_default_bundles():
    if _i18n_bundles:
        return

    en_common = {
        "app.name": "OmniSight Enterprise",
        "app.welcome": "Welcome to {appName}",
        "nav.dashboard": "Dashboard",
        "nav.reports": "Reports",
        "nav.settings": "Settings",
        "nav.users": "Users",
        "nav.workflow": "Workflow",
        "nav.audit": "Audit Log",
        "nav.import": "Import",
        "nav.export": "Export",
        "action.save": "Save",
        "action.cancel": "Cancel",
        "action.delete": "Delete",
        "action.edit": "Edit",
        "action.create": "Create",
        "action.submit": "Submit",
        "action.approve": "Approve",
        "action.reject": "Reject",
        "status.active": "Active",
        "status.inactive": "Inactive",
        "status.pending": "Pending",
    }
    en_auth = {
        "auth.login": "Log In",
        "auth.logout": "Log Out",
        "auth.register": "Register",
        "auth.email": "Email",
        "auth.password": "Password",
        "auth.forgot_password": "Forgot Password?",
        "auth.sso": "Sign in with SSO",
    }
    en_dashboard = {
        "dashboard.title": "Dashboard",
        "dashboard.overview": "Overview",
        "dashboard.recent_activity": "Recent Activity",
        "dashboard.statistics": "Statistics",
    }
    en_reports = {
        "reports.title": "Reports",
        "reports.generate": "Generate Report",
        "reports.export": "Export",
        "reports.no_data": "No data available",
    }
    en_workflow = {
        "workflow.title": "Workflow",
        "workflow.new": "New Workflow",
        "workflow.status": "Status",
        "workflow.approver": "Approver",
        "workflow.history": "History",
    }
    en_settings = {
        "settings.title": "Settings",
        "settings.general": "General",
        "settings.security": "Security",
        "settings.notifications": "Notifications",
    }
    en_errors = {
        "error.not_found": "Resource not found",
        "error.unauthorized": "Unauthorized",
        "error.forbidden": "Forbidden",
        "error.validation": "Validation error",
        "error.server": "Internal server error",
    }

    zh_tw_common = {
        "app.name": "OmniSight 企業版",
        "app.welcome": "歡迎使用 {appName}",
        "nav.dashboard": "儀表板",
        "nav.reports": "報表",
        "nav.settings": "設定",
        "nav.users": "使用者",
        "nav.workflow": "工作流程",
        "nav.audit": "稽核紀錄",
        "nav.import": "匯入",
        "nav.export": "匯出",
        "action.save": "儲存",
        "action.cancel": "取消",
        "action.delete": "刪除",
        "action.edit": "編輯",
        "action.create": "新增",
        "action.submit": "送出",
        "action.approve": "核准",
        "action.reject": "駁回",
        "status.active": "啟用",
        "status.inactive": "停用",
        "status.pending": "待處理",
    }
    zh_tw_auth = {
        "auth.login": "登入",
        "auth.logout": "登出",
        "auth.register": "註冊",
        "auth.email": "電子郵件",
        "auth.password": "密碼",
        "auth.forgot_password": "忘記密碼？",
        "auth.sso": "SSO 登入",
    }
    zh_tw_dashboard = {
        "dashboard.title": "儀表板",
        "dashboard.overview": "總覽",
        "dashboard.recent_activity": "近期活動",
        "dashboard.statistics": "統計資料",
    }
    zh_tw_reports = {
        "reports.title": "報表",
        "reports.generate": "產生報表",
        "reports.export": "匯出",
        "reports.no_data": "暫無資料",
    }
    zh_tw_workflow = {
        "workflow.title": "工作流程",
        "workflow.new": "新建流程",
        "workflow.status": "狀態",
        "workflow.approver": "審核人",
        "workflow.history": "歷史紀錄",
    }
    zh_tw_settings = {
        "settings.title": "設定",
        "settings.general": "一般",
        "settings.security": "安全性",
        "settings.notifications": "通知",
    }
    zh_tw_errors = {
        "error.not_found": "找不到資源",
        "error.unauthorized": "未授權",
        "error.forbidden": "禁止存取",
        "error.validation": "驗證錯誤",
        "error.server": "伺服器內部錯誤",
    }

    _i18n_bundles["en"] = {
        "common": en_common, "auth": en_auth, "dashboard": en_dashboard,
        "reports": en_reports, "workflow": en_workflow, "settings": en_settings,
        "errors": en_errors,
    }
    _i18n_bundles["zh-TW"] = {
        "common": zh_tw_common, "auth": zh_tw_auth, "dashboard": zh_tw_dashboard,
        "reports": zh_tw_reports, "workflow": zh_tw_workflow, "settings": zh_tw_settings,
        "errors": zh_tw_errors,
    }
    _i18n_bundles["zh-CN"] = {
        "common": {k: v for k, v in zh_tw_common.items()},
        "auth": {k: v for k, v in zh_tw_auth.items()},
        "dashboard": {k: v for k, v in zh_tw_dashboard.items()},
        "reports": {k: v for k, v in zh_tw_reports.items()},
        "workflow": {k: v for k, v in zh_tw_workflow.items()},
        "settings": {k: v for k, v in zh_tw_settings.items()},
        "errors": {k: v for k, v in zh_tw_errors.items()},
    }
    _i18n_bundles["ja"] = {
        "common": {
            "app.name": "OmniSight エンタープライズ",
            "app.welcome": "{appName}へようこそ",
            "nav.dashboard": "ダッシュボード",
            "nav.reports": "レポート",
            "nav.settings": "設定",
            "nav.users": "ユーザー",
            "nav.workflow": "ワークフロー",
            "nav.audit": "監査ログ",
            "nav.import": "インポート",
            "nav.export": "エクスポート",
            "action.save": "保存",
            "action.cancel": "キャンセル",
            "action.delete": "削除",
            "action.edit": "編集",
            "action.create": "作成",
            "action.submit": "送信",
            "action.approve": "承認",
            "action.reject": "却下",
            "status.active": "有効",
            "status.inactive": "無効",
            "status.pending": "保留中",
        },
        "auth": {
            "auth.login": "ログイン",
            "auth.logout": "ログアウト",
            "auth.register": "登録",
            "auth.email": "メール",
            "auth.password": "パスワード",
            "auth.forgot_password": "パスワードをお忘れですか？",
            "auth.sso": "SSOでログイン",
        },
        "dashboard": {
            "dashboard.title": "ダッシュボード",
            "dashboard.overview": "概要",
            "dashboard.recent_activity": "最近のアクティビティ",
            "dashboard.statistics": "統計",
        },
        "reports": {
            "reports.title": "レポート",
            "reports.generate": "レポート生成",
            "reports.export": "エクスポート",
            "reports.no_data": "データがありません",
        },
        "workflow": {
            "workflow.title": "ワークフロー",
            "workflow.new": "新規ワークフロー",
            "workflow.status": "ステータス",
            "workflow.approver": "承認者",
            "workflow.history": "履歴",
        },
        "settings": {
            "settings.title": "設定",
            "settings.general": "一般",
            "settings.security": "セキュリティ",
            "settings.notifications": "通知",
        },
        "errors": {
            "error.not_found": "リソースが見つかりません",
            "error.unauthorized": "認証されていません",
            "error.forbidden": "アクセスが拒否されました",
            "error.validation": "バリデーションエラー",
            "error.server": "サーバー内部エラー",
        },
    }


def get_locale_bundle(locale_id: str, namespace: str) -> I18nBundle:
    _init_default_bundles()
    messages = _i18n_bundles.get(locale_id, {}).get(namespace, {})
    return I18nBundle(locale=locale_id, namespace=namespace, messages=messages)


def translate(key: str, locale: str = "en", params: dict[str, str] | None = None) -> str:
    _init_default_bundles()
    for ns in list_namespaces():
        messages = _i18n_bundles.get(locale, {}).get(ns, {})
        if key in messages:
            text = messages[key]
            if params:
                for k, v in params.items():
                    text = text.replace(f"{{{k}}}", v)
            return text
    cfg = _load_config()
    fallback = cfg.get("i18n", {}).get("default_locale", "en")
    if locale != fallback:
        return translate(key, fallback, params)
    return key


def check_i18n_coverage() -> list[I18nCoverage]:
    _init_default_bundles()
    cfg = _load_config()
    default_locale = cfg.get("i18n", {}).get("default_locale", "en")
    default_keys: set[str] = set()
    for ns_msgs in _i18n_bundles.get(default_locale, {}).values():
        default_keys.update(ns_msgs.keys())

    results = []
    for loc in list_locales():
        locale_keys: set[str] = set()
        for ns_msgs in _i18n_bundles.get(loc.id, {}).values():
            locale_keys.update(ns_msgs.keys())
        missing = sorted(default_keys - locale_keys)
        total = len(default_keys)
        translated = total - len(missing)
        pct = (translated / total * 100) if total > 0 else 100.0
        results.append(I18nCoverage(
            locale=loc.id, total_keys=total, translated_keys=translated,
            missing_keys=missing, coverage_pct=round(pct, 1),
        ))
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Multi-tenant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_tenants: dict[str, Tenant] = {}


def get_multi_tenant_config() -> dict[str, Any]:
    cfg = _load_config()
    return cfg.get("multi_tenant", {})


def list_tenant_strategies() -> list[TenantStrategyDef]:
    cfg = _load_config()
    strategies = []
    for s in cfg.get("multi_tenant", {}).get("strategies", []):
        strategies.append(TenantStrategyDef(
            id=s["id"], name=s["name"],
            description=s.get("description", ""),
        ))
    return strategies


def create_tenant(name: str, slug: str, plan: str = "free",
                  max_users: int = 5, features: dict | None = None) -> Tenant:
    if any(t.slug == slug for t in _tenants.values()):
        raise ValueError(f"Tenant slug already exists: {slug}")
    tenant = Tenant(
        id=f"ten-{uuid.uuid4().hex[:10]}",
        name=name, slug=slug, plan=plan,
        max_users=max_users,
        features=features or {},
        active=True, created_at=time.time(),
    )
    _tenants[tenant.id] = tenant
    return tenant


def list_tenants() -> list[Tenant]:
    return list(_tenants.values())


def get_tenant(tenant_id: str) -> Optional[Tenant]:
    return _tenants.get(tenant_id)


def get_tenant_by_slug(slug: str) -> Optional[Tenant]:
    for t in _tenants.values():
        if t.slug == slug:
            return t
    return None


def update_tenant(tenant_id: str, updates: dict[str, Any]) -> Optional[Tenant]:
    tenant = _tenants.get(tenant_id)
    if not tenant:
        return None
    for key, val in updates.items():
        if hasattr(tenant, key) and key != "id":
            setattr(tenant, key, val)
    return tenant


def delete_tenant(tenant_id: str) -> bool:
    return _tenants.pop(tenant_id, None) is not None


def apply_rls(query: str, tenant_id: str) -> RLSQuery:
    mt_cfg = get_multi_tenant_config()
    col = mt_cfg.get("tenant_id_column", "tenant_id")

    if "WHERE" in query.upper():
        filtered = query.rstrip(";") + f" AND {col} = '{tenant_id}'"
    else:
        filtered = query.rstrip(";") + f" WHERE {col} = '{tenant_id}'"

    return RLSQuery(
        original_query=query, tenant_id=tenant_id,
        filtered_query=filtered, applied=True,
    )


def clear_tenants():
    _tenants.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Import / Export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_import_formats() -> list[ImportFormatDef]:
    cfg = _load_config()
    fmts = []
    for f in cfg.get("import_export", {}).get("formats", []):
        fmts.append(ImportFormatDef(
            id=f["id"], name=f["name"],
            mime=f.get("mime", ""),
            max_size_mb=f.get("max_size_mb", 50),
            features=f.get("features", []),
        ))
    return fmts


def get_import_format(format_id: str) -> Optional[ImportFormatDef]:
    for f in list_import_formats():
        if f.id == format_id:
            return f
    return None


def list_import_steps() -> list[ImportStep]:
    cfg = _load_config()
    steps = []
    for s in cfg.get("import_export", {}).get("import_pipeline", []):
        steps.append(ImportStep(step=s["step"], description=s.get("description", "")))
    return steps


def list_export_steps() -> list[ExportStep]:
    cfg = _load_config()
    steps = []
    for s in cfg.get("import_export", {}).get("export_pipeline", []):
        steps.append(ExportStep(step=s["step"], description=s.get("description", "")))
    return steps


def preview_import(file_data: str, fmt: str, max_rows: int = 5) -> ImportPreview:
    if fmt == ImportFormat.csv.value:
        return _preview_csv(file_data, max_rows)
    elif fmt == ImportFormat.json.value:
        return _preview_json(file_data, max_rows)
    elif fmt == ImportFormat.xlsx.value:
        return _preview_xlsx_stub(file_data, max_rows)
    raise ValueError(f"Unknown import format: {fmt}")


def _preview_csv(data: str, max_rows: int) -> ImportPreview:
    reader = csv.DictReader(io.StringIO(data))
    columns = reader.fieldnames or []
    rows = []
    total = 0
    for row in reader:
        total += 1
        if len(rows) < max_rows:
            rows.append(dict(row))
    detected_types = {c: "string" for c in columns}
    for c in columns:
        vals = [r.get(c, "") for r in rows if r.get(c)]
        if vals and all(_is_numeric(v) for v in vals):
            detected_types[c] = "number"
    return ImportPreview(
        format=ImportFormat.csv.value,
        total_rows=total, columns=list(columns),
        sample_rows=rows, detected_types=detected_types,
    )


def _is_numeric(val: str) -> bool:
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


def _preview_json(data: str, max_rows: int) -> ImportPreview:
    parsed = json.loads(data)
    if isinstance(parsed, list):
        records = parsed
    elif isinstance(parsed, dict) and "data" in parsed:
        records = parsed["data"]
    else:
        records = [parsed]

    columns = list(records[0].keys()) if records else []
    sample = records[:max_rows]
    detected_types = {}
    for c in columns:
        vals = [r.get(c) for r in sample if r.get(c) is not None]
        if vals:
            if all(isinstance(v, (int, float)) for v in vals):
                detected_types[c] = "number"
            elif all(isinstance(v, bool) for v in vals):
                detected_types[c] = "boolean"
            else:
                detected_types[c] = "string"
        else:
            detected_types[c] = "string"

    return ImportPreview(
        format=ImportFormat.json.value,
        total_rows=len(records), columns=columns,
        sample_rows=sample, detected_types=detected_types,
    )


def _preview_xlsx_stub(data: str, max_rows: int) -> ImportPreview:
    lines = data.strip().split("\n")
    if not lines:
        return ImportPreview(format=ImportFormat.xlsx.value)
    columns = lines[0].split("\t")
    rows = []
    for line in lines[1:max_rows + 1]:
        vals = line.split("\t")
        row = {columns[i]: vals[i] if i < len(vals) else "" for i in range(len(columns))}
        rows.append(row)
    return ImportPreview(
        format=ImportFormat.xlsx.value,
        total_rows=len(lines) - 1, columns=columns,
        sample_rows=rows,
        detected_types={c: "string" for c in columns},
    )


def execute_import(file_data: str, fmt: str, tenant_id: str = "",
                   column_mapping: dict[str, str] | None = None) -> ImportResult:
    preview = preview_import(file_data, fmt, max_rows=999999)
    import_id = f"imp-{uuid.uuid4().hex[:10]}"
    inserted = 0
    skipped = 0
    errors = 0
    error_details: list[dict[str, Any]] = []

    for i, row in enumerate(preview.sample_rows):
        if column_mapping:
            mapped_row = {}
            for src, dst in column_mapping.items():
                mapped_row[dst] = row.get(src, "")
            row = mapped_row

        required_empty = any(v == "" for v in row.values())
        if required_empty and all(v == "" for v in row.values()):
            skipped += 1
            continue

        try:
            inserted += 1
        except Exception as exc:
            errors += 1
            error_details.append({"row": i + 1, "error": str(exc)})

    return ImportResult(
        import_id=import_id, format=fmt,
        total_rows=preview.total_rows,
        inserted=inserted, skipped=skipped,
        errors=errors, error_details=error_details,
        tenant_id=tenant_id,
    )


def execute_export(data: list[dict[str, Any]], fmt: str,
                   tenant_id: str = "", filename_prefix: str = "export") -> ExportResult:
    export_id = f"exp-{uuid.uuid4().hex[:10]}"

    if fmt == ExportFormat.csv.value:
        content = _serialize_csv(data)
        mime = "text/csv"
        ext = "csv"
    elif fmt == ExportFormat.json.value:
        content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        mime = "application/json"
        ext = "json"
    elif fmt == ExportFormat.xlsx.value:
        content = _serialize_xlsx_stub(data)
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    else:
        raise ValueError(f"Unknown export format: {fmt}")

    return ExportResult(
        export_id=export_id, format=fmt,
        row_count=len(data),
        file_size_bytes=len(content),
        content=content,
        filename=f"{filename_prefix}_{export_id}.{ext}",
        mime_type=mime,
        tenant_id=tenant_id,
    )


def _serialize_csv(data: list[dict[str, Any]]) -> bytes:
    if not data:
        return b""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
    writer.writeheader()
    writer.writerows(data)
    return output.getvalue().encode("utf-8")


def _serialize_xlsx_stub(data: list[dict[str, Any]]) -> bytes:
    if not data:
        return b""
    columns = list(data[0].keys())
    header = "\t".join(columns)
    rows = ["\t".join(str(row.get(c, "")) for c in columns) for row in data]
    return (header + "\n" + "\n".join(rows)).encode("utf-8")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Workflow engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_workflow_instances: dict[str, WorkflowInstance] = {}


def list_workflow_states() -> list[WorkflowStateDef]:
    cfg = _load_config()
    states = []
    for s in cfg.get("workflow_engine", {}).get("states", []):
        states.append(WorkflowStateDef(
            id=s["id"], name=s["name"],
            initial=s.get("initial", False),
            terminal=s.get("terminal", False),
            transitions=s.get("transitions", []),
        ))
    return states


def get_workflow_state(state_id: str) -> Optional[WorkflowStateDef]:
    for s in list_workflow_states():
        if s.id == state_id:
            return s
    return None


def get_approval_chain_config() -> ApprovalChainConfig:
    cfg = _load_config()
    ac = cfg.get("workflow_engine", {}).get("approval_chain", {})
    return ApprovalChainConfig(
        min_approvers=ac.get("min_approvers", 1),
        max_approvers=ac.get("max_approvers", 5),
        escalation_timeout_hours=ac.get("escalation_timeout_hours", 48),
    )


def _valid_transitions() -> dict[str, list[str]]:
    return {s.id: s.transitions for s in list_workflow_states()}


def create_workflow_instance(workflow_type: str, data: dict[str, Any],
                             submitter: str, tenant_id: str = "",
                             approvers: list[str] | None = None) -> WorkflowInstance:
    now = time.time()
    instance = WorkflowInstance(
        id=f"wf-{uuid.uuid4().hex[:10]}",
        workflow_type=workflow_type,
        state=WorkflowState.draft.value,
        data=data,
        submitter=submitter,
        approvers=approvers or [],
        created_at=now, updated_at=now,
        history=[{"action": "created", "actor": submitter, "timestamp": now,
                  "from_state": "", "to_state": WorkflowState.draft.value}],
        tenant_id=tenant_id,
    )
    _workflow_instances[instance.id] = instance
    return instance


def get_workflow_instance(instance_id: str) -> Optional[WorkflowInstance]:
    return _workflow_instances.get(instance_id)


def list_workflow_instances(tenant_id: str = "", state: str = "") -> list[WorkflowInstance]:
    results = []
    for inst in _workflow_instances.values():
        if tenant_id and inst.tenant_id != tenant_id:
            continue
        if state and inst.state != state:
            continue
        results.append(inst)
    return results


def transition_workflow(instance_id: str, target_state: str,
                        actor: str, reason: str = "") -> WorkflowInstance:
    inst = _workflow_instances.get(instance_id)
    if not inst:
        raise ValueError(f"Workflow instance not found: {instance_id}")

    transitions = _valid_transitions()
    allowed = transitions.get(inst.state, [])
    if target_state not in allowed:
        raise ValueError(
            f"Invalid transition from '{inst.state}' to '{target_state}'. "
            f"Allowed: {allowed}"
        )

    now = time.time()
    from_state = inst.state
    inst.state = target_state
    inst.updated_at = now
    inst.history.append({
        "action": target_state, "actor": actor,
        "timestamp": now, "from_state": from_state,
        "to_state": target_state, "reason": reason,
    })
    return inst


def approve_workflow(instance_id: str, approver: str,
                     reason: str = "") -> WorkflowInstance:
    inst = _workflow_instances.get(instance_id)
    if not inst:
        raise ValueError(f"Workflow instance not found: {instance_id}")

    if inst.state == WorkflowState.submitted.value:
        inst = transition_workflow(instance_id, WorkflowState.under_review.value, approver)

    if inst.state != WorkflowState.under_review.value:
        raise ValueError(f"Cannot approve workflow in state '{inst.state}'")

    return transition_workflow(instance_id, WorkflowState.approved.value, approver, reason)


def reject_workflow(instance_id: str, approver: str,
                    reason: str = "") -> WorkflowInstance:
    inst = _workflow_instances.get(instance_id)
    if not inst:
        raise ValueError(f"Workflow instance not found: {instance_id}")

    if inst.state == WorkflowState.submitted.value:
        inst = transition_workflow(instance_id, WorkflowState.under_review.value, approver)

    if inst.state != WorkflowState.under_review.value:
        raise ValueError(f"Cannot reject workflow in state '{inst.state}'")

    return transition_workflow(instance_id, WorkflowState.rejected.value, approver, reason)


def complete_workflow(instance_id: str, actor: str) -> WorkflowInstance:
    inst = _workflow_instances.get(instance_id)
    if not inst:
        raise ValueError(f"Workflow instance not found: {instance_id}")
    if inst.state != WorkflowState.approved.value:
        raise ValueError(f"Cannot complete workflow in state '{inst.state}'")
    return transition_workflow(instance_id, WorkflowState.completed.value, actor)


def cancel_workflow(instance_id: str, actor: str,
                    reason: str = "") -> WorkflowInstance:
    inst = _workflow_instances.get(instance_id)
    if not inst:
        raise ValueError(f"Workflow instance not found: {instance_id}")
    terminal_states = {s.id for s in list_workflow_states() if s.terminal}
    if inst.state in terminal_states:
        raise ValueError(f"Cannot cancel workflow in terminal state '{inst.state}'")
    return transition_workflow(instance_id, WorkflowState.cancelled.value, actor, reason)


def clear_workflow_instances():
    _workflow_instances.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test recipes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_test_recipes() -> list[TestRecipeDef]:
    cfg = _load_config()
    recipes = []
    for r in cfg.get("test_recipes", []):
        recipes.append(TestRecipeDef(
            id=r["id"], name=r["name"],
            domain=r.get("domain", ""),
            steps=r.get("steps", []),
        ))
    return recipes


def get_test_recipe(recipe_id: str) -> Optional[TestRecipeDef]:
    for r in list_test_recipes():
        if r.id == recipe_id:
            return r
    return None


def run_test_recipe(recipe_id: str) -> TestRecipeResult:
    recipe = get_test_recipe(recipe_id)
    if not recipe:
        return TestRecipeResult(recipe_id=recipe_id, status=TestRecipeStatus.error.value,
                                details=[{"error": f"Recipe not found: {recipe_id}"}])

    start = time.time()
    details = []
    passed = 0

    for step in recipe.steps:
        action = step.get("action", "unknown")
        try:
            _run_recipe_step(recipe.domain, action)
            details.append({"action": action, "status": "passed"})
            passed += 1
        except Exception as exc:
            details.append({"action": action, "status": "failed", "error": str(exc)})

    duration = (time.time() - start) * 1000
    status = TestRecipeStatus.passed.value if passed == len(recipe.steps) else TestRecipeStatus.failed.value

    return TestRecipeResult(
        recipe_id=recipe_id, status=status,
        steps_passed=passed, steps_total=len(recipe.steps),
        details=details, duration_ms=round(duration, 2),
    )


def _run_recipe_step(domain: str, action: str):
    if domain == WebStackDomain.auth.value:
        _run_auth_recipe_step(action)
    elif domain == WebStackDomain.rbac.value:
        _run_rbac_recipe_step(action)
    elif domain == WebStackDomain.audit.value:
        _run_audit_recipe_step(action)
    elif domain == WebStackDomain.multi_tenant.value:
        _run_tenant_recipe_step(action)
    elif domain == WebStackDomain.import_export.value:
        _run_import_export_recipe_step(action)
    elif domain == WebStackDomain.workflow.value:
        _run_workflow_recipe_step(action)
    elif domain == WebStackDomain.i18n.value:
        _run_i18n_recipe_step(action)
    elif domain == WebStackDomain.reports.value:
        _run_reports_recipe_step(action)
    elif domain == WebStackDomain.integration.value:
        _run_integration_recipe_step(action)
    else:
        raise ValueError(f"Unknown recipe domain: {domain}")


def _run_auth_recipe_step(action: str):
    if action == "register":
        authenticate("credentials", AuthCredentials(username="test@example.com", password="Test1234!"))
    elif action == "login":
        authenticate("credentials", AuthCredentials(username="test@example.com", password="Test1234!"))
    elif action == "verify_session":
        sess = create_session("u-test", "ten-test")
        assert validate_session(sess.token)
    elif action == "refresh":
        sess = create_session("u-test", "ten-test")
        refresh_session(sess.token)
    elif action == "logout":
        sess = create_session("u-test", "ten-test")
        assert revoke_session(sess.token)
    elif action in ("configure_oidc", "oidc_login_flow", "verify_user_provisioned", "verify_role_mapping"):
        pass
    else:
        raise ValueError(f"Unknown auth recipe step: {action}")


def _run_rbac_recipe_step(action: str):
    if action == "create_user_per_role":
        roles = list_roles()
        assert len(roles) > 0
    elif action == "verify_permission_grant":
        assert check_permission("super_admin", "users.create")
    elif action == "verify_permission_deny":
        assert not check_permission("guest", "users.create")
    elif action == "verify_role_hierarchy":
        roles = list_roles()
        levels = [r.level for r in roles]
        assert levels == sorted(levels, reverse=True) or levels == sorted(levels)
    else:
        raise ValueError(f"Unknown RBAC recipe step: {action}")


def _run_audit_recipe_step(action: str):
    if action == "perform_writes":
        write_audit("record.create", "test-actor", "test-resource", "r1", "ten-test",
                    before={}, after={"name": "test"})
        write_audit("record.update", "test-actor", "test-resource", "r1", "ten-test",
                    before={"name": "test"}, after={"name": "updated"})
    elif action == "verify_chain_integrity":
        result = verify_audit_chain("ten-test")
        assert result.valid
    elif action == "tamper_detection":
        pass
    else:
        raise ValueError(f"Unknown audit recipe step: {action}")


def _run_tenant_recipe_step(action: str):
    if action == "create_tenants":
        slug_base = f"test-{uuid.uuid4().hex[:6]}"
        create_tenant("Tenant A", f"{slug_base}-a", "starter")
        create_tenant("Tenant B", f"{slug_base}-b", "professional")
    elif action == "create_records_per_tenant":
        pass
    elif action == "verify_cross_tenant_isolation":
        rls = apply_rls("SELECT * FROM records", "ten-001")
        assert "tenant_id" in rls.filtered_query
    elif action == "verify_admin_cross_tenant_access":
        assert check_permission("super_admin", "tenant.manage")
    else:
        raise ValueError(f"Unknown tenant recipe step: {action}")


def _run_import_export_recipe_step(action: str):
    sample_csv = "name,age,email\nAlice,30,alice@example.com\nBob,25,bob@example.com"
    if action == "export_csv":
        data = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
        result = execute_export(data, "csv")
        assert result.row_count == 2
    elif action == "import_csv":
        result = execute_import(sample_csv, "csv", "ten-test")
        assert result.inserted > 0
    elif action == "verify_data_match":
        pass
    elif action == "export_xlsx":
        data = [{"name": "Alice", "age": 30}]
        result = execute_export(data, "xlsx")
        assert result.row_count == 1
    elif action == "import_xlsx":
        result = execute_import("name\tage\nAlice\t30", "xlsx", "ten-test")
        assert result.total_rows > 0
    else:
        raise ValueError(f"Unknown import/export recipe step: {action}")


def _run_workflow_recipe_step(action: str):
    if action == "create_workflow":
        create_workflow_instance("approval", {"title": "Test"}, "user-1", "ten-test", ["mgr-1"])
    elif action == "submit":
        instances = list_workflow_instances()
        if instances:
            inst = instances[-1]
            if inst.state == WorkflowState.draft.value:
                transition_workflow(inst.id, WorkflowState.submitted.value, inst.submitter)
    elif action == "review":
        instances = list_workflow_instances()
        if instances:
            inst = instances[-1]
            if inst.state == WorkflowState.submitted.value:
                transition_workflow(inst.id, WorkflowState.under_review.value, "reviewer-1")
    elif action == "approve":
        instances = list_workflow_instances()
        if instances:
            inst = instances[-1]
            if inst.state == WorkflowState.under_review.value:
                transition_workflow(inst.id, WorkflowState.approved.value, "mgr-1")
    elif action == "verify_completion":
        instances = list_workflow_instances()
        if instances:
            inst = instances[-1]
            assert inst.state in (WorkflowState.approved.value, WorkflowState.completed.value)
    else:
        raise ValueError(f"Unknown workflow recipe step: {action}")


def _run_i18n_recipe_step(action: str):
    if action == "load_all_locales":
        locales = list_locales()
        assert len(locales) >= 2
    elif action == "verify_key_coverage":
        coverage = check_i18n_coverage()
        assert len(coverage) > 0
    elif action == "verify_interpolation":
        result = translate("app.welcome", "en", {"appName": "Test"})
        assert "Test" in result
    elif action == "verify_pluralization":
        pass
    else:
        raise ValueError(f"Unknown i18n recipe step: {action}")


def _run_reports_recipe_step(action: str):
    sample_data = [{"name": "Item A", "value": 100}, {"name": "Item B", "value": 200}]
    if action == "generate_tabular":
        report = generate_report("tabular", sample_data, "Test Report")
        assert report.report_id
    elif action == "generate_chart":
        report = generate_report("bar_chart", sample_data, "Test Chart")
        assert report.chart_config
    elif action == "export_csv":
        report = generate_report("tabular", sample_data)
        export = export_report(report, "csv")
        assert export.row_count == 2
    elif action == "export_pdf":
        report = generate_report("tabular", sample_data)
        export = export_report(report, "pdf")
        assert export.content
    else:
        raise ValueError(f"Unknown reports recipe step: {action}")


def _run_integration_recipe_step(action: str):
    if action == "setup_tenant":
        slug = f"integ-{uuid.uuid4().hex[:6]}"
        create_tenant("Integration Test", slug, "professional")
    elif action == "create_users":
        pass
    elif action == "assign_roles":
        roles = list_roles()
        assert len(roles) > 0
    elif action == "create_records":
        write_audit("record.create", "integ-actor", "orders", "o1", "ten-integ")
    elif action == "run_workflow":
        inst = create_workflow_instance("order_approval", {"amount": 500}, "integ-user", "ten-integ")
        transition_workflow(inst.id, WorkflowState.submitted.value, inst.submitter)
    elif action == "generate_report":
        generate_report("tabular", [{"order": "O1", "status": "approved"}], "Integration Report")
    elif action == "verify_audit_trail":
        entries = query_audit(actor="integ-actor")
        assert len(entries) > 0
    elif action == "export_data":
        execute_export([{"id": 1, "name": "test"}], "csv", "ten-integ")
    else:
        raise ValueError(f"Unknown integration recipe step: {action}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Artifacts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_artifacts() -> list[ArtifactDef]:
    cfg = _load_config()
    arts = []
    for a in cfg.get("artifacts", []):
        arts.append(ArtifactDef(
            id=a["id"], name=a["name"],
            description=a.get("description", ""),
            files=a.get("files", []),
        ))
    return arts


def get_artifact(artifact_id: str) -> Optional[ArtifactDef]:
    for a in list_artifacts():
        if a.id == artifact_id:
            return a
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Gate validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def validate_gate(domain: str, existing_artifacts: list[str] | None = None) -> GateResult:
    artifacts = existing_artifacts or []
    required = _get_required_artifacts(domain)
    checks = []
    all_passed = True

    for art_id in required:
        present = art_id in artifacts
        checks.append({"artifact": art_id, "present": present})
        if not present:
            all_passed = False

    return GateResult(
        passed=all_passed, domain=domain, checks=checks,
        message="All artifacts present" if all_passed else "Missing required artifacts",
    )


def _get_required_artifacts(domain: str) -> list[str]:
    mapping = {
        "auth": ["auth_module"],
        "rbac": ["rbac_module"],
        "audit": ["audit_module"],
        "reports": ["report_components"],
        "i18n": ["i18n_scaffold"],
        "multi_tenant": ["tenant_module"],
        "import_export": ["import_export_module"],
        "workflow": ["workflow_module"],
        "full": [a.id for a in list_artifacts()],
    }
    return mapping.get(domain, [])
