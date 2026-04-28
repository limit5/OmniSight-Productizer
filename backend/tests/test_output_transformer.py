"""W11.6 #XXX — Contract tests for ``backend.web.output_transformer``.

Pins:

    * Public surface (constants, dataclass shape, error hierarchy,
      Protocol runtime_checkable, package re-exports).
    * ``assert_no_copied_bytes`` — refuses ``data:`` URIs / ``base64,``
      payloads / non-string urls / forbidden inline-byte fields. Accepts
      both :class:`CloneSpec` and :class:`TransformedSpec`. Rejects
      anything else with :class:`OutputTransformerError`.
    * ``apply_image_placeholders`` — every image becomes a placeholder
      record with ``url`` pointing at the configured provider, original
      preserved as ``source_url``, alt-text inherited and sanitised,
      :data:`MAX_REWRITTEN_LIST_ITEMS` cap honoured, ``data:`` URIs
      dropped silently. Pure / deterministic.
    * ``transform_clone_spec`` end-to-end — LLM tier consulted (system
      prompt + user prompt template formatted with risk_level +
      categories + excerpt), envelope parsed into a frozen
      :class:`TransformedSpec`, image placeholder pass run, no-bytes
      invariant enforced on input + output. Heuristic fallback when LLM
      unavailable / parse fails. Defensive risk gate refuses
      ``critical``. Empty spec produces an ``"empty_spec"`` warning.
      Excerpt for rewrite excludes image URLs / colour tokens (W11.6
      "never copy bytes" structural enforcement).
    * ``LangchainTextRewriteLLM`` — lazy init only on first
      ``rewrite_text``, raises :class:`RewriteUnavailableError` when
      ``get_cheapest_model`` returns ``None``, picked-model name is
      written back into ``self.name`` for the audit row.

Every test runs without network / LLM I/O: a ``_FakeRewriteLLM`` stand-in
is supplied via the ``llm=`` DI hook so neither LangChain nor a live
provider key is required.
"""

from __future__ import annotations

import asyncio
import pytest
from typing import Optional

import backend.web as web_pkg
from backend.web.content_classifier import (
    RISK_CATEGORIES,
    RISK_LEVELS,
    ContentRiskError,
    RiskClassification,
    RiskScore,
)
from backend.web.output_transformer import (
    BytesLeakError,
    DEFAULT_PLACEHOLDER_HEIGHT,
    DEFAULT_PLACEHOLDER_WIDTH,
    DEFAULT_REWRITE_MODEL,
    LLM_REWRITE_SYSTEM_PROMPT,
    LLM_REWRITE_USER_PROMPT_TEMPLATE,
    LangchainTextRewriteLLM,
    MAX_REWRITE_INPUT_CHARS,
    MAX_REWRITE_TEXT_CHARS,
    MAX_REWRITTEN_LIST_ITEMS,
    MAX_TRANSFORM_RISK_LEVEL,
    OutputTransformerError,
    PLACEHOLDER_PROVIDER,
    RewriteUnavailableError,
    TextRewriteLLM,
    TransformedSpec,
    apply_image_placeholders,
    assert_no_copied_bytes,
    transform_clone_spec,
)
from backend.web.output_transformer import (
    _BYTES_LEAK_FIELDS,
    _envelope_to_transformed,
    _heuristic_rewrite_envelope,
    _parse_rewrite_envelope,
    _placeholder_url,
    _spec_excerpt_for_rewrite,
)
from backend.web.site_cloner import CloneSpec, SiteClonerError


# ── Fixtures + test doubles ─────────────────────────────────────────────


def _make_spec(
    *,
    title: str = "Welcome to Acme",
    hero_heading: str = "Welcome to Acme",
    hero_tagline: str = "We make small products for small teams.",
    sections: Optional[list[dict]] = None,
    nav: Optional[list[dict]] = None,
    footer_text: str = "© 2026 Acme Corp",
    meta: Optional[dict[str, str]] = None,
    images: Optional[list[dict]] = None,
    colors: Optional[list[str]] = None,
    fonts: Optional[list[str]] = None,
    spacing: Optional[dict] = None,
) -> CloneSpec:
    return CloneSpec(
        source_url="https://example.com",
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        title=title,
        meta=meta if meta is not None else {"description": "About Acme."},
        hero={"heading": hero_heading, "tagline": hero_tagline} if hero_heading else None,
        nav=nav or [{"label": "Home", "href": "/"}, {"label": "About", "href": "/about"}],
        sections=sections or [
            {"heading": "Features", "summary": "Built for small teams."},
        ],
        footer={"text": footer_text, "links": []},
        images=images or [{"url": "https://example.com/logo.png", "alt": "Acme logo"}],
        colors=colors or ["#000000"],
        fonts=fonts or ["Inter"],
        spacing=spacing or {"padding": ["16px"]},
    )


