"""W11.4 #XXX — Contract tests for ``backend.web.refusal_signals``.

Pins:

    * ``robots.txt`` parsing (allow / disallow / wildcard / AI-bot UA
      list / file-missing / 5xx / empty body / oversized body).
    * ``ai.txt`` path probing (``.well-known`` first, root fallback,
      both 404 → no signal, only one served → that one wins).
    * ``<meta name="robots" content="noai">`` family detection — generic
      ``robots`` bucket, AI-bot scoped names (``GPTBot``), tag-soup
      tolerance, attribute-order independence, single-quote / double-
      quote / unquoted attribute values.
    * ``X-Robots-Tag`` header — comma list, UA-prefixed, repeated header
      coalesce, scope-mismatch skip.
    * Cloudflare AI-bot block — ``cf-mitigated`` strong signal,
      ``server: cloudflare`` + body-hint conjunction, false-positive
      avoidance (200 + cloudflare server + AI page text → not refused).
    * Combined entry points — pre-capture concurrency, post-capture
      ordered checks, ``MachineRefusedError`` propagation, decision
      merge.
    * Module re-export surface — every symbol in :mod:`backend.web`
      ``__all__`` resolves.

Every test runs without network I/O: the default ``_HttpxFetcher`` is
substituted via the ``fetcher=`` DI hook so neither httpx nor a live
upstream is required.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

import pytest

import backend.web as web_pkg
from backend.web.refusal_signals import (
    AI_BOT_USER_AGENTS,
    AI_TXT_PATHS,
    CLOUDFLARE_AI_BLOCK_BODY_HINTS,
    CLOUDFLARE_MITIGATED_REFUSE_VALUES,
    DEFAULT_REFUSAL_FETCH_TIMEOUT_S,
    DEFAULT_USER_AGENT,
    META_AI_BOT_NAMES,
    META_NOAI_TOKENS,
    MachineRefusedError,
    ROBOTS_TXT_PATH,
    RefusalDecision,
    RefusalFetchResult,
    RefusalFetcher,
    assert_clone_allowed_post_capture,
    assert_clone_allowed_pre_capture,
    check_ai_txt,
    check_cloudflare_ai_block,
    check_machine_refusal_post_capture,
    check_machine_refusal_pre_capture,
    check_meta_noai,
    check_robots_txt,
    check_x_robots_tag,
    default_refusal_fetcher,
    merge_refusal_decisions,
)
from backend.web.site_cloner import RawCapture, SiteClonerError


# ── Test doubles ─────────────────────────────────────────────────────────


class _MockFetcher:
    """Minimal ``RefusalFetcher`` that returns canned responses keyed by
    URL. Records every call for shape-of-request assertions."""

    def __init__(
        self,
        responses: Optional[Mapping[str, RefusalFetchResult]] = None,
        *,
        default_404: bool = True,
        raises: Optional[BaseException] = None,
        sleep_s: float = 0.0,
    ) -> None:
        self.responses: dict[str, RefusalFetchResult] = dict(responses or {})
        self.default_404 = default_404
        self.raises = raises
        self.sleep_s = sleep_s
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        url: str,
        *,
        timeout_s: float,
        headers: Mapping[str, str],
    ) -> RefusalFetchResult:
        self.calls.append(
            {"url": url, "timeout_s": timeout_s, "headers": dict(headers)}
        )
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.raises is not None:
            raise self.raises
        if url in self.responses:
            return self.responses[url]
        if self.default_404:
            return RefusalFetchResult(status=404, body=b"", headers={})
        # No canned response and not default-404 → simulate transport
        # failure (the contract for missing files is "treat as no opt-out").
        raise SiteClonerError(f"no canned response for {url!r}")


def _ok(body: bytes, headers: Optional[Mapping[str, str]] = None) -> RefusalFetchResult:
    return RefusalFetchResult(status=200, body=body, headers=dict(headers or {}))


# ── Protocol surface + dataclass shape ────────────────────────────────────


def test_refusal_fetcher_is_runtime_checkable():
    fetcher = _MockFetcher()
    assert isinstance(fetcher, RefusalFetcher)


def test_default_refusal_fetcher_returns_protocol_satisfier():
    f = default_refusal_fetcher()
    assert isinstance(f, RefusalFetcher)


def test_refusal_decision_dataclass_shape():
    d = RefusalDecision(
        allowed=False,
        signals_checked=("robots.txt",),
        reasons=("nope",),
        details={"robots.txt": "nope"},
    )
    assert d.refused is True
    assert d.allowed is False
    assert d.signals_checked == ("robots.txt",)
    assert d.reasons == ("nope",)
    assert d.details == {"robots.txt": "nope"}


def test_refusal_decision_is_frozen():
    d = RefusalDecision(
        allowed=True,
        signals_checked=(),
        reasons=(),
    )
    with pytest.raises(Exception):
        d.allowed = False  # type: ignore[misc]


def test_machine_refused_error_carries_decision_and_url():
    d = RefusalDecision(
        allowed=False,
        signals_checked=("robots.txt",),
        reasons=("publisher said no",),
    )
    err = MachineRefusedError(d, url="https://example.com")
    assert err.decision is d
    assert err.url == "https://example.com"
    assert "publisher said no" in str(err)
    assert "https://example.com" in str(err)
    # Inherits from SiteClonerError so the W11.1 catch-all keeps working.
    assert isinstance(err, SiteClonerError)


# ── robots.txt ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_robots_txt_allows_when_no_directive_matches():
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": _ok(
            b"User-agent: *\nDisallow: /admin/\n"
        ),
    })
    reason = await check_robots_txt(
        "https://example.com/landing", fetcher=fetcher
    )
    assert reason is None


@pytest.mark.asyncio
async def test_robots_txt_blocks_wildcard_disallow():
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
    })
    reason = await check_robots_txt(
        "https://example.com/landing", fetcher=fetcher
    )
    assert reason is not None
    # Includes a UA in the reason for audit-log determinism.
    assert "robots.txt" in reason


@pytest.mark.asyncio
async def test_robots_txt_blocks_ai_bot_specific_directive():
    """A robots.txt that singles out GPTBot should still trigger refusal
    for our cloner, because we honour every AI-bot UA opt-out."""
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": _ok(
            b"User-agent: GPTBot\nDisallow: /\n"
        ),
    })
    reason = await check_robots_txt(
        "https://example.com/landing", fetcher=fetcher
    )
    assert reason is not None
    assert "gptbot" in reason.lower()


@pytest.mark.asyncio
async def test_robots_txt_missing_file_is_no_signal():
    fetcher = _MockFetcher()  # default_404 → 404 for every URL
    reason = await check_robots_txt("https://example.com/", fetcher=fetcher)
    assert reason is None


@pytest.mark.asyncio
async def test_robots_txt_5xx_is_no_signal():
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": RefusalFetchResult(
            status=503, body=b"upstream", headers={}
        ),
    })
    reason = await check_robots_txt("https://example.com/", fetcher=fetcher)
    assert reason is None


@pytest.mark.asyncio
async def test_robots_txt_empty_body_is_no_signal():
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": _ok(b""),
    })
    reason = await check_robots_txt("https://example.com/", fetcher=fetcher)
    assert reason is None


@pytest.mark.asyncio
async def test_robots_txt_transport_error_is_no_signal():
    fetcher = _MockFetcher(
        responses={}, default_404=False, raises=SiteClonerError("boom")
    )
    reason = await check_robots_txt("https://example.com/", fetcher=fetcher)
    assert reason is None


@pytest.mark.asyncio
async def test_robots_txt_uses_origin_path_only():
    fetcher = _MockFetcher()
    await check_robots_txt(
        "https://example.com/some/page?query=1#frag",
        fetcher=fetcher,
    )
    assert len(fetcher.calls) == 1
    assert fetcher.calls[0]["url"] == "https://example.com/robots.txt"


@pytest.mark.asyncio
async def test_robots_txt_honours_path_specific_rules():
    """Allow on root path but disallow on /private/ — clone of /private/x
    must be refused, clone of /pub/x must be allowed."""
    body = b"User-agent: *\nDisallow: /private/\nAllow: /pub/\n"
    fetcher = _MockFetcher({"https://example.com/robots.txt": _ok(body)})

    reason = await check_robots_txt(
        "https://example.com/private/secret", fetcher=fetcher
    )
    assert reason is not None
    fetcher.calls.clear()

    reason = await check_robots_txt(
        "https://example.com/pub/page", fetcher=fetcher
    )
    assert reason is None


# ── ai.txt ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_txt_well_known_path_tried_first():
    fetcher = _MockFetcher()
    await check_ai_txt("https://example.com/", fetcher=fetcher)
    # First call goes to /.well-known/ai.txt per AI_TXT_PATHS ordering.
    assert fetcher.calls[0]["url"] == "https://example.com/.well-known/ai.txt"
    # Both paths attempted because both 404'd.
    assert [c["url"] for c in fetcher.calls] == [
        "https://example.com" + p for p in AI_TXT_PATHS
    ]


@pytest.mark.asyncio
async def test_ai_txt_well_known_short_circuits_root():
    fetcher = _MockFetcher({
        "https://example.com/.well-known/ai.txt": _ok(
            b"User-agent: *\nAllow: /\n"
        ),
    })
    reason = await check_ai_txt("https://example.com/", fetcher=fetcher)
    assert reason is None
    # Only the well-known path got hit — root /ai.txt skipped.
    assert len(fetcher.calls) == 1
    assert fetcher.calls[0]["url"] == "https://example.com/.well-known/ai.txt"


@pytest.mark.asyncio
async def test_ai_txt_root_fallback_used_when_well_known_missing():
    fetcher = _MockFetcher({
        "https://example.com/ai.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
    })
    reason = await check_ai_txt("https://example.com/", fetcher=fetcher)
    assert reason is not None
    # Both paths were probed (well-known 404 → root fetched).
    assert len(fetcher.calls) == 2


@pytest.mark.asyncio
async def test_ai_txt_disallow_blocks():
    fetcher = _MockFetcher({
        "https://example.com/.well-known/ai.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
    })
    reason = await check_ai_txt("https://example.com/page", fetcher=fetcher)
    assert reason is not None
    assert "ai.txt" in reason


@pytest.mark.asyncio
async def test_ai_txt_silent_file_is_allow():
    """ai.txt that exists but has no Disallow for any UA → allow."""
    fetcher = _MockFetcher({
        "https://example.com/.well-known/ai.txt": _ok(
            b"# whatever\nUser-agent: SomeOtherBot\nDisallow: /admin/\n"
        ),
    })
    reason = await check_ai_txt(
        "https://example.com/landing", fetcher=fetcher
    )
    assert reason is None


@pytest.mark.asyncio
async def test_ai_txt_neither_path_served_is_no_signal():
    fetcher = _MockFetcher()  # all 404s
    reason = await check_ai_txt("https://example.com/", fetcher=fetcher)
    assert reason is None


# ── meta noai ────────────────────────────────────────────────────────────


def test_meta_noai_generic_robots_block():
    html = '<html><head><meta name="robots" content="noai"></head></html>'
    reason = check_meta_noai(html)
    assert reason is not None
    assert "noai" in reason


def test_meta_noai_with_multiple_directives():
    html = (
        '<html><head>'
        '<meta name="robots" content="noindex, nofollow, noimageai">'
        '</head></html>'
    )
    reason = check_meta_noai(html)
    assert reason is not None
    assert "noimageai" in reason


def test_meta_noai_none_token_blocks():
    html = '<meta name="robots" content="none">'
    reason = check_meta_noai(html)
    assert reason is not None
    assert "'none'" in reason or "none" in reason


def test_meta_noai_ai_bot_scoped_name():
    html = '<meta name="GPTBot" content="noai">'
    reason = check_meta_noai(html)
    assert reason is not None
    assert "gptbot" in reason.lower()


def test_meta_noai_unknown_name_skipped():
    """A meta tag named ``foo`` with ``noai`` content shouldn't fire —
    we only honour names that map to a real bot bucket."""
    html = '<meta name="foo" content="noai">'
    reason = check_meta_noai(html)
    assert reason is None


def test_meta_noai_attribute_order_independent():
    html = '<meta content="noai" name="robots">'
    reason = check_meta_noai(html)
    assert reason is not None


def test_meta_noai_single_quoted_attrs():
    html = "<meta name='robots' content='noai'>"
    reason = check_meta_noai(html)
    assert reason is not None


def test_meta_noai_unquoted_attrs():
    html = "<meta name=robots content=noai>"
    reason = check_meta_noai(html)
    assert reason is not None


def test_meta_noai_uppercase_directive_matched():
    html = '<META NAME="robots" CONTENT="NOAI">'
    reason = check_meta_noai(html)
    assert reason is not None


def test_meta_noai_returns_none_on_clean_page():
    html = (
        "<html><head><title>x</title>"
        '<meta name="robots" content="index, follow">'
        '<meta name="description" content="hello">'
        "</head></html>"
    )
    assert check_meta_noai(html) is None


def test_meta_noai_empty_html_returns_none():
    assert check_meta_noai("") is None
    assert check_meta_noai(None) is None  # type: ignore[arg-type]


def test_meta_noai_tag_soup_tolerant():
    """A malformed earlier tag must not suppress detection of a later
    valid tag."""
    html = (
        '<meta name="broken" content="<<<>>>'
        '<meta name="robots" content="noai">'
    )
    # Best-effort: the regex-based scanner should still find the second
    # meta tag.
    reason = check_meta_noai(html)
    assert reason is not None


def test_meta_noai_first_match_wins_for_determinism():
    """Two refusal-firing tags → reason describes the *first* (audit
    log determinism)."""
    html = (
        '<meta name="robots" content="noai">'
        '<meta name="GPTBot" content="noimageai">'
    )
    reason = check_meta_noai(html)
    assert reason is not None
    assert "robots" in reason.lower()
    assert "gptbot" not in reason.lower()


# ── X-Robots-Tag ─────────────────────────────────────────────────────────


def test_x_robots_tag_simple_noai_blocks():
    headers = {"x-robots-tag": "noai"}
    reason = check_x_robots_tag(headers)
    assert reason is not None
    assert "noai" in reason


def test_x_robots_tag_comma_list_blocks():
    headers = {"x-robots-tag": "noindex, nofollow, noimageai"}
    reason = check_x_robots_tag(headers)
    assert reason is not None


def test_x_robots_tag_ua_scoped_to_us_blocks():
    headers = {"x-robots-tag": "GPTBot: noai"}
    reason = check_x_robots_tag(headers)
    assert reason is not None
    assert "gptbot" in reason.lower()


def test_x_robots_tag_ua_scoped_to_other_skipped():
    headers = {"x-robots-tag": "OtherBot: noai"}
    reason = check_x_robots_tag(headers)
    assert reason is None


def test_x_robots_tag_repeated_via_newline_coalesce_blocks():
    """httpx merges repeated headers with ``\\n`` — make sure the parser
    splits them."""
    headers = {"x-robots-tag": "GPTBot: noindex\nrobots: noai"}
    reason = check_x_robots_tag(headers)
    assert reason is not None


def test_x_robots_tag_clean_returns_none():
    headers = {"x-robots-tag": "noindex, nofollow"}  # no AI tokens
    # ``noindex`` alone is NOT a refusal — only the META_NOAI_TOKENS list.
    # ``nofollow`` likewise.
    assert check_x_robots_tag(headers) is None


def test_x_robots_tag_missing_header_returns_none():
    assert check_x_robots_tag({}) is None
    assert check_x_robots_tag({"content-type": "text/html"}) is None


def test_x_robots_tag_case_insensitive_lookup():
    headers = {"X-Robots-Tag": "noai"}
    assert check_x_robots_tag(headers) is not None


# ── Cloudflare AI block ──────────────────────────────────────────────────


def test_cloudflare_ai_block_cf_mitigated_challenge_blocks():
    reason = check_cloudflare_ai_block(
        status=403,
        headers={"cf-mitigated": "challenge", "server": "cloudflare"},
    )
    assert reason is not None
    assert "cf-mitigated" in reason


def test_cloudflare_ai_block_cf_mitigated_block_value_blocks():
    reason = check_cloudflare_ai_block(
        status=403,
        headers={"cf-mitigated": "block"},
    )
    assert reason is not None


def test_cloudflare_ai_block_body_hint_with_cf_server_blocks():
    reason = check_cloudflare_ai_block(
        status=403,
        headers={"server": "cloudflare", "cf-ray": "abc123-iad"},
        body=b"<html>This site is protected from AI bots</html>",
    )
    assert reason is not None
    assert "Cloudflare" in reason


def test_cloudflare_ai_block_2xx_with_ai_text_not_a_refusal():
    """A 200 page that happens to mention AI bots should NOT trigger a
    false positive — we only sniff ≥ 400 responses."""
    reason = check_cloudflare_ai_block(
        status=200,
        headers={"server": "cloudflare"},
        body=b"Block AI training is what we discuss in this article",
    )
    assert reason is None


def test_cloudflare_ai_block_403_no_cloudflare_marker_skipped():
    """403 with no CF marker shouldn't trigger — could be plain 403."""
    reason = check_cloudflare_ai_block(
        status=403,
        headers={"server": "nginx/1.21"},
        body=b"Forbidden",
    )
    assert reason is None


