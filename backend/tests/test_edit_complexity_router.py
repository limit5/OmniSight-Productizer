"""V1 #8 (issue #317) — Edit complexity auto-router contract tests.

Pins ``backend/edit_complexity_router.py`` against:

  * structural invariants (module exports, schema version, frozen
    dataclasses, JSON-safe ``to_dict``);
  * three-bucket complexity enum + fixed ``complexity → (provider,
    model)`` mapping;
  * deterministic signal extraction (word count across bilingual
    input, shadcn primitive detection, small/large pattern hits,
    action-verb counter, conjunction counter);
  * classifier decision boundaries on representative prompts for
    each bucket (pinned by concrete examples from the UI Designer
    skill SOP);
  * graceful handling of blank / ``None`` prompts;
  * multimodal context override (image / figma / url → large);
  * caller overrides (explicit ``complexity=``, ``provider=``,
    ``model=``) and reason provenance;
  * agent entry point returns JSON-safe dict;
  * determinism (byte-identical markdown render across calls).

The router is a *pure function* — no LLM call, no network, no
filesystem — so these tests are fast and hermetic.
"""

from __future__ import annotations

import json

import pytest

from backend import edit_complexity_router as ecr
from backend.edit_complexity_router import (
    COMPLEXITY_TO_MODEL,
    DEFAULT_LARGE_MODEL,
    DEFAULT_MEDIUM_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_SMALL_MODEL,
    EDIT_ROUTER_SCHEMA_VERSION,
    EXPECTED_LATENCY_MS,
    EditComplexity,
    EditRouteDecision,
    EditSignals,
    HEAVY_CONJUNCTION_COUNT,
    LARGE_ACTION_FLOOR,
    LARGE_PRIMITIVE_COUNT,
    LARGE_WORD_FLOOR,
    MEDIUM_ACTION_FLOOR,
    SMALL_WORD_CEILING,
    classify_prompt,
    render_decision_markdown,
    route,
    run_edit_router,
)


# ── Module invariants ────────────────────────────────────────────────


EXPECTED_ALL = {
    "EDIT_ROUTER_SCHEMA_VERSION",
    "DEFAULT_PROVIDER",
    "DEFAULT_SMALL_MODEL",
    "DEFAULT_MEDIUM_MODEL",
    "DEFAULT_LARGE_MODEL",
    "COMPLEXITY_TO_MODEL",
    "EXPECTED_LATENCY_MS",
    "EditComplexity",
    "EditSignals",
    "EditRouteDecision",
    "classify_prompt",
    "route",
    "run_edit_router",
    "render_decision_markdown",
}


