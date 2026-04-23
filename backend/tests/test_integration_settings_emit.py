"""Q.3-SUB-5 (#297) — Integration-settings cross-device SSE sync.

Before Q.3-SUB-5 ``PUT /runtime/settings`` mirrored non-LLM fields
(Gerrit / JIRA / GitHub / GitLab / Slack / PagerDuty / webhooks / CI
/ Docker) into SharedKV but never published an SSE event for the
non-LLM subset. The LLM subset already had ``emit_invoke(
'provider_switch')`` wiring — those fields stayed covered. Other
integration tabs relied on the SYSTEM INTEGRATIONS modal's
``useEffect(() => { if (open) refetch(); }, [open])`` to discover
changes, meaning a passively-open modal on a second device never
saw the new value until the operator closed and re-opened it.

This suite locks the emit + the cross-device contract:

  * ``test_integration_emit_on_non_llm_update`` — a successful PUT
    touching a single non-LLM field publishes exactly one
    ``integration.settings.updated`` event carrying the applied
    field list on the bus.
  * ``test_integration_no_emit_on_pure_llm_update`` — a PUT that
    only touches LLM fields MUST NOT publish the new event (the
    LLM subset owns the existing ``invoke('provider_switch')`` emit
    and we don't want to double-fire on a pure-LLM save).
  * ``test_integration_emit_on_mixed_update`` — a PUT that touches
    BOTH LLM and non-LLM fields publishes the non-LLM subset only,
    so subscribers can ignore the LLM keys that arrive via the
    ``invoke`` channel.
  * ``test_integration_no_emit_on_rejected_only`` — a PUT where
    every key lands in ``rejected`` (nothing applied) MUST NOT
    publish — an empty event would trigger spurious refetches on
    every connected client.
  * ``test_integration_emit_scope_is_user`` — payload contract
    lock: ``_broadcast_scope='user'`` so Q.4 (#298) can switch from
    advisory to enforced without a payload change.
  * ``test_integration_cross_device_fanout`` — two parallel
    ``bus.subscribe()`` listeners (simulating two operators with
    the modal open on different devices); a PUT from session A
    drives the ``integration.settings.updated`` payload onto
    session B's queue with the applied-field list the frontend
    refetch dispatcher consumes.
  * ``test_integration_emit_failure_does_not_break_put`` — a flaky
    SSE bus / Redis outage must NEVER fail the PUT HTTP call; the
    SharedKV mirror is still in effect so cross-worker coherence
    within a single device stays intact.

Audit evidence: ``docs/design/multi-device-state-sync.md`` Path 2.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.events import bus as _bus


async def _drain_for_integration(
    queue, timeout: float = 2.0,
):
    """Drain the SSE queue until an ``integration.settings.updated``
    event arrives, or return None on timeout.

    Filters out heartbeats and other events so ambient chatter from
    sibling fixtures doesn't masquerade as the Q.3-SUB-5 payload.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if msg.get("event") != "integration.settings.updated":
            continue
        return json.loads(msg["data"])
    return None


