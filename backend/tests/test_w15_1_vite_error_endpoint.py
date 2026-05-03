"""W15.1 — Contract tests for the Vite plugin error reporting endpoint.

Locks the wire shape, validation, ring-buffer behaviour, and drift
guards between :mod:`packages/omnisight-vite-plugin` (JS) and
:mod:`backend.web_sandbox_vite_errors` / the new
``POST /web-sandbox/preview/{workspace_id}/error`` endpoint.

§A — Schema / drift guards (literals must match the JS plugin pin).
§B — Wire-shape validation (kind / phase / message / file / line /
     column / stack normalisation; truncation; extra-key rejection).
§C — Ring buffer semantics (capacity, FIFO eviction, per-workspace
     isolation, drop, count, recent ordering).
§D — Endpoint behaviour (200 happy path, 422 shape error, GET listing,
     auth gate via ``require_operator`` / ``require_viewer``).
§E — Round-trip integration (POST → GET → buffer.recent shape parity).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend.routers import web_sandbox as web_sandbox_router
from backend.web_sandbox_vite_errors import (
    VITE_ERROR_ALLOWED_KINDS,
    VITE_ERROR_ALLOWED_PHASES,
    VITE_ERROR_BUFFER_DEFAULT_CAP,
    VITE_ERROR_MESSAGE_MAX_BYTES,
    VITE_ERROR_PLUGIN_NAME,
    VITE_ERROR_PLUGIN_VERSION,
    VITE_ERROR_STACK_MAX_BYTES,
    WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
    ViteBuildError,
    ViteBuildErrorValidationError,
    ViteErrorBuffer,
    set_default_buffer_for_tests,
    validate_error_payload,
)


# ── Fixtures ───────────────────────────────────────────────────────


def _operator() -> _au.User:
    return _au.User(
        id="u-operator",
        email="op@example.com",
        name="Op",
        role="operator",
    )


def _viewer() -> _au.User:
    return _au.User(
        id="u-viewer",
        email="viewer@example.com",
        name="V",
        role="viewer",
    )


@pytest.fixture
def buffer() -> ViteErrorBuffer:
    return ViteErrorBuffer(capacity=4)


@pytest.fixture
def client(buffer: ViteErrorBuffer) -> TestClient:
    app = FastAPI()
    app.include_router(web_sandbox_router.router)
    app.dependency_overrides[_au.require_operator] = _operator
    app.dependency_overrides[_au.require_viewer] = _viewer
    web_sandbox_router.set_vite_error_buffer_for_tests(buffer)
    try:
        yield TestClient(app)
    finally:
        web_sandbox_router.set_vite_error_buffer_for_tests(None)


@pytest.fixture(autouse=True)
def reset_default_buffer() -> Any:
    """Make sure the per-process default buffer is fresh between
    tests (the router's `set_vite_error_buffer_for_tests` covers the
    happy path, but tests that bypass the fixture should still see a
    clean slate)."""

    set_default_buffer_for_tests(None)
    yield
    set_default_buffer_for_tests(None)


def _ok_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema_version": WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        "kind": "compile",
        "phase": "transform",
        "message": "Failed to parse module",
        "file": "src/App.tsx",
        "line": 42,
        "column": 7,
        "stack": "Error: Failed to parse module\n  at transform (vite/...)",
        "plugin": VITE_ERROR_PLUGIN_NAME,
        "plugin_version": VITE_ERROR_PLUGIN_VERSION,
        "occurred_at": 1714760400.123,
    }
    base.update(overrides)
    return base


# ────────────────────────────────────────────────────────────────────
# §A — Schema / drift guards
# ────────────────────────────────────────────────────────────────────


def test_schema_version_pin_matches_js_plugin() -> None:
    """JS pin lives in ``packages/omnisight-vite-plugin/index.js``
    (``OMNISIGHT_VITE_ERROR_SCHEMA_VERSION``).  Bumping one without
    the other produces a wire-shape mismatch the runtime
    `validate_error_payload` already rejects, but locking the literal
    to "1.0.0" here makes that drift catch fire at *test time* rather
    than only when a real plugin posts."""

    assert WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION == "1.0.0"


def test_allowed_phases_match_js_plugin() -> None:
    """Order + membership both matter — the JS plugin's
    ``ALLOWED_PHASES`` array is frozen identically and the drift
    guard test on the JS side asserts the exact same tuple."""

    assert VITE_ERROR_ALLOWED_PHASES == (
        "config",
        "buildStart",
        "load",
        "transform",
        "hmr",
        "client",
    )


def test_allowed_kinds_locked() -> None:
    assert VITE_ERROR_ALLOWED_KINDS == frozenset({"compile", "runtime"})


def test_message_and_stack_caps_match_js_plugin() -> None:
    assert VITE_ERROR_MESSAGE_MAX_BYTES == 4 * 1024
    assert VITE_ERROR_STACK_MAX_BYTES == 8 * 1024


def test_buffer_default_capacity_locked() -> None:
    assert VITE_ERROR_BUFFER_DEFAULT_CAP == 200


def test_plugin_name_and_version_pinned() -> None:
    assert VITE_ERROR_PLUGIN_NAME == "omnisight-vite-plugin"
    assert VITE_ERROR_PLUGIN_VERSION == "0.1.0"


# ────────────────────────────────────────────────────────────────────
# §B — Wire-shape validation
# ────────────────────────────────────────────────────────────────────


def test_validate_payload_happy_path() -> None:
    payload = _ok_payload()
    err = validate_error_payload(payload)
    assert isinstance(err, ViteBuildError)
    assert err.kind == "compile"
    assert err.phase == "transform"
    assert err.file == "src/App.tsx"
    assert err.line == 42
    assert err.column == 7
    assert err.plugin == VITE_ERROR_PLUGIN_NAME
    assert err.plugin_version == VITE_ERROR_PLUGIN_VERSION
    assert err.received_at > 0  # auto-populated


def test_validate_rejects_extra_keys() -> None:
    payload = _ok_payload()
    payload["extra_key"] = "nope"
    with pytest.raises(ViteBuildErrorValidationError, match="unexpected keys"):
        validate_error_payload(payload)


def test_validate_rejects_missing_keys() -> None:
    payload = _ok_payload()
    del payload["message"]
    with pytest.raises(ViteBuildErrorValidationError, match="missing required keys"):
        validate_error_payload(payload)


def test_validate_rejects_schema_mismatch() -> None:
    payload = _ok_payload(schema_version="9.9.9")
    with pytest.raises(ViteBuildErrorValidationError, match="schema_version mismatch"):
        validate_error_payload(payload)


def test_validate_rejects_unknown_kind() -> None:
    payload = _ok_payload(kind="warning")
    with pytest.raises(ViteBuildErrorValidationError, match="kind must be one of"):
        validate_error_payload(payload)


def test_validate_rejects_unknown_phase() -> None:
    payload = _ok_payload(phase="bundle")
    with pytest.raises(ViteBuildErrorValidationError, match="phase must be one of"):
        validate_error_payload(payload)


def test_validate_rejects_unknown_plugin() -> None:
    payload = _ok_payload(plugin="rogue-plugin")
    with pytest.raises(ViteBuildErrorValidationError, match="must be 'omnisight-vite-plugin'"):
        validate_error_payload(payload)


def test_validate_truncates_long_message() -> None:
    long = "a" * (VITE_ERROR_MESSAGE_MAX_BYTES + 100)
    payload = _ok_payload(message=long)
    err = validate_error_payload(payload)
    assert len(err.message.encode("utf-8")) <= VITE_ERROR_MESSAGE_MAX_BYTES


def test_validate_truncates_long_stack() -> None:
    long_stack = "x" * (VITE_ERROR_STACK_MAX_BYTES + 500)
    payload = _ok_payload(stack=long_stack)
    err = validate_error_payload(payload)
    assert err.stack is not None
    assert len(err.stack.encode("utf-8")) <= VITE_ERROR_STACK_MAX_BYTES


def test_validate_preserves_utf8_codepoints_during_truncation() -> None:
    """Multi-byte codepoint at the truncation boundary must not be
    split mid-byte — that would produce invalid UTF-8 and break
    json.dumps downstream."""

    # 3-byte codepoint (CJK) repeated to overshoot the cap.
    msg = "中" * (VITE_ERROR_MESSAGE_MAX_BYTES // 3 + 50)
    payload = _ok_payload(message=msg)
    err = validate_error_payload(payload)
    # Must still decode cleanly as UTF-8.
    assert err.message.encode("utf-8").decode("utf-8") == err.message


def test_validate_rejects_negative_occurred_at() -> None:
    payload = _ok_payload(occurred_at=-1.0)
    with pytest.raises(ViteBuildErrorValidationError, match="non-negative"):
        validate_error_payload(payload)


def test_validate_rejects_bool_for_int_fields() -> None:
    """Pydantic v2 normally treats bool as int; we reject explicitly
    because a bool sneaking in for ``line`` would silently store as
    0/1 and confuse the W15.3 prompt template."""

    payload = _ok_payload(line=True)
    with pytest.raises(ViteBuildErrorValidationError, match="must be an int"):
        validate_error_payload(payload)


def test_validate_accepts_null_file_line_column_stack() -> None:
    payload = _ok_payload(file=None, line=None, column=None, stack=None)
    err = validate_error_payload(payload)
    assert err.file is None and err.line is None and err.column is None
    assert err.stack is None


# ────────────────────────────────────────────────────────────────────
# §C — Ring buffer semantics
# ────────────────────────────────────────────────────────────────────


def _make_error(idx: int = 0, kind: str = "compile", phase: str = "transform") -> ViteBuildError:
    return ViteBuildError(
        schema_version=WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        kind=kind,
        phase=phase,
        message=f"err-{idx}",
        file=f"src/file{idx}.tsx",
        line=idx,
        column=0,
        stack=None,
        plugin=VITE_ERROR_PLUGIN_NAME,
        plugin_version=VITE_ERROR_PLUGIN_VERSION,
        occurred_at=1714760400.0 + idx,
        received_at=time.time(),
    )


def test_buffer_records_and_reads_back_in_chronological_order() -> None:
    buf = ViteErrorBuffer(capacity=8)
    for i in range(3):
        buf.record("ws-1", _make_error(i))
    entries = buf.recent("ws-1")
    assert [e.message for e in entries] == ["err-0", "err-1", "err-2"]


def test_buffer_evicts_oldest_when_at_capacity() -> None:
    buf = ViteErrorBuffer(capacity=3)
    for i in range(5):
        buf.record("ws-1", _make_error(i))
    entries = buf.recent("ws-1")
    assert len(entries) == 3
    assert [e.message for e in entries] == ["err-2", "err-3", "err-4"]


def test_buffer_per_workspace_isolation() -> None:
    buf = ViteErrorBuffer(capacity=4)
    buf.record("ws-A", _make_error(0))
    buf.record("ws-B", _make_error(99))
    a = buf.recent("ws-A")
    b = buf.recent("ws-B")
    assert len(a) == 1 and a[0].message == "err-0"
    assert len(b) == 1 and b[0].message == "err-99"
    assert buf.count("ws-A") == 1 and buf.count("ws-B") == 1


def test_buffer_drop_clears_workspace() -> None:
    buf = ViteErrorBuffer(capacity=4)
    buf.record("ws-1", _make_error(0))
    buf.record("ws-1", _make_error(1))
    dropped = buf.drop("ws-1")
    assert dropped == 2
    assert buf.recent("ws-1") == []
    assert buf.count("ws-1") == 0


def test_buffer_recent_with_limit() -> None:
    buf = ViteErrorBuffer(capacity=8)
    for i in range(5):
        buf.record("ws-1", _make_error(i))
    last_two = buf.recent("ws-1", limit=2)
    assert [e.message for e in last_two] == ["err-3", "err-4"]


def test_buffer_recent_unknown_workspace_returns_empty() -> None:
    buf = ViteErrorBuffer(capacity=4)
    assert buf.recent("nonexistent") == []
    assert buf.count("nonexistent") == 0


def test_buffer_workspaces_returns_known_keys() -> None:
    buf = ViteErrorBuffer(capacity=4)
    buf.record("ws-A", _make_error(0))
    buf.record("ws-B", _make_error(0))
    assert sorted(buf.workspaces()) == ["ws-A", "ws-B"]


def test_buffer_record_fills_in_received_at_when_zero() -> None:
    buf = ViteErrorBuffer(capacity=4)
    raw = ViteBuildError(
        schema_version=WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        kind="compile",
        phase="transform",
        message="x",
        file=None,
        line=None,
        column=None,
        stack=None,
        plugin=VITE_ERROR_PLUGIN_NAME,
        plugin_version=VITE_ERROR_PLUGIN_VERSION,
        occurred_at=1.0,
        received_at=0.0,
    )
    stored = buf.record("ws-1", raw)
    assert stored.received_at > 0


def test_buffer_rejects_non_string_workspace_id() -> None:
    buf = ViteErrorBuffer(capacity=4)
    with pytest.raises(ValueError, match="non-empty string"):
        buf.record("", _make_error(0))


def test_buffer_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="capacity must be positive"):
        ViteErrorBuffer(capacity=0)


# ────────────────────────────────────────────────────────────────────
# §D — Endpoint behaviour
# ────────────────────────────────────────────────────────────────────


def test_post_error_records_payload(client: TestClient, buffer: ViteErrorBuffer) -> None:
    resp = client.post(
        "/web-sandbox/preview/ws-42/error",
        json=_ok_payload(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION
    assert body["workspace_id"] == "ws-42"
    assert body["buffer_count"] == 1
    rec = body["recorded"]
    assert rec["kind"] == "compile"
    assert rec["phase"] == "transform"
    assert rec["file"] == "src/App.tsx"
    assert rec["line"] == 42
    assert rec["received_at"] > 0
    # The buffer fixture is the same instance the endpoint wrote to.
    assert buffer.count("ws-42") == 1


def test_post_error_rejects_extra_field(client: TestClient) -> None:
    payload = _ok_payload()
    payload["malicious"] = "extra"
    resp = client.post(
        "/web-sandbox/preview/ws-42/error",
        json=payload,
    )
    assert resp.status_code == 422, resp.text


def test_post_error_rejects_schema_mismatch(client: TestClient) -> None:
    payload = _ok_payload(schema_version="2.0.0")
    resp = client.post(
        "/web-sandbox/preview/ws-42/error",
        json=payload,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "schema_version mismatch" in body["detail"]


def test_post_error_rejects_unknown_phase_via_pydantic(client: TestClient) -> None:
    """Pydantic accepts the string but our ``validate_error_payload``
    cross-check rejects the unknown phase before it reaches the
    buffer.  Path returns 422 either way; this test pins the message."""

    payload = _ok_payload(phase="invalid")
    resp = client.post(
        "/web-sandbox/preview/ws-42/error",
        json=payload,
    )
    assert resp.status_code == 422


def test_post_error_runtime_kind_with_client_phase(
    client: TestClient, buffer: ViteErrorBuffer
) -> None:
    """Browser-side runtime overlay POSTs ``kind=runtime``, ``phase=client``."""

    payload = _ok_payload(
        kind="runtime",
        phase="client",
        file=None,
        line=None,
        column=None,
        message="ReferenceError: foo is not defined",
    )
    resp = client.post(
        "/web-sandbox/preview/ws-42/error",
        json=payload,
    )
    assert resp.status_code == 200
    rec = resp.json()["recorded"]
    assert rec["kind"] == "runtime"
    assert rec["phase"] == "client"
    assert rec["file"] is None and rec["line"] is None
    assert buffer.count("ws-42") == 1


def test_get_errors_returns_recorded_list(
    client: TestClient, buffer: ViteErrorBuffer
) -> None:
    for i in range(3):
        client.post(
            "/web-sandbox/preview/ws-7/error",
            json=_ok_payload(message=f"e-{i}", line=i, occurred_at=1700.0 + i),
        )
    resp = client.get("/web-sandbox/preview/ws-7/errors")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == "ws-7"
    assert body["buffer_count"] == 3
    msgs = [e["message"] for e in body["errors"]]
    assert msgs == ["e-0", "e-1", "e-2"]


def test_get_errors_with_limit_caps_returned_rows(
    client: TestClient,
) -> None:
    """Buffer fixture has capacity=4 — POST 4 errors so we can pin
    the limit-applied ordering without entanglement with the FIFO
    eviction rule (covered separately in
    test_post_error_evicts_oldest_when_buffer_full)."""

    for i in range(4):
        client.post(
            "/web-sandbox/preview/ws-7/error",
            json=_ok_payload(message=f"e-{i}", occurred_at=1700.0 + i),
        )
    resp = client.get("/web-sandbox/preview/ws-7/errors?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["buffer_count"] == 4
    assert [e["message"] for e in body["errors"]] == ["e-2", "e-3"]


def test_get_errors_unknown_workspace_returns_empty_list(
    client: TestClient,
) -> None:
    resp = client.get("/web-sandbox/preview/ws-unknown/errors")
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == []
    assert body["buffer_count"] == 0


def test_post_error_per_workspace_isolation(
    client: TestClient, buffer: ViteErrorBuffer
) -> None:
    client.post(
        "/web-sandbox/preview/ws-A/error",
        json=_ok_payload(message="A-fail"),
    )
    client.post(
        "/web-sandbox/preview/ws-B/error",
        json=_ok_payload(message="B-fail"),
    )
    a = buffer.recent("ws-A")
    b = buffer.recent("ws-B")
    assert len(a) == 1 and a[0].message == "A-fail"
    assert len(b) == 1 and b[0].message == "B-fail"


def test_post_error_evicts_oldest_when_buffer_full(
    client: TestClient, buffer: ViteErrorBuffer
) -> None:
    """Buffer fixture has capacity=4 — POST 5 errors and confirm the
    oldest is dropped."""

    for i in range(5):
        client.post(
            "/web-sandbox/preview/ws-cap/error",
            json=_ok_payload(message=f"m-{i}", occurred_at=1700.0 + i),
        )
    msgs = [e.message for e in buffer.recent("ws-cap")]
    assert msgs == ["m-1", "m-2", "m-3", "m-4"]


# ────────────────────────────────────────────────────────────────────
# §E — Round-trip integration
# ────────────────────────────────────────────────────────────────────


def test_post_then_get_round_trips_shape(client: TestClient) -> None:
    payload = _ok_payload(message="round-trip", line=99)
    post = client.post(
        "/web-sandbox/preview/ws-rt/error",
        json=payload,
    )
    assert post.status_code == 200
    get = client.get("/web-sandbox/preview/ws-rt/errors")
    assert get.status_code == 200
    body = get.json()
    assert len(body["errors"]) == 1
    e = body["errors"][0]
    assert e["message"] == "round-trip"
    assert e["line"] == 99
    assert e["plugin"] == VITE_ERROR_PLUGIN_NAME
    assert e["schema_version"] == WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION
    # received_at populated server-side, occurred_at echoes the request
    assert e["occurred_at"] == pytest.approx(payload["occurred_at"])
    assert e["received_at"] > 0


def test_default_buffer_singleton_is_shared_across_imports() -> None:
    """Drift guard — re-importing ``get_default_buffer`` must return
    the same instance the router will use, otherwise W15.2's drain
    loop reads from a different buffer than the endpoint writes to."""

    from backend.web_sandbox_vite_errors import get_default_buffer

    first = get_default_buffer()
    second = get_default_buffer()
    assert first is second


def test_set_default_buffer_for_tests_resets_singleton() -> None:
    fresh = ViteErrorBuffer(capacity=2)
    set_default_buffer_for_tests(fresh)
    from backend.web_sandbox_vite_errors import get_default_buffer

    assert get_default_buffer() is fresh
    set_default_buffer_for_tests(None)
    assert get_default_buffer() is not fresh
