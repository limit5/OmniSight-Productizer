"""Q.4 #298 checkbox 2 — ``emit_*`` helper scope-parameter enforcement.

Scope (pun intended):
  - Every ``emit_*`` helper in ``backend/events.py``,
    ``backend/orchestration_observability.py`` and
    ``backend/ui_sandbox_sse.py`` must accept a ``broadcast_scope``
    kwarg that defaults to ``None``.
  - Calling without ``broadcast_scope`` (or with ``None``) logs a
    deprecation warning *once* per helper and falls back to the
    helper's legacy scope value (the ``"global"`` / ``"user"`` /
    ``"session"`` / ``"tenant"`` the helper used to hard-code).
  - Passing an explicit scope silences the warning and the emitted
    payload carries that scope verbatim in ``_broadcast_scope``.
  - Setting ``OMNISIGHT_SSE_SCOPE_STRICT=1`` turns the warning path
    into ``raise TypeError`` — previews the next-release behaviour
    so CI can opt in early.

This file is the contract test for the current release's grace
period. Checkbox 4 of the Q.4 TODO row owns the AST-walking
``test_event_scope_declared`` (lint every call site), which is a
separate test — this one just pins the helper signatures + the
resolution logic.

Audit evidence: ``docs/design/multi-device-state-sync.md`` §6.3
acceptance hook.
"""

from __future__ import annotations

import inspect
import json
import logging
import os

import pytest

from backend import events as events_mod
from backend import orchestration_observability as obs_mod
from backend import ui_sandbox_sse as ui_mod


# ─── Per-helper expectation table ────────────────────────────────────
# (module, helper_name, legacy_default, minimal_call_kwargs)
# ``minimal_call_kwargs`` provides just enough arguments for the
# helper to reach ``bus.publish`` without raising on a missing
# required positional.

_EVENTS_TABLE: list[tuple[str, str, dict]] = [
    ("emit_agent_update",             "global", dict(agent_id="a1", status="running")),
    ("emit_task_update",              "global", dict(task_id="t1", status="done")),
    ("emit_tool_progress",            "global", dict(tool_name="grep", phase="start")),
    ("emit_pipeline_phase",           "global", dict(phase="plan")),
    ("emit_workspace",                "global", dict(agent_id="a1", action="create")),
    ("emit_container",                "global", dict(agent_id="a1", action="start")),
    ("emit_invoke",                   "global", dict(action_type="dispatch")),
    ("emit_token_warning",            "user",   dict(level="warn", message="80%")),
    ("emit_simulation",               "global", dict(sim_id="s1", action="start")),
    ("emit_agent_entropy",            "global", dict(agent_id="a1", entropy_score=0.1, verdict="ok")),
    ("emit_agent_scratchpad_saved",   "global", dict(agent_id="a1", turn=1, size_bytes=10, sections_count=1)),
    ("emit_agent_token_continuation", "global", dict(agent_id="a1")),
    ("emit_debug_finding",            "global", dict(task_id="t1", agent_id="a1", finding_type="stuck", severity="warn", message="x")),
    ("emit_workflow_updated",         "user",   dict(run_id="r1", status="done", version=1)),
    ("emit_notification_read",        "user",   dict(notification_id="n1", user_id="u1")),
    ("emit_preferences_updated",      "user",   dict(pref_key="theme", value="dark", user_id="u1")),
    ("emit_integration_settings_updated", "user", dict(fields_changed=["jira_url"])),
    ("emit_chat_message",             "user",   dict(message_id="m1", user_id="u1", role="user", content="hi", timestamp="2026-04-24T00:00:00")),
    ("emit_new_device_login",         "user",   dict(user_id="u1", token_hint="abc", ip="1.2.3.4", user_agent="Mozilla")),
]

_OBS_TABLE: list[tuple[str, str, dict]] = [
    ("emit_queue_tick",              "tenant", {}),
    ("emit_lock_acquired",           "tenant", dict(task_id="t1", paths=["a"], priority=0, wait_seconds=0.0, expires_at=0.0)),
    ("emit_lock_released",           "tenant", dict(task_id="t1", released_count=1)),
    ("emit_merger_voted",            "tenant", dict(change_id="c1", file_path="a.c", reason="ok", voted_score=2, confidence=0.9)),
    ("emit_change_awaiting_human",   "tenant", dict(change_id="c1", project="p", file_path="a.c", merger_confidence=0.9)),
]


