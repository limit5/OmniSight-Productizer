"""W14.2 — `backend/web_sandbox.py` contract tests.

Pins the launcher module's structural + behavioural promises:

* Module surface (``__all__`` membership, schema version, default
  constants, error hierarchy).
* :class:`WebPreviewManifest` validation (image_name / runtime_uid /
  workdir / exposed_ports cross-checks against the on-disk W14.1
  ``web-preview/manifest.json``).
* :class:`WebSandboxConfig` validation (workspace_id charset, git_ref
  charset, abs workspace path, abs workdir, container_port range,
  command tuple normalisation, env key/value types, positive
  timeouts).
* :class:`WebSandboxInstance` lifecycle correctness — ``pending →
  installing → running → stopping → stopped`` via
  :meth:`WebSandboxManager.launch / mark_ready / stop`; idempotent
  re-launch path; docker-name-conflict recovery via inspect.
* Pure helpers — :func:`format_sandbox_id` /
  :func:`format_container_name` / :func:`build_preview_url` /
  :func:`allocate_host_port` / :func:`build_install_argv` /
  :func:`build_dev_argv` / :func:`build_composite_command` /
  :func:`build_docker_run_spec` are all deterministic functions of
  their inputs.
* Manifest cross-check — :func:`build_docker_run_spec` raises when
  the caller's ``workdir`` or ``container_port`` disagrees with the
  W14.1 image manifest.
* Graceful docker failure paths — :meth:`WebSandboxManager.launch`
  records ``status=failed`` rather than re-raising mid-agent-loop.
* Event callback emission on every state transition.
* Concurrency: 16 worker threads launching 16 distinct workspace ids
  do not corrupt manager state.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from backend import web_sandbox as ws
from backend.web_sandbox import (
    DEFAULT_CONTAINER_PORT,
    DEFAULT_DEV_COMMAND,
    DEFAULT_HOST_PORT_RANGE,
    DEFAULT_IMAGE_TAG,
    DEFAULT_INSTALL_COMMAND,
    DEFAULT_STARTUP_TIMEOUT_S,
    DEFAULT_STOP_TIMEOUT_S,
    DEFAULT_WORKDIR,
    MANIFEST_RELATIVE_PATH,
    MAX_LOG_CHARS,
    WEB_SANDBOX_SCHEMA_VERSION,
    WebPreviewManifest,
    WebSandboxAlreadyExists,
    WebSandboxConfig,
    WebSandboxError,
    WebSandboxInstance,
    WebSandboxManager,
    WebSandboxNotFound,
    WebSandboxStatus,
    allocate_host_port,
    build_composite_command,
    build_dev_argv,
    build_docker_run_spec,
    build_install_argv,
    build_preview_url,
    detect_dev_server_ready,
    format_container_name,
    format_sandbox_id,
    load_image_manifest,
    validate_workspace_path,
)


# ── Module invariants ────────────────────────────────────────────────


EXPECTED_ALL = {
    "WEB_SANDBOX_SCHEMA_VERSION",
    "DEFAULT_IMAGE_TAG",
    "DEFAULT_INSTALL_COMMAND",
    "DEFAULT_DEV_COMMAND",
    "DEFAULT_CONTAINER_PORT",
    "DEFAULT_HOST_PORT_RANGE",
    "DEFAULT_WORKDIR",
    "DEFAULT_STARTUP_TIMEOUT_S",
    "DEFAULT_STOP_TIMEOUT_S",
    "DEFAULT_PREVIEW_HOST",
    "MANIFEST_RELATIVE_PATH",
    "MANIFEST_ABSOLUTE_IN_CONTAINER",
    "MAX_LOG_CHARS",
    "WebSandboxStatus",
    "WebPreviewManifest",
    "WebSandboxConfig",
    "WebSandboxInstance",
    "WebSandboxError",
    "WebSandboxAlreadyExists",
    "WebSandboxNotFound",
    "WebSandboxNameConflict",
    "WebSandboxManager",
    "DEFAULT_RESOURCE_LIMITS",
    "load_image_manifest",
    "format_sandbox_id",
    "format_container_name",
    "build_preview_url",
    "build_install_argv",
    "build_dev_argv",
    "build_composite_command",
    "build_docker_run_spec",
    "allocate_host_port",
    "validate_workspace_path",
    "detect_dev_server_ready",
}


def test_all_exports_match_expected() -> None:
    assert set(ws.__all__) == EXPECTED_ALL


def test_all_exports_unique_and_alphabetisable() -> None:
    # No duplicates, every export resolvable on the module.
    assert len(ws.__all__) == len(set(ws.__all__))
    for name in ws.__all__:
        assert hasattr(ws, name), f"missing export: {name}"


def test_schema_version_is_semver() -> None:
    parts = WEB_SANDBOX_SCHEMA_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_defaults_match_w14_1_image() -> None:
    assert DEFAULT_IMAGE_TAG == "omnisight-web-preview:dev"
    assert DEFAULT_WORKDIR == "/workspace"
    # 5173 = Vite, 3000 = Nuxt — image EXPOSEs both, default to 5173.
    assert DEFAULT_CONTAINER_PORT == 5173
    assert DEFAULT_INSTALL_COMMAND == ("pnpm", "install", "--frozen-lockfile")
    assert DEFAULT_DEV_COMMAND == ("pnpm", "dev", "--host", "0.0.0.0")


def test_default_host_port_range_disjoint_from_ui_sandbox() -> None:
    # ui_sandbox uses 40000-40999; web_sandbox uses 41000-41999.
    from backend.ui_sandbox import DEFAULT_HOST_PORT_RANGE as UI_RANGE

    ui_lo, ui_hi = UI_RANGE
    ws_lo, ws_hi = DEFAULT_HOST_PORT_RANGE
    assert ws_lo > ui_hi or ws_hi < ui_lo, (
        f"ui range {UI_RANGE} and web range {DEFAULT_HOST_PORT_RANGE} overlap"
    )


def test_default_timeouts_positive() -> None:
    assert DEFAULT_STARTUP_TIMEOUT_S > 0
    assert DEFAULT_STOP_TIMEOUT_S > 0
    # Startup timeout must comfortably accommodate cold-cache pnpm
    # install (30-90s) + dev-server ready (10-30s).
    assert DEFAULT_STARTUP_TIMEOUT_S >= 120


def test_max_log_chars_matches_ui_sandbox() -> None:
    from backend.ui_sandbox import MAX_LOG_CHARS as UI_MAX

    assert MAX_LOG_CHARS == UI_MAX


def test_manifest_relative_path_points_at_w14_1_artefact() -> None:
    assert MANIFEST_RELATIVE_PATH == Path("web-preview") / "manifest.json"


def test_error_hierarchy() -> None:
    assert issubclass(WebSandboxAlreadyExists, WebSandboxError)
    assert issubclass(WebSandboxNotFound, WebSandboxError)
    assert issubclass(WebSandboxError, RuntimeError)


def test_status_enum_values() -> None:
    assert {s.value for s in WebSandboxStatus} == {
        "pending",
        "installing",
        "running",
        "stopping",
        "stopped",
        "failed",
    }


# ── load_image_manifest cross-check ─────────────────────────────────


def test_load_image_manifest_reads_w14_1_artefact() -> None:
    manifest = load_image_manifest()
    assert manifest.image_name == "omnisight-web-preview"
    assert manifest.workdir == "/workspace"
    assert 5173 in manifest.exposed_ports
    assert 3000 in manifest.exposed_ports
    assert manifest.runtime_uid == 10002
    assert manifest.runtime_gid == 10002
    assert "node_major" in manifest.version_pins
    assert manifest.default_cmd[0] == "pnpm"


def test_load_image_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(WebSandboxError) as exc:
        load_image_manifest(tmp_path)
    assert "manifest missing" in str(exc.value)


def test_load_image_manifest_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "web-preview").mkdir()
    (tmp_path / "web-preview" / "manifest.json").write_text("not json")
    with pytest.raises(WebSandboxError) as exc:
        load_image_manifest(tmp_path)
    assert "not valid JSON" in str(exc.value)


def test_load_image_manifest_missing_required_key(tmp_path: Path) -> None:
    (tmp_path / "web-preview").mkdir()
    (tmp_path / "web-preview" / "manifest.json").write_text(
        '{"image_name": "x"}'
    )
    with pytest.raises(WebSandboxError):
        load_image_manifest(tmp_path)


def test_manifest_post_init_rejects_bad_workdir() -> None:
    with pytest.raises(ValueError):
        WebPreviewManifest(
            image_name="x",
            runtime_uid=1000,
            runtime_gid=1000,
            workdir="not-absolute",
            exposed_ports=(5173,),
            version_pins={},
            entrypoint="/bin/sh",
            default_cmd=("sh",),
            schema_version="1",
            raw={},
        )


def test_manifest_post_init_rejects_zero_uid() -> None:
    with pytest.raises(ValueError):
        WebPreviewManifest(
            image_name="x",
            runtime_uid=0,
            runtime_gid=1000,
            workdir="/workspace",
            exposed_ports=(5173,),
            version_pins={},
            entrypoint="/bin/sh",
            default_cmd=("sh",),
            schema_version="1",
            raw={},
        )


def test_manifest_post_init_rejects_empty_ports() -> None:
    with pytest.raises(ValueError):
        WebPreviewManifest(
            image_name="x",
            runtime_uid=1000,
            runtime_gid=1000,
            workdir="/workspace",
            exposed_ports=(),
            version_pins={},
            entrypoint="/bin/sh",
            default_cmd=("sh",),
            schema_version="1",
            raw={},
        )


def test_manifest_post_init_freezes_collections() -> None:
    manifest = load_image_manifest()
    # version_pins is exposed as a MappingProxyType — read-only.
    with pytest.raises(TypeError):
        manifest.version_pins["pnpm"] = "BAD"  # type: ignore[index]
    # exposed_ports is a tuple.
    assert isinstance(manifest.exposed_ports, tuple)
    # default_cmd is a tuple.
    assert isinstance(manifest.default_cmd, tuple)


# ── format_sandbox_id / format_container_name ───────────────────────


def test_format_sandbox_id_deterministic() -> None:
    assert format_sandbox_id("ws-42") == format_sandbox_id("ws-42")


def test_format_sandbox_id_different_inputs_differ() -> None:
    assert format_sandbox_id("a") != format_sandbox_id("b")


def test_format_sandbox_id_dns_label_safe() -> None:
    sid = format_sandbox_id("My-Workspace_42")
    # DNS labels: [a-z0-9-], <= 63 chars.
    import re

    assert re.fullmatch(r"[a-z0-9-]+", sid)
    assert len(sid) <= 63


def test_format_sandbox_id_rejects_empty() -> None:
    with pytest.raises(ValueError):
        format_sandbox_id("")
    with pytest.raises(ValueError):
        format_sandbox_id("   ")


def test_format_container_name_deterministic_and_capped() -> None:
    name = format_container_name("ws-42")
    assert name.startswith("omnisight-web-preview-")
    assert len(name) <= 63
    assert format_container_name("ws-42") == format_container_name("ws-42")


def test_format_container_name_distinct_for_distinct_workspaces() -> None:
    assert format_container_name("a") != format_container_name("b")


def test_format_container_name_with_long_workspace_id_still_capped() -> None:
    long_id = "x" * 200
    name = format_container_name(long_id)
    assert len(name) <= 63


# ── build_preview_url ───────────────────────────────────────────────


def test_build_preview_url_default() -> None:
    assert build_preview_url(41234) == "http://127.0.0.1:41234/"


def test_build_preview_url_custom_host() -> None:
    assert build_preview_url(41234, host="0.0.0.0") == "http://0.0.0.0:41234/"


def test_build_preview_url_normalises_path() -> None:
    assert build_preview_url(41234, path="api/health") == "http://127.0.0.1:41234/api/health"


def test_build_preview_url_rejects_bad_port() -> None:
    with pytest.raises(ValueError):
        build_preview_url(0)
    with pytest.raises(ValueError):
        build_preview_url(70000)


# ── validate_workspace_path ─────────────────────────────────────────


def test_validate_workspace_path_existing_dir(tmp_path: Path) -> None:
    resolved = validate_workspace_path(str(tmp_path))
    assert resolved == tmp_path


def test_validate_workspace_path_nonexistent(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        validate_workspace_path(str(tmp_path / "nope"))


def test_validate_workspace_path_relative_rejected() -> None:
    with pytest.raises(ValueError):
        validate_workspace_path("relative/path")


def test_validate_workspace_path_file_rejected(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hi")
    with pytest.raises(ValueError):
        validate_workspace_path(str(f))


def test_validate_workspace_path_empty_rejected() -> None:
    with pytest.raises(ValueError):
        validate_workspace_path("")
    with pytest.raises(ValueError):
        validate_workspace_path("   ")


# ── allocate_host_port ──────────────────────────────────────────────


def test_allocate_host_port_inside_range() -> None:
    port = allocate_host_port("ws-42")
    assert DEFAULT_HOST_PORT_RANGE[0] <= port <= DEFAULT_HOST_PORT_RANGE[1]


def test_allocate_host_port_deterministic() -> None:
    assert allocate_host_port("ws-42") == allocate_host_port("ws-42")


def test_allocate_host_port_avoids_in_use() -> None:
    first = allocate_host_port("ws-42")
    second = allocate_host_port("ws-42", in_use=[first])
    assert second != first


def test_allocate_host_port_invalid_range() -> None:
    with pytest.raises(ValueError):
        allocate_host_port("ws-42", port_range=(70000, 80000))


def test_allocate_host_port_full_range_raises() -> None:
    full = list(range(41000, 41006))
    with pytest.raises(WebSandboxError):
        allocate_host_port("ws-42", in_use=full, port_range=(41000, 41005))


# ── WebSandboxConfig validation ─────────────────────────────────────


def test_config_minimal_happy_path(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    assert cfg.workspace_id == "ws-42"
    assert cfg.workspace_path == str(tmp_path)
    assert cfg.image_tag == DEFAULT_IMAGE_TAG
    assert cfg.git_ref is None
    assert cfg.install_command == DEFAULT_INSTALL_COMMAND
    assert cfg.dev_command == DEFAULT_DEV_COMMAND
    assert cfg.container_port == DEFAULT_CONTAINER_PORT
    assert cfg.workdir == DEFAULT_WORKDIR


def test_config_to_dict_round_trip(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        env={"FOO": "BAR"},
    )
    payload = cfg.to_dict()
    assert payload["schema_version"] == WEB_SANDBOX_SCHEMA_VERSION
    assert payload["workspace_id"] == "ws-42"
    assert payload["env"] == {"FOO": "BAR"}
    assert payload["install_command"] == list(DEFAULT_INSTALL_COMMAND)


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "ws/42", "ws 42", "../etc", "ws$42", "x" * 129],
)
def test_config_rejects_bad_workspace_id(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(ValueError):
        WebSandboxConfig(workspace_id=bad_id, workspace_path=str(tmp_path))


def test_config_rejects_relative_workdir(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            workdir="relative",
        )


def test_config_rejects_bad_container_port(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            container_port=0,
        )
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            container_port=70000,
        )


def test_config_rejects_zero_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            startup_timeout_s=0,
        )
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            stop_timeout_s=-1,
        )


def test_config_rejects_empty_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            install_command=(),
        )
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            dev_command=(),
        )


def test_config_rejects_non_string_env(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            env={"FOO": 123},  # type: ignore[dict-item]
        )


def test_config_normalises_command_lists_to_tuples(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        install_command=["pnpm", "install"],  # list, not tuple
    )
    assert isinstance(cfg.install_command, tuple)


@pytest.mark.parametrize(
    "bad_ref",
    ["", "   ", "feature branch", "feature;rm -rf /", "$(echo x)", "../../etc"],
)
def test_config_rejects_bad_git_ref(tmp_path: Path, bad_ref: str) -> None:
    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            git_ref=bad_ref,
        )


def test_config_accepts_safe_git_ref(tmp_path: Path) -> None:
    for ref in ["main", "feature/foo", "v1.2.3", "release-2024-01"]:
        cfg = WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(tmp_path),
            git_ref=ref,
        )
        assert cfg.git_ref == ref


# ── WebSandboxInstance shape ────────────────────────────────────────


def test_instance_default_terminal_flags(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    inst = WebSandboxInstance(
        workspace_id="ws-42",
        sandbox_id="ws-abc",
        container_name="omnisight-web-preview-ws-abc",
        config=cfg,
        status=WebSandboxStatus.pending,
    )
    assert not inst.is_running
    assert not inst.is_terminal


def test_instance_running_flag(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    inst = WebSandboxInstance(
        workspace_id="ws-42",
        sandbox_id="ws-abc",
        container_name="omnisight-web-preview-ws-abc",
        config=cfg,
        status=WebSandboxStatus.running,
    )
    assert inst.is_running
    assert not inst.is_terminal


def test_instance_terminal_flags(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    for status in (WebSandboxStatus.stopped, WebSandboxStatus.failed):
        inst = WebSandboxInstance(
            workspace_id="ws-42",
            sandbox_id="ws-abc",
            container_name="omnisight-web-preview-ws-abc",
            config=cfg,
            status=status,
        )
        assert inst.is_terminal


def test_instance_idle_seconds(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    inst_fresh = WebSandboxInstance(
        workspace_id="ws-42",
        sandbox_id="ws-abc",
        container_name="omnisight-web-preview-ws-abc",
        config=cfg,
    )
    assert inst_fresh.idle_seconds(now=10000.0) == 0.0
    inst_active = WebSandboxInstance(
        workspace_id="ws-42",
        sandbox_id="ws-abc",
        container_name="omnisight-web-preview-ws-abc",
        config=cfg,
        last_request_at=9000.0,
    )
    assert inst_active.idle_seconds(now=10000.0) == 1000.0


def test_instance_to_dict_shape(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    inst = WebSandboxInstance(
        workspace_id="ws-42",
        sandbox_id="ws-abc",
        container_name="omnisight-web-preview-ws-abc",
        config=cfg,
    )
    payload = inst.to_dict()
    assert payload["schema_version"] == WEB_SANDBOX_SCHEMA_VERSION
    assert payload["workspace_id"] == "ws-42"
    assert payload["sandbox_id"] == "ws-abc"
    assert payload["status"] == "pending"
    assert isinstance(payload["config"], dict)


# ── build_install_argv / build_dev_argv / build_composite_command ───


def test_build_install_and_dev_argv(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    assert build_install_argv(cfg) == list(DEFAULT_INSTALL_COMMAND)
    assert build_dev_argv(cfg) == list(DEFAULT_DEV_COMMAND)


def test_build_composite_command_default(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    composite = build_composite_command(cfg)
    assert composite[:2] == ("sh", "-c")
    body = composite[2]
    assert "set -e" in body
    assert "pnpm install --frozen-lockfile" in body
    assert "pnpm dev --host 0.0.0.0" in body
    # Install must run *before* dev.
    assert body.index("pnpm install") < body.index("pnpm dev")
    # No git step when git_ref is None.
    assert "git fetch" not in body
    assert "git checkout" not in body


def test_build_composite_command_with_git_ref(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        git_ref="feature/foo",
    )
    composite = build_composite_command(cfg)
    body = composite[2]
    assert "git fetch --all --tags" in body
    # shlex.quote leaves shell-safe strings (only [A-Za-z0-9_./-]) bare;
    # the test verifies the ref reaches the checkout and is sequenced
    # before pnpm install — defence in depth on the regex validation.
    assert "git checkout feature/foo" in body
    assert body.index("git fetch") < body.index("pnpm install")
    assert body.index("git checkout") < body.index("pnpm install")


def test_build_composite_command_quotes_unsafe_git_ref_chars(tmp_path: Path) -> None:
    # ``shlex.quote`` only quotes when shell-special chars exist.  Refs
    # that contain ``.`` (e.g. ``v1.2.3``) carry a literal dot that
    # bash does not expand, so shlex leaves them bare. Either form is
    # safe — the regex validation already pre-filters meta-chars.
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        git_ref="v1.2.3",
    )
    body = build_composite_command(cfg)[2]
    assert "v1.2.3" in body


# ── build_docker_run_spec ──────────────────────────────────────────


def test_build_docker_run_spec_deterministic(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        host_port=41234,
    )
    spec_a = build_docker_run_spec(cfg)
    spec_b = build_docker_run_spec(cfg)
    assert spec_a == spec_b


def test_build_docker_run_spec_no_host_port(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    spec = build_docker_run_spec(cfg)
    assert spec["ports"] == {}


def test_build_docker_run_spec_with_host_port(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        host_port=41234,
    )
    spec = build_docker_run_spec(cfg)
    assert spec["ports"] == {41234: 5173}


def test_build_docker_run_spec_bind_mount(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    spec = build_docker_run_spec(cfg)
    mount = spec["mounts"][0]
    assert mount["source"] == str(tmp_path)
    assert mount["target"] == "/workspace"
    assert mount["read_only"] is False


def test_build_docker_run_spec_env_defaults(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    spec = build_docker_run_spec(cfg)
    env = spec["env"]
    assert env["HOST"] == "0.0.0.0"
    assert env["PORT"] == "5173"
    assert env["NODE_ENV"] == "development"


def test_build_docker_run_spec_user_env_takes_precedence(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        env={"NODE_ENV": "test"},
    )
    spec = build_docker_run_spec(cfg)
    assert spec["env"]["NODE_ENV"] == "test"


def test_build_docker_run_spec_image_tag(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        image_tag="omnisight-web-preview:custom",
    )
    spec = build_docker_run_spec(cfg)
    assert spec["image"] == "omnisight-web-preview:custom"


def test_build_docker_run_spec_command_includes_install_and_dev(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    spec = build_docker_run_spec(cfg)
    assert spec["command"][:2] == ["sh", "-c"]
    body = spec["command"][2]
    assert "pnpm install" in body
    assert "pnpm dev" in body


def test_build_docker_run_spec_manifest_workdir_drift_raises(tmp_path: Path) -> None:
    manifest = load_image_manifest()
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        workdir="/elsewhere",
    )
    with pytest.raises(WebSandboxError) as exc:
        build_docker_run_spec(cfg, manifest)
    assert "workdir" in str(exc.value)


def test_build_docker_run_spec_manifest_port_drift_raises(tmp_path: Path) -> None:
    manifest = load_image_manifest()
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        container_port=9999,
    )
    with pytest.raises(WebSandboxError) as exc:
        build_docker_run_spec(cfg, manifest)
    assert "container_port" in str(exc.value)


def test_build_docker_run_spec_manifest_happy_path(tmp_path: Path) -> None:
    manifest = load_image_manifest()
    # 3000 is valid because manifest exposes both 3000 and 5173.
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(tmp_path),
        container_port=3000,
    )
    spec = build_docker_run_spec(cfg, manifest)
    assert spec["env"]["PORT"] == "3000"


def test_build_docker_run_spec_rejects_non_config() -> None:
    with pytest.raises(TypeError):
        build_docker_run_spec({"workspace_id": "ws-42"})  # type: ignore[arg-type]


def test_build_docker_run_spec_rejects_non_manifest(tmp_path: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(tmp_path))
    with pytest.raises(TypeError):
        build_docker_run_spec(cfg, {"image_name": "x"})  # type: ignore[arg-type]


# ── detect_dev_server_ready ────────────────────────────────────────


@pytest.mark.parametrize(
    "log",
    [
        "VITE v5.4.10  ready in 432 ms",
        "  ➜  Local:   http://localhost:5173/",
        "Listening on http://0.0.0.0:5173",
        "compiled successfully",
    ],
)
def test_detect_dev_server_ready_positive(log: str) -> None:
    assert detect_dev_server_ready(log)


def test_detect_dev_server_ready_negative() -> None:
    assert not detect_dev_server_ready("warming up...")
    assert not detect_dev_server_ready("")


# ── FakeDockerClient + FakeClock + RecordingEventCallback ──────────


class FakeDockerClient:
    """In-memory DockerClient for W14.2 tests.

    Tracks every call so tests can assert exact argv shape, and lets
    the test pin ``run_error`` / ``stop_error`` to drive the failure
    paths. Mirrors the V2 ``ui_sandbox`` fixture so callers familiar
    with one are immediately at home with the other.
    """

    def __init__(
        self,
        *,
        run_error: Exception | None = None,
        stop_error: Exception | None = None,
        remove_error: Exception | None = None,
        canned_logs: str = "",
        inspect_payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.run_error = run_error
        self.stop_error = stop_error
        self.remove_error = remove_error
        self.canned_logs = canned_logs
        self.inspect_payload = inspect_payload
        self.run_calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []
        self.remove_calls: list[dict[str, Any]] = []
        self.inspect_calls: list[str] = []
        self._next_id = 0
        self._lock = threading.Lock()

    def run_detached(
        self,
        *,
        image: str,
        name: str,
        command: Sequence[str],
        mounts: Sequence[Mapping[str, str]],
        ports: Mapping[int, int],
        env: Mapping[str, str],
        workdir: str,
        # W14.9 — accept resource-limit kwargs the manager threads
        # through. Defaults match the DockerClient Protocol.
        memory_limit_bytes: int | None = None,
        cpu_limit: float | None = None,
        storage_limit_bytes: int | None = None,
        memory_swap_disabled: bool = True,
    ) -> str:
        if self.run_error is not None:
            raise self.run_error
        with self._lock:
            self._next_id += 1
            cid = f"fake-cid-{self._next_id:04d}"
        self.run_calls.append(
            {
                "image": image,
                "name": name,
                "command": list(command),
                "mounts": [dict(m) for m in mounts],
                "ports": dict(ports),
                "env": dict(env),
                "workdir": workdir,
                "memory_limit_bytes": memory_limit_bytes,
                "cpu_limit": cpu_limit,
                "storage_limit_bytes": storage_limit_bytes,
                "memory_swap_disabled": memory_swap_disabled,
                "container_id": cid,
            }
        )
        return cid

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        self.stop_calls.append({"container_id": container_id, "timeout_s": timeout_s})
        if self.stop_error is not None:
            raise self.stop_error

    def remove(self, container_id: str, *, force: bool = False) -> None:
        self.remove_calls.append({"container_id": container_id, "force": force})
        if self.remove_error is not None:
            raise self.remove_error

    def logs(self, container_id: str, *, tail: int | None = None) -> str:
        return self.canned_logs

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        self.inspect_calls.append(container_id)
        if self.inspect_payload is not None:
            return self.inspect_payload
        return {"Id": "recovered-cid", "State": {"Running": True}}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        v = self._t
        self._t += 1.0
        return v


class RecordingEventCallback:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))


# ── WebSandboxManager lifecycle ────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A throwaway workspace dir for tests."""

    return tmp_path


