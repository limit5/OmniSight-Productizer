"""R8 #314 row 2876 — Integration test: task fail → retry → fresh worktree.

Locks the fifth (and final) sub-bullet under R8 in TODO.md (row 2931):

    "整合測試：task fail → retry → old worktree 消失 + new worktree 乾淨
     + anchor_sha 正確 + audit logged"

This is **the** end-to-end contract test for the R8 retry primitive.
The four prior sub-bullets (rows 2872-2875) each lock one piece in
isolation:

    row 2872  Anchor SHA captured at provision time           (test_workspace_anchor.py)
    row 2873  discard_and_recreate() helper                   (test_workspace_discard_recreate.py)
    row 2874  audit_log retry.worktree_recreated row          (test_workspace_discard_recreate.py)
    row 2875  startup orphan worktree scan                    (test_workspace_orphan_cleanup.py)

This file wires those pieces into the **full retry cycle** an
operator-facing orchestrator would run:

    1. provision()  — anchor_sha captured into WorkspaceInfo + a CATC TaskCard
    2. agent works — commits, scratch artifacts, dirty index ("task fail" state)
    3. orchestrator reads ``catc.navigation.anchor_commit_sha``  ← retry trigger
    4. orchestrator calls discard_and_recreate(agent_id, anchor_sha, reason)
    5. assertions:
        - old worktree contents (agent's commits + scratch files) ARE GONE
        - new worktree HEAD == anchor_sha (CLEAN reset)
        - WorkspaceInfo.anchor_sha is unchanged (still the original anchor)
        - audit row ``retry.worktree_recreated`` is emitted with the
          right shape (before.worktree_path, before.branch_tip,
          after.anchor_sha, after.reason)
        - SSE ``workspace.retried`` event is emitted on the bus

There is NO retry orchestrator wired up in master yet (the design doc
§4 sketches the future ``orchestrator.retry_agent_task`` call path).
This file simulates that orchestrator with a tiny inline helper
(``_simulate_orchestrator_retry``) that does the exact two-step
"read CATC → call discard_and_recreate" sequence the future real
orchestrator will do — so the day a real orchestrator lands, it just
has to keep this contract green.

Why this matters as an integration test (vs. the unit tests):
the unit tests in ``test_workspace_discard_recreate.py`` exercise
``discard_and_recreate`` in isolation, asserting that *given*
``anchor_sha``, the function does the right thing. This file proves
the ``anchor_sha`` actually *flows* end-to-end from the CATC payload
captured at provision time, through the orchestrator-style retry call,
into the audit chain — there's no place a unit test can catch a wiring
gap (e.g. the orchestrator forgetting to thread ``anchor_commit_sha``
through the CATC card and passing ``None`` instead).

Audit log + SSE event are spied (monkeypatched) — same pattern as the
sibling unit tests. Real PG + real bus aren't required because the
*contract* this row owns is "the orchestrator-style retry trigger
calls audit/SSE with the right payload"; chain-hash integrity is
``test_audit.py``'s domain, real SSE delivery is the bus's domain.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend import workspace as ws_mod
from backend.catc import ImpactScope, Navigation, TaskCard


# ---------------------------------------------------------------------------
# Fixtures: throwaway repo + redirected workspaces root + audit/SSE spies
# (same shape as test_workspace_discard_recreate.py / test_workspace_anchor.py
#  to keep the suite cohesive)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True,
        stderr=subprocess.STDOUT,
    ).strip()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A 1-commit git repo we can use as a worktree source."""
    repo = tmp_path / "src_repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@local", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def redirected_ws_root(tmp_path: Path, monkeypatch):
    """Point workspace module at a tmp root so tests can't pollute the
    real ``.agent_workspaces`` of the project repo."""
    root = tmp_path / "ws_root"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    return root


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    """Each test starts with an empty ``_workspaces`` dict — guarantees
    no cross-test bleed of provisioned WorkspaceInfo entries."""
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)