# ─── Signature pins ──────────────────────────────────────────────────


@pytest.mark.parametrize("name,_legacy,_kwargs", _EVENTS_TABLE)
def test_events_helper_has_none_scope_default(name, _legacy, _kwargs):
    """Every ``backend/events.py`` emit_* helper defaults broadcast_scope to None."""
    fn = getattr(events_mod, name)
    sig = inspect.signature(fn)
    assert "broadcast_scope" in sig.parameters, f"{name} missing broadcast_scope kwarg"
    assert sig.parameters["broadcast_scope"].default is None, (
        f"{name}.broadcast_scope default must be None (got "
        f"{sig.parameters['broadcast_scope'].default!r})"
    )


@pytest.mark.parametrize("name,_legacy,_kwargs", _OBS_TABLE)
def test_orchestration_helper_has_none_scope_default(name, _legacy, _kwargs):
    fn = getattr(obs_mod, name)
    sig = inspect.signature(fn)
    assert "broadcast_scope" in sig.parameters, f"{name} missing broadcast_scope kwarg"
    assert sig.parameters["broadcast_scope"].default is None


@pytest.mark.parametrize("name", ["emit_ui_sandbox_screenshot_event", "emit_ui_sandbox_error_event"])
def test_ui_sandbox_helper_has_none_scope_default(name):
    fn = getattr(ui_mod, name)
    sig = inspect.signature(fn)
    assert "broadcast_scope" in sig.parameters, f"{name} missing broadcast_scope kwarg"
    assert sig.parameters["broadcast_scope"].default is None


def test_ui_sandbox_bus_publisher_default_is_none():
    """:class:`BusEventPublisher` defaults must be None so the publisher
    hits ``_resolve_scope`` like every other emit-path."""
    pub = ui_mod.BusEventPublisher()
    assert pub.broadcast_scope is None
    assert pub.legacy_default_scope == "global"


# ─── Resolution behaviour ────────────────────────────────────────────


def _capture_events(monkeypatch):
    """Replace bus.publish with a capture shim; return the list it fills."""
    captured: list[dict] = []

    def _fake_publish(event, data, session_id=None, broadcast_scope="global", tenant_id=None):
        captured.append({
            "event": event,
            "data": dict(data),
            "session_id": session_id,
            "broadcast_scope": broadcast_scope,
            "tenant_id": tenant_id,
        })

    monkeypatch.setattr(events_mod.bus, "publish", _fake_publish)
    return captured


@pytest.mark.parametrize("name,legacy,kwargs", _EVENTS_TABLE)
def test_events_helper_falls_back_to_legacy_default(name, legacy, kwargs, monkeypatch, caplog):
    """With no ``broadcast_scope`` the helper must warn + emit using ``legacy``."""
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.delenv(events_mod._SCOPE_STRICT_ENV, raising=False)
    captured = _capture_events(monkeypatch)
    # Quiet the ``_log`` REPORTER VORTEX write — it doesn't affect scope logic.
    monkeypatch.setattr(events_mod, "_log", lambda *a, **kw: None)
    caplog.set_level(logging.WARNING, logger="backend.events")

    fn = getattr(events_mod, name)
    fn(**kwargs)

    assert len(captured) == 1, f"{name} did not emit exactly one event"
    assert captured[0]["broadcast_scope"] == legacy
    assert any(
        name + "()" in r.getMessage() and "broadcast_scope" in r.getMessage()
        for r in caplog.records
    ), f"{name} did not emit deprecation warning"


@pytest.mark.parametrize("name,_legacy,kwargs", _EVENTS_TABLE)
def test_events_helper_explicit_scope_silences_warning(name, _legacy, kwargs, monkeypatch, caplog):
    """Passing ``broadcast_scope=...`` must not trigger the deprecation log."""
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.delenv(events_mod._SCOPE_STRICT_ENV, raising=False)
    captured = _capture_events(monkeypatch)
    monkeypatch.setattr(events_mod, "_log", lambda *a, **kw: None)
    caplog.set_level(logging.WARNING, logger="backend.events")

    fn = getattr(events_mod, name)
    fn(broadcast_scope="session", **kwargs)

    assert captured[0]["broadcast_scope"] == "session"
    for rec in caplog.records:
        assert "broadcast_scope" not in rec.getMessage() or "without explicit" not in rec.getMessage(), (
            f"{name}: warning fired even though broadcast_scope was explicit"
        )


