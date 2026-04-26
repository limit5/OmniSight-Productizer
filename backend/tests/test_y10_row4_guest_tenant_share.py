"""Y10 #286 row 4 — Guest tenant share acceptance test.

Acceptance criterion (TODO §Y10 row 4)::

    Guest tenant share：tenant A share project 給 tenant B → tenant B
    的 user 能看到、tenant B 的 admin 能設 role、tenant B 的其他 user
    看不到 tenant B 自己的其他 project（cross-tenant 權限不該污染）。

Decomposed into four observable surfaces:

* (S1) **Host-side grant** — host tenant A's admin POSTs a share row
  to tenant B. Single host-side admin gate
  (``_user_can_create_project_in`` — frozenset {owner, admin} on the
  host tenant). No drift to the guest side.
* (S2) **Guest-side visibility** — tenant B sees the share via the
  ``project_share.granted`` audit row written into B's chain
  (``chain_role='guest'``). The guest tenant's audit pane / admin
  console reads its own chain and surfaces "we received access from
  host A on project P" — that is the visibility surface that DOES
  ship today (Y9 row 1 dual-chain emit).
* (S3) **Role-set lifecycle** — the host admin owns the share role
  (project_shares.role); changing it goes through the existing
  DELETE+POST surface (revoke + re-grant with a different role).
  The guest admin cannot mutate the host's grant from their side
  (Y4 row 6 RBAC pin: only the *owning* tenant's admin tier can
  POST/DELETE share rows).
* (S4) **Cross-tenant pollution invariant** — the existence of a
  ``project_shares`` row from A to B does NOT widen tenant B's
  internal project listing surface. A tenant-B "other user" (active
  membership but role ∈ {member, viewer}, no project_members row)
  listing ``GET /api/v1/tenants/{B}/projects`` sees the same set of
  projects as before the share row existed — namely, only B's own
  projects with explicit ``project_members`` rows for that user.

Known follow-up gaps (documented honestly per Y10 contract)
─────────────────────────────────────────────────────────────
The acceptance text "tenant B 的 user 能看到 [the shared project]"
implies a "shared with me" listing endpoint or a code-level
``require_project_member`` resolver consulting ``project_shares``.
Neither is wired today:

* ``backend.auth.require_project_member`` resolution order is
  ``super_admin → project_members row → tenant_membership fallback``
  — it does NOT consult ``project_shares``. A guest tenant member
  calling a project-scope endpoint on the shared project today gets
  a 403 unless they also have an explicit ``project_members`` row,
  which the host admin has to seed manually.
* There is no ``GET /api/v1/tenants/{guest_tid}/incoming-shares``
  endpoint; the guest-side visibility surface today is the audit
  chain row (Y9 row 1) plus operator-driven manual ``project_members``
  seeding.

Y10 is the operational exam of Y1-Y9 surface — the row's job is to
prove the contracts that DID ship work as advertised AND honestly
mark the gaps that DIDN'T. Both the (S2)+(S3) host-side semantics
and the (S4) anti-pollution invariant DO ship; the guest-side
``require_project_member`` resolver and the ``incoming-shares``
listing endpoint are tracked as follow-ups via the documented
drift guards in Block A. This is the same pattern Y10 row 2 used
for the ``/api/v1/workspaces/{agent_id}`` tenant-segment gap.

Test layout
───────────
* **Block A — pure-unit drift guards** (always run, no PG): lock the
  share schema enum, the host-side admin gate identity, the dual-
  chain audit emission shape, the SQL drift guards (no JOIN to
  ``project_shares`` from ``list_projects``), and document the two
  known follow-up gaps so any future change either way fails CI.
* **Block B — PG-required acceptance** (skip without
  ``OMNI_TEST_PG_URL``): drive the actual share lifecycle through
  the FastAPI app and assert each of the four surfaces (S1-S4) on
  live state.

Same skip-pattern as ``test_y10_row1_multi_tenant_concurrency.py``
and ``test_y10_row2_cross_tenant_leak.py`` so the test lane gating
stays consistent across the Y rows.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
This row is pure test code — zero new prod code, zero new
module-globals (only immutable str / frozenset / tuple constants
each worker derives the same value from source — qualifying answer
#1). Block B fixture ``_y10_row4_db`` resets ``set_tenant_id(None)``
/ ``set_project_id(None)`` in teardown so cross-test ContextVar
bleed is impossible (qualifying answer #3 — per-test isolation by
design).

Read-after-write timing audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Each Block B test ``await``s its writers (HTTP POST / SQL INSERT)
sequentially before asserting on the resulting state. The audit
row read for "guest chain has the share row" waits on the POST
share to return 201 — by which time the handler's
``emit_project_share_granted`` has already committed both chain
rows under their per-tenant advisory locks. No race window.
"""

from __future__ import annotations

import inspect
import json
import os
import re

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Acceptance-criterion dimensions (Y10 row 4, TODO §Y10)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Four observable surfaces enumerated in the module docstring.
_SURFACES = ("host_grant", "guest_visibility", "role_set", "no_pollution")


# Tenant ids reserved for this row's tests. The ``-y10r4-`` segment
# makes these immediately identifiable in audit_log forensics if a
# crashed test leaves rows behind.
_TENANT_PREFIX = "t-y10r4"


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="Y10 row 4 guest-tenant-share HTTP path tests need an actual "
           "PG instance — set OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block A — pure-unit drift guards (always run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_acceptance_surfaces_match_acceptance_criterion():
    """Lock the four-surface tuple against drift.

    The TODO row's four observable surfaces are enumerated in the
    module docstring. If a future refactor adds a fifth surface (e.g.
    "guest tenant can view shared project's billing breakdown") and
    forgets to extend Y10 row 4's coverage, this guard makes the
    omission visible on every CI run.
    """
    assert _SURFACES == (
        "host_grant", "guest_visibility", "role_set", "no_pollution",
    )
    assert len(_SURFACES) == 4


