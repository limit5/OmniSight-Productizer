# OP-1322 tenant projects router refactor proposal

## Context

`backend/routers/tenant_projects.py` owns the tenant-scoped project REST
surface: project CRUD, archive/restore/GC, project membership management, and
cross-tenant share management. The module is intentionally explicit about its
RBAC, tenant/project scoping, audit payloads, and status-code branches, but the
same validation and side-effect patterns are repeated across many handlers.

Two small refactors would reduce coupling and make future endpoint additions
easier to review without changing the public route surface:

1. Extract common path and existence guards into private helpers. Most handlers
   repeat tenant/project/share/user id validation, tenant existence probing, and
   tenant-scoped project probing before doing endpoint-specific work. Helpers
   such as `_invalid_tenant_id_response(tenant_id)`,
   `_invalid_project_id_response(project_id)`, `_fetch_tenant_or_404(tenant_id)`,
   and `_fetch_tenant_project_or_404(project_id, tenant_id)` could preserve the
   current response bodies while keeping each route focused on its unique
   policy branch.
2. Centralize best-effort audit and dot-notation event emission. Several write
   handlers build small audit payloads, call `_audit.log()`, catch/log failures,
   and sometimes call `backend.audit_events` with the same best-effort posture.
   A narrow helper such as `_emit_project_audit(...)` and per-event wrappers for
   the existing dot-notation emitters would keep failure handling consistent
   while preserving the current action names, entity ids, payload fields, and
   log messages.

## Non-goals

- No behavior change to route paths, request schemas, response bodies,
  status-code choices, RBAC rules, tenant/project/share scoping, archive/restore
  idempotency, GC behavior, or quota oversell locking.
- No public API rename for existing helpers such as `_row_to_project_dict()`,
  `_row_to_project_member_dict()`, `_row_to_project_share_dict()`,
  `_user_can_create_project_in()`, `_user_can_manage_project_members()`, or
  `gc_archived_projects()`.
- No SQL statement, database schema, migration, transaction, advisory-lock, or
  audit-event contract changes.
- No production dependency changes.
- No code or test changes in this proposal patch set.
- No db, devops, embedded, frontend, security, tests, tooling, or out-of-scope
  domain changes.

## Follow-up implementation ticket draft

Summary: Refactor tenant project router guard and audit helpers

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1322 proposal refactors in
`backend/routers/tenant_projects.py`:

- Add private helper functions for repeated malformed-id responses and common
  tenant/project existence probes.
- Preserve current 422/404 response body text, status codes, tenant/project
  non-enumeration semantics, and RBAC-before-existence ordering.
- Add private helper functions for best-effort `_audit.log()` emission and
  existing dot-notation `backend.audit_events` calls used by project create,
  archive, and share grant flows.
- Preserve current audit action names, entity kinds, entity ids, before/after
  payload fields, actor values, warning log text, and event emitter arguments.
- Keep the refactor scoped to `backend/routers/tenant_projects.py` plus focused
  tests under the existing tenant-project test files if the extraction needs
  explicit regression coverage.

Acceptance criteria:

1. Existing tenant project route paths, schemas, status codes, and response
   payloads remain unchanged.
2. Malformed tenant/project/user/share id responses and tenant/project 404
   responses preserve their current body text and non-enumeration behavior.
3. Project create, archive, restore, member add/update/remove, share grant, and
   share revoke audit behavior remains best-effort and preserves current action
   names and payload fields.
4. The relevant tenant-project pytest coverage passes with the project venv.

Filed follow-up: pending operator/JIRA creation. This pickup is restricted to
`jira_dispatch.add_comment()` / `transition_back_to_todo()` for programmatic
JIRA writes, and the allowed helper set does not expose a general follow-up
ticket creation helper.