def test_all_exports_complete():
    assert set(ecr.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(ecr, name)


def test_schema_version_is_semver():
    parts = EDIT_ROUTER_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    for p in parts:
        assert p.isdigit()


def test_defaults_pin_claude_models():
    """The router must stay aligned with the rest of V1 pipeline — Opus
    4.7 for large, Haiku 4.5 for small, Sonnet 4.6 for medium."""
    assert DEFAULT_PROVIDER == "anthropic"
    assert DEFAULT_SMALL_MODEL == "claude-haiku-4-5"
    assert DEFAULT_MEDIUM_MODEL == "claude-sonnet-4-6"
    assert DEFAULT_LARGE_MODEL == "claude-opus-4-7"


def test_complexity_to_model_is_readonly():
    with pytest.raises(TypeError):
        COMPLEXITY_TO_MODEL["small"] = ("x", "y")  # type: ignore[index]


def test_complexity_to_model_has_all_three_buckets():
    assert set(COMPLEXITY_TO_MODEL.keys()) == {"small", "medium", "large"}
    for bucket, (provider, model) in COMPLEXITY_TO_MODEL.items():
        assert provider == DEFAULT_PROVIDER
        assert model


def test_expected_latency_buckets_monotonic():
    assert EXPECTED_LATENCY_MS["small"] < EXPECTED_LATENCY_MS["medium"]
    assert EXPECTED_LATENCY_MS["medium"] < EXPECTED_LATENCY_MS["large"]


def test_expected_latency_readonly():
    with pytest.raises(TypeError):
        EXPECTED_LATENCY_MS["small"] = 1  # type: ignore[index]


def test_small_latency_meets_sub_three_second_spec():
    """TODO contract: Haiku path 必須 < 3s.  Enforce the pinned budget."""
    assert EXPECTED_LATENCY_MS["small"] <= 3_000


def test_enum_values_pin_strings():
    assert EditComplexity.SMALL.value == "small"
    assert EditComplexity.MEDIUM.value == "medium"
    assert EditComplexity.LARGE.value == "large"


def test_enum_is_str_subclass_for_json():
    """Routing decisions are serialised straight to JSON; enum members
    should be usable as strings without a custom encoder."""
    assert isinstance(EditComplexity.SMALL, str)
    assert json.dumps({"c": EditComplexity.SMALL}) == '{"c": "small"}'


def test_thresholds_sane():
    assert SMALL_WORD_CEILING >= 5
    assert LARGE_WORD_FLOOR > SMALL_WORD_CEILING
    assert LARGE_PRIMITIVE_COUNT >= 2
    assert MEDIUM_ACTION_FLOOR >= 2
    assert LARGE_ACTION_FLOOR >= MEDIUM_ACTION_FLOOR
    assert HEAVY_CONJUNCTION_COUNT >= 3


# ── EditSignals ──────────────────────────────────────────────────────


def test_edit_signals_frozen():
    s = EditSignals()
    with pytest.raises(Exception):
        s.word_count = 5  # type: ignore[misc]


def test_edit_signals_empty_defaults_ok():
    s = EditSignals()
    assert s.word_count == 0
    assert s.small_hits == ()
    assert s.large_hits == ()
    assert s.component_mentions == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("word_count", -1),
        ("action_verb_count", -1),
        ("conjunction_count", -1),
    ],
)
def test_edit_signals_rejects_negative(field, value):
    with pytest.raises(ValueError):
        EditSignals(**{field: value})


def test_edit_signals_to_dict_json_safe():
    s = EditSignals(
        word_count=10,
        small_hits=("copy_tweak",),
        large_hits=(),
        component_mentions=("button",),
        action_verb_count=2,
        conjunction_count=1,
        has_image=True,
        has_figma=False,
        has_url=False,
        has_existing_code=True,
    )
    d = s.to_dict()
    # Round-trips through json.
    round_tripped = json.loads(json.dumps(d))
    assert round_tripped == d
    # No tuples leak.
    assert isinstance(d["small_hits"], list)
    assert isinstance(d["component_mentions"], list)


# ── EditRouteDecision ────────────────────────────────────────────────


def test_decision_frozen():
    d = route("rename the button")
    with pytest.raises(Exception):
        d.complexity = "large"  # type: ignore[misc]


def test_decision_rejects_unknown_complexity():
    with pytest.raises(ValueError):
        EditRouteDecision(
            complexity="xxl",  # unknown bucket
            provider="anthropic",
            model="claude-haiku-4-5",
            reasons=(),
            signals=EditSignals(),
        )


def test_decision_rejects_blank_provider_or_model():
    with pytest.raises(ValueError):
        EditRouteDecision(
            complexity="small",
            provider="",
            model="claude-haiku-4-5",
            reasons=(),
            signals=EditSignals(),
        )
    with pytest.raises(ValueError):
        EditRouteDecision(
            complexity="small",
            provider="anthropic",
            model="",
            reasons=(),
            signals=EditSignals(),
        )


def test_decision_to_dict_json_safe():
    d = route("build a new pricing page with three tiers and toggle")
    out = d.to_dict()
    assert json.loads(json.dumps(out)) == out
    assert out["schema_version"] == EDIT_ROUTER_SCHEMA_VERSION
    assert out["complexity"] in COMPLEXITY_TO_MODEL
    assert isinstance(out["reasons"], list)
    assert isinstance(out["signals"], dict)
    assert out["expected_latency_ms"] > 0


# ── Signal extraction (classify_prompt pure behaviour) ───────────────