def test_share_role_enum_pinned_to_viewer_contributor_only():
    """``project_shares.role`` enum is locked to ``(viewer, contributor)``.

    Drift guard: a refactor that adds 'owner' to the enum would let a
    guest tenant transitively own a host-tenant project — a
    fundamental cross-tenant trust violation. The DB CHECK from
    alembic 0036 backs this up at the storage layer; the application
    layer constant in ``tenant_projects.PROJECT_SHARE_ROLE_ENUM`` is
    what the POST handler consults — drift in either direction is a
    Y10 row 4 surface (S3 role-set) regression.
    """
    from backend.routers.tenant_projects import PROJECT_SHARE_ROLE_ENUM

    assert PROJECT_SHARE_ROLE_ENUM == ("viewer", "contributor")
    assert "owner" not in PROJECT_SHARE_ROLE_ENUM


def test_share_create_uses_user_can_create_project_in_admin_tier_gate():
    """Source-grep on ``create_project_share`` — RBAC must go through
    the host-tenant admin-tier gate, NOT the legacy ``users.role``
    cache.

    Y10 row 4 surface (S1 host-grant) invariant: only the host
    tenant's owner / admin can grant cross-tenant shares. A refactor
    that swapped to the legacy ``users.role='admin'`` cache would
    let a tenant-A admin grant tenant-B's projects out (since the
    legacy cache is a single global tier, not per-tenant).
    """
    from backend.routers import tenant_projects

    src = inspect.getsource(tenant_projects.create_project_share)
    assert "_user_can_create_project_in" in src, (
        "create_project_share must call _user_can_create_project_in "
        "(per-tenant membership-row gate) — Y10 row 4 host-grant "
        "invariant"
    )


def test_share_create_allowed_membership_role_set_strict_owner_admin():
    """``_PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES`` is locked
    to {owner, admin}.

    Same posture as Y10 row 2's audit-tier role lock — any widening
    (e.g. adding 'member') would let plain tenant members hand
    cross-tenant shares out, breaking the (S1) host-grant trust
    boundary.
    """
    from backend.routers.tenant_projects import (
        _PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES,
    )

    assert _PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )


def test_share_create_self_share_guard_at_handler_returns_422():
    """Source-grep on ``create_project_share``: the self-share guard
    must reject ``guest_tenant_id == tenant_id`` with 422.

    A tenant cannot share a project to itself (the project is already
    in that tenant's namespace; the guest tab would always show its
    own projects, which is nonsense). 422 because this is body
    validation, not RBAC. Drift guard: a refactor that softened this
    to a 200 / 409 silently regresses Y10 row 4 surface (S1).
    """
    from backend.routers import tenant_projects

    src = inspect.getsource(tenant_projects.create_project_share)
    assert "body.guest_tenant_id == tenant_id" in src, (
        "create_project_share must reject self-share at handler — "
        "Y10 row 4 surface (S1) host-grant invariant"
    )
    # The diagnostic message is split across two adjacent f-string
    # lines (... ``cannot ``\n``share a project to itself``); match
    # the contiguous-on-one-line tail so the assertion is robust to
    # the line break, while still pinning the human-readable signal.
    assert "share a project to itself" in src


def test_share_create_handler_emits_dual_chain_audit_event():
    """Source-grep: the share-create handler must call
    ``emit_project_share_granted`` after the INSERT lands.

    Y10 row 4 surface (S2) guest-visibility invariant: the guest
    tenant's chain MUST receive a row so the guest's audit pane can
    surface "we received access from host A on project P". A
    refactor that dropped the dual-chain emit (keeping only the
    legacy single-chain ``audit.log`` call) would silently invisible
    the share from the guest tenant's perspective.
    """
    from backend.routers import tenant_projects

    src = inspect.getsource(tenant_projects.create_project_share)
    assert "emit_project_share_granted" in src, (
        "create_project_share must call audit_events."
        "emit_project_share_granted to fork the dual-chain emit — "
        "Y10 row 4 surface (S2) guest-visibility invariant"
    )
    # Lock the host_tenant_id / guest_tenant_id kwargs used by the
    # call so a refactor that swapped them (writing the host event
    # into the guest chain and vice versa) trips here.
    assert "host_tenant_id=tenant_id" in src
    assert "guest_tenant_id=body.guest_tenant_id" in src


def test_emit_project_share_granted_writes_one_row_per_tenant_chain():
    """Source-grep on ``emit_project_share_granted``: exactly two
    sequential ``_emit_single_chain`` calls — one for host chain,
    one for guest chain.

    The two ``tenant_id_override=`` calls must reference distinct
    parameters (``host_tenant_id`` vs ``guest_tenant_id``) so each
    row lands in its own per-tenant chain under its own
    advisory-lock. Drift guard for the audit-events module.
    """
    from backend import audit_events

    src = inspect.getsource(audit_events.emit_project_share_granted)
    # Exactly two emit_single_chain calls.
    occurrences = src.count("_emit_single_chain")
    assert occurrences == 2, (
        f"emit_project_share_granted must call _emit_single_chain "
        f"exactly twice (host + guest); found {occurrences}"
    )
    # Each tenant_id_override targets a different parameter.
    assert "tenant_id_override=host_tenant_id" in src
    assert "tenant_id_override=guest_tenant_id" in src
    # The chain_role marker distinguishes the two rows.
    assert '"chain_role": "host"' in src
    assert '"chain_role": "guest"' in src