def _make_manager(
    workspace: Path,
    *,
    docker: FakeDockerClient | None = None,
    clock: FakeClock | None = None,
    events: RecordingEventCallback | None = None,
    manifest: WebPreviewManifest | None = None,
) -> tuple[WebSandboxManager, FakeDockerClient, FakeClock, RecordingEventCallback]:
    docker = docker or FakeDockerClient()
    clock = clock or FakeClock()
    events = events or RecordingEventCallback()
    mgr = WebSandboxManager(
        docker_client=docker,
        manifest=manifest,
        clock=clock,
        event_cb=events,
    )
    return mgr, docker, clock, events


def test_manager_launch_happy_path(workspace: Path) -> None:
    mgr, docker, clock, events = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    assert inst.status == WebSandboxStatus.installing
    assert inst.container_id == "fake-cid-0001"
    assert inst.host_port is not None
    assert inst.preview_url is not None
    assert inst.preview_url.endswith(f":{inst.host_port}/")
    assert len(docker.run_calls) == 1
    assert docker.run_calls[0]["image"] == DEFAULT_IMAGE_TAG
    # Event emitted on success.
    types = [t for t, _ in events.events]
    assert "web_sandbox.launched" in types


def test_manager_launch_idempotent_returns_existing(workspace: Path) -> None:
    mgr, docker, clock, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    first = mgr.launch(cfg)
    second = mgr.launch(cfg)
    assert first.workspace_id == second.workspace_id
    assert first.sandbox_id == second.sandbox_id
    # Docker only ran once — second call was a no-op idempotent return.
    assert len(docker.run_calls) == 1
    # last_request_at bumped on the second call (clock ticked).
    assert second.last_request_at >= first.last_request_at


