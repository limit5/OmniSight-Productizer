"""OP-1765 — Family ⑤ end-to-end integration (§10.3 of the image-surfacing
contract, ``docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md``).

This is the *integration deliverable* for the Family ⑤ chain. Unlike the
per-component suites (``test_version_endpoint.py`` OP-1745,
``test_deployment_audit_image_sha.py`` OP-1753,
``test_deployment_audit_evidence.py`` OP-1761) which each stub their
neighbour, this test wires the **whole truth-source join** together once and
asserts the join produces the correct ``result_state`` + §6.2 evidence JSON:

    baked MANIFEST.json (OP-1751 shape)
        → the REAL ``GET /version`` route handler (OP-1745) reads it
        → served over HTTP on a throwaway loopback port
        → ``scripts/deployment-audit.sh`` genuinely ``curl``s it as T1 (OP-1753)
        → the image-SHA state machine joins T1 vs the stubbed registry digest
          (T2) vs the SAME baked manifest (T4)
        → the §6 evidence writer emits YYYY-MM-DD.json + latest.json + index.json
          (OP-1761).

Crucially we do NOT set ``OMNISIGHT_AUDIT_T1_VERSION_JSON`` — T1 flows over the
real curl path so the recorded ``truth_sources.T1_running_version`` is whatever
the live endpoint produced from the baked manifest. That is the join under test.

Three §10.3 scenarios are exercised:

  * OK              — T1.image_sha == T2.digest == T4.manifest.image_sha.
  * WARN_STALE_IMAGE — registry digest drifted but the push is recent
                       (integrity still holds: T1 == T4).
  * INCOMPLETE      — the baked manifest is missing a required §3.2 field, so the
                      real endpoint returns 503 and ``curl -f`` fails → T1
                      unreadable → INCOMPLETE with NO false-positive stale alert.

MUST NOT (per ticket): this is verification-only — it copies the shipped script
into a throwaway repo and never edits the OP-1753 state machine or the OP-1761
evidence writer, and it never requires a live registry (T2 is stubbed).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from backend.routers import system

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "deployment-audit.sh"

# The image identity that is "running" — baked into MANIFEST.json and therefore
# surfaced by /version (T1) and re-read as the integrity source (T4).
SHA_RUNNING = "sha256:" + "a" * 64
# A different digest for ":latest" in the registry → drift between T1 and T2.
SHA_REGISTRY_DRIFT = "sha256:" + "b" * 64

NOW = "2026-05-27T12:00:00Z"
TODAY_FILE = "2026-05-27.json"
# 1h before NOW → inside the 24h stale window → WARN (not PAGE).
PUSHED_RECENT = "2026-05-27T11:00:00Z"

GIT_REF = "b782b8b8c4e7f1d2a3b4c5d6e7f8a9b0c1d2e3f4"
BUILD_TIME = "2026-05-26T00:00:00Z"
ALEMBIC_HEAD = "0204_add_runner_claims_table"


# ── the baked image manifest (OP-1751 bake output shape) ──────────────────────
def _manifest_doc(*, complete: bool = True) -> dict:
    doc = {
        "image_sha": SHA_RUNNING,
        "build_time": BUILD_TIME,
        "git_ref": GIT_REF,
        "alembic_head_in_image": ALEMBIC_HEAD,
    }
    if not complete:
        # Drop a required §3.2 field while keeping image_sha: the real endpoint
        # then returns 503 manifest_invalid, but the file is still a readable
        # manifest for T4 — isolating /version as the sole INCOMPLETE driver.
        doc.pop("alembic_head_in_image")
    return doc


@pytest.fixture()
def baked_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a baked MANIFEST.json and point the REAL endpoint reader at it.

    Returns a writer ``bake(complete=...)`` so each scenario can choose a full
    or schema-incomplete manifest; the monkeypatched ``_IMAGE_MANIFEST_PATH`` is
    read by ``system.get_version()`` at request time (the HTTP handler below).
    """
    manifest = tmp_path / "MANIFEST.json"
    monkeypatch.setattr(system, "_IMAGE_MANIFEST_PATH", manifest)

    def bake(*, complete: bool = True) -> Path:
        manifest.write_text(json.dumps(_manifest_doc(complete=complete)), encoding="utf-8")
        return manifest

    return bake