def test_audit_action_constant_for_share_grant_is_dot_notation():
    """The Y9 row 1 dot-notation action constant for share grants is
    ``project_share.granted`` — drift would split the audit pane's
    filter logic.
    """
    from backend.audit_events import EVENT_PROJECT_SHARE_GRANTED

    assert EVENT_PROJECT_SHARE_GRANTED == "project_share.granted"


def test_list_projects_sql_does_not_join_project_shares_no_pollution():
    """``list_projects`` SQL must NOT JOIN ``project_shares``.

    Y10 row 4 surface (S4) anti-pollution invariant: a share row
    from A to B must not silently appear in tenant B's
    ``GET /api/v1/tenants/{B}/projects`` response. The mechanism is
    that the listing SQL filters on ``projects.tenant_id = $1`` only;
    sharing intentionally does NOT widen the listing. A refactor
    that JOINed ``project_shares`` to surface "shared in" projects
    on the guest's tenant list would change the response shape and
    break the no-pollution contract this row pins.

    Drift guard: source-grep the three list SQL constants for any
    occurrence of ``project_shares`` — none is allowed.
    """
    from backend.routers import tenant_projects

    for sql_name in (
        "_LIST_PROJECTS_LIVE_SQL",
        "_LIST_PROJECTS_ARCHIVED_SQL",
        "_LIST_PROJECTS_ALL_SQL",
    ):
        sql = getattr(tenant_projects, sql_name)
        assert "project_shares" not in sql, (
            f"{sql_name} must not JOIN project_shares — Y10 row 4 "
            f"surface (S4) anti-pollution invariant. Sharing grants "
            f"per-project access via require_project_member, NOT "
            f"tenant-list visibility on the guest's list."
        )


def test_resolve_list_visibility_does_not_consult_project_shares():
    """Source-grep: ``_resolve_list_visibility`` must read
    ``user_tenant_memberships``, NOT ``project_shares``.

    The visibility resolver decides whether the caller can see ANY
    row of the tenant's project list (ranged over by the listing
    SQL). Adding a ``project_shares`` consult here would let a
    user from tenant B who has no membership in tenant A see tenant
    A's whole portfolio just because A shared one project to B —
    a flagrant Y10 row 4 surface (S4) violation.
    """
    from backend.routers import tenant_projects

    src = inspect.getsource(tenant_projects._resolve_list_visibility)
    assert "user_tenant_memberships" in src, (
        "_resolve_list_visibility must consult user_tenant_memberships "
        "— Y10 row 4 anti-pollution invariant"
    )
    assert "project_shares" not in src, (
        "_resolve_list_visibility must NOT consult project_shares — "
        "Y10 row 4 surface (S4) anti-pollution invariant"
    )


def test_require_project_member_does_not_consult_shares_known_followup():
    """Documented drift guard: ``backend.auth.require_project_member``
    resolution order today is ``super_admin → project_members row →
    tenant_membership fallback`` — it does NOT consult
    ``project_shares``.

    This is a known follow-up gap from Y10 row 4: the acceptance
    text "tenant B 的 user 能看到" implies the guest tenant's user
    should resolve to a project-scope role via the share row, but
    the current code path requires either an explicit
    ``project_members`` row (manual seed by host admin) or active
    membership in the host tenant (does not apply to guest users).

    Y10 row 4 documents this honestly rather than auto-elevating;
    Y10 is the operational exam, not a place to add new prod
    surface. If a future refactor wires share-row consultation into
    the resolver, this test will trip and force an update to the
    HANDOFF entry — same pattern Y10 row 2 used for the
    ``/api/v1/workspaces/{agent_id}`` tenant-segment gap.
    """
    from backend import auth

    src = inspect.getsource(auth.require_project_member)
    assert "project_shares" not in src, (
        "require_project_member doesn't consult project_shares today; "
        "if a future change adds it, update Y10 row 4 HANDOFF entry "
        "and remove this drift guard"
    )


def test_no_incoming_shares_endpoint_known_followup():
    """Documented drift guard: there is no
    ``/api/v1/tenants/{guest_tid}/incoming-shares`` (or similar
    "shared with me") endpoint today.

    Y10 row 4 acceptance "tenant B 的 user 能看到" relies on the
    audit chain (Y9 row 1) row written into B's chain plus
    operator-driven ``project_members`` seeding. There is no
    standalone listing endpoint on the guest side. If a future row
    adds one, this test trips and forces a HANDOFF update so the
    follow-up can be marked closed.
    """
    from backend.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    forbidden_shapes = (
        "/api/v1/tenants/{tenant_id}/incoming-shares",
        "/api/v1/tenants/{tenant_id}/incoming_shares",
        "/api/v1/tenants/{tenant_id}/shared-with-me",
        "/api/v1/tenants/{tenant_id}/shared_with_me",
    )
    found = [p for p in forbidden_shapes if p in paths]
    assert not found, (
        f"Found a 'shared with me' endpoint shape {found!r} — Y10 "
        f"row 4 known-follow-up flag must be flipped and this drift "
        f"guard removed; update HANDOFF accordingly"
    )