def test_manager_launch_non_idempotent_raises_already_exists(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    with pytest.raises(WebSandboxAlreadyExists):
        mgr.launch(cfg, idempotent=False)


def test_manager_launch_invalid_workspace_path_raises(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path="/does/not/exist/anywhere",
    )
    with pytest.raises(ValueError):
        mgr.launch(cfg)


def test_manager_launch_docker_failure_marks_failed(workspace: Path) -> None:
    docker = FakeDockerClient(run_error=RuntimeError("daemon dead"))
    mgr, _, _, events = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    assert inst.status == WebSandboxStatus.failed
    assert "daemon dead" in (inst.error or "")
    types = [t for t, _ in events.events]
    assert "web_sandbox.failed" in types


def test_manager_launch_name_conflict_recovers_via_inspect(workspace: Path) -> None:
    err = RuntimeError(
        "Error response from daemon: Conflict. The container name "
        '"omnisight-web-preview-ws-xx" is already in use'
    )
    docker = FakeDockerClient(
        run_error=err, inspect_payload={"Id": "recovered-cid-9999"}
    )
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    assert inst.container_id == "recovered-cid-9999"
    assert inst.status == WebSandboxStatus.installing


def test_manager_launch_name_conflict_inspect_empty_marks_failed(workspace: Path) -> None:
    err = RuntimeError(
        "Conflict. The container name x is already in use"
    )
    docker = FakeDockerClient(run_error=err, inspect_payload={})
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    assert inst.status == WebSandboxStatus.failed
    assert "name_conflict_unrecoverable" in (inst.error or "")


def test_manager_mark_ready_transitions(workspace: Path) -> None:
    mgr, _, _, events = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    inst = mgr.mark_ready("ws-42")
    assert inst.status == WebSandboxStatus.running
    assert inst.ready_at is not None
    types = [t for t, _ in events.events]
    assert "web_sandbox.ready" in types


def test_manager_mark_ready_idempotent(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    a = mgr.mark_ready("ws-42")
    b = mgr.mark_ready("ws-42")
    assert a.ready_at == b.ready_at  # second call is no-op


def test_manager_mark_ready_unknown_raises(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    with pytest.raises(WebSandboxNotFound):
        mgr.mark_ready("nope")


def test_manager_mark_ready_terminal_raises(workspace: Path) -> None:
    docker = FakeDockerClient(run_error=RuntimeError("daemon dead"))
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)  # marks failed
    with pytest.raises(WebSandboxError):
        mgr.mark_ready("ws-42")


def test_manager_touch_bumps_last_request_at(workspace: Path) -> None:
    mgr, _, clock, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    before = mgr.get("ws-42").last_request_at
    after = mgr.touch("ws-42").last_request_at
    assert after > before


def test_manager_touch_unknown_raises(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    with pytest.raises(WebSandboxNotFound):
        mgr.touch("nope")


def test_manager_touch_terminal_no_bump(workspace: Path) -> None:
    docker = FakeDockerClient(run_error=RuntimeError("dead"))
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    failed = mgr.launch(cfg)
    touched = mgr.touch("ws-42")
    assert touched.last_request_at == failed.last_request_at


def test_manager_stop_runs_docker_stop_and_remove(workspace: Path) -> None:
    mgr, docker, _, events = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    stopped = mgr.stop("ws-42")
    assert stopped.status == WebSandboxStatus.stopped
    assert stopped.stopped_at is not None
    assert len(docker.stop_calls) == 1
    assert len(docker.remove_calls) == 1
    types = [t for t, _ in events.events]
    assert "web_sandbox.stopped" in types


def test_manager_stop_records_reason(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    stopped = mgr.stop("ws-42", reason="idle_timeout")
    assert stopped.killed_reason == "idle_timeout"


def test_manager_stop_captures_docker_errors_as_warnings(workspace: Path) -> None:
    docker = FakeDockerClient(stop_error=RuntimeError("boom"))
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    stopped = mgr.stop("ws-42")
    assert stopped.status == WebSandboxStatus.stopped
    assert any("stop_failed" in w for w in stopped.warnings)


def test_manager_stop_idempotent_on_terminal(workspace: Path) -> None:
    mgr, docker, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    mgr.stop("ws-42")
    initial_stop_calls = len(docker.stop_calls)
    mgr.stop("ws-42")
    # second stop is no-op
    assert len(docker.stop_calls) == initial_stop_calls


def test_manager_remove_terminal_only(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    with pytest.raises(WebSandboxError):
        mgr.remove("ws-42")
    mgr.stop("ws-42")
    final = mgr.remove("ws-42")
    assert final.status == WebSandboxStatus.stopped
    assert mgr.get("ws-42") is None


def test_manager_remove_unknown_raises(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    with pytest.raises(WebSandboxNotFound):
        mgr.remove("nope")


def test_manager_get_returns_none_for_unknown(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    assert mgr.get("nope") is None


def test_manager_list_and_snapshot(workspace: Path) -> None:
    mgr, _, _, _ = _make_manager(workspace)
    cfg_a = WebSandboxConfig(workspace_id="ws-a", workspace_path=str(workspace))
    cfg_b = WebSandboxConfig(workspace_id="ws-b", workspace_path=str(workspace))
    mgr.launch(cfg_a)
    mgr.launch(cfg_b)
    items = mgr.list()
    assert len(items) == 2
    snap = mgr.snapshot()
    assert snap["count"] == 2
    assert snap["schema_version"] == WEB_SANDBOX_SCHEMA_VERSION
    assert len(snap["sandboxes"]) == 2


def test_manager_logs(workspace: Path) -> None:
    docker = FakeDockerClient(canned_logs="VITE v5.4.10 ready in 432 ms\n")
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    assert "ready in 432 ms" in mgr.logs("ws-42")


def test_manager_logs_capped(workspace: Path) -> None:
    huge = "x" * (MAX_LOG_CHARS + 5_000)
    docker = FakeDockerClient(canned_logs=huge)
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    assert len(mgr.logs("ws-42")) == MAX_LOG_CHARS


def test_manager_poll_ready_uses_logs(workspace: Path) -> None:
    docker = FakeDockerClient(canned_logs="  ➜  Local:   http://localhost:5173/")
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    assert mgr.poll_ready("ws-42")


def test_manager_manifest_property(workspace: Path) -> None:
    manifest = load_image_manifest()
    mgr, _, _, _ = _make_manager(workspace, manifest=manifest)
    assert mgr.manifest is manifest


def test_manager_event_callback_does_not_kill_on_error(workspace: Path) -> None:
    events = RecordingEventCallback()

    def bad_cb(event_type: str, payload: Mapping[str, Any]) -> None:
        raise RuntimeError("event boom")

    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        event_cb=bad_cb,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    assert inst.status == WebSandboxStatus.installing


# ── Concurrency: 16 workers, 16 distinct workspaces, no corruption ──


def test_manager_concurrent_launches(workspace: Path) -> None:
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        event_cb=None,
    )
    workers = 16
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def go(idx: int) -> None:
        try:
            cfg = WebSandboxConfig(
                workspace_id=f"ws-{idx:02d}",
                workspace_path=str(workspace),
            )
            barrier.wait()
            mgr.launch(cfg)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(mgr.list()) == workers
    # Distinct sandbox_ids and host_ports.
    sandbox_ids = {inst.sandbox_id for inst in mgr.list()}
    host_ports = {inst.host_port for inst in mgr.list()}
    assert len(sandbox_ids) == workers
    assert len(host_ports) == workers


# ── Cross-worker contract — deterministic naming ───────────────────


def test_format_container_name_recoverable_across_workers() -> None:
    """Two workers computing format_container_name(ws_id) must agree —
    that's the cross-worker recovery contract for SOP §1."""

    workers_results = [format_container_name("ws-42") for _ in range(8)]
    assert len(set(workers_results)) == 1


# ── W14.3 — CFIngressManager integration ───────────────────────────


from backend.cf_ingress import (  # noqa: E402  — integration, not module surface
    CFIngressAPIError,
    CFIngressConfig,
    CFIngressManager,
)
from backend.tests.test_cf_ingress import (  # noqa: E402
    FakeCFIngressClient,
    _ok_config_kwargs,
)


def _make_cf_manager(
    *, ingress: list | None = None
) -> tuple[CFIngressManager, FakeCFIngressClient]:
    config = CFIngressConfig(**_ok_config_kwargs())
    fake = FakeCFIngressClient(ingress=ingress)
    return CFIngressManager(config=config, client=fake), fake


def test_w14_3_launch_creates_cf_ingress_rule(workspace: Path) -> None:
    """When CFIngressManager is wired in, launch creates the ingress
    rule and pins ``ingress_url`` on the instance."""

    cf_mgr, cf_fake = _make_cf_manager()
    docker = FakeDockerClient()
    clock = FakeClock()
    events = RecordingEventCallback()
    mgr = WebSandboxManager(
        docker_client=docker,
        clock=clock,
        event_cb=events,
        cf_ingress_manager=cf_mgr,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)

    assert inst.ingress_url is not None
    assert inst.ingress_url == f"https://preview-{inst.sandbox_id}.ai.sora-dev.app"
    assert inst.preview_url is not None  # still set
    # CF API was called.
    assert cf_fake.gets >= 1
    assert len(cf_fake.puts) == 1
    # The launched event payload carries ingress_url.
    launched = next(p for t, p in events.events if t == "web_sandbox.launched")
    assert launched["ingress_url"] == inst.ingress_url


def test_w14_3_launch_without_cf_manager_keeps_ingress_url_none(
    workspace: Path,
) -> None:
    """Default W14.2 path — no CF wiring, ingress_url stays None."""

    mgr, _, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    assert inst.ingress_url is None


def test_w14_3_launch_cf_failure_falls_through_with_warning(workspace: Path) -> None:
    """A CF API outage during launch must NOT fail the launch — the
    operator's local-host preview still works, and the failure is
    surfaced as a per-instance warning."""

    cf_mgr, cf_fake = _make_cf_manager()
    cf_fake.raise_on_get = CFIngressAPIError("flaky CF", status=502)
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_mgr,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    # Launch succeeded.
    assert inst.status == WebSandboxStatus.installing
    assert inst.preview_url is not None
    # ingress_url stays None.
    assert inst.ingress_url is None
    # Warning recorded.
    joined = " | ".join(inst.warnings)
    assert "cf_ingress_create_failed" in joined
    assert "flaky CF" in joined


def test_w14_3_stop_removes_cf_ingress_rule(workspace: Path) -> None:
    cf_mgr, cf_fake = _make_cf_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_mgr,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    launched = mgr.launch(cfg)
    assert launched.ingress_url is not None
    cf_fake.puts.clear()

    mgr.stop("ws-42")
    # The stop call removed the rule.
    assert len(cf_fake.puts) == 1
    # No more rules with the preview hostname.
    rules = cf_fake.current_ingress()
    target = f"preview-{launched.sandbox_id}.ai.sora-dev.app"
    assert all(r.get("hostname") != target for r in rules)


def test_w14_3_stop_skips_cf_when_ingress_url_none(workspace: Path) -> None:
    """If launch never set ingress_url (CF was down at launch time),
    stop should skip the CF round-trip entirely."""

    cf_mgr, cf_fake = _make_cf_manager()
    cf_fake.raise_on_get = CFIngressAPIError("CF down", status=502)
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_mgr,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    # Now CF is back up — but we never set ingress_url so stop should
    # not even attempt the delete.
    cf_fake.raise_on_get = None
    cf_fake.gets = 0
    cf_fake.puts.clear()
    mgr.stop("ws-42")
    # No CF round-trip on stop because ingress_url was None.
    assert cf_fake.gets == 0
    assert cf_fake.puts == []


def test_w14_3_stop_cf_failure_records_warning(workspace: Path) -> None:
    cf_mgr, cf_fake = _make_cf_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_mgr,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    cf_fake.raise_on_get = CFIngressAPIError("CF flaky on stop", status=502)
    stopped = mgr.stop("ws-42")
    # Local stop succeeded.
    assert stopped.status == WebSandboxStatus.stopped
    # Warning recorded.
    joined = " | ".join(stopped.warnings)
    assert "cf_ingress_delete_failed" in joined


def test_w14_3_idempotent_relaunch_keeps_ingress_url(workspace: Path) -> None:
    """Idempotent re-launch returns the cached instance — ingress_url
    must come along for the ride."""

    cf_mgr, _ = _make_cf_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_mgr,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    a = mgr.launch(cfg)
    b = mgr.launch(cfg)  # idempotent
    assert a.ingress_url is not None
    assert b.ingress_url == a.ingress_url


def test_w14_3_constructor_accepts_optional_cf_manager() -> None:
    """The constructor's ``cf_ingress_manager`` is keyword-only and
    defaults to None — drift guard for the W14.2 backward-compat
    contract."""

    import inspect

    sig = inspect.signature(WebSandboxManager.__init__)
    assert "cf_ingress_manager" in sig.parameters
    param = sig.parameters["cf_ingress_manager"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_w14_3_to_dict_carries_ingress_url(workspace: Path) -> None:
    cf_mgr, _ = _make_cf_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_mgr,
    )
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    inst = mgr.launch(cfg)
    d = inst.to_dict()
    assert d["ingress_url"] == inst.ingress_url
    assert d["ingress_url"].startswith("https://preview-ws-")


# ── W14.4 — CFAccessManager integration ────────────────────────────


from backend.cf_access import (  # noqa: E402  — integration, not module surface
    CFAccessAPIError,
    CFAccessConfig,
    CFAccessManager,
)
from backend.tests.test_cf_access import (  # noqa: E402
    FakeCFAccessClient,
    _ok_config_kwargs as _ok_access_config_kwargs,
)


def _make_access_manager(
    *, default_emails: tuple[str, ...] = (), apps: list | None = None
) -> tuple[CFAccessManager, FakeCFAccessClient]:
    config = CFAccessConfig(
        **_ok_access_config_kwargs(), default_emails=default_emails
    )
    fake = FakeCFAccessClient(apps=apps)
    return CFAccessManager(config=config, client=fake), fake


def test_w14_4_launch_creates_cf_access_app(workspace: Path) -> None:
    """When CFAccessManager is wired in and the config carries
    ``allowed_emails``, launch creates an Access app and pins the app
    id onto the instance."""

    access_mgr, access_fake = _make_access_manager()
    docker = FakeDockerClient()
    clock = FakeClock()
    events = RecordingEventCallback()
    mgr = WebSandboxManager(
        docker_client=docker,
        clock=clock,
        event_cb=events,
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)

    assert inst.access_app_id is not None
    assert inst.access_app_id.startswith("app-")
    assert len(access_fake.create_calls) == 1
    body = access_fake.create_calls[0]
    assert body["name"] == f"omnisight-preview-{inst.sandbox_id}"
    assert body["domain"] == f"preview-{inst.sandbox_id}.ai.sora-dev.app"
    # Policy carries the operator's email.
    include = body["policies"][0]["include"]
    assert {"email": {"email": "op@example.com"}} in include
    # Event payload carries access_app_id.
    launched = next(p for t, p in events.events if t == "web_sandbox.launched")
    assert launched["access_app_id"] == inst.access_app_id


def test_w14_4_launch_unions_default_emails(workspace: Path) -> None:
    """The Access policy's email list = config.allowed_emails ∪
    cf_access_default_emails."""

    access_mgr, access_fake = _make_access_manager(
        default_emails=("admin@example.com",)
    )
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)
    assert inst.access_app_id is not None
    body = access_fake.create_calls[0]
    include = body["policies"][0]["include"]
    addrs = {entry["email"]["email"] for entry in include}
    assert addrs == {"op@example.com", "admin@example.com"}


def test_w14_4_launch_without_cf_access_manager_keeps_app_id_none(
    workspace: Path,
) -> None:
    """Default W14.3 path — no Access wiring, access_app_id stays None."""

    mgr, _, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)
    assert inst.access_app_id is None


def test_w14_4_launch_skipped_when_no_emails(workspace: Path) -> None:
    """When neither allowed_emails nor default_emails are set, launch
    skips the Access app create and surfaces a per-instance warning —
    posting an empty-include policy would lock everyone out."""

    access_mgr, access_fake = _make_access_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=(),
    )
    inst = mgr.launch(cfg)
    # No CF Access call.
    assert access_fake.create_calls == []
    # access_app_id stays None.
    assert inst.access_app_id is None
    # Warning surfaced.
    joined = " | ".join(inst.warnings)
    assert "cf_access_skipped" in joined


def test_w14_4_launch_cf_access_failure_falls_through_with_warning(
    workspace: Path,
) -> None:
    """A CF Access API outage during launch must NOT fail the launch —
    the operator's local-host preview still works, and the failure is
    surfaced as a per-instance warning. The W14.3 ingress_url stays as
    a publicly-reachable (unauthenticated) URL until the operator can
    fix the CF Access wiring; that's the documented best-effort fold."""

    access_mgr, access_fake = _make_access_manager()
    access_fake.raise_on_create = CFAccessAPIError("flaky CF Access", status=502)
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)
    # Launch succeeded.
    assert inst.status == WebSandboxStatus.installing
    assert inst.preview_url is not None
    # access_app_id stays None.
    assert inst.access_app_id is None
    # Warning recorded.
    joined = " | ".join(inst.warnings)
    assert "cf_access_create_failed" in joined
    assert "flaky CF Access" in joined


def test_w14_4_stop_deletes_cf_access_app(workspace: Path) -> None:
    access_mgr, access_fake = _make_access_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    launched = mgr.launch(cfg)
    assert launched.access_app_id is not None
    access_fake.delete_calls.clear()

    mgr.stop("ws-42")
    # Access app was deleted.
    assert len(access_fake.delete_calls) == 1


def test_w14_4_stop_skips_cf_access_when_app_id_none(workspace: Path) -> None:
    """If launch never set access_app_id (CF Access was down at launch
    time, or no emails), stop should skip the CF Access round-trip
    entirely."""

    access_mgr, access_fake = _make_access_manager()
    access_fake.raise_on_create = CFAccessAPIError("CF down", status=502)
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    access_fake.raise_on_create = None
    access_fake.list_calls = 0
    access_fake.delete_calls.clear()
    mgr.stop("ws-42")
    # No CF Access round-trip on stop because access_app_id was None.
    assert access_fake.delete_calls == []


def test_w14_4_stop_cf_access_failure_records_warning(workspace: Path) -> None:
    access_mgr, access_fake = _make_access_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    access_fake.raise_on_delete = CFAccessAPIError("CF flaky on stop", status=502)
    stopped = mgr.stop("ws-42")
    # Local stop succeeded.
    assert stopped.status == WebSandboxStatus.stopped
    # Warning recorded.
    joined = " | ".join(stopped.warnings)
    assert "cf_access_delete_failed" in joined


def test_w14_4_constructor_accepts_optional_cf_access_manager() -> None:
    """The constructor's ``cf_access_manager`` is keyword-only and
    defaults to None — drift guard for the W14.3 backward-compat
    contract."""

    import inspect

    sig = inspect.signature(WebSandboxManager.__init__)
    assert "cf_access_manager" in sig.parameters
    param = sig.parameters["cf_access_manager"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_w14_4_to_dict_carries_access_app_id(workspace: Path) -> None:
    access_mgr, _ = _make_access_manager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_access_manager=access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)
    d = inst.to_dict()
    assert d["access_app_id"] == inst.access_app_id
    assert d["access_app_id"].startswith("app-")


def test_w14_4_config_normalises_allowed_emails_list_to_tuple() -> None:
    """WebSandboxConfig.allowed_emails accepts list defensively (router
    builds a list from request body) and freezes to a tuple."""

    from pathlib import Path as _P

    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(_P("/tmp")),
        allowed_emails=["op@example.com", "admin@example.com"],
    )
    assert isinstance(cfg.allowed_emails, tuple)
    assert cfg.allowed_emails == ("op@example.com", "admin@example.com")


def test_w14_4_config_rejects_non_string_email_entry() -> None:
    from pathlib import Path as _P

    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(_P("/tmp")),
            allowed_emails=(123,),  # type: ignore[arg-type]
        )


