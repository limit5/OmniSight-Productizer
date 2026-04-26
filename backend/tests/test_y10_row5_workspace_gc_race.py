"""Y10 #286 row 5 — Workspace GC race acceptance test.

Acceptance criterion (TODO §Y10 row 5)::

    Workspace GC race：agent 正在 clone 時 GC reaper 被觸發 →
    clone 完成、GC 跳過、audit 記錄「skipped-live」。

Three observable dimensions
───────────────────────────
* (D1) **clone_completes** — an in-flight ``provision()`` (or any
  long-running git op against the workspace) must NOT have its
  on-disk leaf trashed by a concurrent ``sweep_once()`` invocation.
  The GC must observe the in-flight state and step around it.
* (D2) **gc_skips** — the sweep records the busy workspace in
  ``GCSummary.skipped_busy`` (with the ``"{leaf} [{reason}]"``
  format) instead of moving it to ``_trash/``. Two reasons cover
  the race window today:
    1. ``"registry"`` — the agent is in
       ``backend.workspace._workspaces`` (in-process active
       registry).
    2. ``"index.lock fresh (Ns)"`` — the leaf has a
       ``.git/index.lock`` younger than ``_FRESH_LOCK_AGE_S`` (60s)
       indicating an in-flight git operation even when no
       registry entry covers it (CLI / chatops invocation outside
       the FastAPI lifespan).
* (D3) **audit_skipped_live** — the sweep emits an audit row
  reflecting the skipped-busy state. Today this is the **aggregate**
  ``workspace.gc_executed`` row written by
  ``audit_events.emit_workspace_gc_executed`` — its
  ``after.skipped_busy_count`` carries ``len(summary.skipped_busy)``.

Known follow-up gap (documented honestly per the Y10 row 4 pattern)
───────────────────────────────────────────────────────────────────
The acceptance text "audit 記錄『skipped-live』" can be read in two
ways:

* **Aggregate-row reading**: the sweep-completion ``workspace.gc_executed``
  row is enough — operators reading the aggregate row see "this
  sweep skipped N busy workspaces" and can correlate against the
  in-process registry / the SSE ``workspace_gc`` event stream. This
  IS what ships today (Y9 row 1).
* **Per-workspace-row reading**: a dedicated
  ``workspace.gc_skipped_live`` event constant emitted PER skipped
  workspace, so the audit chain carries one row per (tenant_id,
  agent_id) tuple that was preserved by the busy gate.

Y10 row 5 covers the surface that DOES ship (the aggregate row's
``skipped_busy_count`` field) and **honestly documents the
per-workspace-row gap** as a follow-up via Block A drift guards
(a11 / a12). This is the same posture Y10 row 2 used for the
``/api/v1/workspaces/{agent_id}`` tenant-segment gap and Y10 row 4
used for ``require_project_member`` not consulting ``project_shares``.
Y10 is the operational exam — the row's job is to prove the
contracts that DID ship work as advertised AND honestly mark the
ones that DIDN'T. Adding a new ``EVENT_WORKSPACE_GC_SKIPPED_LIVE``
constant + per-workspace ``_emit_gc_audit`` on the skip branch
would be a prod-surface change, which Y10 explicitly does not
introduce.

Test layout
───────────
* **Block A — pure-unit drift guards** (always run, no FS, no PG):
  lock the busy-detection contract identity, the
  ``_FRESH_LOCK_AGE_S`` constant, the ``GCSummary.skipped_busy``
  field, the aggregate-row payload shape, the
  ``EVENT_WORKSPACE_GC_EXECUTED`` dot-notation constant, and
  document the two known follow-up gaps (no
  per-workspace event constant, no per-workspace skip audit emit
  on the stale-leaf scan branch). Source-grep guards on
  ``provision()`` confirm the registry is registered AFTER clone
  completes (the actual race window) and that the source-side
  fresh-lock check uses the same 60s freshness window.
* **Block B — PG-required acceptance** (skip without
  ``OMNI_TEST_PG_URL``): trigger ``sweep_once()`` against a real
  PG-backed audit chain with a planted busy workspace and assert
  the ``workspace.gc_executed`` row landed with
  ``after_json.skipped_busy_count >= 1``, and that no
  ``workspace.gc_skipped_*`` per-workspace row exists today.
* **Block C — filesystem-only acceptance** (always run, uses
  ``tmp_path``, no PG): exercise the actual race scenarios with a
  fake leaf, a registry pin, and / or a fresh ``.git/index.lock``,
  and verify the on-disk leaf survives + ``summary.skipped_busy``
  records the correct reason. Includes one ``asyncio.gather``
  test where a fake "clone" task races against a concurrent
  ``sweep_once()`` and the leaf still exists at the end.

Same skip-pattern as ``test_y10_row1_multi_tenant_concurrency.py``,
``test_y10_row2_cross_tenant_leak.py``, ``test_y10_row3_migration_idempotency.py``
and ``test_y10_row4_guest_tenant_share.py`` so the test lane gating
stays consistent across the Y10 rows.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Pure test code — zero new prod code, zero new module-globals
beyond the immutable ``_DIMENSIONS`` 3-tuple, the ``_TENANT_PREFIX``
str, the ``_AGENT_PREFIX`` str, and the ``_requires_pg`` decorator.
Each uvicorn worker derives the same value from this source file
(SOP audit answer #1). Block C uses pytest's per-test ``monkeypatch``
fixture to redirect ``backend.workspace._WORKSPACES_ROOT`` onto
``tmp_path`` and to swap ``backend.workspace._workspaces`` with a
fresh empty dict per test, so cross-test pollution of the in-process
registry is impossible (SOP audit answer #3 — per-test isolation
by design). Block B fixture resets ``set_tenant_id(None)`` /
``set_project_id(None)`` in teardown so cross-test ContextVar bleed
is impossible (SOP audit answer #3).

Read-after-write timing audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Block C tests sequentially ``await sweep_once(...)`` and only then
assert on the captured ``silenced_audit`` spy / ``summary.skipped_busy``
list. The audit spy and the registry monkeypatch both run on the
same event loop as the sweep, so all writes complete before the
asserts. The single concurrent test (c6) uses ``asyncio.gather`` to
run a fake clone task and a sweep task in parallel; the assertion
runs after both have been awaited to completion, so the on-disk
state observed is final. Block B tests ``await`` ``sweep_once()``
fully before reading from ``audit_log`` — and ``audit.log`` itself
holds a ``pg_advisory_xact_lock`` per tenant chain across its INSERT,
so the row is committed and visible before ``sweep_once()`` returns.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Acceptance-criterion dimensions (Y10 row 5, TODO §Y10)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Three observable dimensions enumerated in the module docstring.
_DIMENSIONS = ("clone_completes", "gc_skips", "audit_skipped_live")


# Tenant ids reserved for this row's tests. The ``-y10r5-`` segment
# makes these immediately identifiable in audit_log forensics if a
# crashed test leaves rows behind.
_TENANT_PREFIX = "t-y10r5"
_AGENT_PREFIX = "a-y10r5"


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="Y10 row 5 workspace-GC-race PG-chain tests need an actual "
           "PG instance — set OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block A — pure-unit drift guards (always run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_acceptance_dimensions_match_acceptance_criterion():
    """Lock the three-dimension tuple against drift.

    The TODO row's three observable dimensions are enumerated in the
    module docstring. If a future refactor adds a fourth dimension
    (e.g. a dedicated "skipped_live" SSE event topic) and forgets to
    extend Y10 row 5's coverage, this guard makes the omission
    visible on every CI run.
    """
    assert _DIMENSIONS == (
        "clone_completes", "gc_skips", "audit_skipped_live",
    )
    assert len(_DIMENSIONS) == 3


def test_fresh_lock_age_seconds_pinned_at_60s():
    """``workspace_gc._FRESH_LOCK_AGE_S`` must equal 60.0.

    Drift guard: the 60-second freshness window is the load-bearing
    constant that decides "is this in-flight git op or zombie lock?".
    It mirrors ``backend.workspace.cleanup_stale_locks`` — a refactor
    that lowered it to e.g. 5s would let GC trample legitimately-
    in-flight clones; raising it past several minutes would let
    crashed agents hold ghost locks indefinitely.
    """
    from backend import workspace_gc

    assert workspace_gc._FRESH_LOCK_AGE_S == 60.0


def test_is_workspace_busy_returns_registry_reason_string():
    """Source-grep on ``_is_workspace_busy``: the registry-hit branch
    returns the literal string ``"registry"`` as the reason.

    Y10 row 5 (D2 gc_skips) invariant: the format string for the
    registry skip reason is part of the contract operators rely on
    when grepping logs / inspecting ``summary.skipped_busy``. A
    refactor that changed the literal to ``"in-registry"`` or
    ``"active"`` would silently break log scrapers.
    """
    from backend import workspace_gc

    src = inspect.getsource(workspace_gc._is_workspace_busy)
    assert 'return True, "registry"' in src, (
        "_is_workspace_busy must return the literal 'registry' string "
        "as the busy reason for active-registry hits — Y10 row 5 D2 "
        "skip-format invariant"
    )


def test_is_workspace_busy_returns_lock_reason_format():
    """Source-grep on ``_is_workspace_busy``: the fresh-lock branch
    returns ``f"index.lock fresh ({age:.0f}s)"`` as the reason.

    Y10 row 5 (D2 gc_skips) invariant: the lock-skip reason carries
    the lock age in seconds for forensic readability — operators
    distinguishing "5s old, just now" vs "55s old, stuck near
    threshold" rely on the literal format. Drift guard.
    """
    from backend import workspace_gc

    src = inspect.getsource(workspace_gc._is_workspace_busy)
    assert 'index.lock fresh' in src, (
        "_is_workspace_busy must include 'index.lock fresh' in its "
        "lock-skip reason — Y10 row 5 D2 skip-format invariant"
    )
    # The age is interpolated as ``({age:.0f}s)`` so a refactor that
    # dropped the suffix or the seconds unit trips here.
    assert '{age:.0f}s' in src


def test_is_workspace_busy_checks_registry_before_lock():
    """Source-grep on ``_is_workspace_busy``: the active-registry
    check must precede the ``.git/index.lock`` check.

    Ordering matters because the registry check is O(1) (a set
    membership probe on already-resolved paths) while the lock check
    issues a ``stat()`` per leaf. Inverting the order would cost an
    extra syscall on every busy workspace. Y10 row 5 pins the order
    so a future refactor cannot silently regress sweep cost on a
    tenant with thousands of busy leaves.
    """
    from backend import workspace_gc

    src = inspect.getsource(workspace_gc._is_workspace_busy)
    # Find the relative positions; registry-check must come first.
    registry_pos = src.find("active_paths")
    lock_pos = src.find("index.lock")
    assert registry_pos > -1 and lock_pos > -1, (
        "_is_workspace_busy must reference both active_paths and "
        ".git/index.lock — Y10 row 5 D2 invariant"
    )
    assert registry_pos < lock_pos, (
        "_is_workspace_busy must check active_paths registry BEFORE "
        ".git/index.lock — Y10 row 5 D2 invariant"
    )


def test_gc_summary_has_skipped_busy_field_with_default_empty_list():
    """``GCSummary.skipped_busy`` must exist and default to ``[]``.

    Y10 row 5 (D2 gc_skips) wires the busy detection through this
    field so callers (the loop logger, the aggregate audit emit, the
    Block C tests) have a single source of truth for what was
    skipped this sweep. Drift guard against any rename / removal.
    """
    from backend.workspace_gc import GCSummary

    summary = GCSummary()
    assert summary.skipped_busy == []
    # ``as_dict()`` must surface the field for the aggregate audit
    # emit and for SSE / dashboard consumers.
    snapshot = summary.as_dict()
    assert "skipped_busy" in snapshot
    assert snapshot["skipped_busy"] == []


def test_sweep_stale_leaves_appends_to_skipped_busy_with_reason_format():
    """Source-grep: the stale-leaf scan branch appends to
    ``summary.skipped_busy`` using the ``f"{leaf} [{reason}]"`` format.

    Y10 row 5 (D2 gc_skips) invariant: the bracket format is what
    operators grep for when reconstructing why a workspace survived
    a sweep. Two reason strings appear in brackets: ``[registry]``
    and ``[index.lock fresh (...)]``. Drift guard against any
    refactor that simplified the format (e.g. dropping the brackets,
    inverting "leaf" and "reason", or omitting the path).
    """
    from backend import workspace_gc

    src = inspect.getsource(workspace_gc._sweep_stale_leaves)
    # The exact append-format expression. Pinning the f-string body
    # so a refactor that simplified to e.g. ``str(leaf)`` (dropping
    # the reason) is caught.
    assert 'summary.skipped_busy.append(f"{leaf} [{reason}]")' in src, (
        "_sweep_stale_leaves must append leaves with the "
        "'{leaf} [{reason}]' format — Y10 row 5 D2 forensic "
        "invariant"
    )


def test_sweep_quota_evict_also_appends_to_skipped_busy():
    """Source-grep: the quota-eviction branch ALSO calls
    ``_is_workspace_busy`` and skips into ``summary.skipped_busy``.

    Y10 row 5 (D1 clone_completes) invariant: a quota-driven sweep
    must respect the same busy-detection contract as the stale-leaf
    sweep — an in-flight clone is just as in-flight whether the
    sweep was triggered by mtime age or by quota pressure. Without
    this guard a tenant under quota pressure could have its actively-
    cloning workspace torn out from under it.
    """
    from backend import workspace_gc

    src = inspect.getsource(workspace_gc._sweep_quota_evict)
    assert "_is_workspace_busy" in src, (
        "_sweep_quota_evict must call _is_workspace_busy — Y10 row 5 "
        "D1 invariant: quota path must respect the same busy gate as "
        "the stale-leaf path"
    )
    assert "summary.skipped_busy" in src, (
        "_sweep_quota_evict must append busy workspaces to "
        "summary.skipped_busy — Y10 row 5 D2 invariant"
    )


def test_emit_workspace_gc_executed_carries_skipped_busy_count_field():
    """Source-grep on ``emit_workspace_gc_executed``: the aggregate
    audit row's ``after`` payload must include ``skipped_busy_count``.

    Y10 row 5 (D3 audit_skipped_live) — the aggregate-row reading of
    the acceptance criterion. The ``workspace.gc_executed`` row's
    payload exposes the busy-skip count as a top-level field so
    operators / billing rollups don't have to scan SSE events to
    reconstruct it. Drift guard against a refactor that flattened
    or renamed the field.
    """
    from backend import audit_events

    src = inspect.getsource(audit_events.emit_workspace_gc_executed)
    assert '"skipped_busy_count"' in src, (
        "emit_workspace_gc_executed's after payload must include the "
        "'skipped_busy_count' field — Y10 row 5 D3 aggregate-row "
        "invariant"
    )
    assert 'summary.get("skipped_busy"' in src, (
        "emit_workspace_gc_executed must source skipped_busy_count "
        "from summary['skipped_busy'] — Y10 row 5 D3 invariant"
    )


def test_event_workspace_gc_executed_dot_notation_constant():
    """``EVENT_WORKSPACE_GC_EXECUTED == 'workspace.gc_executed'``.

    Y9 row 1 dot-notation contract — drift would split the audit
    pane's filter logic and the T-series billing rollup's source
    of truth.
    """
    from backend.audit_events import (
        ALL_EVENT_TYPES,
        EVENT_WORKSPACE_GC_EXECUTED,
    )

    assert EVENT_WORKSPACE_GC_EXECUTED == "workspace.gc_executed"
    # Membership in the canonical list — the verifier loop in
    # I8 / Y9 row 1 keys on ALL_EVENT_TYPES so a constant defined
    # but not exported would silently bypass the contract guard.
    assert EVENT_WORKSPACE_GC_EXECUTED in ALL_EVENT_TYPES


def test_no_event_workspace_gc_skipped_live_constant_known_followup():
    """Documented drift guard: ``audit_events`` does NOT export an
    ``EVENT_WORKSPACE_GC_SKIPPED_LIVE`` constant today.

    Y10 row 5 acceptance text "audit 記錄『skipped-live』" is satisfied
    today by the **aggregate** ``workspace.gc_executed`` row's
    ``skipped_busy_count`` field — operators reading the aggregate
    row see "this sweep skipped N busy workspaces" and can correlate
    against the SSE event stream. A dedicated per-workspace event
    constant + per-workspace ``_emit_gc_audit`` on the skip branch
    would be a prod-surface change, which Y10 explicitly does not
    introduce.

    If a future row adds the constant, this drift guard trips and
    forces an update to the HANDOFF entry — same pattern Y10 row 2
    used for the workspace tenant-segment gap and Y10 row 4 used
    for the ``require_project_member`` / incoming-shares gaps.
    """
    from backend import audit_events

    constants_with_skipped = [
        name for name in dir(audit_events)
        if name.startswith("EVENT_") and "SKIPPED" in name.upper()
    ]
    assert constants_with_skipped == [], (
        f"Found new EVENT_*SKIPPED* constants in audit_events: "
        f"{constants_with_skipped!r}. Y10 row 5 documented this gap "
        f"as a follow-up — flip the HANDOFF entry and remove this "
        f"drift guard."
    )


def test_sweep_stale_leaves_skip_branch_does_not_emit_per_workspace_audit_known_followup():
    """Documented drift guard: ``_sweep_stale_leaves`` does NOT call
    ``_emit_gc_audit`` on the skip branch today — only on the trash
    branch.

    Y10 row 5 honest follow-up: a per-workspace ``workspace.gc_skipped_live``
    audit row would require the skip branch to call ``_emit_gc_audit``
    *before* the ``continue`` statement. Today the ``continue`` short-
    circuits before any audit emit, so per-workspace skip rows never
    land in ``audit_log``. The aggregate ``workspace.gc_executed``
    row written at the end of ``sweep_once`` IS what carries the
    skipped-busy signal today (D3 aggregate reading).

    If a future change wires per-workspace audit emit into the skip
    branch, this drift guard trips and forces an update to the
    HANDOFF entry.
    """
    from backend import workspace_gc

    src = inspect.getsource(workspace_gc._sweep_stale_leaves)
    # Locate the skip branch. The branch is the ``if busy:`` block
    # that ends in a ``continue``. Slice from ``if busy:`` to the
    # next blank line / next top-level statement and confirm
    # ``_emit_gc_audit`` does NOT appear inside.
    busy_block_match = re.search(
        r"if busy:\s*\n(?:.*\n)*?\s+continue", src,
    )
    assert busy_block_match is not None, (
        "Could not locate the 'if busy:' / 'continue' skip branch "
        "in _sweep_stale_leaves source — drift in branch shape"
    )
    skip_block = busy_block_match.group(0)
    assert "_emit_gc_audit" not in skip_block, (
        "_sweep_stale_leaves skip branch now calls _emit_gc_audit; "
        "Y10 row 5 documented this gap as a follow-up — flip the "
        "HANDOFF entry and remove this drift guard."
    )


def test_provision_registers_in_workspaces_dict_after_clone_completes():
    """Source-grep on ``backend.workspace.provision``: the
    ``_workspaces[agent_id] = info`` registration happens AFTER the
    git clone / worktree-add subprocess returns.

    Y10 row 5 (D1 clone_completes) — this is THE race window. The
    on-disk leaf exists from the moment ``git clone`` writes the
    first object; the in-process registry is updated only after the
    subprocess returns. During that window, busy detection relies
    on ``.git/index.lock`` freshness (``_FRESH_LOCK_AGE_S = 60s``).
    A refactor that pre-registered the workspace before clone
    completed would be safer (closes the race window from both
    sides) but would require care around exception cleanup. The
    drift guard pins the current behaviour so any change is
    deliberate.
    """
    from backend import workspace as ws_mod

    src = inspect.getsource(ws_mod.provision)
    # The registration line.
    reg_pos = src.find("_workspaces[agent_id] = info")
    assert reg_pos > -1, (
        "provision() must register the WorkspaceInfo in _workspaces "
        "via the canonical key form — Y10 row 5 D1 invariant"
    )
    # The clone / worktree add commands. Either form must precede
    # the registration.
    worktree_pos = src.find('git worktree add')
    clone_pos = src.find('git clone')
    assert worktree_pos > -1 and clone_pos > -1, (
        "provision() must reference both git worktree add and git "
        "clone — Y10 row 5 D1 invariant"
    )
    # Both clone-path commands must appear before the registration
    # so the natural race window (clone in progress, registry not yet
    # populated) is closed by .git/index.lock freshness on the
    # source repo, not by a pre-registration in _workspaces.
    assert worktree_pos < reg_pos
    assert clone_pos < reg_pos


def test_provision_uses_60s_window_for_fresh_source_lock():
    """Source-grep on ``provision``: the ``source_lock`` (i.e. the
    main repo's ``.git/index.lock``) freshness check uses a 60s
    window mirroring ``_FRESH_LOCK_AGE_S``.

    Y10 row 5 (D1 clone_completes) — when ``provision`` itself runs
    against an already-locked source repo (another git op in
    progress), it MUST honour the same 60s freshness convention as
    the GC reaper. Removing a fresh source lock corrupts the peer
    git's transaction; the convention is shared between
    ``workspace.provision`` and ``workspace_gc._is_workspace_busy``.
    """
    from backend import workspace as ws_mod

    src = inspect.getsource(ws_mod.provision)
    assert "if age >= 60:" in src, (
        "provision() must use a 60s window for stale source-lock "
        "removal, mirroring workspace_gc._FRESH_LOCK_AGE_S — Y10 "
        "row 5 D1 invariant"
    )


def test_compat_fingerprint_clean_in_test_file():
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
    # self-match. Same posture as Y10 row 3 / row 4's self-scan.
    cleaned_lines: list[str] = []
    in_doc = False
    for raw in src.splitlines():
        line = raw
        if line.lstrip().startswith('"""') or line.lstrip().startswith("'''"):
            in_doc = not in_doc
            continue
        if in_doc:
            continue
        if line.lstrip().startswith("#"):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    hits = [m.group(0) for m in fingerprint.finditer(cleaned)]
    assert not hits, (
        "Y10 row 5 test file contains compat-era fingerprints: "
        f"{hits!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block C — filesystem-only acceptance (always run, tmp_path)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def isolated_ws_root(monkeypatch, tmp_path):
    """Redirect ``backend.workspace._WORKSPACES_ROOT`` onto pytest's
    ``tmp_path`` so the GC sweep walks a fake hierarchy and not the
    real ``./.agent_workspaces`` / ``./data/workspaces`` tree on the
    dev host. Same pattern as ``test_y6_row6_workspace_gc.py``.
    """
    from backend import workspace as ws_mod

    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    yield root


@pytest.fixture(autouse=False)
def empty_registry(monkeypatch):
    """Swap ``backend.workspace._workspaces`` for a fresh empty dict
    so each Block C test starts from a known-empty registry. Must
    be requested explicitly by tests that mutate the registry —
    autouse=False because some Block A tests don't need it and we
    want to keep test isolation deterministic per module."""
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod

    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)
    gc_mod._reset_for_tests()


@pytest.fixture
def silenced_audit(monkeypatch):
    """Replace ``backend.audit.log`` with a capture spy so Block C
    tests don't need a live PG pool. Captures every audit call —
    Block C tests assert on / negative-assert against this list to
    verify the aggregate row landed and no per-workspace skip row
    was emitted (the documented Y10 row 5 follow-up gap).
    """
    from backend import audit as _audit

    captured: list[dict] = []

    async def _spy(action, entity_kind, entity_id, before=None, after=None,
                   actor="system", session_id=None, conn=None):
        captured.append({
            "action": action,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "before": before,
            "after": after,
            "actor": actor,
        })
        return None

    monkeypatch.setattr(_audit, "log", _spy, raising=True)
    return captured


def _make_leaf(
    root: Path, *, tenant_id: str, agent_id: str,
    project_id: str = "default", product_line: str = "default",
    repo_hash: str = "self", file_size: int = 256,
    age_days: float = 0.0,
) -> Path:
    """Create a workspace leaf at the row-1 layout path.

    Mirrors ``test_y6_row6_workspace_gc._make_leaf``. Drops a
    ``.git`` placeholder + a sized blob, then back-dates everything
    by ``age_days`` so the stale-leaf scan picks it up.
    """
    leaf = (
        root / tenant_id / product_line / project_id / agent_id / repo_hash
    )
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / ".git").write_text("gitdir: /tmp/fake\n")
    (leaf / "blob.bin").write_bytes(b"\0" * file_size)
    if age_days > 0:
        when = time.time() - age_days * 86400
        os.utime(leaf / "blob.bin", (when, when))
        os.utime(leaf / ".git", (when, when))
        os.utime(leaf, (when, when))
    return leaf


