"""BS.4.6 drift-guard — ``omnisight-installer`` compose service contract.

Pinned by ``docs/security/bs-installer-threat-model.md`` §11 row
``test_threat_model_compose_lint.py`` (the threat-model CI table). Each
assertion below maps 1:1 to a §4.x section so a future operator who
edits ``docker-compose.prod.yml`` cannot silently weaken the sidecar's
hardening without flipping a test red.

Why parse YAML instead of asking the daemon: tests run in CI with no
docker socket. ``yaml.safe_load`` is enough — every assertion is a
declarative compose-key shape check.

Mapping:

* §4.1 USER 10001 — owned by ``Dockerfile.installer`` (covered by
  separate image-layer test); compose enforces "no ``user:`` override"
  here.
* §4.2 read-only rootfs + ``/tmp`` ``/run`` tmpfs.
* §4.3 non-root + namespaced uid — Dockerfile-side; compose just keeps
  hands off (no ``user: root``).
* §4.4 ``cap_drop: [ALL]`` + ``cap_add: []`` + ``no-new-privileges``.
* §4.5 bind-mount allowlist (only toolchains rw + airgap ro).
* §4.6 ``DOCKER_HOST`` points at proxy, host docker.sock NOT mounted.
* §4.7 ``mem_limit`` / ``cpus`` / ``pids_limit`` / ``storage_opt.size``.
* BS.4.6 row spec — image tag pinned to ``:bs-v1`` (NOT
  ``${OMNISIGHT_IMAGE_TAG}`` and NOT ``:latest``); ``depends_on``
  ``backend-a`` + ``docker-socket-proxy``; ``restart: unless-stopped``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO_ROOT / "docker-compose.prod.yml"
_SVC = "omnisight-installer"


@pytest.fixture(scope="module")
def compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def installer(compose: dict) -> dict:
    services = compose.get("services") or {}
    assert _SVC in services, (
        f"BS.4.6 contract: ``services.{_SVC}`` MUST exist in "
        "docker-compose.prod.yml — sidecar deployment is the wire-up "
        "point for the BS.4 epic. Missing service block means the "
        "epic 1-5 image is built but never started in prod."
    )
    return services[_SVC]


# ─── BS.4.6 row spec — image tag pin + depends_on + restart ────────


def test_image_tag_is_pinned_to_bs_v1(installer: dict) -> None:
    """Row spec: ``不掛 :latest，掛 :bs-v1 防 silent drift``.

    Pinning the protocol-version family at the compose layer (NOT the
    global ``${OMNISIGHT_IMAGE_TAG}``) is what stops a backend-only
    image bump from silently dragging the sidecar to a wire-protocol
    revision the operator hasn't validated. See ADR §4.3 + R26.
    """
    image = installer.get("image", "")
    assert image.endswith(":bs-v1"), (
        f"omnisight-installer image MUST be pinned to ``:bs-v1`` "
        f"(got {image!r}). The bs-vN tag carries the wire-protocol "
        "version contract — see installer/main.py docstring + ADR §4.3."
    )
    assert ":latest" not in image, (
        ":latest is banned (BS.4.6 row spec + R26). Operator must "
        "deliberately re-tag bs-v1 → bs-v2 to opt into a new protocol "
        "version."
    )
    assert "${OMNISIGHT_IMAGE_TAG" not in image, (
        "image tag must NOT track the global OMNISIGHT_IMAGE_TAG env "
        "knob — that would re-couple sidecar deployment to the backend "
        "release cadence and re-introduce the drift R26 mitigates."
    )


def test_depends_on_backend_a_and_docker_socket_proxy(installer: dict) -> None:
    """Row spec: ``+ depends_on backend-a``.

    Plus ``docker-socket-proxy`` because docker_pull (BS.4.3) routes
    through the proxy — the sidecar booting before the proxy is
    healthy would just fail every docker_pull until the proxy is up.
    """
    dep = installer.get("depends_on") or {}
    if isinstance(dep, list):
        keys = set(dep)
        # Long-form is required so condition: service_healthy applies.
        pytest.fail(
            "depends_on MUST be long-form mapping with "
            "``condition: service_healthy`` — short-list form skips "
            "the health gate."
        )
    else:
        keys = set(dep.keys())
    assert "backend-a" in keys, (
        "Sidecar long-polls backend-a (per installer/main.py default "
        "OMNISIGHT_INSTALLER_BACKEND_URL). depends_on must list it so "
        "compose waits for /readyz before the first poll."
    )
    assert dep["backend-a"].get("condition") == "service_healthy", (
        "backend-a dep must wait for ``service_healthy`` — bare "
        "``service_started`` would fire poll loops at a backend that "
        "is mid-Alembic migration and 503 every request."
    )
    assert "docker-socket-proxy" in keys, (
        "docker_pull (BS.4.3) routes through docker-socket-proxy via "
        "DOCKER_HOST — depends_on keeps it sequenced."
    )
    assert dep["docker-socket-proxy"].get("condition") == "service_healthy", (
        "proxy dep must wait for ``service_healthy``."
    )


def test_restart_policy_is_unless_stopped(installer: dict) -> None:
    """Row spec: ``+ restart unless-stopped``.

    Distinct from backend-a's ``always``: an operator-initiated
    ``docker compose stop omnisight-installer`` (catalog freeze /
    maintenance) MUST persist; ``always`` would silently restart on
    daemon reboot and re-process whatever was queued.
    """
    assert installer.get("restart") == "unless-stopped", (
        "restart MUST be ``unless-stopped`` (NOT ``always`` / ``on-"
        "failure``) so operator stop is honoured across host reboots."
    )


# ─── §4.2 — read-only rootfs + tmpfs ───────────────────────────────


def test_read_only_rootfs(installer: dict) -> None:
    assert installer.get("read_only") is True, (
        "Threat model §4.2: omnisight-installer rootfs MUST be "
        "read-only. Vendor installers cannot drop persistent backdoors "
        "(e.g. PATH binaries) without a writable rootfs."
    )


def test_tmpfs_tmp_capped_and_noexec(installer: dict) -> None:
    """Threat model §4.2: ``/tmp`` 512M + noexec/nosuid/nodev.

    noexec is the load-bearing flag: vendor installers that
    self-extract a binary into /tmp and exec it are blocked. Install
    methods (BS.4.3) are explicitly cwd'd into the toolchains scratch
    dir (not noexec) so legitimate ./install.sh paths still work.
    """
    tmpfs = installer.get("tmpfs") or []
    tmp_mounts = [m for m in tmpfs if m.startswith("/tmp")]
    assert tmp_mounts, "tmpfs mount for /tmp is required (§4.2)"
    spec = tmp_mounts[0]
    for required in ("size=", "noexec", "nosuid", "nodev"):
        assert required in spec, (
            f"/tmp tmpfs option ``{required}`` missing from {spec!r} "
            "(threat model §4.2)"
        )


# ─── §4.4 — capability drop ────────────────────────────────────────


def test_cap_drop_all(installer: dict) -> None:
    cap_drop = installer.get("cap_drop") or []
    assert "ALL" in cap_drop, (
        "Threat model §4.4: ``cap_drop: [ALL]`` is mandatory. Without "
        "it the sidecar inherits docker's default cap bag (CHOWN, "
        "DAC_OVERRIDE, FOWNER, ...) and a compromised vendor installer "
        "can chown host bind-mount paths it shouldn't touch."
    )


def test_cap_add_explicit_empty(installer: dict) -> None:
    """``cap_add: []`` is intentional, NOT omitted.

    Threat model §4.4 spells this out: omitting cap_add and writing
    ``cap_add: []`` are visually similar but semantically the same to
    docker — the test asserts the explicit empty list so code review
    sees "we deliberately added zero caps" not "we forgot".
    """
    assert "cap_add" in installer, (
        "cap_add MUST be present (even as []) per §4.4 — explicit "
        "intent."
    )
    assert installer["cap_add"] == [], (
        f"cap_add MUST be empty list (got {installer['cap_add']!r}). "
        "Adding any cap requires a paired threat model §4.4 update + "
        "Gerrit dual-sign per CLAUDE.md Safety Rules."
    )


def test_no_new_privileges(installer: dict) -> None:
    sec = installer.get("security_opt") or []
    assert "no-new-privileges:true" in sec, (
        "Threat model §4.4: security_opt MUST include "
        "``no-new-privileges:true`` so a setuid binary in the rootfs "
        "(none today, but defense-in-depth) cannot escalate."
    )


# ─── §4.5 — bind-mount allowlist ───────────────────────────────────


def test_bind_mounts_only_toolchains_and_airgap(installer: dict) -> None:
    """Threat model §4.5: only two host bind-mounts allowed."""
    volumes = installer.get("volumes") or []
    bind_sources = []
    for v in volumes:
        if isinstance(v, str) and v.startswith("/"):
            bind_sources.append(v.split(":", 1)[0])
    expected = {
        "/var/lib/omnisight/toolchains",
        "/var/lib/omnisight/airgap",
    }
    assert set(bind_sources) == expected, (
        f"Bind-mount sources MUST be exactly {sorted(expected)} per "
        f"threat model §4.5 (got {sorted(bind_sources)}). Adding a "
        "host path widens the sidecar's filesystem reach beyond the "
        "audit; removing one breaks an install method."
    )
    # Specifically: no /var/run/docker.sock anywhere.
    for v in volumes:
        assert "/var/run/docker.sock" not in str(v), (
            "Threat model §4.6: omnisight-installer MUST NOT bind-"
            "mount /var/run/docker.sock — it talks to the daemon ONLY "
            "via docker-socket-proxy (DOCKER_HOST env). Bind-mounting "
            "the socket is a tier-0 container escape primitive."
        )


def test_airgap_mount_is_read_only(installer: dict) -> None:
    volumes = installer.get("volumes") or []
    airgap = [v for v in volumes if "/var/lib/omnisight/airgap" in str(v)]
    assert airgap, "airgap bind-mount missing (§4.5)"
    spec = airgap[0]
    assert spec.endswith(":ro") or ":ro:" in spec, (
        f"airgap bind-mount MUST be ``:ro`` (got {spec!r}). Operator "
        "stages tarballs there; sidecar should never write back into "
        "the air-gap bundle store."
    )


# ─── §4.6 — DOCKER_HOST routes through proxy ───────────────────────


def test_docker_host_points_at_proxy(installer: dict) -> None:
    env = installer.get("environment") or []
    if isinstance(env, dict):
        items = env
    else:
        items = dict(e.split("=", 1) for e in env if "=" in e)
    docker_host = items.get("DOCKER_HOST", "")
    assert docker_host == "tcp://docker-socket-proxy:2375", (
        f"DOCKER_HOST MUST be ``tcp://docker-socket-proxy:2375`` (got "
        f"{docker_host!r}). The default unix-socket fallback would "
        "fail (sidecar has no socket mount), but if a future operator "
        "tries to ``fix`` it by bind-mounting the socket they re-open "
        "the §4.6 escape primitive — pinning DOCKER_HOST here keeps "
        "the right answer in code."
    )


# ─── §4.7 — cgroup limits ──────────────────────────────────────────


def test_memory_cap_2g(installer: dict) -> None:
    assert installer.get("mem_limit") == "2g", (
        "Threat model §4.7: ``mem_limit: 2g`` (got "
        f"{installer.get('mem_limit')!r}). Vendor installer that "
        "leaks memory hits OOM-kill before host pressure rises."
    )


def test_cpu_cap_2(installer: dict) -> None:
    assert float(installer.get("cpus", 0)) == 2.0, (
        f"Threat model §4.7: ``cpus: 2.0`` (got "
        f"{installer.get('cpus')!r}). Bounds ``make -j$(nproc)``."
    )


def test_pids_cap_512(installer: dict) -> None:
    assert installer.get("pids_limit") == 512, (
        f"Threat model §4.7: ``pids_limit: 512`` (got "
        f"{installer.get('pids_limit')!r}). Fork-bomb ceiling."
    )


def test_storage_opt_size_10g(installer: dict) -> None:
    """Documented unconditionally — overlay2 silently ignores
    ``storage_opt.size``; devicemapper/btrfs honour it. Listed so the
    cap is in place the moment the host's storage driver supports it.
    """
    so = installer.get("storage_opt") or {}
    assert so.get("size") == "10G", (
        f"Threat model §4.7: ``storage_opt.size: 10G`` (got "
        f"{so.get('size')!r}). Bounds rootfs+tmpfs to 10 GB."
    )


# ─── §4.3 + BS.4.1 — no user override ──────────────────────────────


def test_no_user_root_override(installer: dict) -> None:
    """Threat model §4.3: Dockerfile.installer pins USER 10001:10001;
    compose MUST NOT override with ``user: root`` / ``user: 0``.
    """
    user = installer.get("user")
    assert user is None or str(user) not in {"root", "0", "0:0"}, (
        f"omnisight-installer ``user:`` override is forbidden (got "
        f"{user!r}). Dockerfile §4.3 fixes uid 10001; overriding "
        "back to root defeats the entire §4.x layer."
    )


# ─── BS.4.5 — health endpoint reachability ─────────────────────────


def test_healthcheck_probes_health_endpoint(installer: dict) -> None:
    hc = installer.get("healthcheck") or {}
    test = hc.get("test") or []
    joined = " ".join(str(x) for x in test)
    assert "/health" in joined and ":9090" in joined, (
        f"healthcheck MUST probe BS.4.5's :9090/health endpoint (got "
        f"{joined!r}). Without it ``restart: unless-stopped`` cannot "
        "auto-recover a stuck poll loop."
    )


def test_health_port_not_published_to_host(installer: dict) -> None:
    """The /health JSON includes sidecar_id + last_job_id — operator-
    facing detail that should not be reachable from outside the
    compose network. ``expose:`` (compose-internal only) is correct;
    ``ports:`` (host publish) would leak it.
    """
    ports = installer.get("ports") or []
    assert not any("9090" in str(p) for p in ports), (
        f"Health port 9090 MUST NOT be published to host (got "
        f"ports={ports!r}). Use ``expose:`` instead — compose-internal "
        "reach only."
    )