def test_w14_4_config_rejects_csv_string_in_allowed_emails() -> None:
    from pathlib import Path as _P

    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(_P("/tmp")),
            allowed_emails="op@example.com,admin@example.com",  # type: ignore[arg-type]
        )


def test_w14_4_config_strips_whitespace_emails() -> None:
    from pathlib import Path as _P

    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(_P("/tmp")),
        allowed_emails=(" op@example.com ", "", "admin@example.com"),
    )
    assert cfg.allowed_emails == ("op@example.com", "admin@example.com")


def test_w14_4_config_to_dict_carries_allowed_emails() -> None:
    from pathlib import Path as _P

    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(_P("/tmp")),
        allowed_emails=("op@example.com",),
    )
    d = cfg.to_dict()
    assert d["allowed_emails"] == ["op@example.com"]


# ── W14.9 — Resource limit cgroup integration ──────────────────────


from backend.web_sandbox import DEFAULT_RESOURCE_LIMITS  # noqa: E402
from backend.web_sandbox_resource_limits import (  # noqa: E402
    CGROUP_OOM_REASON,
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEMORY_LIMIT_BYTES,
    DEFAULT_STORAGE_LIMIT_BYTES,
    WebPreviewResourceLimits,
)


def test_w14_9_default_resource_limits_is_row_spec() -> None:
    """W14.9 row spec literals: 2 GiB / 1 CPU / 5 GiB."""

    assert DEFAULT_RESOURCE_LIMITS.memory_limit_bytes == 2 * 1024**3
    assert DEFAULT_RESOURCE_LIMITS.cpu_limit == 1.0
    assert DEFAULT_RESOURCE_LIMITS.storage_limit_bytes == 5 * 1024**3