def test_clone_via_registry_pin_blocks_gc_trash(
    isolated_ws_root, empty_registry, silenced_audit,
):
    """(C1 D1+D2) Registry-pinned workspace survives the sweep.

    Simulates "agent is in the middle of a clone" by pinning a
    ``WorkspaceInfo`` in the in-process registry. The GC sweep must
    observe the pin and skip the leaf — the on-disk leaf survives
    and its path lands in ``summary.skipped_busy`` with reason
    ``[registry]``. This is the cleanest representation of the
    Y10 row 5 acceptance scenario for the active-agent surface.
    """
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod

    leaf = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c1",
        agent_id=f"{_AGENT_PREFIX}-cloning",
        age_days=60,
    )

    # Pin the agent in the active registry as if its clone is mid-
    # flight — provision() registers _workspaces AFTER clone completes
    # but the registry pin gives the same skip semantics either way.
    ws_mod._workspaces[f"{_AGENT_PREFIX}-cloning"] = ws_mod.WorkspaceInfo(
        agent_id=f"{_AGENT_PREFIX}-cloning",
        task_id="t-y10r5-c1",
        branch=f"agent/{_AGENT_PREFIX}-cloning/t-y10r5-c1",
        path=leaf,
        repo_source="(test-fixture)",
    )

    summary = asyncio.run(gc_mod.sweep_once(stale_days=30))

    # (D1) clone_completes: leaf must be intact on disk.
    assert leaf.is_dir(), (
        "Registry-pinned leaf was trashed — Y10 row 5 D1 violation"
    )
    assert (leaf / "blob.bin").exists()
    assert summary.trashed == [], (
        "Registry-pinned leaf landed in trashed summary — Y10 row 5 "
        "D1 violation"
    )
    # (D2) gc_skips: skipped_busy must record the path with the
    # ``[registry]`` reason in brackets.
    assert any(
        str(leaf) in s and "[registry]" in s
        for s in summary.skipped_busy
    ), (
        f"Registry-pinned leaf not recorded in summary.skipped_busy "
        f"with [registry] reason; got: {summary.skipped_busy!r}"
    )