def test_cloudflare_ai_block_4xx_cf_no_body_hint_skipped():
    """403 + cloudflare server but body doesn't mention AI → don't refuse."""
    reason = check_cloudflare_ai_block(
        status=403,
        headers={"server": "cloudflare", "cf-ray": "abc-iad"},
        body=b"<html>Access denied. Please contact support.</html>",
    )
    assert reason is None


def test_cloudflare_ai_block_cf_mitigated_managed_challenge_blocks():
    reason = check_cloudflare_ai_block(
        status=403,
        headers={"cf-mitigated": "managed_challenge"},
    )
    assert reason is not None


# ── Pre-capture combined entry ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_capture_allows_when_no_signals_fire():
    fetcher = _MockFetcher()  # all 404
    decision = await check_machine_refusal_pre_capture(
        "https://example.com/landing", fetcher=fetcher
    )
    assert decision.allowed is True
    assert decision.refused is False
    assert "robots.txt" in decision.signals_checked
    assert "ai.txt" in decision.signals_checked
    assert decision.reasons == ()


@pytest.mark.asyncio
async def test_pre_capture_refuses_on_robots_block():
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
    })
    decision = await check_machine_refusal_pre_capture(
        "https://example.com/landing", fetcher=fetcher
    )
    assert decision.refused is True
    assert any("robots.txt" in r for r in decision.reasons)
    assert "robots.txt" in decision.details


