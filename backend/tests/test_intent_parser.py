"""Phase 68-A — Intent Parser tests.

Covers:
  * ParsedSpec helpers: low_confidence, needs_clarification, to_dict
  * Heuristic parser: framework / arch / persistence / runtime extraction
    on English and CJK prompts
  * `static_with_runtime_db` conflict: fires when SSG + runtime DB hint
    co-occur; doesn't fire on pure SSG, pure SSR, or unrelated prompts
  * LLM-backed parser: happy path + fenced JSON + malformed response
    + fallback to heuristic when ask_fn returns empty
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend import intent_parser as ip


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ParsedSpec helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_low_confidence_lists_fields_below_threshold():
    ps = ip.ParsedSpec()
    # Every field except target_os (default 0.3) and hardware_required
    # starts at confidence 0, so low_confidence reports them all.
    low = ps.low_confidence(threshold=0.7)
    assert "project_type" in low
    assert "framework" in low
    assert "target_os" in low


def test_needs_clarification_when_conflict_present():
    ps = ip.ParsedSpec(
        project_type=ip.Field("web_app", 0.9),
        project_class=ip.Field("enterprise_web", 0.9),
        runtime_model=ip.Field("ssg", 0.9),
        target_arch=ip.Field("x86_64", 0.9),
        target_os=ip.Field("linux", 0.9),
        framework=ip.Field("nextjs", 0.9),
        persistence=ip.Field("sqlite", 0.9),
        deploy_target=ip.Field("local", 0.9),
    )
    # All confidences high → not flagged purely on confidence.
    assert not ps.needs_clarification(threshold=0.7)
    ps.conflicts.append(ip.SpecConflict(
        id="x", message="y", fields=("runtime_model",), options=(),
    ))
    assert ps.needs_clarification(threshold=0.7)


def test_to_dict_shape_is_json_safe():
    ps = ip.ParsedSpec(project_type=ip.Field("web_app", 0.8))
    d = ps.to_dict()
    # Round-trip through JSON to catch any non-serialisable leaks.
    json.dumps(d)
    assert d["project_type"] == {"value": "web_app", "confidence": 0.8}
    assert isinstance(d["conflicts"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Heuristic parser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_heuristic_parses_cjk_motivating_example():
    """The design-review example: x86_64 embedded Next.js static site
    reading from local DB. Heuristic must extract all four signal
    fields even though half the sentence is Chinese."""
    p = await ip.parse_intent(
        "這個專案要在x86_64的嵌入式系統中，架設一套網頁伺服器全端，"
        "並使用Next.js開發，目的是做從本地端資料庫拉取資料的靜態網頁展示。"
    )
    assert p.framework.value == "nextjs"
    assert p.framework.confidence >= 0.5
    assert p.target_arch.value == "x86_64"
    assert p.runtime_model.value == "ssg"
    assert p.project_type.value == "web_app"


@pytest.mark.asyncio
async def test_heuristic_parses_english_ssg():
    p = await ip.parse_intent(
        "Build a Next.js SSG site that pulls from a local SQLite at build time."
    )
    assert p.framework.value == "nextjs"
    assert p.runtime_model.value == "ssg"
    assert p.persistence.value == "sqlite"


@pytest.mark.asyncio
async def test_empty_input_returns_all_unknown():
    p = await ip.parse_intent("")
    assert p.framework.value == "unknown"
    assert p.framework.confidence == 0.0
    assert p.conflicts == []
    # needs_clarification == True (every required field is 0 confidence)
    assert p.needs_clarification()


@pytest.mark.asyncio
async def test_embedded_firmware_inferred_from_keywords():
    p = await ip.parse_intent(
        "Write an RTOS driver for the IMX335 sensor over MIPI CSI."
    )
    assert p.framework.value == "embedded"
    assert p.project_type.value == "embedded_firmware"
    assert p.hardware_required.value == "yes"


@pytest.mark.asyncio
async def test_regex_respects_word_boundaries():
    """`rust` keyword must not match inside another word like 'trusted'."""
    p = await ip.parse_intent("We need a trusted network policy review.")
    assert p.framework.value == "unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Conflict detector — smoke (68-B replaces with YAML)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_static_with_runtime_db_conflict_fires():
    p = await ip.parse_intent(
        "Build a static Next.js site that reads from a local SQLite at request time."
    )
    ids = [c.id for c in p.conflicts]
    assert "static_with_runtime_db" in ids
    # The conflict must ship at least 2 options so Decision-Engine
    # can render them as radio choices.
    c = next(c for c in p.conflicts if c.id == "static_with_runtime_db")
    assert len(c.options) >= 2
    assert all(opt.get("id") and opt.get("label") for opt in c.options)


@pytest.mark.asyncio
async def test_pure_ssg_with_build_time_db_no_conflict():
    """Build-time DB read is the whole point of SSG; must NOT fire
    the conflict (false-positive regression guard)."""
    p = await ip.parse_intent(
        "Next.js SSG site, queries the DB once at `next build`, deploys only `out/`."
    )
    assert not any(c.id == "static_with_runtime_db" for c in p.conflicts)


@pytest.mark.asyncio
async def test_pure_ssr_no_ssg_conflict():
    p = await ip.parse_intent(
        "SSR Next.js app backed by local SQLite, query at request time."
    )
    assert not any(c.id == "static_with_runtime_db" for c in p.conflicts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM-backed parse
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _good_llm_response(**overrides: Any) -> str:
    """Shape the LLM is contracted to emit. Default values satisfy every
    field so individual tests only need to change what they're testing."""
    base = {
        "project_type":      {"value": "web_app",   "confidence": 0.9},
        "runtime_model":     {"value": "ssg",       "confidence": 0.9},
        "target_arch":       {"value": "x86_64",    "confidence": 0.9},
        "target_os":         {"value": "linux",     "confidence": 0.9},
        "framework":         {"value": "nextjs",    "confidence": 0.9},
        "persistence":       {"value": "sqlite",    "confidence": 0.9},
        "deploy_target":     {"value": "local",     "confidence": 0.9},
        "hardware_required": {"value": "no",        "confidence": 0.9},
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.asyncio
async def test_llm_parse_happy_path():
    async def ask_fn(model, prompt):
        return _good_llm_response(), 50
    p = await ip.parse_intent(
        "anything", ask_fn=ask_fn, model="anthropic/claude-test",
    )
    # LLM path returned high confidence; no clarification unless
    # a conflict also fires.
    assert p.framework.value == "nextjs"
    assert p.framework.confidence == 0.9
    assert p.project_type.value == "web_app"


@pytest.mark.asyncio
async def test_llm_parse_tolerates_fenced_json():
    """LLMs sometimes wrap JSON in ```json ... ``` markdown fences."""
    async def ask_fn(model, prompt):
        return f"```json\n{_good_llm_response()}\n```", 50
    p = await ip.parse_intent("x", ask_fn=ask_fn, model="test")
    assert p.framework.value == "nextjs"


@pytest.mark.asyncio
async def test_llm_parse_falls_back_on_malformed_response():
    async def ask_fn(model, prompt):
        return "sorry I'm not going to answer that", 10
    p = await ip.parse_intent(
        "Build a Next.js app", ask_fn=ask_fn, model="test",
    )
    # Heuristic kicks in — nextjs still extracted.
    assert p.framework.value == "nextjs"


@pytest.mark.asyncio
async def test_llm_parse_falls_back_on_empty_response():
    """ask_fn returning empty string is the documented 'no LLM available'
    signal (see iq_runner.live_ask_fn). Must not raise; must degrade."""
    async def ask_fn(model, prompt):
        return "", 0
    p = await ip.parse_intent(
        "Django REST API over PostgreSQL", ask_fn=ask_fn, model="test",
    )
    assert p.framework.value == "django"
    assert p.persistence.value == "postgres"


@pytest.mark.asyncio
async def test_llm_parse_clamps_confidence_to_unit_range():
    """An LLM returning confidence=2.5 or -1 must get clamped, not
    silently stored. Defends against prompt injection that would try
    to force high confidence on garbage."""
    async def ask_fn(model, prompt):
        return json.dumps({
            "framework": {"value": "nextjs", "confidence": 2.5},
        }), 10
    p = await ip.parse_intent("x", ask_fn=ask_fn, model="test")
    assert 0.0 <= p.framework.confidence <= 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 68-B — YAML conflict library + iterative clarification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_yaml_loads_and_contains_motivating_rule():
    rules = ip._load_conflicts_yaml()
    assert any(r.get("id") == "static_with_runtime_db" for r in rules)


@pytest.mark.asyncio
async def test_embedded_to_cloud_yaml_rule_fires():
    """The YAML-only `embedded_to_cloud_mismatch` rule — not in the
    68-A hardcoded path — must fire purely from a ParsedSpec that
    has project_type=embedded_firmware AND deploy_target=cloud."""
    # Build a spec directly so we don't have to construct a prompt
    # that triggers every sub-detection of the heuristic.
    ps = ip.ParsedSpec(
        project_type=ip.Field("embedded_firmware", 0.9),
        deploy_target=ip.Field("cloud", 0.9),
        raw_text="",
    )
    conflicts = ip.detect_conflicts(ps)
    assert any(c.id == "embedded_to_cloud_mismatch" for c in conflicts)


@pytest.mark.asyncio
async def test_hardware_without_arch_yaml_rule_fires():
    ps = ip.ParsedSpec(
        hardware_required=ip.Field("yes", 0.9),
        target_arch=ip.Field("unknown", 0.0),
        raw_text="",
    )
    conflicts = ip.detect_conflicts(ps)
    assert any(c.id == "hardware_without_arch" for c in conflicts)


def test_rule_with_empty_when_is_disabled():
    """Safety: an empty/missing `when:` clause would otherwise fire
    on every parse. Detector must treat it as disabled."""
    rule = {"id": "x", "fields": [], "options": [], "when": {}}
    ps = ip.ParsedSpec()
    assert not ip._rule_matches(rule, ps)
    rule_none = {"id": "y"}
    assert not ip._rule_matches(rule_none, ps)


def test_malformed_rule_does_not_crash_detector(monkeypatch):
    """A partial YAML entry must be tolerated — we can't have one
    typo nuking the whole detector."""
    bogus = [
        {"id": "ok", "when": {"project_type": "web_app"}, "options": [], "fields": []},
        {"id": "bad", "when": {"project_type": {"re": "[invalid-regex"}},
         "options": [], "fields": []},
    ]
    monkeypatch.setattr(ip, "_CONFLICTS_CACHE", bogus)
    ps = ip.ParsedSpec(project_type=ip.Field("web_app", 0.9))
    conflicts = ip.detect_conflicts(ps)
    assert any(c.id == "ok" for c in conflicts)
    # bad rule logged at debug + skipped — no exception propagates


# ─── Iterative clarification ──────────────────────────────────

@pytest.mark.asyncio
async def test_apply_clarification_overrides_at_full_confidence():
    p = await ip.parse_intent(
        "Build a static Next.js site that reads from a local SQLite at request time."
    )
    assert any(c.id == "static_with_runtime_db" for c in p.conflicts)
    chosen = ip.apply_clarification(p, "static_with_runtime_db", "ssr_runtime")
    # The operator's pick flips runtime_model to ssr at confidence 1.0.
    assert chosen.runtime_model.value == "ssr"
    assert chosen.runtime_model.confidence == 1.0
    # And the conflict must no longer fire (ssr + runtime DB ≠ conflict).
    assert not any(c.id == "static_with_runtime_db" for c in chosen.conflicts)


def test_apply_clarification_unknown_conflict_is_noop():
    ps = ip.ParsedSpec(runtime_model=ip.Field("ssg", 0.9))
    out = ip.apply_clarification(ps, "does-not-exist", "any")
    assert out is ps  # object identity — truly unchanged


def test_apply_clarification_unknown_option_is_noop():
    ps = ip.ParsedSpec(runtime_model=ip.Field("ssg", 0.9))
    out = ip.apply_clarification(ps, "static_with_runtime_db", "not-a-real-option")
    assert out is ps


@pytest.mark.asyncio
async def test_clarification_loop_bounded_by_max_rounds():
    """Pathological case: operator keeps picking options that don't
    actually resolve the conflict (here `ssg_build_time` leaves
    runtime_model=ssg, raw_text's runtime-DB hint intact, so the
    rule keeps firing). The MAX_CLARIFY_ROUNDS guard is the caller's
    safety net — verify a loop capped by it does terminate and
    doesn't spin."""
    ps = await ip.parse_intent(
        "Build a static Next.js site reads from sqlite at request time."
    )
    rounds = 0
    while ps.conflicts and rounds < ip.MAX_CLARIFY_ROUNDS:
        first = ps.conflicts[0]
        if not first.options:
            break
        ps = ip.apply_clarification(ps, first.id, "ssg_build_time")
        rounds += 1
    assert rounds == ip.MAX_CLARIFY_ROUNDS


@pytest.mark.asyncio
async def test_choosing_ssr_resolves_static_runtime_conflict():
    """Sanity: picking the *right* option actually does resolve the
    conflict in one round. Confirms apply_clarification's override
    is semantic (flips runtime_model to ssr), not cosmetic."""
    ps = await ip.parse_intent(
        "Build a static Next.js site reads from sqlite at request time."
    )
    assert any(c.id == "static_with_runtime_db" for c in ps.conflicts)
    ps2 = ip.apply_clarification(ps, "static_with_runtime_db", "ssr_runtime")
    assert ps2.runtime_model.value == "ssr"
    assert not any(c.id == "static_with_runtime_db" for c in ps2.conflicts)