def test_clone_via_fresh_index_lock_blocks_gc_trash(
    isolated_ws_root, empty_registry, silenced_audit,
):
    """(C2 D1+D2) Fresh ``.git/index.lock`` blocks GC trash.

    Simulates "external CLI / chatops invocation cloning into the
    workspace" — no registry entry, but a fresh ``.git/index.lock``
    inside the leaf indicates an in-flight git op. The GC must
    treat this as busy and skip with reason ``[index.lock fresh
    ...]``. This is the alternate code path for the Y10 row 5
    acceptance scenario when the agent is operating outside the
    FastAPI lifespan (the registry never sees it).
    """
    from backend import workspace_gc as gc_mod

    leaf = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c2",
        agent_id=f"{_AGENT_PREFIX}-cli-clone",
        age_days=0,
    )

    # Replace the .git placeholder file with a real .git directory so
    # we can drop a fresh index.lock inside it. The leaf detection
    # (``(node / ".git").exists()`` in _iter_workspace_leaves) treats
    # both file and directory as a workspace.
    git_dir = leaf / ".git"
    git_dir.unlink()
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.write_text("")
    # Lock mtime = now → "fresh" by the 60s rule.
    os.utime(lock, (time.time(), time.time()))
    # Back-date the leaf itself so the stale-leaf scan would
    # otherwise pick it up — the fresh lock is what saves it, not
    # mtime youth.
    old = time.time() - 60 * 86400
    os.utime(leaf, (old, old))

    summary = asyncio.run(gc_mod.sweep_once(stale_days=30))

    # (D1) clone_completes: leaf intact.
    assert leaf.is_dir()
    assert summary.trashed == []
    # (D2) gc_skips: skipped_busy lists the leaf with the
    # ``[index.lock fresh (...)]`` reason. The exact age value is
    # timing-dependent; we just look for the substring.
    assert any(
        str(leaf) in s and "index.lock fresh" in s
        for s in summary.skipped_busy
    ), (
        f"Fresh-locked leaf not recorded in summary.skipped_busy "
        f"with [index.lock fresh (...)] reason; got: "
        f"{summary.skipped_busy!r}"
    )


