r"""[OP-1482] preflight verifier + env-lock loader contract tests.

Covers the four ACs on the JIRA ticket:

  1. Code AC — `scripts/load_env_lock.sh` populates
     OMNISIGHT_{BACKEND,FRONTEND,BRIDGE}_DIGEST; the resulting
     `docker compose config` output pins each image by digest.
  2. Deploy AC — surrogated here by asserting compose-config emits
     `@sha256:` on each image line and never `:tag` (we can't run a
     real prod `docker compose up` from pytest, but the same compose
     parser path is the one the runbook calls).
  3. Integration AC — `verify_image_bundle.py` exits nonzero when
     (a) the lock digest disagrees with the local cache (Codex #15
     stale-cache failure mode), and (b) the cosign signature fails.
  4. Exercised AC — fixture simulates the Codex #15 failure mode by
     monkeypatching the docker-inspect helper to return a stale digest.

Notes on test isolation:

  * We DO NOT call out to a real docker daemon. The script's I/O is
    pushed through helper functions which are monkey-patched here so
    the test runs in any CI lane (no daemon, no registry network, no
    cosign install). The contract under test is the orchestration
    logic — "given remote-digest X, cache-digest Y, signature Z, do
    you exit 1 with the right diagnostic?" — not docker itself.

  * The compose-pin contract IS exercised end-to-end against the real
    `docker compose config` binary when it's on PATH (skipped
    otherwise) so a future YAML regression is caught here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
VERIFY = SCRIPTS / "verify_image_bundle.py"
LOAD_LOCK = SCRIPTS / "load_env_lock.sh"
PROD_LOCK = REPO_ROOT / "prod.env.lock.json"
STAGING_LOCK = REPO_ROOT / "staging.env.lock.json"
CANARY_LOCK = REPO_ROOT / "canary.env.lock.json"
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
STAGING_COMPOSE = REPO_ROOT / "docker-compose.staging.yml"

GOOD_DIGEST_A = "sha256:" + "a" * 64
GOOD_DIGEST_B = "sha256:" + "b" * 64
GOOD_DIGEST_C = "sha256:" + "c" * 64
GOOD_DIGEST_STALE = "sha256:" + "9" * 64
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


# Make scripts importable as a module so we can monkeypatch its helpers.
sys.path.insert(0, str(SCRIPTS))
import verify_image_bundle as vib  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────


def _write_lock(path: Path, *, backend: str, frontend: str, bridge: str,
                env: str = "test") -> Path:
    path.write_text(json.dumps({
        "env": env,
        "bundle_id": "test-bundle-abcdef",
        "last_promoted_at": "2026-05-18T00:00:00Z",
        "promoted_from": "ci",
        "attestation_ref": "sigstore://test/PLACEHOLDER",
        "images": {
            "backend": {
                "repository": "ghcr.io/test/omnisight-backend",
                "digest": backend,
                "env_var": "OMNISIGHT_BACKEND_DIGEST",
            },
            "frontend": {
                "repository": "ghcr.io/test/omnisight-frontend",
                "digest": frontend,
                "env_var": "OMNISIGHT_FRONTEND_DIGEST",
            },
            "bridge": {
                "repository": "ghcr.io/test/omnisight-bridge",
                "digest": bridge,
                "env_var": "OMNISIGHT_BRIDGE_DIGEST",
            },
        },
    }, indent=2))
    return path


def _write_compose(path: Path, *, backend_repo: str, frontend_repo: str,
                   pin_by_digest: bool = True) -> Path:
    if pin_by_digest:
        backend_ref = f"{backend_repo}@${{OMNISIGHT_BACKEND_DIGEST}}"
        frontend_ref = f"{frontend_repo}@${{OMNISIGHT_FRONTEND_DIGEST}}"
    else:
        backend_ref = f"{backend_repo}:latest"
        frontend_ref = f"{frontend_repo}:latest"
    path.write_text(
        "services:\n"
        f"  backend:\n    image: {backend_ref}\n    pull_policy: always\n"
        f"  frontend:\n    image: {frontend_ref}\n    pull_policy: always\n"
    )
    return path


# ── lock-file schema parsing ─────────────────────────────────────


class TestLockFileShape:
    def test_shipped_lock_files_load_clean(self):
        for path in (STAGING_LOCK, CANARY_LOCK, PROD_LOCK):
            lock = vib.LockFile.load(path)
            assert set(lock.images.keys()) == {"backend", "frontend", "bridge"}
            for entry in lock.images.values():
                assert entry["repository"].startswith("ghcr.io/")
                assert vib._DIGEST_RE.match(entry["digest"])
                assert entry["env_var"].startswith("OMNISIGHT_")
                assert entry["env_var"].endswith("_DIGEST")

    def test_invalid_digest_rejected(self, tmp_path):
        p = _write_lock(tmp_path / "lock.json",
                        backend="not-a-digest", frontend=GOOD_DIGEST_B,
                        bridge=GOOD_DIGEST_C)
        with pytest.raises(vib.BadInvocation, match="invalid digest"):
            vib.LockFile.load(p)

    def test_missing_images_object_rejected(self, tmp_path):
        p = tmp_path / "lock.json"
        p.write_text(json.dumps({"env": "x", "images": {}}))
        with pytest.raises(vib.BadInvocation, match="no 'images' object"):
            vib.LockFile.load(p)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(vib.BadInvocation, match="not found"):
            vib.LockFile.load(tmp_path / "nope.json")


# ── compose-pin contract ─────────────────────────────────────────


class TestComposeDigestPin:
    """AC #1 / #2 — every image line in shipped compose files is
    pinned by `@sha256:` once digests are populated, never by `:tag`."""

    def _docker_available(self) -> bool:
        return shutil.which("docker") is not None

    def test_prod_compose_pins_by_digest(self):
        text = PROD_COMPOSE.read_text()
        ghcr_image_lines = [
            line for line in text.splitlines()
            if "image: ghcr.io/" in line and "omnisight-" in line
            and "installer" not in line
        ]
        assert ghcr_image_lines, "no ghcr image refs found in prod compose"
        for line in ghcr_image_lines:
            assert "@${OMNISIGHT_" in line, (
                f"prod compose still uses tag interpolation: {line}"
            )
            assert ":${OMNISIGHT_IMAGE_TAG" not in line, (
                f"prod compose still has tag pin leftover: {line}"
            )

    def test_staging_compose_pins_by_digest(self):
        text = STAGING_COMPOSE.read_text()
        ghcr_image_lines = [
            line for line in text.splitlines()
            if "image: ghcr.io/" in line and "omnisight-" in line
        ]
        assert ghcr_image_lines, "no ghcr image refs found in staging compose"
        for line in ghcr_image_lines:
            assert "@${OMNISIGHT_" in line, (
                f"staging compose still uses tag interpolation: {line}"
            )

    def test_prod_compose_pull_policy_always(self):
        text = PROD_COMPOSE.read_text()
        assert "pull_policy: missing" not in (
            "\n".join(line for line in text.splitlines() if "omnisight-backend@" in line or "omnisight-frontend@" in line)
        )
        backend_blocks = re.findall(
            r"omnisight-backend@\$\{OMNISIGHT_BACKEND_DIGEST[^}]*\}\n\s+pull_policy:\s*(\w+)",
            text,
        )
        assert backend_blocks, "no backend image+pull_policy pair found"
        for policy in backend_blocks:
            assert policy == "always", (
                f"backend pull_policy is {policy!r}, expected 'always' for "
                "digest-pinned image (OP-1482)"
            )

    def test_staging_compose_pull_policy_always(self):
        text = STAGING_COMPOSE.read_text()
        backend_blocks = re.findall(
            r"omnisight-backend@\$\{OMNISIGHT_BACKEND_DIGEST[^}]*\}\n\s+pull_policy:\s*(\w+)",
            text,
        )
        assert backend_blocks
        for policy in backend_blocks:
            assert policy == "always"

    def test_docker_compose_config_emits_digest(self, tmp_path):
        """AC #1 — `docker compose config` materialises the digest into the
        rendered image: line. Skipped when docker isn't on PATH."""
        if not self._docker_available():
            pytest.skip("docker CLI not available")
        # Compose insists on a `.env` next to the compose file's project
        # root because the files declare `env_file: - .env`. Touch it
        # but immediately remove after — keeps the working tree clean.
        env_file = REPO_ROOT / ".env"
        created = False
        if not env_file.exists():
            env_file.write_text("")
            created = True
        try:
            env = {
                **os.environ,
                "OMNISIGHT_BACKEND_DIGEST": GOOD_DIGEST_A,
                "OMNISIGHT_FRONTEND_DIGEST": GOOD_DIGEST_B,
                "OMNISIGHT_BRIDGE_DIGEST": GOOD_DIGEST_C,
                "OMNISIGHT_GHCR_NAMESPACE": "test",
                "OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN": "stub",
                "OMNISIGHT_CLOUDFLARE_TUNNEL_ID": "stub",
                "OMNISIGHT_CLOUDFLARE_TUNNEL_HOSTNAME": "stub",
            }
            proc = subprocess.run(
                ["docker", "compose", "-f", str(PROD_COMPOSE), "config"],
                env=env, cwd=str(REPO_ROOT),
                capture_output=True, text=True, timeout=60,
            )
        finally:
            if created:
                env_file.unlink(missing_ok=True)
        assert proc.returncode == 0, f"docker compose config failed: {proc.stderr}"
        out = proc.stdout
        assert f"omnisight-backend@{GOOD_DIGEST_A}" in out, (
            "rendered compose missing backend digest pin"
        )
        assert f"omnisight-frontend@{GOOD_DIGEST_B}" in out, (
            "rendered compose missing frontend digest pin"
        )

    def test_docker_compose_config_missing_digest_errors(self, tmp_path):
        if not self._docker_available():
            pytest.skip("docker CLI not available")
        env_file = REPO_ROOT / ".env"
        created = False
        if not env_file.exists():
            env_file.write_text("")
            created = True
        try:
            env = {
                **os.environ,
                "OMNISIGHT_GHCR_NAMESPACE": "test",
                "OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN": "stub",
                "OMNISIGHT_CLOUDFLARE_TUNNEL_ID": "stub",
                "OMNISIGHT_CLOUDFLARE_TUNNEL_HOSTNAME": "stub",
            }
            env.pop("OMNISIGHT_BACKEND_DIGEST", None)
            env.pop("OMNISIGHT_FRONTEND_DIGEST", None)
            proc = subprocess.run(
                ["docker", "compose", "-f", str(PROD_COMPOSE), "config"],
                env=env, cwd=str(REPO_ROOT),
                capture_output=True, text=True, timeout=60,
            )
        finally:
            if created:
                env_file.unlink(missing_ok=True)
        assert proc.returncode != 0
        assert "OMNISIGHT_BACKEND_DIGEST" in (proc.stderr + proc.stdout)
        assert "scripts/load_env_lock.sh" in (proc.stderr + proc.stdout)