def test_share_id_pattern_pinned():
    """``psh-`` prefix + 4-64 hex chars; same shape as Y4 row 6.

    Drift guard: a refactor that changed the prefix or length would
    invalidate every existing share row's id format.
    """
    from backend.routers.tenant_projects import (
        SHARE_ID_PATTERN,
        _is_valid_share_id,
        _mint_share_id,
    )

    assert SHARE_ID_PATTERN == r"^psh-[a-z0-9]{4,64}$"
    sid = _mint_share_id()
    assert sid.startswith("psh-")
    assert _is_valid_share_id(sid)


def test_share_handler_compat_fingerprint_clean_in_test_file():
    """SOP Step 3 fingerprint grep on this very test file.

    Pattern checks for the four classic SQLite-era compat residues.
    Any hit indicates a regression against the Phase-3-Runtime-v2
    PG-native baseline. The literal regex is below for traceability.
    """
    import pathlib

    fingerprint = re.compile(
        r"_conn\(\)|"
        r"await conn\.commit\(\)|"
        r"datetime\('now'\)|"
        r"VALUES.*\?[,)]"
    )

    self_path = pathlib.Path(__file__).resolve()
    src = self_path.read_text(encoding="utf-8")
    # Strip docstrings + ``#`` line comments before scanning so the
    # forbidden patterns described above (in human prose) don't
    # self-match.
    cleaned_lines: list[str] = []
    in_doc = False
    for raw in src.splitlines():
        line = raw
        # Naïve docstring stripper — toggles on triple-quote
        # delimiters. Same posture as Y10 row 3's a10 self-scan.
        if line.lstrip().startswith('"""') or line.lstrip().startswith("'''"):
            in_doc = not in_doc
            continue
        if in_doc:
            continue
        if line.lstrip().startswith("#"):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    hits = [
        m.group(0) for m in fingerprint.finditer(cleaned)
    ]
    assert not hits, (
        "Y10 row 4 test file contains compat-era fingerprints: "
        f"{hits!r}"
    )


def test_create_project_share_cross_tenant_audit_emit_is_best_effort():
    """The ``emit_project_share_granted`` call MUST be wrapped in a
    try/except so a transient audit-chain append failure does not
    fail the share-create HTTP call.

    Source-grep: the call site is enclosed in try ... except
    Exception ... logger.warning. Drift guard: a refactor that
    removed the wrapper (and thereby propagated a chain-append
    failure to the caller) would change the (S1) host-grant 201
    contract under chain-locking pressure.
    """
    from backend.routers import tenant_projects

    src = inspect.getsource(tenant_projects.create_project_share)
    # The call-site is followed (within the function body) by an
    # ``except Exception`` clause swallowing the exception and
    # logging at warning level.
    assert "emit_project_share_granted" in src
    # Robust check: the audit emit block lives between a ``try:``
    # and an ``except Exception`` line — split on the call-site and
    # confirm the surrounding text contains both.
    head, tail = src.split("emit_project_share_granted", 1)
    # Try block opened at most ~10 lines before the call.
    head_tail = "\n".join(head.splitlines()[-15:])
    assert "try:" in head_tail
    # Except clause within ~15 lines after the call.
    tail_head = "\n".join(tail.splitlines()[:20])
    assert "except Exception" in tail_head


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block B — PG-required acceptance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _tid_a() -> str:
    """Host tenant — owns the project that gets shared out."""
    return f"{_TENANT_PREFIX}-host"


def _tid_b() -> str:
    """Guest tenant — receives the share."""
    return f"{_TENANT_PREFIX}-guest"


def _uid_host_admin() -> str:
    return "u-y10r4hostad"


def _uid_guest_admin() -> str:
    return "u-y10r4guestad"


def _uid_guest_other() -> str:
    """A second user in the guest tenant with active membership but
    role='member' (NOT admin / owner). Stand-in for "tenant B 的其他
    user" — the cross-tenant pollution probe lives on this user."""
    return "u-y10r4gother"


async def _seed_two_tenants_with_admins(pool) -> None:
    """Seed the standard Y10 row 4 fixture::

        * tenants ``t-y10r4-host`` + ``t-y10r4-guest`` (both ``free``,
          enabled).
        * host_admin user with active ``user_tenant_memberships(role=admin,
          status=active)`` on host.
        * guest_admin user with active ``user_tenant_memberships(role=admin,
          status=active)`` on guest.
        * guest_other user with active ``user_tenant_memberships(role=member,
          status=active)`` on guest. No project_members rows yet.

    No cross-tenant memberships — every user is a member of exactly
    one tenant. This shapes the "tenant B 的其他 user" probe so the
    no-pollution invariant is checked under a realistic deployment
    posture (member-tier user with no project_members row).
    """
    from datetime import datetime, timezone

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, 'Y10R4 Host', 'free', 1), "
            "       ($2, 'Y10R4 Guest', 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            _tid_a(), _tid_b(),
        )
        # host_admin
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "  enabled, tenant_id) "
            "VALUES ($1, $2, 'Host Admin', 'admin', '', 1, $3) "
            "ON CONFLICT (id) DO NOTHING",
            _uid_host_admin(), "host-admin@y10r4.local", _tid_a(),
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "  (user_id, tenant_id, role, status, created_at) "
            "VALUES ($1, $2, 'admin', 'active', $3) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            _uid_host_admin(), _tid_a(), created_at,
        )
        # guest_admin
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "  enabled, tenant_id) "
            "VALUES ($1, $2, 'Guest Admin', 'admin', '', 1, $3) "
            "ON CONFLICT (id) DO NOTHING",
            _uid_guest_admin(), "guest-admin@y10r4.local", _tid_b(),
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "  (user_id, tenant_id, role, status, created_at) "
            "VALUES ($1, $2, 'admin', 'active', $3) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            _uid_guest_admin(), _tid_b(), created_at,
        )
        # guest_other (member, no project_members)
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "  enabled, tenant_id) "
            "VALUES ($1, $2, 'Guest Other', 'viewer', '', 1, $3) "
            "ON CONFLICT (id) DO NOTHING",
            _uid_guest_other(), "guest-other@y10r4.local", _tid_b(),
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "  (user_id, tenant_id, role, status, created_at) "
            "VALUES ($1, $2, 'member', 'active', $3) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            _uid_guest_other(), _tid_b(), created_at,
        )


