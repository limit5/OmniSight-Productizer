"""Y6 #282 row 8 — composition matrix for the Workspace Hierarchy + GC + Quota epic.

Rows 1-7 each have their own per-row drift-guard tests
(``test_workspace_hierarchy.py`` … ``test_y6_row7_url_hash_collision.py``).
Each is unit-shaped: it pins ONE contract introduced by ONE sub-bullet
under Y6.

Row 8's value-add is the *composition* — realistic operator scenarios
that cross row boundaries and assert the cross-row invariants survive
when several contracts run together. Same posture as Y4 row 8
(``test_y4_row8_project_matrix.py``).

Test families
─────────────
A. **Same-name repo collision matrix** — two repos both named ``foo``
   pulled from different URLs by the SAME agent under the SAME tenant /
   project must land on distinct on-disk leaves and **each must keep
   its own README content** (audit-bug regression at the composition
   layer; row 7 covers it from the URL-hash side, row 1 covers it from
   the path-resolver side, row 8 ties them through the live
   ``provision()`` end-to-end pipeline).

B. **Per-tenant quota isolation** — bytes seeded under tenant A's
   workspace slice MUST NOT leak into tenant B's
   ``measure_tenant_usage`` / ``check_hard_quota`` accounting. Pins the
   row-1 path layout × row-5 quota integration cross-product.

C. **Quota breach → per-project LRU eviction** — when tenant is over
   ``hard_bytes`` the GC reaper trashes the **oldest** workspace in
   that project first, leaving the newest alone (spec quote: "優先刪
   舊的 workspace（per-project LRU）而非新的"). Composes row 5 (quota
   measurement) × row 6 (GC reaper) × row 1 (per-project leaf grouping).

D. **Active-agent guard** — even a *stale* leaf whose owning ``agent_id``
   sits in the in-process ``_workspaces`` registry MUST survive the
   sweep. Composes row 6 (GC reaper) × row 3 (registry lifecycle): the
   GC must defer to the registry, never take the active workspace's
   files out from under a running agent.

E. **OMNISIGHT_WORKSPACE_ROOT env knob** — flipping the env var must
   actually re-target the on-disk layout end-to-end:
     1. ``Settings().workspace_root`` reflects the env value
        (already tested in ``test_config.py`` but re-pinned here as a
        composition-layer guard so a regression that decoupled row 2
        from the runtime path immediately fails THIS test too)
     2. The migrator's ``_default_target_root`` resolves the same
        value (row 4 honours the operator's choice without a CLI flag)
     3. ``backend.tenant_quota._tenant_workspaces_root`` follows the
        live ``backend.workspace._WORKSPACES_ROOT`` so a runtime
        rebase of the constant flows into quota measurement (the
        runtime-bridging seam relies on this — row 5's measurement
        calls into row 1's path-of-truth at quota-measure time)

Pure-unit posture — no PG, no live FastAPI app, no network. Audit /
SSE / git plumbing is exercised through real subprocess git invocations
under ``tmp_path`` (matches the row 5 / row 7 pattern).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend import tenant_fs as tfs
from backend import tenant_quota as tq
from backend import workspace as ws_mod
from backend import workspace_gc as gc_mod


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures — isolate every disk root onto pytest's tmp_path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def isolated_roots(monkeypatch, tmp_path):
    """Redirect every quota-relevant root onto ``tmp_path``.

    Mirrors row-5's ``isolated_roots`` fixture so the matrix sees the
    same shape of world as the per-row tests it composes — no leakage
    onto the dev host's ``./.agent_workspaces`` / ``./data/tenants``
    trees.
    """
    tenants_dir = tmp_path / "tenants"
    ingest_dir = tmp_path / "tmp_ingest"
    workspaces_dir = tmp_path / "workspaces"
    tenants_dir.mkdir()
    ingest_dir.mkdir()
    workspaces_dir.mkdir()
    monkeypatch.setattr(tfs, "_TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(tfs, "_INGEST_BASE", ingest_dir)
    monkeypatch.setattr(
        ws_mod, "_WORKSPACES_ROOT", workspaces_dir, raising=True,
    )
    tq._reset_for_tests()
    yield tmp_path


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    """Each test starts with an empty in-process workspace registry +
    a fresh GC singleton flag so cross-test bleed never masks a real
    failure here."""
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)
    gc_mod._reset_for_tests()


@pytest.fixture
def silenced_audit(monkeypatch):
    """Spy on ``audit.log`` without needing a live PG pool."""
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


@pytest.fixture
def captured_sse(monkeypatch):
    """Recorder for ``backend.events.bus.publish`` so we can assert
    composition-layer events surface (workspace_gc / workspace
    provisioned / quota_evicted)."""
    from backend import events as _events
    captured: list[tuple[str, dict]] = []

    def _publish(topic, payload, *args, **kwargs):
        captured.append((topic, payload))

    monkeypatch.setattr(_events.bus, "publish", _publish, raising=True)
    return captured


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _make_repo(path: Path, marker: str) -> Path:
    """Create a tiny initialised git repo with one tracked README so
    the composition tests can prove the **right** repo's content
    survived a same-name collision."""
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@local", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "README.md").write_text(marker)
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "initial", cwd=path)
    return path


def _seed_leaf(
    root: Path, *, tenant_id: str, project_id: str = "default",
    product_line: str = "default", agent_id: str = "agent-x",
    repo_hash: str = "self", file_size: int = 256,
    age_days: float = 0.0,
) -> Path:
    """Create a fake workspace leaf at the row-1 5-layer path. Drops a
    ``.git`` placeholder + a sized blob, then back-dates the leaf by
    ``age_days`` so the GC sweep's mtime cutoff sees it as stale."""
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family A — Same-name repo collision matrix (row 1 × row 7 e2e)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSameNameRepoMatrix:
    """Audit-bug regression at the composition layer.

    Row 7's dedicated test (``test_y6_row7_url_hash_collision``) covers
    the URL-hash function and the path-resolver branch in isolation.
    Row 1's test covers the path layout. Row 8 ties them through the
    live ``provision()`` pipeline — the END-TO-END check that two
    same-basename repos in fact coexist on disk under the same agent
    + tenant + project, with each leaf still holding its OWN repo's
    content (no silent overwrite).
    """

    def test_two_same_basename_repos_under_same_agent_coexist(
        self, isolated_roots, tmp_path,
    ):
        # Two repos both literally named ``foo`` — premise of the audit
        # bug. team_a publishes one in ``team_a/foo``, team_b in
        # ``team_b/foo``.
        repo_a = _make_repo(tmp_path / "team_a" / "foo", marker="A-content\n")
        repo_b = _make_repo(tmp_path / "team_b" / "foo", marker="B-content\n")
        assert repo_a.name == repo_b.name == "foo"

        # SAME tenant + project + agent — only the URL differs. The
        # only path-axis that disambiguates these two clones is the
        # row-1 url_hash leaf.
        common = dict(
            tenant_id="t-acme",
            product_line="cameras",
            project_id="proj-isp",
            agent_id="row8-collision-agent",
            task_id="row8-collision-task",
        )

        info_a = asyncio.run(ws_mod.provision(
            **common, remote_url=str(repo_a),
        ))
        # Pop the registry between provisions so the per-agent cleanup
        # branch in ``provision()`` doesn't unwind ``info_a`` before
        # we get a chance to verify its contents.
        ws_mod._workspaces.pop(common["agent_id"], None)

        info_b = asyncio.run(ws_mod.provision(
            **common, remote_url=str(repo_b),
        ))
        try:
            # Distinct leaves — same parent (same agent_id), different
            # url_hash leaf names.
            assert info_a.path != info_b.path
            assert info_a.path.parent == info_b.path.parent
            assert info_a.path.name == ws_mod._repo_url_hash(str(repo_a))
            assert info_b.path.name == ws_mod._repo_url_hash(str(repo_b))

            # Each leaf still holds its OWN repo's README — proves no
            # silent overwrite happened. This is the audit-bug invariant
            # that pre-Y6 was VIOLATED (two clones into same flat path
            # would clobber each other).
            assert (info_a.path / "README.md").read_text() == "A-content\n"
            assert (info_b.path / "README.md").read_text() == "B-content\n"

            # And both leaves live under the per-tenant slice, so quota
            # measurement of t-acme picks them both up.
            assert info_a.path.is_relative_to(
                ws_mod._WORKSPACES_ROOT / "t-acme",
            )
            assert info_b.path.is_relative_to(
                ws_mod._WORKSPACES_ROOT / "t-acme",
            )
        finally:
            asyncio.run(ws_mod.cleanup(common["agent_id"]))

    def test_collision_safe_leaves_attribute_to_correct_tenant_quota(
        self, isolated_roots, tmp_path,
    ):
        """End-to-end consequence: after the collision-safe provision,
        ``measure_tenant_usage`` accounting includes BOTH leaves'
        bytes. Catches a regression where the second clone would
        clobber the first AND therefore report half the actual disk
        consumption to quota — the worst case for a noisy-neighbour
        runaway clone storm."""
        repo_a = _make_repo(tmp_path / "team_a" / "foo", marker="A" * 1000)
        repo_b = _make_repo(tmp_path / "team_b" / "foo", marker="B" * 1500)

        common = dict(
            tenant_id="t-quota-acc",
            product_line="cameras",
            project_id="proj-isp",
            agent_id="row8-acc-agent",
            task_id="row8-acc-task",
        )
        info_a = asyncio.run(ws_mod.provision(
            **common, remote_url=str(repo_a),
        ))
        ws_mod._workspaces.pop(common["agent_id"], None)
        info_b = asyncio.run(ws_mod.provision(
            **common, remote_url=str(repo_b),
        ))
        try:
            usage = tq.measure_tenant_usage("t-quota-acc")
            # Both READMEs survive on disk → quota walk picks both up.
            # Lower bound: at least the two README sizes (1000 + 1500)
            # plus git plumbing.
            assert usage["workspaces_bytes"] >= 1000 + 1500
            # Pinning the actual byte count here would be brittle (git's
            # internal storage varies by version); the contract is "both
            # contributed to the total", which we verify via the lower
            # bound + by walking the dirs directly.
            a_bytes = sum(
                p.stat().st_size for p in info_a.path.rglob("*") if p.is_file()
            )
            b_bytes = sum(
                p.stat().st_size for p in info_b.path.rglob("*") if p.is_file()
            )
            assert a_bytes >= 1000
            assert b_bytes >= 1500
        finally:
            asyncio.run(ws_mod.cleanup(common["agent_id"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family B — Per-tenant quota isolation (row 1 × row 5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTenantQuotaIsolation:
    """Tenant A's workspace bytes must NOT show up in tenant B's
    measurement and must NOT push tenant B over its hard cap. This is
    the cross-tenant defence the audit row was opened to enforce —
    a runaway clone-storm in one tenant must not eat another tenant's
    headroom.
    """

    def test_tenant_a_workspace_bytes_never_attributed_to_tenant_b(
        self, isolated_roots,
    ):
        # Seed t-A with a fat workspace, t-B with a tiny one.
        _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-A",
            project_id="p-1", agent_id="agent-A1", file_size=10_000,
        )
        _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-B",
            project_id="p-1", agent_id="agent-B1", file_size=2_000,
        )

        usage_a = tq.measure_tenant_usage("t-A")
        usage_b = tq.measure_tenant_usage("t-B")

        # Per-tenant slices are byte-exact (helper writes one blob of
        # the requested size + a tiny ``.git`` text file with a known
        # short content; assert with a tight lower bound).
        assert usage_a["workspaces_bytes"] >= 10_000
        assert usage_b["workspaces_bytes"] >= 2_000
        # Critical invariant — no leakage.
        assert usage_a["workspaces_bytes"] < 10_000 + 2_000
        assert usage_b["workspaces_bytes"] < 2_000 + 10_000
        # Total carries the workspace contribution.
        assert usage_a["total_bytes"] == usage_a["workspaces_bytes"]
        assert usage_b["total_bytes"] == usage_b["workspaces_bytes"]

    def test_tenant_a_breach_does_not_breach_tenant_b(
        self, isolated_roots,
    ):
        """t-A is bigger than its hard cap; t-B has plenty of headroom.
        ``check_hard_quota`` for t-A raises, for t-B passes. Different
        tenants have different quotas — the breach must be scoped to
        the offender."""
        tq.write_quota("t-A", tq.DiskQuota(
            soft_bytes=100, hard_bytes=200, keep_recent_runs=1,
        ))
        tq.write_quota("t-B", tq.DiskQuota(
            soft_bytes=50_000, hard_bytes=100_000, keep_recent_runs=1,
        ))
        _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-A",
            project_id="p", agent_id="a", file_size=1_000,
        )
        _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-B",
            project_id="p", agent_id="b", file_size=500,
        )

        # t-A is over hard.
        with pytest.raises(tq.QuotaExceeded) as exc:
            tq.check_hard_quota("t-A")
        assert exc.value.tenant_id == "t-A"
        assert exc.value.hard == 200

        # t-B is well under.
        tq.check_hard_quota("t-B")  # must NOT raise

    def test_provision_into_tenant_b_unaffected_by_tenant_a_breach(
        self, isolated_roots, tmp_path, monkeypatch,
    ):
        """The deny-path is per-tenant: provisioning a workspace into
        tenant B must succeed even when tenant A is over hard. Catches
        a regression where the gate was accidentally widened to a
        cluster-level cap (every tenant fails when ANY tenant fails)."""
        from backend import audit as _audit

        async def _audit_noop(**kwargs):  # silence audit chain
            return None

        monkeypatch.setattr(_audit, "log", _audit_noop)

        # t-A in breach.
        tq.write_quota("t-A", tq.DiskQuota(
            soft_bytes=10, hard_bytes=100, keep_recent_runs=1,
        ))
        _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-A",
            project_id="p", agent_id="agentA", file_size=500,
        )
        # t-B has tons of headroom.
        tq.write_quota("t-B", tq.DiskQuota(
            soft_bytes=5_000_000, hard_bytes=10_000_000, keep_recent_runs=1,
        ))

        repo = _make_repo(tmp_path / "src_repo", marker="ok\n")
        info = asyncio.run(ws_mod.provision(
            agent_id="row8-tenant-b-agent", task_id="task",
            tenant_id="t-B", remote_url=str(repo),
        ))
        try:
            assert info.path.is_dir()
            # Landed under t-B's slice, not t-A's.
            assert info.path.is_relative_to(
                ws_mod._WORKSPACES_ROOT / "t-B",
            )
            assert not info.path.is_relative_to(
                ws_mod._WORKSPACES_ROOT / "t-A",
            )
        finally:
            asyncio.run(ws_mod.cleanup("row8-tenant-b-agent"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family C — Quota breach → per-project LRU eviction (row 5 × row 6)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestQuotaTriggeredLRU:
    """Spec: "遇 tenant hard quota 超標時，優先刪舊的 workspace
    （per-project LRU）而非新的". Composes row 5 (quota measurement)
    × row 6 (GC reaper) × row 1 (per-project leaf grouping). The
    matrix-level promise is that the **ordering** holds end-to-end —
    not just that the eviction happens, but that it picks the OLDEST
    member of the offending project first."""

    def test_oldest_in_project_evicted_first_newest_survives(
        self, isolated_roots, silenced_audit, monkeypatch,
    ):
        # Two workspaces in the SAME project under tenant t-LRU. One
        # is 10 days old, the other 1 day old.
        old_leaf = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-LRU",
            project_id="proj-lru", agent_id="agent-old",
            file_size=2_000, age_days=10,
        )
        new_leaf = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-LRU",
            project_id="proj-lru", agent_id="agent-new",
            file_size=2_000, age_days=1,
        )

        # Force the GC to think we're over hard. Use a stub so the test
        # is independent of the precise byte accounting (the row-5 walk
        # picks up files in unpredictable orders across hosts).
        from backend import tenant_quota as _tq
        usage_calls = {"n": 0}

        def fake_measure(tid):
            usage_calls["n"] += 1
            if tid == "t-LRU" and usage_calls["n"] == 1:
                return {
                    "total_bytes": 10_000, "artifacts_bytes": 0,
                    "workflow_runs_bytes": 0, "backups_bytes": 0,
                    "ingest_tmp_bytes": 0, "workspaces_bytes": 10_000,
                }
            return {
                "total_bytes": 100, "artifacts_bytes": 0,
                "workflow_runs_bytes": 0, "backups_bytes": 0,
                "ingest_tmp_bytes": 0, "workspaces_bytes": 100,
            }

        def fake_load(tid, plan=None):
            return _tq.DiskQuota(
                soft_bytes=500, hard_bytes=1_000, keep_recent_runs=1,
            )

        monkeypatch.setattr(_tq, "measure_tenant_usage", fake_measure)
        monkeypatch.setattr(_tq, "load_quota", fake_load)

        # ``stale_days=999`` so the stale-leaf branch doesn't also
        # trash the old leaf — we want to observe the QUOTA-driven
        # path exclusively here.
        summary = asyncio.run(gc_mod.sweep_once(stale_days=999))

        # The matrix invariant: oldest gone, newest preserved.
        assert not old_leaf.exists(), (
            "per-project LRU must evict the OLDEST workspace first"
        )
        assert new_leaf.is_dir(), (
            "the most recent workspace in the project must NOT be evicted"
        )
        assert len(summary.quota_evicted) >= 1
        record = summary.quota_evicted[0]
        assert record["tenant_id"] == "t-LRU"
        assert record["project_id"] == "proj-lru"
        assert record["agent_id"] == "agent-old"

        # And the eviction surfaces in audit + SSE so operators can
        # forensically explain a missing workspace.
        actions = [c["action"] for c in silenced_audit]
        assert "workspace.gc_quota_evicted" in actions

    def test_lru_does_not_drain_one_project_starving_others(
        self, isolated_roots, silenced_audit, monkeypatch,
    ):
        """Spec qualifier "per-project" matters when MULTIPLE projects
        are over the same tenant cap — the round-robin merge in
        ``_project_lru_workspaces`` must rotate evictions across
        projects so a single noisy project cannot monopolise the
        eviction quota and leave another project untouched. Composes
        row 1 (per-project leaf grouping) × row 6 (round-robin
        merger).
        """
        # Three OLD workspaces in project A, one OLD workspace in
        # project B. Without round-robin a naive "oldest-first across
        # tenant" would drain A entirely before touching B.
        leaf_a1 = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-rr",
            project_id="proj-A", agent_id="agent-A1",
            file_size=500, age_days=30,
        )
        leaf_a2 = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-rr",
            project_id="proj-A", agent_id="agent-A2",
            file_size=500, age_days=20,
        )
        leaf_a3 = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-rr",
            project_id="proj-A", agent_id="agent-A3",
            file_size=500, age_days=15,
        )
        leaf_b1 = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-rr",
            project_id="proj-B", agent_id="agent-B1",
            file_size=500, age_days=25,
        )

        # Stay over hard for two evictions (so we observe the
        # round-robin pattern), then drop under so the loop exits.
        from backend import tenant_quota as _tq
        usage_calls = {"n": 0}

        def fake_measure(tid):
            usage_calls["n"] += 1
            # First two probes report over-hard; subsequent probes
            # report under so we evict exactly two leaves.
            if tid == "t-rr" and usage_calls["n"] <= 2:
                return {
                    "total_bytes": 5_000, "artifacts_bytes": 0,
                    "workflow_runs_bytes": 0, "backups_bytes": 0,
                    "ingest_tmp_bytes": 0, "workspaces_bytes": 5_000,
                }
            return {
                "total_bytes": 100, "artifacts_bytes": 0,
                "workflow_runs_bytes": 0, "backups_bytes": 0,
                "ingest_tmp_bytes": 0, "workspaces_bytes": 100,
            }

        def fake_load(tid, plan=None):
            return _tq.DiskQuota(
                soft_bytes=500, hard_bytes=1_000, keep_recent_runs=1,
            )

        monkeypatch.setattr(_tq, "measure_tenant_usage", fake_measure)
        monkeypatch.setattr(_tq, "load_quota", fake_load)

        summary = asyncio.run(gc_mod.sweep_once(stale_days=999))

        # Both proj-A's oldest AND proj-B's only leaf must have been
        # touched — proving the round-robin merge alternates rather
        # than draining one project. Without round-robin: leaf_a1 +
        # leaf_a2 evicted, leaf_b1 untouched (the failure mode).
        evicted_projects = {r["project_id"] for r in summary.quota_evicted}
        assert "proj-A" in evicted_projects
        assert "proj-B" in evicted_projects, (
            "per-project round-robin must touch proj-B even when "
            "proj-A has older candidates"
        )

        # The proj-A leaf evicted is the OLDEST one (age_days=30).
        proj_a_evictions = [
            r for r in summary.quota_evicted if r["project_id"] == "proj-A"
        ]
        assert proj_a_evictions[0]["agent_id"] == "agent-A1"
        # And proj-B's evicted leaf is the only one it had.
        proj_b_evictions = [
            r for r in summary.quota_evicted if r["project_id"] == "proj-B"
        ]
        assert proj_b_evictions[0]["agent_id"] == "agent-B1"

        # The 2nd-oldest and 3rd-oldest in proj-A survive — the loop
        # exited as soon as the stub reported under-hard. proves the
        # gate is "stop once we drop under", not "evict everything
        # marked stale".
        assert leaf_a2.is_dir()
        assert leaf_a3.is_dir()
        # And exactly 2 evictions happened (one per usage>hard probe).
        assert len(summary.quota_evicted) == 2
        # Sanity — the actual oldest leaf names fit the eviction shape.
        del leaf_a1, leaf_b1  # both gone; existence already asserted


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family D — Active-agent guard (row 3 × row 6)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestActiveAgentGuard:
    """A workspace listed in the in-process ``_workspaces`` registry
    must survive every GC sweep — stale or not, quota-pressure or not.
    The GC is pure recovery code; it must defer to the registry, never
    yank files from under a running agent."""

    def test_active_workspace_survives_stale_sweep(
        self, isolated_roots, silenced_audit,
    ):
        """A leaf old enough to be a stale-sweep candidate, but whose
        owning agent is still in the registry, must NOT be touched."""
        leaf = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-active",
            project_id="proj", agent_id="agent-running",
            file_size=128, age_days=60,
        )
        # Register the agent as active.
        ws_mod._workspaces["agent-running"] = ws_mod.WorkspaceInfo(
            agent_id="agent-running", task_id="task-1",
            branch="agent/agent-running/task-1", path=leaf,
            repo_source="(test)",
        )

        summary = asyncio.run(gc_mod.sweep_once(stale_days=30))

        assert leaf.is_dir(), (
            "active agent's workspace must NOT be reaped by stale sweep"
        )
        assert summary.trashed == []
        # Telemetry surfaces the skip reason so operators can
        # confirm the guard fired.
        assert any("registry" in s for s in summary.skipped_busy)

    def test_active_workspace_survives_quota_pressure(
        self, isolated_roots, silenced_audit, monkeypatch,
    ):
        """Even under hard-quota pressure the active workspace stays
        — the quota-evict branch shares the same ``_is_workspace_busy``
        gate as the stale branch so an active leaf is not evictable
        even when the tenant is in the red."""
        # One active leaf + one inactive leaf, both old. Quota path
        # should evict only the inactive one.
        active_leaf = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-pressure",
            project_id="proj", agent_id="agent-active",
            file_size=1_000, age_days=20,
        )
        inactive_leaf = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-pressure",
            project_id="proj", agent_id="agent-inactive",
            file_size=1_000, age_days=15,
        )
        ws_mod._workspaces["agent-active"] = ws_mod.WorkspaceInfo(
            agent_id="agent-active", task_id="t",
            branch="agent/agent-active/t", path=active_leaf,
            repo_source="(test)",
        )

        from backend import tenant_quota as _tq
        usage_calls = {"n": 0}

        def fake_measure(tid):
            usage_calls["n"] += 1
            if tid == "t-pressure" and usage_calls["n"] == 1:
                return {
                    "total_bytes": 5_000, "artifacts_bytes": 0,
                    "workflow_runs_bytes": 0, "backups_bytes": 0,
                    "ingest_tmp_bytes": 0, "workspaces_bytes": 5_000,
                }
            return {
                "total_bytes": 100, "artifacts_bytes": 0,
                "workflow_runs_bytes": 0, "backups_bytes": 0,
                "ingest_tmp_bytes": 0, "workspaces_bytes": 100,
            }

        monkeypatch.setattr(_tq, "measure_tenant_usage", fake_measure)
        monkeypatch.setattr(
            _tq, "load_quota",
            lambda tid, plan=None: _tq.DiskQuota(
                soft_bytes=500, hard_bytes=1_000, keep_recent_runs=1,
            ),
        )

        summary = asyncio.run(gc_mod.sweep_once(stale_days=999))

        # Active leaf survives quota pressure.
        assert active_leaf.is_dir()
        # Inactive leaf evicted.
        assert not inactive_leaf.exists()
        evicted_agents = {r["agent_id"] for r in summary.quota_evicted}
        assert "agent-inactive" in evicted_agents
        assert "agent-active" not in evicted_agents
        # And the active path is recorded as skipped/busy.
        assert any("registry" in s for s in summary.skipped_busy)

    def test_active_workspace_survives_fresh_lock_path(
        self, isolated_roots, silenced_audit,
    ):
        """Even without a registry entry, a fresh ``.git/index.lock``
        protects the leaf — the GC defers to ANY in-flight git op
        regardless of provenance (CLI, chatops, FastAPI request).
        Composes row 6 stale-lock-window guard."""
        leaf = _seed_leaf(
            isolated_roots / "workspaces", tenant_id="t-lock-guard",
            project_id="proj", agent_id="agent-lock",
            file_size=128, age_days=60,
        )
        # Replace the placeholder file with a directory so we can drop
        # an index.lock inside it (mirroring the on-disk shape git
        # produces during a real op).
        git_path = leaf / ".git"
        git_path.unlink()
        git_path.mkdir()
        lock = git_path / "index.lock"
        lock.write_text("")
        # Lock mtime = now → fresh by 60s rule.
        os.utime(lock, (time.time(), time.time()))
        # Back-date the leaf so the stale-cutoff would otherwise fire.
        old = time.time() - 90 * 86400
        os.utime(leaf, (old, old))

        summary = asyncio.run(gc_mod.sweep_once(stale_days=30))

        assert leaf.is_dir(), (
            "fresh index.lock must shield the leaf even when no "
            "registry entry covers it (CLI/chatops protection path)"
        )
        assert summary.trashed == []
        assert any("index.lock" in s for s in summary.skipped_busy)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family E — OMNISIGHT_WORKSPACE_ROOT env knob effective end-to-end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_migrator():
    """Load ``scripts/migrate_workspace_hierarchy.py`` as an importable
    module so we can call its ``_default_target_root`` directly. The
    script is not part of any package."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "migrate_workspace_hierarchy.py"
    spec = importlib.util.spec_from_file_location(
        "migrate_workspace_hierarchy_row8", script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestWorkspaceRootEnvKnob:
    """Spec wording: "env ``OMNISIGHT_WORKSPACE_ROOT`` 有效".

    "有效" means the env knob actually re-targets the on-disk layout
    end-to-end, not just that the field exists on the Settings dataclass.
    Three matrix-level facts pin "effective":
      1. ``Settings()`` reflects the env value
      2. The migrator picks up the same value as its default target root
      3. The runtime quota measurement walks the live
         ``_WORKSPACES_ROOT`` so a runtime rebase of that constant
         flows into ``measure_tenant_usage`` (the bridging seam row 5
         depends on)
    """

    def test_settings_workspace_root_picks_up_env_var(self, monkeypatch):
        """Composition gate: Settings reads the env. Already covered
        directly in test_config.py — we re-pin here so the row-8
        composition matrix catches a regression that decoupled the
        env knob from the runtime path even when the per-row test
        still passes for unrelated reasons."""
        monkeypatch.setenv(
            "OMNISIGHT_WORKSPACE_ROOT", "/srv/row8/workspaces",
        )
        from backend.config import Settings
        s = Settings()
        assert s.workspace_root == "/srv/row8/workspaces"

    def test_settings_workspace_root_falls_back_when_env_unset(
        self, monkeypatch,
    ):
        """The default flowing through Settings — operators who do not
        set the env still get the row-2 documented default
        ``./data/workspaces``, NOT the legacy ``./.agent_workspaces``
        path."""
        monkeypatch.delenv("OMNISIGHT_WORKSPACE_ROOT", raising=False)
        from backend.config import Settings
        s = Settings()
        assert s.workspace_root == "./data/workspaces"

    def test_migrator_default_target_root_honours_env(
        self, monkeypatch, tmp_path,
    ):
        """Row 4's migrator script must default to whatever
        ``Settings().workspace_root`` resolves to — operators setting
        the env knob get the same target root from the migrator
        without needing ``--target``. This is what ties "the env knob
        works" to "the migration tool actually moves files into the
        right place" without operator vigilance."""
        custom = str(tmp_path / "custom-workspaces-root")
        monkeypatch.setenv("OMNISIGHT_WORKSPACE_ROOT", custom)
        # Settings is class-level — re-instantiating picks up the env.
        from backend import config as _cfg
        # The migrator reads ``settings.workspace_root`` at call time,
        # so monkeypatch the live singleton too.
        original = _cfg.settings.workspace_root
        try:
            _cfg.settings.workspace_root = custom
            migrator = _load_migrator()
            target = migrator._default_target_root()
            # Absolute-path semantics: env value already absolute → path
            # passes through unchanged.
            assert target == Path(custom)
        finally:
            _cfg.settings.workspace_root = original

    def test_migrator_relative_path_resolves_against_repo_root(
        self, monkeypatch,
    ):
        """The migrator's docstring says relative ``workspace_root``
        is resolved against the project root. Composition test pins
        the rule so a regression that broke relative-path semantics
        (e.g. cwd-relative instead of repo-relative) fails here."""
        from backend import config as _cfg
        original = _cfg.settings.workspace_root
        try:
            _cfg.settings.workspace_root = "./data/row8-relative"
            migrator = _load_migrator()
            target = migrator._default_target_root()
            assert target.is_absolute()
            # Anchored at the repo root, not the test cwd.
            repo_root = Path(__file__).resolve().parents[2]
            assert target == repo_root / "data" / "row8-relative"
        finally:
            _cfg.settings.workspace_root = original

    def test_runtime_workspace_rebase_flows_into_quota_measurement(
        self, monkeypatch, tmp_path,
    ):
        """The runtime bridging seam: ``tenant_quota._tenant_workspaces_root``
        reads the live ``backend.workspace._WORKSPACES_ROOT`` (NOT a
        cached snapshot), so when an operator points the constant at
        a new directory (today via test monkeypatch, tomorrow via a
        bootstrap-time read of the env knob) the quota measurement
        follows on the next call. Without this property the env knob
        would be "settings-effective" but "quota-blind" — the worst
        kind of half-wired."""
        new_root = tmp_path / "rebased-workspaces"
        new_root.mkdir()
        monkeypatch.setattr(
            ws_mod, "_WORKSPACES_ROOT", new_root, raising=True,
        )
        # Also redirect tenants/ingest so the quota walk doesn't fall
        # over on the dev host's real ./data/tenants tree.
        tenants_dir = tmp_path / "tenants"
        tenants_dir.mkdir()
        monkeypatch.setattr(tfs, "_TENANTS_ROOT", tenants_dir)
        ingest_dir = tmp_path / "ingest"
        ingest_dir.mkdir()
        monkeypatch.setattr(tfs, "_INGEST_BASE", ingest_dir)
        tq._reset_for_tests()

        # Seed a workspace blob under the REBASED root only.
        leaf = (
            new_root / "t-rebase" / "default" / "default"
            / "agent" / "self"
        )
        leaf.mkdir(parents=True)
        (leaf / "blob.bin").write_bytes(b"\0" * 4_096)

        # Quota measurement must see the rebased file (not the
        # default ``.agent_workspaces`` tree on the dev host).
        usage = tq.measure_tenant_usage("t-rebase")
        assert usage["workspaces_bytes"] == 4_096
        assert usage["total_bytes"] == 4_096


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family F — Cross-cutting composition gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCrossRowComposition:
    """One end-to-end scenario that walks the full epic: provision
    two collision-prone repos, observe both contribute to per-tenant
    quota, then trigger a quota-driven GC sweep and confirm the
    eviction order respects per-project LRU + active-agent guard.
    Catches subtle regressions where individual rows still pass
    in isolation but their interaction breaks."""

    def test_full_epic_walkthrough(
        self, isolated_roots, tmp_path, silenced_audit, monkeypatch,
    ):
        # Step 1 — provision two same-name repos under same agent
        # (row 1 + row 7 collision-safety contract).
        repo_a = _make_repo(tmp_path / "x" / "foo", marker="A" * 4_000)
        repo_b = _make_repo(tmp_path / "y" / "foo", marker="B" * 4_000)
        common = dict(
            tenant_id="t-walk",
            product_line="cameras",
            project_id="proj-walk",
            agent_id="row8-walk-agent",
            task_id="row8-walk-task",
        )
        info_a = asyncio.run(ws_mod.provision(
            **common, remote_url=str(repo_a),
        ))
        ws_mod._workspaces.pop(common["agent_id"], None)
        info_b = asyncio.run(ws_mod.provision(
            **common, remote_url=str(repo_b),
        ))
        # info_b is the registered (active) workspace for the agent.

        # Sanity — distinct leaves, both populated, both attribute to
        # t-walk.
        assert info_a.path != info_b.path
        assert info_a.path.parent == info_b.path.parent
        assert (info_a.path / "README.md").read_text().startswith("A")
        assert (info_b.path / "README.md").read_text().startswith("B")

        # Step 2 — quota measurement sees both leaves' bytes (row 5).
        usage_before = tq.measure_tenant_usage("t-walk")
        assert usage_before["workspaces_bytes"] >= 8_000

        # Back-date the inactive leaf (info_a) so the LRU evict picks
        # it before the active one (info_b — registered for the
        # agent_id).
        old = time.time() - 60 * 86400
        for p in info_a.path.rglob("*"):
            try:
                os.utime(p, (old, old))
            except OSError:
                pass
        os.utime(info_a.path, (old, old))

        # Step 3 — synthesise a quota breach to force the eviction
        # path. Stub measure/load to avoid the real walk reporting
        # different totals depending on git plumbing (the test cares
        # about ORDERING + GUARDS, not exact byte accounting).
        from backend import tenant_quota as _tq
        usage_calls = {"n": 0}

        def fake_measure(tid):
            usage_calls["n"] += 1
            if tid == "t-walk" and usage_calls["n"] == 1:
                return {
                    "total_bytes": 50_000, "artifacts_bytes": 0,
                    "workflow_runs_bytes": 0, "backups_bytes": 0,
                    "ingest_tmp_bytes": 0, "workspaces_bytes": 50_000,
                }
            return {
                "total_bytes": 100, "artifacts_bytes": 0,
                "workflow_runs_bytes": 0, "backups_bytes": 0,
                "ingest_tmp_bytes": 0, "workspaces_bytes": 100,
            }

        monkeypatch.setattr(_tq, "measure_tenant_usage", fake_measure)
        monkeypatch.setattr(
            _tq, "load_quota",
            lambda tid, plan=None: _tq.DiskQuota(
                soft_bytes=5_000, hard_bytes=10_000, keep_recent_runs=1,
            ),
        )

        try:
            summary = asyncio.run(gc_mod.sweep_once(stale_days=999))

            # Step 4 — verify cross-row invariants:
            # (a) info_b (active in registry, newest) survives.
            assert info_b.path.is_dir()
            # (b) info_a (older + NOT in registry) was evicted.
            assert not info_a.path.exists()
            # (c) The eviction was attributed to the correct project.
            assert any(
                r["project_id"] == "proj-walk"
                and r["agent_id"] == common["agent_id"]
                for r in summary.quota_evicted
            )
            # (d) Audit row landed for forensics.
            actions = [c["action"] for c in silenced_audit]
            assert "workspace.gc_quota_evicted" in actions
        finally:
            asyncio.run(ws_mod.cleanup(common["agent_id"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit route presence guards (analogous to Y4 row 8)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_y6_public_surface_intact():
    """A row-1-7 regression that dropped one of the public symbols
    this matrix relies on would silently turn the composition tests
    into "module not found" / AttributeError noise. Pin the surface
    here so a refactor that renames or hides one of these breaks
    THIS test cleanly with a useful message."""
    # Row 1 — path resolver + url-hash helper.
    assert hasattr(ws_mod, "_workspace_path_for")
    assert hasattr(ws_mod, "_repo_url_hash")
    assert hasattr(ws_mod, "_WORKSPACES_ROOT")
    assert hasattr(ws_mod, "_DEFAULT_TENANT_ID")
    assert hasattr(ws_mod, "_DEFAULT_PRODUCT_LINE")
    assert hasattr(ws_mod, "_DEFAULT_PROJECT_ID")
    assert hasattr(ws_mod, "_SELF_REPO_HASH")
    # Row 3 — provision signature accepts the five-context kwargs.
    import inspect
    sig = inspect.signature(ws_mod.provision)
    for kw in (
        "agent_id", "task_id", "remote_url",
        "tenant_id", "product_line", "project_id",
    ):
        assert kw in sig.parameters, (
            f"provision() missing the row-3 kwarg {kw!r}"
        )
    # Row 5 — quota measurement exposes workspaces_bytes.
    assert "workspaces_bytes" in tq.measure_tenant_usage("t-probe-no-such")
    # Row 6 — GC reaper public surface.
    assert hasattr(gc_mod, "sweep_once")
    assert hasattr(gc_mod, "run_gc_loop")
    assert hasattr(gc_mod, "GCSummary")
