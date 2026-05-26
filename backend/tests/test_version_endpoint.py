"""Public ``GET /version`` image-surfacing endpoint (Family ⑤ §3, OP-1745).

Covers the §3.5 test contract from
docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md:

  1. Happy path        — 200 + the exact 5-field §3.2 wire shape.
  2. Missing manifest  — 503 ``manifest_unavailable`` (§3.3).
  3. Malformed manifest — 503 ``manifest_unavailable`` (§3.3).
  4. Schema mismatch    — 503 ``manifest_invalid`` (§3.5 case 4).
  5. Auth bypass        — 200 under ``OMNISIGHT_AUTH_BASELINE_MODE=enforce``
                          with no Authorization header (regression that
                          keeps a future Family ⑦ drift from gating
                          /version).

Case 6 (latency / cache-after-startup) from §3.5 is intentionally NOT
asserted here: the endpoint reuses the existing per-request
``_load_image_manifest_version_fields`` reader unchanged (per the OP-1745
"wire the ROUTE" scope), so the "no further filesystem opens" cache
assertion would not hold without a separate caching change that is out of
this ticket's 4-AC scope. A ~200-byte JSON read per request is sub-ms.
"""

from pathlib import Path

import pytest

from backend import auth_baseline
from backend.routers import system


VERSION_URL = "/version"

# A clearly non-public, non-allowlisted path used as the control in the
# auth-bypass test — proves ``enforce`` mode is actually live (so the
# /version 200 is a real bypass, not a vacuous pass).
PROTECTED_URL = "/api/v1/runtime/info"


def _write_full_manifest(path: Path) -> None:
    path.write_text(
        (
            '{"image_sha":"sha256:op1745","build_time":"2026-05-26T00:00:00Z",'
            '"git_ref":"b782b8b8c4e7f1d2a3b4c5d6e7f8a9b0c1d2e3f4",'
            '"alembic_head_in_image":"0204_add_runner_claims_table"}'
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def full_manifest(tmp_path, monkeypatch) -> Path:
    manifest = tmp_path / "MANIFEST.json"
    _write_full_manifest(manifest)
    monkeypatch.setattr(system, "_IMAGE_MANIFEST_PATH", manifest)
    return manifest


# ── §3.5 case 1: happy path ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_version_happy_path_returns_200_with_five_fields(client, full_manifest):
    resp = await client.get(VERSION_URL)

    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "image_sha": "sha256:op1745",
        "build_time": "2026-05-26T00:00:00Z",
        "git_ref": "b782b8b8c4e7f1d2a3b4c5d6e7f8a9b0c1d2e3f4",
        "alembic_head_in_image": "0204_add_runner_claims_table",
        # §4.1: manifest_path reports the actual read location so a future
        # image-layout change is a visible contract change, not a silent
        # break. Here that is the monkeypatched fixture path; in the image
        # it is the /app/MANIFEST.json constant (locked below).
        "manifest_path": str(full_manifest),
    }
    # §3.2: exactly these five fields, nothing extra.
    assert set(data) == {
        "image_sha",
        "build_time",
        "git_ref",
        "alembic_head_in_image",
        "manifest_path",
    }


# ── §3.2 / §4.1: the in-image manifest_path constant is /app/MANIFEST.json ──
def test_image_manifest_path_constant_matches_contract():
    assert str(system._IMAGE_MANIFEST_PATH) == "/app/MANIFEST.json"


# ── §3.5 case 2: missing manifest ────────────────────────────────────
@pytest.mark.asyncio
async def test_version_missing_manifest_returns_503_unavailable(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(system, "_IMAGE_MANIFEST_PATH", tmp_path / "absent.json")

    resp = await client.get(VERSION_URL)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "manifest_unavailable"
    assert "remediation" in body and body["remediation"]


# ── §3.5 case 3: malformed manifest ──────────────────────────────────
@pytest.mark.asyncio
async def test_version_malformed_manifest_returns_503_unavailable(
    client, tmp_path, monkeypatch
):
    bad = tmp_path / "MANIFEST.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(system, "_IMAGE_MANIFEST_PATH", bad)

    resp = await client.get(VERSION_URL)

    assert resp.status_code == 503
    assert resp.json()["error"] == "manifest_unavailable"


# ── §3.5 case 4: schema mismatch (missing required field) ────────────
@pytest.mark.asyncio
async def test_version_schema_mismatch_returns_503_invalid(
    client, tmp_path, monkeypatch
):
    partial = tmp_path / "MANIFEST.json"
    # Valid JSON object, but missing the required ``git_ref`` field.
    partial.write_text(
        (
            '{"image_sha":"sha256:op1745","build_time":"2026-05-26T00:00:00Z",'
            '"alembic_head_in_image":"0204_add_runner_claims_table"}'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system, "_IMAGE_MANIFEST_PATH", partial)

    resp = await client.get(VERSION_URL)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "manifest_invalid"
    assert "remediation" in body and body["remediation"]


# ── §3.5 case 5: auth bypass under enforce ───────────────────────────
@pytest.mark.asyncio
async def test_version_public_under_baseline_enforce(client, full_manifest, monkeypatch):
    """200 with no Authorization header even when the secure-by-default
    baseline is flipped to ``enforce``. The control assertion proves
    enforce is live (a protected path 401s) so the /version pass is a
    genuine allowlist bypass."""
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")

    # Control: a non-allowlisted path must be rejected under enforce.
    blocked = await client.get(PROTECTED_URL)
    assert blocked.status_code == 401

    # /version stays public.
    resp = await client.get(VERSION_URL)
    assert resp.status_code == 200
    assert resp.json()["image_sha"] == "sha256:op1745"


# ── Allowlist wiring (unit-level guard) ──────────────────────────────
def test_version_is_on_baseline_allowlist():
    assert auth_baseline._path_allowed("/version") is True
