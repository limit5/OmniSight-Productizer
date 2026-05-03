"""W16.5 — ``preview.hmr_reload`` SSE event contract tests.

Locks the public surface of ``backend.web.preview_hmr_reload`` so the
W16.5 ``preview.hmr_reload`` event stays binding for the orchestrator-
chat SSE consumer (``components/omnisight/workspace-chat.tsx``) and
the W16.6 (vite error in dialogue) row that re-uses the same event
shape after a self-fix lands.

Coverage axes
─────────────

  §A  Drift guards — every frozen wire-shape constant + bound cap +
      change-kind enum + dataclass invariant.
  §B  Validation happy paths — full payload + label-defaulting +
      change-kind defaulting + optional-field elision.
  §C  Validation failure paths — empty workspace_id / over-cap label
      / unknown change_kind / wrong type.
  §D  ``PreviewHmrReloadPayload.to_event_data`` — drops ``None`` keys,
      preserves ``label`` + ``change_kind`` even at default, never
      bleeds extra keys.
  §E  ``build_chat_message_for_preview_hmr_reload`` — produces the
      WorkspaceChat shape, role=system, ``previewHmrReload`` carries
      the workspace id.
  §F  ``emit_preview_hmr_reload`` — publishes one
      ``preview.hmr_reload`` event with the validated payload, default
      broadcast scope, never raises on transport failure.
  §G  Re-export sweep — every public symbol surfaces from the
      ``backend.web`` package.

These tests are PG-free, LLM-free, and never bring up a real docker
daemon.  Pure module-level contract checks.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend import web as web_pkg
from backend.web import preview_hmr_reload as phr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:

    def test_event_name_pinned(self):
        # Frontend SSE consumer switches on this literal — drift
        # breaks the iframe-reload-counter branch silently.
        assert phr.PREVIEW_HMR_RELOAD_EVENT_NAME == "preview.hmr_reload"

    def test_pipeline_phase_pinned(self):
        assert phr.PREVIEW_HMR_RELOAD_PIPELINE_PHASE == "preview_hmr_reload"

    def test_default_broadcast_scope_is_session(self):
        assert phr.PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE == "session"

    def test_default_label_is_human_readable(self):
        assert isinstance(phr.PREVIEW_HMR_RELOAD_DEFAULT_LABEL, str)
        assert phr.PREVIEW_HMR_RELOAD_DEFAULT_LABEL.strip()

    def test_default_change_kind_is_update(self):
        assert phr.PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND == "update"
        assert (
            phr.PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND
            in phr.PREVIEW_HMR_RELOAD_CHANGE_KINDS
        )

    def test_change_kinds_pinned(self):
        # The frozen enum the FE branches on for status icon /
        # severity rendering.  Drift here means the FE may receive a
        # change_kind value it cannot map to an icon.
        assert phr.PREVIEW_HMR_RELOAD_CHANGE_KINDS == (
            "update", "full-reload", "prune", "error-clear",
        )

    def test_max_workspace_id_bytes_pinned(self):
        assert phr.MAX_PREVIEW_HMR_RELOAD_WORKSPACE_ID_BYTES == 256

    def test_max_label_bytes_pinned(self):
        assert phr.MAX_PREVIEW_HMR_RELOAD_LABEL_BYTES == 120

    def test_max_change_kind_bytes_pinned(self):
        assert phr.MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES == 32

    def test_max_source_path_bytes_pinned(self):
        assert phr.MAX_PREVIEW_HMR_RELOAD_SOURCE_PATH_BYTES == 4096

    def test_max_edit_hash_bytes_pinned(self):
        assert phr.MAX_PREVIEW_HMR_RELOAD_EDIT_HASH_BYTES == 64

    def test_payload_dataclass_is_frozen(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-1")
        with pytest.raises((AttributeError, Exception)):
            payload.workspace_id = "ws-2"  # type: ignore[misc]

    def test_payload_default_label_is_constant(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-1")
        assert payload.label == phr.PREVIEW_HMR_RELOAD_DEFAULT_LABEL

    def test_payload_default_change_kind_is_constant(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-1")
        assert payload.change_kind == phr.PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND

    def test_error_subclasses_value_error(self):
        assert issubclass(phr.PreviewHmrReloadError, ValueError)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  Validation happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadHappyPaths:

    def test_full_payload(self):
        payload = phr.build_preview_hmr_reload_payload(
            workspace_id="ws-42",
            label="Preview updated: header bigger",
            change_kind="update",
            source_path="components/Header.tsx",
            edit_hash="ce1fa22faeff8fe8",
        )
        assert payload.workspace_id == "ws-42"
        assert payload.label == "Preview updated: header bigger"
        assert payload.change_kind == "update"
        assert payload.source_path == "components/Header.tsx"
        assert payload.edit_hash == "ce1fa22faeff8fe8"

    def test_label_defaults_when_omitted(self):
        payload = phr.build_preview_hmr_reload_payload(workspace_id="ws-42")
        assert payload.label == phr.PREVIEW_HMR_RELOAD_DEFAULT_LABEL

    def test_label_defaults_when_empty(self):
        payload = phr.build_preview_hmr_reload_payload(
            workspace_id="ws-42", label="",
        )
        assert payload.label == phr.PREVIEW_HMR_RELOAD_DEFAULT_LABEL

    def test_change_kind_defaults(self):
        payload = phr.build_preview_hmr_reload_payload(workspace_id="ws-42")
        assert payload.change_kind == phr.PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND

    def test_optional_fields_default_to_none(self):
        payload = phr.build_preview_hmr_reload_payload(workspace_id="ws-42")
        assert payload.source_path is None
        assert payload.edit_hash is None

    def test_full_reload_change_kind_accepted(self):
        payload = phr.build_preview_hmr_reload_payload(
            workspace_id="ws-42", change_kind="full-reload",
        )
        assert payload.change_kind == "full-reload"

    def test_error_clear_change_kind_accepted(self):
        # Sibling W16.6 hook says "the error is fixed" — must be in
        # the enum so the FE can render a green check rather than an
        # update toast.
        payload = phr.build_preview_hmr_reload_payload(
            workspace_id="ws-42", change_kind="error-clear",
        )
        assert payload.change_kind == "error-clear"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  Validation failure paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadFailures:

    def test_empty_workspace_id_raises(self):
        with pytest.raises(phr.PreviewHmrReloadError):
            phr.build_preview_hmr_reload_payload(workspace_id="")

    def test_non_string_workspace_id_raises(self):
        with pytest.raises(phr.PreviewHmrReloadError):
            phr.build_preview_hmr_reload_payload(
                workspace_id=42,  # type: ignore[arg-type]
            )

    def test_workspace_id_over_cap_raises(self):
        with pytest.raises(phr.PreviewHmrReloadError):
            phr.build_preview_hmr_reload_payload(
                workspace_id="x" * (
                    phr.MAX_PREVIEW_HMR_RELOAD_WORKSPACE_ID_BYTES + 1
                ),
            )

    def test_label_over_cap_raises(self):
        with pytest.raises(phr.PreviewHmrReloadError):
            phr.build_preview_hmr_reload_payload(
                workspace_id="ws-1",
                label="x" * (phr.MAX_PREVIEW_HMR_RELOAD_LABEL_BYTES + 1),
            )

    def test_unknown_change_kind_rejected(self):
        with pytest.raises(phr.PreviewHmrReloadError):
            phr.build_preview_hmr_reload_payload(
                workspace_id="ws-1", change_kind="garbage",
            )

    def test_source_path_over_cap_raises(self):
        with pytest.raises(phr.PreviewHmrReloadError):
            phr.build_preview_hmr_reload_payload(
                workspace_id="ws-1",
                source_path="x" * (
                    phr.MAX_PREVIEW_HMR_RELOAD_SOURCE_PATH_BYTES + 1
                ),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  to_event_data projection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToEventData:

    def test_minimal_payload_has_three_keys(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-1")
        data = payload.to_event_data()
        assert set(data.keys()) == {"workspace_id", "label", "change_kind"}

    def test_full_payload_has_all_keys(self):
        payload = phr.PreviewHmrReloadPayload(
            workspace_id="ws-1", label="custom",
            change_kind="full-reload",
            source_path="components/Header.tsx",
            edit_hash="aaaaaaaaaaaaaaaa",
        )
        data = payload.to_event_data()
        assert set(data.keys()) == {
            "workspace_id", "label", "change_kind",
            "source_path", "edit_hash",
        }

    def test_optional_none_keys_dropped(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-1")
        data = payload.to_event_data()
        assert "source_path" not in data
        assert "edit_hash" not in data

    def test_label_default_preserved_in_event_data(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-1")
        data = payload.to_event_data()
        assert data["label"] == phr.PREVIEW_HMR_RELOAD_DEFAULT_LABEL


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  build_chat_message_for_preview_hmr_reload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildChatMessage:

    def test_role_is_system(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-42")
        msg = phr.build_chat_message_for_preview_hmr_reload(payload)
        assert msg["role"] == "system"

    def test_text_uses_label(self):
        payload = phr.PreviewHmrReloadPayload(
            workspace_id="ws-42", label="Preview updated: header bigger",
        )
        msg = phr.build_chat_message_for_preview_hmr_reload(payload)
        assert msg["text"] == "Preview updated: header bigger"

    def test_preview_hmr_reload_carries_workspace_id(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-42")
        msg = phr.build_chat_message_for_preview_hmr_reload(payload)
        assert msg["previewHmrReload"]["workspaceId"] == "ws-42"

    def test_preview_hmr_reload_carries_change_kind(self):
        payload = phr.PreviewHmrReloadPayload(
            workspace_id="ws-42", change_kind="full-reload",
        )
        msg = phr.build_chat_message_for_preview_hmr_reload(payload)
        assert msg["previewHmrReload"]["changeKind"] == "full-reload"

    def test_optional_source_path_threaded_through(self):
        payload = phr.PreviewHmrReloadPayload(
            workspace_id="ws-42", source_path="components/Header.tsx",
        )
        msg = phr.build_chat_message_for_preview_hmr_reload(payload)
        assert msg["previewHmrReload"]["sourcePath"] == "components/Header.tsx"

    def test_optional_edit_hash_threaded_through(self):
        payload = phr.PreviewHmrReloadPayload(
            workspace_id="ws-42", edit_hash="ce1fa22faeff8fe8",
        )
        msg = phr.build_chat_message_for_preview_hmr_reload(payload)
        assert msg["previewHmrReload"]["editHash"] == "ce1fa22faeff8fe8"

    def test_message_id_threaded_through(self):
        payload = phr.PreviewHmrReloadPayload(workspace_id="ws-42")
        msg = phr.build_chat_message_for_preview_hmr_reload(
            payload, message_id="m-1",
        )
        assert msg["id"] == "m-1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  emit_preview_hmr_reload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmitPreviewHmrReload:

    def test_publishes_one_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from backend import events as events_mod
        captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **kwargs: Any) -> None:
            captured.append((event, dict(data), dict(kwargs)))

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        phr.emit_preview_hmr_reload(
            workspace_id="ws-42",
            broadcast_scope="session",
        )
        assert len(captured) == 1
        event, data, kwargs = captured[0]
        assert event == "preview.hmr_reload"
        assert data["workspace_id"] == "ws-42"
        assert data["label"] == phr.PREVIEW_HMR_RELOAD_DEFAULT_LABEL
        assert data["change_kind"] == phr.PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND
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
        phr.emit_preview_hmr_reload(workspace_id="ws-42")
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
        # A brand-new key like ``triggered_by`` rides through alongside
        # the frozen contract keys without colliding (``setdefault``
        # semantics — never overwrites the validated payload).
        phr.emit_preview_hmr_reload(
            workspace_id="ws-42",
            broadcast_scope="session",
            triggered_by="agent_edit",
        )
        assert len(captured) == 1
        data = captured[0]
        assert data["workspace_id"] == "ws-42"
        assert data["triggered_by"] == "agent_edit"

    def test_returns_validated_payload(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod

        monkeypatch.setattr(
            events_mod.EventBus, "publish",
            lambda *a, **k: None, raising=True,
        )
        payload = phr.emit_preview_hmr_reload(
            workspace_id="ws-42",
            broadcast_scope="session",
            change_kind="full-reload",
            edit_hash="ce1fa22faeff8fe8",
        )
        assert isinstance(payload, phr.PreviewHmrReloadPayload)
        assert payload.workspace_id == "ws-42"
        assert payload.change_kind == "full-reload"
        assert payload.edit_hash == "ce1fa22faeff8fe8"

    def test_propagates_validation_error(self) -> None:
        # Bad input must surface — we don't silently drop bad calls.
        with pytest.raises(phr.PreviewHmrReloadError):
            phr.emit_preview_hmr_reload(
                workspace_id="",
                broadcast_scope="session",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  Re-export sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W16_5_HMR_SYMBOLS = (
    "MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_EDIT_HASH_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_LABEL_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_SOURCE_PATH_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_WORKSPACE_ID_BYTES",
    "PREVIEW_HMR_RELOAD_CHANGE_KINDS",
    "PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND",
    "PREVIEW_HMR_RELOAD_DEFAULT_LABEL",
    "PREVIEW_HMR_RELOAD_EVENT_NAME",
    "PREVIEW_HMR_RELOAD_PIPELINE_PHASE",
    "PreviewHmrReloadError",
    "PreviewHmrReloadPayload",
    "build_chat_message_for_preview_hmr_reload",
    "build_preview_hmr_reload_payload",
    "emit_preview_hmr_reload",
)


@pytest.mark.parametrize("symbol", _W16_5_HMR_SYMBOLS)
def test_w16_5_preview_hmr_reload_symbol_re_exported(symbol: str) -> None:
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert getattr(web_pkg, symbol) is getattr(phr, symbol)


def test_w16_5_preview_hmr_reload_module_all_count() -> None:
    assert len(phr.__all__) == len(_W16_5_HMR_SYMBOLS)
    assert set(phr.__all__) == set(_W16_5_HMR_SYMBOLS)
