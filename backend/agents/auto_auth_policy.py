"""U6-0 B-autoauth (AA-1) — server-side auto-authorization policy (DORMANT).

A PURE, side-effect-free predicate. Given a mutating file-write that the U6
action-guard has BLOCKED under enforce, decide whether the server may
auto-issue the grant (skip the human confirm) — ``AUTO_GRANT`` — or leave it as
a pending human challenge — ``HUMAN_CHALLENGE``. There is NO "deny": a
non-auto-grant simply falls through to the EXISTING human-challenge path.

FAIL-CLOSED: any missing/malformed signal, any exception, ANY doubt ⇒
``HUMAN_CHALLENGE``. No I/O, no DB, no clock, no env read inside the predicate
(the master flag is read by the CALLER via :func:`auto_auth_enabled`) ⇒ pure
over its inputs ⇒ trivially testable + auditable.

Trust axis = BLAST-RADIUS CONTAINMENT (v3 design ``docs`` — a server-launched
runner writing a file STRICTLY inside its own disposable workspace can only
cause effects whose every escape — merge to main, deploy, external comms — is
ALREADY gated by an existing human control). v3 layers three defenses because
"in-workspace" alone is NOT contained (the runner's OWN later ``git commit`` /
``pytest`` executes in-workspace hooks / conftest / CI files before any human
sees them): (1) the workspace-containment gate, (2) an executable-in-pipeline
PATH DENYLIST that removes those self-executing targets from the auto-grantable
set, and (3) a MONOTONE provenance gate that additionally forces a human
challenge when the turn was influenced by the highest-injection-authority
sources. Provenance is a risk-reducer, NOT a proof (completeness is imperfect);
the path gate is what structurally closes the self-execution RCE.

NOTHING wires this yet — dormant. AA-3 plugs it into the server-side challenge
producer (``action_challenge``) behind ``OMNISIGHT_U6_AUTO_AUTH`` (default OFF).
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import unicodedata
from enum import Enum
from pathlib import Path

from backend.agents import provenance as _prov
from backend.agents.execution_context import (
    ExecutionContext,
    SERVER_RUNNER_TRUSTED_PAIRS,
)

_ENABLE_ENV = "OMNISIGHT_U6_AUTO_AUTH"


def auto_auth_enabled() -> bool:
    """The auto-auth master switch (default OFF; read by the CALLER, not the
    predicate — keeps ``evaluate_auto_auth`` pure over its inputs). Same
    default-OFF posture as the other U6 execution-side gates."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class AutoAuthVerdict(Enum):
    """Binary outcome. A non-AUTO_GRANT falls through to the human-challenge
    path — there is deliberately no ``DENY`` (the guard already blocked; this
    only decides who confirms)."""

    AUTO_GRANT = "auto_grant"
    HUMAN_CHALLENGE = "human_challenge"


# ── Gate 3: the sole auto-grantable family (post-B-split = file-writes only) ──
_AUTO_GRANTABLE_FAMILY = "code_write"

# ── Gate 4: closed tool-identity allowlist (STRICTER than the family; fail-
# closed against future registry drift — a new code_write tool is NOT auto-
# grantable until added here with its own review). Conservative start (C1):
# excludes Edit/write_yaml until the grant rate is observed. ``tool_name`` is
# registry-derived (unspoofable from model args). ──
_AUTO_GRANTABLE_TOOLS = frozenset(
    {"Write", "write_file", "patch_file", "str_replace_based_edit_tool"}
)

