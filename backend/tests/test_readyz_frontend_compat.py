"""OP-1483 — /readyz ``frontend_compat_check`` contract.

Pins the runtime detector behaviour:

* No FE observations recorded → ``ok=True`` (freshly-booted replica
  that hasn't seen browser traffic yet — silent is healthy).
* All recorded FE observations match the backend's own
  ``bundle_id`` → ``ok=True``.
* Any mismatched observation → ``ok=False`` plus
  ``mismatch_count > 0`` and the offending bundle in
  ``observed_fe_bundles``.

The middleware that feeds the ring buffer is also covered: posting
the ``X-OmniSight-Frontend-Bundle`` header on a real request drops
the observation into the in-process buffer and the Prometheus
counter bumps when the FE bundle differs from the BE one.
"""

from __future__ import annotations

import json

import pytest

from backend import api_versioning as av
from backend import frontend_compat
from backend.routers import health as health_mod


_FIXTURE_BUNDLE = {
    "bundle_id": "v0.5.0-rc3-3f1c0a4e",
    "git_ref": "refs/tags/v0.5.0-rc3",
    "git_sha": "3f1c0a4e" + "0" * 32,
    "build_time": "2026-05-18T00:00:00Z",
    "images": {
        "backend":  {"digest": "sha256:" + "a" * 64},
        "frontend": {"digest": "sha256:" + "b" * 64},
        "bridge":   {"digest": "sha256:" + "c" * 64},
    },
    "contracts": {
        "api_required": "v1",
        "api_supported": ["v1", "v2"],
        "openapi_hash": "d" * 64,
        "db_migration_head": "0237_runner_audit_events",
        "frontend_built_against_api": "v1",
    },
    "signatures": [],
}


@pytest.fixture(autouse=True)
def _reset_compat_ring():
    """Each test starts with an empty ring buffer + zero counter."""
    frontend_compat.reset_for_tests()
    yield
    frontend_compat.reset_for_tests()


@pytest.fixture()
def baked_bundle(tmp_path, monkeypatch):
    """Point ``api_versioning`` at a fixture bundle.json so the BE
    bundle_id is deterministic and equals ``v0.5.0-rc3-3f1c0a4e``."""
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_FIXTURE_BUNDLE), encoding="utf-8")
    monkeypatch.setattr(av, "BUNDLE_MANIFEST_PATH", path)
    monkeypatch.setattr(av, "_IMAGE_MANIFEST_PATH", tmp_path / "MANIFEST.json")
    return path


# ───── unit: frontend_compat ─────────────────────────────────────────


def test_summary_empty_buffer_is_ok():
    """No FE observations → ok=True (AC#1 case a)."""
    out = frontend_compat.summary("v0.5.0-rc3-3f1c0a4e", "v1")
    assert out["ok"] is True
    assert out["observed_fe_bundles"] == []
    assert out["mismatch_count"] == 0
    assert out["samples_in_window"] == 0


def test_summary_all_match_is_ok():
    """Every observation == backend bundle → ok=True (AC#1 case b)."""
    for _ in range(5):
        frontend_compat.record_observation(
            "v0.5.0-rc3-3f1c0a4e", "v1",
            be_bundle="v0.5.0-rc3-3f1c0a4e",
        )
    out = frontend_compat.summary("v0.5.0-rc3-3f1c0a4e", "v1")
    assert out["ok"] is True
    assert out["observed_fe_bundles"] == ["v0.5.0-rc3-3f1c0a4e"]
    assert out["mismatch_count"] == 0
    assert out["samples_in_window"] == 5