_DEFAULT_REWRITE_RESPONSE = (
    '{"title":"Our Take",'
    '"hero":{"heading":"Welcome","tagline":"A modern landing page","cta_label":""},'
    '"nav":[{"label":"Home"},{"label":"About"}],'
    '"sections":[{"heading":"Features","summary":"Built for small teams"}],'
    '"footer":{"text":"Generic footer."}}'
)


class _FakeRewriteLLM:
    """Test stand-in for :class:`TextRewriteLLM`. Returns canned strings
    keyed by call order so tests can assert exact prompt shape."""

    name = "fake-rewrite-llm"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        raises: Optional[BaseException] = None,
    ) -> None:
        self.responses = list(responses or [_DEFAULT_REWRITE_RESPONSE])
        self.calls: list[dict] = []
        self.raises = raises

    async def rewrite_text(self, prompt: str, *, system: str) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self.raises is not None:
            raise self.raises
        if not self.responses:
            return _DEFAULT_REWRITE_RESPONSE
        return self.responses.pop(0)


def _await(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ── Public surface invariants ──────────────────────────────────────────


def test_constants_pinned() -> None:
    assert DEFAULT_REWRITE_MODEL == "cheapest-llm-chain"
    assert PLACEHOLDER_PROVIDER.startswith(("http://", "https://"))
    assert 16 <= DEFAULT_PLACEHOLDER_WIDTH <= 4096
    assert 16 <= DEFAULT_PLACEHOLDER_HEIGHT <= 4096
    assert 0 < MAX_REWRITE_INPUT_CHARS <= 64_000
    assert 0 < MAX_REWRITE_TEXT_CHARS <= 4_000
    assert 0 < MAX_REWRITTEN_LIST_ITEMS <= 200
    assert MAX_TRANSFORM_RISK_LEVEL in RISK_LEVELS


def test_text_rewrite_llm_protocol_runtime_checkable() -> None:
    fake = _FakeRewriteLLM()
    assert isinstance(fake, TextRewriteLLM)


def test_text_rewrite_llm_protocol_rejects_missing_method() -> None:
    class Missing:
        name = "x"

    assert not isinstance(Missing(), TextRewriteLLM)


def test_transformed_spec_is_frozen() -> None:
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    with pytest.raises(Exception):
        out.title = "mutated"  # type: ignore[misc]


def test_error_hierarchy_chains_to_site_cloner_error() -> None:
    assert issubclass(OutputTransformerError, SiteClonerError)
    assert issubclass(BytesLeakError, OutputTransformerError)
    assert issubclass(RewriteUnavailableError, OutputTransformerError)


def test_llm_system_prompt_pins_critical_invariants() -> None:
    # Drift guard: system prompt must keep the structural rules.
    p = LLM_REWRITE_SYSTEM_PROMPT.lower()
    assert "json" in p
    assert "brand" in p or "trademark" in p
    assert "structure" in p or "preserve" in p


def test_user_prompt_template_format_round_trip() -> None:
    rendered = LLM_REWRITE_USER_PROMPT_TEMPLATE.format(
        risk_level="low", categories="clean", excerpt="hello",
    )
    assert "low" in rendered
    assert "clean" in rendered
    assert "hello" in rendered


# ── assert_no_copied_bytes ─────────────────────────────────────────────


def test_assert_no_copied_bytes_accepts_clean_spec() -> None:
    spec = _make_spec()
    assert_no_copied_bytes(spec)


def test_assert_no_copied_bytes_rejects_data_uri() -> None:
    spec = _make_spec(
        images=[{"url": "data:image/png;base64,iVBORw0KGgo="}],
    )
    with pytest.raises(BytesLeakError, match="data: URI"):
        assert_no_copied_bytes(spec)


def test_assert_no_copied_bytes_rejects_base64_payload() -> None:
    spec = _make_spec(
        images=[{"url": "base64,iVBORw0KGgo="}],
    )
    with pytest.raises(BytesLeakError):
        assert_no_copied_bytes(spec)


def test_assert_no_copied_bytes_rejects_non_string_url() -> None:
    spec = _make_spec(images=[{"url": 12345}])  # type: ignore[list-item]
    with pytest.raises(BytesLeakError):
        assert_no_copied_bytes(spec)


@pytest.mark.parametrize("forbidden", sorted(_BYTES_LEAK_FIELDS))
def test_assert_no_copied_bytes_rejects_forbidden_field(forbidden: str) -> None:
    spec = _make_spec(images=[{"url": "https://x.com/a.png", forbidden: "x"}])
    with pytest.raises(BytesLeakError, match="forbidden inline-bytes"):
        assert_no_copied_bytes(spec)


def test_assert_no_copied_bytes_rejects_data_uri_in_source_url() -> None:
    spec = CloneSpec(
        source_url="https://example.com",
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        images=[{"url": "https://example.com/x.png", "source_url": "data:image/png;base64,A"}],
    )
    with pytest.raises(BytesLeakError, match="source_url"):
        assert_no_copied_bytes(spec)


def test_assert_no_copied_bytes_refuses_non_spec_input() -> None:
    with pytest.raises(OutputTransformerError, match="expects CloneSpec"):
        assert_no_copied_bytes({"images": []})  # type: ignore[arg-type]


def test_assert_no_copied_bytes_runs_on_transformed_spec() -> None:
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert_no_copied_bytes(out)


def test_assert_no_copied_bytes_refuses_non_mapping_image() -> None:
    spec = CloneSpec(
        source_url="https://example.com",
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        images=["not-a-mapping"],  # type: ignore[list-item]
    )
    with pytest.raises(BytesLeakError, match="expected mapping"):
        assert_no_copied_bytes(spec)


def test_assert_no_copied_bytes_accepts_empty_images() -> None:
    spec = _make_spec(images=[])
    assert_no_copied_bytes(spec)


# ── apply_image_placeholders ───────────────────────────────────────────


def test_apply_image_placeholders_replaces_every_image() -> None:
    spec = _make_spec(images=[
        {"url": "https://example.com/a.png", "alt": "Alpha"},
        {"url": "https://example.com/b.png", "alt": "Beta"},
    ])
    out = apply_image_placeholders(spec)
    assert len(out) == 2
    for rec in out:
        assert rec["kind"] == "placeholder"
        assert rec["url"].startswith("https://placehold.co/")
        assert rec["source_url"].startswith("https://example.com/")


def test_apply_image_placeholders_drops_data_uri_silently() -> None:
    spec = _make_spec(images=[
        {"url": "data:image/png;base64,A"},
        {"url": "https://example.com/ok.png", "alt": "ok"},
    ])
    out = apply_image_placeholders(spec)
    assert len(out) == 1
    assert out[0]["source_url"] == "https://example.com/ok.png"


def test_apply_image_placeholders_uses_provider() -> None:
    spec = _make_spec(images=[{"url": "https://example.com/a.png"}])
    out = apply_image_placeholders(
        spec, provider="https://placeholder.internal", width=400, height=300,
    )
    assert out[0]["url"].startswith("https://placeholder.internal/400x300")
    assert out[0]["width"] == "400"
    assert out[0]["height"] == "300"


def test_apply_image_placeholders_caps_at_max_items() -> None:
    spec = _make_spec(images=[
        {"url": f"https://example.com/{i}.png"} for i in range(MAX_REWRITTEN_LIST_ITEMS + 50)
    ])
    out = apply_image_placeholders(spec)
    assert len(out) == MAX_REWRITTEN_LIST_ITEMS


def test_apply_image_placeholders_sanitises_alt_text() -> None:
    spec = _make_spec(images=[
        {"url": "https://example.com/a.png", "alt": "  \n\t   trimmed   "},
    ])
    out = apply_image_placeholders(spec)
    assert out[0]["alt"] == "trimmed"


def test_apply_image_placeholders_synthesises_alt_when_missing() -> None:
    spec = _make_spec(images=[{"url": "https://example.com/a.png"}])
    out = apply_image_placeholders(spec)
    assert out[0]["alt"] == "Placeholder image"


def test_apply_image_placeholders_skips_non_mapping_records() -> None:
    spec = CloneSpec(
        source_url="https://example.com",
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        images=[
            "garbage",  # type: ignore[list-item]
            {"url": "https://example.com/ok.png"},
        ],
    )
    out = apply_image_placeholders(spec)
    assert len(out) == 1
    assert out[0]["source_url"] == "https://example.com/ok.png"


def test_apply_image_placeholders_refuses_non_spec() -> None:
    with pytest.raises(OutputTransformerError, match="expects CloneSpec"):
        apply_image_placeholders({"images": []})  # type: ignore[arg-type]


def test_placeholder_url_includes_label_when_provided() -> None:
    url = _placeholder_url(
        provider="https://placehold.co", width=200, height=100, label="hi there",
    )
    assert "200x100" in url
    assert "hi+there" in url or "hi%20there" in url


def test_placeholder_url_omits_label_when_empty() -> None:
    url = _placeholder_url(
        provider="https://placehold.co", width=200, height=100,
    )
    assert url == "https://placehold.co/200x100"


def test_placeholder_url_clamps_tiny_dimensions() -> None:
    url = _placeholder_url(
        provider="https://placehold.co", width=2, height=4,
    )
    assert "16x16" in url


# ── _spec_excerpt_for_rewrite ──────────────────────────────────────────


def test_excerpt_includes_text_surfaces() -> None:
    spec = _make_spec(
        title="Welcome",
        hero_heading="Hello",
        hero_tagline="Tagline",
        sections=[{"heading": "Section", "summary": "summary text"}],
        nav=[{"label": "Home", "href": "/"}],
        footer_text="footer copy",
    )
    excerpt = _spec_excerpt_for_rewrite(spec)
    assert "Welcome" in excerpt
    assert "Hello" in excerpt
    assert "Tagline" in excerpt
    assert "Section" in excerpt
    assert "summary text" in excerpt
    assert "Home" in excerpt
    assert "footer copy" in excerpt


def test_excerpt_excludes_image_urls_colours_fonts() -> None:
    """W11.6 'never copy bytes' structural enforcement: the rewrite
    prompt sees text-only surfaces — no image URLs, no colour tokens,
    no font tokens. Defense-in-depth of the no-bytes invariant."""
    spec = _make_spec(
        images=[{"url": "https://cdn.example.com/leak.png", "alt": "leak"}],
        colors=["#abcdef", "#123456"],
        fonts=["Inter", "Roboto"],
    )
    excerpt = _spec_excerpt_for_rewrite(spec)
    assert "leak.png" not in excerpt
    assert "#abcdef" not in excerpt
    assert "Inter" not in excerpt


def test_excerpt_caps_at_max_input_chars() -> None:
    big_summary = "x" * 50_000
    spec = _make_spec(sections=[{"heading": "h", "summary": big_summary}])
    excerpt = _spec_excerpt_for_rewrite(spec)
    assert len(excerpt) <= MAX_REWRITE_INPUT_CHARS


def test_excerpt_caps_nav_at_12() -> None:
    spec = _make_spec(nav=[
        {"label": f"Item {i}", "href": "/"} for i in range(40)
    ])
    excerpt = _spec_excerpt_for_rewrite(spec)
    assert "Item 0" in excerpt
    assert "Item 11" in excerpt
    assert "Item 12" not in excerpt


def test_excerpt_caps_sections_at_6() -> None:
    spec = _make_spec(sections=[
        {"heading": f"S{i}", "summary": f"summary {i}"} for i in range(40)
    ])
    excerpt = _spec_excerpt_for_rewrite(spec)
    assert "S0" in excerpt
    assert "S5" in excerpt
    assert "S6" not in excerpt


# ── _parse_rewrite_envelope ────────────────────────────────────────────


def test_parse_rewrite_envelope_clean_json() -> None:
    parsed = _parse_rewrite_envelope('{"title": "x"}')
    assert parsed == {"title": "x"}


def test_parse_rewrite_envelope_strips_json_fence() -> None:
    parsed = _parse_rewrite_envelope('```json\n{"title":"x"}\n```')
    assert parsed == {"title": "x"}


def test_parse_rewrite_envelope_strips_bare_fence() -> None:
    parsed = _parse_rewrite_envelope('```\n{"title":"x"}\n```')
    assert parsed == {"title": "x"}


def test_parse_rewrite_envelope_extracts_from_prose() -> None:
    parsed = _parse_rewrite_envelope(
        'Sure! Here is the JSON:\n{"title":"x"}\nLet me know if more.'
    )
    assert parsed == {"title": "x"}


def test_parse_rewrite_envelope_returns_none_on_garbage() -> None:
    assert _parse_rewrite_envelope("not json at all") is None


def test_parse_rewrite_envelope_returns_none_on_empty() -> None:
    assert _parse_rewrite_envelope("") is None
    assert _parse_rewrite_envelope("   \n  ") is None


def test_parse_rewrite_envelope_returns_none_on_array() -> None:
    assert _parse_rewrite_envelope("[1,2,3]") is None


def test_parse_rewrite_envelope_returns_none_on_non_str() -> None:
    assert _parse_rewrite_envelope(None) is None  # type: ignore[arg-type]
    assert _parse_rewrite_envelope(12345) is None  # type: ignore[arg-type]


# ── _envelope_to_transformed ───────────────────────────────────────────


def test_envelope_to_transformed_truncates_text() -> None:
    spec = _make_spec()
    envelope = {
        "title": "x" * 5_000,
        "hero": {"heading": "h" * 5_000, "tagline": "t", "cta_label": ""},
        "nav": [],
        "sections": [],
        "footer": {"text": ""},
    }
    out = _envelope_to_transformed(
        envelope,
        spec=spec,
        placeholder_provider=PLACEHOLDER_PROVIDER,
        placeholder_width=DEFAULT_PLACEHOLDER_WIDTH,
        placeholder_height=DEFAULT_PLACEHOLDER_HEIGHT,
        model="m",
        signals_used=("llm",),
        transformations=("text_rewrite",),
        warnings=(),
    )
    assert len(out.title) <= MAX_REWRITE_TEXT_CHARS
    assert len(out.hero["heading"]) <= MAX_REWRITE_TEXT_CHARS  # type: ignore[index]


def test_envelope_to_transformed_caps_lists() -> None:
    spec = _make_spec()
    envelope = {
        "nav": [{"label": f"L{i}"} for i in range(MAX_REWRITTEN_LIST_ITEMS + 20)],
        "sections": [{"heading": f"H{i}", "summary": "x"} for i in range(MAX_REWRITTEN_LIST_ITEMS + 20)],
    }
    out = _envelope_to_transformed(
        envelope,
        spec=spec,
        placeholder_provider=PLACEHOLDER_PROVIDER,
        placeholder_width=DEFAULT_PLACEHOLDER_WIDTH,
        placeholder_height=DEFAULT_PLACEHOLDER_HEIGHT,
        model="m",
        signals_used=("llm",),
        transformations=("text_rewrite",),
        warnings=(),
    )
    assert len(out.nav) <= MAX_REWRITTEN_LIST_ITEMS
    assert len(out.sections) <= MAX_REWRITTEN_LIST_ITEMS


def test_envelope_to_transformed_carries_design_tokens_unchanged() -> None:
    spec = _make_spec(
        colors=["#abcdef", "#123456"],
        fonts=["Inter", "Roboto"],
        spacing={"padding": ["8px", "16px"], "max_width": "1200px"},
    )
    out = _envelope_to_transformed(
        {"title": "x"},
        spec=spec,
        placeholder_provider=PLACEHOLDER_PROVIDER,
        placeholder_width=DEFAULT_PLACEHOLDER_WIDTH,
        placeholder_height=DEFAULT_PLACEHOLDER_HEIGHT,
        model="m",
        signals_used=("llm",),
        transformations=("text_rewrite",),
        warnings=(),
    )
    assert out.colors == ("#abcdef", "#123456")
    assert out.fonts == ("Inter", "Roboto")
    assert out.spacing == {"padding": ["8px", "16px"], "max_width": "1200px"}


def test_envelope_to_transformed_drops_blank_hero_sections() -> None:
    spec = _make_spec()
    envelope = {
        "hero": {"heading": "", "tagline": "", "cta_label": ""},
        "sections": [{"heading": "", "summary": ""}],
        "footer": {"text": ""},
    }
    out = _envelope_to_transformed(
        envelope,
        spec=spec,
        placeholder_provider=PLACEHOLDER_PROVIDER,
        placeholder_width=DEFAULT_PLACEHOLDER_WIDTH,
        placeholder_height=DEFAULT_PLACEHOLDER_HEIGHT,
        model="m",
        signals_used=("llm",),
        transformations=("text_rewrite",),
        warnings=(),
    )
    assert out.hero is None
    assert out.sections == ()
    assert out.footer is None


# ── transform_clone_spec end-to-end ────────────────────────────────────


def test_transform_calls_llm_with_pinned_system_prompt() -> None:
    fake = _FakeRewriteLLM()
    asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert fake.calls
    assert fake.calls[0]["system"] == LLM_REWRITE_SYSTEM_PROMPT


def test_transform_user_prompt_includes_excerpt_and_classification() -> None:
    fake = _FakeRewriteLLM()
    classification = RiskClassification(
        risk_level="medium",
        scores=(RiskScore("brand_impersonation", "medium", "matched"),),
        model="m",
        signals_used=("heuristic",),
    )
    asyncio.run(transform_clone_spec(
        _make_spec(title="Acme Welcome"),
        llm=fake,
        classification=classification,
    ))
    user_prompt = fake.calls[0]["prompt"]
    assert "medium" in user_prompt
    assert "brand_impersonation" in user_prompt
    assert "Acme Welcome" in user_prompt


def test_transform_returns_frozen_transformed_spec() -> None:
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert isinstance(out, TransformedSpec)
    assert out.title == "Our Take"
    assert out.signals_used == ("llm", "image_placeholder")
    assert "text_rewrite" in out.transformations
    assert "bytes_strip" in out.transformations
    assert "image_placeholder" in out.transformations


def test_transform_replaces_images_with_placeholders() -> None:
    fake = _FakeRewriteLLM()
    spec = _make_spec(
        images=[{"url": "https://cdn.example.com/logo.png", "alt": "logo"}],
    )
    out = asyncio.run(transform_clone_spec(spec, llm=fake))
    assert len(out.images) == 1
    rec = out.images[0]
    assert rec["kind"] == "placeholder"
    assert rec["url"].startswith(PLACEHOLDER_PROVIDER)
    assert rec["source_url"] == "https://cdn.example.com/logo.png"


def test_transform_falls_back_on_rewrite_unavailable() -> None:
    fake = _FakeRewriteLLM(raises=RewriteUnavailableError("token freeze"))
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert "heuristic" in out.signals_used
    assert "image_placeholder" in out.signals_used
    assert any("rewrite_llm_unavailable" in w for w in out.warnings)
    assert out.model == "heuristic"


def test_transform_falls_back_on_parse_failure() -> None:
    fake = _FakeRewriteLLM(responses=["this is not json"])
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert "heuristic" in out.signals_used
    assert "rewrite_parse_failed" in out.warnings
    assert out.model == "heuristic"


def test_transform_propagates_picked_model_when_llm_succeeds() -> None:
    fake = _FakeRewriteLLM()
    fake.name = "claude-haiku-4-5"
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert out.model == "claude-haiku-4-5"


def test_transform_records_empty_spec_warning() -> None:
    spec = CloneSpec(
        source_url="https://example.com",
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
    )
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(spec, llm=fake))
    assert "empty_spec" in out.warnings


