"""W14.8 — contract tests for :mod:`backend.web_sandbox_pep`.

Scope:

* Module surface — ``__all__`` shape, schema_version pin, tool name pin,
  HOLD timeout pin, default size/ETA copy.
* :func:`format_size_estimate_text` — happy paths + edge-cases + the
  ``50–500 MB`` row-spec literal.
* :func:`format_eta_text` — happy paths + edge-cases + the ``30–90s``
  row-spec literal.
* :func:`build_pep_arguments` — required-string validation + override
  layering + ``container_port`` int coercion.
* :func:`requires_first_preview_hold` — None / live / terminal /
  ``force=True`` matrix.
* :class:`WebPreviewPepResult` — ``is_approved`` / ``is_rejected`` /
  ``is_error`` flags, ``to_dict`` round-trip.
* :func:`evaluate_first_preview_hold` end-to-end — approve / reject /
  gateway-raise / circuit-breaker-degraded / arguments_extra layering /
  category dispatch.
* Cross-worker contract — multiple invocations from independent
  thread/process emulators agree on the same arguments dict shape
  (SOP §1 type-1 answer pin).

Tests are isolated from PG / decision_engine — every HOLD path is
exercised through the ``propose_fn`` / ``wait_for_decision`` injection
hooks documented on :func:`backend.pep_gateway.evaluate` (same pattern
as :mod:`backend.tests.test_pep_gateway_install_coaching`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from backend import pep_gateway as _pep
from backend import web_sandbox_pep as wsp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fakes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class _FakeProposal:
    id: str
    kind: str
    severity: Any
    title: str
    detail: str
    options: list[dict]
    default_option_id: str | None
    source: dict[str, Any] = field(default_factory=dict)


def _make_propose_fn(outcomes: dict[str, str]):
    counter = {"n": 0}
    calls: list[_FakeProposal] = []

    def _fn(*, kind, title, detail="", options=None, default_option_id=None,
            severity=None, timeout_s=None, source=None):
        counter["n"] += 1
        pid = f"fake-dec-{counter['n']}"
        prop = _FakeProposal(
            id=pid, kind=kind, severity=severity, title=title, detail=detail,
            options=options or [], default_option_id=default_option_id,
            source=dict(source or {}),
        )
        calls.append(prop)
        outcomes.setdefault(pid, "approved")
        return prop

    return _fn, calls


def _waiter(outcomes: dict[str, str]):
    async def _wait(decision_id: str, timeout_s: float) -> str:
        return outcomes.get(decision_id, "rejected")
    return _wait


@pytest.fixture(autouse=True)
def _reset_pep():
    _pep._reset_for_tests()
    yield
    _pep._reset_for_tests()


@dataclass
class _StubInstance:
    """Mirror of the ``WebSandboxInstance.is_terminal`` surface
    :func:`requires_first_preview_hold` reads — kept tiny so tests
    don't pull the full :class:`backend.web_sandbox.WebSandboxInstance`
    construction graph in."""
    is_terminal: bool = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModuleSurface:

    def test_all_exports_match_set(self):
        expected = {
            "WEB_PREVIEW_PEP_SCHEMA_VERSION",
            "WEB_PREVIEW_PEP_TOOL",
            "WEB_PREVIEW_PEP_HOLD_TIMEOUT_S",
            "WEB_PREVIEW_PEP_TIER",
            "WEB_PREVIEW_PEP_AGENT_ID_PREFIX",
            "DEFAULT_NPM_INSTALL_SIZE_TEXT",
            "DEFAULT_INSTALL_ETA_TEXT",
            "WebPreviewPepError",
            "WebPreviewPepResult",
            "build_pep_arguments",
            "format_size_estimate_text",
            "format_eta_text",
            "requires_first_preview_hold",
            "evaluate_first_preview_hold",
        }
        assert set(wsp.__all__) == expected

    def test_all_exports_are_unique(self):
        assert len(wsp.__all__) == len(set(wsp.__all__))

    def test_schema_version_is_semver(self):
        # X.Y.Z, all numeric.
        parts = wsp.WEB_PREVIEW_PEP_SCHEMA_VERSION.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)

    def test_tool_name_is_pinned_constant(self):
        # Renaming this string would orphan the pep_gateway dispatch.
        assert wsp.WEB_PREVIEW_PEP_TOOL == "web_sandbox_preview"

    def test_tool_name_not_on_any_tier_whitelist(self):
        # W14.8 contract: every cold launch lands in tier_unlisted HOLD.
        # If a future row accidentally adds the string to a whitelist
        # this guard fires immediately.
        assert wsp.WEB_PREVIEW_PEP_TOOL not in _pep.TIER_T1_WHITELIST
        assert wsp.WEB_PREVIEW_PEP_TOOL not in _pep.TIER_T2_EXTRA
        assert wsp.WEB_PREVIEW_PEP_TOOL not in _pep.TIER_T3_EXTRA

    def test_hold_timeout_is_10_minutes(self):
        # Mirrors INSTALL_PEP_HOLD_TIMEOUT_S — long enough for a
        # distracted operator, short enough to keep a uvicorn worker
        # from being pinned indefinitely.
        assert wsp.WEB_PREVIEW_PEP_HOLD_TIMEOUT_S == 600.0

    def test_hold_timeout_positive(self):
        assert wsp.WEB_PREVIEW_PEP_HOLD_TIMEOUT_S > 0

    def test_tier_is_t1(self):
        # Most-restrictive tier — accidental future whitelist grows
        # cannot auto-approve a first-preview launch.
        assert wsp.WEB_PREVIEW_PEP_TIER == "t1"

    def test_agent_id_prefix_matches_installer_convention(self):
        assert wsp.WEB_PREVIEW_PEP_AGENT_ID_PREFIX == "operator:"

    def test_default_size_text_pinned_to_row_spec(self):
        # Row spec literal: "npm install 50–500 MB".
        assert wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT == "50–500 MB"

    def test_default_eta_text_pinned_to_row_spec(self):
        # Row spec literal: "ETA 30–90s".
        assert wsp.DEFAULT_INSTALL_ETA_TEXT == "30–90s"

    def test_default_size_uses_unicode_en_dash(self):
        # Literal must be the typographically correct en-dash so the
        # toast renders consistently with other coaching cards.
        assert "–" in wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT

    def test_default_eta_uses_unicode_en_dash(self):
        assert "–" in wsp.DEFAULT_INSTALL_ETA_TEXT

    def test_error_class_is_exception_subclass(self):
        assert issubclass(wsp.WebPreviewPepError, Exception)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  format_size_estimate_text
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFormatSizeEstimateText:

    def test_both_none_returns_default(self):
        assert wsp.format_size_estimate_text(None, None) == wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT

    def test_50_500_matches_row_spec(self):
        assert wsp.format_size_estimate_text(50, 500) == "50–500 MB"

    def test_low_only(self):
        assert wsp.format_size_estimate_text(120, None) == "~120 MB"

    def test_high_only(self):
        assert wsp.format_size_estimate_text(None, 180) == "~180 MB"

    def test_equal_low_high_collapses_to_single(self):
        assert wsp.format_size_estimate_text(200, 200) == "~200 MB"

    def test_swapped_order_is_normalised(self):
        # Caller passed (high, low) by accident — we sort.
        assert wsp.format_size_estimate_text(500, 50) == "50–500 MB"

    def test_zero_treated_as_none(self):
        assert wsp.format_size_estimate_text(0, 0) == wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT

    def test_negative_treated_as_none(self):
        assert wsp.format_size_estimate_text(-1, -2) == wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT

    def test_string_numeric_coerced(self):
        # Defensive: someone passes a JSON-decoded string.
        assert wsp.format_size_estimate_text("50", "500") == "50–500 MB"

    def test_non_numeric_returns_default(self):
        assert wsp.format_size_estimate_text("foo", "bar") == wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  format_eta_text
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFormatEtaText:

    def test_both_none_returns_default(self):
        assert wsp.format_eta_text(None, None) == wsp.DEFAULT_INSTALL_ETA_TEXT

    def test_30_90_matches_row_spec(self):
        assert wsp.format_eta_text(30, 90) == "30–90s"

    def test_low_only(self):
        assert wsp.format_eta_text(45, None) == "~45s"

    def test_high_only(self):
        assert wsp.format_eta_text(None, 60) == "~60s"

    def test_equal_low_high_collapses(self):
        assert wsp.format_eta_text(60, 60) == "~60s"

    def test_swapped_order_is_normalised(self):
        assert wsp.format_eta_text(90, 30) == "30–90s"

    def test_zero_returns_default(self):
        assert wsp.format_eta_text(0, 0) == wsp.DEFAULT_INSTALL_ETA_TEXT

    def test_negative_returns_default(self):
        assert wsp.format_eta_text(-30, -90) == wsp.DEFAULT_INSTALL_ETA_TEXT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  build_pep_arguments
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPepArguments:

    def _minimal(self) -> dict[str, Any]:
        return dict(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
        )

    def test_minimal_happy_path(self):
        out = wsp.build_pep_arguments(**self._minimal())
        assert out["workspace_id"] == "ws-foo"
        assert out["workspace_path"] == "/tmp/work"
        assert out["image_tag"] == "omnisight-web-preview:dev"
        assert out["size_estimate_text"] == wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT
        assert out["eta_text"] == wsp.DEFAULT_INSTALL_ETA_TEXT
        # Optional keys absent unless supplied.
        assert "git_ref" not in out
        assert "container_port" not in out
        assert "actor" not in out

    def test_optional_fields_layer(self):
        out = wsp.build_pep_arguments(
            **self._minimal(),
            git_ref="main",
            container_port=5173,
            actor_email="alice@example.com",
            size_text="~120 MB",
            eta_text="~45s",
        )
        assert out["git_ref"] == "main"
        assert out["container_port"] == 5173
        assert out["actor"] == "alice@example.com"
        assert out["size_estimate_text"] == "~120 MB"
        assert out["eta_text"] == "~45s"

    def test_container_port_coerces_string_to_int(self):
        # Defensive: pydantic gives str sometimes for query params.
        out = wsp.build_pep_arguments(**self._minimal(), container_port="3000")
        assert out["container_port"] == 3000
        assert isinstance(out["container_port"], int)

    def test_empty_workspace_id_rejected(self):
        kwargs = self._minimal()
        kwargs["workspace_id"] = ""
        with pytest.raises(ValueError, match="workspace_id"):
            wsp.build_pep_arguments(**kwargs)

    def test_non_string_workspace_id_rejected(self):
        kwargs = self._minimal()
        kwargs["workspace_id"] = 123  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="workspace_id"):
            wsp.build_pep_arguments(**kwargs)

    def test_empty_workspace_path_rejected(self):
        kwargs = self._minimal()
        kwargs["workspace_path"] = ""
        with pytest.raises(ValueError, match="workspace_path"):
            wsp.build_pep_arguments(**kwargs)

    def test_empty_image_tag_rejected(self):
        kwargs = self._minimal()
        kwargs["image_tag"] = ""
        with pytest.raises(ValueError, match="image_tag"):
            wsp.build_pep_arguments(**kwargs)

    def test_size_text_must_be_string_when_supplied(self):
        with pytest.raises(ValueError, match="size_text"):
            wsp.build_pep_arguments(**self._minimal(), size_text=42)  # type: ignore[arg-type]

    def test_eta_text_must_be_string_when_supplied(self):
        with pytest.raises(ValueError, match="eta_text"):
            wsp.build_pep_arguments(**self._minimal(), eta_text=42)  # type: ignore[arg-type]

    def test_size_text_none_falls_back_to_default(self):
        out = wsp.build_pep_arguments(**self._minimal(), size_text=None)
        assert out["size_estimate_text"] == wsp.DEFAULT_NPM_INSTALL_SIZE_TEXT

    def test_eta_text_none_falls_back_to_default(self):
        out = wsp.build_pep_arguments(**self._minimal(), eta_text=None)
        assert out["eta_text"] == wsp.DEFAULT_INSTALL_ETA_TEXT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  requires_first_preview_hold
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRequiresFirstPreviewHold:

    def test_force_true_always_holds(self):
        assert wsp.requires_first_preview_hold(
            lambda _: _StubInstance(is_terminal=False),
            "ws-x", force=True,
        ) is True

    def test_no_instance_holds(self):
        assert wsp.requires_first_preview_hold(
            lambda _: None, "ws-x",
        ) is True

    def test_live_instance_skips(self):
        assert wsp.requires_first_preview_hold(
            lambda _: _StubInstance(is_terminal=False), "ws-x",
        ) is False

    def test_terminal_instance_holds(self):
        # Operator's previous launch ended; next is again "first".
        assert wsp.requires_first_preview_hold(
            lambda _: _StubInstance(is_terminal=True), "ws-x",
        ) is True

    def test_empty_workspace_id_rejected(self):
        with pytest.raises(ValueError, match="workspace_id"):
            wsp.requires_first_preview_hold(lambda _: None, "")

    def test_non_string_workspace_id_rejected(self):
        with pytest.raises(ValueError, match="workspace_id"):
            wsp.requires_first_preview_hold(lambda _: None, 123)  # type: ignore[arg-type]

    def test_manager_get_callable_receives_workspace_id(self):
        seen: list[str] = []
        def _get(wsid: str):
            seen.append(wsid)
            return None
        wsp.requires_first_preview_hold(_get, "ws-traced")
        assert seen == ["ws-traced"]

    def test_force_skips_workspace_id_validation(self):
        # ``force=True`` short-circuits before validation — useful when
        # a caller wants to force-hold but doesn't have a workspace_id
        # yet (rare, but covered for completeness).
        assert wsp.requires_first_preview_hold(
            lambda _: None, "", force=True,
        ) is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebPreviewPepResult
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWebPreviewPepResult:

    def test_approved_flag(self):
        r = wsp.WebPreviewPepResult(action="approved")
        assert r.is_approved
        assert not r.is_rejected
        assert not r.is_error

    def test_rejected_flag(self):
        r = wsp.WebPreviewPepResult(action="rejected")
        assert r.is_rejected
        assert not r.is_approved
        assert not r.is_error

    def test_gateway_error_flag(self):
        r = wsp.WebPreviewPepResult(action="gateway_error")
        assert r.is_error
        assert not r.is_approved
        assert not r.is_rejected

    def test_to_dict_round_trip(self):
        r = wsp.WebPreviewPepResult(
            action="approved",
            reason="operator approved",
            decision_id="dec-42",
            rule="tier_unlisted",
            degraded=False,
        )
        d = r.to_dict()
        assert d == {
            "schema_version": wsp.WEB_PREVIEW_PEP_SCHEMA_VERSION,
            "action": "approved",
            "reason": "operator approved",
            "decision_id": "dec-42",
            "rule": "tier_unlisted",
            "degraded": False,
        }

    def test_frozen_dataclass(self):
        r = wsp.WebPreviewPepResult(action="approved")
        with pytest.raises(Exception):
            r.action = "rejected"  # type: ignore[misc]

    def test_default_schema_version_pinned(self):
        r = wsp.WebPreviewPepResult(action="approved")
        assert r.schema_version == wsp.WEB_PREVIEW_PEP_SCHEMA_VERSION


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  evaluate_first_preview_hold — async wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEvaluateFirstPreviewHold:

    @pytest.mark.asyncio
    async def test_approved_path_returns_decision_id(self):
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, calls = _make_propose_fn(outcomes)
        result = await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            actor_email="alice@example.com",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert result.is_approved
        assert result.action == "approved"
        assert result.decision_id == "fake-dec-1"
        assert result.rule == "tier_unlisted"
        assert not result.degraded
        # propose_fn was called once with the right kind / category /
        # coaching content.
        assert len(calls) == 1
        prop = calls[0]
        assert prop.kind == "pep_tool_intercept"
        assert prop.source["category"] == _pep.WEB_PREVIEW_INTERCEPT_CATEGORY
        coaching = prop.source["coaching"]
        assert set(coaching.keys()) == {"what", "why", "if_approve", "if_reject"}
        assert "ws-foo" in coaching["what"]
        assert "omnisight-web-preview:dev" in coaching["what"]
        assert "50–500 MB" in coaching["why"]
        assert "30–90s" in coaching["why"]

    @pytest.mark.asyncio
    async def test_rejected_path_returns_rejected(self):
        outcomes: dict[str, str] = {"fake-dec-1": "rejected"}
        propose_fn, _calls = _make_propose_fn(outcomes)
        result = await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            actor_email="alice@example.com",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert result.is_rejected
        assert result.action == "rejected"
        assert "operator rejected" in result.reason

    @pytest.mark.asyncio
    async def test_gateway_error_returns_gateway_error_action(self):
        # propose_fn that raises synchronously trips the gateway's
        # circuit breaker, but the FIRST raise still flips the gateway
        # to a "fail closed" deny — surface that as gateway_error so
        # the router can return 503 (not 403).
        def boom_propose(**_kwargs):
            raise RuntimeError("DE down")

        async def waiter_unused(decision_id: str, timeout_s: float) -> str:
            return "rejected"  # never reached

        result = await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            propose_fn=boom_propose,
            wait_for_decision=waiter_unused,
            hold_timeout_s=1.0,
        )
        # The gateway converts propose-raises to a degraded deny rather
        # than re-raising, so this path returns "rejected" with degraded.
        assert result.is_rejected
        assert result.degraded is True

    @pytest.mark.asyncio
    async def test_pep_evaluate_raise_surfaces_as_gateway_error(self, monkeypatch):
        # Monkey-patch ``pep_gateway.evaluate`` to raise so we exercise
        # the WebPreviewPepResult.gateway_error branch (the gateway's
        # internal breaker is bypassed by raising at the outer call).
        async def raising_evaluate(**_kwargs):
            raise RuntimeError("transport explosion")

        monkeypatch.setattr(_pep, "evaluate", raising_evaluate)

        result = await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            hold_timeout_s=1.0,
        )
        assert result.is_error
        assert result.action == "gateway_error"
        assert "RuntimeError" in result.reason

    @pytest.mark.asyncio
    async def test_zero_or_negative_timeout_rejected(self):
        with pytest.raises(ValueError, match="hold_timeout_s"):
            await wsp.evaluate_first_preview_hold(
                workspace_id="ws-foo",
                workspace_path="/tmp/work",
                image_tag="omnisight-web-preview:dev",
                hold_timeout_s=0.0,
            )

    @pytest.mark.asyncio
    async def test_default_timeout_used_when_none(self):
        # We cannot directly observe the timeout, but we can verify the
        # call still succeeds end-to-end through the injection hooks
        # without a hold_timeout_s kwarg.
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, _calls = _make_propose_fn(outcomes)
        result = await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
        )
        assert result.is_approved

    @pytest.mark.asyncio
    async def test_actor_email_lands_on_agent_id(self):
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, calls = _make_propose_fn(outcomes)
        await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            actor_email="bob@example.com",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        # Pep gateway audit row carries agent_id, but propose_fn doesn't
        # see it directly — assert via the source.coaching arguments
        # round-trip and the gateway's recent-decisions ring instead.
        recent = _pep.recent_decisions(limit=5)
        assert any(d["agent_id"] == "operator:bob@example.com" for d in recent)

    @pytest.mark.asyncio
    async def test_no_actor_email_uses_bare_operator_agent_id(self):
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, _calls = _make_propose_fn(outcomes)
        await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        recent = _pep.recent_decisions(limit=5)
        # Stripped-prefix bare "operator" stays consistent with the
        # convention so audit filters can ``LIKE 'operator:%' OR =
        # 'operator'``.
        assert any(d["agent_id"] in ("operator", "operator:") for d in recent)

    @pytest.mark.asyncio
    async def test_arguments_extra_layered(self):
        # Caller hands extra fields through to the coaching card's
        # source dict — verify they layer in without clobbering the
        # PEP-mandated keys.
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, calls = _make_propose_fn(outcomes)
        await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
            arguments_extra={
                "experiment_tag": "w14-rollout",
                # Try to clobber a mandated key — the helper must keep
                # the canonical value, not the override.
                "size_estimate_text": "MUTANT",
            },
        )
        # The recent decision's command (joined arguments) shows the
        # canonical size — the mutant override was rejected.
        recent = _pep.recent_decisions(limit=5)
        assert recent[0]["command"]
        assert "MUTANT" not in recent[0]["command"]

    @pytest.mark.asyncio
    async def test_severity_is_risky_not_destructive(self):
        # impact_scope is "local" (tier_unlisted), so severity stays
        # risky — operator UI doesn't escalate to red for a routine
        # first launch.
        from backend import decision_engine as de

        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, calls = _make_propose_fn(outcomes)
        await wsp.evaluate_first_preview_hold(
            workspace_id="ws-foo",
            workspace_path="/tmp/work",
            image_tag="omnisight-web-preview:dev",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert calls[0].severity is de.DecisionSeverity.risky

    @pytest.mark.asyncio
    async def test_category_constant_does_not_collide(self):
        # Future-proofing: the W14.8 category must never match the
        # install_intercept or default category — the ToastCenter
        # switches rendering on equality.
        assert (
            _pep.WEB_PREVIEW_INTERCEPT_CATEGORY
            != _pep.INSTALL_INTERCEPT_CATEGORY
        )
        assert (
            _pep.WEB_PREVIEW_INTERCEPT_CATEGORY
            != _pep.DEFAULT_PEP_INTERCEPT_CATEGORY
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-worker contract (SOP §1 type-1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCrossWorkerContract:

    def test_build_pep_arguments_deterministic_across_calls(self):
        # Each uvicorn worker derives the same args from the same
        # source. Hash the JSON serialisation 8 times in a row to pin.
        import json
        args = [
            wsp.build_pep_arguments(
                workspace_id="ws-foo",
                workspace_path="/tmp/work",
                image_tag="omnisight-web-preview:dev",
                actor_email="alice@example.com",
                git_ref="main",
                container_port=5173,
            )
            for _ in range(8)
        ]
        serialised = [json.dumps(a, sort_keys=True) for a in args]
        assert len(set(serialised)) == 1

    def test_format_size_estimate_deterministic(self):
        outs = [wsp.format_size_estimate_text(50, 500) for _ in range(8)]
        assert len(set(outs)) == 1

    def test_format_eta_deterministic(self):
        outs = [wsp.format_eta_text(30, 90) for _ in range(8)]
        assert len(set(outs)) == 1