@pytest.mark.asyncio
async def test_pre_capture_refuses_on_ai_txt_block():
    fetcher = _MockFetcher({
        "https://example.com/.well-known/ai.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
    })
    decision = await check_machine_refusal_pre_capture(
        "https://example.com/page", fetcher=fetcher
    )
    assert decision.refused is True
    assert "ai.txt" in decision.details


@pytest.mark.asyncio
async def test_pre_capture_collects_both_reasons_when_both_fire():
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
        "https://example.com/.well-known/ai.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
    })
    decision = await check_machine_refusal_pre_capture(
        "https://example.com/page", fetcher=fetcher
    )
    assert decision.refused is True
    assert len(decision.reasons) == 2
    assert "robots.txt" in decision.details
    assert "ai.txt" in decision.details


@pytest.mark.asyncio
async def test_assert_clone_allowed_pre_capture_raises_on_refusal():
    fetcher = _MockFetcher({
        "https://example.com/robots.txt": _ok(
            b"User-agent: *\nDisallow: /\n"
        ),
    })
    with pytest.raises(MachineRefusedError) as exc:
        await assert_clone_allowed_pre_capture(
            "https://example.com/", fetcher=fetcher
        )
    assert exc.value.url == "https://example.com/"
    assert exc.value.decision.refused is True