def test_events_helper_warns_only_once_per_helper(monkeypatch, caplog):
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.delenv(events_mod._SCOPE_STRICT_ENV, raising=False)
    _capture_events(monkeypatch)
    monkeypatch.setattr(events_mod, "_log", lambda *a, **kw: None)
    caplog.set_level(logging.WARNING, logger="backend.events")

    for _ in range(5):
        events_mod.emit_agent_update("a1", "running")

    warnings = [r for r in caplog.records if "emit_agent_update()" in r.getMessage()]
    assert len(warnings) == 1, (
        f"Expected exactly one warning across 5 calls, got {len(warnings)}"
    )


def test_strict_env_raises_typeerror(monkeypatch):
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.setenv(events_mod._SCOPE_STRICT_ENV, "1")
    _capture_events(monkeypatch)
    monkeypatch.setattr(events_mod, "_log", lambda *a, **kw: None)

    with pytest.raises(TypeError, match="requires broadcast_scope"):
        events_mod.emit_task_update("t1", "done")


def test_strict_env_does_not_affect_explicit_callers(monkeypatch):
    """Strict mode must not break callers that *do* pass scope."""
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.setenv(events_mod._SCOPE_STRICT_ENV, "1")
    captured = _capture_events(monkeypatch)
    monkeypatch.setattr(events_mod, "_log", lambda *a, **kw: None)

    events_mod.emit_task_update("t1", "done", broadcast_scope="user")
    assert captured and captured[0]["broadcast_scope"] == "user"


# ─── Orchestration helpers route through _resolve_scope too ──────────


def test_orchestration_helper_falls_back_and_warns(monkeypatch, caplog):
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.delenv(events_mod._SCOPE_STRICT_ENV, raising=False)
    captured = _capture_events(monkeypatch)
    caplog.set_level(logging.WARNING, logger="backend.events")

    obs_mod.emit_lock_acquired(
        task_id="t1", paths=["a"], priority=0, wait_seconds=0.0, expires_at=0.0,
    )

    assert len(captured) == 1
    assert captured[0]["broadcast_scope"] == "tenant"
    assert any(
        "emit_lock_acquired()" in r.getMessage()
        for r in caplog.records
    )


def test_orchestration_helper_honours_explicit_scope(monkeypatch, caplog):
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.delenv(events_mod._SCOPE_STRICT_ENV, raising=False)
    captured = _capture_events(monkeypatch)
    caplog.set_level(logging.WARNING, logger="backend.events")

    obs_mod.emit_lock_released(
        task_id="t1", released_count=2, broadcast_scope="tenant", tenant_id="x",
    )

    assert captured[0]["broadcast_scope"] == "tenant"
    assert captured[0]["tenant_id"] == "x"
    assert not any(
        "emit_lock_released()" in r.getMessage() and "without explicit" in r.getMessage()
        for r in caplog.records
    )


# ─── ui_sandbox bridge publisher ────────────────────────────────────


def test_ui_sandbox_publisher_falls_back_and_warns(monkeypatch, caplog):
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.delenv(events_mod._SCOPE_STRICT_ENV, raising=False)
    captured = _capture_events(monkeypatch)
    caplog.set_level(logging.WARNING, logger="backend.events")

    pub = ui_mod.BusEventPublisher()
    pub.publish("ui_sandbox.screenshot", {"image_url": ""}, session_id="s1")

    assert captured and captured[0]["broadcast_scope"] == "global"
    assert any(
        "ui_sandbox_sse.BusEventPublisher.publish" in r.getMessage()
        for r in caplog.records
    )


def test_ui_sandbox_publisher_honours_explicit_session_scope(monkeypatch, caplog):
    events_mod._reset_scope_warned_for_tests()
    monkeypatch.delenv(events_mod._SCOPE_STRICT_ENV, raising=False)
    captured = _capture_events(monkeypatch)
    caplog.set_level(logging.WARNING, logger="backend.events")

    pub = ui_mod.BusEventPublisher(broadcast_scope="session")
    pub.publish("ui_sandbox.screenshot", {"image_url": ""}, session_id="s1")

    assert captured[0]["broadcast_scope"] == "session"
    assert not any(
        "without explicit" in r.getMessage() for r in caplog.records
    )
