"""OP-1843 (P2.2) — pinned product-source build orchestration."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.agents import product_build as pb
from backend.agents import product_source as ps
from backend.config import settings


@pytest.fixture(autouse=True)
def _reset_product_sources(monkeypatch):
    monkeypatch.setattr(settings, "product_sources", "", raising=False)


def _configure(
    monkeypatch,
    *,
    tier: str = "consumer",
    pinned_ref: str = "c360a9d0",
    build_system: str = "gradle",
    artifact_glob: str | None = None,
) -> None:
    entry = {
        "repo_url": "https://github.com/operator/camviewpro-android.git",
        "tier": tier,
        "branch": "main",
        "pinned_ref": pinned_ref,
        "git_account_ref": "camviewpro-ro",
    }
    if build_system != "gradle":
        entry["build_system"] = build_system
    if artifact_glob is not None:
        entry["artifact_glob"] = artifact_glob
    monkeypatch.setattr(
        settings,
        "product_sources",
        json.dumps({"DEMO": entry}),
        raising=False,
    )


async def _fake_credential(source, *, tenant_id=None):
    _fake_credential.seen = (source.git_account_ref, tenant_id)
    return {"id": "camviewpro-ro", "username": "ci", "token": "ghp_secret_token"}


_fake_credential.seen = None


class RecordingRun:
    def __init__(
        self,
        *,
        head: str = "c360a9d0",
        gradle_rc: int = 0,
        gradle_stderr: str = "",
    ):
        self.calls: list[tuple[list[str], dict]] = []
        self.head = head
        self.gradle_rc = gradle_rc
        self.gradle_stderr = gradle_stderr

    def __call__(self, cmd, *args, **kwargs):
        argv = list(cmd)
        self.calls.append((argv, kwargs))
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{self.head}\n", stderr="")
        if argv and argv[0] == "gradle":
            return SimpleNamespace(
                returncode=self.gradle_rc,
                stdout="",
                stderr=self.gradle_stderr,
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls]


def _apk_path(tmp_path, tier: str):
    return (
        tmp_path
        / "camviewpro-android"
        / "apps"
        / tier
        / "build"
        / "outputs"
        / "apk"
        / "debug"
        / f"{tier}-debug.apk"
    )


def _build(
    tmp_path,
    monkeypatch,
    *,
    tier: str = "consumer",
    recorder: RecordingRun | None = None,
):
    _configure(monkeypatch, tier=tier)
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    run = recorder or RecordingRun()
    monkeypatch.setattr(pb.subprocess, "run", run)
    apk = _apk_path(tmp_path, tier)
    apk.parent.mkdir(parents=True)
    apk.write_text("apk", encoding="utf-8")
    result = asyncio.run(
        pb.build_product_source("DEMO", workspace_root=tmp_path, tenant_id="t-demo")
    )
    return result, run


def test_clone_and_checkout_use_pinned_ref(tmp_path, monkeypatch):
    result, run = _build(tmp_path, monkeypatch)
    commands = run.commands()
    assert commands[0][0:2] == ["git", "clone"]
    assert "ghp_secret_token" in commands[0][2]
    assert ["git", "checkout", "--detach", "c360a9d0"] in commands
    assert result.pinned_ref == "c360a9d0"
    assert _fake_credential.seen == ("camviewpro-ro", "t-demo")


def test_head_mismatch_raises_reproducibility_guard(tmp_path, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    monkeypatch.setattr(pb.subprocess, "run", RecordingRun(head="not-the-pin"))
    with pytest.raises(ps.ProductSourceError, match="does not match pinned_ref"):
        asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))


def test_token_is_scrubbed_from_recorded_remote_url(tmp_path, monkeypatch):
    _, run = _build(tmp_path, monkeypatch)
    scrub = next(
        cmd
        for cmd in run.commands()
        if cmd[:4] == ["git", "remote", "set-url", "origin"]
    )
    assert scrub[4] == "https://github.com/operator/camviewpro-android.git"
    assert "ghp_secret_token" not in scrub[4]


@pytest.mark.parametrize(
    ("tier", "module"),
    [
        ("consumer", ":apps:consumer"),
        ("medical", ":apps:medical"),
        ("automotive", ":apps:automotive"),
    ],
)
def test_tier_to_module_map_for_supported_tiers(tmp_path, monkeypatch, tier, module):
    result, run = _build(tmp_path, monkeypatch, tier=tier)
    assert result.module == module
    assert [
        "gradle",
        f"{module}:assembleDebug",
        "--offline",
        "--console=plain",
    ] in run.commands()


def test_gradle_command_sequence_and_apk_path_are_unchanged(tmp_path, monkeypatch):
    result, run = _build(tmp_path, monkeypatch)
    assert run.commands() == [
        [
            "git",
            "clone",
            "https://ci:ghp_secret_token@github.com/operator/camviewpro-android.git",
            str(tmp_path / "camviewpro-android"),
        ],
        [
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/operator/camviewpro-android.git",
        ],
        ["git", "checkout", "--detach", "c360a9d0"],
        ["git", "rev-parse", "HEAD"],
        ["gradle", ":apps:consumer:assembleDebug", "--offline", "--console=plain"],
    ]
    assert result.apk_path == _apk_path(tmp_path, "consumer")


def test_unknown_tier_raises_defence_in_depth(tmp_path, monkeypatch):
    source = ps.ProductSource(
        repo_url="https://github.com/operator/camviewpro-android.git",
        tier="enterprise",
        branch="main",
        pinned_ref="c360a9d0",
        git_account_ref="camviewpro-ro",
    )
    monkeypatch.setattr(pb, "resolve_product_source", lambda key: source)
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    with pytest.raises(ps.ProductSourceError, match="cannot be built"):
        asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))


def test_unconfigured_project_raises(tmp_path, monkeypatch):
    with pytest.raises(ps.ProductSourceError, match="project not configured"):
        asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))


def test_missing_credential_raises(tmp_path, monkeypatch):
    _configure(monkeypatch)

    async def missing(source, *, tenant_id=None):
        raise ps.ProductSourceError("git_accounts row missing")

    monkeypatch.setattr(pb, "resolve_product_source_credential", missing)
    with pytest.raises(ps.ProductSourceError, match="git_accounts row missing"):
        asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))


def test_gradle_non_zero_raises_with_stderr_tail(tmp_path, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    run = RecordingRun(
        gradle_rc=1,
        gradle_stderr="\n".join(f"line {i}" for i in range(45)),
    )
    monkeypatch.setattr(pb.subprocess, "run", run)
    apk = _apk_path(tmp_path, "consumer")
    apk.parent.mkdir(parents=True)
    apk.write_text("apk", encoding="utf-8")
    with pytest.raises(ps.ProductSourceError, match="line 44"):
        asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))


def test_cmake_source_invokes_configure_build_and_returns_artifact(
    tmp_path,
    monkeypatch,
):
    _configure(monkeypatch, build_system="cmake", artifact_glob="build/UVCCamera")
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    monkeypatch.setattr(pb.shutil, "which", lambda name: "/usr/bin/cmake")
    run = RecordingRun()
    monkeypatch.setattr(pb.subprocess, "run", run)
    artifact = tmp_path / "camviewpro-android" / "build" / "UVCCamera"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("binary", encoding="utf-8")

    result = asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))

    assert [
        "/usr/bin/cmake",
        "-S",
        ".",
        "-B",
        "build",
        "-DCMAKE_PREFIX_PATH=",
        "-DCMAKE_BUILD_TYPE=Release",
    ] in run.commands()
    build_cmd = next(
        cmd
        for cmd in run.commands()
        if cmd[:3] == ["/usr/bin/cmake", "--build", "build"]
    )
    assert build_cmd[3].startswith("-j")
    assert result.apk_path == artifact
    assert result.module == "cmake"


def test_cmake_artifact_glob_no_match_raises_with_build_dir_contents(
    tmp_path,
    monkeypatch,
):
    _configure(monkeypatch, build_system="cmake", artifact_glob="build/UVCCamera")
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    monkeypatch.setattr(pb.shutil, "which", lambda name: "/usr/bin/cmake")
    monkeypatch.setattr(pb.subprocess, "run", RecordingRun())
    marker = tmp_path / "camviewpro-android" / "build" / "CMakeCache.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("cache", encoding="utf-8")

    with pytest.raises(
        ps.ProductSourceError,
        match=r"build/UVCCamera[\s\S]*CMakeCache.txt",
    ):
        asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))


def test_missing_cmake_binary_raises(tmp_path, monkeypatch):
    _configure(monkeypatch, build_system="cmake", artifact_glob="build/UVCCamera")
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    monkeypatch.setattr(pb.shutil, "which", lambda name: None)
    monkeypatch.setattr(pb.subprocess, "run", RecordingRun())

    with pytest.raises(ps.ProductSourceError, match="cmake"):
        asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))


def test_cmake_prefix_path_flows_to_configure_command_and_env(
    tmp_path,
    monkeypatch,
):
    _configure(monkeypatch, build_system="cmake", artifact_glob="build/UVCCamera")
    monkeypatch.setattr(pb, "resolve_product_source_credential", _fake_credential)
    monkeypatch.setattr(pb.shutil, "which", lambda name: "/usr/bin/cmake")
    monkeypatch.setenv("CMAKE_PREFIX_PATH", "/opt/Qt/6.8.3/gcc_64")
    run = RecordingRun()
    monkeypatch.setattr(pb.subprocess, "run", run)
    artifact = tmp_path / "camviewpro-android" / "build" / "UVCCamera"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("binary", encoding="utf-8")

    asyncio.run(pb.build_product_source("DEMO", workspace_root=tmp_path))

    configure = next(
        call
        for call in run.calls
        if call[0][:5] == ["/usr/bin/cmake", "-S", ".", "-B", "build"]
    )
    assert "-DCMAKE_PREFIX_PATH=/opt/Qt/6.8.3/gcc_64" in configure[0]
    assert configure[1]["env"]["CMAKE_PREFIX_PATH"] == "/opt/Qt/6.8.3/gcc_64"
