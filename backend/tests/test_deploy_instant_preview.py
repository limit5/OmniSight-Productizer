"""V4 #2 (TODO row 1533) — Contract tests for the W4 instant-preview
quick-mode (``vercel deploy --preview`` REST parity + ``docker run``
localhost URL), covering:

- pure helpers (TTL / URL parsing / safe image tag / container naming /
  free-port search / docker port parsing / cleanup command rendering)
- ``create_docker_run_preview`` via a recording subprocess stub (no
  daemon touched)
- ``create_vercel_preview`` via ``respx`` (no network)
- ``create_instant_preview`` dispatcher routing + error cases
- package-level re-exports

The tests pin the *contract* — if a future refactor changes the result
shape or helper semantics, a failing assertion surfaces before the
router / HMI layer breaks.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from backend import deploy
from backend.deploy import BuildArtifact
from backend.deploy.base import DeployArtifactError
from backend.deploy.docker_nginx import DockerNginxAdapter
from backend.deploy.instant_preview import (
    INSTANT_PREVIEW_MODES,
    MODE_DOCKER_RUN,
    MODE_VERCEL_PREVIEW,
    InstantPreviewError,
    InstantPreviewResult,
    InstantPreviewUnavailable,
    _DOCKER_HOST_PORT_RANGE,
    assert_safe_image_tag,
    build_preview_container_name,
    compute_expires_at,
    create_docker_run_preview,
    create_instant_preview,
    create_vercel_preview,
    default_ttl_seconds,
    extract_vercel_preview_url,
    find_free_port,
    format_cleanup_command,
    normalize_project_for_image,
    parse_docker_port_line,
    validate_preview_port,
)
from backend.deploy.vercel import VERCEL_API_BASE, VercelAdapter

V = VERCEL_API_BASE


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def build_site(tmp_path):
    """A tiny static build artifact on disk."""
    site = tmp_path / "build-out"
    site.mkdir()
    (site / "index.html").write_text("<!doctype html><title>x</title>")
    (site / "assets").mkdir()
    (site / "assets" / "app.js").write_text("console.log('hi');")
    return site


def _mk_docker(tmp_path, **kw):
    return DockerNginxAdapter.from_plaintext_token(
        token="",
        project_name="demo-site",
        output_dir=tmp_path / "deploy-ctx",
        port=8082,
        **kw,
    )


def _mk_vercel(**kw):
    return VercelAdapter(
        token="vrc_test_token_ABCD1234",
        project_name="demo-app",
        **kw,
    )


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


class _RecordingRun:
    """A drop-in stand-in for ``subprocess.run`` that records calls and
    returns canned ``CompletedProcess``-shaped values (using
    SimpleNamespace so we sidestep subprocess's stricter ctor)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        if not self._responses:
            # default fallback: success w/ empty output
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ── Pure helpers ──────────────────────────────────────────────────

class TestDefaultTtlSeconds:

    def test_vercel_preview_defaults_to_24h(self):
        assert default_ttl_seconds(MODE_VERCEL_PREVIEW) == 24 * 3600

    def test_docker_run_defaults_to_1h(self):
        assert default_ttl_seconds(MODE_DOCKER_RUN) == 3600

    def test_unknown_mode_falls_back_to_1h(self):
        assert default_ttl_seconds("mystery-mode") == 3600

    def test_mode_constants_are_exact(self):
        assert MODE_VERCEL_PREVIEW == "vercel-preview"
        assert MODE_DOCKER_RUN == "docker-run"
        assert INSTANT_PREVIEW_MODES == ("vercel-preview", "docker-run")


class TestComputeExpiresAt:

    def test_iso_8601_z_stamp(self):
        now = datetime.datetime(2026, 4, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
        out = compute_expires_at(now, 3600)
        assert out == "2026-04-18T13:00:00Z"

    def test_handles_naive_datetime_as_utc(self):
        # No tzinfo → treat as UTC so downstream parsing is deterministic.
        naive = datetime.datetime(2026, 4, 18, 0, 0, 0)
        out = compute_expires_at(naive, 60)
        assert out == "2026-04-18T00:01:00Z"

    def test_zero_ttl_yields_now_stamp(self):
        now = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        assert compute_expires_at(now, 0) == "2026-01-01T00:00:00Z"

    def test_negative_ttl_treated_as_zero(self):
        now = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        assert compute_expires_at(now, -999) == "2026-01-01T00:00:00Z"


class TestAssertSafeImageTag:

    @pytest.mark.parametrize("tag", [
        "latest", "v1.2.3", "preview",
        "foo-bar_baz.qux", "A1",
    ])
    def test_accepts_common_tags(self, tag):
        assert assert_safe_image_tag(tag) == tag

    @pytest.mark.parametrize("tag", [
        "", "bad tag", "with;semi", "back`tick`", "$(whoami)",
        "new\nline", "dot.start" + "x" * 200,
        "-leading-dash", ".leading-dot",
    ])
    def test_rejects_unsafe_tags(self, tag):
        with pytest.raises(InstantPreviewError):
            assert_safe_image_tag(tag)

    def test_non_string_raises(self):
        with pytest.raises(InstantPreviewError):
            assert_safe_image_tag(None)  # type: ignore[arg-type]


class TestNormalizeProjectForImage:

    def test_lowercases_and_trims(self):
        assert normalize_project_for_image("  Demo-Site ") == "demo-site"

    def test_replaces_unsafe_punct(self):
        assert normalize_project_for_image("My!App@v1") == "my-app-v1"

    def test_strips_leading_punct(self):
        assert normalize_project_for_image("---cool-proj") == "cool-proj"

    def test_caps_at_64_chars(self):
        out = normalize_project_for_image("a" * 200)
        assert len(out) == 64
        assert out == "a" * 64

    def test_empty_raises(self):
        with pytest.raises(InstantPreviewError):
            normalize_project_for_image("")

    def test_all_unsafe_raises(self):
        with pytest.raises(InstantPreviewError):
            normalize_project_for_image("!!!")

    def test_preserves_alnum_dash_dot_underscore(self):
        assert normalize_project_for_image("foo.bar_baz-qux") == "foo.bar_baz-qux"


class TestExtractVercelPreviewUrl:

    def test_pulls_first_vercel_url(self):
        stdout = "Deploying...\nPreview: https://my-app-abc123.vercel.app\nReady."
        assert extract_vercel_preview_url(stdout) == "https://my-app-abc123.vercel.app"

    def test_ignores_non_vercel_urls(self):
        stdout = "Docs: https://docs.example.com — build OK"
        assert extract_vercel_preview_url(stdout) is None

    def test_empty_input_returns_none(self):
        assert extract_vercel_preview_url("") is None
        assert extract_vercel_preview_url(None) is None  # type: ignore[arg-type]

    def test_only_matches_vercel_app_host(self):
        stdout = "Preview: https://not-vercel.example.com"
        assert extract_vercel_preview_url(stdout) is None


class TestParseDockerPortLine:

    def test_parses_ipv4_mapping(self):
        assert parse_docker_port_line("8080/tcp -> 0.0.0.0:32768") == ("0.0.0.0", 32768)

    def test_parses_ipv6_mapping(self):
        assert parse_docker_port_line("8080/tcp -> [::]:49153") == ("::", 49153)

    def test_rejects_missing_arrow(self):
        assert parse_docker_port_line("8080/tcp") is None

    def test_rejects_empty(self):
        assert parse_docker_port_line("") is None

    def test_rejects_malformed_port(self):
        assert parse_docker_port_line("8080/tcp -> 0.0.0.0:abc") is None

    def test_rejects_out_of_range(self):
        assert parse_docker_port_line("8080/tcp -> 0.0.0.0:99999") is None

    def test_rejects_unterminated_ipv6(self):
        assert parse_docker_port_line("8080/tcp -> [::49153") is None


class TestFindFreePort:

    def test_uses_probe_when_in_range(self):
        calls = []

        def probe():
            calls.append(1)
            return 50000

        port = find_free_port(_probe=probe)
        assert port == 50000
        assert len(calls) == 1

    def test_invalid_range_raises(self):
        with pytest.raises(InstantPreviewError):
            find_free_port(start=5000, end=5000)

    def test_probe_out_of_range_falls_back_to_scan(self, monkeypatch):
        # If probe returns outside [start, end], we fall back to a
        # deterministic scan of the window.
        def probe():
            return 80  # way under the dynamic range

        # The scan path will try to bind — rather than hit the OS, we
        # monkeypatch socket.socket to accept binding to port ``start``.
        import socket as _socket

        class FakeSock:
            calls = 0

            def setsockopt(self, *a, **kw):
                pass

            def bind(self, addr):
                FakeSock.calls += 1
                # first call succeeds on start port
                if FakeSock.calls > 1:
                    raise OSError("taken")

            def close(self):
                pass

        monkeypatch.setattr(_socket, "socket", lambda *a, **kw: FakeSock())
        port = find_free_port(start=55000, end=55010, _probe=probe)
        assert port == 55000

    def test_default_range_matches_iana_dynamic(self):
        assert _DOCKER_HOST_PORT_RANGE == (49152, 65535)


class TestValidatePreviewPort:

    @pytest.mark.parametrize("port", [1, 80, 8080, 65535])
    def test_accepts_valid(self, port):
        assert validate_preview_port(port) == port

    @pytest.mark.parametrize("port", [0, -1, 65536, 999999])
    def test_rejects_out_of_range(self, port):
        with pytest.raises(InstantPreviewError):
            validate_preview_port(port)

    def test_rejects_non_int(self):
        with pytest.raises(InstantPreviewError):
            validate_preview_port("8080")  # type: ignore[arg-type]


class TestFormatCleanupCommand:

    def test_docker_run_quotes_container_name(self):
        cmd = format_cleanup_command(MODE_DOCKER_RUN, container_name="omnisight-preview-demo")
        assert cmd == "docker rm -f omnisight-preview-demo"

    def test_docker_run_shell_quotes_weird_names(self):
        cmd = format_cleanup_command(MODE_DOCKER_RUN, container_name="has space")
        # shlex.quote wraps in single quotes
        assert "'has space'" in cmd

    def test_docker_run_without_name_degrades_to_docker_ps(self):
        assert format_cleanup_command(MODE_DOCKER_RUN) == "docker ps"

    def test_vercel_preview_uses_safe_flag(self):
        cmd = format_cleanup_command(
            MODE_VERCEL_PREVIEW, deployment_id="dpl_abc123",
        )
        assert cmd == "vercel remove dpl_abc123 --safe --yes"

    def test_vercel_preview_falls_back_to_project_name(self):
        cmd = format_cleanup_command(
            MODE_VERCEL_PREVIEW, project_name="demo-app",
        )
        assert cmd == "vercel remove demo-app --safe --yes"

    def test_vercel_preview_without_any_target_lists(self):
        assert format_cleanup_command(MODE_VERCEL_PREVIEW) == "vercel list"

    def test_unknown_mode_returns_empty(self):
        assert format_cleanup_command("something-else") == ""


class TestBuildPreviewContainerName:

    def test_uses_suffix_when_provided(self):
        name = build_preview_container_name("Demo-Site", suffix="abc12345")
        assert name == "omnisight-preview-demo-site-abc12345"

    def test_generates_random_suffix_by_default(self):
        n1 = build_preview_container_name("demo")
        n2 = build_preview_container_name("demo")
        assert n1 != n2  # effectively always — 8 hex chars
        assert n1.startswith("omnisight-preview-demo-")

    def test_sanitises_unsafe_project_name(self):
        name = build_preview_container_name("My!Weird@Project", suffix="ffff0000")
        assert name == "omnisight-preview-my-weird-project-ffff0000"

    def test_sanitises_suffix(self):
        name = build_preview_container_name("demo", suffix="bad!suffix$$")
        # Non-alnum stripped; empty result fallback to random hex.
        assert name.startswith("omnisight-preview-demo-")
        core = name.rsplit("-", 1)[1]
        assert re.match(r"^[A-Za-z0-9]+$", core)


# ── InstantPreviewResult dataclass ─────────────────────────────────

class TestInstantPreviewResultDataclass:

    def test_to_dict_roundtrip(self):
        r = InstantPreviewResult(
            mode=MODE_DOCKER_RUN,
            url="http://localhost:32768",
            provider="docker-nginx",
            project_name="demo",
            deployment_id="cid-abc",
            host_port=32768,
            image_tag="demo:preview",
            expires_at="2026-04-18T13:00:00Z",
            ttl_seconds=3600,
            cleanup_command="docker rm -f x",
            commit_sha="deadbeef",
        )
        d = r.to_dict()
        assert d["mode"] == MODE_DOCKER_RUN
        assert d["full_ci_cd"] is False
        assert d["host_port"] == 32768
        assert d["url"] == "http://localhost:32768"
        assert d["ttl_seconds"] == 3600

    def test_full_ci_cd_defaults_false(self):
        r = InstantPreviewResult(
            mode=MODE_VERCEL_PREVIEW,
            url="https://x.vercel.app",
            provider="vercel",
            project_name="x",
            deployment_id="dpl",
        )
        assert r.full_ci_cd is False


# ── docker-run quick mode ─────────────────────────────────────────

class TestCreateDockerRunPreviewSuccess:

    def test_happy_path_produces_localhost_url(
        self, tmp_path, build_site, monkeypatch,
    ):
        adapter = _mk_docker(tmp_path)
        rec = _RecordingRun([
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # docker build
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # docker rm -f (stale cleanup)
            SimpleNamespace(                                       # docker run -d
                returncode=0,
                stdout="abc123def456container-id-0000\n",
                stderr="",
            ),
        ])
        monkeypatch.setattr(
            "backend.deploy.instant_preview._docker_cli",
            lambda: "/usr/bin/docker",
        )

        result = create_docker_run_preview(
            adapter,
            BuildArtifact(path=build_site, commit_sha="cafef00d"),
            host_port=50123,
            ttl_seconds=600,
            run_subprocess=rec,
            now=datetime.datetime(2026, 4, 18, 10, 0, 0, tzinfo=datetime.timezone.utc),
        )

        assert result.mode == MODE_DOCKER_RUN
        assert result.url == "http://localhost:50123"
        assert result.host_port == 50123
        assert result.provider == "docker-nginx"
        assert result.full_ci_cd is False
        assert result.ttl_seconds == 600
        assert result.expires_at == "2026-04-18T10:10:00Z"
        assert result.commit_sha == "cafef00d"
        # deployment_id is the container id from docker run stdout
        assert result.deployment_id == "abc123def456container-id-0000"
        # cleanup command references the generated container name
        assert result.cleanup_command.startswith("docker rm -f omnisight-preview-demo-site-")
        assert result.raw["files_copied"] == 2
        # three subprocess calls: build + rm + run
        assert len(rec.calls) == 3
        build_cmd = rec.calls[0]
        run_cmd = rec.calls[2]
        assert build_cmd[0] == "/usr/bin/docker"
        assert "build" in build_cmd and "-t" in build_cmd
        # run_cmd binds 127.0.0.1:<port>:<internal_port>
        assert "-p" in run_cmd
        p_idx = run_cmd.index("-p")
        assert run_cmd[p_idx + 1] == "127.0.0.1:50123:8082"
        # label is set
        assert "--label" in run_cmd
        label = run_cmd[run_cmd.index("--label") + 1]
        assert label == "org.opencontainers.omnisight.mode=instant-preview"

    def test_auto_picks_free_port_when_not_specified(
        self, tmp_path, build_site, monkeypatch,
    ):
        adapter = _mk_docker(tmp_path)
        rec = _RecordingRun([
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="cid\n", stderr=""),
        ])
        monkeypatch.setattr(
            "backend.deploy.instant_preview._docker_cli",
            lambda: "/usr/bin/docker",
        )
        result = create_docker_run_preview(
            adapter,
            BuildArtifact(path=build_site),
            run_subprocess=rec,
            port_probe=lambda: 50555,
            now=datetime.datetime(2026, 4, 18, tzinfo=datetime.timezone.utc),
        )
        assert result.host_port == 50555
        assert result.url == "http://localhost:50555"
        # default ttl is 1h for docker-run
        assert result.ttl_seconds == 3600

    def test_ensures_build_context_even_when_output_dir_missing(
        self, tmp_path, build_site, monkeypatch,
    ):
        adapter = _mk_docker(tmp_path)
        rec = _RecordingRun([
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="cid\n", stderr=""),
        ])
        monkeypatch.setattr(
            "backend.deploy.instant_preview._docker_cli",
            lambda: "/usr/bin/docker",
        )
        create_docker_run_preview(
            adapter,
            BuildArtifact(path=build_site),
            host_port=50100,
            run_subprocess=rec,
        )
        # After call, Dockerfile + nginx.conf should exist (provision-equivalent).
        assert (adapter.output_dir / "Dockerfile").exists()
        assert (adapter.output_dir / "nginx.conf").exists()
        # Public dir populated with build artifact.
        assert (adapter.output_dir / "public" / "index.html").exists()


