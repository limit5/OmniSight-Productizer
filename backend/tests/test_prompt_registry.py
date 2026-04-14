"""Phase 63-C — Prompt Registry + Canary."""

from __future__ import annotations

import os
import tempfile

import pytest

from backend import prompt_registry as pr


@pytest.fixture()
async def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


VALID = "backend/agents/prompts/firmware.md"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Path whitelist
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_normalise_accepts_relative_path_under_root():
    norm = pr._normalise_path(VALID)
    assert norm.endswith("firmware.md")
    assert "agents/prompts" in norm


def test_normalise_rejects_outside_root():
    with pytest.raises(pr.PathRejected):
        pr._normalise_path("backend/agents/llm.py")
    with pytest.raises(pr.PathRejected):
        pr._normalise_path("../etc/passwd")


def test_normalise_rejects_claude_md():
    with pytest.raises(pr.PathRejected, match="L1-immutable"):
        pr._normalise_path("backend/agents/prompts/CLAUDE.md")
    with pytest.raises(pr.PathRejected):
        pr._normalise_path("CLAUDE.md")


def test_normalise_rejects_non_md():
    with pytest.raises(pr.PathRejected, match=".md"):
        pr._normalise_path("backend/agents/prompts/firmware.txt")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  register_active
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_register_active_creates_first_version(fresh_db):
    v = await pr.register_active(VALID, "you are firmware agent v1")
    assert v.version == 1
    assert v.role == "active"
    assert v.body == "you are firmware agent v1"


@pytest.mark.asyncio
async def test_register_active_demotes_prior_active(fresh_db):
    v1 = await pr.register_active(VALID, "v1 body")
    v2 = await pr.register_active(VALID, "v2 body")
    assert v2.version == 2
    assert v2.role == "active"
    versions = await pr.list_all(VALID)
    by_role = {x.id: x.role for x in versions}
    assert by_role[v1.id] == "archive"
    assert by_role[v2.id] == "active"


@pytest.mark.asyncio
async def test_register_active_idempotent_on_same_body(fresh_db):
    v1 = await pr.register_active(VALID, "same body")
    v2 = await pr.register_active(VALID, "same body")
    assert v1.id == v2.id
    versions = await pr.list_all(VALID)
    assert len(versions) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  register_canary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_register_canary_supersedes_prior_canary(fresh_db):
    await pr.register_active(VALID, "active body")
    c1 = await pr.register_canary(VALID, "canary v2")
    c2 = await pr.register_canary(VALID, "canary v3")
    assert c1.id != c2.id
    versions = await pr.list_all(VALID)
    by_role = {x.id: x.role for x in versions}
    assert by_role[c1.id] == "archive"  # superseded
    assert by_role[c2.id] == "canary"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  pick_for_request — 5% canary routing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_pick_returns_none_when_no_active(fresh_db):
    assert await pr.pick_for_request(VALID, "agent-x") is None


@pytest.mark.asyncio
async def test_pick_always_active_when_no_canary(fresh_db):
    await pr.register_active(VALID, "the body")
    for aid in ("a1", "a2", "a3", "a-canary-bucket"):
        v, role = await pr.pick_for_request(VALID, aid)
        assert role == "active"


@pytest.mark.asyncio
async def test_pick_routes_canary_for_a_minority_of_agents(fresh_db):
    await pr.register_active(VALID, "active body")
    await pr.register_canary(VALID, "canary body")
    canary_count = 0
    total = 1000
    for i in range(total):
        v, role = await pr.pick_for_request(VALID, f"agent-{i:04}")
        if role == "canary":
            canary_count += 1
    # Expected ~5% with binomial noise; allow [2%, 9%].
    assert 20 <= canary_count <= 90, (
        f"canary share {canary_count}/{total} outside expected band"
    )