# ── Gate 6: executable-in-pipeline PATH denylist. A write to any of these,
# though strictly in-workspace, is executed/loaded by the runner's OWN later
# pipeline (``git commit`` → .git/hooks; ``pytest`` → conftest/plugins/*.pth;
# make/nox/CI/build/packaging/editor/agent config) BEFORE any human review — so
# it ESCAPES the containment the auto-grant relies on. Any match ⇒
# HUMAN_CHALLENGE. Matched on the NORMALIZED workspace-relative POSIX path:
# each component is NFKC-normalized, trailing dots/spaces stripped (Win/mac FS
# folding), and CASEFOLDED — so ``Conftest.py`` / ``makefile`` / fullwidth
# homoglyphs cannot evade. first-component match for VCS/CI/editor DIRS; basename
# (any depth) for files that matter anywhere.
#
# NOTE (accepted residual, dormant): this cannot enumerate EVERY self-executing
# file, and a regular ``.py`` the runner's own pytest imports also runs its
# module-level code. That residual is inherent to a code-authoring-and-running
# runner; it is backstopped by gate-7 (provenance) + the MANDATORY observe-only
# (execute-OFF) first rollout + the human +2 merge gate, and must be re-evaluated
# before the flag is ever enabled. ──
_DENY_FIRST_COMPONENTS = frozenset(
    {".git", ".github", ".gitlab", ".hg", ".svn", ".circleci", ".vscode", ".idea", ".claude"}
)
_DENY_BASENAMES = frozenset(
    {
        # pytest / test-runner auto-exec
        "conftest.py", "pytest.ini", "tox.ini", "noxfile.py", "tasks.py", "fabfile.py",
        ".coveragerc", "mypy.ini", ".flake8", "ruff.toml", ".ruff.toml",
        # packaging / install-time exec
        "setup.py", "setup.cfg", "pyproject.toml", ".python-version",
        "package.json", ".npmrc", "pnpm-workspace.yaml", ".yarnrc", ".yarnrc.yml",
        # make / build-system exec
        "makefile", "gnumakefile", "makefile.am", "cmakelists.txt", "meson.build",
        "sconstruct", "sconscript", "configure", "configure.ac",
        "rakefile", "justfile", "build", "build.bazel", "workspace", "workspace.bazel",
        # python import-time exec
        "sitecustomize.py", "usercustomize.py", "manage.py", "wsgi.py", "asgi.py",
        # containers / dev env / pre-commit
        "dockerfile", "devcontainer.json", ".pre-commit-config.yaml", ".envrc",
        # shell rc (sourced by wrappers)
        ".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc",
        # CI descriptors
        ".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml", ".drone.yml",
        # git config that alters behavior
        ".gitmodules", ".gitattributes",
        # agent / harness config that registers hooks
        "claude.md",
    }
)
_DENY_SUFFIXES = (".pth",)

# Casefolded views for normalized comparison (basenames/first-components above
# are already lowercase where casefold-stable; fold to be certain).
_DENY_FIRST_COMPONENTS_CF = frozenset(c.casefold() for c in _DENY_FIRST_COMPONENTS)
_DENY_BASENAMES_CF = frozenset(b.casefold() for b in _DENY_BASENAMES)
_DENY_SUFFIXES_CF = tuple(s.casefold() for s in _DENY_SUFFIXES)


def _norm_component(component: str) -> str:
    """NFKC + strip trailing dots/spaces (Win/mac FS folding) + casefold — so a
    self-executing filename cannot evade the denylist by case/unicode/trailing
    tricks."""
    c = unicodedata.normalize("NFKC", component)
    c = c.rstrip(". ")
    return c.casefold()

# ── Gate 7: highest-injection-authority sources. Their PRESENCE in the sealed
# turn provenance forces a human challenge even for a contained write (external
# agent / external tool content — the least-controlled injection surface). This
# gate can ONLY narrow (never grants); it is a risk-reducer, not a proof. Other
# source-kinds (episodic/rag/read_file/tool_result/chat/runner_memory) are
# PERMITTED — their poison→executable-target vector is closed by gate 6, and
# their poison→inert-file vector is blast-radius contained; forcing a challenge
# on them would challenge every real turn and kill the feature. ──
_HIGH_INJECTION_SOURCES = frozenset({_prov.A2A_RESULT, _prov.MCP_RESULT})

_HEX = frozenset("0123456789abcdef")


def _gate_server_principal(ctx: object) -> bool:
    """Gate 2 (+ gate 8 ctx-tenant): a SERVER-LAUNCHED runner with a
    FACTORY-OWNED trusted (source, actor_id) pair and a non-empty tenant."""
    if not isinstance(ctx, ExecutionContext):
        return False
    if ctx.principal_type != "service":
        return False
    if (ctx.authorization_source, ctx.actor_id) not in SERVER_RUNNER_TRUSTED_PAIRS:
        return False
    return isinstance(ctx.tenant_id, str) and bool(ctx.tenant_id)


def _gate_contained_family_and_tool(prepared: object) -> bool:
    """Gate 3 + 4: a mutating ``code_write`` op whose tool is on the closed
    file-write allowlist."""
    desc = getattr(prepared, "operation_descriptor", None)
    if desc is None:
        return False
    if getattr(desc, "effect", None) != "mutating":
        return False
    if getattr(desc, "family", None) != _AUTO_GRANTABLE_FAMILY:
        return False
    return getattr(desc, "tool_name", None) in _AUTO_GRANTABLE_TOOLS


def _contained_relpath(target: object, root_resolved: str) -> str | None:
    """Gate 5 containment, RE-DERIVED (never inherited from the canonicalizer):
    return the normalized workspace-relative POSIX path IFF ``target`` resolves
    STRICTLY within ``root_resolved``; else None. NECESSARY-not-sufficient — the
    executor's O_NOFOLLOW capability walk is the containment of record; this is a
    cheap fail-closed filter on an obviously-uncontained/malformed target."""
    if not isinstance(target, str) or not target:
        return None
    try:
        root_p = Path(root_resolved)
        joined = target if os.path.isabs(target) else os.path.join(root_resolved, target)
        resolved = Path(joined).resolve()
        rel = resolved.relative_to(root_p)
    except (ValueError, OSError):
        return None
    rel_posix = rel.as_posix()
    if rel_posix in ("", "."):
        return None  # the root itself is not a writable file target
    return rel_posix