def test_classify_is_pure_and_deterministic():
    p = "Change the button colour to blue and make the spacing tighter"
    a = classify_prompt(p)
    b = classify_prompt(p)
    assert a == b


def test_classify_blank_returns_medium_and_empty_marker():
    bucket, signals, reasons = classify_prompt("")
    assert bucket == "medium"
    assert reasons == ("empty_prompt",)
    assert signals.word_count == 0


def test_classify_none_is_treated_as_blank():
    bucket, signals, reasons = classify_prompt(None)  # type: ignore[arg-type]
    assert bucket == "medium"
    assert "empty_prompt" in reasons


def test_classify_whitespace_only_is_empty():
    bucket, signals, reasons = classify_prompt("   \n  \t  ")
    assert bucket == "medium"
    assert reasons == ("empty_prompt",)


# ── Small-bucket examples ────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "rename the submit button to Save",
        "fix the typo in the footer",
        "change the button colour to blue",
        "tighten the card header spacing",
        "make the title a bit bigger",
        "small tweak: bump the gap",
        "改字：把標題改成「開始」",
        "改色：primary 換成 neural-blue",
        "微調 card 的內距",
    ],
)
def test_small_bucket_representative_prompts(prompt: str):
    d = route(prompt)
    assert d.complexity == "small", (
        f"expected small, got {d.complexity} for {prompt!r}; "
        f"reasons={d.reasons}"
    )
    assert d.model == DEFAULT_SMALL_MODEL
    assert d.provider == DEFAULT_PROVIDER


def test_small_bucket_surfaces_specific_reason():
    """A small-bucket decision should name *why* — the heuristic's
    value comes from explainability, not just the pick."""
    d = route("change the Primary button colour to blue")
    assert any(r.startswith("small:") for r in d.reasons), d.reasons


# ── Large-bucket examples ────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "build a new pricing page with three tiers and a yearly/monthly toggle",
        "redesign the whole dashboard from scratch",
        "revamp the settings layout end-to-end",
        "restructure the sidebar navigation and add a breadcrumb",
        "做一個定價頁面，三個方案，年月切換",
        "重新設計整個 settings 頁面並加入 multi-step form",
        "major rewrite of the onboarding flow with new auth",
        "create a landing page hero features and pricing section",
    ],
)
def test_large_bucket_representative_prompts(prompt: str):
    d = route(prompt)
    assert d.complexity == "large", (
        f"expected large, got {d.complexity} for {prompt!r}; "
        f"reasons={d.reasons}"
    )
    assert d.model == DEFAULT_LARGE_MODEL


def test_large_bucket_surfaces_specific_reason():
    d = route("redesign the entire checkout flow")
    assert any(r.startswith("large:") for r in d.reasons), d.reasons


# ── Medium-bucket examples (the catch-all) ───────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        # Medium by ambiguity: multi-action but no structural marker and
        # more words than the small ceiling.
        "add a helper text under the email input, hook up blur validation, "
        "and show an inline error badge beside the field",
        # Medium by action count — several verbs but nothing structural.
        "swap the hero background, hook the CTA to the new route handler, "
        "and wire the state to the cart context",
    ],
)
def test_medium_bucket_representative_prompts(prompt: str):
    d = route(prompt)
    assert d.complexity == "medium", (
        f"expected medium, got {d.complexity} for {prompt!r}; "
        f"reasons={d.reasons}"
    )
    assert d.model == DEFAULT_MEDIUM_MODEL


# ── Multimodal context override ──────────────────────────────────────


def test_has_image_forces_large():
    d = route("tiny tweak", has_image=True)
    assert d.complexity == "large"
    assert "multimodal_image" in d.reasons


def test_has_figma_forces_large():
    d = route("rename the button", has_figma=True)
    assert d.complexity == "large"
    assert "multimodal_figma" in d.reasons


def test_has_url_forces_large():
    d = route("change spacing", has_url=True)
    assert d.complexity == "large"
    assert "multimodal_url" in d.reasons


def test_empty_prompt_with_image_still_large():
    d = route("", has_image=True)
    assert d.complexity == "large"
    assert d.model == DEFAULT_LARGE_MODEL
    assert "multimodal_image" in d.reasons