async def _drain_any(queue, event_name: str, timeout: float = 0.6):
    """Return the first event matching ``event_name`` within
    ``timeout`` seconds, or None if none arrives. Used by the
    negative tests (pure-LLM + rejected-only) to prove the event
    was NOT published — short timeout keeps the suite fast.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        if msg.get("event") == event_name:
            return json.loads(msg["data"])
    return None


@pytest.mark.asyncio
async def test_integration_emit_on_non_llm_update(client):
    """PUT /runtime/settings touching a non-LLM field emits exactly
    one ``integration.settings.updated`` event with the applied key
    list on the bus.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"gerrit_url": "https://gerrit.example.com"}},
        )
        assert res.status_code == 200, res.text
        assert "gerrit_url" in res.json()["applied"]

        data = await _drain_for_integration(q)
        assert data is not None, (
            "PUT /runtime/settings touching a non-LLM field must "
            "publish integration.settings.updated — cross-device sync "
            "would otherwise wait for a manual modal close/re-open."
        )
        assert "gerrit_url" in data["fields_changed"]
    finally:
        _bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_integration_no_emit_on_pure_llm_update(client):
    """A PUT that only touches LLM fields MUST NOT fire the new
    event — the LLM subset already owns the ``invoke('provider_
    switch')`` emit and we don't want to double-fire on a pure-LLM
    save (ambient consumers would otherwise refetch /runtime/settings
    on every provider change).
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"llm_temperature": 0.25}},
        )
        assert res.status_code == 200, res.text
        assert "llm_temperature" in res.json()["applied"]

        leaked = await _drain_any(q, "integration.settings.updated")
        assert leaked is None, (
            "pure-LLM save must not publish integration.settings."
            "updated — invoke('provider_switch') already covers the "
            "LLM case and double-emits would thrash the modal refetch."
        )
    finally:
        _bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_integration_emit_on_mixed_update(client):
    """A PUT that touches BOTH LLM and non-LLM fields publishes only
    the non-LLM subset in ``fields_changed`` — keeps the event
    channel clean so subscribers don't have to know which keys are
    LLM-owned.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {
                "llm_temperature": 0.5,
                "gerrit_project": "omnisight",
            }},
        )
        assert res.status_code == 200, res.text
        applied = set(res.json()["applied"])
        assert {"llm_temperature", "gerrit_project"} <= applied

        data = await _drain_for_integration(q)
        assert data is not None, (
            "mixed save must publish the non-LLM subset — the "
            "non-LLM side still needs cross-device repaint even "
            "when the operator also flipped a temperature slider."
        )
        fields = set(data["fields_changed"])
        assert "gerrit_project" in fields
        assert "llm_temperature" not in fields, (
            "LLM keys must not appear in integration.settings."
            "updated — they're already delivered via invoke("
            "'provider_switch')."
        )
    finally:
        _bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_integration_no_emit_on_rejected_only(client):
    """A PUT whose every key lands in ``rejected`` (nothing
    applied) MUST NOT publish — an empty event would burn refetch
    cycles on every connected client for a no-op mutation.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"nonexistent_field": "x"}},
        )
        assert res.status_code == 200, res.text
        assert res.json()["applied"] == []
        assert "nonexistent_field" in res.json()["rejected"]

        leaked = await _drain_any(q, "integration.settings.updated")
        assert leaked is None, (
            "rejected-only save must not publish — empty fan-out "
            "would spuriously refetch every open modal."
        )
    finally:
        _bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_integration_emit_scope_is_user(client):
    """Lock the ``broadcast_scope='user'`` payload contract — the
    frontend filter (and the eventual Q.4 #298 server enforcement)
    both rely on this label being present on every emit.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {
                "notification_jira_url": "https://jira.example.com"
            }},
        )
        assert res.status_code == 200, res.text

        data = await _drain_for_integration(q)
        assert data is not None
        assert data["_broadcast_scope"] == "user", (
            "integration.settings.updated must carry "
            "broadcast_scope=user so Q.4 (#298) can enforce "
            "per-user delivery without changing the payload shape."
        )
        assert "notification_jira_url" in data["fields_changed"]
    finally:
        _bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_integration_cross_device_fanout(client):
    """Two bus subscribers (simulating two operator devices each
    with the modal open); the HTTP PUT from session A must reach
    session B's queue with ``fields_changed`` so the frontend
    refetch dispatcher can repaint without waiting for a manual
    close/re-open.
    """
    q_a = _bus.subscribe(tenant_id=None)
    q_b = _bus.subscribe(tenant_id=None)
    try:
        res = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {
                "github_token": "ghp_dummy_cross_device_fanout"
            }},
        )
        assert res.status_code == 200, res.text
        assert "github_token" in res.json()["applied"]

        data_b = await _drain_for_integration(q_b)
        assert data_b is not None, (
            "session B must receive integration.settings.updated"
        )
        assert "github_token" in data_b["fields_changed"]

        data_a = await _drain_for_integration(q_a)
        assert data_a is not None, (
            "originator must also see its own event — refetch is "
            "idempotent so double-apply is safe"
        )
        assert "github_token" in data_a["fields_changed"]
    finally:
        _bus.unsubscribe(q_a)
        _bus.unsubscribe(q_b)


@pytest.mark.asyncio
async def test_integration_emit_failure_does_not_break_put(
    client, monkeypatch,
):
    """A flaky SSE bus / Redis outage must NEVER fail the PUT
    HTTP call — the SharedKV mirror is still in effect so
    cross-worker coherence within a single device stays intact,
    and the emit is pure latency-optimisation for cross-device.
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SSE bus outage")

    # Monkey-patch the symbol the handler imports inside the try
    # block. The handler does ``from backend.events import
    # emit_integration_settings_updated`` inline, so we patch the
    # source module — the fresh import resolves to the boom.
    from backend import events as _events
    monkeypatch.setattr(
        _events, "emit_integration_settings_updated", _boom,
    )

    res = await client.put(
        "/api/v1/runtime/settings",
        json={"updates": {
            "gerrit_ssh_host": "gerrit.example.com"
        }},
    )
    # 200 must still return; the local setattr + SharedKV mirror
    # must have landed even though the SSE emit raised.
    assert res.status_code == 200, res.text
    assert "gerrit_ssh_host" in res.json()["applied"]

    # Confirm the follow-up GET reflects the applied value, proving
    # the mutation survived the SSE failure.
    get_res = await client.get("/api/v1/runtime/settings")
    assert get_res.status_code == 200
    assert (
        get_res.json()["gerrit"]["ssh_host"] == "gerrit.example.com"
    )