@pytest.fixture
def captured_audit(monkeypatch):
    """Spy on ``backend.audit.log`` — captures the kwargs each call site
    passes so we can assert payload shape across the retry pipeline.

    Mirror of test_workspace_discard_recreate.py's `_spy_audit_log`,
    extracted to a fixture here because integration-style tests in this
    file may emit multiple audit rows per scenario (provision-time +
    retry-time) and want a single timeline to query."""
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
            "session_id": session_id,
        })
        return None

    monkeypatch.setattr(_audit, "log", _spy, raising=True)
    return captured


@pytest.fixture
def captured_sse(monkeypatch):
    """Spy on ``backend.events.bus.publish`` — captures every (channel,
    data) pair so retry-cycle SSE assertions can look at the full
    event stream (not just the last event)."""
    from backend import events as _events
    captured: list[tuple[str, dict]] = []
    real_publish = _events.bus.publish

    def _spy(channel: str, data, **kw):
        captured.append((
            channel,
            dict(data) if isinstance(data, dict) else data,
        ))
        return real_publish(channel, data, **kw)

    monkeypatch.setattr(_events.bus, "publish", _spy, raising=True)
    return captured


# ---------------------------------------------------------------------------
# Test helpers — simulate "agent task" + "orchestrator retry"
# ---------------------------------------------------------------------------


def _agent_task_does_work(ws_path: Path) -> dict:
    """Simulate an agent making changes that a retry must wipe.

    Returns a dict of artefacts the retry path should subsequently NOT
    find — used as the "task fail" baseline for the integration test
    body to assert against post-retry."""
    # Tracked-file edit + agent commit on the branch
    (ws_path / "agent_progress.md").write_text("agent partial progress\n")
    _git("add", "agent_progress.md", cwd=ws_path)
    _git("commit", "-q", "-m", "agent: partial work before failure", cwd=ws_path)
    commit_sha = _git("rev-parse", "HEAD", cwd=ws_path)

    # Scratch artefacts (untracked + ignored-style) — the cargo-cult
    # build leftovers + .o files that whitepaper §三.2's git clean
    # would have missed.
    (ws_path / "build_scratch.bin").write_bytes(b"\x42" * 1024)
    (ws_path / "node_modules").mkdir(exist_ok=True)
    (ws_path / "node_modules" / "fake_pkg.txt").write_text("garbage\n")

    # Dirty tracked file (uncommitted modification on top of the agent
    # commit) — ``git clean`` wouldn't touch this, ``git checkout .``
    # would but only for tracked files.
    (ws_path / "README.md").write_text("AGENT MUTATED THIS\n")

    return {
        "commit_sha": commit_sha,
        "tracked_artifact": "agent_progress.md",
        "untracked_artifact": "build_scratch.bin",
        "ignored_dir": "node_modules",
        "dirty_tracked": "README.md",
    }


async def _simulate_orchestrator_retry(
    agent_id: str, task_card: TaskCard, reason: str = "retry",
) -> ws_mod.WorkspaceInfo:
    """Stand-in for the future ``orchestrator.retry_agent_task`` call.

    Per ``docs/design/r8-idempotent-retry-worktree.md`` §4 the real
    orchestrator's retry path is:

        catc = read_catc(agent_id)
        anchor = catc.navigation.anchor_commit_sha
        info = await WorkspaceManager.discard_and_recreate(agent_id, anchor)
        # (audit log + SSE happen inside discard_and_recreate)

    We inline that two-step here. Once the orchestrator lands this
    helper goes away; the integration test then calls the real
    orchestrator and the rest of the assertions hold unchanged."""
    anchor = task_card.navigation.anchor_commit_sha
    if not anchor:
        # Per design §5: legacy CATC without anchor falls back to the
        # whitepaper's clean+checkout path. Real orchestrator does that
        # decision; this helper just reflects it as an explicit error so
        # tests can assert "anchor flows through".
        raise RuntimeError(
            f"CATC for {agent_id!r} has no anchor_commit_sha — "
            "orchestrator must use legacy fallback path"
        )
    return await ws_mod.discard_and_recreate(
        agent_id=agent_id, anchor_sha=anchor, reason=reason,
    )