def test_stale_index_lock_does_not_block_gc_negative_race_closed(
    isolated_ws_root, empty_registry, silenced_audit,
):
    """(C3 negative D2) Stale ``.git/index.lock`` (>60s) does NOT
    block GC.

    Negative invariant: the busy gate must distinguish "in-flight
    op" from "zombie lock left by a crashed agent". A lock older
    than ``_FRESH_LOCK_AGE_S`` (60s) is treated as stale —
    workspace will be trashed if otherwise eligible. Without this
    distinction, every crashed agent would leave an undeletable
    workspace forever.
    """
    from backend import workspace_gc as gc_mod

    leaf = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c3",
        agent_id=f"{_AGENT_PREFIX}-zombie",
        age_days=60,
    )
    git_dir = leaf / ".git"
    git_dir.unlink()
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.write_text("")
    # Lock mtime = 5 minutes ago → past the 60s freshness window.
    old = time.time() - 300
    os.utime(lock, (old, old))
    # Push the leaf mtime past the stale threshold too.
    old_leaf = time.time() - 60 * 86400
    os.utime(leaf, (old_leaf, old_leaf))

    summary = asyncio.run(gc_mod.sweep_once(stale_days=30))

    # Negative invariant: the leaf gets trashed. The race window is
    # CLOSED — the lock is too old to indicate a live op.
    assert not leaf.exists(), (
        "Stale-lock leaf was preserved — Y10 row 5 D2 negative "
        "invariant violation (zombie locks must not extend the "
        "race window indefinitely)"
    )
    assert len(summary.trashed) == 1
    # The path must NOT show up in skipped_busy because the gate
    # let it through.
    assert not any(
        f"{_AGENT_PREFIX}-zombie" in s for s in summary.skipped_busy
    )