# ── Caller overrides ─────────────────────────────────────────────────


def test_explicit_complexity_override_wins():
    d = route("redesign the entire dashboard", complexity="small")
    assert d.complexity == "small"
    assert d.model == DEFAULT_SMALL_MODEL
    assert any(r.startswith("caller_override:") for r in d.reasons)


def test_explicit_complexity_override_confirm_reason():
    d = route("rename the button", complexity="small")
    assert d.complexity == "small"
    assert "caller_override_confirm" in d.reasons


def test_unknown_complexity_override_raises():
    with pytest.raises(ValueError):
        route("whatever", complexity="tiny")


def test_explicit_provider_model_override():
    d = route(
        "rename the button",
        provider="openrouter",
        model="anthropic/claude-haiku-4-5",
    )
    assert d.provider == "openrouter"
    assert d.model == "anthropic/claude-haiku-4-5"
    assert any(r.startswith("provider_override:") for r in d.reasons)
    assert any(r.startswith("model_override:") for r in d.reasons)


def test_existing_code_nudges_medium_toward_small():
    """An edit to existing code with no structural signal and a short
    prompt should prefer small — editing is narrower than creating."""
    # A prompt that would otherwise land in medium.
    base = route("adjust the helper text")
    d = route("adjust the helper text", has_existing_code=True)
    # Either we were already small (no nudge needed) or we got nudged.
    assert d.complexity in {"small", "medium"}
    if base.complexity == "medium":
        # We expect the nudge to fire and flip the bucket.
        assert d.complexity == "small"
        assert "existing_code_nudge" in d.reasons


def test_existing_code_flag_does_not_downgrade_large():
    d = route(
        "redesign the dashboard from scratch",
        has_existing_code=True,
    )
    assert d.complexity == "large"  # large markers still win


# ── Component mention detection ──────────────────────────────────────


def test_primitive_mentions_collected():
    d = route("update the Button and the Card padding")
    assert "button" in d.signals.component_mentions
    assert "card" in d.signals.component_mentions


def test_many_primitives_lifts_to_large():
    d = route(
        "Update the Button, Input, Card, and Dialog to match the new tokens"
    )
    assert d.complexity == "large"
    assert any(r.startswith("many_primitives:") for r in d.reasons)


def test_primitive_mentions_deduplicated_and_sorted():
    d = route("The Button, the button, and another Button")
    assert d.signals.component_mentions == ("button",)


def test_hyphen_variant_of_component_name_detected():
    d = route("update the alert-dialog copy")
    assert "alert-dialog" in d.signals.component_mentions


# ── Word count / CJK handling ────────────────────────────────────────


def test_word_count_ignores_fenced_code():
    # Without fence stripping a 500-char code block would dominate.
    prompt_with_code = "fix typo\n```tsx\n" + ("x " * 200) + "\n```"
    d = route(prompt_with_code)
    # Should stay short despite the big code block.
    assert d.signals.word_count < 20


def test_cjk_words_counted_per_glyph():
    prompt = "重構整個儀表板並加入登入流程"  # 13 glyphs
    signals = classify_prompt(prompt)[1]
    assert signals.word_count >= 10


# ── Conjunction / action verb counters ───────────────────────────────


def test_action_verbs_counted():
    d = route("add a header, rename the button, swap the colour, move the icon")
    assert d.signals.action_verb_count >= 3


def test_many_conjunctions_block_small_even_with_small_hits():
    # Five things at once, all individually "small" — should not be
    # routed to Haiku because the composite work is medium/large.
    p = (
        "change the title, change the colour, change the padding, "
        "change the icon, change the footer text"
    )
    d = route(p)
    assert d.complexity != "small", (d.complexity, d.reasons)


# ── Reason provenance ────────────────────────────────────────────────


def test_reasons_are_tuple_of_strings():
    d = route("rename the button")
    assert isinstance(d.reasons, tuple)
    assert all(isinstance(r, str) for r in d.reasons)


def test_reasons_not_empty_after_classification():
    d = route("redesign the dashboard")
    assert d.reasons  # never empty once we commit to a bucket