def test_w14_9_manager_default_resource_limits_property() -> None:
    """Manager exposes its resource_limits via property — the W14.10
    audit row reads this to record the cgroup contract per-launch."""

    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
    )
    assert mgr.resource_limits == DEFAULT_RESOURCE_LIMITS


def test_w14_9_manager_accepts_resource_limits_override() -> None:
    custom = WebPreviewResourceLimits(
        memory_limit_bytes=4 * 1024**3,
        cpu_limit=2.0,
        storage_limit_bytes=10 * 1024**3,
    )
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        resource_limits=custom,
    )
    assert mgr.resource_limits is custom


def test_w14_9_manager_constructor_resource_limits_is_keyword_only() -> None:
    """Drift guard — resource_limits is keyword-only with default
    ``None`` so existing callers remain backward-compatible."""

    import inspect

    sig = inspect.signature(WebSandboxManager.__init__)
    assert "resource_limits" in sig.parameters
    param = sig.parameters["resource_limits"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_w14_9_manager_rejects_non_limits_type() -> None:
    with pytest.raises(TypeError):
        WebSandboxManager(
            docker_client=FakeDockerClient(),
            manifest=None,
            clock=FakeClock(),
            resource_limits={"memory": 1},  # type: ignore[arg-type]
        )


def test_w14_9_launch_passes_default_limits_to_run_detached(workspace: Path) -> None:
    mgr, docker, _, _ = _make_manager(workspace)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    call = docker.run_calls[0]
    assert call["memory_limit_bytes"] == DEFAULT_MEMORY_LIMIT_BYTES
    assert call["cpu_limit"] == DEFAULT_CPU_LIMIT
    assert call["storage_limit_bytes"] == DEFAULT_STORAGE_LIMIT_BYTES
    assert call["memory_swap_disabled"] is True


def test_w14_9_launch_passes_manager_override(workspace: Path) -> None:
    custom = WebPreviewResourceLimits(
        memory_limit_bytes=4 * 1024**3,
        cpu_limit=2.0,
        storage_limit_bytes=None,  # disabled disk cap
        memory_swap_disabled=False,
    )
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        resource_limits=custom,
    )
    docker = mgr._docker  # type: ignore[attr-defined]
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    call = docker.run_calls[0]  # type: ignore[attr-defined]
    assert call["memory_limit_bytes"] == 4 * 1024**3
    assert call["cpu_limit"] == 2.0
    assert call["storage_limit_bytes"] is None
    assert call["memory_swap_disabled"] is False