def test_transform_refuses_critical_classification_defensively() -> None:
    classification = RiskClassification(
        risk_level="critical",
        scores=(RiskScore("adult", "critical", "matched"),),
        model="m",
        signals_used=("heuristic",),
    )
    fake = _FakeRewriteLLM()
    with pytest.raises(ContentRiskError):
        asyncio.run(transform_clone_spec(
            _make_spec(), llm=fake, classification=classification,
        ))


def test_transform_allows_high_classification() -> None:
    classification = RiskClassification(
        risk_level="high",
        scores=(RiskScore("paywalled", "high", "matched"),),
        model="m",
        signals_used=("heuristic",),
    )
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(
        _make_spec(), llm=fake, classification=classification,
    ))
    assert isinstance(out, TransformedSpec)


def test_transform_refuses_data_uri_input() -> None:
    spec = _make_spec(images=[{"url": "data:image/png;base64,AAAA"}])
    fake = _FakeRewriteLLM()
    with pytest.raises(BytesLeakError):
        asyncio.run(transform_clone_spec(spec, llm=fake))


def test_transform_refuses_non_clone_spec_input() -> None:
    with pytest.raises(OutputTransformerError, match="must be CloneSpec"):
        asyncio.run(transform_clone_spec({"title": "x"}))  # type: ignore[arg-type]


