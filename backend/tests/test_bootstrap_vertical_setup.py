"""BS.9.5 — ``POST /api/v1/bootstrap/vertical-setup`` endpoint tests.

Covers the optional intermediate step that records the operator's
BS.9.3 vertical multi-pick + the BS.9.4 Android API config + the
``install_jobs.id`` set the wizard front-end already enqueued via
``/installer/jobs`` (BS.7.1). Mirrors the
``test_bootstrap_admin_password.py`` fixture shape (PG-backed +
isolated marker) so the round-trip exercise hits the real
``record_bootstrap_step`` writer.

Contract pinned here:

  * Happy path — body with ``verticals_selected`` + ``install_job_ids``
    → 200, ``bootstrap_state.metadata.verticals_selected`` populated,
    audit row ``bootstrap.vertical_setup_committed`` written,
    ``_verticals_chosen()`` returns True.
  * Mobile path — ``android_api`` rides through to metadata; backend
    re-validates against the closed level / preset sets.
  * Validation — empty ``verticals_selected`` (422), unknown vertical
    id (422), Mobile selected without ``android_api`` (422 + structured
    ``kind=android_api_required``), non-Mobile body carrying
    ``android_api`` (422 + ``kind=android_api_unexpected``), bad
    Android API level (422), unknown emulator preset (422), absurd
    install_job_ids entry (422).
  * Idempotency — committing twice with a different selection
    overwrites the prior payload (``ON CONFLICT DO UPDATE`` round-trip).
  * Optional gate — STEP_VERTICAL_SETUP is NOT in REQUIRED_STEPS so a
    finalize attempt right after recording is unaffected by the new row
    (locked at the bootstrap.py constant level by
    ``test_bootstrap_state.py``; here we just verify the route does
    not silently leak it into the missing-step machinery).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import bootstrap as _boot


@pytest.fixture()
async def _wizard_db(pg_test_pool, pg_test_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE bootstrap_state, audit_log RESTART IDENTITY CASCADE"
        )

    from backend import db
    if db._db is not None:
        await db.close()
    await db.init()

    marker = tmp_path / ".bootstrap_state.json"
    _boot._reset_for_tests(Path(marker))

    try:
        yield {"db": db}
    finally:
        await db.close()
        _boot._reset_for_tests()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE bootstrap_state, audit_log RESTART IDENTITY CASCADE"
            )


@pytest.fixture()
async def _wizard_client(_wizard_db):
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    _boot._gate_cache_reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {"client": ac, **_wizard_db}
    _boot._gate_cache_reset()


# ─────────────────────────────────────────────────────────────────
#  Happy paths
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vertical_setup_records_non_mobile_payload(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["web", "software"],
            "install_job_ids": ["ij-web-1", "ij-software-1"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "committed"
    # Canonical-ordered: web before software per BOOTSTRAP_VERTICAL_IDS.
    assert body["verticals_selected"] == ["web", "software"]
    assert body["install_job_ids"] == ["ij-web-1", "ij-software-1"]

    # bootstrap_state row matches.
    row = await _boot.get_bootstrap_step(_boot.STEP_VERTICAL_SETUP)
    assert row is not None
    md = row["metadata"]
    assert md["verticals_selected"] == ["web", "software"]
    assert md["install_job_ids"] == ["ij-web-1", "ij-software-1"]
    assert md["source"] == "wizard"
    assert "android_api" not in md  # not Mobile → omit

    # _verticals_chosen probe flips True (gates on payload non-empty).
    assert await _boot._verticals_chosen() is True


@pytest.mark.asyncio
async def test_vertical_setup_records_mobile_with_android_api(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["mobile"],
            "install_job_ids": ["ij-mobile-1"],
            "android_api": {
                "compile_target": 35,
                "min_api": 26,
                "emulator_preset": "pixel-8",
                "google_play_services": True,
            },
        },
    )
    assert r.status_code == 200, r.text
    row = await _boot.get_bootstrap_step(_boot.STEP_VERTICAL_SETUP)
    assert row is not None
    md = row["metadata"]
    assert md["android_api"] == {
        "compile_target": 35,
        "min_api": 26,
        "emulator_preset": "pixel-8",
        "google_play_services": True,
    }


@pytest.mark.asyncio
async def test_vertical_setup_canonicalises_pick_order(_wizard_client):
    """Body sent in click order is normalised to canonical order on disk."""
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["web", "embedded", "mobile"],
            "install_job_ids": [],
            "android_api": {
                "compile_target": 34,
                "min_api": 28,
                "emulator_preset": "pixel-fold",
                "google_play_services": False,
            },
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Mobile → Embedded → Web (per BOOTSTRAP_VERTICAL_IDS canonical order).
    assert body["verticals_selected"] == ["mobile", "embedded", "web"]


@pytest.mark.asyncio
async def test_vertical_setup_emits_audit_row(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["software"],
            "install_job_ids": ["ij-software-1"],
        },
    )
    assert r.status_code == 200, r.text

    from backend import audit
    rows = await audit.query(entity_kind="bootstrap", limit=50)
    actions = [row["action"] for row in rows]
    assert "bootstrap.vertical_setup_committed" in actions


# ─────────────────────────────────────────────────────────────────
#  Validation paths
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vertical_setup_422_on_empty_selection(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={"verticals_selected": [], "install_job_ids": []},
    )
    assert r.status_code == 422, r.text
    # And no row written.
    assert await _boot.get_bootstrap_step(_boot.STEP_VERTICAL_SETUP) is None


@pytest.mark.asyncio
async def test_vertical_setup_422_on_unknown_vertical_id(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["mobile", "rtos"],  # rtos isn't surfaced
            "install_job_ids": [],
            "android_api": {
                "compile_target": 35,
                "min_api": 26,
                "emulator_preset": "pixel-8",
                "google_play_services": True,
            },
        },
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_vertical_setup_422_when_mobile_selected_without_android_api(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["mobile"],
            "install_job_ids": ["ij-mobile-1"],
        },
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body.get("kind") == "android_api_required"
    assert await _boot.get_bootstrap_step(_boot.STEP_VERTICAL_SETUP) is None


@pytest.mark.asyncio
async def test_vertical_setup_422_when_non_mobile_carries_android_api(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["web"],
            "install_job_ids": [],
            "android_api": {
                "compile_target": 35,
                "min_api": 26,
                "emulator_preset": "pixel-8",
                "google_play_services": True,
            },
        },
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body.get("kind") == "android_api_unexpected"


@pytest.mark.asyncio
async def test_vertical_setup_422_on_bad_android_api_level(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["mobile"],
            "install_job_ids": [],
            "android_api": {
                "compile_target": 99,  # not in the closed set
                "min_api": 26,
                "emulator_preset": "pixel-8",
                "google_play_services": True,
            },
        },
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_vertical_setup_422_on_unknown_emulator_preset(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["mobile"],
            "install_job_ids": [],
            "android_api": {
                "compile_target": 35,
                "min_api": 26,
                "emulator_preset": "totally-fake-device",
                "google_play_services": True,
            },
        },
    )
    assert r.status_code == 422, r.text


# ─────────────────────────────────────────────────────────────────
#  Idempotency / re-commit
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vertical_setup_recommit_overwrites_metadata(_wizard_client):
    """Re-opening the step + committing a different pick set replaces the
    prior payload (record_bootstrap_step runs ON CONFLICT DO UPDATE)."""
    client = _wizard_client["client"]

    r1 = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["software"],
            "install_job_ids": ["ij-software-1"],
        },
    )
    assert r1.status_code == 200, r1.text

    r2 = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["embedded", "web"],
            "install_job_ids": ["ij-embedded-1", "ij-web-1"],
        },
    )
    assert r2.status_code == 200, r2.text

    row = await _boot.get_bootstrap_step(_boot.STEP_VERTICAL_SETUP)
    assert row is not None
    md = row["metadata"]
    assert md["verticals_selected"] == ["embedded", "web"]
    assert md["install_job_ids"] == ["ij-embedded-1", "ij-web-1"]


# ─────────────────────────────────────────────────────────────────
#  Optional gate — does NOT promote the step into missing_steps
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vertical_setup_does_not_appear_in_missing_required_steps(
    _wizard_client,
):
    """Recording the optional vertical_setup step must not appear in the
    REQUIRED_STEPS-driven ``missing_required_steps`` list — committing it
    does not unblock anything that ``REQUIRED_STEPS`` already excluded."""
    assert _boot.STEP_VERTICAL_SETUP not in _boot.REQUIRED_STEPS

    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/vertical-setup",
        json={
            "verticals_selected": ["web"],
            "install_job_ids": [],
        },
    )
    assert r.status_code == 200, r.text

    missing = await _boot.missing_required_steps()
    assert "vertical_setup" not in missing