def _make_catc(anchor_sha: str | None) -> TaskCard:
    """Build a minimal valid CATC TaskCard with the given anchor.

    The ``impact_scope.allowed`` glob and ``acceptance_criteria`` are
    schema-required; their values don't matter for retry semantics."""
    return TaskCard(
        jira_ticket="OMNI-314",
        acceptance_criteria="Worktree resets cleanly to anchor on retry.",
        navigation=Navigation(
            entry_point="backend/workspace.py",
            impact_scope=ImpactScope(allowed=["backend/**"]),
            anchor_commit_sha=anchor_sha,
        ),
    )


# ===========================================================================
# 1) Headline contract — the literal TODO row 2876 statement, end-to-end
# ===========================================================================


def test_e2e_task_fail_then_retry_full_cycle(
    fake_repo, redirected_ws_root, captured_audit, captured_sse,
):
    """The TODO row 2876 acceptance test, exact wording:

        task fail → retry → old worktree 消失 + new worktree 乾淨
                          + anchor_sha 正確 + audit logged

    Wires every R8 piece in series: provision (anchor capture) →
    "task fail" (agent dirties the tree) → orchestrator retry
    (CATC anchor → discard_and_recreate) → assertions on all four
    post-conditions.
    """
    # ── Stage 1: provision + anchor capture ───────────────────────────
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-r8-int",
        task_id="task-r8-int",
        repo_source=str(fake_repo),
    ))
    anchor = info.anchor_sha
    assert anchor and len(anchor) == 40
    ws_path = info.path
    # Snapshot of the post-provision baseline status. ``provision()``
    # adds the ``/test_assets/`` line to ``.gitignore`` (CLAUDE.md
    # Safety Rule defence) which surfaces as an untracked-file entry
    # on a fresh repo; the retry primitive re-applies the same overlay,
    # so the post-retry status must match this baseline rather than
    # be empty.
    baseline_status = _git("status", "--porcelain", cwd=ws_path)

    # CATC payload would be persisted by the orchestrator at task-
    # provision time. We build one here with the anchor we just
    # captured — proves the round-trip from WorkspaceInfo →
    # CATC → orchestrator-side retry holds.
    catc = _make_catc(anchor)
    assert catc.navigation.anchor_commit_sha == anchor

    # ── Stage 2: simulate task failure (agent dirties the tree) ───────
    artefacts = _agent_task_does_work(ws_path)
    pre_retry_tip = _git("rev-parse", "HEAD", cwd=ws_path)
    assert pre_retry_tip != anchor, (
        "agent commit should have moved HEAD past anchor"
    )
    # Sanity: every "fail" artefact is actually present before retry.
    assert (ws_path / artefacts["tracked_artifact"]).exists()
    assert (ws_path / artefacts["untracked_artifact"]).exists()
    assert (ws_path / artefacts["ignored_dir"]).is_dir()
    assert (ws_path / artefacts["dirty_tracked"]).read_text() == \
        "AGENT MUTATED THIS\n"

    # ── Stage 3: orchestrator-style retry (CATC → discard_and_recreate) ─
    returned = asyncio.run(_simulate_orchestrator_retry(
        agent_id="agent-r8-int",
        task_card=catc,
        reason="task-fail-retry",
    ))

    # ── Stage 4 — Assertion A: same WorkspaceInfo identity preserved ──
    assert returned is info
    assert returned.path == ws_path
    assert returned.anchor_sha == anchor  # anchor_sha must NOT mutate

    # ── Stage 4 — Assertion B: "old worktree 消失" ────────────────────
    # The agent's commit, the agent's tracked file, the agent's scratch
    # binary, the agent's stray node_modules dir, AND the dirty edit to
    # README.md must ALL be gone — that's what differentiates this
    # retry primitive from `git clean -fd` + `git checkout .` (per the
    # rationale in r8 design doc §3, the rejected whitepaper recipe
    # would leave the ignored dir + scratch binary behind).
    assert not (ws_path / artefacts["tracked_artifact"]).exists()
    assert not (ws_path / artefacts["untracked_artifact"]).exists()
    assert not (ws_path / artefacts["ignored_dir"]).exists()
    # README.md is back to the anchor's content (not the agent's edit).
    assert (ws_path / artefacts["dirty_tracked"]).read_text() == "hello\n"

    # ── Stage 4 — Assertion C: "new worktree 乾淨" + "anchor_sha 正確" ─
    head_after = _git("rev-parse", "HEAD", cwd=ws_path)
    assert head_after == anchor, (
        f"new worktree HEAD should be anchor {anchor[:12]}, got "
        f"{head_after[:12]}"
    )
    # Working tree is "clean of agent residue" — every entry in the
    # post-retry status must also be in the post-provision baseline.
    # ``provision()`` produces some untracked-file entries (``.gitignore``
    # /test_assets/ overlay, ``.omnisight/`` platform-hint dir) that
    # ``discard_and_recreate`` may or may not recreate depending on the
    # subsystem; what matters for the retry contract is that NO agent
    # leftover (agent_progress.md, build_scratch.bin, node_modules/)
    # appears post-retry. Subset semantics (post-retry ⊆ baseline)
    # encodes "no residue" without falsely failing on infra deltas.
    baseline_lines = set(baseline_status.splitlines())
    post_retry_lines = set(_git("status", "--porcelain", cwd=ws_path).splitlines())
    leftover = post_retry_lines - baseline_lines
    assert not leftover, (
        f"new worktree carries agent residue not in the provision "
        f"baseline: {leftover}"
    )
    # Branch ref in source repo is realigned to anchor (proves the
    # branch was reborn at the anchor SHA, not the abandoned tip).
    src_branch_tip = _git("rev-parse", info.branch, cwd=fake_repo)
    assert src_branch_tip == anchor

    # Registry side effects: commit_count reset, status re-marked
    # active (so the next provision-or-retry call sees a clean slate).
    assert returned.commit_count == 0
    assert returned.status == "active"

    # ── Stage 4 — Assertion D: "audit logged" with full payload ───────
    retry_audit = [
        a for a in captured_audit
        if a["action"] == "retry.worktree_recreated"
    ]
    assert len(retry_audit) == 1, (
        f"expected exactly one retry.worktree_recreated audit row, "
        f"got {[a['action'] for a in captured_audit]}"
    )
    row = retry_audit[0]
    assert row["entity_kind"] == "workspace"
    assert row["entity_id"] == "agent-r8-int"
    # before: pre-discard branch tip (the agent's commit), worktree path,
    # branch name. Forensics query "what did we throw away" maps here.
    before = row["before"] or {}
    assert before.get("worktree_path") == str(ws_path)
    assert before.get("branch_tip") == pre_retry_tip
    assert before.get("branch") == info.branch
    # after: new HEAD (anchor), worktree path (same — same-path-reuse),
    # branch name, the orchestrator's reason label.
    after = row["after"] or {}
    assert after.get("anchor_sha") == anchor
    assert after.get("worktree_path") == str(ws_path)
    assert after.get("branch") == info.branch
    assert after.get("reason") == "task-fail-retry"

    # ── Stage 4 — Assertion E: SSE workspace.retried event emitted ────
    retried_events = [
        d for ch, d in captured_sse
        if ch == "workspace"
        and isinstance(d, dict)
        and d.get("agent_id") == "agent-r8-int"
        and d.get("action") == "retried"
    ]
    assert len(retried_events) == 1
    detail = retried_events[0]["detail"]
    assert anchor[:12] in detail
    assert "task-fail-retry" in detail
    assert info.branch in detail