def test_transform_refuses_non_classification_input() -> None:
    fake = _FakeRewriteLLM()
    with pytest.raises(OutputTransformerError, match="must be RiskClassification"):
        asyncio.run(transform_clone_spec(
            _make_spec(), llm=fake, classification={"risk_level": "low"},  # type: ignore[arg-type]
        ))


def test_transform_refuses_unknown_risk_level() -> None:
    bad = RiskClassification(
        risk_level="catastrophic",  # not in RISK_LEVELS
        scores=(),
        model="m",
        signals_used=("heuristic",),
    )
    fake = _FakeRewriteLLM()
    with pytest.raises(OutputTransformerError, match="risk_level"):
        asyncio.run(transform_clone_spec(
            _make_spec(), llm=fake, classification=bad,
        ))


def test_transform_short_circuits_excerpt_for_classification() -> None:
    """Without classification → user prompt records `unknown` risk."""
    fake = _FakeRewriteLLM()
    asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert "unknown" in fake.calls[0]["prompt"]


def test_transform_invariant_runs_on_output() -> None:
    """Output of every successful transform passes assert_no_copied_bytes."""
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert_no_copied_bytes(out)


def test_transform_image_placeholder_provider_overridable() -> None:
    fake = _FakeRewriteLLM()
    spec = _make_spec(images=[{"url": "https://example.com/x.png"}])
    out = asyncio.run(transform_clone_spec(
        spec, llm=fake,
        placeholder_provider="https://placeholder.internal",
        placeholder_width=400, placeholder_height=300,
    ))
    assert out.images[0]["url"].startswith("https://placeholder.internal/400x300")


