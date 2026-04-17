"""X3 #299 — Build & package adapter contract tests.

Pins the adapter behavior end-to-end without ever shelling out to a
real ``docker`` / ``helm`` / ``rpmbuild`` etc. — every external runner
call is monkey-patched. The goal is to keep the suite offline, fast,
and equally correct on a CI-first-run image that has no build tools.

Companion to ``test_software_role_skills.py`` (X2 #298) and
``test_software_simulator.py`` (X1 #297).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

from backend import build_adapters as ba


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def tmp_source(tmp_path: Path) -> Path:
    """Empty source dir with no manifest — adapters that require one fail
    `_validate_source` here."""
    src = tmp_path / "src"
    src.mkdir()
    return src


@pytest.fixture
def docker_source(tmp_path: Path) -> Path:
    src = tmp_path / "docker_src"
    src.mkdir()
    (src / "Dockerfile").write_text("FROM scratch\n")
    return src


@pytest.fixture
def helm_source(tmp_path: Path) -> Path:
    src = tmp_path / "helm_src"
    src.mkdir()
    (src / "Chart.yaml").write_text(
        "apiVersion: v2\nname: foo\nversion: 0.1.0\nappVersion: \"1.0.0\"\n"
    )
    return src


@pytest.fixture
def deb_source(tmp_path: Path) -> Path:
    src = tmp_path / "deb_src"
    (src / "usr" / "bin").mkdir(parents=True)
    (src / "usr" / "bin" / "foo").write_text("#!/bin/sh\nexit 0\n")
    return src


@pytest.fixture
def rust_source(tmp_path: Path) -> Path:
    src = tmp_path / "rust_src"
    src.mkdir()
    (src / "Cargo.toml").write_text("[package]\nname=\"foo\"\nversion=\"0.1.0\"\n")
    return src


@pytest.fixture
def go_source(tmp_path: Path) -> Path:
    src = tmp_path / "go_src"
    src.mkdir()
    (src / "go.mod").write_text("module foo\n\ngo 1.21\n")
    return src


@pytest.fixture
def python_source(tmp_path: Path) -> Path:
    src = tmp_path / "py_src"
    src.mkdir()
    (src / "main.py").write_text("print('hi')\n")
    return src


@pytest.fixture
def electron_source(tmp_path: Path) -> Path:
    src = tmp_path / "el_src"
    src.mkdir()
    (src / "package.json").write_text('{"name":"foo","version":"0.1.0"}\n')
    return src


@pytest.fixture
def fake_run_success():
    """Patch ``ba._run`` to a successful subprocess result."""
    with mock.patch.object(ba, "_run", return_value=(0, "ok\n", "", 0.01)) as m:
        yield m


@pytest.fixture
def fake_run_fail():
    with mock.patch.object(ba, "_run", return_value=(2, "", "boom\n", 0.01)) as m:
        yield m


@pytest.fixture
def fake_which_present():
    """Make every tool look installed."""
    with mock.patch.object(ba.shutil, "which", side_effect=lambda x: f"/usr/bin/{x}") as m:
        yield m


@pytest.fixture
def fake_which_absent():
    with mock.patch.object(ba.shutil, "which", return_value=None) as m:
        yield m


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants & registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRegistry:

    def test_list_targets_returns_all_twelve(self):
        targets = ba.list_targets()
        assert len(targets) == 12, f"expected 12 targets, got {len(targets)}"
        for t in ba.NATIVE_TARGETS + ba.SKILL_HOOK_TARGETS:
            assert t in targets, f"target {t!r} missing from registry"

    def test_native_targets_count(self):
        assert len(ba.NATIVE_TARGETS) == 8

    def test_skill_hook_targets_count(self):
        assert len(ba.SKILL_HOOK_TARGETS) == 4

    def test_native_and_skill_hook_disjoint(self):
        assert not set(ba.NATIVE_TARGETS) & set(ba.SKILL_HOOK_TARGETS)

    def test_get_adapter_known(self):
        cls = ba.get_adapter("docker")
        assert cls is ba.DockerImageAdapter

    def test_get_adapter_unknown_raises(self):
        with pytest.raises(ba.UnknownTargetError):
            ba.get_adapter("nonexistent-format")

    def test_every_target_has_host_requirement(self):
        for t in ba.list_targets():
            assert t in ba.TARGET_HOST_REQUIREMENTS, f"{t} missing host_os"
            assert ba.TARGET_HOST_REQUIREMENTS[t], f"{t} host_os list empty"

    def test_every_target_has_tool_binaries(self):
        for t in ba.list_targets():
            assert t in ba.TOOL_BINARIES
            assert ba.TOOL_BINARIES[t]

    def test_every_target_has_output_pattern(self):
        for t in ba.list_targets():
            assert t in ba.OUTPUT_PATTERNS

    def test_docker_registries_cover_major_clouds(self):
        for r in ("ghcr", "dockerhub", "ecr", "gcr", "acr", "private"):
            assert r in ba.DOCKER_REGISTRIES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestVersionNormalization:

    @pytest.mark.parametrize("v", ["1.0.0", "1.2.3-rc.1", "0.4.0+build.42", "2.5"])
    def test_semver_shaped_accepted_for_generic(self, v):
        assert ba.normalize_version(v, target="helm") == v

    def test_docker_lowercases(self):
        assert ba.normalize_version("V1.0.0", target="docker") == "v1.0.0"

    def test_docker_replaces_plus(self):
        assert ba.normalize_version("1.0.0+sha", target="docker") == "1.0.0-sha"

    def test_docker_rejects_weird_prefix(self):
        with pytest.raises(ba.InvalidVersionError):
            ba.normalize_version(".invalid", target="docker")

    def test_rpm_replaces_dash(self):
        # rpm version cannot contain '-' — that's the release separator.
        assert ba.normalize_version("1.0.0-rc.1", target="rpm") == "1.0.0_rc.1"

    def test_empty_version_rejected(self):
        with pytest.raises(ba.InvalidVersionError):
            ba.normalize_version("", target="docker")

    def test_non_semver_rejected_for_helm(self):
        with pytest.raises(ba.InvalidVersionError):
            ba.normalize_version("v-totally bogus", target="helm")


class TestArtifactNameValidation:

    def test_helm_kebab_case(self):
        assert ba.validate_artifact_name("foo-bar-baz", target="helm") == "foo-bar-baz"

    def test_helm_rejects_uppercase(self):
        with pytest.raises(ba.BuildAdapterError):
            ba.validate_artifact_name("FooBar", target="helm")

    def test_helm_rejects_starts_with_digit(self):
        with pytest.raises(ba.BuildAdapterError):
            ba.validate_artifact_name("9foo", target="helm")

    def test_deb_pkg_name(self):
        assert ba.validate_artifact_name("my-pkg2", target="deb") == "my-pkg2"

    def test_deb_rejects_uppercase(self):
        with pytest.raises(ba.BuildAdapterError):
            ba.validate_artifact_name("My-Pkg", target="deb")

    def test_docker_image_name(self):
        assert ba.validate_artifact_name("org/sub/img", target="docker") == "org/sub/img"

    def test_empty_name_rejected(self):
        with pytest.raises(ba.BuildAdapterError):
            ba.validate_artifact_name("", target="docker")


class TestHostKindMapping:

    def test_returns_known_string(self):
        assert ba.current_host_kind() in ("linux", "darwin", "windows")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BuildResult / BuildSource
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBuildResult:

    def test_status_skip_when_unavailable(self):
        r = ba.BuildResult(target="docker", name="x", version="1.0.0",
                           available=False, ok=False)
        assert r.status() == "skip"

    def test_status_pass_when_available_and_ok(self):
        r = ba.BuildResult(target="docker", name="x", version="1.0.0",
                           available=True, ok=True)
        assert r.status() == "pass"

    def test_status_fail_when_available_not_ok(self):
        r = ba.BuildResult(target="docker", name="x", version="1.0.0",
                           available=True, ok=False)
        assert r.status() == "fail"

    def test_to_dict_round_trip(self):
        r = ba.BuildResult(target="docker", name="x", version="1.0.0")
        d = r.to_dict()
        assert d["target"] == "docker"
        assert d["name"] == "x"


class TestBuildSource:

    def test_validate_missing_path(self, tmp_path):
        src = ba.BuildSource(path=tmp_path / "nope")
        with pytest.raises(ba.ArtifactSourceError):
            src.validate()

    def test_validate_path_is_file(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        src = ba.BuildSource(path=f)
        with pytest.raises(ba.ArtifactSourceError):
            src.validate()

    def test_validate_ok(self, tmp_source):
        ba.BuildSource(path=tmp_source).validate()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DockerImageAdapter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDockerAdapter:

    def test_requires_dockerfile(self, tmp_source):
        a = ba.DockerImageAdapter(name="foo", version="1.0.0")
        with pytest.raises(ba.ArtifactSourceError):
            a.build(ba.BuildSource(path=tmp_source))

    def test_skip_when_no_runner(self, docker_source, fake_which_absent):
        a = ba.DockerImageAdapter(name="foo", version="1.0.0")
        r = a.build(ba.BuildSource(path=docker_source))
        assert r.status() == "skip"
        assert not r.available

    def test_build_passes_with_present_runner(self, docker_source, fake_which_present, fake_run_success):
        a = ba.DockerImageAdapter(name="foo", version="1.0.0")
        r = a.build(ba.BuildSource(path=docker_source))
        assert r.status() == "pass"
        assert r.runner == "/usr/bin/docker"
        assert r.artifact_uri == "foo:1.0.0"

    def test_build_fails_when_runner_errors(self, docker_source, fake_which_present, fake_run_fail):
        a = ba.DockerImageAdapter(name="foo", version="1.0.0")
        r = a.build(ba.BuildSource(path=docker_source))
        assert r.status() == "fail"

    def test_resolve_uri_ghcr(self):
        a = ba.DockerImageAdapter(
            name="omnisight", version="1.0.0",
            registry="ghcr", registry_args={"namespace": "anthropic"},
        )
        assert a.resolve_image_uri() == "ghcr.io/anthropic/omnisight:1.0.0"

    def test_resolve_uri_dockerhub(self):
        a = ba.DockerImageAdapter(
            name="omnisight", version="1.0.0",
            registry="dockerhub", registry_args={"namespace": "anthropic"},
        )
        assert a.resolve_image_uri() == "docker.io/anthropic/omnisight:1.0.0"

    def test_resolve_uri_ecr(self):
        a = ba.DockerImageAdapter(
            name="omni", version="1.0.0",
            registry="ecr",
            registry_args={"account": "123456789012", "region": "us-west-2"},
        )
        assert a.resolve_image_uri() == "123456789012.dkr.ecr.us-west-2.amazonaws.com/omni:1.0.0"

    def test_ecr_requires_account(self):
        a = ba.DockerImageAdapter(name="x", version="1.0.0", registry="ecr")
        with pytest.raises(ba.BuildAdapterError):
            a.resolve_image_uri()

    def test_acr_requires_registry_name(self):
        a = ba.DockerImageAdapter(name="x", version="1.0.0", registry="acr")
        with pytest.raises(ba.BuildAdapterError):
            a.resolve_image_uri()

    def test_private_requires_host(self):
        a = ba.DockerImageAdapter(name="x", version="1.0.0", registry="private")
        with pytest.raises(ba.BuildAdapterError):
            a.resolve_image_uri()

    def test_no_registry_returns_local_tag(self):
        a = ba.DockerImageAdapter(name="x", version="1.0.0")
        assert a.resolve_image_uri() == "x:1.0.0"

    def test_push_failure_marked(self, docker_source, fake_which_present):
        # First call (build) ok; second (inspect) ok; third (push) fail.
        with mock.patch.object(ba, "_run", side_effect=[
            (0, "built", "", 0.1),
            (0, "sha256:abc", "", 0.01),
            (2, "", "auth required", 0.1),
        ]):
            a = ba.DockerImageAdapter(name="foo", version="1.0.0", push=True, registry="ghcr")
            r = a.build(ba.BuildSource(path=docker_source))
            assert r.status() == "fail"
            assert any("push failed" in n for n in r.notes)

    def test_invalid_version_uppercase_normalized(self):
        a = ba.DockerImageAdapter(name="x", version="V1.0.0")
        # Docker tag rule lowercases — no error.
        assert a._version == "v1.0.0"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HelmChartAdapter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHelmAdapter:

    def test_requires_chart_yaml(self, tmp_source):
        a = ba.HelmChartAdapter(name="foo", version="0.1.0")
        with pytest.raises(ba.ArtifactSourceError):
            a.build(ba.BuildSource(path=tmp_source))

    def test_skip_when_no_helm(self, helm_source, fake_which_absent):
        a = ba.HelmChartAdapter(name="foo", version="0.1.0")
        r = a.build(ba.BuildSource(path=helm_source))
        assert r.status() == "skip"

    def test_runs_lint_then_package(self, helm_source, fake_which_present, tmp_path):
        # Pretend lint ok, package ok, and write the expected file so the
        # adapter sees a real artifact path.
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        artifact = out_dir / "foo-0.1.0.tgz"
        artifact.write_bytes(b"x")
        with mock.patch.object(ba, "_run", return_value=(0, "ok", "", 0.01)) as m:
            a = ba.HelmChartAdapter(name="foo", version="0.1.0", output_dir=out_dir)
            r = a.build(ba.BuildSource(path=helm_source))
            # Two runs: lint + package.
            assert m.call_count == 2
            assert r.status() == "pass"
            assert r.artifact_path == str(artifact)

    def test_lint_warning_propagated_to_notes(self, helm_source, fake_which_present, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "foo-0.1.0.tgz").write_bytes(b"x")
        with mock.patch.object(ba, "_run", side_effect=[
            (1, "", "WARN: bad value", 0.01),  # lint
            (0, "", "", 0.01),                  # package
        ]):
            a = ba.HelmChartAdapter(name="foo", version="0.1.0", output_dir=out_dir)
            r = a.build(ba.BuildSource(path=helm_source))
            assert any("lint warnings" in n for n in r.notes)
            assert r.status() == "pass"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Linux package adapters (deb / rpm)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDebAdapter:

    def test_requires_marker(self, tmp_source):
        a = ba.DebPackageAdapter(name="foo", version="1.0.0")
        # Patch host_compatible to allow on darwin/windows during test.
        with mock.patch.object(a, "host_compatible", return_value=True):
            with pytest.raises(ba.ArtifactSourceError):
                a.build(ba.BuildSource(path=tmp_source))

    def test_skip_when_no_runner(self, deb_source, fake_which_absent):
        a = ba.DebPackageAdapter(name="foo", version="1.0.0")
        with mock.patch.object(a, "host_compatible", return_value=True):
            r = a.build(ba.BuildSource(path=deb_source))
            assert r.status() == "skip"

    def test_uses_fpm_when_available(self, deb_source, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "foo_1.0.0_noarch.deb").write_bytes(b"x")
        with mock.patch.object(ba.shutil, "which", side_effect=lambda x: "/usr/bin/fpm" if x == "fpm" else None):
            with mock.patch.object(ba, "_run", return_value=(0, "ok", "", 0.01)) as m:
                a = ba.DebPackageAdapter(name="foo", version="1.0.0", output_dir=out_dir)
                with mock.patch.object(a, "host_compatible", return_value=True):
                    r = a.build(ba.BuildSource(path=deb_source))
                    assert r.status() == "pass"
                    # fpm command must include `-t deb`.
                    cmd = m.call_args.args[0]
                    assert "-t" in cmd and "deb" in cmd


class TestRpmAdapter:

    def test_skip_without_runner(self, deb_source, fake_which_absent):
        a = ba.RpmPackageAdapter(name="foo", version="1.0.0")
        with mock.patch.object(a, "host_compatible", return_value=True):
            r = a.build(ba.BuildSource(path=deb_source))
            assert r.status() == "skip"

    def test_arch_in_output_pattern(self):
        a = ba.RpmPackageAdapter(name="foo", version="1.0.0", arch="x86_64")
        assert a.expected_artifact_path() == "foo-1.0.0.x86_64.rpm"

    def test_dash_normalized_in_version(self):
        a = ba.RpmPackageAdapter(name="foo", version="1.0.0-rc.1")
        assert a._version == "1.0.0_rc.1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Windows installers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMsiAdapter:

    def test_requires_wxs(self, tmp_source):
        a = ba.MsiInstallerAdapter(name="foo", version="1.0.0")
        with mock.patch.object(a, "host_compatible", return_value=True):
            with pytest.raises(ba.ArtifactSourceError):
                a.build(ba.BuildSource(path=tmp_source))

    def test_two_phase_compose(self, tmp_path, fake_which_present):
        src = tmp_path / "src"
        src.mkdir()
        (src / "installer.wxs").write_text("<Wix/>\n")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "foo-1.0.0.msi").write_bytes(b"x")
        with mock.patch.object(ba, "_run", return_value=(0, "ok", "", 0.01)) as m:
            a = ba.MsiInstallerAdapter(name="foo", version="1.0.0", output_dir=out_dir)
            with mock.patch.object(a, "host_compatible", return_value=True):
                r = a.build(ba.BuildSource(path=src))
                assert m.call_count == 2  # candle + light
                assert r.status() == "pass"


class TestNsisAdapter:

    def test_requires_nsi(self, tmp_source):
        a = ba.NsisInstallerAdapter(name="foo", version="1.0.0")
        with mock.patch.object(a, "host_compatible", return_value=True):
            with pytest.raises(ba.ArtifactSourceError):
                a.build(ba.BuildSource(path=tmp_source))

    def test_invokes_makensis(self, tmp_path, fake_which_present):
        src = tmp_path / "src"
        src.mkdir()
        (src / "installer.nsi").write_text("OutFile out.exe")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "foo-1.0.0-setup.exe").write_bytes(b"x")
        with mock.patch.object(ba, "_run", return_value=(0, "ok", "", 0.01)) as m:
            a = ba.NsisInstallerAdapter(name="foo", version="1.0.0", output_dir=out_dir)
            with mock.patch.object(a, "host_compatible", return_value=True):
                r = a.build(ba.BuildSource(path=src))
                assert r.status() == "pass"
                cmd = m.call_args.args[0]
                assert any("PRODUCT_VERSION" in c for c in cmd)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  macOS installers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDmgAdapter:

    def test_requires_payload(self, tmp_source):
        a = ba.DmgInstallerAdapter(name="foo", version="1.0.0")
        with mock.patch.object(a, "host_compatible", return_value=True):
            with pytest.raises(ba.ArtifactSourceError):
                a.build(ba.BuildSource(path=tmp_source))


class TestPkgAdapter:

    def test_requires_root_dir(self, tmp_source):
        a = ba.PkgInstallerAdapter(name="foo", version="1.0.0")
        with mock.patch.object(a, "host_compatible", return_value=True):
            with pytest.raises(ba.ArtifactSourceError):
                a.build(ba.BuildSource(path=tmp_source))

    def test_command_includes_identifier(self, tmp_path, fake_which_present):
        src = tmp_path / "src"
        (src / "root").mkdir(parents=True)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "foo-1.0.0.pkg").write_bytes(b"x")
        with mock.patch.object(ba, "_run", return_value=(0, "ok", "", 0.01)) as m:
            a = ba.PkgInstallerAdapter(
                name="foo", version="1.0.0", output_dir=out_dir,
                extra={"identifier": "com.acme.foo", "install_location": "/opt/foo"},
            )
            with mock.patch.object(a, "host_compatible", return_value=True):
                r = a.build(ba.BuildSource(path=src))
                cmd = m.call_args.args[0]
                assert "com.acme.foo" in cmd
                assert "/opt/foo" in cmd
                assert r.status() == "pass"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill-hook adapters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCargoDistAdapter:

    def test_requires_cargo_toml(self, tmp_source):
        a = ba.CargoDistAdapter(name="foo", version="0.1.0")
        with pytest.raises(ba.ArtifactSourceError):
            a.build(ba.BuildSource(path=tmp_source))

    def test_skill_hook_label(self, rust_source, fake_which_present, fake_run_success):
        a = ba.CargoDistAdapter(name="foo", version="0.1.0")
        r = a.build(ba.BuildSource(path=rust_source))
        assert r.skill_hook == "cargo-dist"

    def test_falls_back_to_cargo_subcommand(self, rust_source, fake_run_success):
        with mock.patch.object(ba.shutil, "which", side_effect=lambda x: "/usr/bin/cargo" if x == "cargo" else None):
            a = ba.CargoDistAdapter(name="foo", version="0.1.0")
            r = a.build(ba.BuildSource(path=rust_source))
            cmd = fake_run_success.call_args.args[0]
            assert cmd[1] == "dist"
            assert r.status() == "pass"


class TestGoreleaserAdapter:

    def test_requires_go_mod_or_config(self, tmp_source):
        a = ba.GoreleaserAdapter(name="foo", version="0.1.0")
        with pytest.raises(ba.ArtifactSourceError):
            a.build(ba.BuildSource(path=tmp_source))

    def test_snapshot_when_not_pushing(self, go_source, fake_which_present, fake_run_success):
        a = ba.GoreleaserAdapter(name="foo", version="0.1.0", push=False)
        r = a.build(ba.BuildSource(path=go_source))
        cmd = fake_run_success.call_args.args[0]
        assert "--snapshot" in cmd
        assert r.skill_hook == "goreleaser"

    def test_no_snapshot_when_pushing(self, go_source, fake_which_present, fake_run_success):
        a = ba.GoreleaserAdapter(name="foo", version="0.1.0", push=True)
        a.build(ba.BuildSource(path=go_source))
        cmd = fake_run_success.call_args.args[0]
        assert "--snapshot" not in cmd


class TestPyInstallerAdapter:

    def test_requires_entrypoint(self, tmp_source):
        a = ba.PyInstallerAdapter(name="foo", version="1.0.0")
        with pytest.raises(ba.ArtifactSourceError):
            a.build(ba.BuildSource(path=tmp_source))

    def test_explicit_entrypoint_extra(self, tmp_path, fake_which_present, fake_run_success):
        src = tmp_path / "src"
        src.mkdir()
        (src / "cli.py").write_text("print('x')\n")
        a = ba.PyInstallerAdapter(name="foo", version="1.0.0", extra={"entrypoint": "cli.py"})
        a.build(ba.BuildSource(path=src))
        cmd = fake_run_success.call_args.args[0]
        assert cmd[-1] == "cli.py"

    def test_default_entrypoint_main_py(self, python_source, fake_which_present, fake_run_success):
        a = ba.PyInstallerAdapter(name="foo", version="1.0.0")
        a.build(ba.BuildSource(path=python_source))
        cmd = fake_run_success.call_args.args[0]
        assert cmd[-1] == "main.py"


class TestElectronBuilderAdapter:

    def test_requires_package_json(self, tmp_source):
        a = ba.ElectronBuilderAdapter(name="foo", version="1.0.0")
        with pytest.raises(ba.ArtifactSourceError):
            a.build(ba.BuildSource(path=tmp_source))

    def test_uses_npx_fallback(self, electron_source, fake_run_success):
        with mock.patch.object(ba.shutil, "which", side_effect=lambda x: "/usr/bin/npx" if x == "npx" else None):
            a = ba.ElectronBuilderAdapter(name="foo", version="1.0.0")
            a.build(ba.BuildSource(path=electron_source))
            cmd = fake_run_success.call_args.args[0]
            assert cmd[1] == "electron-builder"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Top-level dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBuildArtifact:

    def test_dispatches_to_correct_adapter(self, docker_source, fake_which_present, fake_run_success):
        r = ba.build_artifact(
            target="docker", app_path=docker_source,
            name="foo", version="1.0.0",
        )
        assert r.target == "docker"

    def test_unknown_target(self, tmp_source):
        with pytest.raises(ba.UnknownTargetError):
            ba.build_artifact(
                target="floppy-disk", app_path=tmp_source,
                name="foo", version="1.0.0",
            )

    def test_invalid_version_propagates(self, tmp_source):
        with pytest.raises(ba.InvalidVersionError):
            ba.build_artifact(
                target="docker", app_path=tmp_source,
                name="foo", version="",
            )

    def test_host_mismatch_raises(self, deb_source):
        # Force current_host_kind to "darwin" → deb adapter should refuse.
        with mock.patch.object(ba, "current_host_kind", return_value="darwin"):
            with pytest.raises(ba.HostMismatchError):
                ba.build_artifact(
                    target="deb", app_path=deb_source,
                    name="foo", version="1.0.0",
                )


class TestBuildMatrix:

    def test_runs_each_target(self, docker_source, fake_which_present, fake_run_success):
        results = ba.build_matrix(
            targets=["docker"],
            app_path=docker_source,
            name="foo", version="1.0.0",
        )
        assert "docker" in results
        assert results["docker"].status() == "pass"

    def test_host_mismatch_returned_as_skip_not_raised(self, deb_source):
        with mock.patch.object(ba, "current_host_kind", return_value="darwin"):
            results = ba.build_matrix(
                targets=["deb"],
                app_path=deb_source,
                name="foo", version="1.0.0",
            )
            assert results["deb"].status() == "skip"
            assert any("host mismatch" in n for n in results["deb"].notes)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Role default targets
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRoleDefaults:

    EXPECTED_ROLES = (
        "backend-python", "backend-go", "backend-rust",
        "backend-node", "backend-java",
        "cli-tooling", "desktop-electron", "desktop-tauri", "desktop-qt",
    )

    def test_every_x2_role_has_default_targets(self):
        for role_id in self.EXPECTED_ROLES:
            targets = ba.default_targets_for_role(role_id)
            assert targets, f"role {role_id!r} has no default targets"

    def test_every_default_target_is_registered(self):
        registered = set(ba.list_targets())
        for role_id in self.EXPECTED_ROLES:
            for t in ba.default_targets_for_role(role_id):
                assert t in registered, f"role {role_id} -> {t} not registered"

    def test_unknown_role_returns_empty(self):
        assert ba.default_targets_for_role("not-a-real-role") == ()

    def test_rust_role_uses_cargo_dist(self):
        assert "cargo-dist" in ba.default_targets_for_role("backend-rust")

    def test_go_role_uses_goreleaser(self):
        assert "goreleaser" in ba.default_targets_for_role("backend-go")

    def test_python_role_uses_pyinstaller(self):
        assert "pyinstaller" in ba.default_targets_for_role("backend-python")

    def test_electron_role_uses_electron_builder(self):
        assert "electron-builder" in ba.default_targets_for_role("desktop-electron")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Declarative config (configs/build_targets.yaml)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigYaml:

    @pytest.fixture
    def cfg(self) -> dict:
        path = ba._PROJECT_ROOT / "configs" / "build_targets.yaml"
        with path.open() as f:
            return yaml.safe_load(f)

    def test_yaml_loads(self, cfg):
        assert cfg["schema_version"] == 1
        assert cfg["ticket"] == "X3 #299"

    def test_yaml_lists_every_registered_target(self, cfg):
        cfg_ids = {t["id"] for t in cfg["targets"]}
        registered = set(ba.list_targets())
        assert cfg_ids == registered, (
            f"YAML targets vs registry mismatch: only-yaml={cfg_ids - registered}, "
            f"only-registry={registered - cfg_ids}"
        )

    def test_yaml_native_skill_split_matches(self, cfg):
        kinds_yaml = {t["id"]: t["kind"] for t in cfg["targets"]}
        for tid in ba.NATIVE_TARGETS:
            assert kinds_yaml[tid] == "native"
        for tid in ba.SKILL_HOOK_TARGETS:
            assert kinds_yaml[tid] == "skill-hook"

    def test_yaml_role_defaults_match_module(self, cfg):
        for role_id, targets in cfg["role_defaults"].items():
            assert tuple(targets) == ba.default_targets_for_role(role_id), (
                f"role {role_id!r} default targets diverged between YAML and module"
            )

    def test_yaml_host_os_matches_module(self, cfg):
        for entry in cfg["targets"]:
            mod_hosts = set(ba.TARGET_HOST_REQUIREMENTS[entry["id"]])
            yaml_hosts = set(entry["host_os"])
            assert mod_hosts == yaml_hosts, (
                f"target {entry['id']!r} host_os mismatch: yaml={yaml_hosts}, module={mod_hosts}"
            )

    def test_release_gates_present(self, cfg):
        assert "release_gates" in cfg
        assert "require_simulate_pass" in cfg["release_gates"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI driver (scripts/build_package.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCliDriver:

    @pytest.fixture
    def cli_path(self) -> Path:
        return ba._PROJECT_ROOT / "scripts" / "build_package.py"

    def test_cli_exists_and_executable(self, cli_path):
        assert cli_path.exists()
        assert os.access(cli_path, os.X_OK)

    def test_cli_list_targets(self, cli_path):
        result = subprocess.run(
            [sys.executable, str(cli_path), "--list-targets"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert set(payload["targets"]) == set(ba.list_targets())

    def test_cli_unknown_target_exits_2(self, cli_path, tmp_path):
        result = subprocess.run(
            [sys.executable, str(cli_path),
             "--target=does-not-exist", "--app-path", str(tmp_path),
             "--name=foo", "--version=1.0.0"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2

    def test_cli_skip_when_no_runner(self, cli_path, docker_source):
        # Force which() to return None by clearing PATH. We can't patch
        # the subprocess from here, so we rely on the build returning a
        # skip when docker isn't available. We'll do this by giving an
        # invalid app_path so it errors before tool dispatch is checked.
        # Instead, run a target whose source-validate succeeds and whose
        # runner is intentionally missing — we use a fake env with PATH=
        env = os.environ.copy()
        env["PATH"] = "/nonexistent"
        result = subprocess.run(
            [sys.executable, str(cli_path),
             "--target=docker", "--app-path", str(docker_source),
             "--name=foo", "--version=1.0.0", "--pretty"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        # Exit 3 = every target was skipped (no runner on PATH).
        assert result.returncode == 3, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["counts"]["skip"] == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Output pattern coverage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestOutputPatterns:

    @pytest.mark.parametrize("target,expected", [
        ("docker", "foo:1.0.0"),
        ("helm", "foo-1.0.0.tgz"),
        ("deb", "foo_1.0.0_amd64.deb"),
        ("nsis", "foo-1.0.0-setup.exe"),
        ("dmg", "foo-1.0.0.dmg"),
        ("pkg", "foo-1.0.0.pkg"),
        ("msi", "foo-1.0.0.msi"),
    ])
    def test_pattern_substitution(self, target, expected):
        cls = ba.get_adapter(target)
        a = cls(name="foo", version="1.0.0", arch="amd64" if target == "deb" else "noarch")
        assert a.expected_artifact_path() == expected

    def test_rpm_pattern(self):
        a = ba.RpmPackageAdapter(name="foo", version="1.0.0", arch="x86_64")
        assert a.expected_artifact_path() == "foo-1.0.0.x86_64.rpm"

    def test_electron_ext(self):
        a = ba.ElectronBuilderAdapter(name="foo", version="1.0.0", extra={"ext": "AppImage"})
        assert a.expected_artifact_path() == "foo-1.0.0.AppImage"