def test_aggregate_audit_row_carries_skipped_busy_count_via_silenced_audit(
    isolated_ws_root, empty_registry, silenced_audit,
):
    """(C4 D3 aggregate-row reading) The sweep emits exactly one
    ``workspace.gc_executed`` row whose ``after.skipped_busy_count``
    reflects the busy-skip count.

    This is the Y10 row 5 acceptance "audit 記錄『skipped-live』" in
    its aggregate-row reading: the audit chain carries the skipped-
    busy signal via the ``workspace.gc_executed`` summary row's
    payload.
    """
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod

    # Pin two busy workspaces so the aggregate count is exactly 2.
    leaf_a = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c4a",
        agent_id=f"{_AGENT_PREFIX}-busy-a",
        age_days=60,
    )
    leaf_b = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c4b",
        agent_id=f"{_AGENT_PREFIX}-busy-b",
        age_days=60,
    )
    for leaf, agent in (
        (leaf_a, f"{_AGENT_PREFIX}-busy-a"),
        (leaf_b, f"{_AGENT_PREFIX}-busy-b"),
    ):
        ws_mod._workspaces[agent] = ws_mod.WorkspaceInfo(
            agent_id=agent, task_id="t-y10r5-c4",
            branch=f"agent/{agent}/t-y10r5-c4", path=leaf,
            repo_source="(test-fixture)",
        )

    asyncio.run(gc_mod.sweep_once(stale_days=30))

    gc_executed = [
        c for c in silenced_audit if c["action"] == "workspace.gc_executed"
    ]
    assert len(gc_executed) == 1, (
        f"Expected exactly one workspace.gc_executed audit row per "
        f"sweep; got {len(gc_executed)}: {gc_executed!r}"
    )
    row = gc_executed[0]
    assert row["entity_kind"] == "workspace"
    assert row["entity_id"] == "sweep"
    assert row["actor"] == "system:workspace-gc"
    # The aggregate count includes our two busy workspaces. Other
    # tests in this module run in isolation (autouse=False on
    # empty_registry), so 2 is the exact expectation here.
    assert row["after"]["skipped_busy_count"] == 2, (
        f"workspace.gc_executed.skipped_busy_count expected 2 (two "
        f"registry-pinned leaves); got {row['after']['skipped_busy_count']!r}"
    )
    # And the row must NOT mis-attribute trashed (we pinned everything
    # busy, so the sweep must not have trashed anything).
    assert row["after"]["trashed_count"] == 0