def test_w14_9_per_launch_override_beats_manager_policy(workspace: Path) -> None:
    """When config carries resource_limits, it supersedes the manager-
    wide policy (operator-on-call can pin a one-off larger sandbox
    without rewiring the manager)."""

    mgr, docker, _, _ = _make_manager(workspace)  # default 2g/1/5g
    one_shot = WebPreviewResourceLimits(
        memory_limit_bytes=8 * 1024**3,
        cpu_limit=4.0,
        storage_limit_bytes=20 * 1024**3,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-big",
        workspace_path=str(workspace),
        resource_limits=one_shot,
    )
    mgr.launch(cfg)
    call = docker.run_calls[0]
    assert call["memory_limit_bytes"] == 8 * 1024**3
    assert call["cpu_limit"] == 4.0
    assert call["storage_limit_bytes"] == 20 * 1024**3


def test_w14_9_config_resource_limits_is_keyword_only_default_none() -> None:
    import inspect

    sig = inspect.signature(WebSandboxConfig.__init__)
    assert "resource_limits" in sig.parameters
    assert sig.parameters["resource_limits"].default is None


def test_w14_9_config_rejects_non_limits_type() -> None:
    from pathlib import Path as _P

    with pytest.raises(ValueError):
        WebSandboxConfig(
            workspace_id="ws-42",
            workspace_path=str(_P("/tmp")),
            resource_limits={"memory": 1},  # type: ignore[arg-type]
        )