class TestCreateDockerRunPreviewErrors:

    def test_wrong_adapter_type_raises(self, tmp_path, build_site):
        with pytest.raises(InstantPreviewError, match="DockerNginxAdapter"):
            create_docker_run_preview(
                _mk_vercel(),  # type: ignore[arg-type]
                BuildArtifact(path=build_site),
            )

    def test_missing_docker_raises_unavailable(
        self, tmp_path, build_site, monkeypatch,
    ):
        adapter = _mk_docker(tmp_path)
        monkeypatch.setattr(
            "backend.deploy.instant_preview.shutil.which",
            lambda name: None,
        )
        with pytest.raises(InstantPreviewUnavailable):
            create_docker_run_preview(
                adapter,
                BuildArtifact(path=build_site),
            )

    def test_build_failure_raises(
        self, tmp_path, build_site, monkeypatch,
    ):
        adapter = _mk_docker(tmp_path)
        monkeypatch.setattr(
            "backend.deploy.instant_preview._docker_cli",
            lambda: "/usr/bin/docker",
        )
        rec = _RecordingRun([
            SimpleNamespace(returncode=1, stdout="", stderr="build failed!"),
        ])
        with pytest.raises(InstantPreviewError, match="docker build failed"):
            create_docker_run_preview(
                adapter,
                BuildArtifact(path=build_site),
                host_port=50111,
                run_subprocess=rec,
            )

    def test_run_failure_raises(
        self, tmp_path, build_site, monkeypatch,
    ):
        adapter = _mk_docker(tmp_path)
        monkeypatch.setattr(
            "backend.deploy.instant_preview._docker_cli",
            lambda: "/usr/bin/docker",
        )
        rec = _RecordingRun([
            SimpleNamespace(returncode=0, stdout="", stderr=""),           # build OK
            SimpleNamespace(returncode=0, stdout="", stderr=""),           # stale rm
            SimpleNamespace(returncode=125, stdout="", stderr="port busy"),# run FAIL
        ])
        with pytest.raises(InstantPreviewError, match="docker run failed"):
            create_docker_run_preview(
                adapter,
                BuildArtifact(path=build_site),
                host_port=50222,
                run_subprocess=rec,
            )

    def test_empty_artifact_raises(self, tmp_path, monkeypatch):
        adapter = _mk_docker(tmp_path)
        monkeypatch.setattr(
            "backend.deploy.instant_preview._docker_cli",
            lambda: "/usr/bin/docker",
        )
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(DeployArtifactError):
            create_docker_run_preview(
                adapter,
                BuildArtifact(path=empty),
                host_port=50333,
                run_subprocess=_RecordingRun([]),
            )