# ===========================================================================
# 2) Anchor SHA flows through CATC, not just from in-memory WorkspaceInfo
# ===========================================================================


def test_anchor_sha_flows_from_catc_through_retry(
    fake_repo, redirected_ws_root, captured_audit,
):
    """Proves the anchor the orchestrator passes to discard_and_recreate
    is the one persisted in the CATC TaskCard — not anything pulled
    out of the in-process registry.

    Why this matters: in production, the worker that triggers retry
    might NOT be the worker that originally provisioned (per-worker
    affinity is best-effort, not strict). The CATC card is the
    durable, cross-worker source of truth for anchor_sha. If the
    orchestrator-style helper accidentally bypassed the card and read
    ``WorkspaceInfo.anchor_sha`` directly, this test wouldn't catch
    the mistake — so we explicitly exercise the CATC path.
    """
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-catc",
        task_id="task-catc",
        repo_source=str(fake_repo),
    ))
    persisted_anchor = info.anchor_sha

    # Round-trip the CATC card through JSON to prove the anchor
    # survives serialisation — what would actually happen on the
    # message queue between orchestrator and worker.
    raw = _make_catc(persisted_anchor).to_json()
    catc_from_wire = TaskCard.from_json(raw)
    assert catc_from_wire.navigation.anchor_commit_sha == persisted_anchor

    _agent_task_does_work(info.path)

    asyncio.run(_simulate_orchestrator_retry(
        agent_id="agent-catc",
        task_card=catc_from_wire,
        reason="catc-roundtrip",
    ))

    head_after = _git("rev-parse", "HEAD", cwd=info.path)
    assert head_after == persisted_anchor

    row = next(
        a for a in captured_audit
        if a["action"] == "retry.worktree_recreated"
    )
    assert (row["after"] or {}).get("anchor_sha") == persisted_anchor