def test_ambiguous_medium_reason_when_no_signals_hit():
    d = route("please")  # no signals, but non-empty
    assert d.complexity == "small" or d.complexity == "medium"
    # The classifier should always explain itself.
    assert d.reasons


# ── Agent entry point (run_edit_router) ──────────────────────────────


def test_run_edit_router_returns_json_safe_dict():
    out = run_edit_router("rename the button")
    assert json.loads(json.dumps(out)) == out
    assert out["schema_version"] == EDIT_ROUTER_SCHEMA_VERSION
    assert out["complexity"] == "small"
    assert out["model"] == DEFAULT_SMALL_MODEL
    assert out["provider"] == DEFAULT_PROVIDER
    assert isinstance(out["signals"], dict)
    assert isinstance(out["reasons"], list)


def test_run_edit_router_round_trips_context_flags():
    out = run_edit_router(
        "refine the heading",
        has_image=True,
        has_figma=False,
        has_url=False,
        has_existing_code=True,
    )
    assert out["complexity"] == "large"
    sig = out["signals"]
    assert sig["has_image"] is True
    assert sig["has_existing_code"] is True


def test_run_edit_router_respects_caller_overrides():
    out = run_edit_router(
        "redesign the dashboard",
        complexity="small",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
    )
    assert out["complexity"] == "small"
    assert out["model"] == "claude-haiku-4-5-20251001"


def test_run_edit_router_rejects_unknown_complexity_override():
    with pytest.raises(ValueError):
        run_edit_router("whatever", complexity="huge")


# ── Determinism of markdown render ───────────────────────────────────


def test_render_decision_markdown_is_deterministic():
    d = route("rename the button")
    a = render_decision_markdown(d)
    b = render_decision_markdown(d)
    assert a == b


def test_render_decision_markdown_has_expected_sections():
    d = route("redesign the dashboard from scratch")
    md = render_decision_markdown(d)
    # Core headers present.
    assert f"schema {EDIT_ROUTER_SCHEMA_VERSION}" in md
    assert "complexity" in md
    assert "provider:model" in md
    assert "reasons" in md
    assert "Signals" in md
    assert d.complexity in md
    assert d.model in md


def test_render_decision_markdown_shows_none_for_empty_signals():
    d = route("", has_url=False)  # blank prompt → empty_prompt + medium
    md = render_decision_markdown(d)
    # small_hits and large_hits are empty, which should render as "(none)"
    assert "(none)" in md


# ── Sibling alignment (contract that keeps V1 pipeline coherent) ─────


def test_large_default_model_matches_vision_to_ui_default():
    """The vision / figma / url pipelines all default to Opus 4.7 —
    this router's 'large' path must pick the same model to avoid
    silent drift."""
    from backend.vision_to_ui import DEFAULT_VISION_MODEL

    assert DEFAULT_LARGE_MODEL == DEFAULT_VISION_MODEL


def test_large_default_provider_matches_siblings():
    from backend.vision_to_ui import DEFAULT_VISION_PROVIDER

    assert DEFAULT_PROVIDER == DEFAULT_VISION_PROVIDER


# ── Pinned SOP example from the TODO ─────────────────────────────────


def test_todo_integration_example_routes_to_large():
    """The integration-test prompt pinned at TODO line 1505 must reach
    Opus 4.7 — otherwise the V1 pipeline never exercises its deep-
    thinking path on the golden-path example."""
    prompt = "做一個定價頁面，三個方案，年月切換"
    d = route(prompt)
    assert d.complexity == "large"
    assert d.model == DEFAULT_LARGE_MODEL
    assert d.provider == DEFAULT_PROVIDER


def test_todo_haiku_example_routes_to_small():
    """SOP step 2 lists 'text / color / spacing' as the Haiku path.
    Pin concrete examples so a future refactor doesn't break the spec."""
    for prompt in [
        "change the title text to Save",
        "change the primary button colour to blue",
        "tighten the card header spacing",
    ]:
        d = route(prompt)
        assert d.complexity == "small", (prompt, d.reasons)
        assert d.model == DEFAULT_SMALL_MODEL