# ── vercel-preview quick mode ─────────────────────────────────────

class TestCreateVercelPreviewSuccess:

    @respx.mock
    async def test_preview_target_via_rest_api(self, build_site):
        adapter = _mk_vercel(project_id="prj_123")
        # File upload: two unique SHA1 files → two POST /v2/files calls
        respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        deploy_route = respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({
                "id": "dpl_preview_xyz",
                "url": "demo-app-preview-xyz.vercel.app",
                "readyState": "READY",
            }),
        )
        result = await create_vercel_preview(
            adapter,
            BuildArtifact(path=build_site, commit_sha="cafef00d"),
            ttl_seconds=7200,
            now=datetime.datetime(2026, 4, 18, 10, 0, 0, tzinfo=datetime.timezone.utc),
        )
        assert result.mode == MODE_VERCEL_PREVIEW
        assert result.url == "https://demo-app-preview-xyz.vercel.app"
        assert result.deployment_id == "dpl_preview_xyz"
        assert result.provider == "vercel"
        assert result.ttl_seconds == 7200
        assert result.expires_at == "2026-04-18T12:00:00Z"
        assert result.full_ci_cd is False
        assert result.cleanup_command == "vercel remove dpl_preview_xyz --safe --yes"
        # Confirm target=preview was actually sent
        sent = deploy_route.calls.last.request.read()
        assert b'"target":"preview"' in sent.replace(b" ", b"")

    @respx.mock
    async def test_looks_up_project_when_id_missing(self, build_site):
        adapter = _mk_vercel()  # no project_id on construct
        respx.get(f"{V}/v9/projects/demo-app").mock(
            return_value=_ok({"id": "prj_lookup", "name": "demo-app"}),
        )
        respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({
                "id": "dpl_xyz",
                "url": "demo-app-abc.vercel.app",
                "readyState": "QUEUED",
            }),
        )
        result = await create_vercel_preview(
            adapter,
            BuildArtifact(path=build_site),
        )
        assert result.url == "https://demo-app-abc.vercel.app"
        assert adapter._project_id == "prj_lookup"

    @respx.mock
    async def test_missing_url_degrades_to_canonical_host(self, build_site):
        adapter = _mk_vercel(project_id="prj_123")
        respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        # No url field in response
        respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({"id": "dpl_noop"}),
        )
        result = await create_vercel_preview(
            adapter,
            BuildArtifact(path=build_site),
        )
        assert result.url == "https://demo-app.vercel.app"
        assert result.deployment_id == "dpl_noop"


