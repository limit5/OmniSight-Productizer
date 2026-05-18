"""OP-1481 — promotion CLI digest retag and audit tests."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMOTE_SCRIPT = REPO_ROOT / "scripts" / "promote_image_bundle.py"
SIGN_SCRIPT = REPO_ROOT / "scripts" / "sign_promotion_attestation.py"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def promote():
    return _load_module("promote_image_bundle", PROMOTE_SCRIPT)


@pytest.fixture(scope="module")
def sign():
    return _load_module("sign_promotion_attestation", SIGN_SCRIPT)


def _bundle(path: Path, repository_prefix: str = "ghcr.io/example") -> Path:
    payload = {
        "bundle_id": "test-bundle",
        "git_ref": "refs/tags/v0.5.0-rc3",
        "git_sha": "3f1c0a4e" + "b" * 32,
        "images": {
            "backend": {
                "repository": f"{repository_prefix}/omnisight-backend",
                "digest": DIGEST_A,
            },
            "bridge": {
                "repository": f"{repository_prefix}/omnisight-bridge",
                "digest": DIGEST_B,
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_dry_run_prints_plan_without_executing(promote, tmp_path, capsys):
    bundle = _bundle(tmp_path / "bundle.json")
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):  # pragma: no cover - must not be reached
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    promotions = promote.promote_bundle(
        bundle_path=bundle,
        from_env="staging",
        to_env="canary",
        actor="sora",
        approval_refs=["OP-1234", "OP-5678"],
        audit_log=tmp_path / "audit.jsonl",
        lock_path=tmp_path / "canary.env.lock.json",
        dry_run=True,
        runner=runner,
    )

    out = capsys.readouterr().out
    assert len(promotions) == 2
    assert "Promoting bundle test-bundle: staging -> canary" in out
    assert "docker buildx imagetools create -t ghcr.io/example/omnisight-backend:canary" in out
    assert "cosign verify ghcr.io/example/omnisight-backend@" in out
    assert out.count("sign_promotion_attestation.py") == 1
    assert calls == []
    assert not (tmp_path / "audit.jsonl").exists()
    assert not (tmp_path / "canary.env.lock.json").exists()


def test_no_dry_run_retags_verifies_attests_audits_and_locks(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    promote.promote_bundle(
        bundle_path=bundle,
        from_env="staging",
        to_env="canary",
        actor="sora",
        approval_refs=["OP-1234", "OP-5678"],
        audit_log=tmp_path / "audit.jsonl",
        lock_path=tmp_path / "canary.env.lock.json",
        dry_run=False,
        runner=runner,
    )

    assert calls[0] == [
        "docker",
        "buildx",
        "imagetools",
        "create",
        "-t",
        "ghcr.io/example/omnisight-backend:canary",
        f"ghcr.io/example/omnisight-backend@{DIGEST_A}",
    ]
    assert calls[1] == ["cosign", "verify", f"ghcr.io/example/omnisight-backend@{DIGEST_A}"]
    assert calls[2] == [
        "docker",
        "buildx",
        "imagetools",
        "create",
        "-t",
        "ghcr.io/example/omnisight-bridge:canary",
        f"ghcr.io/example/omnisight-bridge@{DIGEST_B}",
    ]
    assert calls[3] == ["cosign", "verify", f"ghcr.io/example/omnisight-bridge@{DIGEST_B}"]
    assert calls[4][:2] == [sys.executable, str(SIGN_SCRIPT)]
    assert "--approval-refs" in calls[4]

    rows = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["event"] == "image_bundle_promoted"
    assert row["bundle_id"] == "test-bundle"
    assert row["approval_refs"] == ["OP-1234", "OP-5678"]
    assert len(row["images"]) == 2

    lock = json.loads((tmp_path / "canary.env.lock.json").read_text(encoding="utf-8"))
    assert lock["environment"] == "canary"
    assert lock["bundle_id"] == "test-bundle"
    assert lock["images"]["backend"]["digest"] == DIGEST_A
    assert lock["images"]["backend"]["ref"] == "ghcr.io/example/omnisight-backend:canary"


def test_sign_promotion_attestation_writes_predicate_and_cosign_command(sign, tmp_path):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    predicate = sign.build_predicate(
        bundle_id="test-bundle",
        from_env="staging",
        to_env="canary",
        actor="sora",
        approval_refs=["OP-1234", "OP-5678"],
        parent_attestation="sha256:parent",
        image_ref=f"ghcr.io/example/omnisight-backend@{DIGEST_A}",
        time="2026-05-18T00:00:00Z",
    )
    path = sign.write_predicate(predicate, tmp_path)
    sign.attest(
        image_ref=predicate["image_ref"],
        predicate_path=path,
        dry_run=False,
        runner=runner,
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["bundle_id"] == "test-bundle"
    assert saved["from_env"] == "staging"
    assert saved["to_env"] == "canary"
    assert saved["parent_attestation"] == "sha256:parent"
    assert calls == [
        [
            "cosign",
            "attest",
            "--predicate",
            str(path),
            "--type",
            "omnisight.image.promotion.v1",
            "--yes",
            f"ghcr.io/example/omnisight-backend@{DIGEST_A}",
        ],
    ]


def test_cli_accepts_required_dry_run_invocation(tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    proc = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_SCRIPT),
            "--bundle",
            str(bundle),
            "--from",
            "staging",
            "--to",
            "canary",
            "--actor",
            "sora",
            "--approval-refs",
            "OP-1234,OP-5678",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Promoting bundle test-bundle: staging -> canary" in proc.stdout
    assert "DRY RUN: would exec docker buildx imagetools create" in proc.stdout
    assert "DRY RUN: would append audit row" in proc.stdout


def test_local_registry_retag_verification(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("docker not installed")
    if os.environ.get("RUN_OP1481_LOCAL_REGISTRY") != "1":
        pytest.skip("set RUN_OP1481_LOCAL_REGISTRY=1 to exercise registry:2 retag path")

    port = os.environ.get("OP1481_REGISTRY_PORT", "5001")
    name = f"op1481-registry-{uuid.uuid4().hex[:8]}"
    registry = f"localhost:{port}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", f"{port}:5000", "--name", name, "registry:2"],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        (image_dir / "payload.txt").write_text("op-1481\n", encoding="utf-8")
        (image_dir / "Dockerfile").write_text(
            "FROM scratch\nCOPY payload.txt /payload.txt\n",
            encoding="utf-8",
        )
        repo = f"{registry}/omnisight-backend"
        staging_ref = f"{repo}:staging"
        canary_ref = f"{repo}:canary"
        subprocess.run(
            ["docker", "build", "-t", staging_ref, str(image_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["docker", "push", staging_ref], check=True, capture_output=True, text=True)
        digest = subprocess.check_output(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                staging_ref,
                "--format",
                "{{.Manifest.Digest}}",
            ],
            text=True,
        ).strip()

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_cosign = fake_bin / "cosign"
        fake_cosign.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        fake_cosign.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        bundle = _bundle(tmp_path / "bundle.json", repository_prefix=registry)
        payload = json.loads(bundle.read_text(encoding="utf-8"))
        payload["images"] = {"backend": {"repository": repo, "digest": digest}}
        bundle.write_text(json.dumps(payload), encoding="utf-8")

        proc = subprocess.run(
            [
                sys.executable,
                str(PROMOTE_SCRIPT),
                "--bundle",
                str(bundle),
                "--from",
                "staging",
                "--to",
                "canary",
                "--actor",
                "sora",
                "--approval-refs",
                "OP-1481",
                "--audit-log",
                str(tmp_path / "audit.jsonl"),
                "--lock-file",
                str(tmp_path / "canary.env.lock.json"),
                "--predicate-out-dir",
                str(tmp_path / "predicates"),
                "--no-dry-run",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        promoted_digest = subprocess.check_output(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                canary_ref,
                "--format",
                "{{.Manifest.Digest}}",
            ],
            text=True,
        ).strip()
        assert promoted_digest == digest
        assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)
