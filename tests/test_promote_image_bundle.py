"""OP-1590 / RT-12 -- promote-by-retag tests.

Covers the RT-12 acceptance criteria:

* the validated backend+frontend digests (RT-21 pair) are retagged to
  the reserved version ``vX.Y.Z`` in GitLab CR, by digest, never rebuilt;
* each final tag is verified to resolve to the exact validated digest;
* a fresh ``v*`` git tag is NEVER created and the registry is never asked
  to build (RT-20);
* the promote refuses without a green staging gate (RT-05e precondition);
* the retag is idempotent (re-running a partial/failed promote is safe);
* exactly one bundle audit row is written.
"""
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

DIGEST_BE = "sha256:" + "a" * 64
DIGEST_FE = "sha256:" + "b" * 64
VERSION = "v1.4.0"
REGISTRY = "sora.services:49154/omnisight/OmniSight-Productizer"


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


def _bundle(path: Path, *, include_bridge: bool = False) -> Path:
    images = {
        "backend": {"repository": f"{REGISTRY}/backend", "digest": DIGEST_BE},
        "frontend": {"repository": f"{REGISTRY}/frontend", "digest": DIGEST_FE},
    }
    if include_bridge:
        images["bridge"] = {"repository": f"{REGISTRY}/bridge", "digest": "sha256:" + "c" * 64}
    payload = {
        "bundle_id": "test-bundle",
        "git_sha": "3f1c0a4e" + "b" * 32,
        "images": images,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _green_evidence() -> dict:
    return {
        "status": "green",
        "bundle_id": "test-bundle",
        "backend_digest": DIGEST_BE,
        "frontend_digest": DIGEST_FE,
    }


class FakeRegistry:
    """Stateful fake of ``docker buildx imagetools`` (create + inspect)."""

    def __init__(self, *, preset: dict[str, str] | None = None):
        self.tags: dict[str, str] = dict(preset or {})
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        # docker buildx imagetools create -t <target> <repo@digest>
        if cmd[:4] == ["docker", "buildx", "imagetools", "create"]:
            target = cmd[cmd.index("-t") + 1]
            source = cmd[-1]
            self.tags[target] = source.split("@", 1)[1]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # docker buildx imagetools inspect <ref> --format {{.Manifest.Digest}}
        if cmd[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            ref = cmd[4]
            if ref in self.tags:
                return subprocess.CompletedProcess(cmd, 0, self.tags[ref] + "\n", "")
            return subprocess.CompletedProcess(cmd, 1, "", "not found")
        # bash verify_image_signature.sh / python sign_promotion_attestation.py
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_promote_retags_pair_to_version_and_verifies_digest(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    reg = FakeRegistry()

    outcome = promote.promote_bundle(
        bundle_path=bundle,
        version=VERSION,
        from_env="staging",
        actor="release-manager",
        approval_refs=["OP-1590"],
        audit_log=tmp_path / "audit.jsonl",
        staging_evidence_path=None,
        require_staging_gate=False,
        dry_run=False,
        runner=reg,
    )

    assert outcome.final_digest_equality is True
    assert outcome.per_image_equality == {"backend": True, "frontend": True}
    # Both members of the pair landed at vX.Y.Z resolving to the exact digest.
    assert reg.tags[f"{REGISTRY}/backend:{VERSION}"] == DIGEST_BE
    assert reg.tags[f"{REGISTRY}/frontend:{VERSION}"] == DIGEST_FE

    creates = [c for c in reg.calls if c[:4] == ["docker", "buildx", "imagetools", "create"]]
    assert len(creates) == 2
    signs = [c for c in reg.calls if str(SIGN_SCRIPT) in c]
    assert len(signs) == 2  # per-image attestation

    rows = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1  # exactly one bundle audit row
    row = json.loads(rows[0])
    assert row["event"] == "image_bundle_promoted"
    assert row["version"] == VERSION
    assert row["final_digest_equality"] is True
    assert len(row["images"]) == 2


def test_never_creates_git_tag_or_rebuilds(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    reg = FakeRegistry()

    promote.promote_bundle(
        bundle_path=bundle,
        version=VERSION,
        from_env="staging",
        actor="rm",
        approval_refs=["OP-1590"],
        audit_log=tmp_path / "audit.jsonl",
        require_staging_gate=False,
        dry_run=False,
        runner=reg,
    )

    for cmd in reg.calls:
        assert cmd[0] != "git", f"promote must never invoke git: {cmd}"
        # No image build of any kind -- retag only.
        assert cmd[:3] != ["docker", "build", "-t"], f"promote must not rebuild: {cmd}"
        assert cmd[:3] != ["docker", "buildx", "build"], f"promote must not rebuild: {cmd}"
    # The only docker subcommands used are imagetools create/inspect.
    docker_cmds = [c for c in reg.calls if c[0] == "docker"]
    assert docker_cmds, "expected at least one registry op"
    for cmd in docker_cmds:
        assert cmd[1:3] == ["buildx", "imagetools"]
        assert cmd[3] in {"create", "inspect"}


def test_idempotent_rerun_skips_already_correct_tag(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    # Pre-seed both target tags at the validated digests (a prior promote
    # already retagged them; only the audit row failed last time).
    reg = FakeRegistry(
        preset={
            f"{REGISTRY}/backend:{VERSION}": DIGEST_BE,
            f"{REGISTRY}/frontend:{VERSION}": DIGEST_FE,
        }
    )

    outcome = promote.promote_bundle(
        bundle_path=bundle,
        version=VERSION,
        from_env="staging",
        actor="rm",
        approval_refs=["OP-1590"],
        audit_log=tmp_path / "audit.jsonl",
        require_staging_gate=False,
        dry_run=False,
        runner=reg,
    )

    assert outcome.final_digest_equality is True
    creates = [c for c in reg.calls if c[:4] == ["docker", "buildx", "imagetools", "create"]]
    assert creates == []  # nothing re-created -- idempotent no-op


def test_refuses_to_overwrite_tag_pointing_at_other_digest(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    reg = FakeRegistry(preset={f"{REGISTRY}/backend:{VERSION}": "sha256:" + "9" * 64})

    with pytest.raises(promote.PromoteError, match="already resolves to"):
        promote.promote_bundle(
            bundle_path=bundle,
            version=VERSION,
            from_env="staging",
            actor="rm",
            approval_refs=["OP-1590"],
            audit_log=tmp_path / "audit.jsonl",
            require_staging_gate=False,
            dry_run=False,
            runner=reg,
        )


def test_final_tag_digest_mismatch_raises(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")

    class DriftingRegistry(FakeRegistry):
        def __call__(self, cmd, **kwargs):
            cmd = list(cmd)
            # Pretend the create silently lands a different digest.
            if cmd[:4] == ["docker", "buildx", "imagetools", "create"]:
                self.calls.append(cmd)
                self.tags[cmd[cmd.index("-t") + 1]] = "sha256:" + "0" * 64
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return super().__call__(cmd, **kwargs)

    with pytest.raises(promote.PromoteError, match="expected the validated digest"):
        promote.promote_bundle(
            bundle_path=bundle,
            version=VERSION,
            from_env="staging",
            actor="rm",
            approval_refs=["OP-1590"],
            audit_log=tmp_path / "audit.jsonl",
            require_staging_gate=False,
            dry_run=False,
            runner=DriftingRegistry(),
        )


def test_staging_gate_required_when_no_evidence(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    with pytest.raises(promote.StagingGateNotPassed, match="staging gate"):
        promote.promote_bundle(
            bundle_path=bundle,
            version=VERSION,
            from_env="staging",
            actor="rm",
            approval_refs=["OP-1590"],
            audit_log=tmp_path / "audit.jsonl",
            staging_evidence_path=None,
            require_staging_gate=True,
            dry_run=False,
            runner=FakeRegistry(),
        )


def test_green_staging_evidence_authorises_promote(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    evidence = tmp_path / "staging.jsonl"
    evidence.write_text(json.dumps(_green_evidence()) + "\n", encoding="utf-8")

    outcome = promote.promote_bundle(
        bundle_path=bundle,
        version=VERSION,
        from_env="staging",
        actor="rm",
        approval_refs=["OP-1590"],
        audit_log=tmp_path / "audit.jsonl",
        staging_evidence_path=evidence,
        require_staging_gate=True,
        dry_run=False,
        runner=FakeRegistry(),
    )
    assert outcome.final_digest_equality is True


def test_red_staging_evidence_blocks_promote(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    evidence = tmp_path / "staging.jsonl"
    red = _green_evidence()
    red["status"] = "red"
    evidence.write_text(json.dumps(red) + "\n", encoding="utf-8")

    with pytest.raises(promote.StagingGateNotPassed, match="not green"):
        promote.promote_bundle(
            bundle_path=bundle,
            version=VERSION,
            from_env="staging",
            actor="rm",
            approval_refs=["OP-1590"],
            audit_log=tmp_path / "audit.jsonl",
            staging_evidence_path=evidence,
            require_staging_gate=True,
            dry_run=False,
            runner=FakeRegistry(),
        )


def test_staging_evidence_digest_mismatch_blocks_promote(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    evidence = tmp_path / "staging.jsonl"
    skewed = _green_evidence()
    skewed["backend_digest"] = "sha256:" + "7" * 64
    evidence.write_text(json.dumps(skewed) + "\n", encoding="utf-8")

    with pytest.raises(promote.StagingGateNotPassed, match="does not match"):
        promote.promote_bundle(
            bundle_path=bundle,
            version=VERSION,
            from_env="staging",
            actor="rm",
            approval_refs=["OP-1590"],
            audit_log=tmp_path / "audit.jsonl",
            staging_evidence_path=evidence,
            require_staging_gate=True,
            dry_run=False,
            runner=FakeRegistry(),
        )


def test_rejects_bridge_image_rt21(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json", include_bridge=True)
    with pytest.raises(promote.PromoteError, match="backend\\+frontend pair only"):
        promote.promote_bundle(
            bundle_path=bundle,
            version=VERSION,
            from_env="staging",
            actor="rm",
            approval_refs=["OP-1590"],
            audit_log=tmp_path / "audit.jsonl",
            require_staging_gate=False,
            dry_run=False,
            runner=FakeRegistry(),
        )


def test_rejects_non_version_target(promote, tmp_path):
    bundle = _bundle(tmp_path / "bundle.json")
    with pytest.raises(promote.PromoteError, match="release version vX.Y.Z"):
        promote.promote_bundle(
            bundle_path=bundle,
            version="canary",
            from_env="staging",
            actor="rm",
            approval_refs=["OP-1590"],
            audit_log=tmp_path / "audit.jsonl",
            require_staging_gate=False,
            dry_run=False,
            runner=FakeRegistry(),
        )


def test_dry_run_prints_plan_without_executing(promote, tmp_path, capsys):
    bundle = _bundle(tmp_path / "bundle.json")
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):  # pragma: no cover - must not be reached
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    outcome = promote.promote_bundle(
        bundle_path=bundle,
        version=VERSION,
        from_env="staging",
        actor="rm",
        approval_refs=["OP-1590"],
        audit_log=tmp_path / "audit.jsonl",
        require_staging_gate=False,
        dry_run=True,
        runner=runner,
    )

    out = capsys.readouterr().out
    assert len(outcome.promotions) == 2
    assert f"Promoting bundle test-bundle -> {VERSION}" in out
    assert "DRY RUN: would exec docker buildx imagetools create" in out
    assert "DRY RUN: would append bundle audit row" in out
    assert calls == []
    assert not (tmp_path / "audit.jsonl").exists()


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
            VERSION,
            "--actor",
            "rm",
            "--approval-refs",
            "OP-1590",
            "--skip-staging-gate",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"Promoting bundle test-bundle -> {VERSION}" in proc.stdout
    assert "DRY RUN: would exec docker buildx imagetools create" in proc.stdout
    assert "DRY RUN: would append bundle audit row" in proc.stdout


def test_cli_rejects_v_star_git_tag_never_in_command(tmp_path):
    """The CLI plan must mention the image retag, never a git tag op."""
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
            VERSION,
            "--actor",
            "rm",
            "--approval-refs",
            "OP-1590",
            "--skip-staging-gate",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "git tag" not in proc.stdout
    # No rebuild: the only docker op is the imagetools retag, never a build.
    assert "docker build -t" not in proc.stdout
    assert "docker buildx build" not in proc.stdout
    assert "imagetools create" in proc.stdout


def test_local_registry_retag_verification(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("docker not installed")
    if os.environ.get("RUN_RT12_LOCAL_REGISTRY") != "1":
        pytest.skip("set RUN_RT12_LOCAL_REGISTRY=1 to exercise registry:2 retag path")

    port = os.environ.get("RT12_REGISTRY_PORT", "5001")
    name = f"rt12-registry-{uuid.uuid4().hex[:8]}"
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
        (image_dir / "payload.txt").write_text("op-1590\n", encoding="utf-8")
        (image_dir / "Dockerfile").write_text(
            "FROM scratch\nCOPY payload.txt /payload.txt\n",
            encoding="utf-8",
        )
        digests: dict[str, str] = {}
        for image in ("backend", "frontend"):
            repo = f"{registry}/{image}"
            staging_ref = f"{repo}:staging"
            subprocess.run(
                ["docker", "build", "-t", staging_ref, str(image_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["docker", "push", staging_ref], check=True, capture_output=True, text=True)
            digests[image] = subprocess.check_output(
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
        for tool in ("cosign",):
            p = fake_bin / tool
            p.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
            p.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        bundle = tmp_path / "bundle.json"
        bundle.write_text(
            json.dumps(
                {
                    "bundle_id": "rt12-bundle",
                    "images": {
                        "backend": {"repository": f"{registry}/backend", "digest": digests["backend"]},
                        "frontend": {"repository": f"{registry}/frontend", "digest": digests["frontend"]},
                    },
                }
            ),
            encoding="utf-8",
        )

        proc = subprocess.run(
            [
                sys.executable,
                str(PROMOTE_SCRIPT),
                "--bundle",
                str(bundle),
                "--from",
                "staging",
                "--to",
                VERSION,
                "--actor",
                "rm",
                "--approval-refs",
                "OP-1590",
                "--audit-log",
                str(tmp_path / "audit.jsonl"),
                "--predicate-out-dir",
                str(tmp_path / "predicates"),
                "--skip-staging-gate",
                "--no-dry-run",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        for image in ("backend", "frontend"):
            promoted = subprocess.check_output(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    f"{registry}/{image}:{VERSION}",
                    "--format",
                    "{{.Manifest.Digest}}",
                ],
                text=True,
            ).strip()
            assert promoted == digests[image]
        assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)
