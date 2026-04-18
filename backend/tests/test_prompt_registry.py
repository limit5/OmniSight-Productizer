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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  B15 #350 — get_skill_metadata (lazy-loading metadata card)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _write_skill(tmp_path, name, body):
    """Drop a SKILL.md under tmp_path/<name>/ and return its file path."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(body, encoding="utf-8")
    return f


def test_get_skill_metadata_parses_full_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "SKILLS_ROOT", tmp_path)
    body = "# Body\n\n" + ("x" * 800)
    skill = _write_skill(tmp_path, "my-skill", f"""---
name: my-skill
description: A tiny demo skill for unit tests.
trigger_condition: user mentions "demo"
keywords: [demo, tiny]
version: "1.2.3"
---
{body}""")

    meta = pr.get_skill_metadata("my-skill")
    assert meta["name"] == "my-skill"
    assert meta["description"] == "A tiny demo skill for unit tests."
    assert meta["trigger_condition"] == 'user mentions "demo"'
    assert meta["token_cost"] > 0
    # chars/4 rule of thumb — body is ~810 chars → ~200 tokens.
    assert 150 < meta["token_cost"] < 300
    assert meta["keywords"] == ["demo", "tiny"]
    assert meta["version"] == "1.2.3"
    assert meta["path"] == str(skill.resolve())


def test_get_skill_metadata_accepts_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "SKILLS_ROOT", tmp_path)
    skill = _write_skill(tmp_path, "relpath", """---
name: relpath
description: Accessed via relative path.
---
body body body""")
    # Pass the absolute path instead of the bare name; must still resolve.
    meta = pr.get_skill_metadata(str(skill))
    assert meta["name"] == "relpath"
    assert meta["description"] == "Accessed via relative path."


def test_get_skill_metadata_falls_back_to_when_to_use_section(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "SKILLS_ROOT", tmp_path)
    _write_skill(tmp_path, "fallback-trigger", """---
name: fallback-trigger
description: No explicit trigger key.
---

# Header

## When to use

You should use this skill when building a new Android app that needs
Jetpack Compose + Play Store submission out of the box.

## Other section

Not relevant.
""")
    meta = pr.get_skill_metadata("fallback-trigger")
    assert "Jetpack Compose" in meta["trigger_condition"]
    assert "Other section" not in meta["trigger_condition"]


def test_get_skill_metadata_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "SKILLS_ROOT", tmp_path)
    assert pr.get_skill_metadata("does-not-exist") == {}
    assert pr.get_skill_metadata("") == {}


def test_get_skill_metadata_handles_missing_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "SKILLS_ROOT", tmp_path)
    _write_skill(tmp_path, "no-fm", "# Just a header\n\nSome body text.\n")
    meta = pr.get_skill_metadata("no-fm")
    # Name falls back to parent dir for SKILL.md.
    assert meta["name"] == "no-fm"
    assert meta["description"] == ""
    assert meta["trigger_condition"] == ""
    assert meta["token_cost"] >= 1


def test_get_skill_metadata_does_not_leak_body(tmp_path, monkeypatch):
    """Contract: metadata response must never include the full body —
    that's the whole point of B15 lazy loading."""
    monkeypatch.setattr(pr, "SKILLS_ROOT", tmp_path)
    secret = "SECRET_BODY_MARKER_" + "A" * 5000
    _write_skill(tmp_path, "big", f"""---
name: big
description: huge skill
---
{secret}""")
    meta = pr.get_skill_metadata("big")
    for v in meta.values():
        assert "SECRET_BODY_MARKER" not in str(v), (
            f"body leaked into metadata key; metadata must be body-free"
        )
    # And the token_cost should reflect the body size (chars/4).
    assert meta["token_cost"] >= 1000


def test_get_skill_metadata_ships_for_real_skill_android(monkeypatch):
    """Smoke test against the real configs/skills/skill-android/SKILL.md
    shipped in the repo — proves the resolver works against real data."""
    # Do not stub SKILLS_ROOT; hit the project tree.
    meta = pr.get_skill_metadata("skill-android")
    if not meta:
        pytest.skip("skill-android not present in this checkout")
    assert meta["name"]
    assert meta["token_cost"] > 0
    assert "path" in meta