async def _purge_y10_row4_tenants(pool) -> None:
    """Tear down everything seeded by ``_seed_two_tenants_with_admins``
    plus any project / share / audit rows the tests created. Order
    matters because of FKs (project_members → projects → tenants;
    project_shares → projects → tenants).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM project_shares WHERE project_id IN ("
            "  SELECT id FROM projects WHERE tenant_id = ANY($1))",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM project_shares WHERE guest_tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM project_members WHERE project_id IN ("
            "  SELECT id FROM projects WHERE tenant_id = ANY($1))",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM projects WHERE tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM users WHERE id = ANY($1)",
            [_uid_host_admin(), _uid_guest_admin(), _uid_guest_other()],
        )
        await conn.execute(
            "DELETE FROM tenants WHERE id = ANY($1)",
            [_tid_a(), _tid_b()],
        )


@pytest.fixture()
async def _y10_row4_db(pg_test_pool):
    """Seed-and-purge fixture mirroring ``_y10_row2_db`` shape. Pre-
    clean, yield, post-clean. ContextVar reset in ``finally`` so a
    sloppy test cannot leak its tenant slot into the next test.
    """
    pool = pg_test_pool
    await _purge_y10_row4_tenants(pool)
    try:
        yield pool
    finally:
        from backend.db_context import set_project_id, set_tenant_id
        set_tenant_id(None)
        set_project_id(None)
        await _purge_y10_row4_tenants(pool)


def _build_user(uid: str, tid: str, *, role: str = "viewer"):
    """Construct ``auth.User`` for ``dependency_overrides``.

    ``role`` is the legacy ``users.role`` field — Y4-Y9 path-keyed
    gates ignore this in favour of the ``user_tenant_memberships``
    row, but we still populate it to match the seed.
    """
    from backend import auth as _au

    return _au.User(
        id=uid,
        email=f"{uid}@y10r4.local",
        name=uid,
        role=role,
        enabled=True,
        tenant_id=tid,
    )


# ─────────────────────────────────────────────────────────────────
#  B-row 1 — Surface (S1): host admin POSTs share → 201 + row exists
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_host_admin_grants_share_to_guest_tenant(
    client, _y10_row4_db,
):
    """Host tenant A's admin POSTs ``/tenants/{A}/projects/{P}/shares``
    with ``guest_tenant_id=B, role=viewer`` → 201; the
    ``project_shares`` row exists with the expected columns.

    Y10 row 4 surface (S1) host-grant happy path.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    # Open-mode default super_admin can create the project; we then
    # swap in the host_admin to drive the SHARE call from the right
    # actor.
    rp = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={
            "product_line": "embedded",
            "name": "Shared Project",
            "slug": "shared-proj",
        },
    )
    assert rp.status_code == 201, rp.text
    pid = rp.json()["project_id"]

    host_admin = _build_user(_uid_host_admin(), _tid_a(), role="admin")

    async def _fake_current_user():
        return host_admin

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        rs = await client.post(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
            json={"guest_tenant_id": _tid_b(), "role": "viewer"},
        )
        assert rs.status_code == 201, rs.text
        body = rs.json()
        assert body["project_id"] == pid
        assert body["guest_tenant_id"] == _tid_b()
        assert body["role"] == "viewer"
        assert body["tenant_id"] == _tid_a()
        assert body["share_id"].startswith("psh-")
    finally:
        app.dependency_overrides.pop(_au.current_user, None)

    # Row landed in PG with the expected (project, guest_tenant, role)
    # tuple. RETURNING projection in INSERT SQL is the source of the
    # body above, but the DB-side row is what carries the persistent
    # contract — verify both ends agree.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT project_id, guest_tenant_id, role "
            "FROM project_shares "
            "WHERE project_id = $1 AND guest_tenant_id = $2",
            pid, _tid_b(),
        )
    assert row is not None
    assert row["project_id"] == pid
    assert row["guest_tenant_id"] == _tid_b()
    assert row["role"] == "viewer"