# ===========================================================================
# 3) Idempotent multi-cycle retry — fail → retry → fail → retry → fail → retry
# ===========================================================================


def test_idempotent_multi_cycle_retry(
    fake_repo, redirected_ws_root, captured_audit, captured_sse,
):
    """Real production retry orchestrators retry until success or until
    a max-retries cap. After 3 consecutive task-fail-retry cycles the
    invariants must STILL hold: HEAD == anchor, working tree clean,
    one audit row per cycle (not e.g. one row total because of stale
    state, not zero rows because the helper short-circuited)."""
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-loop",
        task_id="task-loop",
        repo_source=str(fake_repo),
    ))
    anchor = info.anchor_sha
    catc = _make_catc(anchor)
    baseline_status = _git("status", "--porcelain", cwd=info.path)

    for cycle in range(3):
        # Each cycle: agent does work → fails → orchestrator retries.
        _agent_task_does_work(info.path)
        # Sentinel file unique to this cycle so we can prove subsequent
        # retries actually wipe THIS cycle's leftover (not just the
        # first cycle's by accident).
        sentinel = info.path / f"cycle_{cycle}_sentinel.dat"
        sentinel.write_bytes(b"X" * 32)
        assert sentinel.exists()

        asyncio.run(_simulate_orchestrator_retry(
            agent_id="agent-loop",
            task_card=catc,
            reason=f"cycle-{cycle}",
        ))

        # After each retry: HEAD == anchor, working tree carries no
        # agent residue (subset semantics — see headline test), and
        # the cycle sentinel is gone.
        assert _git("rev-parse", "HEAD", cwd=info.path) == anchor
        baseline_lines = set(baseline_status.splitlines())
        post_lines = set(
            _git("status", "--porcelain", cwd=info.path).splitlines()
        )
        assert not (post_lines - baseline_lines), (
            f"cycle {cycle}: residue leaked across retry: "
            f"{post_lines - baseline_lines}"
        )
        assert not sentinel.exists()
        # WorkspaceInfo's anchor_sha is immutable across retries.
        assert info.anchor_sha == anchor

    # Audit timeline: exactly 3 retry rows, in cycle order, each with
    # the right reason label. Proves no row got dropped (which would
    # be a load-bearing bug for forensics: missing rows fragment the
    # incident timeline).
    retries = [
        a for a in captured_audit
        if a["action"] == "retry.worktree_recreated"
    ]
    assert len(retries) == 3
    assert [(r["after"] or {}).get("reason") for r in retries] == [
        "cycle-0", "cycle-1", "cycle-2",
    ]

    # Three SSE retried events, one per cycle.
    sse_retried = [
        d for ch, d in captured_sse
        if ch == "workspace" and isinstance(d, dict)
        and d.get("action") == "retried"
        and d.get("agent_id") == "agent-loop"
    ]
    assert len(sse_retried) == 3


