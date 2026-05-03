"""W16.4 — Inline-preview iframe SSE event contract tests.

Locks the public surface of ``backend.web.web_preview_ready`` so the
W16.4 ``preview.ready`` event stays binding for the orchestrator-chat
SSE consumer (``components/omnisight/workspace-chat.tsx``) and the
W16.5 edit-while-preview row that re-uses the same event after an
HMR-driven rebuild.

Coverage axes
─────────────

  §A  Drift guards — every frozen wire-shape constant + bound cap +
      dataclass invariant.
  §B  Validation happy paths — full payload + label-defaulting +
      optional-field elision.
  §C  Validation failure paths — empty workspace_id / over-cap URL /
      bad host_port / wrong type.
  §D  ``PreviewReadyPayload.to_event_data`` — drops ``None`` keys,
      preserves ``label`` even at default, never bleeds extra keys.
  §E  ``preview_ready_payload_from_instance_dict`` — prefers ingress,
      falls back to host-port, returns ``None`` when no URL is usable.
  §F  ``build_chat_message_for_preview_ready`` — produces the
      WorkspaceChat shape, role=system, ``previewEmbed`` carries the
      sandbox URL.
  §G  ``emit_preview_ready`` — publishes one ``preview.ready`` event
      with the validated payload, default broadcast scope, never
      raises on transport failure.
  §H  Router wiring — ``POST /web-sandbox/preview/{ws}/ready`` fires
      ``emit_preview_ready`` once per running transition.
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
from backend.web import web_preview_ready as wpr
from backend.web_sandbox import WebSandboxManager, WebSandboxStatus


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:

    def test_event_name_pinned(self):
        # Frontend SSE consumer switches on this literal — drift breaks
        # the iframe-mounting branch silently.
        assert wpr.PREVIEW_READY_EVENT_NAME == "preview.ready"

    def test_pipeline_phase_pinned(self):
        assert wpr.PREVIEW_READY_PIPELINE_PHASE == "preview_ready"

    def test_default_broadcast_scope_is_session(self):
        # Operator-specific URL — broadcasting globally exposes a URL
        # other tenants cannot open (CF Access SSO gates ingress to the
        # launching operator's email).
        assert wpr.PREVIEW_READY_DEFAULT_BROADCAST_SCOPE == "session"

    def test_default_label_is_human_readable_string(self):
        assert isinstance(wpr.PREVIEW_READY_DEFAULT_LABEL, str)
        assert wpr.PREVIEW_READY_DEFAULT_LABEL.strip()

    def test_max_workspace_id_bytes_pinned(self):
        assert wpr.MAX_PREVIEW_READY_WORKSPACE_ID_BYTES == 256

    def test_max_url_bytes_pinned(self):
        assert wpr.MAX_PREVIEW_READY_URL_BYTES == 4096

    def test_max_label_bytes_pinned(self):
        assert wpr.MAX_PREVIEW_READY_LABEL_BYTES == 120

    def test_max_sandbox_id_bytes_pinned(self):
        assert wpr.MAX_PREVIEW_READY_SANDBOX_ID_BYTES == 64

    def test_max_ingress_url_bytes_pinned(self):
        assert wpr.MAX_PREVIEW_READY_INGRESS_URL_BYTES == 4096

    def test_payload_dataclass_is_frozen(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-1", preview_url="http://x:5173",
        )
        with pytest.raises((AttributeError, Exception)):
            payload.workspace_id = "ws-2"  # type: ignore[misc]

    def test_payload_dataclass_default_label_is_constant(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-1", preview_url="http://x:5173",
        )
        assert payload.label == wpr.PREVIEW_READY_DEFAULT_LABEL

    def test_preview_ready_error_subclasses_value_error(self):
        assert issubclass(wpr.PreviewReadyError, ValueError)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  Validation happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadHappyPaths:

    def test_full_payload(self):
        payload = wpr.build_preview_ready_payload(
            workspace_id="ws-42",
            preview_url="https://preview-abc.example.com",
            label="Landing page ready",
            sandbox_id="ws-abcdef0123456789",
            ingress_url="https://preview-abc.example.com",
            host_port=5173,
        )
        assert payload.workspace_id == "ws-42"
        assert payload.preview_url == "https://preview-abc.example.com"
        assert payload.label == "Landing page ready"
        assert payload.sandbox_id == "ws-abcdef0123456789"
        assert payload.ingress_url == "https://preview-abc.example.com"
        assert payload.host_port == 5173

    def test_label_defaults_when_omitted(self):
        payload = wpr.build_preview_ready_payload(
            workspace_id="ws-42",
            preview_url="http://localhost:5173",
        )
        assert payload.label == wpr.PREVIEW_READY_DEFAULT_LABEL

    def test_label_defaults_when_empty_string(self):
        payload = wpr.build_preview_ready_payload(
            workspace_id="ws-42",
            preview_url="http://localhost:5173",
            label="",
        )
        assert payload.label == wpr.PREVIEW_READY_DEFAULT_LABEL

    def test_optional_fields_default_to_none(self):
        payload = wpr.build_preview_ready_payload(
            workspace_id="ws-42",
            preview_url="http://localhost:5173",
        )
        assert payload.sandbox_id is None
        assert payload.ingress_url is None
        assert payload.host_port is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  Validation failure paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildPayloadFailures:

    def test_empty_workspace_id_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="", preview_url="http://x:5173",
            )

    def test_non_string_workspace_id_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id=42,  # type: ignore[arg-type]
                preview_url="http://x:5173",
            )

    def test_workspace_id_over_cap_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="x" * (wpr.MAX_PREVIEW_READY_WORKSPACE_ID_BYTES + 1),
                preview_url="http://x:5173",
            )

    def test_empty_preview_url_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="ws-1", preview_url="",
            )

    def test_url_over_cap_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="ws-1",
                preview_url="http://x/" + "a" * wpr.MAX_PREVIEW_READY_URL_BYTES,
            )

    def test_label_over_cap_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="ws-1", preview_url="http://x:5173",
                label="x" * (wpr.MAX_PREVIEW_READY_LABEL_BYTES + 1),
            )

    def test_host_port_out_of_range_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="ws-1", preview_url="http://x:5173",
                host_port=99999,
            )

    def test_host_port_zero_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="ws-1", preview_url="http://x:5173",
                host_port=0,
            )

    def test_host_port_bool_rejected(self):
        # ``True`` is an int subclass — defensive guard avoids accidental
        # ``bool`` slipping through.
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="ws-1", preview_url="http://x:5173",
                host_port=True,  # type: ignore[arg-type]
            )

    def test_host_port_non_int_raises(self):
        with pytest.raises(wpr.PreviewReadyError):
            wpr.build_preview_ready_payload(
                workspace_id="ws-1", preview_url="http://x:5173",
                host_port="5173",  # type: ignore[arg-type]
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  to_event_data projection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToEventData:

    def test_minimal_payload_has_three_keys(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-1", preview_url="http://x:5173",
        )
        data = payload.to_event_data()
        assert set(data.keys()) == {"workspace_id", "preview_url", "label"}

    def test_full_payload_has_all_keys(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-1", preview_url="https://x.example.com",
            label="custom", sandbox_id="ws-abc",
            ingress_url="https://x.example.com", host_port=5173,
        )
        data = payload.to_event_data()
        assert set(data.keys()) == {
            "workspace_id", "preview_url", "label",
            "sandbox_id", "ingress_url", "host_port",
        }

    def test_optional_none_keys_dropped(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-1", preview_url="http://x:5173",
            sandbox_id=None, ingress_url=None, host_port=None,
        )
        data = payload.to_event_data()
        assert "sandbox_id" not in data
        assert "ingress_url" not in data
        assert "host_port" not in data

    def test_label_default_preserved_in_event_data(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-1", preview_url="http://x:5173",
        )
        data = payload.to_event_data()
        assert data["label"] == wpr.PREVIEW_READY_DEFAULT_LABEL


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  preview_ready_payload_from_instance_dict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPayloadFromInstanceDict:

    def test_prefers_ingress_url_over_host_port_url(self):
        instance = {
            "workspace_id": "ws-42",
            "sandbox_id": "ws-abc",
            "preview_url": "http://localhost:5173",
            "ingress_url": "https://preview-abc.example.com",
            "host_port": 5173,
        }
        payload = wpr.preview_ready_payload_from_instance_dict(instance)
        assert payload is not None
        assert payload.preview_url == "https://preview-abc.example.com"

    def test_falls_back_to_host_port_url_when_no_ingress(self):
        instance = {
            "workspace_id": "ws-42",
            "sandbox_id": "ws-abc",
            "preview_url": "http://localhost:5173",
            "ingress_url": None,
            "host_port": 5173,
        }
        payload = wpr.preview_ready_payload_from_instance_dict(instance)
        assert payload is not None
        assert payload.preview_url == "http://localhost:5173"
        assert payload.host_port == 5173

    def test_returns_none_when_no_workspace_id(self):
        assert wpr.preview_ready_payload_from_instance_dict({}) is None

    def test_returns_none_when_no_url_available(self):
        instance = {
            "workspace_id": "ws-42",
            "preview_url": None,
            "ingress_url": None,
            "host_port": None,
        }
        assert wpr.preview_ready_payload_from_instance_dict(instance) is None

    def test_returns_none_when_workspace_id_is_empty_string(self):
        assert wpr.preview_ready_payload_from_instance_dict(
            {"workspace_id": "", "preview_url": "http://x:5173"},
        ) is None

    def test_passes_through_label_kwarg(self):
        instance = {
            "workspace_id": "ws-42",
            "preview_url": "http://localhost:5173",
        }
        payload = wpr.preview_ready_payload_from_instance_dict(
            instance, label="Landing live",
        )
        assert payload is not None
        assert payload.label == "Landing live"

    def test_drops_ingress_url_when_identical_to_chosen_url(self):
        # When ingress_url == preview_url chosen, we don't double-emit.
        instance = {
            "workspace_id": "ws-42",
            "preview_url": "https://x.example.com",
            "ingress_url": "https://x.example.com",
        }
        payload = wpr.preview_ready_payload_from_instance_dict(instance)
        assert payload is not None
        assert payload.preview_url == "https://x.example.com"
        assert payload.ingress_url is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  build_chat_message_for_preview_ready
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildChatMessage:

    def test_role_is_system(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-42", preview_url="https://x.example.com",
        )
        msg = wpr.build_chat_message_for_preview_ready(payload)
        assert msg["role"] == "system"

    def test_text_uses_label(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-42", preview_url="https://x.example.com",
            label="Landing page ready",
        )
        msg = wpr.build_chat_message_for_preview_ready(payload)
        assert msg["text"] == "Landing page ready"

    def test_preview_embed_carries_url_and_workspace_id(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-42", preview_url="https://x.example.com",
        )
        msg = wpr.build_chat_message_for_preview_ready(payload)
        assert msg["previewEmbed"]["url"] == "https://x.example.com"
        assert msg["previewEmbed"]["workspaceId"] == "ws-42"

    def test_preview_embed_carries_label(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-42", preview_url="https://x.example.com",
            label="Landing page ready",
        )
        msg = wpr.build_chat_message_for_preview_ready(payload)
        assert msg["previewEmbed"]["label"] == "Landing page ready"

    def test_message_id_threaded_through(self):
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-42", preview_url="https://x.example.com",
        )
        msg = wpr.build_chat_message_for_preview_ready(
            payload, message_id="m-1",
        )
        assert msg["id"] == "m-1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  emit_preview_ready
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmitPreviewReady:

    def test_publishes_one_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from backend import events as events_mod
        captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def _fake_publish(self: Any, event: str, data: dict[str, Any],
                          **kwargs: Any) -> None:
            captured.append((event, dict(data), dict(kwargs)))

        monkeypatch.setattr(
            events_mod.EventBus, "publish", _fake_publish, raising=True,
        )
        wpr.emit_preview_ready(
            workspace_id="ws-42",
            preview_url="https://x.example.com",
            broadcast_scope="session",
        )
        assert len(captured) == 1
        event, data, kwargs = captured[0]
        assert event == "preview.ready"
        assert data["workspace_id"] == "ws-42"
        assert data["preview_url"] == "https://x.example.com"
        assert data["label"] == wpr.PREVIEW_READY_DEFAULT_LABEL
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
        wpr.emit_preview_ready(
            workspace_id="ws-42",
            preview_url="https://x.example.com",
        )
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
        # A brand-new key like ``status_message`` rides through alongside
        # the frozen contract keys without colliding (``setdefault``
        # semantics — never overwrites the validated payload).
        wpr.emit_preview_ready(
            workspace_id="ws-42",
            preview_url="https://x.example.com",
            broadcast_scope="session",
            status_message="dev server up",
        )
        assert len(captured) == 1
        data = captured[0]
        assert data["workspace_id"] == "ws-42"
        assert data["preview_url"] == "https://x.example.com"
        assert data["status_message"] == "dev server up"

    def test_extras_cannot_clobber_frozen_keys(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Direct ``setdefault`` invariant — even if a caller hand-builds
        # ``data`` and emits via the lower-level path, the frozen keys
        # land first.  We assert via a payload + dict-merge round-trip
        # so we don't have to bypass the kwarg signature.
        payload = wpr.PreviewReadyPayload(
            workspace_id="ws-42", preview_url="https://x.example.com",
        )
        data = payload.to_event_data()
        # Simulate a buggy caller trying to retroactively change the
        # validated url via setdefault — it must NOT replace the canon.
        data.setdefault("preview_url", "https://wrong.example.com")
        assert data["preview_url"] == "https://x.example.com"

    def test_returns_validated_payload(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod

        monkeypatch.setattr(
            events_mod.EventBus, "publish",
            lambda *a, **k: None, raising=True,
        )
        payload = wpr.emit_preview_ready(
            workspace_id="ws-42",
            preview_url="https://x.example.com",
            broadcast_scope="session",
            sandbox_id="ws-abc",
            host_port=5173,
        )
        assert isinstance(payload, wpr.PreviewReadyPayload)
        assert payload.workspace_id == "ws-42"
        assert payload.host_port == 5173

    def test_propagates_validation_error(self) -> None:
        # Bad input must surface — we don't silently drop bad calls.
        with pytest.raises(wpr.PreviewReadyError):
            wpr.emit_preview_ready(
                workspace_id="",
                preview_url="https://x.example.com",
                broadcast_scope="session",
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

    def test_post_ready_emits_preview_ready_event(
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

        # Launch (installing) → mark ready (running) on the same workspace.
        launch = client.post(
            "/web-sandbox/preview",
            json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
        )
        assert launch.status_code == 200, launch.text
        ready = client.post("/web-sandbox/preview/ws-42/ready")
        assert ready.status_code == 200, ready.text
        body = ready.json()
        assert body["status"] == WebSandboxStatus.running.value

        preview_events = [
            (event, data) for (event, data) in captured
            if event == wpr.PREVIEW_READY_EVENT_NAME
        ]
        assert len(preview_events) == 1, preview_events
        _, data = preview_events[0]
        assert data["workspace_id"] == "ws-42"
        assert data["preview_url"]
        assert data["label"]

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
        assert wpr.PREVIEW_READY_EVENT_NAME not in captured

    def test_idempotent_ready_emits_once_per_call(
        self, client: TestClient, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The W14.2 ``mark_ready`` is idempotent — already-running keeps
        # the same status. We still emit a fresh SSE event so the FE can
        # re-mount the iframe (matches W16.5's edit-while-preview reuse).
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
        preview_count = sum(
            1 for e in captured if e == wpr.PREVIEW_READY_EVENT_NAME
        )
        assert preview_count == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §I  Re-export sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W16_4_SYMBOLS = (
    "MAX_PREVIEW_READY_INGRESS_URL_BYTES",
    "MAX_PREVIEW_READY_LABEL_BYTES",
    "MAX_PREVIEW_READY_SANDBOX_ID_BYTES",
    "MAX_PREVIEW_READY_URL_BYTES",
    "MAX_PREVIEW_READY_WORKSPACE_ID_BYTES",
    "PREVIEW_READY_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_READY_DEFAULT_LABEL",
    "PREVIEW_READY_EVENT_NAME",
    "PREVIEW_READY_PIPELINE_PHASE",
    "PreviewReadyError",
    "PreviewReadyPayload",
    "build_chat_message_for_preview_ready",
    "build_preview_ready_payload",
    "emit_preview_ready",
    "preview_ready_payload_from_instance_dict",
)


@pytest.mark.parametrize("symbol", _W16_4_SYMBOLS)
def test_w16_4_symbol_re_exported(symbol: str) -> None:
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert getattr(web_pkg, symbol) is getattr(wpr, symbol)


def test_total_re_export_count_matches_w16_4_baseline() -> None:
    # Bumped from 330 (W16.3 baseline) → 345 (W16.4 +15
    # web_preview_ready) → 374 (W16.5 +13 edit_intent + 16
    # preview_hmr_reload) → 396 (W16.6 +22 preview_vite_error) →
    # 426 (W16.7 +30 preview_next_steps).
    # Drift here means the W16 epic added/removed surface without the
    # lock-step bump landing across all neighbour test files.
    assert len(web_pkg.__all__) == 426


def test_w16_4_symbol_count_matches_module_all() -> None:
    # The module's own ``__all__`` should be exactly the 15 W16.4 symbols.
    assert len(wpr.__all__) == len(_W16_4_SYMBOLS)
    assert set(wpr.__all__) == set(_W16_4_SYMBOLS)