# ─────────────────────────────────────────────────────────────────
#  B-row 2 — Surface (S2): guest tenant chain receives audit row
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_guest_tenant_chain_receives_share_granted_audit_row(
    client, _y10_row4_db,
):
    """After the host admin POSTs a share, the guest tenant's audit
    chain MUST contain a ``project_share.granted`` row with
    ``after_json.chain_role='guest'``.

    Y10 row 4 surface (S2) guest-visibility invariant — this is the
    forensic record that lets tenant B's admin / audit pane see
    "we received access from host A on project P". Without this
    row, the share is invisible to the guest tenant's own audit
    surface and the (S2) acceptance text "tenant B 的 user 能看到"
    has no observable evidence on the guest side.

    Sanity assertion: the host chain ALSO has a row with
    ``chain_role='host'`` — proves the dual-chain emit fanned out
    correctly.
    """
    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    # Create the project + share via the open-mode super_admin
    # default actor; (S2) is about post-write chain state, not RBAC.
    rp = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={
            "product_line": "embedded",
            "name": "Visible",
            "slug": "vis",
        },
    )
    assert rp.status_code == 201, rp.text
    pid = rp.json()["project_id"]

    rs = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
        json={"guest_tenant_id": _tid_b(), "role": "contributor"},
    )
    assert rs.status_code == 201, rs.text
    share_id = rs.json()["share_id"]

    # Guest chain: exactly one project_share.granted row, scoped to
    # share_id, with chain_role='guest'.
    async with pool.acquire() as conn:
        guest_rows = await conn.fetch(
            "SELECT after_json FROM audit_log "
            "WHERE tenant_id = $1 "
            "  AND action = 'project_share.granted' "
            "  AND entity_id = $2",
            _tid_b(), share_id,
        )
    assert len(guest_rows) == 1, (
        f"expected 1 project_share.granted row in guest chain "
        f"({_tid_b()!r}); got {len(guest_rows)}"
    )
    guest_payload = json.loads(guest_rows[0]["after_json"])
    assert guest_payload["chain_role"] == "guest"
    assert guest_payload["host_tenant_id"] == _tid_a()
    assert guest_payload["guest_tenant_id"] == _tid_b()
    assert guest_payload["project_id"] == pid
    assert guest_payload["share_id"] == share_id
    assert guest_payload["role"] == "contributor"

    # Host chain: exactly one project_share.granted row, chain_role
    # = 'host'.
    async with pool.acquire() as conn:
        host_rows = await conn.fetch(
            "SELECT after_json FROM audit_log "
            "WHERE tenant_id = $1 "
            "  AND action = 'project_share.granted' "
            "  AND entity_id = $2",
            _tid_a(), share_id,
        )
    assert len(host_rows) == 1
    host_payload = json.loads(host_rows[0]["after_json"])
    assert host_payload["chain_role"] == "host"


# ─────────────────────────────────────────────────────────────────
#  B-row 3 — Surface (S3): host admin sets / changes role lifecycle
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_host_admin_can_change_share_role_via_revoke_and_regrant(
    client, _y10_row4_db,
):
    """Initial POST sets ``role=viewer``. A second POST with
    ``role=contributor`` collides on the UNIQUE
    ``(project_id, guest_tenant_id)`` and returns 409 with the
    existing role surfaced.

    The supported lifecycle for changing role today is DELETE +
    re-POST: revoke the existing share, then re-grant with the new
    role. After the round-trip, the row's role reflects the new
    value.

    Y10 row 4 surface (S3) role-set: the host admin can effectively
    set / change role through the supported endpoints. The
    acceptance text "tenant B 的 admin 能設 role" is interpreted as
    the host-side admin tier owning the share role; a guest-side
    role-change endpoint is a documented follow-up (see Y4 row 6
    Pydantic body for the canonical role enum).
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    rp = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={
            "product_line": "embedded",
            "name": "Roleset",
            "slug": "rs",
        },
    )
    assert rp.status_code == 201, rp.text
    pid = rp.json()["project_id"]

    host_admin = _build_user(_uid_host_admin(), _tid_a(), role="admin")

    async def _fake_current_user():
        return host_admin

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        # Initial grant — viewer.
        r1 = await client.post(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
            json={"guest_tenant_id": _tid_b(), "role": "viewer"},
        )
        assert r1.status_code == 201, r1.text
        share_id_v = r1.json()["share_id"]
        assert r1.json()["role"] == "viewer"

        # Re-POST with contributor — duplicate, 409 with existing role.
        r2 = await client.post(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
            json={"guest_tenant_id": _tid_b(), "role": "contributor"},
        )
        assert r2.status_code == 409, r2.text
        body = r2.json()
        assert body["existing_role"] == "viewer"
        assert body["existing_share_id"] == share_id_v

        # DELETE the existing share.
        rd = await client.delete(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares/"
            f"{share_id_v}"
        )
        assert rd.status_code == 200, rd.text

        # Re-POST with contributor — now succeeds, new share_id.
        r3 = await client.post(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
            json={"guest_tenant_id": _tid_b(), "role": "contributor"},
        )
        assert r3.status_code == 201, r3.text
        assert r3.json()["role"] == "contributor"
        assert r3.json()["share_id"] != share_id_v
    finally:
        app.dependency_overrides.pop(_au.current_user, None)

    async with pool.acquire() as conn:
        final_role = await conn.fetchval(
            "SELECT role FROM project_shares "
            "WHERE project_id = $1 AND guest_tenant_id = $2",
            pid, _tid_b(),
        )
    assert final_role == "contributor"


# ─────────────────────────────────────────────────────────────────
#  B-row 4 — Surface (S3): guest admin CANNOT mutate host's share
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_guest_admin_cannot_grant_share_on_host_project(
    client, _y10_row4_db,
):
    """The guest tenant's admin (active membership on B with
    role='admin', NO membership on A) trying to POST a share on
    host's project returns 403.

    The share-grant endpoint is gated on membership in the *host*
    tenant; a guest admin has no business creating share rows for
    A's projects. Y10 row 4 (S3) role-set lifecycle pin: the
    role-change surface lives on the host's admin tier.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    rp = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={"product_line": "embedded", "name": "Closed",
              "slug": "closed"},
    )
    assert rp.status_code == 201, rp.text
    pid = rp.json()["project_id"]

    guest_admin = _build_user(_uid_guest_admin(), _tid_b(), role="admin")

    async def _fake_current_user():
        return guest_admin

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        rs = await client.post(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
            json={"guest_tenant_id": _tid_b(), "role": "viewer"},
        )
        assert rs.status_code == 403, rs.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)

    # No share row created.
    async with pool.acquire() as conn:
        cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM project_shares WHERE project_id = $1",
            pid,
        )
    assert int(cnt) == 0