def test_w14_9_config_to_dict_carries_resource_limits() -> None:
    from pathlib import Path as _P

    custom = WebPreviewResourceLimits(memory_limit_bytes=4 * 1024**3)
    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(_P("/tmp")),
        resource_limits=custom,
    )
    d = cfg.to_dict()
    assert d["resource_limits"]["memory_limit_bytes"] == 4 * 1024**3


def test_w14_9_config_to_dict_resource_limits_is_none_by_default() -> None:
    from pathlib import Path as _P

    cfg = WebSandboxConfig(
        workspace_id="ws-42",
        workspace_path=str(_P("/tmp")),
    )
    assert cfg.to_dict()["resource_limits"] is None


def test_w14_9_build_docker_run_spec_includes_limit_keys(workspace: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    spec = ws.build_docker_run_spec(cfg, resource_limits=DEFAULT_RESOURCE_LIMITS)
    assert spec["memory_limit_bytes"] == DEFAULT_MEMORY_LIMIT_BYTES
    assert spec["cpu_limit"] == DEFAULT_CPU_LIMIT
    assert spec["storage_limit_bytes"] == DEFAULT_STORAGE_LIMIT_BYTES
    assert spec["memory_swap_disabled"] is True
    assert "resource_limits" in spec


def test_w14_9_build_docker_run_spec_omits_limit_keys_when_none(
    workspace: Path,
) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    spec = ws.build_docker_run_spec(cfg, resource_limits=None)
    assert "memory_limit_bytes" not in spec
    assert "cpu_limit" not in spec
    assert "storage_limit_bytes" not in spec


def test_w14_9_build_docker_run_spec_rejects_non_limits_type(workspace: Path) -> None:
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    with pytest.raises(TypeError):
        ws.build_docker_run_spec(cfg, resource_limits={"memory": 1})  # type: ignore[arg-type]


def test_w14_9_stop_detects_oom_kill_overrides_reason(workspace: Path) -> None:
    """When docker.inspect reports OOMKilled=true, stop() records
    killed_reason='cgroup_oom' regardless of the caller's reason —
    the kernel verdict is canonical."""

    docker = FakeDockerClient(
        inspect_payload={"Id": "fake-cid-0001", "State": {"OOMKilled": True}}
    )
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    stopped = mgr.stop("ws-42", reason="idle_timeout")
    assert stopped.killed_reason == CGROUP_OOM_REASON
    joined = " | ".join(stopped.warnings)
    assert "cgroup_oom_detected" in joined


def test_w14_9_stop_no_oom_keeps_caller_reason(workspace: Path) -> None:
    """Default inspect payload (OOMKilled missing) ⇒ caller's reason
    survives — backward compat with W14.5 idle reaper."""

    mgr, _, _, _ = _make_manager(workspace)  # default inspect: no OOMKilled
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    stopped = mgr.stop("ws-42", reason="idle_timeout")
    assert stopped.killed_reason == "idle_timeout"


def test_w14_9_stop_oom_with_no_caller_reason_still_records_oom(
    workspace: Path,
) -> None:
    docker = FakeDockerClient(
        inspect_payload={"Id": "fake-cid-0001", "State": {"OOMKilled": True}}
    )
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    stopped = mgr.stop("ws-42")  # no reason
    assert stopped.killed_reason == CGROUP_OOM_REASON


def test_w14_9_stop_oom_inspect_failure_falls_through_silently(
    workspace: Path,
) -> None:
    """Inspect raising during OOM check must NOT block the local stop —
    a transient docker daemon hiccup mid-stop is more likely than an
    actual OOM-kill, and we'd rather record the caller's reason than
    fabricate one."""

    class FlakyInspectDocker(FakeDockerClient):
        def inspect(self, container_id):  # type: ignore[no-untyped-def]
            raise RuntimeError("daemon transient")

    docker = FlakyInspectDocker()
    mgr, _, _, _ = _make_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(workspace_id="ws-42", workspace_path=str(workspace))
    mgr.launch(cfg)
    stopped = mgr.stop("ws-42", reason="operator_request")
    assert stopped.status == WebSandboxStatus.stopped
    assert stopped.killed_reason == "operator_request"

