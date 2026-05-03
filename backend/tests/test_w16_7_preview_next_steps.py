"""W16.7 — Next-step coaching SSE event contract tests.

Locks the public surface of ``backend.web.preview_next_steps`` so the
W16.7 ``preview.next_steps`` event stays binding for the
orchestrator-chat SSE consumer (``components/omnisight/workspace-chat.tsx``).

Coverage axes
─────────────

  §A  Drift guards — every frozen wire-shape constant + bound cap +
      kind tuple + slash command literal + dataclass invariant.
  §B  Validation happy paths — full payload + label-defaulting +
      default option set.
  §C  Validation failure paths — empty workspace_id / over-cap fields /
      bad option types / unknown kinds.
  §D  ``PreviewNextStepsPayload.to_event_data`` — drops ``preview_url``
      when None, projects options[].
  §E  ``build_default_next_step_options`` — row-spec order +
      recommended marker + label defaults + slash command threading.
  §F  ``build_chat_message_for_preview_next_steps`` — produces the
      WorkspaceChat shape, role=system, ``previewNextSteps`` carries
      the menu.
  §G  ``emit_preview_next_steps`` — publishes one event with the
      validated payload, default broadcast scope, never raises on
      transport failure.
  §H  Router wiring — ``POST /web-sandbox/preview/{ws}/ready`` fires
      ``preview.next_steps`` once per running transition.
  §I  Re-export sweep — every public symbol surfaces from the
      ``backend.web`` package.

These tests are PG-free, LLM-free, and do not bring up a real docker
daemon; the W14 manager-side concerns are covered by
``test_web_sandbox.py`` / ``test_web_sandbox_router.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend import web as web_pkg
from backend import workspace as _ws
from backend.routers import web_sandbox as web_sandbox_router
from backend.tests.test_web_sandbox import (
    FakeClock,
    FakeDockerClient,
    RecordingEventCallback,
)
from backend.web import preview_next_steps as pns
from backend.web_sandbox import WebSandboxManager, WebSandboxStatus


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:

    def test_event_name_pinned(self) -> None:
        # Frontend SSE consumer switches on this literal — drift breaks
        # the menu-mounting branch silently.
        assert pns.PREVIEW_NEXT_STEPS_EVENT_NAME == "preview.next_steps"

    def test_pipeline_phase_pinned(self) -> None:
        assert pns.PREVIEW_NEXT_STEPS_PIPELINE_PHASE == "preview_next_steps"

    def test_default_broadcast_scope_is_session(self) -> None:
        assert (
            pns.PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE == "session"
        )

    def test_default_label_is_human_readable_string(self) -> None:
        assert isinstance(pns.PREVIEW_NEXT_STEPS_DEFAULT_LABEL, str)
        assert pns.PREVIEW_NEXT_STEPS_DEFAULT_LABEL.strip()
        assert "?" in pns.PREVIEW_NEXT_STEPS_DEFAULT_LABEL

    def test_kinds_pinned_to_row_spec_order(self) -> None:
        # Row spec: (a) Vercel deploy / (b) a11y scan / (c) commit+PR /
        # (d) 繼續編輯 — order is binding because the FE iterates this
        # tuple to render the menu deterministically.
        assert pns.PREVIEW_NEXT_STEP_KINDS == (
            "vercel_deploy",
            "a11y_scan",
            "commit_pr",
            "continue_edit",
        )

    def test_kind_identifiers_pinned(self) -> None:
        assert pns.PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY == "vercel_deploy"
        assert pns.PREVIEW_NEXT_STEP_KIND_A11Y_SCAN == "a11y_scan"
        assert pns.PREVIEW_NEXT_STEP_KIND_COMMIT_PR == "commit_pr"
        assert pns.PREVIEW_NEXT_STEP_KIND_CONTINUE_EDIT == "continue_edit"

    def test_default_recommended_kind_is_vercel_deploy(self) -> None:
        # The most common follow-up to "preview is live" is "make this
        # URL shareable" — Vercel deploy carries the ★ by default.
        assert (
            pns.PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND
            == pns.PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY
        )

    def test_slash_commands_start_with_slash(self) -> None:
        assert pns.PREVIEW_NEXT_STEP_DEPLOY_SLASH_COMMAND.startswith("/")
        assert pns.PREVIEW_NEXT_STEP_A11Y_SLASH_COMMAND.startswith("/")
        assert pns.PREVIEW_NEXT_STEP_COMMIT_PR_SLASH_COMMAND.startswith("/")
        assert pns.PREVIEW_NEXT_STEP_CONTINUE_EDIT_SLASH_COMMAND.startswith("/")

    def test_slash_commands_pinned(self) -> None:
        assert pns.PREVIEW_NEXT_STEP_DEPLOY_SLASH_COMMAND == "/deploy-preview"
        assert pns.PREVIEW_NEXT_STEP_A11Y_SLASH_COMMAND == "/a11y-scan"
        assert pns.PREVIEW_NEXT_STEP_COMMIT_PR_SLASH_COMMAND == "/commit-and-pr"
        # Continue-edit reuses W16.5's slash so the menu doesn't
        # introduce a new verb the operator has to learn.
        assert (
            pns.PREVIEW_NEXT_STEP_CONTINUE_EDIT_SLASH_COMMAND
            == "/edit-preview"
        )

    def test_labels_cover_every_kind(self) -> None:
        assert set(pns.PREVIEW_NEXT_STEP_LABELS.keys()) == set(
            pns.PREVIEW_NEXT_STEP_KINDS
        )

    def test_labels_are_bilingual(self) -> None:
        # Every row-spec label carries both Chinese and English so
        # bilingual operators see their preferred locale on first read.
        for kind, label in pns.PREVIEW_NEXT_STEP_LABELS.items():
            assert "/" in label, (
                f"label for {kind} not bilingual (missing '/'): {label!r}"
            )

    def test_max_workspace_id_bytes_pinned(self) -> None:
        assert pns.MAX_PREVIEW_NEXT_STEPS_WORKSPACE_ID_BYTES == 256

    def test_max_label_bytes_pinned(self) -> None:
        assert pns.MAX_PREVIEW_NEXT_STEPS_LABEL_BYTES == 120

    def test_max_url_bytes_pinned(self) -> None:
        assert pns.MAX_PREVIEW_NEXT_STEPS_URL_BYTES == 4096

    def test_max_kind_bytes_pinned(self) -> None:
        assert pns.MAX_PREVIEW_NEXT_STEP_KIND_BYTES == 32

    def test_max_option_label_bytes_pinned(self) -> None:
        assert pns.MAX_PREVIEW_NEXT_STEP_OPTION_LABEL_BYTES == 120

    def test_max_slash_command_bytes_pinned(self) -> None:
        assert pns.MAX_PREVIEW_NEXT_STEP_SLASH_COMMAND_BYTES == 256

    def test_payload_dataclass_is_frozen(self) -> None:
        payload = pns.PreviewNextStepsPayload(
            workspace_id="ws-1", options=tuple(),
        )
        with pytest.raises((AttributeError, Exception)):
            payload.workspace_id = "ws-2"  # type: ignore[misc]

    def test_option_dataclass_is_frozen(self) -> None:
        opt = pns.PreviewNextStepOption(
            kind="vercel_deploy", label="x",
            slash_command="/deploy-preview ws-1 --target=vercel",
        )
        with pytest.raises((AttributeError, Exception)):
            opt.kind = "a11y_scan"  # type: ignore[misc]

    def test_preview_next_steps_error_subclasses_value_error(self) -> None:
        assert issubclass(pns.PreviewNextStepsError, ValueError)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  Validation happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadHappyPaths:

    def test_full_payload_with_defaults(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42",
        )
        assert payload.workspace_id == "ws-42"
        assert payload.label == pns.PREVIEW_NEXT_STEPS_DEFAULT_LABEL
        assert len(payload.options) == 4
        # Vercel deploy should be the recommended default.
        assert payload.options[0].kind == "vercel_deploy"
        assert payload.options[0].recommended is True
        # Other three are not recommended.
        assert all(not o.recommended for o in payload.options[1:])

    def test_label_defaults_when_omitted(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42",
        )
        assert payload.label == pns.PREVIEW_NEXT_STEPS_DEFAULT_LABEL

    def test_label_defaults_when_empty_string(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42", label="",
        )
        assert payload.label == pns.PREVIEW_NEXT_STEPS_DEFAULT_LABEL

    def test_custom_label_threaded(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42", label="預覽好了，接下來？",
        )
        assert payload.label == "預覽好了，接下來？"

    def test_preview_url_threaded(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42",
            preview_url="https://preview-abc.example.com",
        )
        assert payload.preview_url == "https://preview-abc.example.com"

    def test_preview_url_default_none(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42",
        )
        assert payload.preview_url is None

    def test_recommended_kind_override(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42",
            recommended_kind="commit_pr",
        )
        # Only commit_pr should be recommended.
        recs = [o for o in payload.options if o.recommended]
        assert len(recs) == 1
        assert recs[0].kind == "commit_pr"

    def test_recommended_kind_empty_string_drops_marker(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42", recommended_kind="",
        )
        assert all(not o.recommended for o in payload.options)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  Validation failure paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadFailures:

    def test_empty_workspace_id_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(workspace_id="")

    def test_non_string_workspace_id_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id=42,  # type: ignore[arg-type]
            )

    def test_workspace_id_over_cap_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id="x" * (
                    pns.MAX_PREVIEW_NEXT_STEPS_WORKSPACE_ID_BYTES + 1
                ),
            )

    def test_label_over_cap_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id="ws-1",
                label="x" * (pns.MAX_PREVIEW_NEXT_STEPS_LABEL_BYTES + 1),
            )

    def test_preview_url_over_cap_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id="ws-1",
                preview_url="http://x/" + "a" * pns.MAX_PREVIEW_NEXT_STEPS_URL_BYTES,
            )

    def test_unknown_recommended_kind_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id="ws-1", recommended_kind="bogus",
            )

    def test_options_must_be_tuple(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id="ws-1",
                options=[],  # type: ignore[arg-type]
            )

    def test_options_must_contain_option_dataclass(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id="ws-1",
                options=({"kind": "vercel_deploy"},),  # type: ignore[arg-type]
            )

    def test_unknown_option_kind_raises(self) -> None:
        # Tampered dataclass — bypass the constructor to surface the
        # higher-level guard.
        fake = pns.PreviewNextStepOption.__new__(pns.PreviewNextStepOption)
        object.__setattr__(fake, "kind", "bogus")
        object.__setattr__(fake, "label", "x")
        object.__setattr__(fake, "slash_command", "/x")
        object.__setattr__(fake, "recommended", False)
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_steps_payload(
                workspace_id="ws-1", options=(fake,),
            )

    def test_option_unknown_kind_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_step_option(
                kind="bogus", workspace_id="ws-1",
            )

    def test_option_slash_command_must_start_with_slash(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_step_option(
                kind="vercel_deploy", workspace_id="ws-1",
                slash_command="deploy-preview ws-1",
            )

    def test_option_label_over_cap_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_step_option(
                kind="vercel_deploy", workspace_id="ws-1",
                label="x" * (pns.MAX_PREVIEW_NEXT_STEP_OPTION_LABEL_BYTES + 1),
            )

    def test_option_recommended_must_be_bool(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_preview_next_step_option(
                kind="vercel_deploy", workspace_id="ws-1",
                recommended="yes",  # type: ignore[arg-type]
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  to_event_data projection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToEventData:

    def test_minimal_payload_three_keys(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-1")
        data = payload.to_event_data()
        assert set(data.keys()) == {"workspace_id", "label", "options"}

    def test_full_payload_includes_preview_url(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-1",
            preview_url="https://x.example.com",
        )
        data = payload.to_event_data()
        assert "preview_url" in data
        assert data["preview_url"] == "https://x.example.com"

    def test_options_projected_in_row_spec_order(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-1")
        data = payload.to_event_data()
        kinds = [o["kind"] for o in data["options"]]
        assert kinds == list(pns.PREVIEW_NEXT_STEP_KINDS)

    def test_recommended_flag_only_on_recommended(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-1")
        data = payload.to_event_data()
        recs = [o for o in data["options"] if o.get("recommended")]
        assert len(recs) == 1
        assert recs[0]["kind"] == "vercel_deploy"

    def test_each_option_carries_kind_label_slash_command(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-1")
        data = payload.to_event_data()
        for opt in data["options"]:
            assert "kind" in opt
            assert "label" in opt
            assert "slash_command" in opt

    def test_slash_commands_thread_workspace_id(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-77")
        data = payload.to_event_data()
        for opt in data["options"]:
            assert "ws-77" in opt["slash_command"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  build_default_next_step_options
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildDefaultOptions:

    def test_returns_four_options_in_row_spec_order(self) -> None:
        opts = pns.build_default_next_step_options("ws-1")
        assert tuple(o.kind for o in opts) == pns.PREVIEW_NEXT_STEP_KINDS

    def test_default_recommended_is_vercel_deploy(self) -> None:
        opts = pns.build_default_next_step_options("ws-1")
        recs = [o for o in opts if o.recommended]
        assert len(recs) == 1
        assert recs[0].kind == "vercel_deploy"

    def test_recommended_kind_override_a11y(self) -> None:
        opts = pns.build_default_next_step_options(
            "ws-1", recommended_kind="a11y_scan",
        )
        recs = [o for o in opts if o.recommended]
        assert recs[0].kind == "a11y_scan"

    def test_recommended_kind_none_uses_default(self) -> None:
        opts = pns.build_default_next_step_options(
            "ws-1", recommended_kind=None,
        )
        # None resolves to the default (vercel_deploy).
        recs = [o for o in opts if o.recommended]
        assert recs[0].kind == "vercel_deploy"

    def test_recommended_kind_empty_drops_marker(self) -> None:
        opts = pns.build_default_next_step_options(
            "ws-1", recommended_kind="",
        )
        assert all(not o.recommended for o in opts)

    def test_unknown_recommended_kind_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.build_default_next_step_options(
                "ws-1", recommended_kind="bogus",
            )

    def test_default_labels_match_row_spec_table(self) -> None:
        opts = pns.build_default_next_step_options("ws-1")
        for opt in opts:
            assert opt.label == pns.PREVIEW_NEXT_STEP_LABELS[opt.kind]

    def test_default_slash_commands_thread_workspace_id(self) -> None:
        opts = pns.build_default_next_step_options("ws-99")
        slashes = {o.kind: o.slash_command for o in opts}
        assert slashes["vercel_deploy"] == "/deploy-preview ws-99 --target=vercel"
        assert slashes["a11y_scan"] == "/a11y-scan ws-99"
        assert slashes["commit_pr"] == "/commit-and-pr ws-99"
        assert slashes["continue_edit"] == "/edit-preview ws-99"


class TestRenderDefaultSlashCommand:

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.render_default_slash_command("bogus", "ws-1")

    def test_vercel_deploy_format(self) -> None:
        s = pns.render_default_slash_command("vercel_deploy", "ws-7")
        assert s == "/deploy-preview ws-7 --target=vercel"

    def test_a11y_format(self) -> None:
        s = pns.render_default_slash_command("a11y_scan", "ws-7")
        assert s == "/a11y-scan ws-7"

    def test_commit_pr_format(self) -> None:
        s = pns.render_default_slash_command("commit_pr", "ws-7")
        assert s == "/commit-and-pr ws-7"

    def test_continue_edit_format(self) -> None:
        s = pns.render_default_slash_command("continue_edit", "ws-7")
        assert s == "/edit-preview ws-7"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  build_chat_message_for_preview_next_steps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildChatMessage:

    def test_role_is_system(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-42")
        msg = pns.build_chat_message_for_preview_next_steps(payload)
        assert msg["role"] == "system"

    def test_text_uses_label(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42", label="預覽好了，接下來？",
        )
        msg = pns.build_chat_message_for_preview_next_steps(payload)
        assert msg["text"] == "預覽好了，接下來？"

    def test_preview_next_steps_shape(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-42")
        msg = pns.build_chat_message_for_preview_next_steps(payload)
        assert msg["previewNextSteps"]["workspaceId"] == "ws-42"
        assert msg["previewNextSteps"]["label"]
        assert len(msg["previewNextSteps"]["options"]) == 4

    def test_options_use_camelCase_slash_command_field(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-42")
        msg = pns.build_chat_message_for_preview_next_steps(payload)
        for opt in msg["previewNextSteps"]["options"]:
            assert "slashCommand" in opt  # camelCase for FE consumption

    def test_recommended_flag_threaded(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-42")
        msg = pns.build_chat_message_for_preview_next_steps(payload)
        recs = [
            o for o in msg["previewNextSteps"]["options"]
            if o.get("recommended")
        ]
        assert len(recs) == 1
        assert recs[0]["kind"] == "vercel_deploy"

    def test_preview_url_threaded_via_camelCase(self) -> None:
        payload = pns.build_preview_next_steps_payload(
            workspace_id="ws-42",
            preview_url="https://x.example.com",
        )
        msg = pns.build_chat_message_for_preview_next_steps(payload)
        assert msg["previewNextSteps"]["previewUrl"] == "https://x.example.com"

    def test_preview_url_omitted_when_none(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-42")
        msg = pns.build_chat_message_for_preview_next_steps(payload)
        assert "previewUrl" not in msg["previewNextSteps"]

    def test_message_id_threaded(self) -> None:
        payload = pns.build_preview_next_steps_payload(workspace_id="ws-42")
        msg = pns.build_chat_message_for_preview_next_steps(
            payload, message_id="m-9",
        )
        assert msg["id"] == "m-9"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  emit_preview_next_steps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmitPreviewNextSteps:

    def test_publishes_one_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from backend import events as events_mod
        captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **kwargs: Any) -> None:
            captured.append((event, dict(data), dict(kwargs)))

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        pns.emit_preview_next_steps(
            workspace_id="ws-42", broadcast_scope="session",
        )
        assert len(captured) == 1
        event, data, kwargs = captured[0]
        assert event == "preview.next_steps"
        assert data["workspace_id"] == "ws-42"
        assert data["label"] == pns.PREVIEW_NEXT_STEPS_DEFAULT_LABEL
        assert len(data["options"]) == 4
        assert kwargs["broadcast_scope"] == "session"

    def test_default_broadcast_scope_is_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod
        captured: list[dict[str, Any]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **kwargs: Any) -> None:
            captured.append(kwargs)

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        pns.emit_preview_next_steps(workspace_id="ws-42")
        assert captured[0]["broadcast_scope"] == "session"

    def test_extras_pass_through_alongside_frozen_keys(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod
        captured: list[dict[str, Any]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **kwargs: Any) -> None:
            captured.append(data)

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        pns.emit_preview_next_steps(
            workspace_id="ws-42",
            broadcast_scope="session",
            schema_version="v1",
        )
        assert captured[0]["schema_version"] == "v1"
        assert captured[0]["workspace_id"] == "ws-42"

    def test_returns_validated_payload(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod

        monkeypatch.setattr(
            events_mod.EventBus, "publish",
            lambda *a, **k: None, raising=True,
        )
        payload = pns.emit_preview_next_steps(
            workspace_id="ws-42",
            preview_url="https://x.example.com",
            broadcast_scope="session",
        )
        assert isinstance(payload, pns.PreviewNextStepsPayload)
        assert payload.workspace_id == "ws-42"
        assert payload.preview_url == "https://x.example.com"

    def test_propagates_validation_error(self) -> None:
        with pytest.raises(pns.PreviewNextStepsError):
            pns.emit_preview_next_steps(
                workspace_id="", broadcast_scope="session",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §H  Router wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _operator() -> _au.User:
    return _au.User(
        id="u-operator", email="op@example.com",
        name="Op", role="operator",
    )


def _viewer() -> _au.User:
    return _au.User(
        id="u-viewer", email="viewer@example.com",
        name="V", role="viewer",
    )


@pytest.fixture
def manager(tmp_path: Path) -> WebSandboxManager:
    return WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
    )


@pytest.fixture
def client(
    manager: WebSandboxManager, monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    app = FastAPI()
    app.include_router(web_sandbox_router.router)
    app.dependency_overrides[_au.require_operator] = _operator
    app.dependency_overrides[_au.require_viewer] = _viewer
    app.dependency_overrides[web_sandbox_router.get_manager] = lambda: manager
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_workspace_registry() -> Any:
    saved = dict(_ws._workspaces)
    _ws._workspaces.clear()
    yield
    _ws._workspaces.clear()
    _ws._workspaces.update(saved)


@pytest.fixture(autouse=True)
def stub_pep_evaluator() -> Any:
    from backend import web_sandbox_pep as _wsp

    async def _auto_approve(**_kwargs: Any) -> _wsp.WebPreviewPepResult:
        return _wsp.WebPreviewPepResult(
            action="approved",
            reason="auto-approved by stub_pep_evaluator fixture",
            decision_id="stub-dec",
            rule="tier_unlisted",
        )

    web_sandbox_router.set_pep_evaluator_for_tests(_auto_approve)
    yield
    web_sandbox_router.set_pep_evaluator_for_tests(None)


class TestRouterWiring:

    def test_post_ready_emits_next_steps_event(
        self, client: TestClient, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod
        captured: list[tuple[str, dict[str, Any]]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **_kwargs: Any) -> None:
            captured.append((event, dict(data)))

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )

        launch = client.post(
            "/web-sandbox/preview",
            json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
        )
        assert launch.status_code == 200, launch.text
        ready = client.post("/web-sandbox/preview/ws-42/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["status"] == WebSandboxStatus.running.value

        next_step_events = [
            (event, data) for (event, data) in captured
            if event == pns.PREVIEW_NEXT_STEPS_EVENT_NAME
        ]
        assert len(next_step_events) == 1, next_step_events
        _, data = next_step_events[0]
        assert data["workspace_id"] == "ws-42"
        assert len(data["options"]) == 4
        kinds = [o["kind"] for o in data["options"]]
        assert kinds == list(pns.PREVIEW_NEXT_STEP_KINDS)

    def test_post_ready_404_does_not_emit(
        self, client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod
        captured: list[str] = []

        def _fake_publish(self: Any, event: str, *_a: Any, **_kw: Any) -> None:
            captured.append(event)

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        resp = client.post("/web-sandbox/preview/ws-nope/ready")
        assert resp.status_code == 404
        assert pns.PREVIEW_NEXT_STEPS_EVENT_NAME not in captured

    def test_idempotent_ready_emits_once_per_call(
        self, client: TestClient, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Idempotent W14.2 mark_ready re-fires the SSE event each call,
        # mirroring W16.4's policy so the FE can re-mount the iframe AND
        # re-coach.
        from backend import events as events_mod
        captured: list[str] = []

        def _fake_publish(self: Any, event: str, *_a: Any, **_kw: Any) -> None:
            captured.append(event)

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )

        client.post(
            "/web-sandbox/preview",
            json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
        )
        client.post("/web-sandbox/preview/ws-42/ready")
        client.post("/web-sandbox/preview/ws-42/ready")
        nstep_count = sum(
            1 for e in captured if e == pns.PREVIEW_NEXT_STEPS_EVENT_NAME
        )
        assert nstep_count == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §I  Re-export sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W16_7_SYMBOLS = (
    "MAX_PREVIEW_NEXT_STEPS_LABEL_BYTES",
    "MAX_PREVIEW_NEXT_STEPS_URL_BYTES",
    "MAX_PREVIEW_NEXT_STEPS_WORKSPACE_ID_BYTES",
    "MAX_PREVIEW_NEXT_STEP_KIND_BYTES",
    "MAX_PREVIEW_NEXT_STEP_OPTION_LABEL_BYTES",
    "MAX_PREVIEW_NEXT_STEP_SLASH_COMMAND_BYTES",
    "PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_NEXT_STEPS_DEFAULT_LABEL",
    "PREVIEW_NEXT_STEPS_EVENT_NAME",
    "PREVIEW_NEXT_STEPS_PIPELINE_PHASE",
    "PREVIEW_NEXT_STEP_A11Y_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_COMMIT_PR_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_CONTINUE_EDIT_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND",
    "PREVIEW_NEXT_STEP_DEPLOY_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_KINDS",
    "PREVIEW_NEXT_STEP_KIND_A11Y_SCAN",
    "PREVIEW_NEXT_STEP_KIND_COMMIT_PR",
    "PREVIEW_NEXT_STEP_KIND_CONTINUE_EDIT",
    "PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY",
    "PREVIEW_NEXT_STEP_LABELS",
    "PreviewNextStepOption",
    "PreviewNextStepsError",
    "PreviewNextStepsPayload",
    "build_chat_message_for_preview_next_steps",
    "build_default_next_step_options",
    "build_preview_next_step_option",
    "build_preview_next_steps_payload",
    "emit_preview_next_steps",
    "render_default_slash_command",
)


@pytest.mark.parametrize("symbol", _W16_7_SYMBOLS)
def test_w16_7_symbol_re_exported(symbol: str) -> None:
    assert symbol in web_pkg.__all__, (
        f"{symbol} missing from backend.web.__all__"
    )
    assert getattr(web_pkg, symbol) is getattr(pns, symbol)


def test_total_re_export_count_matches_w16_7_baseline() -> None:
    # Bumped from 396 (W16.6 baseline) → 426 (W16.7 +30
    # preview_next_steps).
    assert len(web_pkg.__all__) == 466


def test_w16_7_symbol_count_matches_module_all() -> None:
    assert len(pns.__all__) == len(_W16_7_SYMBOLS)
    assert set(pns.__all__) == set(_W16_7_SYMBOLS)