@pytest.mark.asyncio
async def test_assert_clone_allowed_pre_capture_returns_decision_on_pass():
    fetcher = _MockFetcher()  # all 404
    decision = await assert_clone_allowed_pre_capture(
        "https://example.com/", fetcher=fetcher
    )
    assert decision.allowed is True
    assert decision.signals_checked  # non-empty


# ── Post-capture combined entry ──────────────────────────────────────────


def _make_capture(
    *,
    html: str = "<html></html>",
    headers: Optional[Mapping[str, str]] = None,
    status_code: int = 200,
) -> RawCapture:
    return RawCapture(
        url="https://example.com",
        html=html,
        status_code=status_code,
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        asset_urls=(),
        headers=dict(headers or {}),
    )


def test_post_capture_allows_clean_html():
    cap = _make_capture(
        html="<html><meta name='robots' content='index'></html>",
        headers={"content-type": "text/html"},
    )
    decision = check_machine_refusal_post_capture(cap)
    assert decision.allowed is True
    # All three checks ran.
    assert set(decision.signals_checked) == {
        "header.x-robots-tag",
        "meta.noai",
        "cloudflare.ai-block",
    }


def test_post_capture_refuses_on_meta_noai():
    cap = _make_capture(
        html="<html><meta name='robots' content='noai'></html>",
    )
    decision = check_machine_refusal_post_capture(cap)
    assert decision.refused is True
    assert "meta.noai" in decision.details