# ===========================================================================
# 4) Operator rollback path (R1 #307 ChatOps `/omnisight rollback`) — same
#    plumbing, different ``reason`` label
# ===========================================================================


def test_chatops_rollback_uses_same_retry_pipeline(
    fake_repo, redirected_ws_root, captured_audit, captured_sse,
):
    """Per design doc §4 (last bullet): R1's ``/omnisight rollback``
    reuses ``discard_and_recreate`` with a different ``reason`` label.
    The integration assertion is that the orchestrator path and the
    operator path are NOT two parallel implementations — they go
    through the same primitive, so the audit semantics differ only in
    the ``reason`` field (and potentially in ``actor`` once R1 lands
    actor-attribution; that's not in this row's scope).
    """
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-rollback",
        task_id="task-rollback",
        repo_source=str(fake_repo),
    ))
    catc = _make_catc(info.anchor_sha)
    _agent_task_does_work(info.path)

    asyncio.run(_simulate_orchestrator_retry(
        agent_id="agent-rollback",
        task_card=catc,
        reason="operator-rollback",  # ← the ChatOps label
    ))

    row = next(
        a for a in captured_audit
        if a["action"] == "retry.worktree_recreated"
    )
    # Same action name (proves single audit timeline for forensics —
    # operator can grep one action and see both auto-retries and
    # operator rollbacks).
    assert row["entity_kind"] == "workspace"
    assert (row["after"] or {}).get("reason") == "operator-rollback"

    sse = next(
        d for ch, d in captured_sse
        if ch == "workspace" and isinstance(d, dict)
        and d.get("action") == "retried"
        and d.get("agent_id") == "agent-rollback"
    )
    assert "operator-rollback" in sse["detail"]


# ===========================================================================
# 5) Legacy CATC without anchor — orchestrator escalates, NOT silently no-ops
# ===========================================================================


def test_orchestrator_rejects_catc_without_anchor(
    fake_repo, redirected_ws_root, captured_audit,
):
    """Per design §5: CATC payloads predating R8 (anchor_commit_sha=None)
    should fall back to the legacy clean+checkout path. The orchestrator
    is responsible for that decision — discard_and_recreate itself
    refuses None anchors. This test pins the orchestrator-side contract
    so a future regression where the orchestrator silently passes
    None doesn't slip through. The test simulates a legacy CATC and
    asserts the orchestrator escalates cleanly (RuntimeError with a
    message naming the agent, NOT a silent corruption of the
    workspace state)."""
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-legacy",
        task_id="task-legacy",
        repo_source=str(fake_repo),
    ))
    legacy_catc = _make_catc(None)  # ← anchor missing (legacy payload)
    _agent_task_does_work(info.path)
    pre_tip = _git("rev-parse", "HEAD", cwd=info.path)

    with pytest.raises(RuntimeError, match="anchor_commit_sha"):
        asyncio.run(_simulate_orchestrator_retry(
            agent_id="agent-legacy",
            task_card=legacy_catc,
        ))

    # Fail-safe contract: workspace state is UNCHANGED on
    # orchestrator-side rejection. The retry didn't proceed, so the
    # agent's progress (commit + scratch) must still be present —
    # otherwise we'd be losing work without the corresponding retry
    # actually running.
    assert _git("rev-parse", "HEAD", cwd=info.path) == pre_tip
    assert (info.path / "agent_progress.md").exists()
    # No retry audit row — the helper rejected before reaching audit.
    assert [
        a for a in captured_audit
        if a["action"] == "retry.worktree_recreated"
    ] == []