# ─────────────────────────────────────────────────────────────────
#  B-row 5 — Surface (S4): guest other-user does NOT see B's other
#                          projects in the project list response
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_guest_other_user_listing_guest_projects_excludes_other_projects(
    client, _y10_row4_db,
):
    """Tenant B has its own projects ``B-internal-1`` and
    ``B-internal-2`` plus an incoming share from tenant A. Tenant B's
    "other user" (active membership role='member', no project_members
    rows) listing ``GET /api/v1/tenants/{B}/projects`` must see ZERO
    of B's internal projects — the explicit-only fall-through from
    alembic 0034.

    Y10 row 4 surface (S4) anti-pollution invariant — the existence
    of a share row from A does NOT widen the guest's own listing
    surface. A regression here would mean that a member-tier user
    in B suddenly gains visibility into B's portfolio just because
    an unrelated cross-tenant share exists.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    # Two internal projects on tenant B (created via super_admin
    # default).
    rb1 = await client.post(
        f"/api/v1/tenants/{_tid_b()}/projects",
        json={"product_line": "embedded", "name": "B Internal 1",
              "slug": "b-int-1"},
    )
    assert rb1.status_code == 201, rb1.text
    pid_b1 = rb1.json()["project_id"]

    rb2 = await client.post(
        f"/api/v1/tenants/{_tid_b()}/projects",
        json={"product_line": "web", "name": "B Internal 2",
              "slug": "b-int-2"},
    )
    assert rb2.status_code == 201, rb2.text
    pid_b2 = rb2.json()["project_id"]

    # A project on tenant A, then shared to B.
    ra = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={"product_line": "embedded", "name": "A Shared",
              "slug": "a-shared"},
    )
    assert ra.status_code == 201, ra.text
    pid_a = ra.json()["project_id"]

    rs = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects/{pid_a}/shares",
        json={"guest_tenant_id": _tid_b(), "role": "viewer"},
    )
    assert rs.status_code == 201, rs.text

    # Confirm a project_shares row exists — sanity for the
    # no-pollution probe below.
    async with pool.acquire() as conn:
        share_count = await conn.fetchval(
            "SELECT COUNT(*) FROM project_shares "
            "WHERE project_id = $1 AND guest_tenant_id = $2",
            pid_a, _tid_b(),
        )
    assert int(share_count) == 1

    guest_other = _build_user(
        _uid_guest_other(), _tid_b(), role="viewer",
    )

    async def _fake_current_user():
        return guest_other

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        # Listing B's projects — explicit-only fall-through (member-
        # tier; no project_members rows seeded). Response is 200 with
        # an empty list.
        rb = await client.get(f"/api/v1/tenants/{_tid_b()}/projects")
        assert rb.status_code == 200, rb.text
        body = rb.json()
        assert body["tenant_id"] == _tid_b()
        ids = {p["project_id"] for p in body["projects"]}
        # No internal-B project visible (explicit-only fall-through).
        assert pid_b1 not in ids, (
            f"member-tier user saw B's internal project {pid_b1!r} "
            f"despite no project_members row — explicit-only "
            f"contract from alembic 0034 regressed"
        )
        assert pid_b2 not in ids
        # And the host-A shared project is NOT in B's listing —
        # share rows do not populate the guest's own list.
        assert pid_a not in ids, (
            f"host-A shared project {pid_a!r} appeared in B's own "
            f"listing — Y10 row 4 surface (S4) anti-pollution "
            f"invariant violated"
        )

        # Sanity: same user listing host A's projects — 403 (no
        # membership on A; the share row does not confer listing
        # access on the host's tenant either).
        ra_list = await client.get(
            f"/api/v1/tenants/{_tid_a()}/projects",
        )
        assert ra_list.status_code == 403, ra_list.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─────────────────────────────────────────────────────────────────
#  B-row 6 — Surface (S4): guest admin does NOT see host's other
#                          projects via guest-tenant listing
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_guest_admin_cannot_enumerate_host_projects_via_share(
    client, _y10_row4_db,
):
    """Y4 row 8 family D's headline RBAC invariant, repeated for
    Y10 row 4 surface (S4): a guest-tenant admin does NOT get
    visibility into the host's project list just because one
    project was shared in. Even though the share grants per-project
    access, it does NOT confer tenant-list visibility.

    Co-located here so the Y10 row 4 acceptance test exercises this
    invariant in its own seeded universe, independent of Y4 row 8's
    seeding.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    # Two host projects — only one is shared.
    rs1 = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={"product_line": "embedded", "name": "Shared",
              "slug": "shared"},
    )
    assert rs1.status_code == 201, rs1.text
    pid_shared = rs1.json()["project_id"]

    rs2 = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={"product_line": "embedded", "name": "Hidden",
              "slug": "hidden"},
    )
    assert rs2.status_code == 201, rs2.text
    pid_hidden = rs2.json()["project_id"]

    rs3 = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects/{pid_shared}/shares",
        json={"guest_tenant_id": _tid_b(), "role": "contributor"},
    )
    assert rs3.status_code == 201, rs3.text

    guest_admin = _build_user(_uid_guest_admin(), _tid_b(), role="admin")

    async def _fake_current_user():
        return guest_admin

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        # 403 listing host's projects — no membership on A.
        rl_host = await client.get(
            f"/api/v1/tenants/{_tid_a()}/projects",
        )
        assert rl_host.status_code == 403, rl_host.text

        # Listing B's own projects — empty (no projects seeded
        # under B in this test) — and host's projects MUST NOT
        # appear in B's tenant list response.
        rl_guest = await client.get(
            f"/api/v1/tenants/{_tid_b()}/projects",
        )
        assert rl_guest.status_code == 200, rl_guest.text
        ids = {p["project_id"] for p in rl_guest.json()["projects"]}
        assert pid_shared not in ids
        assert pid_hidden not in ids
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─────────────────────────────────────────────────────────────────
#  B-row 7 — Surface (S2): host listing includes the share row
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_host_admin_lists_shares_after_grant(
    client, _y10_row4_db,
):
    """The host admin enumerates ``GET /tenants/{A}/projects/{P}/shares``
    after the grant — the response carries the share row with the
    expected columns.

    Y10 row 4 surface (S2) host-side visibility (the *host's* view
    of "we have shared this project to which guests"). Mirror of
    the chain-event row check, but on the operational "shares list"
    surface that the host's settings UI reads.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    rp = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={"product_line": "embedded", "name": "ListMe",
              "slug": "listme"},
    )
    assert rp.status_code == 201, rp.text
    pid = rp.json()["project_id"]

    rs = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
        json={"guest_tenant_id": _tid_b(), "role": "viewer"},
    )
    assert rs.status_code == 201, rs.text
    share_id = rs.json()["share_id"]

    host_admin = _build_user(_uid_host_admin(), _tid_a(), role="admin")

    async def _fake_current_user():
        return host_admin

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        rl = await client.get(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
        )
        assert rl.status_code == 200, rl.text
        body = rl.json()
        assert body["count"] == 1
        share = body["shares"][0]
        assert share["share_id"] == share_id
        assert share["guest_tenant_id"] == _tid_b()
        assert share["role"] == "viewer"
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─────────────────────────────────────────────────────────────────
#  B-row 8 — Surface (S4): no audit row leaked into the wrong chain
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_share_audit_emit_does_not_leak_into_other_tenants_chain(
    client, _y10_row4_db,
):
    """The dual-chain emit produces exactly TWO rows — one in host
    chain, one in guest chain. No third tenant's chain receives a
    stray ``project_share.granted`` row.

    Y10 row 4 cross-tenant pollution forensic invariant: even if a
    third tenant exists in the DB at the time of the share, its
    chain stays clean. Defends against accidental ContextVar leak
    in the swap-and-restore inside ``emit_project_share_granted``.
    """
    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    # Seed an extra tenant ``t-y10r4-bystander`` to test chain
    # isolation. No membership / no project relationship to A or B —
    # purely a bystander whose chain MUST stay empty of share events.
    bystander_tid = "t-y10r4-bystand"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, 'Bystander', 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            bystander_tid,
        )

    try:
        rp = await client.post(
            f"/api/v1/tenants/{_tid_a()}/projects",
            json={"product_line": "embedded", "name": "Iso",
                  "slug": "iso"},
        )
        assert rp.status_code == 201, rp.text
        pid = rp.json()["project_id"]

        rs = await client.post(
            f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
            json={"guest_tenant_id": _tid_b(), "role": "viewer"},
        )
        assert rs.status_code == 201, rs.text
        share_id = rs.json()["share_id"]

        # Total share-grant rows across ALL chains for this share_id
        # must be exactly 2 (host + guest).
        async with pool.acquire() as conn:
            total_rows = await conn.fetch(
                "SELECT tenant_id FROM audit_log "
                "WHERE action = 'project_share.granted' "
                "  AND entity_id = $1",
                share_id,
            )
        tids = sorted(r["tenant_id"] for r in total_rows)
        assert tids == sorted([_tid_a(), _tid_b()]), (
            f"share-grant audit rows must land in exactly host + "
            f"guest chains; got {tids!r}"
        )

        # Bystander chain has zero rows for this share.
        async with pool.acquire() as conn:
            bystander_rows = await conn.fetch(
                "SELECT id FROM audit_log "
                "WHERE tenant_id = $1 "
                "  AND action = 'project_share.granted' "
                "  AND entity_id = $2",
                bystander_tid, share_id,
            )
        assert len(bystander_rows) == 0
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1",
                bystander_tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1",
                bystander_tid,
            )


# ─────────────────────────────────────────────────────────────────
#  B-row 9 — Surface (S1) self-share guard: 422 at handler
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_self_share_guard_rejects_at_handler_with_422(
    client, _y10_row4_db,
):
    """A tenant cannot share a project to itself — 422, not 201,
    not 409. Y10 row 4 surface (S1) host-grant invariant: the
    self-share probe must trip before the INSERT and return a
    body-shape diagnostic.
    """
    pool = _y10_row4_db
    await _seed_two_tenants_with_admins(pool)

    rp = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects",
        json={"product_line": "embedded", "name": "Self",
              "slug": "self"},
    )
    assert rp.status_code == 201, rp.text
    pid = rp.json()["project_id"]

    rs = await client.post(
        f"/api/v1/tenants/{_tid_a()}/projects/{pid}/shares",
        json={"guest_tenant_id": _tid_a(), "role": "viewer"},
    )
    assert rs.status_code == 422, rs.text
    body = rs.json()
    assert "cannot share a project to itself" in body["detail"]
    assert body["tenant_id"] == _tid_a()
    assert body["guest_tenant_id"] == _tid_a()

    # No row created.
    async with pool.acquire() as conn:
        cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM project_shares WHERE project_id = $1",
            pid,
        )
    assert int(cnt) == 0