def test_summary_any_mismatch_flips_ok_false():
    """Any divergent observation → ok=False (AC#1 case c)."""
    frontend_compat.record_observation(
        "v0.5.0-rc3-3f1c0a4e", "v1",
        be_bundle="v0.5.0-rc3-3f1c0a4e",
    )
    frontend_compat.record_observation(
        "v0.5.0-rc2-OLDER123",  # 3 hotfixes behind — the Codex scenario
        "v1",
        be_bundle="v0.5.0-rc3-3f1c0a4e",
    )
    out = frontend_compat.summary("v0.5.0-rc3-3f1c0a4e", "v1")
    assert out["ok"] is False
    assert "v0.5.0-rc2-OLDER123" in out["observed_fe_bundles"]
    assert out["mismatch_count"] == 1
    assert out["samples_in_window"] == 2


def test_summary_unknown_be_bundle_is_ok_with_detail():
    """If the backend doesn't know its own bundle id (local dev with
    no baked bundle.json) we cannot decide mismatch — stay ok=True so
    /readyz isn't flagged on every dev box. The detail line says why."""
    frontend_compat.record_observation(
        "anything-at-all", "v1",
        be_bundle=None,
    )
    out = frontend_compat.summary(None, None)
    assert out["ok"] is True
    assert "backend_bundle_unknown" in out["detail"]


def test_record_observation_ignores_empty_inputs():
    """Whitespace-only / None bundle and contract → no-op."""
    frontend_compat.record_observation(None, None)
    frontend_compat.record_observation("   ", "")
    out = frontend_compat.summary("v0.5.0-rc3-3f1c0a4e", "v1")
    assert out["samples_in_window"] == 0


def test_ring_buffer_caps_at_max():
    """Once the ring is full, oldest observations roll off (but the
    cumulative ``mismatch_count`` survives the eviction)."""
    cap = frontend_compat._RING_BUFFER_MAX
    for i in range(cap + 25):
        frontend_compat.record_observation(
            f"bundle-{i}", "v1",
            be_bundle="v0.5.0-rc3-3f1c0a4e",
        )
    out = frontend_compat.summary("v0.5.0-rc3-3f1c0a4e", "v1")
    assert out["samples_in_window"] == cap
    assert out["mismatch_count"] == cap + 25  # cumulative — never decays


# ───── unit: _check_frontend_compat returns the same shape ───────────


def test_check_frontend_compat_uses_backend_bundle_id(baked_bundle):
    """The /readyz helper resolves the BE bundle id via the same
    ``build_version_payload`` path operators read at /api/version."""
    frontend_compat.record_observation(
        "v0.5.0-rc2-OLDER123", "v1",
        be_bundle="v0.5.0-rc3-3f1c0a4e",
    )
    out = health_mod._check_frontend_compat()
    assert out["ok"] is False
    assert "v0.5.0-rc2-OLDER123" in out["observed_fe_bundles"]
    assert out["mismatch_count"] == 1


# ───── integration: /readyz JSON contract ────────────────────────────


@pytest.mark.asyncio
async def test_readyz_surfaces_frontend_compat_check_object(client, monkeypatch):
    """AC#2: ``curl /readyz | jq .checks.frontend_compat_check``
    must return a JSON object, never the string "missing".

    We don't assert on the overall /readyz status here — the test
    environment may not have a PG DSN wired so db/migration checks
    can legitimately fail. The contract this test pins is purely the
    *presence and shape* of the ``frontend_compat_check`` block in
    the response payload.
    """
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    r = await client.get("/readyz", follow_redirects=False)
    body = r.json()
    assert "frontend_compat_check" in body["checks"]
    check = body["checks"]["frontend_compat_check"]
    assert isinstance(check, dict)
    # Fields the runbook + Grafana panels rely on.
    assert "ok" in check
    assert "observed_fe_bundles" in check
    assert "mismatch_count" in check
    assert "detail" in check


@pytest.mark.asyncio
async def test_readyz_no_fe_traffic_is_ok_true(client, monkeypatch, baked_bundle):
    """AC#1 case (a): a replica that has not seen browser traffic
    reports ok=True with an empty observed_fe_bundles list."""
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    r = await client.get("/readyz", follow_redirects=False)
    check = r.json()["checks"]["frontend_compat_check"]
    assert check["ok"] is True
    assert check["observed_fe_bundles"] == []
    assert check["mismatch_count"] == 0