def test_no_skipped_live_audit_row_in_captured_audit_known_followup(
    isolated_ws_root, empty_registry, silenced_audit,
):
    """(C5 D3 follow-up gap) No per-workspace ``workspace.gc_skipped*``
    audit row is emitted today.

    Y10 row 5 honest follow-up: per-workspace skip rows would land
    here if the skip branch called ``_emit_gc_audit``. Today the
    branch ``continue``s before any audit emit. This test pins the
    current behaviour so a future change that adds per-workspace
    skip audit rows trips here and forces an update to the HANDOFF
    entry — same posture as Y10 row 4's
    ``test_require_project_member_does_not_consult_shares_known_followup``.
    """
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod

    leaf = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c5",
        agent_id=f"{_AGENT_PREFIX}-skip-probe",
        age_days=60,
    )
    ws_mod._workspaces[f"{_AGENT_PREFIX}-skip-probe"] = ws_mod.WorkspaceInfo(
        agent_id=f"{_AGENT_PREFIX}-skip-probe", task_id="t-y10r5-c5",
        branch=f"agent/{_AGENT_PREFIX}-skip-probe/t-y10r5-c5", path=leaf,
        repo_source="(test-fixture)",
    )

    asyncio.run(gc_mod.sweep_once(stale_days=30))

    skip_actions = [
        c["action"] for c in silenced_audit
        if "skipped" in (c.get("action") or "").lower()
        or "skip" in (c.get("action") or "").lower()
    ]
    assert skip_actions == [], (
        f"Found per-workspace skip-flavoured audit rows: "
        f"{skip_actions!r}. Y10 row 5 documented this as a follow-up "
        f"gap — flip the HANDOFF entry and remove this drift guard."
    )


def test_concurrent_clone_completes_while_sweep_runs(
    isolated_ws_root, empty_registry, silenced_audit,
):
    """(C6 D1+D2 timing race) A concurrent fake-clone task and
    ``sweep_once()`` task race; the leaf survives the sweep.

    This is the most direct representation of the Y10 row 5
    acceptance text: ``asyncio.gather`` runs a fake clone task that
    holds a fresh ``.git/index.lock`` (touching it periodically to
    keep it within the 60s freshness window) and writes files,
    while a ``sweep_once()`` task runs in parallel. After both
    tasks complete the leaf and its written files must exist; the
    sweep's summary must contain the leaf in ``skipped_busy``.

    The fake clone completes by writing a marker file *after* the
    sweep has had a chance to inspect the leaf — this is the
    "clone in flight at sweep time, completes after sweep" timeline.
    """
    from backend import workspace_gc as gc_mod

    leaf = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c6",
        agent_id=f"{_AGENT_PREFIX}-race-clone",
        age_days=60,
    )
    git_dir = leaf / ".git"
    git_dir.unlink()
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.write_text("")
    # Initial fresh lock.
    os.utime(lock, (time.time(), time.time()))
    # Push the leaf mtime past the stale threshold so the only
    # thing standing between it and the trash is the fresh-lock
    # gate.
    old_leaf = time.time() - 60 * 86400
    os.utime(leaf, (old_leaf, old_leaf))

    summary_holder: dict = {}

    async def _fake_clone_task() -> Path:
        """Stand-in for an in-flight ``provision()`` body. Holds the
        index.lock fresh (touches it every ~25ms, well below the
        60s freshness window) and writes a marker file at the end
        to simulate "clone completed"."""
        for _ in range(4):
            try:
                os.utime(lock, (time.time(), time.time()))
            except OSError:
                pass
            await asyncio.sleep(0.025)
        # "Clone completes" — write the marker, then drop the lock
        # (mirrors what real git does at the end of a clone).
        (leaf / "cloned-payload.bin").write_bytes(b"complete")
        try:
            lock.unlink()
        except OSError:
            pass
        return leaf

    async def _sweep_task() -> None:
        # Small jitter so the sweep doesn't beat the clone task to
        # the lock-touch.
        await asyncio.sleep(0.005)
        summary_holder["s"] = await gc_mod.sweep_once(stale_days=30)

    async def _race():
        return await asyncio.gather(_fake_clone_task(), _sweep_task())

    asyncio.run(_race())

    # (D1) clone_completes: leaf survives + the marker file the
    # fake-clone wrote at the end is present.
    assert leaf.is_dir(), (
        "Concurrent clone leaf was trashed by the sweep — Y10 row 5 "
        "D1 violation"
    )
    assert (leaf / "cloned-payload.bin").exists(), (
        "Fake-clone marker file missing after race — Y10 row 5 "
        "D1 violation"
    )
    # (D2) gc_skips: the sweep recorded the leaf as busy via the
    # fresh-lock gate. The lock was fresh for at least the first
    # ~100ms of the race so the sweep's stat() saw a young lock.
    summary = summary_holder.get("s")
    assert summary is not None, "sweep task did not complete"
    assert summary.trashed == [], (
        f"Concurrent-clone leaf landed in summary.trashed "
        f"({summary.trashed!r}) — Y10 row 5 D1 violation"
    )
    assert any(
        str(leaf) in s and "index.lock fresh" in s
        for s in summary.skipped_busy
    ), (
        f"Concurrent-clone leaf not recorded in summary.skipped_busy "
        f"with index.lock-fresh reason; got: {summary.skipped_busy!r}"
    )