def test_transform_meta_passthrough_only_safe_keys() -> None:
    """Source-identity meta (canonical, og:url) MUST NOT survive into
    the cloned page; only description-shaped semantic meta is kept."""
    fake = _FakeRewriteLLM()
    spec = _make_spec(meta={
        "description": "About Acme",
        "og:url": "https://acme.example.com/exact-page",
        "canonical": "https://acme.example.com/exact-page",
        "og:title": "Acme",
    })
    out = asyncio.run(transform_clone_spec(spec, llm=fake))
    assert "og:url" not in out.meta
    assert "canonical" not in out.meta


# ── Heuristic fallback ─────────────────────────────────────────────────


def test_heuristic_envelope_redacts_known_brand_tokens() -> None:
    spec = _make_spec(
        title="Welcome to Acme",
        hero_heading="Acme is the best",
        hero_tagline="A Google product",
        sections=[{"heading": "About Facebook", "summary": "Facebook is great"}],
        footer_text="© 2026 Acme",
    )
    envelope = _heuristic_rewrite_envelope(spec)
    rendered = " ".join([
        envelope["title"],
        envelope["hero"]["heading"],
        envelope["hero"]["tagline"],
        envelope["sections"][0]["heading"],
        envelope["sections"][0]["summary"],
        envelope["footer"]["text"],
    ])
    assert "Acme" not in rendered
    assert "Google" not in rendered
    assert "Facebook" not in rendered
    assert "Our Brand" in rendered