# ── load_env_lock.sh contract ────────────────────────────────────


class TestLoadEnvLock:
    """AC #1 — sourcing the lock loader populates the three digest
    env vars from the JSON lock file."""

    def test_stdout_mode_emits_three_digest_lines(self, tmp_path):
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        proc = subprocess.run(
            ["bash", str(LOAD_LOCK), str(lock)],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        assert f"OMNISIGHT_BACKEND_DIGEST={GOOD_DIGEST_A}" in proc.stdout
        assert f"OMNISIGHT_FRONTEND_DIGEST={GOOD_DIGEST_B}" in proc.stdout
        assert f"OMNISIGHT_BRIDGE_DIGEST={GOOD_DIGEST_C}" in proc.stdout
        assert "OMNISIGHT_BUNDLE_ID=test-bundle-abcdef" in proc.stdout

    def test_out_mode_writes_env_file(self, tmp_path):
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        out = tmp_path / "combined.env"
        proc = subprocess.run(
            ["bash", str(LOAD_LOCK), str(lock), "--out", str(out)],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        body = out.read_text()
        # Header is a comment so `docker compose --env-file` ignores it.
        assert body.startswith("#")
        # Three digest vars + 4 metadata vars in the body.
        lines = [l for l in body.splitlines() if l and not l.startswith("#")]
        assert any(l == f"OMNISIGHT_BACKEND_DIGEST={GOOD_DIGEST_A}" for l in lines)
        assert any(l == f"OMNISIGHT_FRONTEND_DIGEST={GOOD_DIGEST_B}" for l in lines)
        assert any(l == f"OMNISIGHT_BRIDGE_DIGEST={GOOD_DIGEST_C}" for l in lines)

    def test_source_mode_exports_vars(self, tmp_path):
        """Spawn a child bash that sources the script and prints the
        digest var — proves the source-mode `export` path lands the
        value in the caller shell."""
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        wrapper = tmp_path / "wrapper.sh"
        wrapper.write_text(
            f"set -e\n"
            f"source {LOAD_LOCK} {lock}\n"
            f"echo BACKEND=$OMNISIGHT_BACKEND_DIGEST\n"
            f"echo FRONTEND=$OMNISIGHT_FRONTEND_DIGEST\n"
            f"echo BRIDGE=$OMNISIGHT_BRIDGE_DIGEST\n"
        )
        proc = subprocess.run(
            ["bash", str(wrapper)],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        assert f"BACKEND={GOOD_DIGEST_A}" in proc.stdout
        assert f"FRONTEND={GOOD_DIGEST_B}" in proc.stdout
        assert f"BRIDGE={GOOD_DIGEST_C}" in proc.stdout

    def test_missing_lock_file_exits_2(self, tmp_path):
        proc = subprocess.run(
            ["bash", str(LOAD_LOCK), str(tmp_path / "nope.json")],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 2


# ── verify_image_bundle.py — orchestration ───────────────────────


@pytest.fixture
def fake_docker(monkeypatch):
    """Stub out the three subprocess-bound helpers so the test owns
    the responses they return. Each test sets `state` dict entries:

      remote[<repo>@<digest>] → (digest_or_None, detail)
      cache[<repo>@<digest>]  → (repodigests_list, detail)
      sig[<repo>@<digest>]    → (status, detail)
    """
    state = {"remote": {}, "cache": {}, "sig": {}}

    def fake_remote(ref):
        return state["remote"].get(ref, (None, "no stub configured"))

    def fake_cache(ref):
        return state["cache"].get(ref, ([], "No such image"))

    def fake_sig(ref):
        return state["sig"].get(ref, ("ok", "stubbed"))

    monkeypatch.setattr(vib, "_docker_manifest_digest", fake_remote)
    monkeypatch.setattr(vib, "_docker_image_cache_digests", fake_cache)
    monkeypatch.setattr(vib, "_verify_cosign_signature", fake_sig)
    return state


class TestVerifyImageBundle:
    """AC #3 + AC #4."""

    def test_all_match_exits_zero(self, tmp_path, fake_docker, capsys):
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        compose = _write_compose(
            tmp_path / "compose.yml",
            backend_repo="ghcr.io/test/omnisight-backend",
            frontend_repo="ghcr.io/test/omnisight-frontend",
        )
        for repo, digest in [
            ("ghcr.io/test/omnisight-backend", GOOD_DIGEST_A),
            ("ghcr.io/test/omnisight-frontend", GOOD_DIGEST_B),
            ("ghcr.io/test/omnisight-bridge", GOOD_DIGEST_C),
        ]:
            ref = f"{repo}@{digest}"
            fake_docker["remote"][ref] = (digest, "ok")
            fake_docker["cache"][ref] = ([ref], "ok")
            fake_docker["sig"][ref] = ("ok", "stub-cosign-ok")
        rc = vib.main(["--compose", str(compose), "--lock", str(lock)])
        assert rc == vib.EXIT_OK
        out = capsys.readouterr().out
        assert "RESULT: OK" in out

    def test_remote_mismatch_exits_one(self, tmp_path, fake_docker, capsys):
        """Lock claims digest A, registry serves digest STALE — alias
        was retagged after the lock was sealed. Must FAIL."""
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        compose = _write_compose(
            tmp_path / "compose.yml",
            backend_repo="ghcr.io/test/omnisight-backend",
            frontend_repo="ghcr.io/test/omnisight-frontend",
        )
        backend_ref = f"ghcr.io/test/omnisight-backend@{GOOD_DIGEST_A}"
        fake_docker["remote"][backend_ref] = (GOOD_DIGEST_STALE, "ok")
        fake_docker["cache"][backend_ref] = ([backend_ref], "ok")
        for repo, digest in [
            ("ghcr.io/test/omnisight-frontend", GOOD_DIGEST_B),
            ("ghcr.io/test/omnisight-bridge", GOOD_DIGEST_C),
        ]:
            ref = f"{repo}@{digest}"
            fake_docker["remote"][ref] = (digest, "ok")
            fake_docker["cache"][ref] = ([ref], "ok")
        rc = vib.main(["--compose", str(compose), "--lock", str(lock)])
        assert rc == vib.EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "RESULT: FAIL" in out
        assert "retagged" in out

    def test_cache_stale_exits_one(self, tmp_path, fake_docker, capsys):
        """AC #4 — Codex #15: operator manually `docker pull`s + `docker
        tag`s an old digest. Local cache reports a different RepoDigest
        than the lock. Must FAIL with the explicit `docker compose
        pull --policy=always` remediation."""
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        compose = _write_compose(
            tmp_path / "compose.yml",
            backend_repo="ghcr.io/test/omnisight-backend",
            frontend_repo="ghcr.io/test/omnisight-frontend",
        )
        # remote/registry agree with lock — registry is fine.
        # cosign signatures are fine.
        # But local cache has the stale digest under the same alias.
        for repo, digest in [
            ("ghcr.io/test/omnisight-backend", GOOD_DIGEST_A),
            ("ghcr.io/test/omnisight-frontend", GOOD_DIGEST_B),
            ("ghcr.io/test/omnisight-bridge", GOOD_DIGEST_C),
        ]:
            ref = f"{repo}@{digest}"
            fake_docker["remote"][ref] = (digest, "ok")
            fake_docker["sig"][ref] = ("ok", "ok")
        stale_ref = f"ghcr.io/test/omnisight-backend@{GOOD_DIGEST_STALE}"
        # Cache reports a stale RepoDigest pinned to the same alias —
        # exactly the Codex #15 footgun.
        fake_docker["cache"][f"ghcr.io/test/omnisight-backend@{GOOD_DIGEST_A}"] = (
            [stale_ref], "ok",
        )
        fake_docker["cache"][f"ghcr.io/test/omnisight-frontend@{GOOD_DIGEST_B}"] = (
            [f"ghcr.io/test/omnisight-frontend@{GOOD_DIGEST_B}"], "ok",
        )
        fake_docker["cache"][f"ghcr.io/test/omnisight-bridge@{GOOD_DIGEST_C}"] = (
            [f"ghcr.io/test/omnisight-bridge@{GOOD_DIGEST_C}"], "ok",
        )
        rc = vib.main(["--compose", str(compose), "--lock", str(lock)])
        assert rc == vib.EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "RESULT: FAIL" in out
        assert "Codex-#15" in out
        assert "docker compose pull --policy=always" in out

    def test_cosign_signature_failure_exits_one(self, tmp_path, fake_docker, capsys):
        """AC #3(b)."""
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        compose = _write_compose(
            tmp_path / "compose.yml",
            backend_repo="ghcr.io/test/omnisight-backend",
            frontend_repo="ghcr.io/test/omnisight-frontend",
        )
        for repo, digest in [
            ("ghcr.io/test/omnisight-backend", GOOD_DIGEST_A),
            ("ghcr.io/test/omnisight-frontend", GOOD_DIGEST_B),
            ("ghcr.io/test/omnisight-bridge", GOOD_DIGEST_C),
        ]:
            ref = f"{repo}@{digest}"
            fake_docker["remote"][ref] = (digest, "ok")
            fake_docker["cache"][ref] = ([ref], "ok")
            fake_docker["sig"][ref] = ("ok", "ok")
        # Replace one signature with a failure.
        fake_docker["sig"][f"ghcr.io/test/omnisight-backend@{GOOD_DIGEST_A}"] = (
            "mismatch", "cosign key-based verification failed",
        )
        rc = vib.main(["--compose", str(compose), "--lock", str(lock)])
        assert rc == vib.EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "cosign key-based verification failed" in out

    def test_compose_not_pinned_by_digest_fails(self, tmp_path, fake_docker, capsys):
        """The verifier guards against future regressions where someone
        re-introduces a `:tag` reference."""
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        compose = _write_compose(
            tmp_path / "compose.yml",
            backend_repo="ghcr.io/test/omnisight-backend",
            frontend_repo="ghcr.io/test/omnisight-frontend",
            pin_by_digest=False,
        )
        for repo, digest in [
            ("ghcr.io/test/omnisight-backend", GOOD_DIGEST_A),
            ("ghcr.io/test/omnisight-frontend", GOOD_DIGEST_B),
            ("ghcr.io/test/omnisight-bridge", GOOD_DIGEST_C),
        ]:
            ref = f"{repo}@{digest}"
            fake_docker["remote"][ref] = (digest, "ok")
            fake_docker["cache"][ref] = ([ref], "ok")
            fake_docker["sig"][ref] = ("ok", "ok")
        rc = vib.main(["--compose", str(compose), "--lock", str(lock)])
        assert rc == vib.EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "not pinned by digest" in out

    def test_placeholder_lock_skips_checks(self, tmp_path, fake_docker, capsys):
        """The shipped lock files are all-zero placeholders until the
        first CI promotion. Preflight must not error on a fresh clone,
        but must clearly mark the checks as skipped (so an operator
        sees the lock isn't real yet)."""
        lock = _write_lock(tmp_path / "lock.json",
                           backend=PLACEHOLDER_DIGEST,
                           frontend=PLACEHOLDER_DIGEST,
                           bridge=PLACEHOLDER_DIGEST)
        compose = _write_compose(
            tmp_path / "compose.yml",
            backend_repo="ghcr.io/test/omnisight-backend",
            frontend_repo="ghcr.io/test/omnisight-frontend",
        )
        rc = vib.main(["--compose", str(compose), "--lock", str(lock)])
        assert rc == vib.EXIT_OK
        out = capsys.readouterr().out
        assert "placeholder lock digest" in out

    def test_bad_invocation_returns_two(self, tmp_path, capsys):
        rc = vib.main([
            "--compose", str(tmp_path / "noexist.yml"),
            "--lock", str(tmp_path / "noexist.json"),
        ])
        assert rc == vib.EXIT_BAD_INVOCATION

    def test_json_output_shape(self, tmp_path, fake_docker, capsys):
        lock = _write_lock(tmp_path / "lock.json",
                           backend=GOOD_DIGEST_A, frontend=GOOD_DIGEST_B,
                           bridge=GOOD_DIGEST_C)
        compose = _write_compose(
            tmp_path / "compose.yml",
            backend_repo="ghcr.io/test/omnisight-backend",
            frontend_repo="ghcr.io/test/omnisight-frontend",
        )
        for repo, digest in [
            ("ghcr.io/test/omnisight-backend", GOOD_DIGEST_A),
            ("ghcr.io/test/omnisight-frontend", GOOD_DIGEST_B),
            ("ghcr.io/test/omnisight-bridge", GOOD_DIGEST_C),
        ]:
            ref = f"{repo}@{digest}"
            fake_docker["remote"][ref] = (digest, "ok")
            fake_docker["cache"][ref] = ([ref], "ok")
            fake_docker["sig"][ref] = ("ok", "ok")
        rc = vib.main([
            "--compose", str(compose), "--lock", str(lock),
            "--output", "json",
        ])
        assert rc == vib.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        names = [img["name"] for img in payload["images"]]
        assert {"backend", "frontend", "bridge"}.issubset(names)
