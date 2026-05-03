"""W16.6 — ``preview.vite_error`` / ``preview.vite_error_resolved`` SSE event tests.

Locks the public surface of ``backend.web.preview_vite_error`` so the
two W16.6 events stay binding for the orchestrator-chat SSE consumer
(``components/omnisight/workspace-chat.tsx``) and the W15.1 ingest
endpoint (``backend.routers.web_sandbox.report_preview_error``) that
fires ``emit_preview_vite_error(status="detected", ...)`` whenever a
fresh vite error lands.

Coverage axes
─────────────

  §A  Drift guards — every frozen wire-shape constant + bound cap +
      status enum + dataclass invariant.
  §B  Validation happy paths — full payload + label-defaulting +
      status-defaulting + optional-field elision.
  §C  Validation failure paths — empty workspace_id / over-cap label
      / unknown status / wrong types.
  §D  ``PreviewViteErrorPayload.to_event_data`` — drops ``None``
      keys, never bleeds extra keys, ``event_name`` dispatches by
      status.
  §E  ``build_chat_message_for_preview_vite_error`` — produces the
      WorkspaceChat shape, role=system, ``previewViteError`` carries
      the workspace id + status + optional fields.
  §F  ``format_preview_vite_error_detected_label`` — bilingual
      narrative with target / error_class substitution; default
      fallback when both absent; byte-cap clipping.
  §G  ``preview_vite_error_payload_from_history_entry`` — projects
      a W15.2-formatted entry to a payload, extracts file/line,
      falls back gracefully on degraded entries.
  §H  ``emit_preview_vite_error`` — publishes one event per status
      with the matching event name + default broadcast scope.
  §I  Re-export sweep — every public symbol surfaces from the
      ``backend.web`` package.

These tests are PG-free, LLM-free, and never bring up a real docker
daemon.  Pure module-level contract checks.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend import web as web_pkg
from backend.web import preview_vite_error as pve


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:

    def test_detected_event_name_pinned(self):
        assert pve.PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME == "preview.vite_error"

    def test_resolved_event_name_pinned(self):
        assert (
            pve.PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME
            == "preview.vite_error_resolved"
        )

    def test_pipeline_phase_pinned(self):
        assert pve.PREVIEW_VITE_ERROR_PIPELINE_PHASE == "preview_vite_error"

    def test_default_broadcast_scope_is_session(self):
        assert pve.PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE == "session"

    def test_status_detected_pinned(self):
        assert pve.PREVIEW_VITE_ERROR_STATUS_DETECTED == "detected"

    def test_status_resolved_pinned(self):
        assert pve.PREVIEW_VITE_ERROR_STATUS_RESOLVED == "resolved"

    def test_statuses_lifecycle_order(self):
        assert pve.PREVIEW_VITE_ERROR_STATUSES == ("detected", "resolved")

    def test_default_detected_label_row_spec_substring(self):
        # Row spec literal — frontend SSE consumer renders the chat
        # body straight from this constant when no per-error hint is
        # supplied; W16.9 e2e tests grep this substring.
        assert "正在修" in pve.PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL

    def test_default_resolved_label_row_spec(self):
        assert pve.PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL == "已修 ✓"

    def test_max_workspace_id_bytes_pinned(self):
        assert pve.MAX_PREVIEW_VITE_ERROR_WORKSPACE_ID_BYTES == 256

    def test_max_label_bytes_pinned(self):
        assert pve.MAX_PREVIEW_VITE_ERROR_LABEL_BYTES == 200

    def test_max_target_bytes_pinned(self):
        assert pve.MAX_PREVIEW_VITE_ERROR_TARGET_BYTES == 200

    def test_max_error_class_bytes_pinned(self):
        assert pve.MAX_PREVIEW_VITE_ERROR_ERROR_CLASS_BYTES == 32

    def test_max_error_signature_bytes_pinned(self):
        assert pve.MAX_PREVIEW_VITE_ERROR_ERROR_SIGNATURE_BYTES == 280

    def test_max_source_path_bytes_pinned(self):
        assert pve.MAX_PREVIEW_VITE_ERROR_SOURCE_PATH_BYTES == 4096

    def test_payload_dataclass_is_frozen(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-1")
        with pytest.raises((AttributeError, Exception)):
            payload.workspace_id = "ws-2"  # type: ignore[misc]

    def test_payload_default_status_is_detected(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-1")
        assert payload.status == pve.PREVIEW_VITE_ERROR_STATUS_DETECTED

    def test_error_subclasses_value_error(self):
        assert issubclass(pve.PreviewViteErrorError, ValueError)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  Validation happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadHappyPaths:

    def test_full_payload_detected(self):
        payload = pve.build_preview_vite_error_payload(
            workspace_id="ws-42",
            status="detected",
            label="我看到 src/Header.tsx 有 syntax_error，正在修…",
            error_class="syntax_error",
            target="src/Header.tsx",
            error_signature="vite[transform] src/Header.tsx:42: compile:",
            source_path="src/Header.tsx",
            source_line=42,
        )
        assert payload.workspace_id == "ws-42"
        assert payload.status == "detected"
        assert payload.error_class == "syntax_error"
        assert payload.target == "src/Header.tsx"
        assert payload.source_line == 42

    def test_full_payload_resolved(self):
        payload = pve.build_preview_vite_error_payload(
            workspace_id="ws-42",
            status="resolved",
            error_signature="vite[transform] src/Header.tsx:42: compile:",
        )
        assert payload.status == "resolved"
        assert payload.label == pve.PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL
        assert payload.error_signature == "vite[transform] src/Header.tsx:42: compile:"

    def test_status_defaults_to_detected(self):
        payload = pve.build_preview_vite_error_payload(workspace_id="ws-1")
        assert payload.status == pve.PREVIEW_VITE_ERROR_STATUS_DETECTED

    def test_label_defaults_for_detected(self):
        payload = pve.build_preview_vite_error_payload(workspace_id="ws-1")
        assert payload.label == pve.PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL

    def test_label_defaults_for_resolved(self):
        payload = pve.build_preview_vite_error_payload(
            workspace_id="ws-1", status="resolved",
        )
        assert payload.label == pve.PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL

    def test_label_empty_falls_back_to_default(self):
        payload = pve.build_preview_vite_error_payload(
            workspace_id="ws-1", label="",
        )
        assert payload.label == pve.PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL

    def test_optional_fields_default_to_none(self):
        payload = pve.build_preview_vite_error_payload(workspace_id="ws-1")
        assert payload.error_class is None
        assert payload.target is None
        assert payload.error_signature is None
        assert payload.source_path is None
        assert payload.source_line is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  Validation failure paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadFailures:

    def test_empty_workspace_id_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(workspace_id="")

    def test_non_string_workspace_id_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id=42,  # type: ignore[arg-type]
            )

    def test_workspace_id_over_cap_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="x" * (
                    pve.MAX_PREVIEW_VITE_ERROR_WORKSPACE_ID_BYTES + 1
                ),
            )

    def test_label_over_cap_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="ws-1",
                label="x" * (pve.MAX_PREVIEW_VITE_ERROR_LABEL_BYTES + 1),
            )

    def test_unknown_status_rejected(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="ws-1", status="garbage",
            )

    def test_target_over_cap_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="ws-1",
                target="x" * (pve.MAX_PREVIEW_VITE_ERROR_TARGET_BYTES + 1),
            )

    def test_error_class_over_cap_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="ws-1",
                error_class="x" * (
                    pve.MAX_PREVIEW_VITE_ERROR_ERROR_CLASS_BYTES + 1
                ),
            )

    def test_negative_source_line_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="ws-1", source_line=-3,
            )

    def test_non_int_source_line_raises(self):
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="ws-1", source_line="42",  # type: ignore[arg-type]
            )

    def test_bool_source_line_rejected(self):
        # bool is a subclass of int in Python — explicit guard so a
        # misplaced True/False doesn't slip through as line=1 / line=0.
        with pytest.raises(pve.PreviewViteErrorError):
            pve.build_preview_vite_error_payload(
                workspace_id="ws-1", source_line=True,  # type: ignore[arg-type]
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  to_event_data + event_name dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToEventData:

    def test_minimal_payload_three_keys(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-1")
        data = payload.to_event_data()
        assert set(data.keys()) == {"workspace_id", "status", "label"}

    def test_full_payload_all_keys(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-1",
            status="detected",
            label="custom",
            error_class="syntax_error",
            target="src/App.tsx",
            error_signature="vite[transform] src/App.tsx:42: compile:",
            source_path="src/App.tsx",
            source_line=42,
        )
        data = payload.to_event_data()
        assert set(data.keys()) == {
            "workspace_id", "status", "label",
            "error_class", "target", "error_signature",
            "source_path", "source_line",
        }

    def test_optional_none_keys_dropped(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-1")
        data = payload.to_event_data()
        for k in ("error_class", "target", "error_signature",
                  "source_path", "source_line"):
            assert k not in data

    def test_event_name_dispatch_detected(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-1")
        assert payload.event_name() == "preview.vite_error"

    def test_event_name_dispatch_resolved(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-1", status="resolved",
        )
        assert payload.event_name() == "preview.vite_error_resolved"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  build_chat_message_for_preview_vite_error
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildChatMessage:

    def test_role_is_system(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-42")
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert msg["role"] == "system"

    def test_text_uses_label(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-42",
            label="我看到 src/App.tsx 有 syntax_error，正在修…",
        )
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert msg["text"] == "我看到 src/App.tsx 有 syntax_error，正在修…"

    def test_preview_vite_error_carries_workspace_id(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-42")
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert msg["previewViteError"]["workspaceId"] == "ws-42"

    def test_preview_vite_error_carries_status(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-42", status="resolved",
        )
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert msg["previewViteError"]["status"] == "resolved"

    def test_optional_error_class_threaded_through(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-42", error_class="syntax_error",
        )
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert msg["previewViteError"]["errorClass"] == "syntax_error"

    def test_optional_target_threaded_through(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-42", target="src/App.tsx",
        )
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert msg["previewViteError"]["target"] == "src/App.tsx"

    def test_optional_signature_threaded_through(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-42",
            error_signature="vite[transform] src/App.tsx:42: compile:",
        )
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert (
            msg["previewViteError"]["errorSignature"]
            == "vite[transform] src/App.tsx:42: compile:"
        )

    def test_optional_source_threaded_through(self):
        payload = pve.PreviewViteErrorPayload(
            workspace_id="ws-42",
            source_path="src/App.tsx", source_line=42,
        )
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        assert msg["previewViteError"]["sourcePath"] == "src/App.tsx"
        assert msg["previewViteError"]["sourceLine"] == 42

    def test_message_id_threaded_through(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-42")
        msg = pve.build_chat_message_for_preview_vite_error(
            payload, message_id="m-42",
        )
        assert msg["id"] == "m-42"

    def test_optional_keys_omitted_when_unset(self):
        payload = pve.PreviewViteErrorPayload(workspace_id="ws-42")
        msg = pve.build_chat_message_for_preview_vite_error(payload)
        for k in ("errorClass", "target", "errorSignature",
                  "sourcePath", "sourceLine"):
            assert k not in msg["previewViteError"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  format_preview_vite_error_detected_label
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFormatLabel:

    def test_default_when_both_omitted(self):
        out = pve.format_preview_vite_error_detected_label()
        assert out == pve.PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL

    def test_target_only(self):
        out = pve.format_preview_vite_error_detected_label(
            target="src/Header.tsx",
        )
        assert "src/Header.tsx" in out
        assert "正在修" in out

    def test_target_and_class_no_double_error_word(self):
        # error_class already ending in "error" (e.g. "syntax_error")
        # must NOT print "syntax_error error" (redundant).
        out = pve.format_preview_vite_error_detected_label(
            target="src/Header.tsx", error_class="syntax_error",
        )
        assert "syntax_error error" not in out
        assert "syntax_error" in out
        assert "正在修" in out

    def test_target_and_non_error_suffix_class(self):
        # When the class identifier doesn't end in "error" the label
        # appends " error" so the prose reads "ABC error".
        out = pve.format_preview_vite_error_detected_label(
            target="src/Header.tsx", error_class="parse_failure",
        )
        assert "parse_failure error" in out

    def test_class_only(self):
        # No target → falls back to "preview" as the target.
        out = pve.format_preview_vite_error_detected_label(
            error_class="syntax_error",
        )
        assert "preview" in out
        assert "syntax_error" in out

    def test_byte_cap_clipping(self):
        # Pathologically long target — output must stay within
        # MAX_PREVIEW_VITE_ERROR_LABEL_BYTES.
        long_target = "x" * 1000
        out = pve.format_preview_vite_error_detected_label(target=long_target)
        assert len(out.encode("utf-8")) <= pve.MAX_PREVIEW_VITE_ERROR_LABEL_BYTES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  preview_vite_error_payload_from_history_entry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestProjectionFromHistoryEntry:

    def test_non_vite_entry_returns_none(self):
        out = pve.preview_vite_error_payload_from_history_entry(
            "tool[compile] foo:1: error", workspace_id="ws-1",
        )
        assert out is None

    def test_non_string_entry_returns_none(self):
        out = pve.preview_vite_error_payload_from_history_entry(
            None, workspace_id="ws-1",  # type: ignore[arg-type]
        )
        assert out is None

    def test_extracts_target_and_line(self):
        entry = "vite[transform] src/App.tsx:42: compile: Failed to parse module"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1",
        )
        assert out is not None
        assert out.target == "src/App.tsx"
        assert out.source_path == "src/App.tsx"
        assert out.source_line == 42

    def test_extracts_signature_head_only(self):
        entry = "vite[transform] src/App.tsx:42: compile: Failed to parse module"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1",
        )
        assert out is not None
        assert out.error_signature == "vite[transform] src/App.tsx:42: compile:"

    def test_extracts_error_class_via_classifier(self):
        entry = "vite[transform] src/App.tsx:42: compile: Failed to parse module"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1",
        )
        assert out is not None
        assert out.error_class == "syntax_error"

    def test_unclassified_entry_uses_token(self):
        # Generic vite entry the W15.6 classifier doesn't bucket.
        entry = "vite[client] <no-file>:?: runtime: <unknown>"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1",
        )
        assert out is not None
        assert out.error_class == "unclassified"

    def test_no_file_token_skips_target(self):
        entry = "vite[client] <no-file>:?: runtime: ReferenceError: x is not defined"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1",
        )
        assert out is not None
        assert out.target is None
        assert out.source_path is None

    def test_question_line_token_skips_source_line(self):
        entry = "vite[hmr] src/Card.tsx:?: runtime: x is not defined"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1",
        )
        assert out is not None
        assert out.source_line is None

    def test_resolved_status_uses_resolved_label(self):
        entry = "vite[transform] src/App.tsx:42: compile: Failed to parse module"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1", status="resolved",
        )
        assert out is not None
        assert out.label == pve.PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL
        assert out.status == "resolved"

    def test_classifier_override_threaded_through(self):
        # Pass a stub classifier so the projection's classifier
        # contract stays decoupled from W15.6's identity.
        seen: list[str] = []

        def fake_classify(entry: str) -> str | None:
            seen.append(entry)
            return "import_path_typo"

        entry = "vite[transform] src/App.tsx:42: compile: x is not defined"
        out = pve.preview_vite_error_payload_from_history_entry(
            entry, workspace_id="ws-1", classify=fake_classify,
        )
        assert out is not None
        assert out.error_class == "import_path_typo"
        assert seen == [entry]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §H  emit_preview_vite_error
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmitPreviewViteError:

    def test_publishes_detected_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from backend import events as events_mod
        captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **kwargs: Any) -> None:
            captured.append((event, dict(data), dict(kwargs)))

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        pve.emit_preview_vite_error(
            workspace_id="ws-42",
            broadcast_scope="session",
        )
        assert len(captured) == 1
        event, data, kwargs = captured[0]
        assert event == "preview.vite_error"
        assert data["workspace_id"] == "ws-42"
        assert data["status"] == "detected"
        assert kwargs["broadcast_scope"] == "session"

    def test_publishes_resolved_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from backend import events as events_mod
        captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **kwargs: Any) -> None:
            captured.append((event, dict(data), dict(kwargs)))

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        pve.emit_preview_vite_error(
            workspace_id="ws-42",
            status="resolved",
            broadcast_scope="session",
        )
        assert len(captured) == 1
        event, data, _ = captured[0]
        assert event == "preview.vite_error_resolved"
        assert data["status"] == "resolved"
        assert data["label"] == pve.PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL

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
        pve.emit_preview_vite_error(workspace_id="ws-42")
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
        # Brand-new extra rides through alongside frozen contract
        # keys without colliding (``setdefault`` semantics — never
        # overwrites the validated payload).
        pve.emit_preview_vite_error(
            workspace_id="ws-42",
            broadcast_scope="session",
            triggered_by="vite_plugin",
        )
        assert len(captured) == 1
        data = captured[0]
        assert data["workspace_id"] == "ws-42"
        assert data["triggered_by"] == "vite_plugin"

    def test_returns_validated_payload(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod

        monkeypatch.setattr(
            events_mod.EventBus, "publish",
            lambda *a, **k: None, raising=True,
        )
        payload = pve.emit_preview_vite_error(
            workspace_id="ws-42",
            broadcast_scope="session",
            error_class="syntax_error",
            target="src/App.tsx",
        )
        assert isinstance(payload, pve.PreviewViteErrorPayload)
        assert payload.workspace_id == "ws-42"
        assert payload.error_class == "syntax_error"
        assert payload.target == "src/App.tsx"

    def test_propagates_validation_error(self) -> None:
        # Bad input must surface — we don't silently drop bad calls.
        with pytest.raises(pve.PreviewViteErrorError):
            pve.emit_preview_vite_error(
                workspace_id="",
                broadcast_scope="session",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §I  Re-export sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W16_6_SYMBOLS = (
    "MAX_PREVIEW_VITE_ERROR_ERROR_CLASS_BYTES",
    "MAX_PREVIEW_VITE_ERROR_ERROR_SIGNATURE_BYTES",
    "MAX_PREVIEW_VITE_ERROR_LABEL_BYTES",
    "MAX_PREVIEW_VITE_ERROR_SOURCE_PATH_BYTES",
    "MAX_PREVIEW_VITE_ERROR_TARGET_BYTES",
    "MAX_PREVIEW_VITE_ERROR_WORKSPACE_ID_BYTES",
    "PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL",
    "PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL",
    "PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME",
    "PREVIEW_VITE_ERROR_PIPELINE_PHASE",
    "PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME",
    "PREVIEW_VITE_ERROR_STATUSES",
    "PREVIEW_VITE_ERROR_STATUS_DETECTED",
    "PREVIEW_VITE_ERROR_STATUS_RESOLVED",
    "PreviewViteErrorError",
    "PreviewViteErrorPayload",
    "build_chat_message_for_preview_vite_error",
    "build_preview_vite_error_payload",
    "emit_preview_vite_error",
    "format_preview_vite_error_detected_label",
    "preview_vite_error_payload_from_history_entry",
)


@pytest.mark.parametrize("symbol", _W16_6_SYMBOLS)
def test_w16_6_symbol_re_exported(symbol: str) -> None:
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert getattr(web_pkg, symbol) is getattr(pve, symbol)


def test_w16_6_module_all_count() -> None:
    assert len(pve.__all__) == len(_W16_6_SYMBOLS)
    assert set(pve.__all__) == set(_W16_6_SYMBOLS)


def test_total_re_export_count_matches_w16_6_baseline() -> None:
    # Bumped from 374 (W16.5 baseline) → 396 (W16.6 +22 preview_vite_error).
    assert len(web_pkg.__all__) == 396