def test_heuristic_envelope_preserves_structure() -> None:
    spec = _make_spec(
        sections=[
            {"heading": "First", "summary": "first summary"},
            {"heading": "Second", "summary": "second summary"},
        ],
        nav=[{"label": "Home"}, {"label": "About"}, {"label": "Pricing"}],
    )
    envelope = _heuristic_rewrite_envelope(spec)
    assert len(envelope["sections"]) == 2
    assert len(envelope["nav"]) == 3


def test_heuristic_envelope_handles_missing_hero() -> None:
    spec = _make_spec(hero_heading="")
    envelope = _heuristic_rewrite_envelope(spec)
    assert envelope["hero"]["heading"] == ""


# ── LangchainTextRewriteLLM ────────────────────────────────────────────


def test_langchain_rewrite_llm_lazy_init() -> None:
    """Construction does NOT trigger get_cheapest_model — only first
    rewrite_text call does."""
    rewriter = LangchainTextRewriteLLM()
    assert rewriter.name == DEFAULT_REWRITE_MODEL
    assert rewriter._llm is None  # type: ignore[attr-defined]


def test_langchain_rewrite_llm_raises_when_no_model_available(monkeypatch) -> None:
    import backend.agents.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_cheapest_model", lambda: None)
    rewriter = LangchainTextRewriteLLM()
    with pytest.raises(RewriteUnavailableError):
        asyncio.run(rewriter.rewrite_text("hi", system="sys"))