@pytest.mark.asyncio
async def test_readyz_matching_fe_observation_is_ok_true(
    client, monkeypatch, baked_bundle,
):
    """AC#1 case (b): FE that matches BE → ok=True. Hit the bundle-
    aware /api/version endpoint with the X-headers; the middleware
    must drop the observation into the ring buffer with be_bundle
    resolved from the same baked manifest."""
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    # The middleware looks at request headers — drive a request that
    # is NOT exempted from the middleware chain (/api/version is the
    # canonical FE-originated probe).
    await client.get(
        "/api/version",
        headers={
            "X-OmniSight-Frontend-Bundle": "v0.5.0-rc3-3f1c0a4e",
            "X-OmniSight-Frontend-Api-Contract": "v1",
        },
    )

    r = await client.get("/readyz", follow_redirects=False)
    check = r.json()["checks"]["frontend_compat_check"]
    assert check["ok"] is True
    assert check["observed_fe_bundles"] == ["v0.5.0-rc3-3f1c0a4e"]
    assert check["mismatch_count"] == 0


@pytest.mark.asyncio
async def test_readyz_mismatched_fe_observation_is_ok_false(
    client, monkeypatch, baked_bundle,
):
    """AC#1 case (c): FE that does NOT match BE → ok=false plus the
    offending bundle in observed_fe_bundles. This is the Codex 2026-
    05-18 incident in test form."""
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    await client.get(
        "/api/version",
        headers={
            "X-OmniSight-Frontend-Bundle": "v0.5.0-rc2-3hotfixesbehind",
            "X-OmniSight-Frontend-Api-Contract": "v1",
        },
    )

    r = await client.get("/readyz", follow_redirects=False)
    check = r.json()["checks"]["frontend_compat_check"]
    assert check["ok"] is False
    assert "v0.5.0-rc2-3hotfixesbehind" in check["observed_fe_bundles"]
    assert check["mismatch_count"] >= 1


def test_ready_gate_observational_in_prod_dev(monkeypatch):
    """OP-1483 NON-GOAL preserved when the staging flag is UNSET.

    In prod/dev/CI (``OMNISIGHT_REQUIRE_FRONTEND_COMPAT`` unset) the
    frontend_compat_check is observational: ``gate_enforced`` is False
    and a skew must NOT participate in the ``ready`` gate — a skew the
    request side cannot fix must never eject a prod replica from
    rotation. We assert the gate boolean is short-circuited to True
    regardless of the (False) ``ok`` verdict.
    """
    monkeypatch.delenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", raising=False)
    check = {"ok": False, "gate_enforced": health_mod._frontend_compat_required()}
    compat_gate_ok = (not check["gate_enforced"]) or check["ok"]
    assert check["gate_enforced"] is False
    assert compat_gate_ok is True


# ───── RT-05c (OP-1575): FE/BE compat = hard staging gate ─────────────


def test_frontend_compat_required_reads_env(monkeypatch):
    """The staging-gate opt-in honours the same truthy set as the
    deep-check / deploy-overlay knobs (1/true/yes, case-insensitive)."""
    monkeypatch.delenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", raising=False)
    assert health_mod._frontend_compat_required() is False
    for truthy in ("1", "true", "TRUE", "yes", "Yes"):
        monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", truthy)
        assert health_mod._frontend_compat_required() is True
    for falsy in ("", "0", "false", "no"):
        monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", falsy)
        assert health_mod._frontend_compat_required() is False


def test_check_frontend_compat_gate_enforced_flag(baked_bundle, monkeypatch):
    """``_check_frontend_compat`` surfaces ``gate_enforced`` mirroring the
    env flag, additive to the OP-1483 contract fields."""
    monkeypatch.delenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", raising=False)
    out = health_mod._check_frontend_compat()
    assert out["gate_enforced"] is False
    # OP-1483 contract fields remain present (additive change only).
    for field in ("ok", "detail", "observed_fe_bundles", "mismatch_count"):
        assert field in out

    monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", "1")
    out = health_mod._check_frontend_compat()
    assert out["gate_enforced"] is True