def test_skipped_busy_format_string_includes_bracketed_reason(
    isolated_ws_root, empty_registry, silenced_audit,
):
    """(C7 D2 format invariant) The recorded format is exactly
    ``"{leaf_path} [{reason}]"`` — leaf path + space + bracketed
    reason.

    Direct e2e check of the format pinned by Block A's a7 source-
    grep. Two different leaves with two different skip reasons
    confirm the format works for both registry-pin and lock-pin
    code paths uniformly.
    """
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod

    # Reg-pin path.
    leaf_a = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c7a",
        agent_id=f"{_AGENT_PREFIX}-fmt-reg",
        age_days=60,
    )
    ws_mod._workspaces[f"{_AGENT_PREFIX}-fmt-reg"] = ws_mod.WorkspaceInfo(
        agent_id=f"{_AGENT_PREFIX}-fmt-reg", task_id="t-y10r5-c7a",
        branch=f"agent/{_AGENT_PREFIX}-fmt-reg/t-y10r5-c7a", path=leaf_a,
        repo_source="(test-fixture)",
    )

    # Fresh-lock path.
    leaf_b = _make_leaf(
        isolated_ws_root,
        tenant_id=f"{_TENANT_PREFIX}-c7b",
        agent_id=f"{_AGENT_PREFIX}-fmt-lock",
        age_days=0,
    )
    git_dir = leaf_b / ".git"
    git_dir.unlink()
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.write_text("")
    os.utime(lock, (time.time(), time.time()))
    old_leaf = time.time() - 60 * 86400
    os.utime(leaf_b, (old_leaf, old_leaf))

    summary = asyncio.run(gc_mod.sweep_once(stale_days=30))

    # Locate each leaf's record.
    reg_record = next(
        (s for s in summary.skipped_busy if str(leaf_a) in s), None,
    )
    lock_record = next(
        (s for s in summary.skipped_busy if str(leaf_b) in s), None,
    )
    assert reg_record is not None, (
        "Registry-pinned leaf not in skipped_busy"
    )
    assert lock_record is not None, (
        "Fresh-locked leaf not in skipped_busy"
    )

    # Format pin: each record is ``{path} [{reason}]``. Match the
    # exact format with re — the path comes first, a single space,
    # then a bracketed reason.
    reg_match = re.fullmatch(r"(.+) \[([^\[\]]+)\]", reg_record)
    lock_match = re.fullmatch(r"(.+) \[([^\[\]]+)\]", lock_record)
    assert reg_match is not None, (
        f"Registry skip record does not match '{{path}} [{{reason}}]' "
        f"format: {reg_record!r}"
    )
    assert lock_match is not None, (
        f"Lock skip record does not match '{{path}} [{{reason}}]' "
        f"format: {lock_record!r}"
    )
    # Reasons are exactly the documented contract strings.
    assert reg_match.group(2) == "registry"
    assert lock_match.group(2).startswith("index.lock fresh (")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block B — PG-required acceptance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
async def _y10r5_pg_isolated_ws(monkeypatch, tmp_path, pg_test_pool):
    """Block B fixture — PG pool + isolated workspace tree.

    Combines the workspace-tree isolation (tmp_path) with the
    session-scoped ``pg_test_pool`` from conftest. Resets the
    in-process workspace registry + GC singleton flag in teardown
    so a crashed test cannot poison the next one. Also resets the
    db_context ContextVars to ``None`` in teardown for the same
    reason — Y10 row 5 follows the row 4 fixture posture.
    """
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod
    from backend.db_context import set_project_id, set_tenant_id

    root = tmp_path / "y10r5-workspaces"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)
    gc_mod._reset_for_tests()
    try:
        yield {"pool": pg_test_pool, "root": root}
    finally:
        set_tenant_id(None)
        set_project_id(None)
        gc_mod._reset_for_tests()


@pytest.mark.asyncio
@_requires_pg
async def test_pg_skipped_busy_via_registry_emits_aggregate_audit_row_with_count(
    _y10r5_pg_isolated_ws,
):
    """(B1 D3 aggregate-row reading on real PG) Run the sweep with
    a registry-pinned busy leaf against a real PG audit chain and
    confirm the ``workspace.gc_executed`` row landed with
    ``after_json.skipped_busy_count >= 1``.

    Y10 row 5's acceptance text "audit 記錄『skipped-live』" satisfied
    today by the aggregate row — this test exercises the full
    PG-backed audit path (advisory lock + chain hash + INSERT) and
    confirms the row is queryable by action.
    """
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod

    pool = _y10r5_pg_isolated_ws["pool"]
    root = _y10r5_pg_isolated_ws["root"]

    leaf = _make_leaf(
        root, tenant_id=f"{_TENANT_PREFIX}-b1",
        agent_id=f"{_AGENT_PREFIX}-pg-busy",
        age_days=60,
    )
    ws_mod._workspaces[f"{_AGENT_PREFIX}-pg-busy"] = ws_mod.WorkspaceInfo(
        agent_id=f"{_AGENT_PREFIX}-pg-busy", task_id="t-y10r5-b1",
        branch=f"agent/{_AGENT_PREFIX}-pg-busy/t-y10r5-b1", path=leaf,
        repo_source="(test-fixture)",
    )

    # Capture the wall-clock anchor BEFORE the sweep so the SELECT
    # below filters to rows from this test only (the audit_log
    # table can have rows from prior runs; the timestamp filter
    # narrows to fresh ones).
    sweep_anchor_ts = time.time()

    summary = await gc_mod.sweep_once(stale_days=30)

    assert leaf.is_dir()
    assert any(
        str(leaf) in s and "[registry]" in s
        for s in summary.skipped_busy
    )

    # Read back the audit row from PG.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, entity_kind, entity_id, actor, after_json "
            "FROM audit_log "
            "WHERE action = 'workspace.gc_executed' "
            "  AND ts >= $1 "
            "ORDER BY id DESC",
            sweep_anchor_ts,
        )

    assert len(rows) >= 1, (
        "No workspace.gc_executed row landed in audit_log after "
        "sweep_once — Y10 row 5 D3 aggregate-row contract violation"
    )
    row = rows[0]
    assert row["entity_kind"] == "workspace"
    assert row["entity_id"] == "sweep"
    assert row["actor"] == "system:workspace-gc"
    import json as _json
    after_payload = _json.loads(row["after_json"])
    assert after_payload.get("skipped_busy_count", 0) >= 1, (
        f"workspace.gc_executed row's skipped_busy_count is "
        f"{after_payload.get('skipped_busy_count')!r}; expected >= 1 "
        f"because we pinned a registry-busy leaf — Y10 row 5 D3 "
        f"contract violation"
    )