# ── live /version endpoint: the REAL OP-1745 route, served over HTTP ──────────
def _render_version() -> tuple[int, bytes]:
    """Invoke the real ``GET /version`` handler and normalise its response.

    Happy path returns a plain dict (→ 200); the §3.3/§3.5 failure modes return
    a FastAPI ``JSONResponse`` carrying the 503 status + body. Either way we
    serve exactly what the shipped endpoint produced from the baked manifest.
    """
    result = asyncio.run(system.get_version())
    if isinstance(result, dict):
        return 200, json.dumps(result).encode("utf-8")
    return result.status_code, bytes(result.body)


class _VersionHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server contract)
        if self.path != "/version":
            self.send_error(404, "not found")
            return
        status, body = _render_version()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence per-request stderr noise
        return


@contextmanager
def _live_version_endpoint() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/version"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


# ── the shipped deployment-audit.sh, driven against the live endpoint ─────────
@pytest.fixture()
def audit_repo(tmp_path: Path) -> Path:
    """Throwaway repo holding a COPY of the shipped script (verification-only)."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REAL_SCRIPT, repo / "scripts" / "deployment-audit.sh")
    os.chmod(repo / "scripts" / "deployment-audit.sh", 0o755)
    return repo


def _run_audit(
    repo: Path,
    *,
    version_url: str,
    evidence_dir: Path,
    t4_manifest_json: str,
    t2_digest: str,
    t2_pushed_at: str,
) -> subprocess.CompletedProcess:
    manifest = repo / "manifest.tsv"
    manifest.write_text("image-sha\tbackend\tyes\tOP-1753\tfixture\n", encoding="utf-8")
    env = {
        **os.environ,
        "USER": os.environ.get("USER", "tester"),
        "DEPLOYMENT_AUDIT_USER_SYSTEMD": "0",
        "OMNISIGHT_AUDIT_NOW": NOW,
        "OMNISIGHT_AUDIT_EVIDENCE_DIR": str(evidence_dir),
        # E2E: T1 is fetched by the script's own curl from the LIVE endpoint.
        "OMNISIGHT_AUDIT_VERSION_URL": version_url,
        # T2 (registry :latest) is stubbed — MUST NOT require a live registry.
        "OMNISIGHT_AUDIT_T2_DIGEST": t2_digest,
        "OMNISIGHT_AUDIT_T2_PUSHED_AT": t2_pushed_at,
        # T4 is the SAME baked manifest the endpoint surfaced (integrity axis).
        "OMNISIGHT_AUDIT_T4_MANIFEST_JSON": t4_manifest_json,
    }
    # Force the real curl path for T1 — the direct-feed override must be absent.
    env.pop("OMNISIGHT_AUDIT_T1_VERSION_JSON", None)
    return subprocess.run(
        [str(repo / "scripts" / "deployment-audit.sh"), str(manifest)],
        capture_output=True,
        text=True,
        env=env,
    )


def _evidence(evidence_dir: Path) -> dict:
    return json.loads((evidence_dir / TODAY_FILE).read_text(encoding="utf-8"))


def _latest(evidence_dir: Path) -> dict:
    return json.loads((evidence_dir / "latest.json").read_text(encoding="utf-8"))


def _index(evidence_dir: Path) -> dict:
    return json.loads((evidence_dir / "index.json").read_text(encoding="utf-8"))


# ── §10.3 scenario 1: OK — the full join agrees ───────────────────────────────
def test_e2e_ok_join_emits_ok_evidence(
    audit_repo: Path, baked_manifest, tmp_path: Path
) -> None:
    manifest = baked_manifest(complete=True)
    ev = tmp_path / "ev"

    with _live_version_endpoint() as url:
        res = _run_audit(
            audit_repo,
            version_url=url,
            evidence_dir=ev,
            t4_manifest_json=manifest.read_text(encoding="utf-8"),
            t2_digest=SHA_RUNNING,  # registry agrees with running image
            t2_pushed_at=PUSHED_RECENT,
        )

    assert res.returncode == 0, res.stdout + res.stderr
    doc = _evidence(ev)

    assert doc["schema_version"] == 1
    assert doc["result_state"] == "OK"
    assert doc["audit_run_at"] == NOW

    # The join: T1 was fetched over HTTP from the real endpoint, which read the
    # baked manifest — so every baked field threads through to the evidence.
    t1 = doc["truth_sources"]["T1_running_version"]
    assert t1["image_sha"] == SHA_RUNNING
    assert t1["build_time"] == BUILD_TIME
    assert t1["git_ref"] == GIT_REF
    assert t1["alembic_head_in_image"] == ALEMBIC_HEAD
    assert t1["manifest_path"] == str(manifest)  # endpoint surfaced its read path
    assert t1.get("error") is None
    assert t1["fetch_latency_ms"] is not None  # real curl round-trip, not a feed

    # T4 integrity source is the same baked manifest the endpoint read.
    assert doc["truth_sources"]["T4_image_manifest"]["source"] == str(manifest)
    assert doc["truth_sources"]["T4_image_manifest"]["image_sha"] == SHA_RUNNING

    assert doc["comparisons"]["T1_vs_T2_image_sha_match"] is True
    assert doc["comparisons"]["T1_image_sha_vs_T4_integrity"] is True
    assert doc["alerts_emitted"] == []
    assert doc["ghcr_query_evidence"]["method"] == "env-override OMNISIGHT_AUDIT_T2_DIGEST"

    # latest.json points at the OK run; OK runs are NOT in the incident index.
    assert _latest(ev)["result_state"] == "OK"
    assert _index(ev)["non_ok_runs"] == []


# ── §10.3 scenario 2: WARN_STALE_IMAGE — registry drift, recent push ──────────
def test_e2e_warn_stale_image_join(
    audit_repo: Path, baked_manifest, tmp_path: Path
) -> None:
    manifest = baked_manifest(complete=True)
    ev = tmp_path / "ev"

    with _live_version_endpoint() as url:
        res = _run_audit(
            audit_repo,
            version_url=url,
            evidence_dir=ev,
            t4_manifest_json=manifest.read_text(encoding="utf-8"),
            t2_digest=SHA_REGISTRY_DRIFT,  # registry moved ahead of running image
            t2_pushed_at=PUSHED_RECENT,
        )

    assert res.returncode == 0, res.stdout + res.stderr  # WARN is non-fatal
    doc = _evidence(ev)

    assert doc["result_state"] == "WARN_STALE_IMAGE"
    # Integrity still holds (T1 == T4), only the registry digest drifted.
    assert doc["comparisons"]["T1_image_sha_vs_T4_integrity"] is True
    assert doc["comparisons"]["T1_vs_T2_image_sha_match"] is False
    assert doc["comparisons"]["T1_vs_T2_age_seconds"] == 3600

    alerts = {a["alert"]: a.get("severity") for a in doc["alerts_emitted"]}
    assert alerts == {"OmniSightStaleImage": "warn"}

    assert _latest(ev)["result_state"] == "WARN_STALE_IMAGE"
    assert _index(ev)["non_ok_runs"] == [
        {"date": "2026-05-27", "result_state": "WARN_STALE_IMAGE", "alerts": ["OmniSightStaleImage"]}
    ]


# ── §10.3 scenario 3: INCOMPLETE — endpoint 503 → curl fails → T1 unreadable ──
def test_e2e_incomplete_join_when_endpoint_503s(
    audit_repo: Path, baked_manifest, tmp_path: Path
) -> None:
    manifest = baked_manifest(complete=False)  # missing alembic_head_in_image
    ev = tmp_path / "ev"

    # Sanity: the real endpoint genuinely 503s on the schema-incomplete manifest,
    # so the INCOMPLETE state below is driven by the live route, not a stub.
    status, _body = _render_version()
    assert status == 503

    with _live_version_endpoint() as url:
        res = _run_audit(
            audit_repo,
            version_url=url,
            evidence_dir=ev,
            t4_manifest_json=manifest.read_text(encoding="utf-8"),
            t2_digest=SHA_REGISTRY_DRIFT,
            t2_pushed_at=PUSHED_RECENT,
        )

    assert res.returncode == 0, res.stdout + res.stderr  # INCOMPLETE is WARN-level
    doc = _evidence(ev)

    assert doc["result_state"] == "INCOMPLETE"
    # T1 failed to read (curl -f rejected the 503); the failing axis carries its
    # error sub-field (§6.3).
    assert doc["truth_sources"]["T1_running_version"].get("error") == "/version unreadable"

    alerts = {a["alert"] for a in doc["alerts_emitted"]}
    # §6.4 false-positive avoidance: an INCOMPLETE run NEVER fires a stale alert,
    # even though T2 drifted — it surfaces the audit-incomplete gauge instead.
    assert "OmniSightStaleImage" not in alerts
    assert "OmniSightAuditIncomplete" in alerts

    assert _latest(ev)["result_state"] == "INCOMPLETE"
    assert _index(ev)["non_ok_runs"] == [
        {"date": "2026-05-27", "result_state": "INCOMPLETE", "alerts": ["OmniSightAuditIncomplete"]}
    ]