@pytest.mark.asyncio
async def test_readyz_skew_blocks_gate_when_required(
    tmp_path, monkeypatch, baked_bundle,
):
    """RT-05c AC: an injected FE/BE bundle skew BLOCKS the staging gate.

    With ``OMNISIGHT_REQUIRE_FRONTEND_COMPAT`` set (staging compose) a
    recorded skew flips ``/readyz`` to 503 — the signal the staging
    canary probe (``backend/agents/staging_gate.py`` GETs /readyz) turns
    into a red gate, blocking promote. All other gates are pinned green
    so the skew is the only thing flipping ``ready`` to False.
    """
    monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", "1")

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))
    monkeypatch.setattr(health_mod, "_check_deploy_overlay", lambda: (True, "ok"))

    # Inject the skew the Codex 2026-05-18 incident produced.
    frontend_compat.record_observation(
        "v0.5.0-rc2-3hotfixesbehind", "v1",
        be_bundle="v0.5.0-rc3-3f1c0a4e",
    )

    resp = await health_mod._readyz_handler()
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 503
    assert body["ready"] is False
    check = body["checks"]["frontend_compat_check"]
    assert check["gate_enforced"] is True
    assert check["ok"] is False
    assert "v0.5.0-rc2-3hotfixesbehind" in check["observed_fe_bundles"]


@pytest.mark.asyncio
async def test_readyz_skew_observational_when_not_required(
    tmp_path, monkeypatch, baked_bundle,
):
    """Same skew, flag UNSET (prod/dev): /readyz stays 200 ready.

    Proves RT-05c does not regress the OP-1483 prod NON-GOAL — a skew is
    reported (ok=False) but does NOT eject the replica from rotation.
    """
    monkeypatch.delenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", raising=False)

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))
    monkeypatch.setattr(health_mod, "_check_deploy_overlay", lambda: (True, "ok"))

    frontend_compat.record_observation(
        "v0.5.0-rc2-3hotfixesbehind", "v1",
        be_bundle="v0.5.0-rc3-3f1c0a4e",
    )

    resp = await health_mod._readyz_handler()
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["ready"] is True
    check = body["checks"]["frontend_compat_check"]
    assert check["gate_enforced"] is False
    assert check["ok"] is False  # still reported — observational


@pytest.mark.asyncio
async def test_readyz_match_passes_gate_when_required(
    tmp_path, monkeypatch, baked_bundle,
):
    """Flag set + matching FE observation → /readyz stays 200.

    A non-skewed staging deploy must not be blocked by the hard gate.
    """
    monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", "1")

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))
    monkeypatch.setattr(health_mod, "_check_deploy_overlay", lambda: (True, "ok"))

    frontend_compat.record_observation(
        "v0.5.0-rc3-3f1c0a4e", "v1",
        be_bundle="v0.5.0-rc3-3f1c0a4e",
    )

    resp = await health_mod._readyz_handler()
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["ready"] is True
    assert body["checks"]["frontend_compat_check"]["ok"] is True


@pytest.mark.asyncio
async def test_readyz_no_fe_traffic_passes_gate_when_required(
    tmp_path, monkeypatch, baked_bundle,
):
    """Flag set but no FE traffic yet → /readyz stays 200 ready.

    A freshly-booted staging replica that has not received the injected
    probe must not fail closed before any observation exists — the gate
    fires on an *observed* skew, not on silence (OP-1483 case (a)).
    """
    monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", "1")

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))
    monkeypatch.setattr(health_mod, "_check_deploy_overlay", lambda: (True, "ok"))

    resp = await health_mod._readyz_handler()
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["ready"] is True
    assert body["checks"]["frontend_compat_check"]["ok"] is True