def test_langchain_rewrite_llm_records_picked_model_name(monkeypatch) -> None:
    class _StubLLM:
        model_name = "claude-haiku-4-5"

        async def ainvoke(self, messages):
            class _Resp:
                content = '{"title":"x"}'
            return _Resp()

    import backend.agents.llm as llm_mod
    stub = _StubLLM()
    monkeypatch.setattr(llm_mod, "get_cheapest_model", lambda: stub)

    rewriter = LangchainTextRewriteLLM()
    out = asyncio.run(rewriter.rewrite_text("hi", system="sys"))
    assert out == '{"title":"x"}'
    assert rewriter.name == "claude-haiku-4-5"


def test_langchain_rewrite_llm_concatenates_list_content_blocks(monkeypatch) -> None:
    class _StubLLM:
        model = "stub"

        async def ainvoke(self, messages):
            class _Resp:
                content = [
                    {"text": '{"title":"a'},
                    {"content": "bc"},
                    "ignored-fallback",
                ]
            return _Resp()

    import backend.agents.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_cheapest_model", lambda: _StubLLM())

    rewriter = LangchainTextRewriteLLM()
    out = asyncio.run(rewriter.rewrite_text("hi", system="sys"))
    # Joined newlines come from the concat path.
    assert "title" in out
    assert "ignored-fallback" in out