# ===========================================================================
# 6) Audit and SSE happen in the same call — receipts are paired, not split
# ===========================================================================


def test_audit_and_sse_emitted_together_per_retry(
    fake_repo, redirected_ws_root, captured_audit, captured_sse,
):
    """Forensics + dashboard parity: every retry must produce BOTH an
    audit row AND an SSE event. If only one fired, operators staring at
    the dashboard would see a retry the audit log missed (or vice versa)
    — gaps in either side undermine R8's "auditable retry" goal.

    This test is paranoid about pairing: count(audit) == count(SSE) ==
    count(actual retries called). The unit tests assert each side
    independently; the integration assertion is they stay in lockstep
    through the orchestrator-style call path.
    """
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-pair",
        task_id="task-pair",
        repo_source=str(fake_repo),
    ))
    catc = _make_catc(info.anchor_sha)

    n_retries = 4
    for i in range(n_retries):
        _agent_task_does_work(info.path)
        asyncio.run(_simulate_orchestrator_retry(
            agent_id="agent-pair",
            task_card=catc,
            reason=f"pair-{i}",
        ))

    audit_count = sum(
        1 for a in captured_audit
        if a["action"] == "retry.worktree_recreated"
    )
    sse_count = sum(
        1 for ch, d in captured_sse
        if ch == "workspace" and isinstance(d, dict)
        and d.get("agent_id") == "agent-pair"
        and d.get("action") == "retried"
    )

    assert audit_count == n_retries
    assert sse_count == n_retries
    assert audit_count == sse_count


# ===========================================================================
# 7) anchor_sha drift sentinel — CATC survives intermediate provisions
# ===========================================================================


def test_anchor_in_catc_survives_intermediate_agent_commits(
    fake_repo, redirected_ws_root, captured_audit,
):
    """The anchor a CATC card holds is *frozen at provision time*.
    Even if the agent makes 5 commits past anchor, the CATC card we
    hold (presumably persisted in a queue / DB) still points to the
    original anchor — and the retry must reset to THAT anchor, not
    "whatever HEAD was last".

    Catches the failure mode where someone ports the orchestrator and
    accidentally re-reads HEAD instead of using the persisted anchor —
    the retry would then reset to the agent's last commit (which is
    almost certainly the bug state), defeating the whole point of
    R8."""
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-drift",
        task_id="task-drift",
        repo_source=str(fake_repo),
    ))
    original_anchor = info.anchor_sha
    catc = _make_catc(original_anchor)  # captured at provision time

    # Agent makes multiple commits past anchor — this is the "agent has
    # been working" trajectory.
    for i in range(5):
        (info.path / f"step_{i}.txt").write_text(f"step {i}\n")
        _git("add", f"step_{i}.txt", cwd=info.path)
        _git("commit", "-q", "-m", f"step {i}", cwd=info.path)
    drifted_head = _git("rev-parse", "HEAD", cwd=info.path)
    assert drifted_head != original_anchor

    asyncio.run(_simulate_orchestrator_retry(
        agent_id="agent-drift",
        task_card=catc,  # still pointing at original_anchor
        reason="drift-check",
    ))

    head_after = _git("rev-parse", "HEAD", cwd=info.path)
    assert head_after == original_anchor, (
        f"retry reset to wrong SHA: expected anchor {original_anchor[:12]}, "
        f"got {head_after[:12]}"
    )
    # All 5 commits are gone from the working tree.
    for i in range(5):
        assert not (info.path / f"step_{i}.txt").exists()