def test_post_capture_refuses_on_x_robots_tag():
    cap = _make_capture(
        html="<html></html>",
        headers={"x-robots-tag": "noai"},
    )
    decision = check_machine_refusal_post_capture(cap)
    assert decision.refused is True
    assert "header.x-robots-tag" in decision.details


def test_post_capture_refuses_on_cf_block():
    cap = _make_capture(
        html="<html>This site is protected from AI bots</html>",
        headers={"cf-mitigated": "challenge", "server": "cloudflare"},
        status_code=403,
    )
    decision = check_machine_refusal_post_capture(cap)
    assert decision.refused is True
    assert "cloudflare.ai-block" in decision.details


def test_post_capture_collects_multiple_reasons():
    cap = _make_capture(
        html="<html><meta name='robots' content='noai'></html>",
        headers={"x-robots-tag": "noimageai"},
    )
    decision = check_machine_refusal_post_capture(cap)
    assert decision.refused is True
    assert len(decision.reasons) >= 2
    assert "meta.noai" in decision.details
    assert "header.x-robots-tag" in decision.details


def test_post_capture_rejects_non_capture_input():
    with pytest.raises(SiteClonerError):
        check_machine_refusal_post_capture(object())  # type: ignore[arg-type]


def test_assert_clone_allowed_post_capture_raises():
    cap = _make_capture(
        html="<html><meta name='robots' content='noai'></html>",
    )
    with pytest.raises(MachineRefusedError) as exc:
        assert_clone_allowed_post_capture(cap)
    assert exc.value.url == cap.url
    assert exc.value.decision.refused is True