class TestCreateVercelPreviewErrors:

    async def test_wrong_adapter_type_raises(self, tmp_path, build_site):
        with pytest.raises(InstantPreviewError, match="VercelAdapter"):
            await create_vercel_preview(
                _mk_docker(tmp_path),  # type: ignore[arg-type]
                BuildArtifact(path=build_site),
            )

    @respx.mock
    async def test_empty_artifact_raises(self, tmp_path):
        adapter = _mk_vercel(project_id="prj_123")
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(DeployArtifactError):
            await create_vercel_preview(
                adapter,
                BuildArtifact(path=empty),
            )


# ── Dispatcher ────────────────────────────────────────────────────

class TestCreateInstantPreviewDispatcher:

    async def test_picks_docker_run_for_docker_adapter(
        self, tmp_path, build_site, monkeypatch,
    ):
        adapter = _mk_docker(tmp_path)
        monkeypatch.setattr(
            "backend.deploy.instant_preview._docker_cli",
            lambda: "/usr/bin/docker",
        )
        rec = _RecordingRun([
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="cid\n", stderr=""),
        ])
        r = await create_instant_preview(
            adapter,
            BuildArtifact(path=build_site),
            host_port=50999,
            run_subprocess=rec,
        )
        assert r.mode == MODE_DOCKER_RUN
        assert r.url == "http://localhost:50999"

    @respx.mock
    async def test_picks_vercel_preview_for_vercel_adapter(self, build_site):
        adapter = _mk_vercel(project_id="prj_1")
        respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({"id": "dpl_1", "url": "demo-app-1.vercel.app"}),
        )
        r = await create_instant_preview(
            adapter,
            BuildArtifact(path=build_site),
        )
        assert r.mode == MODE_VERCEL_PREVIEW
        assert r.url == "https://demo-app-1.vercel.app"

    async def test_rejects_mode_mismatch_docker_run_on_vercel_adapter(self, build_site):
        with pytest.raises(InstantPreviewError, match="requires a DockerNginxAdapter"):
            await create_instant_preview(
                _mk_vercel(project_id="x"),
                BuildArtifact(path=build_site),
                mode=MODE_DOCKER_RUN,
            )

    async def test_rejects_mode_mismatch_vercel_on_docker_adapter(
        self, tmp_path, build_site,
    ):
        with pytest.raises(InstantPreviewError, match="requires a VercelAdapter"):
            await create_instant_preview(
                _mk_docker(tmp_path),
                BuildArtifact(path=build_site),
                mode=MODE_VERCEL_PREVIEW,
            )

    async def test_rejects_unknown_mode(self, tmp_path, build_site):
        with pytest.raises(InstantPreviewError, match="Unknown instant preview mode"):
            await create_instant_preview(
                _mk_docker(tmp_path),
                BuildArtifact(path=build_site),
                mode="s3-signed-url",
            )

    async def test_rejects_unsupported_adapter(self, build_site):
        class FakeAdapter:
            provider = "fake"

        with pytest.raises(InstantPreviewError, match="not supported"):
            await create_instant_preview(
                FakeAdapter(),  # type: ignore[arg-type]
                BuildArtifact(path=build_site),
            )


