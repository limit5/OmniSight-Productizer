"""C21 — L4-CORE-21 Enterprise web stack pattern endpoints (#242).

REST endpoints for Auth, RBAC, Audit, Reports, i18n, Multi-tenant,
Import/Export, and Workflow engine — the reference template for
all SW-WEB-* tracks.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import enterprise_web_stack as ews

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/enterprise", tags=["enterprise-web-stack"])


# ── Request models ───────────────────────────────────────────────────

class AuthenticateRequest(BaseModel):
    provider_id: str = Field(..., description="Auth provider ID")
    username: str = Field(default="", description="Username (for credentials/LDAP)")
    password: str = Field(default="", description="Password (for credentials/LDAP)")
    provider_data: dict[str, Any] = Field(default_factory=dict, description="Extra provider data (SAML/OIDC)")


class CreateSessionRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    tenant_id: str = Field(default="", description="Tenant ID")


class SessionTokenRequest(BaseModel):
    token: str = Field(..., description="Session token")


class PolicyCheckRequest(BaseModel):
    user_role: str = Field(..., description="User's role ID")
    resource: str = Field(..., description="Resource name")
    action: str = Field(..., description="Action name")


class AuditWriteRequest(BaseModel):
    action: str = Field(..., description="Audit action ID")
    actor: str = Field(..., description="Actor ID")
    resource: str = Field(default="", description="Resource type")
    resource_id: str = Field(default="", description="Resource ID")
    tenant_id: str = Field(default="", description="Tenant ID")
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class AuditQueryRequest(BaseModel):
    action: str = Field(default="")
    actor: str = Field(default="")
    tenant_id: str = Field(default="")
    since: float = Field(default=0.0)
    limit: int = Field(default=100, ge=1, le=1000)


class GenerateReportRequest(BaseModel):
    report_type: str = Field(..., description="Report type ID")
    data: list[dict[str, Any]] = Field(default_factory=list)
    title: str = Field(default="")
    options: dict[str, Any] = Field(default_factory=dict)


class ExportReportRequest(BaseModel):
    report_type: str = Field(..., description="Report type to generate + export")
    data: list[dict[str, Any]] = Field(default_factory=list)
    title: str = Field(default="")
    format: str = Field(..., description="Export format (csv, xlsx, pdf, json)")


class TranslateRequest(BaseModel):
    key: str = Field(..., description="i18n key")
    locale: str = Field(default="en")
    params: dict[str, str] = Field(default_factory=dict)


class CreateTenantRequest(BaseModel):
    name: str = Field(..., description="Tenant name")
    slug: str = Field(..., description="Tenant slug (URL-safe, unique)")
    plan: str = Field(default="free")
    max_users: int = Field(default=5)
    features: dict[str, Any] = Field(default_factory=dict)


class UpdateTenantRequest(BaseModel):
    name: str | None = Field(default=None)
    plan: str | None = Field(default=None)
    max_users: int | None = Field(default=None)
    features: dict[str, Any] | None = Field(default=None)
    active: bool | None = Field(default=None)


class RLSQueryRequest(BaseModel):
    query: str = Field(..., description="SQL query to apply RLS to")
    tenant_id: str = Field(..., description="Tenant ID for RLS filter")


class ImportPreviewRequest(BaseModel):
    file_data: str = Field(..., description="File content as string")
    format: str = Field(..., description="Import format (csv, xlsx, json)")
    max_rows: int = Field(default=5)


class ImportExecuteRequest(BaseModel):
    file_data: str = Field(..., description="File content as string")
    format: str = Field(..., description="Import format (csv, xlsx, json)")
    tenant_id: str = Field(default="")
    column_mapping: dict[str, str] = Field(default_factory=dict)


class ExportExecuteRequest(BaseModel):
    data: list[dict[str, Any]] = Field(default_factory=list)
    format: str = Field(..., description="Export format (csv, xlsx, json)")
    tenant_id: str = Field(default="")
    filename_prefix: str = Field(default="export")


class CreateWorkflowRequest(BaseModel):
    workflow_type: str = Field(..., description="Workflow type")
    data: dict[str, Any] = Field(default_factory=dict)
    submitter: str = Field(..., description="Submitter user ID")
    tenant_id: str = Field(default="")
    approvers: list[str] = Field(default_factory=list)


class TransitionWorkflowRequest(BaseModel):
    target_state: str = Field(..., description="Target state")
    actor: str = Field(..., description="Actor performing the transition")
    reason: str = Field(default="")


class ApproveRejectRequest(BaseModel):
    approver: str = Field(..., description="Approver user ID")
    reason: str = Field(default="")


class GateValidateRequest(BaseModel):
    domain: str = Field(..., description="Domain to validate")
    existing_artifacts: list[str] = Field(default_factory=list)


class TestRecipeRunRequest(BaseModel):
    recipe_id: str = Field(..., description="Test recipe ID")


# ── Auth endpoints ───────────────────────────────────────────────────

@router.get("/auth/providers")
async def get_auth_providers():
    providers = ews.list_auth_providers()
    return [asdict(p) for p in providers]


@router.get("/auth/providers/{provider_id}")
async def get_auth_provider(provider_id: str):
    p = ews.get_auth_provider(provider_id)
    if not p:
        raise HTTPException(404, f"Auth provider not found: {provider_id}")
    return asdict(p)


@router.post("/auth/authenticate")
async def authenticate(req: AuthenticateRequest):
    creds = ews.AuthCredentials(
        username=req.username, password=req.password,
        provider_data=req.provider_data,
    )
    result = ews.authenticate(req.provider_id, creds)
    return asdict(result)


@router.post("/auth/session")
async def create_session(req: CreateSessionRequest):
    sess = ews.create_session(req.user_id, req.tenant_id)
    return asdict(sess)


@router.post("/auth/session/validate")
async def validate_session(req: SessionTokenRequest):
    valid = ews.validate_session(req.token)
    return {"valid": valid}


@router.post("/auth/session/refresh")
async def refresh_session(req: SessionTokenRequest):
    sess = ews.refresh_session(req.token)
    if not sess:
        raise HTTPException(400, "Session cannot be refreshed")
    return asdict(sess)


@router.post("/auth/session/revoke")
async def revoke_session(req: SessionTokenRequest):
    ok = ews.revoke_session(req.token)
    return {"revoked": ok}


@router.get("/auth/session-config")
async def get_session_config():
    return ews.get_session_config()


# ── RBAC endpoints ───────────────────────────────────────────────────

@router.get("/rbac/roles")
async def get_roles():
    roles = ews.list_roles()
    return [asdict(r) for r in roles]


@router.get("/rbac/roles/{role_id}")
async def get_role(role_id: str):
    r = ews.get_role(role_id)
    if not r:
        raise HTTPException(404, f"Role not found: {role_id}")
    return asdict(r)


@router.get("/rbac/permissions")
async def get_permissions():
    perms = ews.list_permissions()
    return [asdict(p) for p in perms]


@router.get("/rbac/roles/{role_id}/permissions")
async def get_role_perms(role_id: str):
    perms = ews.get_role_permissions(role_id)
    return {"role_id": role_id, "permissions": perms}


@router.get("/rbac/check/{role_id}/{permission_id}")
async def check_permission(role_id: str, permission_id: str):
    allowed = ews.check_permission(role_id, permission_id)
    return {"role_id": role_id, "permission_id": permission_id, "allowed": allowed}


@router.post("/rbac/enforce")
async def enforce_policy(req: PolicyCheckRequest):
    result = ews.enforce_policy(req.user_role, req.resource, req.action)
    return asdict(result)


# ── Audit endpoints ──────────────────────────────────────────────────

@router.get("/audit/actions")
async def get_audit_actions():
    actions = ews.list_audit_actions()
    return [asdict(a) for a in actions]


@router.get("/audit/config")
async def get_audit_config():
    return ews.get_audit_config()


@router.post("/audit/write")
async def write_audit_entry(req: AuditWriteRequest):
    entry = ews.write_audit(
        action=req.action, actor=req.actor,
        resource=req.resource, resource_id=req.resource_id,
        tenant_id=req.tenant_id, before=req.before, after=req.after,
    )
    return asdict(entry)


@router.post("/audit/query")
async def query_audit(req: AuditQueryRequest):
    entries = ews.query_audit(
        action=req.action, actor=req.actor,
        tenant_id=req.tenant_id, since=req.since, limit=req.limit,
    )
    return [asdict(e) for e in entries]


@router.post("/audit/verify")
async def verify_audit_chain(tenant_id: str = ""):
    result = ews.verify_audit_chain(tenant_id)
    return asdict(result)


# ── Reports endpoints ────────────────────────────────────────────────

@router.get("/reports/types")
async def get_report_types():
    types = ews.list_report_types()
    return [asdict(t) for t in types]


@router.get("/reports/types/{type_id}")
async def get_report_type(type_id: str):
    t = ews.get_report_type(type_id)
    if not t:
        raise HTTPException(404, f"Report type not found: {type_id}")
    return asdict(t)


@router.get("/reports/export-formats")
async def get_export_formats():
    return ews.list_export_formats()


@router.post("/reports/generate")
async def generate_report(req: GenerateReportRequest):
    try:
        report = ews.generate_report(req.report_type, req.data, req.title, req.options)
        return asdict(report)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/reports/export")
async def export_report(req: ExportReportRequest):
    try:
        report = ews.generate_report(req.report_type, req.data, req.title)
        export = ews.export_report(report, req.format)
        return {
            "format": export.format,
            "filename": export.filename,
            "mime_type": export.mime_type,
            "row_count": export.row_count,
            "file_size_bytes": len(export.content),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── i18n endpoints ───────────────────────────────────────────────────

@router.get("/i18n/locales")
async def get_locales():
    locales = ews.list_locales()
    return [asdict(loc) for loc in locales]


@router.get("/i18n/locales/{locale_id}")
async def get_locale(locale_id: str):
    loc = ews.get_locale(locale_id)
    if not loc:
        raise HTTPException(404, f"Locale not found: {locale_id}")
    return asdict(loc)


@router.get("/i18n/config")
async def get_i18n_config():
    return ews.get_i18n_config()


@router.get("/i18n/namespaces")
async def get_namespaces():
    return ews.list_namespaces()


@router.get("/i18n/bundle/{locale_id}/{namespace}")
async def get_locale_bundle(locale_id: str, namespace: str):
    bundle = ews.get_locale_bundle(locale_id, namespace)
    return asdict(bundle)


@router.post("/i18n/translate")
async def translate_key(req: TranslateRequest):
    result = ews.translate(req.key, req.locale, req.params)
    return {"key": req.key, "locale": req.locale, "translated": result}


@router.get("/i18n/coverage")
async def get_i18n_coverage():
    coverage = ews.check_i18n_coverage()
    return [asdict(c) for c in coverage]


# ── Multi-tenant endpoints ───────────────────────────────────────────

@router.get("/tenants/config")
async def get_tenant_config():
    return ews.get_multi_tenant_config()


@router.get("/tenants/strategies")
async def get_tenant_strategies():
    strategies = ews.list_tenant_strategies()
    return [asdict(s) for s in strategies]


@router.post("/tenants")
async def create_tenant(req: CreateTenantRequest):
    try:
        tenant = ews.create_tenant(
            name=req.name, slug=req.slug, plan=req.plan,
            max_users=req.max_users, features=req.features,
        )
        return asdict(tenant)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/tenants")
async def list_tenants():
    tenants = ews.list_tenants()
    return [asdict(t) for t in tenants]


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str):
    t = ews.get_tenant(tenant_id)
    if not t:
        raise HTTPException(404, f"Tenant not found: {tenant_id}")
    return asdict(t)


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, req: UpdateTenantRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    t = ews.update_tenant(tenant_id, updates)
    if not t:
        raise HTTPException(404, f"Tenant not found: {tenant_id}")
    return asdict(t)


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str):
    ok = ews.delete_tenant(tenant_id)
    if not ok:
        raise HTTPException(404, f"Tenant not found: {tenant_id}")
    return {"deleted": True}


@router.post("/tenants/rls")
async def apply_rls(req: RLSQueryRequest):
    result = ews.apply_rls(req.query, req.tenant_id)
    return asdict(result)


# ── Import/Export endpoints ──────────────────────────────────────────

@router.get("/import/formats")
async def get_import_formats():
    fmts = ews.list_import_formats()
    return [asdict(f) for f in fmts]


@router.get("/import/formats/{format_id}")
async def get_import_format(format_id: str):
    f = ews.get_import_format(format_id)
    if not f:
        raise HTTPException(404, f"Import format not found: {format_id}")
    return asdict(f)


@router.get("/import/steps")
async def get_import_steps():
    steps = ews.list_import_steps()
    return [asdict(s) for s in steps]


@router.get("/export/steps")
async def get_export_steps():
    steps = ews.list_export_steps()
    return [asdict(s) for s in steps]


@router.post("/import/preview")
async def preview_import(req: ImportPreviewRequest):
    try:
        preview = ews.preview_import(req.file_data, req.format, req.max_rows)
        return asdict(preview)
    except (ValueError, Exception) as exc:
        raise HTTPException(400, str(exc))


@router.post("/import/execute")
async def execute_import(req: ImportExecuteRequest):
    try:
        result = ews.execute_import(
            req.file_data, req.format, req.tenant_id, req.column_mapping or None,
        )
        return asdict(result)
    except (ValueError, Exception) as exc:
        raise HTTPException(400, str(exc))


@router.post("/export/execute")
async def execute_export(req: ExportExecuteRequest):
    try:
        result = ews.execute_export(req.data, req.format, req.tenant_id, req.filename_prefix)
        return {
            "export_id": result.export_id,
            "format": result.format,
            "row_count": result.row_count,
            "file_size_bytes": result.file_size_bytes,
            "filename": result.filename,
            "mime_type": result.mime_type,
            "tenant_id": result.tenant_id,
        }
    except (ValueError, Exception) as exc:
        raise HTTPException(400, str(exc))


# ── Workflow endpoints ───────────────────────────────────────────────

@router.get("/workflow/states")
async def get_workflow_states():
    states = ews.list_workflow_states()
    return [asdict(s) for s in states]


@router.get("/workflow/states/{state_id}")
async def get_workflow_state(state_id: str):
    s = ews.get_workflow_state(state_id)
    if not s:
        raise HTTPException(404, f"Workflow state not found: {state_id}")
    return asdict(s)


@router.get("/workflow/approval-config")
async def get_approval_config():
    config = ews.get_approval_chain_config()
    return asdict(config)


@router.post("/workflow/instances")
async def create_workflow(req: CreateWorkflowRequest):
    inst = ews.create_workflow_instance(
        workflow_type=req.workflow_type, data=req.data,
        submitter=req.submitter, tenant_id=req.tenant_id,
        approvers=req.approvers,
    )
    return asdict(inst)


@router.get("/workflow/instances")
async def list_workflows(tenant_id: str = "", state: str = ""):
    instances = ews.list_workflow_instances(tenant_id, state)
    return [asdict(i) for i in instances]


@router.get("/workflow/instances/{instance_id}")
async def get_workflow(instance_id: str):
    inst = ews.get_workflow_instance(instance_id)
    if not inst:
        raise HTTPException(404, f"Workflow instance not found: {instance_id}")
    return asdict(inst)


@router.post("/workflow/instances/{instance_id}/transition")
async def transition_workflow(instance_id: str, req: TransitionWorkflowRequest):
    try:
        inst = ews.transition_workflow(instance_id, req.target_state, req.actor, req.reason)
        return asdict(inst)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/workflow/instances/{instance_id}/approve")
async def approve_workflow(instance_id: str, req: ApproveRejectRequest):
    try:
        inst = ews.approve_workflow(instance_id, req.approver, req.reason)
        return asdict(inst)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/workflow/instances/{instance_id}/reject")
async def reject_workflow(instance_id: str, req: ApproveRejectRequest):
    try:
        inst = ews.reject_workflow(instance_id, req.approver, req.reason)
        return asdict(inst)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/workflow/instances/{instance_id}/complete")
async def complete_workflow(instance_id: str, req: ApproveRejectRequest):
    try:
        inst = ews.complete_workflow(instance_id, req.approver)
        return asdict(inst)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/workflow/instances/{instance_id}/cancel")
async def cancel_workflow(instance_id: str, req: ApproveRejectRequest):
    try:
        inst = ews.cancel_workflow(instance_id, req.approver, req.reason)
        return asdict(inst)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── Test recipes ─────────────────────────────────────────────────────

@router.get("/test-recipes")
async def get_test_recipes():
    recipes = ews.list_test_recipes()
    return [asdict(r) for r in recipes]


@router.get("/test-recipes/{recipe_id}")
async def get_test_recipe(recipe_id: str):
    r = ews.get_test_recipe(recipe_id)
    if not r:
        raise HTTPException(404, f"Test recipe not found: {recipe_id}")
    return asdict(r)


@router.post("/test-recipes/{recipe_id}/run")
async def run_test_recipe(recipe_id: str):
    result = ews.run_test_recipe(recipe_id)
    return asdict(result)


# ── Artifacts ────────────────────────────────────────────────────────

@router.get("/artifacts")
async def get_artifacts():
    arts = ews.list_artifacts()
    return [asdict(a) for a in arts]


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    a = ews.get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")
    return asdict(a)


# ── Gate validation ──────────────────────────────────────────────────

@router.post("/validate")
async def validate_gate(req: GateValidateRequest):
    result = ews.validate_gate(req.domain, req.existing_artifacts)
    return asdict(result)
