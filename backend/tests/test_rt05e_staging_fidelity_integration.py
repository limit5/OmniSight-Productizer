"""[OP-1577] RT-05e — staging-fidelity integration (= activation evidence).

This is the integration verifier that ties the three staging-fidelity gates
landed by RT-05a..d into a single end-to-end narrative for one candidate
bundle. It is *integration only* — no feature code: every behaviour it
asserts is implemented elsewhere; this file proves the seams hold when the
real artifacts are driven together.

The candidate's journey onto staging must clear three gates, in order:

1. **missing-ref reject** (RT-05a / OP-1573 — ``deploy/staging/docker-compose.yml``).
   The real compose file is rendered with ``docker compose config``. With no
   ``OMNISIGHT_IMAGE_TAG`` the render fails closed — staging can never fall
   back to a default/``latest`` tag. With the candidate's tag set it renders
   the GitLab-CR refs with ``pull_policy: always``.

2. **digest-match** (RT-05b / OP-1574 — ``scripts/staging_deploy.sh``).
   The real deploy script's post-pull digest-equality functions are sourced
   and driven against a stub ``docker image inspect``. The pulled
   backend+frontend digests must equal the certified candidate bundle; a
   digest the alias was retagged to under us is REJECTED before the standby
   stack starts.

3. **skew-block** (RT-05c / OP-1575 + RT-05d / OP-1576 — ``backend.routers.health``
   feeding ``backend.agents.staging_gate``).
   With the staging hard-gate flag set, an injected FE/BE bundle skew flips
   the live ``/readyz`` to 503. The *real* staging canary probe consumes that
   ``/readyz`` and turns it into a RED gate (exit 2) — the signal that blocks
   promote. A matching FE bundle leaves the canary GREEN.

One ``_FIXTURE_BUNDLE`` (backend ``sha256:aa…``, frontend ``sha256:bb…``) is
threaded through gates 2 and 3 so the three gates are demonstrably guarding
the *same* candidate, not three unrelated fixtures.

Cost: stdlib + pytest. The compose check shells out to a real ``docker
compose config`` (offline render) and skips if the CLI is absent; the digest
gate stubs ``docker``; the skew gate runs in-process.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend import api_versioning as av
from backend import frontend_compat
from backend.agents import staging_gate as sg
from backend.routers import health as health_mod


REPO_ROOT = Path(__file__).resolve().parents[2]
STAGING_COMPOSE = REPO_ROOT / "deploy" / "staging" / "docker-compose.yml"
STAGING_DEPLOY_SH = REPO_ROOT / "scripts" / "staging_deploy.sh"
GITLAB_CR = "sora.services:49154/omnisight/OmniSight-Productizer"

#: The one candidate carried through every gate below.
CANDIDATE_TAG = "sha-op1577cand"
BACKEND_DIGEST = "sha256:" + "a" * 64
FRONTEND_DIGEST = "sha256:" + "b" * 64
CANDIDATE_SHA = "0123456789abcdef0123456789abcdef01234567"
BE_BUNDLE_ID = "v0.5.0-rc3-3f1c0a4e"
SKEWED_FE_BUNDLE = "v0.5.0-rc2-3hotfixesbehind"  # the Codex 2026-05-18 skew

#: Shape consumed by both ``staging_deploy.sh`` (images.{backend,frontend}.digest)
#: and ``api_versioning.build_version_payload`` (bundle_id + images).
_FIXTURE_BUNDLE = {
    "bundle_id": BE_BUNDLE_ID,
    "git_ref": "refs/tags/v0.5.0-rc3",
    "git_sha": "3f1c0a4e" + "0" * 32,
    "build_time": "2026-05-18T00:00:00Z",
    "images": {
        "backend": {"digest": BACKEND_DIGEST},
        "frontend": {"digest": FRONTEND_DIGEST},
        "bridge": {"digest": "sha256:" + "c" * 64},
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


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture()
def candidate_bundle_file(tmp_path: Path) -> Path:
    path = tmp_path / "candidate-bundle.json"
    path.write_text(json.dumps(_FIXTURE_BUNDLE), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_compat_ring():
    frontend_compat.reset_for_tests()
    yield
    frontend_compat.reset_for_tests()


# ───────────────── gate 1 — missing-ref reject (RT-05a) ──────────────────


def _compose_env(*, include_tag: bool) -> dict[str, str]:
    env = os.environ.copy()
    env["POSTGRES_PASSWORD"] = "test-password"
    env.pop("OMNISIGHT_REGISTRY", None)
    env.pop("OMNISIGHT_IMAGE_TAG", None)
    if include_tag:
        env["OMNISIGHT_IMAGE_TAG"] = CANDIDATE_TAG
    return env


def _compose_config(*, include_tag: bool, json_out: bool) -> subprocess.CompletedProcess:
    argv = ["docker", "compose", "--profile", "bridge", "-f", str(STAGING_COMPOSE), "config"]
    argv += ["--format", "json"] if json_out else ["--quiet"]
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=_compose_env(include_tag=include_tag),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_gate1_missing_ref_rejected_then_pinned_ref_renders() -> None:
    """A candidate with no pinned ref cannot even render the staging stack;
    once its tag is supplied the GitLab-CR refs render with pull_policy:always.
    """
    if not _docker_available():
        pytest.skip("docker CLI not available")

    # missing ref → fail closed, naming the offending var.
    rejected = _compose_config(include_tag=False, json_out=False)
    assert rejected.returncode != 0
    assert "OMNISIGHT_IMAGE_TAG" in rejected.stderr

    # pinned ref → renders the candidate's image refs, no :latest / ghcr.io.
    rendered = _compose_config(include_tag=True, json_out=True)
    assert rendered.returncode == 0, rendered.stderr
    assert ":latest" not in rendered.stdout
    assert "ghcr.io/" not in rendered.stdout
    services = json.loads(rendered.stdout)["services"]
    assert services["backend-a"]["image"] == f"{GITLAB_CR}/backend:{CANDIDATE_TAG}"
    assert services["frontend"]["image"] == f"{GITLAB_CR}/frontend:{CANDIDATE_TAG}"
    assert services["backend-a"]["pull_policy"] == "always"


# ───────────────── gate 2 — digest-match (RT-05b) ────────────────────────

# A stub `docker image inspect <ref> --format {{json .RepoDigests}}` that
# reports whatever digest the test pins per image, so we can model both the
# certified candidate and a hostile retag-under-us.
_STUB_DOCKER = """#!/usr/bin/env bash
# argv: image inspect <ref> --format {{json .RepoDigests}}
ref="$3"
if [[ "$ref" == *"/backend:"* ]]; then
    printf '["%s@%s"]\\n' "${ref%:*}" "$STUB_BACKEND_DIGEST"
