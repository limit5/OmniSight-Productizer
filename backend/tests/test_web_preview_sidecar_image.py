"""W14.1 — `omnisight-web-preview` sidecar image contract tests.

Pins the structural promises that the W14.1 image makes to the rest of
the W14 epic. Two layers of defence:

1. **Static contract** — read `Dockerfile.web-preview`,
   `web-preview/manifest.json`, and `web-preview/entrypoint.sh` and
   assert load-bearing invariants without invoking Docker. Catches
   regressions in <100 ms with no Docker daemon required, runs in
   every CI lane.

2. **Live image lane (opt-in)** — when
   `OMNISIGHT_TEST_DOCKER_WEB_PREVIEW=1` is set AND
   `omnisight-web-preview:dev` is locally built, exercise the image
   for real: run each pinned tool's `--version`, parse the output, and
   assert it matches `web-preview/manifest.json`. Skipped by default
   because building the image takes ~3-5 min on a cold cache (Bun
   download dominates) and most CI lanes do not provision Docker.

Why three artefacts (Dockerfile + manifest + entrypoint), tested as a
trio:

* The **Dockerfile** is the build recipe.
* The **manifest** is the *machine-readable* expression of what the
  image promises (versions, ports, runtime user). Backends that launch
  the sidecar (W14.2) can read the manifest to know which port to wire
  the CF Tunnel ingress (W14.3) against without parsing Dockerfile
  comments.
* The **entrypoint** is the runtime contract: refuses uid 0, cd's to
  `/workspace`, exec's whatever the backend tells it to.

If any of those three drift apart (e.g., manifest says Bun 1.1.40 but
Dockerfile pins 1.1.50), the wire-up between W14.1 and W14.2 silently
breaks. The drift-guard tests below catch every such miss.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile.web-preview"
MANIFEST = REPO_ROOT / "web-preview" / "manifest.json"
ENTRYPOINT = REPO_ROOT / "web-preview" / "entrypoint.sh"


# ---------------------------------------------------------------------------
# Fixtures — read each artefact once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    assert DOCKERFILE.exists(), f"missing: {DOCKERFILE}"
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST.exists(), f"missing: {MANIFEST}"
    return json.loads(MANIFEST.read_text())


@pytest.fixture(scope="module")
def entrypoint_text() -> str:
    assert ENTRYPOINT.exists(), f"missing: {ENTRYPOINT}"
    return ENTRYPOINT.read_text()


# ---------------------------------------------------------------------------
# Manifest schema contract
# ---------------------------------------------------------------------------


def test_manifest_schema_version_pinned(manifest: dict) -> None:
    # If we ever bump the schema, the bump must be visible — assert the
    # current version explicitly so a silent edit fails.
    assert manifest["schema_version"] == "1"


def test_manifest_image_name_matches_row(manifest: dict) -> None:
    # The W14.1 row literally names the image `omnisight-web-preview`.
    # Renaming requires updating TODO + every downstream W14.x reference.
    assert manifest["image_name"] == "omnisight-web-preview"


def test_manifest_lists_four_required_tools(manifest: dict) -> None:
    # W14.1 row requires Node 22 LTS + pnpm + Vite + Bun. Each must
    # appear under `tools`. Adding a fifth is fine; dropping one is the
    # bug we're guarding against.
    tools = set(manifest["tools"].keys())
    assert {"node", "pnpm", "vite", "bun"}.issubset(tools), \
        f"manifest must declare node/pnpm/vite/bun tools; got {tools}"


def test_manifest_node_pinned_to_22_lts(manifest: dict) -> None:
    pins = manifest["version_pins"]
    assert pins["node_major"] == "22", \
        "W14.1 row mandates Node 22 LTS — bumping requires updating the row + this test"
    assert pins["node_channel"] == "lts", \
        "must explicitly track the LTS channel, not the current/latest line"


def test_manifest_pnpm_version_is_semver(manifest: dict) -> None:
    pnpm = manifest["version_pins"]["pnpm"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pnpm), \
        f"pnpm version must be exact semver MAJOR.MINOR.PATCH; got {pnpm!r}"


def test_manifest_bun_version_is_semver(manifest: dict) -> None:
    bun = manifest["version_pins"]["bun"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", bun), \
        f"bun version must be exact semver; got {bun!r}"


def test_manifest_vite_version_is_semver(manifest: dict) -> None:
    vite = manifest["version_pins"]["vite"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", vite), \
        f"vite version must be exact semver; got {vite!r}"


def test_manifest_runtime_user_unprivileged(manifest: dict) -> None:
    # Defence in depth: the manifest must declare the non-root uid that
    # the Dockerfile USER directive uses. uid 0 = security regression.
    assert manifest["runtime_uid"] == 10002
    assert manifest["runtime_gid"] == 10002
    assert manifest["runtime_user"] == "preview"


def test_manifest_uid_distinct_from_sibling_sidecars(manifest: dict) -> None:
    # uid 10001 is BS.4 installer, uid 65532 is the frontend. The
    # web-preview sidecar must not collide with either, otherwise host
    # bind-mount ownership becomes ambiguous when both sidecars live on
    # the same host.
    assert manifest["runtime_uid"] not in (10001, 65532, 0), \
        "web-preview uid must not collide with installer (10001) / frontend (65532) / root (0)"


def test_manifest_workdir_is_workspace(manifest: dict) -> None:
    # The W14.2 backend launcher bind-mounts the operator's checked-out
    # source at /workspace. Hardcoding this in the manifest lets W14.2
    # read the path instead of duplicating it.
    assert manifest["workdir"] == "/workspace"


def test_manifest_exposed_ports_cover_vite_and_nuxt(manifest: dict) -> None:
    # Vite default = 5173, Nuxt SSR default = 3000. Both must appear so
    # the W14.3 CF Tunnel ingress rule has a target regardless of which
    # scaffold the operator picked.
    ports = set(manifest["exposed_ports"])
    assert {3000, 5173}.issubset(ports), \
        f"manifest must expose 3000 (Nuxt) + 5173 (Vite); got {ports}"


def test_manifest_entrypoint_path_matches_image_layout(manifest: dict) -> None:
    assert manifest["entrypoint"] == "/usr/local/bin/web-preview-entrypoint.sh"


def test_manifest_default_cmd_is_pnpm_dev(manifest: dict) -> None:
    # Smoke-test default: `pnpm dev --host 0.0.0.0`. The `--host 0.0.0.0`
    # is load-bearing because Vite/Nuxt bind to 127.0.0.1 by default,
    # which is unreachable from outside the container.
    assert manifest["default_cmd"] == ["pnpm", "dev", "--host", "0.0.0.0"]


def test_manifest_each_tool_has_version_check_command(manifest: dict) -> None:
    # The opt-in live-image lane below shells out using these arrays.
    # Every tool must declare one.
    for tool, spec in manifest["tools"].items():
        assert isinstance(spec.get("version_check"), list) and spec["version_check"], \
            f"tool {tool!r} must declare a non-empty version_check command list"
        for arg in spec["version_check"]:
            assert isinstance(arg, str) and arg, \
                f"tool {tool!r} version_check args must be non-empty strings"


# ---------------------------------------------------------------------------
# Dockerfile structural contract
# ---------------------------------------------------------------------------


def test_dockerfile_uses_node_22_bookworm_slim(dockerfile_text: str) -> None:
    # Bun ships glibc-only binaries — alpine (musl) would silently fail
    # at runtime. Pinning bookworm-slim is load-bearing for Bun support.
    assert re.search(r"^FROM\s+node:22[-.]bookworm-slim\b", dockerfile_text, re.M), (
        "Dockerfile must base on node:22-bookworm-slim "
        "(alpine breaks Bun, full bookworm wastes ~150 MB)"
    )


def test_dockerfile_does_not_use_alpine(dockerfile_text: str) -> None:
    # Belt-and-braces: even if a future bump misses the bookworm-slim
    # check, an alpine base must trip this assertion.
    code_only = "\n".join(
        line for line in dockerfile_text.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "alpine" not in code_only.lower(), \
        "must not use any alpine base — Bun's official binaries are glibc-only"


def test_dockerfile_pnpm_arg_matches_manifest(dockerfile_text: str, manifest: dict) -> None:
    m = re.search(r"^ARG\s+PNPM_VERSION=(\S+)", dockerfile_text, re.M)
    assert m, "Dockerfile must declare ARG PNPM_VERSION=..."
    assert m.group(1) == manifest["version_pins"]["pnpm"], (
        f"Dockerfile pnpm pin {m.group(1)!r} must match manifest "
        f"{manifest['version_pins']['pnpm']!r}"
    )


def test_dockerfile_bun_arg_matches_manifest(dockerfile_text: str, manifest: dict) -> None:
    m = re.search(r"^ARG\s+BUN_VERSION=(\S+)", dockerfile_text, re.M)
    assert m, "Dockerfile must declare ARG BUN_VERSION=..."
    assert m.group(1) == manifest["version_pins"]["bun"], (
        f"Dockerfile bun pin {m.group(1)!r} must match manifest "
        f"{manifest['version_pins']['bun']!r}"
    )


def test_dockerfile_vite_arg_matches_manifest(dockerfile_text: str, manifest: dict) -> None:
    m = re.search(r"^ARG\s+VITE_VERSION=(\S+)", dockerfile_text, re.M)
    assert m, "Dockerfile must declare ARG VITE_VERSION=..."
    assert m.group(1) == manifest["version_pins"]["vite"], (
        f"Dockerfile vite pin {m.group(1)!r} must match manifest "
        f"{manifest['version_pins']['vite']!r}"
    )


def test_dockerfile_installs_pnpm_via_corepack(dockerfile_text: str) -> None:
    # `corepack prepare ... --activate` is the canonical, reproducible
    # path. `npm install -g pnpm` would leave the version unpinned.
    assert "corepack enable" in dockerfile_text, \
        "must enable corepack so the pinned pnpm shim is installed"
    assert re.search(r"corepack prepare\s+.*pnpm@", dockerfile_text), \
        "must use `corepack prepare pnpm@${PNPM_VERSION}` for reproducibility"


def test_dockerfile_installs_bun_to_opt(dockerfile_text: str) -> None:
    # /opt/bun is the fixed install location; symlinked into
    # /usr/local/bin so the unprivileged user finds it on PATH.
    assert "BUN_INSTALL=/opt/bun" in dockerfile_text, \
        "must install Bun under /opt/bun (host-uid-agnostic location)"
    assert re.search(r"ln -s /opt/bun/bin/bun /usr/local/bin/bun", dockerfile_text), \
        "must symlink /opt/bun/bin/bun into /usr/local/bin/ for PATH visibility"


def test_dockerfile_ships_tini_as_pid1(dockerfile_text: str) -> None:
    # tini reaps zombies + forwards SIGTERM. Without it, the W14.5
    # idle-kill reaper's `docker stop` (SIGTERM) leaves esbuild /
    # vite-optimizer worker zombies and the container hangs.
    assert "tini" in dockerfile_text, \
        "must install tini for PID-1 signal forwarding"
    assert re.search(r'ENTRYPOINT\s+\[\s*"/usr/bin/tini"', dockerfile_text), \
        "ENTRYPOINT must run tini as PID 1"
    assert re.search(r'"/usr/bin/tini",\s*"-g"', dockerfile_text), \
        "tini must run with `-g` to forward signals to the entire process group"


def test_dockerfile_runs_as_unprivileged_uid_10002(dockerfile_text: str) -> None:
    assert re.search(r"^USER\s+10002:10002\b", dockerfile_text, re.M), \
        "must drop to USER 10002:10002 (matches manifest.runtime_uid/gid)"
    assert re.search(r"useradd.*-u 10002.*-g 10002", dockerfile_text), \
        "must create a dedicated `preview` user with uid 10002, gid 10002"


def test_dockerfile_does_not_run_as_root(dockerfile_text: str) -> None:
    # Find the LAST USER directive — that wins for the runtime image.
    user_lines = re.findall(r"^USER\s+(\S+)", dockerfile_text, re.M)
    assert user_lines, "Dockerfile must declare at least one USER directive"
    last = user_lines[-1].split(":")[0]
    assert last not in ("0", "root"), \
        f"final USER must be unprivileged; got {last!r}"


def test_dockerfile_workdir_is_workspace(dockerfile_text: str) -> None:
    workdirs = re.findall(r"^WORKDIR\s+(\S+)", dockerfile_text, re.M)
    assert workdirs, "Dockerfile must declare WORKDIR"
    assert workdirs[-1] == "/workspace", \
        f"final WORKDIR must be /workspace (matches manifest); got {workdirs[-1]!r}"


def test_dockerfile_exposes_3000_and_5173(dockerfile_text: str) -> None:
    exposed = set(re.findall(r"^EXPOSE\s+(\d+)\b", dockerfile_text, re.M))
    assert "3000" in exposed, "must EXPOSE 3000 (Nuxt SSR default)"
    assert "5173" in exposed, "must EXPOSE 5173 (Vite dev server default)"


def test_dockerfile_entrypoint_invokes_web_preview_shim(dockerfile_text: str) -> None:
    assert "/usr/local/bin/web-preview-entrypoint.sh" in dockerfile_text, \
        "ENTRYPOINT must chain into the web-preview shim"


def test_dockerfile_default_cmd_is_pnpm_dev(dockerfile_text: str) -> None:
    # The CMD should match the manifest's default_cmd (this is what
    # `docker run` falls through to when the backend doesn't specify
    # a command — used by the smoke test).
    assert re.search(
        r'CMD\s+\[\s*"pnpm",\s*"dev",\s*"--host",\s*"0.0.0.0"\s*\]',
        dockerfile_text,
    ), "Dockerfile CMD must default to `pnpm dev --host 0.0.0.0`"


def test_dockerfile_installs_git_for_workspace_fetch(dockerfile_text: str) -> None:
    # W14.2 backend will `git fetch` the workspace ref into /workspace
    # before exec'ing. Without git in the image, the fetch fails.
    assert re.search(r"\bgit\b", dockerfile_text), \
        "must install git so the W14.2 launcher can fetch the workspace ref"


def test_dockerfile_installs_curl_and_unzip_for_bun(dockerfile_text: str) -> None:
    # Bun's installer uses curl for download + unzip for extraction.
    assert re.search(r"\bcurl\b", dockerfile_text), \
        "must install curl (Bun installer's HTTPS fetch)"
    assert re.search(r"\bunzip\b", dockerfile_text), \
        "must install unzip (Bun installer's tarball extraction)"


def test_dockerfile_does_not_set_docker_host(dockerfile_text: str) -> None:
    # The sidecar must NEVER talk to the docker daemon — it has no
    # business knowing one exists. Compare with Dockerfile.installer,
    # which deliberately leaves DOCKER_HOST unset for the same reason.
    assert "DOCKER_HOST" not in dockerfile_text, \
        "web-preview sidecar must not set DOCKER_HOST — it has no daemon access"


def test_dockerfile_copies_manifest_into_image(dockerfile_text: str) -> None:
    # `/etc/omnisight/web-preview-manifest.json` is the in-container
    # introspection point. Backends running `docker exec ... cat ...`
    # use it to read the version pins without re-parsing the Dockerfile.
    assert re.search(
        r"COPY\s+web-preview/manifest\.json\s+/etc/omnisight/web-preview-manifest\.json",
        dockerfile_text,
    ), "must COPY web-preview/manifest.json into /etc/omnisight/"


def test_dockerfile_copies_entrypoint_with_executable_perms(dockerfile_text: str) -> None:
    assert re.search(
        r"COPY\s+web-preview/entrypoint\.sh\s+/usr/local/bin/web-preview-entrypoint\.sh",
        dockerfile_text,
    ), "must COPY web-preview/entrypoint.sh into /usr/local/bin/"
    assert re.search(
        r"chmod\s+0?555\s+/usr/local/bin/web-preview-entrypoint\.sh",
        dockerfile_text,
    ), "must chmod entrypoint to 0555 (read+exec, no write — defence in depth)"


# ---------------------------------------------------------------------------
# Entrypoint contract
# ---------------------------------------------------------------------------


def test_entrypoint_uses_posix_sh(entrypoint_text: str) -> None:
    first = entrypoint_text.splitlines()[0]
    assert first == "#!/bin/sh", \
        f"entrypoint must use #!/bin/sh shebang; got {first!r}"


def test_entrypoint_set_strict(entrypoint_text: str) -> None:
    # `set -eu`: -e exits on error, -u catches unset variables. -o
    # pipefail isn't POSIX (only bash), so we use the portable subset.
    assert re.search(r"^set\s+-eu\b", entrypoint_text, re.M), \
        "entrypoint must `set -eu` for fail-fast + unset-var safety"


def test_entrypoint_refuses_uid_zero(entrypoint_text: str) -> None:
    # Defence in depth: even if a future operator forgets `--user` in
    # `docker run`, the shim must self-reject root.
    assert re.search(r'id -u', entrypoint_text), \
        "entrypoint must check `id -u` and refuse to run as root"
    assert "exit 1" in entrypoint_text, \
        "entrypoint must `exit 1` on root detection (not just print + continue)"


def test_entrypoint_cd_to_workspace(entrypoint_text: str) -> None:
    assert re.search(r"^cd /workspace\b", entrypoint_text, re.M), \
        "entrypoint must `cd /workspace` (matches WORKDIR + manifest.workdir)"


def test_entrypoint_uses_exec_for_signal_passthrough(entrypoint_text: str) -> None:
    # `exec "$@"` replaces the shell process so SIGTERM from
    # `docker stop` reaches the dev server directly. Without exec,
    # the shell catches signals and the dev server hangs.
    assert re.search(r'exec\s+"\$@"', entrypoint_text), \
        'entrypoint must `exec "$@"` for SIGTERM passthrough'


def test_entrypoint_falls_through_to_pnpm_dev_when_no_args(entrypoint_text: str) -> None:
    # Smoke-test fallback: bare `docker run omnisight-web-preview`
    # should land on `pnpm dev --host 0.0.0.0` (matches Dockerfile CMD).
    assert re.search(r'set --\s+pnpm dev --host 0\.0\.0\.0', entrypoint_text), \
        "entrypoint must fall through to `pnpm dev --host 0.0.0.0` when no CMD supplied"


def test_entrypoint_is_executable_on_disk() -> None:
    # The `chmod 0555` in the Dockerfile takes effect inside the image;
    # on the host side, the file should also be executable so a local
    # `./web-preview/entrypoint.sh foo` works for diagnostics. The
    # COPY into the image preserves this bit, so this test is a
    # belt-and-braces guard against `git checkout` regressing the mode.
    mode = ENTRYPOINT.stat().st_mode & 0o777
    assert mode & 0o111, (
        f"entrypoint.sh must be executable on disk; got mode {oct(mode)}"
    )


# ---------------------------------------------------------------------------
# Cross-artefact drift guards
# ---------------------------------------------------------------------------


def test_dockerfile_workdir_matches_manifest_workdir(
    dockerfile_text: str, manifest: dict
) -> None:
    workdirs = re.findall(r"^WORKDIR\s+(\S+)", dockerfile_text, re.M)
    assert workdirs[-1] == manifest["workdir"], (
        f"final WORKDIR ({workdirs[-1]!r}) must match manifest.workdir "
        f"({manifest['workdir']!r})"
    )


def test_dockerfile_user_matches_manifest_runtime_uid(
    dockerfile_text: str, manifest: dict
) -> None:
    user = re.findall(r"^USER\s+(\S+)", dockerfile_text, re.M)[-1]
    uid_str, gid_str = user.split(":", 1)
    assert int(uid_str) == manifest["runtime_uid"], \
        f"Dockerfile USER uid {uid_str} must match manifest.runtime_uid"
    assert int(gid_str) == manifest["runtime_gid"], \
        f"Dockerfile USER gid {gid_str} must match manifest.runtime_gid"


def test_dockerfile_exposed_ports_match_manifest(
    dockerfile_text: str, manifest: dict
) -> None:
    exposed = {int(p) for p in re.findall(r"^EXPOSE\s+(\d+)\b", dockerfile_text, re.M)}
    assert exposed == set(manifest["exposed_ports"]), (
        f"Dockerfile EXPOSE set {sorted(exposed)} must match "
        f"manifest.exposed_ports {sorted(manifest['exposed_ports'])}"
    )


def test_entrypoint_path_in_dockerfile_matches_manifest(
    dockerfile_text: str, manifest: dict
) -> None:
    assert manifest["entrypoint"] in dockerfile_text, (
        f"manifest.entrypoint {manifest['entrypoint']!r} must appear in the "
        "Dockerfile (so the COPY destination + ENTRYPOINT path agree)"
    )


# ---------------------------------------------------------------------------
# Live image lane (opt-in via OMNISIGHT_TEST_DOCKER_WEB_PREVIEW=1)
# ---------------------------------------------------------------------------


LIVE_OPT_IN = os.environ.get("OMNISIGHT_TEST_DOCKER_WEB_PREVIEW") == "1"
LIVE_IMAGE_TAG = os.environ.get(
    "OMNISIGHT_WEB_PREVIEW_IMAGE_TAG", "omnisight-web-preview:dev"
)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _image_present(tag: str) -> bool:
    if not _docker_available():
        return False
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_LIVE_REASON = (
    "live image lane is opt-in: set OMNISIGHT_TEST_DOCKER_WEB_PREVIEW=1 + build "
    f"`{LIVE_IMAGE_TAG}` locally before running this test"
)


@pytest.mark.skipif(
    not (LIVE_OPT_IN and _docker_available() and _image_present(LIVE_IMAGE_TAG)),
    reason=_LIVE_REASON,
)
def test_live_image_node_version_matches_manifest(manifest: dict) -> None:
    node_major = manifest["version_pins"]["node_major"]
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "node", LIVE_IMAGE_TAG, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    # Node prints e.g. "v22.13.0"
    assert out.stdout.strip().startswith(f"v{node_major}."), (
        f"node --version {out.stdout.strip()!r} must start with v{node_major}."
    )


@pytest.mark.skipif(
    not (LIVE_OPT_IN and _docker_available() and _image_present(LIVE_IMAGE_TAG)),
    reason=_LIVE_REASON,
)
def test_live_image_pnpm_version_matches_manifest(manifest: dict) -> None:
    expected = manifest["version_pins"]["pnpm"]
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "pnpm", LIVE_IMAGE_TAG, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert out.stdout.strip() == expected, (
        f"pnpm --version {out.stdout.strip()!r} must equal manifest pin {expected!r}"
    )


@pytest.mark.skipif(
    not (LIVE_OPT_IN and _docker_available() and _image_present(LIVE_IMAGE_TAG)),
    reason=_LIVE_REASON,
)
def test_live_image_bun_version_matches_manifest(manifest: dict) -> None:
    expected = manifest["version_pins"]["bun"]
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "bun", LIVE_IMAGE_TAG, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert out.stdout.strip() == expected, (
        f"bun --version {out.stdout.strip()!r} must equal manifest pin {expected!r}"
    )


@pytest.mark.skipif(
    not (LIVE_OPT_IN and _docker_available() and _image_present(LIVE_IMAGE_TAG)),
    reason=_LIVE_REASON,
)
def test_live_image_vite_version_matches_manifest(manifest: dict) -> None:
    expected = manifest["version_pins"]["vite"]
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "vite", LIVE_IMAGE_TAG, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    # vite prints e.g. "vite/5.4.10 linux-x64 node-v22.13.0"
    assert expected in out.stdout, (
        f"vite --version {out.stdout.strip()!r} must contain manifest pin {expected!r}"
    )


@pytest.mark.skipif(
    not (LIVE_OPT_IN and _docker_available() and _image_present(LIVE_IMAGE_TAG)),
    reason=_LIVE_REASON,
)
def test_live_image_runs_as_uid_10002(manifest: dict) -> None:
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "id", LIVE_IMAGE_TAG, "-u"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert out.stdout.strip() == str(manifest["runtime_uid"]), (
        f"container uid {out.stdout.strip()!r} must equal manifest "
        f"runtime_uid {manifest['runtime_uid']}"
    )


@pytest.mark.skipif(
    not (LIVE_OPT_IN and _docker_available() and _image_present(LIVE_IMAGE_TAG)),
    reason=_LIVE_REASON,
)
def test_live_image_ships_manifest_at_etc_omnisight(manifest: dict) -> None:
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat", LIVE_IMAGE_TAG,
         "/etc/omnisight/web-preview-manifest.json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    in_image = json.loads(out.stdout)
    assert in_image == manifest, (
        "manifest.json on disk must equal the version baked into the image"
    )
