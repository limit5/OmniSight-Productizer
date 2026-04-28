"""W11.5 #XXX — Contract tests for ``backend.web.content_classifier``.

Pins:

    * Public surface (constants, dataclass shape, error hierarchy,
      Protocol runtime_checkable, package re-exports).
    * Heuristic prefilter — clean, adult, illegal, phishing, regulated
      advice, paywalled, personal-data, brand-impersonation. Negative
      cases (no false positives on innocuous text). ``MAX_REASONS`` cap.
    * Spec-excerpt rendering — title / hero / nav / sections / footer
      pulled in; bytes / colour tokens / image URLs left out;
      ``MAX_PROMPT_INPUT_CHARS`` cap honoured.
    * LLM envelope parser — clean JSON, ``json``-fenced JSON, prose
      around JSON, malformed JSON → ``None``, lenient field defaults.
    * ``classify_clone_spec`` end-to-end — heuristic critical short-
      circuits the LLM call, heuristic-clean still calls the LLM,
      LLM ``ClassifierUnavailableError`` triggers fail-closed (or
      fail-open with the kwarg), parse failure triggers fail-closed,
      ``skip_heuristic`` honoured.
    * ``assert_clone_spec_safe`` — raises ``ContentRiskError`` at + above
      threshold, returns classification at sub-threshold, threshold
      validation.
    * ``merge_risk_classifications`` — empty-args raise, max-aggregate
      semantics, signals_used additive, "clean" pruned in mixed merge,
      ``MAX_REASONS`` cap.
    * ``LangchainClassifierLLM`` — lazy init only on first
      ``classify_text``, raises ``ClassifierUnavailableError`` when
      ``get_cheapest_model`` returns ``None``.

Every test runs without network / LLM I/O: a ``_FakeClassifierLLM``
stand-in is supplied via the ``llm=`` DI hook so neither LangChain nor a
live provider key is required.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

import backend.web as web_pkg
from backend.web.content_classifier import (
    ClassifierLLM,
    ClassifierUnavailableError,
    ContentClassifierError,
    ContentRiskError,
    DEFAULT_CLASSIFIER_MODEL,
    DEFAULT_REFUSAL_THRESHOLD,
    LLM_SYSTEM_PROMPT,
    LLM_USER_PROMPT_TEMPLATE,
    LangchainClassifierLLM,
    MAX_PROMPT_INPUT_CHARS,
    MAX_REASON_CHARS,
    MAX_REASONS,
    RISK_CATEGORIES,
    RISK_LEVELS,
    RiskClassification,
    RiskScore,
    assert_clone_spec_safe,
    classify_clone_spec,
    heuristic_risk_signals,
    merge_risk_classifications,
)
from backend.web.content_classifier import (
    _envelope_to_classification,
    _parse_llm_envelope,
    _spec_excerpt,
)
from backend.web.site_cloner import CloneSpec, SiteClonerError


# ── Fixtures + test doubles ─────────────────────────────────────────────


def _make_spec(
    *,
    title: str = "Example",
    hero_heading: str = "Welcome",
    hero_tagline: str = "A modern landing page for a small product.",
    sections: Optional[list[dict]] = None,
    nav: Optional[list[dict]] = None,
    footer_text: str = "© 2026 Example Corp",
    meta_description: str = "An example marketing page.",
) -> CloneSpec:
    """Build a fully-populated :class:`CloneSpec` for tests."""
    return CloneSpec(
        source_url="https://example.com",
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        title=title,
        meta={"description": meta_description},
        hero={"heading": hero_heading, "tagline": hero_tagline},
        nav=nav or [{"label": "Home", "href": "/"}, {"label": "About", "href": "/about"}],
        sections=sections or [
            {"heading": "Features", "summary": "Built with care for small teams."},
        ],
        footer={"text": footer_text, "links": []},
    )


class _FakeClassifierLLM:
    """Test stand-in for :class:`ClassifierLLM`. Returns canned strings
    keyed by call order so tests can assert exact prompt shape."""

    name = "fake-llm"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        raises: Optional[BaseException] = None,
    ) -> None:
        self.responses = list(responses or ['{"risk_level":"low","categories":[{"name":"clean","level":"low","reason":"ok"}]}'])
        self.calls: list[dict] = []
        self.raises = raises

    async def classify_text(self, prompt: str, *, system: str) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self.raises is not None:
            raise self.raises
        # Round-robin if more calls than canned responses.
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


# ── Public surface ──────────────────────────────────────────────────────


def test_risk_levels_constant_is_ascending_severity():
    assert RISK_LEVELS == ("low", "medium", "high", "critical")


def test_risk_categories_constant_includes_clean_alias():
    # "clean" is a recognised category so a no-flag classification can
    # still emit a recordable score (audit log distinguishes "ran and
    # found nothing" from "never ran").
    assert "clean" in RISK_CATEGORIES
    # Must include the W11 spec line categories.
    for required in (
        "brand_impersonation", "regulated_advice", "paywalled",
        "adult", "illegal", "phishing", "personal_data",
    ):
        assert required in RISK_CATEGORIES


def test_default_refusal_threshold_is_high():
    assert DEFAULT_REFUSAL_THRESHOLD == "high"


def test_default_classifier_model_constant_is_chain_alias():
    assert DEFAULT_CLASSIFIER_MODEL == "cheapest-llm-chain"


def test_max_prompt_input_chars_is_positive():
    assert MAX_PROMPT_INPUT_CHARS > 0
    assert MAX_PROMPT_INPUT_CHARS <= 64_000  # sanity ceiling


def test_max_reason_chars_is_positive():
    assert 0 < MAX_REASON_CHARS <= 1024


def test_max_reasons_is_positive():
    assert 0 < MAX_REASONS <= 32


def test_classifier_llm_is_runtime_checkable_protocol():
    fake = _FakeClassifierLLM()
    assert isinstance(fake, ClassifierLLM)


def test_classifier_llm_protocol_rejects_missing_method():
    class NoCallable:
        name = "x"
    assert not isinstance(NoCallable(), ClassifierLLM)


def test_risk_score_is_frozen_dataclass():
    s = RiskScore("clean", "low", "ok")
    with pytest.raises(Exception):
        s.level = "critical"  # type: ignore[misc]


def test_risk_classification_is_frozen_dataclass():
    rc = RiskClassification(
        risk_level="low", scores=(), model="m", signals_used=(),
    )
    with pytest.raises(Exception):
        rc.risk_level = "high"  # type: ignore[misc]


def test_risk_classification_is_clean_property():
    empty = RiskClassification("low", (), "m", (), False)
    assert empty.is_clean
    only_clean = RiskClassification(
        "low", (RiskScore("clean", "low", ""),), "m", (), False,
    )
    assert only_clean.is_clean
    has_flag = RiskClassification(
        "high", (RiskScore("phishing", "high", "x"),), "m", (), False,
    )
    assert not has_flag.is_clean


def test_risk_classification_categories_and_reasons_props_align():
    rc = RiskClassification(
        "high",
        (
            RiskScore("phishing", "high", "p"),
            RiskScore("adult", "critical", "a"),
        ),
        model="m",
        signals_used=("llm",),
    )
    assert rc.categories == ("phishing", "adult")
    assert rc.reasons == ("p", "a")


def test_error_hierarchy_chains_to_site_cloner_error():
    # All three custom errors descend from SiteClonerError so a single
    # ``except SiteClonerError`` in the calling router catches both
    # layers without special-casing.
    assert issubclass(ContentClassifierError, SiteClonerError)
    assert issubclass(ContentRiskError, ContentClassifierError)
    assert issubclass(ClassifierUnavailableError, ContentClassifierError)


def test_content_risk_error_carries_classification_and_threshold():
    rc = RiskClassification(
        "critical",
        (RiskScore("adult", "critical", "x"),),
        model="m",
        signals_used=("heuristic",),
        prefilter_only=True,
    )
    err = ContentRiskError(rc, threshold="high")
    assert err.classification is rc
    assert err.threshold == "high"
    assert "critical" in str(err)
    assert "high" in str(err)


# ── Heuristic prefilter ─────────────────────────────────────────────────


def test_heuristic_clean_returns_low_with_clean_score():
    spec = _make_spec()
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "low"
    assert rc.is_clean
    assert rc.scores == (
        RiskScore("clean", "low", "no heuristic flags fired"),
    )
    assert rc.signals_used == ("heuristic",)
    assert rc.model == "heuristic"
    assert rc.prefilter_only is False


def test_heuristic_empty_spec_returns_canonical_clean():
    spec = CloneSpec(
        source_url="https://x.com", fetched_at="t", backend="mock",
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "low"
    assert rc.is_clean


def test_heuristic_adult_keyword_fires_critical():
    spec = _make_spec(title="Live cams XXX", hero_tagline="NSFW content")
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "critical"
    assert "adult" in rc.categories


def test_heuristic_illegal_keyword_fires_critical():
    spec = _make_spec(
        hero_tagline="Buy cocaine online with bitcoin",
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "critical"
    assert "illegal" in rc.categories


def test_heuristic_phishing_pattern_fires_critical():
    spec = _make_spec(
        hero_heading="Verify your account immediately",
        hero_tagline="Your account has been suspended",
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "critical"
    assert "phishing" in rc.categories


def test_heuristic_regulated_advice_fires_high():
    spec = _make_spec(
        hero_tagline="We provide medical advice for chronic illnesses.",
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "high"
    assert "regulated_advice" in rc.categories


def test_heuristic_paywalled_fires_high():
    spec = _make_spec(
        hero_tagline="Subscribe to read the full article.",
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "high"
    assert "paywalled" in rc.categories


def test_heuristic_personal_data_fires_medium():
    spec = _make_spec(
        hero_tagline="Use our background check report service.",
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "medium"
    assert "personal_data" in rc.categories


def test_heuristic_brand_impersonation_fires_medium():
    spec = _make_spec(
        hero_heading="Verify your Apple ID",
        hero_tagline="Sign in to continue",
    )
    rc = heuristic_risk_signals(spec)
    # Apple-ID phrase fires brand_impersonation. (No phishing word
    # match here unless the page asks to "verify your account".)
    assert rc.risk_level == "medium"
    assert "brand_impersonation" in rc.categories


def test_heuristic_ignores_innocuous_text_with_no_false_positives():
    spec = _make_spec(
        title="Open Source Documentation",
        hero_heading="Welcome to our docs",
        hero_tagline="A friendly guide to building with our SDK.",
        sections=[
            {"heading": "Quickstart", "summary": "Install with npm."},
            {"heading": "API", "summary": "REST endpoints documented in detail."},
        ],
        footer_text="© 2026 Open Project · Apache 2.0",
        meta_description="Open-source SDK documentation site.",
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "low"
    assert rc.is_clean


def test_heuristic_aggregate_is_max_across_categories():
    spec = _make_spec(
        hero_heading="Subscribe to read",  # paywalled, high
        hero_tagline="Background check report database",  # personal_data, medium
    )
    rc = heuristic_risk_signals(spec)
    assert rc.risk_level == "high"
    assert "paywalled" in rc.categories
    assert "personal_data" in rc.categories


def test_heuristic_caps_scores_at_max_reasons():
    # Stuff every keyword family into one spec to verify the cap works.
    spec = _make_spec(
        hero_heading="Live cams porn xxx",  # adult
        hero_tagline=(
            "Buy cocaine online · subscribe to read · medical advice · "
            "verify your apple id · background check report · "
            "verify your account immediately · click here to confirm "
            "your password"
        ),
    )
    rc = heuristic_risk_signals(spec)
    assert len(rc.scores) <= MAX_REASONS


def test_heuristic_each_score_reason_under_max_chars():
    spec = _make_spec(hero_tagline="medical advice")
    rc = heuristic_risk_signals(spec)
    for s in rc.scores:
        assert len(s.reason) <= MAX_REASON_CHARS


def test_heuristic_only_one_score_per_category():
    spec = _make_spec(
        hero_tagline="medical advice; legal advice; tax advice",
    )
    rc = heuristic_risk_signals(spec)
    cats = [s.category for s in rc.scores]
    assert len(cats) == len(set(cats))


# ── Spec excerpt ────────────────────────────────────────────────────────


def test_spec_excerpt_includes_title():
    spec = _make_spec(title="Distinctive Title 123")
    assert "Distinctive Title 123" in _spec_excerpt(spec)


def test_spec_excerpt_includes_hero():
    spec = _make_spec(hero_heading="HeroH", hero_tagline="HeroT")
    out = _spec_excerpt(spec)
    assert "HeroH" in out
    assert "HeroT" in out


def test_spec_excerpt_includes_meta_description():
    spec = _make_spec(meta_description="A unique meta line")
    assert "A unique meta line" in _spec_excerpt(spec)


def test_spec_excerpt_includes_nav_labels():
    spec = _make_spec(
        nav=[{"label": "Pricing", "href": "/p"},
             {"label": "Docs", "href": "/d"}],
    )
    out = _spec_excerpt(spec)
    assert "Pricing" in out
    assert "Docs" in out


def test_spec_excerpt_includes_section_headings_and_summaries():
    spec = _make_spec(sections=[
        {"heading": "TestSectionA", "summary": "TestSummaryA"},
    ])
    out = _spec_excerpt(spec)
    assert "TestSectionA" in out
    assert "TestSummaryA" in out


def test_spec_excerpt_includes_footer_text():
    spec = _make_spec(footer_text="© 2026 Contoso unique")
    out = _spec_excerpt(spec)
    assert "Contoso unique" in out


def test_spec_excerpt_omits_image_urls_and_colour_tokens():
    # We deliberately do NOT embed bytes / asset URLs / colours into the
    # excerpt — they carry no semantic risk signal.
    spec = CloneSpec(
        source_url="https://x.com", fetched_at="t", backend="mock",
        title="T",
        images=[{"url": "https://cdn.example.com/special-image.png"}],
        colors=["#ff00aa"],
        fonts=["Inter"],
    )
    out = _spec_excerpt(spec)
    assert "special-image.png" not in out
    assert "#ff00aa" not in out
    assert "Inter" not in out


def test_spec_excerpt_truncates_to_max_chars():
    huge = "x" * 50_000
    spec = _make_spec(
        hero_tagline=huge,
        sections=[{"heading": "H", "summary": huge}] * 6,
        footer_text=huge,
    )
    out = _spec_excerpt(spec)
    assert len(out) <= MAX_PROMPT_INPUT_CHARS


def test_spec_excerpt_caps_nav_at_12_links():
    spec = _make_spec(
        nav=[{"label": f"Link{i}", "href": f"/l/{i}"} for i in range(50)],
    )
    out = _spec_excerpt(spec)
    # First 12 in, beyond that out
    assert "Link0" in out
    assert "Link11" in out
    assert "Link20" not in out


def test_spec_excerpt_caps_sections_at_6():
    spec = _make_spec(
        sections=[
            {"heading": f"Section{i}", "summary": f"Summary{i}"}
            for i in range(20)
        ],
    )
    out = _spec_excerpt(spec)
    assert "Section0" in out
    assert "Section5" in out
    assert "Section10" not in out


# ── LLM envelope parser ────────────────────────────────────────────────


def test_parse_envelope_clean_json():
    raw = '{"risk_level":"low","categories":[]}'
    assert _parse_llm_envelope(raw) == {"risk_level": "low", "categories": []}


def test_parse_envelope_strips_json_fence():
    raw = '```json\n{"risk_level":"high","categories":[]}\n```'
    assert _parse_llm_envelope(raw) == {"risk_level": "high", "categories": []}


def test_parse_envelope_strips_bare_fence():
    raw = '```\n{"risk_level":"medium","categories":[]}\n```'
    parsed = _parse_llm_envelope(raw)
    assert parsed is not None
    assert parsed["risk_level"] == "medium"


def test_parse_envelope_extracts_from_prose_wrap():
    raw = "Here is my answer: {\"risk_level\":\"low\",\"categories\":[]}\nThanks!"
    parsed = _parse_llm_envelope(raw)
    assert parsed is not None
    assert parsed["risk_level"] == "low"


def test_parse_envelope_returns_none_on_garbage():
    assert _parse_llm_envelope("not json at all") is None
    assert _parse_llm_envelope("") is None
    assert _parse_llm_envelope("   ") is None


def test_parse_envelope_returns_none_on_non_dict_json():
    # An array is valid JSON but not a dict envelope.
    assert _parse_llm_envelope("[1,2,3]") is None
    assert _parse_llm_envelope('"just a string"') is None


def test_envelope_to_classification_recomputes_aggregate():
    # Even if the envelope claims "low" the aggregate is the max across
    # categories — a hallucinated "low" with a "critical" category MUST
    # surface as critical.
    env = {
        "risk_level": "low",
        "categories": [
            {"name": "adult", "level": "critical", "reason": "x"},
        ],
    }
    rc = _envelope_to_classification(
        env, model="m", signals_used=("llm",),
    )
    assert rc.risk_level == "critical"


def test_envelope_to_classification_drops_unknown_categories():
    env = {
        "risk_level": "low",
        "categories": [
            {"name": "made_up_category", "level": "high", "reason": "x"},
            {"name": "phishing", "level": "high", "reason": "y"},
        ],
    }
    rc = _envelope_to_classification(env, model="m", signals_used=("llm",))
    assert rc.categories == ("phishing",)


def test_envelope_to_classification_truncates_long_reasons():
    env = {
        "risk_level": "high",
        "categories": [
            {"name": "phishing", "level": "high", "reason": "x" * 9999},
        ],
    }
    rc = _envelope_to_classification(env, model="m", signals_used=("llm",))
    assert len(rc.scores[0].reason) <= MAX_REASON_CHARS


def test_envelope_to_classification_synthesises_clean_when_categories_empty():
    env = {"risk_level": "low", "categories": []}
    rc = _envelope_to_classification(env, model="m", signals_used=("llm",))
    assert rc.is_clean
    assert rc.scores[0].category == "clean"


def test_envelope_to_classification_caps_at_max_reasons():
    env = {
        "risk_level": "high",
        "categories": [
            {"name": cat, "level": "high", "reason": "x"}
            for cat in RISK_CATEGORIES * 3  # well over the cap
        ],
    }
    rc = _envelope_to_classification(env, model="m", signals_used=("llm",))
    assert len(rc.scores) <= MAX_REASONS


def test_envelope_to_classification_dedupes_categories():
    env = {
        "risk_level": "high",
        "categories": [
            {"name": "phishing", "level": "high", "reason": "first"},
            {"name": "phishing", "level": "critical", "reason": "second"},
        ],
    }
    rc = _envelope_to_classification(env, model="m", signals_used=("llm",))
    assert len(rc.scores) == 1
    # First occurrence wins on dedupe (deterministic for audit).
    assert rc.scores[0].reason == "first"


# ── classify_clone_spec end-to-end ──────────────────────────────────────


def test_classify_short_circuits_on_heuristic_critical():
    spec = _make_spec(hero_heading="XXX porn live cams")
    fake = _FakeClassifierLLM()
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    assert rc.risk_level == "critical"
    assert rc.prefilter_only is True
    # LLM was NOT called — short-circuit saved the LLM round trip.
    assert fake.calls == []


def test_classify_calls_llm_when_heuristic_clean():
    spec = _make_spec()
    fake = _FakeClassifierLLM(responses=[
        '{"risk_level":"low","categories":[{"name":"clean","level":"low","reason":"safe"}]}',
    ])
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    assert rc.risk_level == "low"
    assert "llm" in rc.signals_used
    assert "heuristic" in rc.signals_used
    assert len(fake.calls) == 1


def test_classify_passes_system_prompt_constant():
    spec = _make_spec()
    fake = _FakeClassifierLLM()
    asyncio.run(classify_clone_spec(spec, llm=fake))
    assert fake.calls[0]["system"] == LLM_SYSTEM_PROMPT


def test_classify_passes_excerpt_in_user_prompt():
    spec = _make_spec(title="DistinctTitle ZQX")
    fake = _FakeClassifierLLM()
    asyncio.run(classify_clone_spec(spec, llm=fake))
    assert "DistinctTitle ZQX" in fake.calls[0]["prompt"]


def test_classify_records_prefilter_summary_in_user_prompt():
    spec = _make_spec(hero_tagline="medical advice")
    fake = _FakeClassifierLLM()
    asyncio.run(classify_clone_spec(spec, llm=fake))
    # Prefilter found regulated_advice → mentioned in user prompt.
    assert "regulated_advice" in fake.calls[0]["prompt"]


def test_classify_merges_heuristic_and_llm_scores():
    spec = _make_spec(hero_tagline="medical advice")  # heuristic high
    fake = _FakeClassifierLLM(responses=[
        '{"risk_level":"medium","categories":[{"name":"brand_impersonation","level":"medium","reason":"logo"}]}',
    ])
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    cats = set(rc.categories)
    assert "regulated_advice" in cats
    assert "brand_impersonation" in cats
    assert rc.risk_level == "high"  # max(high, medium) = high
    assert rc.signals_used == ("heuristic", "llm")


def test_classify_fail_closed_when_llm_unavailable():
    spec = _make_spec()
    fake = _FakeClassifierLLM(
        raises=ClassifierUnavailableError("token frozen"),
    )
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    assert rc.risk_level == "high"
    assert "fail_closed" in rc.signals_used
    assert any(
        "classifier_unavailable" in s.reason for s in rc.scores
    )


def test_classify_fail_open_trusts_heuristic_when_llm_unavailable():
    spec = _make_spec()
    fake = _FakeClassifierLLM(
        raises=ClassifierUnavailableError("token frozen"),
    )
    rc = asyncio.run(classify_clone_spec(spec, llm=fake, fail_open=True))
    # Heuristic was clean → fail_open keeps it clean.
    assert rc.risk_level == "low"
    assert "fail_open" in rc.signals_used


def test_classify_fail_closed_on_llm_parse_failure():
    spec = _make_spec()
    fake = _FakeClassifierLLM(responses=["this is not JSON at all"])
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    assert rc.risk_level == "high"
    assert any("llm_parse_failed" in s.reason for s in rc.scores)
    assert "fail_closed" in rc.signals_used


def test_classify_skip_heuristic_does_not_run_keyword_sweep():
    # The spec has an adult keyword. If the heuristic ran it would
    # short-circuit to critical without consulting the LLM. With
    # ``skip_heuristic=True`` the LLM gets the call.
    spec = _make_spec(hero_heading="XXX live cams")
    fake = _FakeClassifierLLM(responses=[
        '{"risk_level":"low","categories":[{"name":"clean","level":"low","reason":"ok"}]}',
    ])
    rc = asyncio.run(classify_clone_spec(spec, llm=fake, skip_heuristic=True))
    assert len(fake.calls) == 1
    assert rc.risk_level == "low"
    assert rc.signals_used == ("llm",)


def test_classify_rejects_non_clone_spec_input():
    fake = _FakeClassifierLLM()
    with pytest.raises(ContentClassifierError):
        asyncio.run(classify_clone_spec("not a spec", llm=fake))  # type: ignore[arg-type]


def test_classify_records_picked_model_in_classification():
    spec = _make_spec()
    fake = _FakeClassifierLLM()
    fake.name = "haiku-4.5-actual"
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    assert rc.model == "haiku-4.5-actual"


# ── assert_clone_spec_safe ──────────────────────────────────────────────


def test_assert_safe_returns_classification_under_threshold():
    rc = RiskClassification(
        "low", (RiskScore("clean", "low", ""),), "m", ("llm",),
    )
    spec = _make_spec()
    out = assert_clone_spec_safe(spec, classification=rc)
    assert out is rc


def test_assert_safe_raises_at_threshold():
    rc = RiskClassification(
        "high", (RiskScore("phishing", "high", "x"),), "m", ("llm",),
    )
    with pytest.raises(ContentRiskError) as excinfo:
        assert_clone_spec_safe(_make_spec(), classification=rc, threshold="high")
    assert excinfo.value.threshold == "high"
    assert excinfo.value.classification is rc


def test_assert_safe_raises_above_threshold():
    rc = RiskClassification(
        "critical", (RiskScore("adult", "critical", "x"),), "m", ("llm",),
    )
    with pytest.raises(ContentRiskError):
        assert_clone_spec_safe(_make_spec(), classification=rc, threshold="high")


def test_assert_safe_threshold_critical_lets_high_through():
    rc = RiskClassification(
        "high", (RiskScore("phishing", "high", "x"),), "m", ("llm",),
    )
    out = assert_clone_spec_safe(
        _make_spec(), classification=rc, threshold="critical",
    )
    assert out is rc


def test_assert_safe_runs_heuristic_when_classification_omitted():
    spec = _make_spec(hero_heading="XXX live cams")  # adult → critical
    with pytest.raises(ContentRiskError):
        assert_clone_spec_safe(spec)  # heuristic runs


def test_assert_safe_returns_clean_when_no_classification_and_clean_spec():
    spec = _make_spec()
    rc = assert_clone_spec_safe(spec)
    assert rc.risk_level == "low"


def test_assert_safe_rejects_unknown_threshold():
    rc = RiskClassification("low", (), "m", ("llm",))
    with pytest.raises(ContentClassifierError):
        assert_clone_spec_safe(_make_spec(), classification=rc, threshold="banana")


def test_assert_safe_rejects_unknown_risk_level_in_classification():
    rc = RiskClassification("banana", (), "m", ("llm",))  # type: ignore[arg-type]
    with pytest.raises(ContentClassifierError):
        assert_clone_spec_safe(_make_spec(), classification=rc)


# ── merge_risk_classifications ──────────────────────────────────────────


def test_merge_empty_args_raises():
    with pytest.raises(ContentClassifierError):
        merge_risk_classifications()


def test_merge_one_arg_returns_equivalent():
    rc = RiskClassification(
        "high", (RiskScore("phishing", "high", "x"),), "m", ("llm",),
    )
    out = merge_risk_classifications(rc)
    assert out.risk_level == "high"
    assert out.categories == ("phishing",)


def test_merge_takes_max_aggregate():
    a = RiskClassification(
        "low", (RiskScore("clean", "low", "ok"),), "h", ("heuristic",),
    )
    b = RiskClassification(
        "high", (RiskScore("phishing", "high", "x"),), "l", ("llm",),
    )
    out = merge_risk_classifications(a, b)
    assert out.risk_level == "high"
    # Last-input model wins (most authoritative).
    assert out.model == "l"


def test_merge_concatenates_signals_used_in_order_no_dupes():
    a = RiskClassification("low", (), "m", ("heuristic",))
    b = RiskClassification("low", (), "m", ("llm",))
    c = RiskClassification("low", (), "m", ("heuristic", "llm"))
    out = merge_risk_classifications(a, b, c)
    assert out.signals_used == ("heuristic", "llm")


def test_merge_drops_clean_when_other_categories_fire():
    a = RiskClassification(
        "low", (RiskScore("clean", "low", "ok"),), "m", ("heuristic",),
    )
    b = RiskClassification(
        "high", (RiskScore("phishing", "high", "x"),), "m", ("llm",),
    )
    out = merge_risk_classifications(a, b)
    cats = set(out.categories)
    assert "clean" not in cats
    assert "phishing" in cats


def test_merge_caps_scores_at_max_reasons():
    rcs = [
        RiskClassification(
            "high", (RiskScore(cat, "high", "x"),), "m", ("llm",),
        )
        for cat in RISK_CATEGORIES * 2  # > MAX_REASONS
    ]
    out = merge_risk_classifications(*rcs)
    assert len(out.scores) <= MAX_REASONS


def test_merge_later_score_wins_on_category_tie():
    a = RiskClassification(
        "high",
        (RiskScore("phishing", "high", "first"),),
        "h", ("heuristic",),
    )
    b = RiskClassification(
        "critical",
        (RiskScore("phishing", "critical", "second"),),
        "l", ("llm",),
    )
    out = merge_risk_classifications(a, b)
    # LLM verdict overrides heuristic for the same category.
    assert len(out.scores) == 1
    assert out.scores[0].level == "critical"
    assert out.scores[0].reason == "second"


def test_merge_preserves_prefilter_only_when_any_input_set_it():
    a = RiskClassification("low", (), "m", ("heuristic",), prefilter_only=True)
    b = RiskClassification("low", (), "m", ("llm",))
    out = merge_risk_classifications(a, b)
    assert out.prefilter_only is True


# ── LangchainClassifierLLM ──────────────────────────────────────────────


def test_langchain_classifier_lazy_init(monkeypatch):
    """``_get_llm`` must not call ``get_cheapest_model`` until the
    first ``classify_text``."""
    calls = {"n": 0}

    def fake_get_cheapest_model():
        calls["n"] += 1
        # Return ``None`` so we hit the unavailable path; tests don't
        # need a real LLM.
        return None

    monkeypatch.setattr(
        "backend.agents.llm.get_cheapest_model",
        fake_get_cheapest_model,
    )

    inst = LangchainClassifierLLM()
    assert calls["n"] == 0  # construction did not trigger lookup
    with pytest.raises(ClassifierUnavailableError):
        asyncio.run(inst.classify_text("hi", system="sys"))
    assert calls["n"] == 1


def test_langchain_classifier_records_picked_model_name(monkeypatch):
    class _StubLLM:
        model_name = "claude-haiku-4-20250506"

        async def ainvoke(self, msgs):
            class _Msg:
                content = '{"risk_level":"low","categories":[]}'
            return _Msg()

    monkeypatch.setattr(
        "backend.agents.llm.get_cheapest_model",
        lambda: _StubLLM(),
    )

    inst = LangchainClassifierLLM()
    out = asyncio.run(inst.classify_text("hi", system="sys"))
    assert "low" in out
    # ``name`` updated to the picked model so the audit row records
    # the actual provider, not the chain alias.
    assert inst.name == "claude-haiku-4-20250506"


def test_langchain_classifier_handles_list_content_blocks(monkeypatch):
    """langchain-anthropic >= 0.3 returns a list of dict blocks for
    multi-modal responses; the classifier must concatenate text blocks.
    """
    class _StubLLM:
        model = "stub-model"

        async def ainvoke(self, msgs):
            class _Msg:
                content = [
                    {"text": '{"risk_level":"low",'},
                    {"text": '"categories":[]}'},
                ]
            return _Msg()

    monkeypatch.setattr(
        "backend.agents.llm.get_cheapest_model",
        lambda: _StubLLM(),
    )

    inst = LangchainClassifierLLM()
    out = asyncio.run(inst.classify_text("hi", system="sys"))
    # Multi-block content is concatenated with newlines; the parser
    # tolerates the embedded newline because JSON whitespace is
    # insignificant.
    parsed = _parse_llm_envelope(out)
    assert parsed == {"risk_level": "low", "categories": []}


# ── Package re-export surface ───────────────────────────────────────────


_W11_5_RE_EXPORTS = (
    "ClassifierLLM",
    "ClassifierUnavailableError",
    "ContentClassifierError",
    "ContentRiskError",
    "DEFAULT_CLASSIFIER_MODEL",
    "DEFAULT_REFUSAL_THRESHOLD",
    "LLM_SYSTEM_PROMPT",
    "LLM_USER_PROMPT_TEMPLATE",
    "LangchainClassifierLLM",
    "MAX_PROMPT_INPUT_CHARS",
    "MAX_REASONS",
    "MAX_REASON_CHARS",
    "RISK_CATEGORIES",
    "RISK_LEVELS",
    "RiskClassification",
    "RiskScore",
    "assert_clone_spec_safe",
    "classify_clone_spec",
    "heuristic_risk_signals",
    "merge_risk_classifications",
)


@pytest.mark.parametrize("symbol", _W11_5_RE_EXPORTS)
def test_w11_5_symbol_re_exported_from_backend_web(symbol):
    assert symbol in web_pkg.__all__
    assert hasattr(web_pkg, symbol)


def test_total_re_export_count_pinned_at_79():
    # Pin the count so a future row that adds a new symbol must update
    # this assertion deliberately (drift guard).
    # W11.5 originally pinned this at 79; W11.6 added 19 new symbols
    # (output_transformer surface) → 98; W11.7 added 29 new symbols
    # (clone_manifest surface) → 127; W11.8 added 19 new symbols
    # (clone_rate_limit surface) → 146; W11.9 added 23 new symbols
    # (framework_adapter surface) → 169; W11.10 added 12 new symbols
    # (clone_spec_context surface) → 181; W11.12 added 11 new symbols
    # (clone_audit surface) → 192. Each W11 row's own drift guard
    # re-pins at the new value.
    assert len(web_pkg.__all__) == 192


# ── Whole-spec invariants ───────────────────────────────────────────────


def test_classify_returns_risk_level_in_known_set():
    spec = _make_spec()
    fake = _FakeClassifierLLM()
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    assert rc.risk_level in RISK_LEVELS


def test_classify_returns_categories_in_known_set():
    spec = _make_spec()
    fake = _FakeClassifierLLM(responses=[
        '{"risk_level":"medium","categories":['
        '{"name":"brand_impersonation","level":"medium","reason":"r"},'
        '{"name":"clean","level":"low","reason":"r"}]}',
    ])
    rc = asyncio.run(classify_clone_spec(spec, llm=fake))
    for s in rc.scores:
        assert s.category in RISK_CATEGORIES
        assert s.level in RISK_LEVELS


def test_default_user_prompt_template_uses_named_format_placeholders():
    # The prompt template must support ``.format(prefilter_summary=...,
    # excerpt=...)`` — exercise the round trip.
    out = LLM_USER_PROMPT_TEMPLATE.format(
        prefilter_summary="adult=critical", excerpt="<page>",
    )
    assert "adult=critical" in out
    assert "<page>" in out


def test_default_system_prompt_includes_risk_level_words():
    for level in RISK_LEVELS:
        assert level in LLM_SYSTEM_PROMPT


def test_default_system_prompt_includes_each_category():
    for cat in RISK_CATEGORIES:
        assert cat in LLM_SYSTEM_PROMPT