else
    printf '["%s@%s"]\\n' "${ref%:*}" "$STUB_FRONTEND_DIGEST"
fi
"""


def _run_digest_gate(
    tmp_path: Path,
    bundle: Path,
    *,
    pulled_backend: str,
    pulled_frontend: str,
) -> subprocess.CompletedProcess:
    """Source the real staging_deploy.sh and drive verify_pulled_digests with
    a stub docker reporting the given pulled digests."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(_STUB_DOCKER, encoding="utf-8")
    docker.chmod(0o755)

    env = os.environ.copy()
    env["DOCKER_BIN"] = str(docker)
    env["OMNISIGHT_CANDIDATE_BUNDLE"] = str(bundle)
    env["OMNISIGHT_REGISTRY"] = GITLAB_CR
    env["STUB_BACKEND_DIGEST"] = pulled_backend
    env["STUB_FRONTEND_DIGEST"] = pulled_frontend
    env.pop("OMNISIGHT_STAGING_ALERT_WEBHOOK", None)

    # Source the script (it self-guards against running main when sourced),
    # then exercise just the post-pull digest-equality gate.
    harness = (
        f'source "{STAGING_DEPLOY_SH}"\n'
        f'if verify_pulled_digests "{CANDIDATE_TAG}"; then echo DIGEST_OK; '
        f'else echo DIGEST_REJECTED; fi\n'
    )
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, env=env, timeout=30
    )


def test_gate2_matching_pulled_digest_accepted(
    tmp_path: Path, candidate_bundle_file: Path
) -> None:
    """Pulled backend+frontend digests == certified candidate bundle → accept."""
    res = _run_digest_gate(
        tmp_path,
        candidate_bundle_file,
        pulled_backend=BACKEND_DIGEST,
        pulled_frontend=FRONTEND_DIGEST,
    )
    assert res.returncode == 0, res.stderr
    assert "DIGEST_OK" in res.stdout
    assert "DIGEST_REJECTED" not in res.stdout
    assert "pulled digests == candidate bundle" in res.stdout


def test_gate2_retagged_digest_rejected(
    tmp_path: Path, candidate_bundle_file: Path
) -> None:
    """The backend alias was retagged to a different image between candidate
    certification and pull → digest mismatch → REJECTED before standby start."""
    res = _run_digest_gate(
        tmp_path,
        candidate_bundle_file,
        pulled_backend="sha256:" + "f" * 64,  # not what the bundle pins
        pulled_frontend=FRONTEND_DIGEST,
    )
    assert "DIGEST_REJECTED" in res.stdout
    assert "DIGEST_OK" not in res.stdout
    # the rejection names the candidate digest it expected
    assert BACKEND_DIGEST in res.stderr


# ───────────────── gate 3 — skew-block (RT-05c/RT-05d) ───────────────────