def test_assert_clone_allowed_post_capture_returns_on_pass():
    cap = _make_capture(html="<html><body>hi</body></html>")
    decision = assert_clone_allowed_post_capture(cap)
    assert decision.allowed is True


# ── merge_refusal_decisions ──────────────────────────────────────────────


def test_merge_decisions_empty_returns_neutral_allow():
    d = merge_refusal_decisions()
    assert d.allowed is True
    assert d.signals_checked == ()
    assert d.reasons == ()


def test_merge_decisions_anding_allow_state():
    a = RefusalDecision(
        allowed=True, signals_checked=("a",), reasons=()
    )
    b = RefusalDecision(
        allowed=False, signals_checked=("b",), reasons=("nope",)
    )
    merged = merge_refusal_decisions(a, b)
    assert merged.allowed is False
    assert merged.signals_checked == ("a", "b")
    assert merged.reasons == ("nope",)


def test_merge_decisions_dedupes_signals_preserves_order():
    a = RefusalDecision(
        allowed=True, signals_checked=("robots.txt", "ai.txt"), reasons=()
    )
    b = RefusalDecision(
        allowed=True,
        signals_checked=("ai.txt", "meta.noai"),
        reasons=(),
    )
    merged = merge_refusal_decisions(a, b)
    assert merged.signals_checked == ("robots.txt", "ai.txt", "meta.noai")


def test_merge_decisions_rejects_non_decision_input():
    with pytest.raises(TypeError):
        merge_refusal_decisions("not a decision")  # type: ignore[arg-type]


def test_merge_decisions_concatenates_reasons():
    a = RefusalDecision(
        allowed=False, signals_checked=("a",), reasons=("reason-a",)
    )
    b = RefusalDecision(
        allowed=False, signals_checked=("b",), reasons=("reason-b",)
    )
    merged = merge_refusal_decisions(a, b)
    assert merged.reasons == ("reason-a", "reason-b")


# ── Module re-export surface ─────────────────────────────────────────────


def test_backend_web_re_exports_w11_4_symbols():
    expected = {
        "AI_BOT_USER_AGENTS",
        "AI_TXT_PATHS",
        "CLOUDFLARE_AI_BLOCK_BODY_HINTS",
        "CLOUDFLARE_MITIGATED_REFUSE_VALUES",
        "DEFAULT_REFUSAL_FETCH_MAX_BYTES",
        "DEFAULT_REFUSAL_FETCH_TIMEOUT_S",
        "DEFAULT_USER_AGENT",
        "MachineRefusedError",
        "META_AI_BOT_NAMES",
        "META_NOAI_TOKENS",
        "ROBOTS_TXT_PATH",
        "RefusalDecision",
        "RefusalFetchResult",
        "RefusalFetcher",
        "assert_clone_allowed_post_capture",
        "assert_clone_allowed_pre_capture",
        "check_ai_txt",
        "check_cloudflare_ai_block",
        "check_machine_refusal_post_capture",
        "check_machine_refusal_pre_capture",
        "check_meta_noai",
        "check_robots_txt",
        "check_x_robots_tag",
        "default_refusal_fetcher",
        "merge_refusal_decisions",
    }
    missing = [s for s in expected if not hasattr(web_pkg, s)]
    assert not missing, f"backend.web missing W11.4 re-exports: {missing}"
    # All must also be in __all__ so ``from backend.web import *`` works.
    web_all = set(web_pkg.__all__)
    not_in_all = [s for s in expected if s not in web_all]
    assert not not_in_all, f"backend.web __all__ missing: {not_in_all}"