def _gate_workspace_bound(
    ctx: ExecutionContext,
    prepared: object,
    workspace_id: object,
    workspace_root: object,
) -> str | None:
    """Gate 5 (+ gate 8 workspace-tenant): validate the ``workspace_id`` shape,
    bind it to the ctx tenant, re-verify the digest binds THIS resolved root, and
    confirm ``canonical_target`` is contained. Returns the normalized relative
    path (for gate 6) on success, else None."""
    if not isinstance(workspace_id, str):
        return None
    parts = workspace_id.split(":")
    if len(parts) != 4 or parts[0] != "ws":
        return None
    _, wid_tenant, wid_adapter, wid_digest = parts
    if not wid_tenant or not wid_adapter:
        return None
    # gate 8: the workspace must belong to THIS ctx's tenant.
    if wid_tenant != ctx.tenant_id:
        return None
    if len(wid_digest) != 64 or any(c not in _HEX for c in wid_digest):
        return None
    if not isinstance(workspace_root, str) or not workspace_root:
        return None
    try:
        root_resolved = str(Path(workspace_root).resolve())
    except (ValueError, OSError):
        return None
    if not os.path.isabs(root_resolved):
        return None
    # Re-verify the workspace_id digest binds exactly this resolved root
    # (matches authoritative_context_resolver's sha256(resolved_root)).
    if hashlib.sha256(root_resolved.encode("utf-8")).hexdigest() != wid_digest:
        return None
    return _contained_relpath(getattr(prepared, "canonical_target", None), root_resolved)


def _gate_inert_path(rel_posix: str) -> bool:
    """Gate 6: the (already contained) relative target must NOT be an
    executable-in-pipeline file the runner's own next step would run. Each path
    component is normalized (NFKC + trailing-strip + casefold) before matching so
    case/unicode/trailing evasions cannot slip a self-executing file through."""
    if not rel_posix or posixpath.isabs(rel_posix):
        return False
    components = [_norm_component(c) for c in rel_posix.split("/")]
    if not components[-1]:
        return False  # trailing slash / empty basename ⇒ not a file target
    if components[0] in _DENY_FIRST_COMPONENTS_CF:
        return False
    base = components[-1]
    if base in _DENY_BASENAMES_CF:
        return False
    return not base.endswith(_DENY_SUFFIXES_CF)


def _gate_clean_provenance(snapshot: object) -> bool:
    """Gate 7 (MONOTONE, fail-closed): require a COMPLETE model snapshot with NO
    highest-injection-authority (A2A/MCP) source. Missing/partial/non-snapshot ⇒
    fail (challenge)."""
    if not isinstance(snapshot, _prov.ModelSnapshot):
        return False
    inner = getattr(snapshot, "snapshot", None)
    if inner is None or getattr(inner, "completeness", None) != "complete":
        return False
    records = getattr(inner, "records", None)
    if records is None:
        return False
    try:
        for rec in records:
            if getattr(rec, "source_kind", None) in _HIGH_INJECTION_SOURCES:
                return False
    except TypeError:
        return False
    return True


def evaluate_auto_auth(
    *,
    execution_context: object,
    prepared_action: object,
    workspace_id: object,
    workspace_root: object,
    snapshot: object,
) -> AutoAuthVerdict:
    """Pure containment predicate. ALL gates must pass for ``AUTO_GRANT``; any
    failure or exception ⇒ ``HUMAN_CHALLENGE`` (fail-closed). The caller must
    have already checked :func:`auto_auth_enabled` and the existing
    grant-eligible / not-unbound gates.

    NEVER reads ``executable_args`` / ``human_rendering`` / ``content`` or any
    model text as a trust input; NEVER trusts a caller-supplied
    ``authorization_source`` beyond the factory-owned pair set; re-derives
    workspace containment rather than inheriting it.
    """
    try:
        if not _gate_server_principal(execution_context):
            return AutoAuthVerdict.HUMAN_CHALLENGE
        if not _gate_contained_family_and_tool(prepared_action):
            return AutoAuthVerdict.HUMAN_CHALLENGE
        rel_posix = _gate_workspace_bound(
            execution_context, prepared_action, workspace_id, workspace_root
        )
        if rel_posix is None:
            return AutoAuthVerdict.HUMAN_CHALLENGE
        if not _gate_inert_path(rel_posix):
            return AutoAuthVerdict.HUMAN_CHALLENGE
        if not _gate_clean_provenance(snapshot):
            return AutoAuthVerdict.HUMAN_CHALLENGE
        return AutoAuthVerdict.AUTO_GRANT
    except Exception:  # noqa: BLE001 — ANY doubt fails closed to a human challenge
        return AutoAuthVerdict.HUMAN_CHALLENGE