def _readyz_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[int, str]:
    """Render the live /readyz once with all non-compat checks pinned green,
    returning (status_code, body) — the exact bytes the staging canary GETs."""
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_FIXTURE_BUNDLE), encoding="utf-8")
    monkeypatch.setattr(av, "BUNDLE_MANIFEST_PATH", path)
    monkeypatch.setattr(av, "_IMAGE_MANIFEST_PATH", tmp_path / "MANIFEST.json")

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))
    monkeypatch.setattr(health_mod, "_check_deploy_overlay", lambda: (True, "ok"))

    resp = asyncio.run(health_mod._readyz_handler())
    return resp.status_code, bytes(resp.body).decode("utf-8")


def _canary_against_readyz(tmp_path: Path, status: int, body: str) -> sg.GateRunResult:
    """Drive the real staging canary gate against a /readyz that returns
    (status, body) — exactly what backend.agents.staging_gate does in prod."""
    out = tmp_path / "canary-status.jsonl"

    def opener(_url: str, _timeout: float) -> tuple[int, str]:
        return status, body

    def probe() -> tuple[bool, str]:
        return sg.http_probe(
            base_url="https://staging.invalid",
            paths=("/readyz",),
            attempts=1,
            interval=0.0,
            timeout=1.0,
            opener=opener,
            sleeper=lambda _s: None,
        )

    return sg.run_gate(
        suite=sg.SUITE_CANARY,
        revision=CANDIDATE_SHA,
        probe=probe,
        out_path=out,
        audit_sink=lambda *_a, **_k: None,
    )


def test_gate3_skew_blocks_the_staging_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT-05c skew → /readyz 503 → real staging canary goes RED (blocks promote).

    The whole chain runs with the real artifacts: an injected FE/BE skew, the
    real ``_readyz_handler`` hard gate, and the real ``staging_gate`` canary
    consuming that /readyz.
    """
    monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", "1")
    frontend_compat.record_observation(
        SKEWED_FE_BUNDLE, "v1", be_bundle=BE_BUNDLE_ID
    )

    status, body = _readyz_status(monkeypatch, tmp_path)
    assert status == 503, body
    parsed = json.loads(body)
    assert parsed["ready"] is False
    assert parsed["checks"]["frontend_compat_check"]["gate_enforced"] is True
    assert SKEWED_FE_BUNDLE in parsed["checks"]["frontend_compat_check"]["observed_fe_bundles"]

    result = _canary_against_readyz(tmp_path, status, body)
    assert result.exit_code == 2  # RED — promote is blocked
    assert result.record is not None and result.record.status == sg.STATUS_RED


def test_gate3_matching_bundle_keeps_canary_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same hard gate, a matching FE bundle → /readyz 200 → canary GREEN.

    Proves the skew-block does not flip green on a faithful staging deploy of
    the candidate — the activation-evidence positive case."""
    monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", "1")
    frontend_compat.record_observation(
        BE_BUNDLE_ID, "v1", be_bundle=BE_BUNDLE_ID
    )

    status, body = _readyz_status(monkeypatch, tmp_path)
    assert status == 200, body
    assert json.loads(body)["checks"]["frontend_compat_check"]["ok"] is True

    result = _canary_against_readyz(tmp_path, status, body)
    assert result.exit_code == 0  # GREEN — promote may proceed
    assert result.record is not None and result.record.status == sg.STATUS_GREEN


# ───────────────── full chain — one candidate, three gates ───────────────


def test_staging_fidelity_chain_for_one_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_bundle_file: Path
) -> None:
    """Activation evidence: the same candidate clears (or is stopped by) each
    gate in deploy order. Asserts the seams compose, end to end.

    * gate 1: pinned ref renders (missing ref already proven rejected above).
    * gate 2: certified digest accepted; a retag is rejected.
    * gate 3: faithful bundle → canary green; a skew → canary red.
    """
    # gate 1 — the candidate's pinned ref renders the staging stack.
    if _docker_available():
        rendered = _compose_config(include_tag=True, json_out=True)
        assert rendered.returncode == 0, rendered.stderr
        assert (
            json.loads(rendered.stdout)["services"]["backend-a"]["image"]
            == f"{GITLAB_CR}/backend:{CANDIDATE_TAG}"
        )

    # gate 2 — the certified candidate digest is accepted.
    ok = _run_digest_gate(
        tmp_path, candidate_bundle_file,
        pulled_backend=BACKEND_DIGEST, pulled_frontend=FRONTEND_DIGEST,
    )
    assert "DIGEST_OK" in ok.stdout, ok.stderr

    # gate 3 — a faithful (non-skewed) deploy keeps the staging canary green.
    monkeypatch.setenv("OMNISIGHT_REQUIRE_FRONTEND_COMPAT", "1")
    frontend_compat.record_observation(BE_BUNDLE_ID, "v1", be_bundle=BE_BUNDLE_ID)
    status, body = _readyz_status(monkeypatch, tmp_path)
    green = _canary_against_readyz(tmp_path, status, body)
    assert green.exit_code == 0