@pytest.mark.asyncio
@_requires_pg
async def test_pg_skipped_busy_via_fresh_lock_emits_aggregate_audit_row_with_count(
    _y10r5_pg_isolated_ws,
):
    """(B2 D3 aggregate via lock path on real PG) Same as B1 but the
    busy gate fires via the fresh ``.git/index.lock`` path (no
    registry pin).

    Confirms the alternate code path also reaches the aggregate
    audit row with the correct count. Together with B1 the two
    busy reasons are both end-to-end exercised against the real
    audit chain.
    """
    from backend import workspace_gc as gc_mod

    root = _y10r5_pg_isolated_ws["root"]
    pool = _y10r5_pg_isolated_ws["pool"]

    leaf = _make_leaf(
        root, tenant_id=f"{_TENANT_PREFIX}-b2",
        agent_id=f"{_AGENT_PREFIX}-pg-lock",
        age_days=0,
    )
    git_dir = leaf / ".git"
    git_dir.unlink()
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.write_text("")
    os.utime(lock, (time.time(), time.time()))
    old_leaf = time.time() - 60 * 86400
    os.utime(leaf, (old_leaf, old_leaf))

    sweep_anchor_ts = time.time()
    summary = await gc_mod.sweep_once(stale_days=30)
    assert leaf.is_dir()
    assert any(
        str(leaf) in s and "index.lock fresh" in s
        for s in summary.skipped_busy
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT after_json FROM audit_log "
            "WHERE action = 'workspace.gc_executed' "
            "  AND ts >= $1 "
            "ORDER BY id DESC LIMIT 1",
            sweep_anchor_ts,
        )

    assert len(rows) == 1
    import json as _json
    after_payload = _json.loads(rows[0]["after_json"])
    assert after_payload.get("skipped_busy_count", 0) >= 1


@pytest.mark.asyncio
@_requires_pg
async def test_pg_no_per_workspace_skipped_live_audit_row_lands_in_chain_known_followup(
    _y10r5_pg_isolated_ws,
):
    """(B3 D3 follow-up gap on real PG) Confirm no per-workspace
    ``workspace.gc_skipped*`` action lands in audit_log today.

    Documented Y10 row 5 follow-up. The aggregate
    ``workspace.gc_executed`` row carries the count; per-workspace
    rows do not yet exist. Drift guard: if a future change wires
    per-workspace skip rows, this test trips and forces a HANDOFF
    update.
    """
    from backend import workspace as ws_mod
    from backend import workspace_gc as gc_mod

    pool = _y10r5_pg_isolated_ws["pool"]
    root = _y10r5_pg_isolated_ws["root"]

    leaf = _make_leaf(
        root, tenant_id=f"{_TENANT_PREFIX}-b3",
        agent_id=f"{_AGENT_PREFIX}-pg-skip-probe",
        age_days=60,
    )
    ws_mod._workspaces[f"{_AGENT_PREFIX}-pg-skip-probe"] = ws_mod.WorkspaceInfo(
        agent_id=f"{_AGENT_PREFIX}-pg-skip-probe", task_id="t-y10r5-b3",
        branch=f"agent/{_AGENT_PREFIX}-pg-skip-probe/t-y10r5-b3", path=leaf,
        repo_source="(test-fixture)",
    )

    sweep_anchor_ts = time.time()
    await gc_mod.sweep_once(stale_days=30)

    async with pool.acquire() as conn:
        skip_rows = await conn.fetch(
            "SELECT action FROM audit_log "
            "WHERE action LIKE 'workspace.gc_skipped%' "
            "  AND ts >= $1",
            sweep_anchor_ts,
        )

    assert len(skip_rows) == 0, (
        f"Found per-workspace workspace.gc_skipped* rows in PG: "
        f"{[r['action'] for r in skip_rows]!r}. Y10 row 5 documented "
        f"this as a follow-up — flip the HANDOFF entry and remove "
        f"this drift guard."
    )


@pytest.mark.asyncio
@_requires_pg
async def test_pg_aggregate_row_landed_under_advisory_lock_chain_intact(
    _y10r5_pg_isolated_ws,
):
    """(B4 D3 chain integrity on real PG) The aggregate
    ``workspace.gc_executed`` row is appended via the standard
    ``audit.log`` path which holds a per-tenant
    ``pg_advisory_xact_lock``. Verify the chain is intact (no
    divergence) for the chain that received the row.

    Sanity that the Y10 row 5 aggregate-row contract doesn't
    silently regress chain integrity under cross-worker concurrent
    sweeps. The verifier walks the chain from genesis and reports
    the first divergence.
    """
    from backend import audit, workspace as ws_mod
    from backend import workspace_gc as gc_mod
    from backend.db_context import current_tenant_id

    pool = _y10r5_pg_isolated_ws["pool"]
    root = _y10r5_pg_isolated_ws["root"]

    leaf = _make_leaf(
        root, tenant_id=f"{_TENANT_PREFIX}-b4",
        agent_id=f"{_AGENT_PREFIX}-pg-chain",
        age_days=60,
    )
    ws_mod._workspaces[f"{_AGENT_PREFIX}-pg-chain"] = ws_mod.WorkspaceInfo(
        agent_id=f"{_AGENT_PREFIX}-pg-chain", task_id="t-y10r5-b4",
        branch=f"agent/{_AGENT_PREFIX}-pg-chain/t-y10r5-b4", path=leaf,
        repo_source="(test-fixture)",
    )

    await gc_mod.sweep_once(stale_days=30)

    # The aggregate row's tenant lands wherever the ContextVar
    # resolves at emit-time. ``audit.log`` falls back to
    # ``t-default`` when no contextvar is set (the GC sweep runs
    # outside any request scope). Verify that chain.
    target_tid = current_tenant_id() or "t-default"
    ok, divergence = await audit.verify_chain(tenant_id=target_tid)
    assert ok, (
        f"Audit chain for tenant {target_tid!r} broken at row "
        f"{divergence!r} after sweep_once — Y10 row 5 D3 chain "
        f"integrity violation"
    )
    # Sanity: the aggregate row is queryable through the same
    # public ``audit.query`` surface T-series billing rollup uses.
    rows = await audit.query(entity_kind="workspace", limit=10)
    actions = [r["action"] for r in rows]
    assert "workspace.gc_executed" in actions, (
        "audit.query did not return the workspace.gc_executed row — "
        "Y10 row 5 D3 query-surface contract violation"
    )