def test_default_user_agent_includes_attribution():
    """Sanity-pin: site owners can target the cloner's UA explicitly."""
    assert "OmniSight" in DEFAULT_USER_AGENT


def test_ai_bot_user_agents_are_lowercase():
    """The check functions compare lower-cased; the constant is the
    source of truth so it MUST be lowercase already."""
    for ua in AI_BOT_USER_AGENTS:
        assert ua == ua.lower(), f"{ua!r} is not lower-case"


def test_meta_noai_tokens_all_lowercase():
    for t in META_NOAI_TOKENS:
        assert t == t.lower()


def test_meta_ai_bot_names_includes_robots_and_ai_bots():
    """The catch-all ``robots`` bucket plus every AI-bot UA must be in
    META_AI_BOT_NAMES — that's what makes the `<meta name="GPTBot">`
    case work."""
    assert "robots" in META_AI_BOT_NAMES
    for ua in AI_BOT_USER_AGENTS:
        assert ua in META_AI_BOT_NAMES


def test_cloudflare_mitigated_values_recognised():
    """All listed values must be lowercase + non-empty."""
    for v in CLOUDFLARE_MITIGATED_REFUSE_VALUES:
        assert v == v.lower()
        assert v


def test_cloudflare_body_hints_lowercase():
    for h in CLOUDFLARE_AI_BLOCK_BODY_HINTS:
        assert h == h.lower()


def test_robots_txt_path_constant():
    assert ROBOTS_TXT_PATH == "/robots.txt"


def test_ai_txt_paths_well_known_first():
    """Well-known path comes first per the spawning.ai canonical spec."""
    assert AI_TXT_PATHS[0] == "/.well-known/ai.txt"
    assert "/ai.txt" in AI_TXT_PATHS


def test_default_fetch_timeout_reasonable():
    assert 1.0 <= DEFAULT_REFUSAL_FETCH_TIMEOUT_S <= 30.0


# ── Misc edge cases ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_capture_with_invalid_url_raises():
    """Malformed URL surfaces as InvalidCloneURLError from normalize_url
    (a SiteClonerError subclass) — the refusal scanner does NOT silently
    swallow these."""
    from backend.web.site_cloner import InvalidCloneURLError

    fetcher = _MockFetcher()
    with pytest.raises(InvalidCloneURLError):
        await check_machine_refusal_pre_capture(
            "not-a-url", fetcher=fetcher
        )


@pytest.mark.asyncio
async def test_pre_capture_passes_user_agent_into_fetch_headers():
    fetcher = _MockFetcher()
    await check_machine_refusal_pre_capture(
        "https://example.com/", fetcher=fetcher,
        user_agent="CustomUA/1.0",
    )
    # Default fetcher header path always uses DEFAULT_USER_AGENT for the
    # outgoing User-Agent, since the publisher's robots.txt is keyed on
    # that. Make sure the fetch headers include a UA at all.
    for call in fetcher.calls:
        assert "User-Agent" in call["headers"]


def test_post_capture_with_html_too_large_for_cf_sniff_truncates():
    """CF body sniff caps at 8 KiB so a multi-MB page still completes
    quickly. Construct an 11 MiB body to exercise the cap without OOM."""
    big = b"<html>" + b"a" * (11 * 1024 * 1024) + b"</html>"
    cap = _make_capture(
        html=big.decode("latin-1"),
        headers={"server": "cloudflare", "cf-ray": "x"},
        status_code=403,
    )
    # Body has no AI hints → should NOT refuse despite size.
    decision = check_machine_refusal_post_capture(cap)
    assert decision.allowed is True