# ── Package-level re-exports ──────────────────────────────────────

class TestPackageLevelReExports:

    def test_exposes_instant_preview_symbols_from_backend_deploy(self):
        assert deploy.MODE_DOCKER_RUN == MODE_DOCKER_RUN
        assert deploy.MODE_VERCEL_PREVIEW == MODE_VERCEL_PREVIEW
        assert deploy.INSTANT_PREVIEW_MODES == INSTANT_PREVIEW_MODES
        assert deploy.InstantPreviewResult is InstantPreviewResult
        assert deploy.InstantPreviewError is InstantPreviewError
        assert deploy.InstantPreviewUnavailable is InstantPreviewUnavailable
        assert deploy.create_docker_run_preview is create_docker_run_preview
        assert deploy.create_vercel_preview is create_vercel_preview
        assert deploy.create_instant_preview is create_instant_preview

    def test_all_instant_preview_modes_in_tuple(self):
        # every canonical mode string must be present in the tuple
        assert MODE_DOCKER_RUN in INSTANT_PREVIEW_MODES
        assert MODE_VERCEL_PREVIEW in INSTANT_PREVIEW_MODES
        # no duplicates
        assert len(set(INSTANT_PREVIEW_MODES)) == len(INSTANT_PREVIEW_MODES)

    def test_errors_are_subclass_of_deploy_error(self):
        from backend.deploy.base import DeployError
        assert issubclass(InstantPreviewError, DeployError)
        assert issubclass(InstantPreviewUnavailable, InstantPreviewError)