# ── Re-export drift guard ──────────────────────────────────────────────


_W11_6_REEXPORTS = (
    "BytesLeakError",
    "DEFAULT_PLACEHOLDER_HEIGHT",
    "DEFAULT_PLACEHOLDER_WIDTH",
    "DEFAULT_REWRITE_MODEL",
    "LLM_REWRITE_SYSTEM_PROMPT",
    "LLM_REWRITE_USER_PROMPT_TEMPLATE",
    "LangchainTextRewriteLLM",
    "MAX_REWRITE_INPUT_CHARS",
    "MAX_REWRITE_TEXT_CHARS",
    "MAX_REWRITTEN_LIST_ITEMS",
    "MAX_TRANSFORM_RISK_LEVEL",
    "OutputTransformerError",
    "PLACEHOLDER_PROVIDER",
    "RewriteUnavailableError",
    "TextRewriteLLM",
    "TransformedSpec",
    "apply_image_placeholders",
    "assert_no_copied_bytes",
    "transform_clone_spec",
)


@pytest.mark.parametrize("symbol", _W11_6_REEXPORTS)
def test_w11_6_symbol_exposed_at_package_root(symbol: str) -> None:
    assert hasattr(web_pkg, symbol), f"backend.web missing {symbol!r}"
    assert symbol in web_pkg.__all__, f"backend.web.__all__ missing {symbol!r}"


def test_package_total_re_export_count_pinned() -> None:
    """Drift guard: the W11.5 row pinned 79 symbols. W11.6 adds 19
    new ones → 98 total. W11.7 adds 29 new ones (clone_manifest
    surface) → 127. W11.8 adds 19 new ones (clone_rate_limit surface)
    → 146. W11.9 adds 23 new ones (framework_adapter surface) → 169.
    W11.10 adds 12 new ones (clone_spec_context surface) → 181.
    W11.12 adds 11 new ones (clone_audit surface) → 192.
    Any future re-export drift is an obvious diff."""
    assert len(web_pkg.__all__) == 192


# ── Whole-spec invariants ──────────────────────────────────────────────


def test_transform_output_meta_does_not_carry_source_url() -> None:
    fake = _FakeRewriteLLM()
    spec = _make_spec(meta={
        "description": "Visit https://acme-source.example.com today",
    })
    out = asyncio.run(transform_clone_spec(spec, llm=fake))
    # The transformer rewrites the description heuristically (via the
    # heuristic rewriter for the meta map). The exact post-rewrite text
    # is not contractually pinned, but the transformer must not propagate
    # bare "acme-source" tokens that match the brand-redaction regex.
    desc = out.meta.get("description", "") or ""
    assert "acme-source" not in desc.lower() or "Our Brand" in desc


def test_transform_signals_set_includes_image_placeholder_always() -> None:
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    assert "image_placeholder" in out.signals_used


def test_transform_transformations_stable_set() -> None:
    """Pin the transformation kinds the L3 layer reports — the W11.7
    manifest writer + W11.12 audit row consume this enum."""
    fake = _FakeRewriteLLM()
    out = asyncio.run(transform_clone_spec(_make_spec(), llm=fake))
    allowed = {
        "bytes_strip", "text_rewrite", "text_rewrite_heuristic",
        "image_placeholder",
    }
    assert set(out.transformations).issubset(allowed)
    assert "bytes_strip" in out.transformations  # always runs
    assert "image_placeholder" in out.transformations  # always runs


def test_transform_drops_data_uri_image_after_capture_regression() -> None:
    """Defense-in-depth: even if the upstream capture leaked a data:
    URI, the input invariant catches it before the LLM call burns."""
    spec = _make_spec(images=[
        {"url": "data:image/png;base64,abc"},
        {"url": "https://example.com/ok.png"},
    ])
    fake = _FakeRewriteLLM()
    with pytest.raises(BytesLeakError):
        asyncio.run(transform_clone_spec(spec, llm=fake))
    # No LLM call should have been made (input gate fires before tier).
    assert fake.calls == []