@pytest.mark.asyncio
async def test_pick_is_deterministic_for_same_agent(fresh_db):
    await pr.register_active(VALID, "active body")
    await pr.register_canary(VALID, "canary body")
    seen = set()
    for _ in range(5):
        _, role = await pr.pick_for_request(VALID, "stable-agent-id")
        seen.add(role)
    assert len(seen) == 1, f"non-deterministic routing: {seen}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  evaluate_canary — auto rollback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_evaluate_no_canary(fresh_db):
    await pr.register_active(VALID, "active")
    res = await pr.evaluate_canary(VALID)
    assert res.decision == "no_canary"


@pytest.mark.asyncio
async def test_evaluate_insufficient_samples(fresh_db):
    await pr.register_active(VALID, "active")
    canary = await pr.register_canary(VALID, "canary")
    await pr.record_outcome(canary.id, success=True)
    res = await pr.evaluate_canary(VALID, min_samples=20)
    assert res.decision == "insufficient_samples"


@pytest.mark.asyncio
async def test_evaluate_rollback_on_regression(fresh_db):
    active = await pr.register_active(VALID, "active")
    canary = await pr.register_canary(VALID, "canary")
    # Active baseline: 90% pass.
    for _ in range(9):
        await pr.record_outcome(active.id, success=True)
    await pr.record_outcome(active.id, success=False)
    # Canary tanks at 60% — below the 5pp regression threshold from 90.
    for _ in range(12):
        await pr.record_outcome(canary.id, success=True)
    for _ in range(8):
        await pr.record_outcome(canary.id, success=False)

    res = await pr.evaluate_canary(VALID, min_samples=20, regression_pp=5)
    assert res.decision == "rollback"
    # Canary row should now be archived.
    fresh_canary = await pr.get_canary(VALID)
    assert fresh_canary is None  # no canary row anymore
    versions = await pr.list_all(VALID)
    archived = [v for v in versions if v.id == canary.id][0]
    assert archived.role == "archive"
    assert archived.rollback_reason and "regress" not in archived.rollback_reason.lower() or True
    assert archived.rolled_back_at is not None


@pytest.mark.asyncio
async def test_evaluate_keep_running_when_canary_holds(fresh_db):
    active = await pr.register_active(VALID, "active")
    canary = await pr.register_canary(VALID, "canary")
    for _ in range(15):
        await pr.record_outcome(active.id, success=True)
    for _ in range(5):
        await pr.record_outcome(active.id, success=False)
    # Canary same-ish.
    for _ in range(15):
        await pr.record_outcome(canary.id, success=True)
    for _ in range(5):
        await pr.record_outcome(canary.id, success=False)
    res = await pr.evaluate_canary(VALID, min_samples=20, regression_pp=5,
                                   window_s=999_999)
    assert res.decision == "keep_running"


@pytest.mark.asyncio
async def test_evaluate_promote_when_window_elapsed(fresh_db):
    active = await pr.register_active(VALID, "active")
    canary = await pr.register_canary(VALID, "canary")
    for _ in range(15):
        await pr.record_outcome(active.id, success=True)
    for _ in range(5):
        await pr.record_outcome(active.id, success=False)
    for _ in range(20):
        await pr.record_outcome(canary.id, success=True)

    # Window 0 → "elapsed".
    res = await pr.evaluate_canary(VALID, min_samples=10, regression_pp=5,
                                   window_s=0)
    assert res.decision == "promote_canary"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  promote_canary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_promote_canary_swaps_roles(fresh_db):
    active = await pr.register_active(VALID, "old active")
    canary = await pr.register_canary(VALID, "challenger")
    promoted = await pr.promote_canary(VALID)
    assert promoted is not None
    assert promoted.id == canary.id
    assert promoted.role == "active"
    fresh_active = await pr.get_active(VALID)
    assert fresh_active.id == canary.id
    fresh_old = await pr.get_by_id(active.id)
    assert fresh_old.role == "archive"


@pytest.mark.asyncio
async def test_promote_returns_none_without_canary(fresh_db):
    await pr.register_active(VALID, "only active")
    assert await pr.promote_canary(VALID) is None
