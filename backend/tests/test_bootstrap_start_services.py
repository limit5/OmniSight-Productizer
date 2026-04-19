"""L5 Step 4 — ``POST /api/v1/bootstrap/start-services`` endpoint tests.

Covers the launcher dispatch for the wizard's service-start step:

  * ``dev`` mode returns ``already_running`` without exec'ing anything
  * ``systemd`` mode builds ``systemctl start omnisight-backend omnisight-frontend``
    and surfaces returncode / stdout / stderr back through the response
  * ``docker-compose`` mode builds ``docker compose -f <file> up -d`` and
    honours the ``compose_file`` override
  * non-zero returncode is surfaced as HTTP 502 with stderr_tail populated
  * timeout is surfaced as HTTP 504
  * unknown mode override returns HTTP 422
  * audit row ``bootstrap.start_services`` is emitted on every call
  * auto-detect via ``OMNISIGHT_DEPLOY_MODE`` env override works end-to-end
"""

from __future__ import annotations

import asyncio

import pytest


class _FakeProc:
    """Minimal ``asyncio.subprocess.Process`` stand-in used by the fake exec."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes,
                 *, hang: bool = False) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang

    async def communicate(self):  # type: ignore[override]
        if self._hang:
            # Sleep long enough to trip the wait_for timeout under test.
            await asyncio.sleep(60)
        return self._stdout, self._stderr


def _patch_exec(monkeypatch, captured: list, *, returncode: int = 0,
                stdout: bytes = b"", stderr: bytes = b"",
                hang: bool = False, raise_fnf: bool = False):
    """Swap out ``asyncio.create_subprocess_exec`` with a capturing fake."""
    from backend.routers import bootstrap as _br

    async def fake_exec(*args, **kwargs):
        captured.append(list(args))
        if raise_fnf:
            raise FileNotFoundError(
                2, "No such file or directory", args[0] if args else "<?>",
            )
        return _FakeProc(returncode, stdout, stderr, hang=hang)

    monkeypatch.setattr(_br.asyncio, "create_subprocess_exec", fake_exec)


# ── dev mode: no-op ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_services_dev_mode_is_noop(client, monkeypatch):
    """``dev`` mode must NOT exec anything and returns already_running."""
    captured: list = []
    _patch_exec(monkeypatch, captured)

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "dev"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "already_running"
    assert body["mode"] == "dev"
    assert body["command"] == []
    assert body["returncode"] == 0
    assert captured == []  # nothing was exec'd


@pytest.mark.asyncio
async def test_start_services_docker_compose_without_cli_is_managed_externally(
    client, monkeypatch,
):
    """Path B hardened deployment: mode=docker-compose, no docker CLI
    in the backend container (minimal image + docker-socket-proxy
    side-car). The handler must short-circuit with
    ``managed_externally`` instead of exec'ing ``docker compose up -d``
    — which would fail with ``binary_missing`` and scare the operator.
    """
    from backend.routers import bootstrap as _br

    captured: list = []
    _patch_exec(monkeypatch, captured)
    # Force ``shutil.which("docker")`` to look empty regardless of the
    # test runner's real PATH.
    monkeypatch.setattr(
        _br.shutil, "which",
        lambda name: None if name == "docker" else "/usr/bin/" + name,
    )

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "docker-compose"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "managed_externally"
    assert body["mode"] == "docker-compose"
    assert body["command"] == []
    assert body["returncode"] == 0
    assert captured == []  # nothing was exec'd


# ── systemd mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_services_systemd_success(client, monkeypatch):
    """``systemd`` mode exec's ``sudo -n systemctl start`` for both units."""
    captured: list = []
    _patch_exec(monkeypatch, captured, returncode=0,
                stdout=b"Started omnisight-backend.service\n",
                stderr=b"")

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "started"
    assert body["mode"] == "systemd"
    assert body["command"] == [
        "sudo", "-n", "systemctl", "start",
        "omnisight-backend.service", "omnisight-frontend.service",
    ]
    assert body["returncode"] == 0
    assert "Started omnisight-backend" in body["stdout_tail"]

    # Fake exec saw exactly the command in the response.
    assert captured == [[
        "sudo", "-n", "systemctl", "start",
        "omnisight-backend.service", "omnisight-frontend.service",
    ]]


@pytest.mark.asyncio
async def test_start_services_systemd_nonzero_returns_502(client, monkeypatch):
    """Non-zero rc → 502 with stderr_tail echoed back for UI debug."""
    _patch_exec(
        monkeypatch,
        captured=[],
        returncode=5,
        stdout=b"",
        stderr=b"Failed to start omnisight-backend.service: unit not found\n",
    )

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["mode"] == "systemd"
    assert body["returncode"] == 5
    assert "unit not found" in body["stderr_tail"]


# ── L8 #3 — kind-field classification per failure mode ────────────────
#
# The wizard UI picks a dedicated banner + remediation hint keyed by
# ``kind`` rather than string-parsing stderr. These tests pin the
# classification so a future stderr-wording change still routes to the
# right UX bucket.


@pytest.mark.asyncio
async def test_start_services_kind_sudoers_missing(client, monkeypatch):
    """systemd stderr 'sudo: a password is required' → kind=sudoers_missing."""
    _patch_exec(
        monkeypatch,
        captured=[],
        returncode=1,
        stdout=b"",
        stderr=b"sudo: a password is required\n",
    )
    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 502, r.text
    body = r.json()
    assert body.get("kind") == "sudoers_missing"
    assert body.get("mode") == "systemd"


@pytest.mark.asyncio
async def test_start_services_kind_unit_missing(client, monkeypatch):
    """systemd stderr 'Unit not found' → kind=unit_missing."""
    _patch_exec(
        monkeypatch,
        captured=[],
        returncode=5,
        stdout=b"",
        stderr=b"Failed to start omnisight-backend.service: Unit not found.\n",
    )
    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 502, r.text
    assert r.json().get("kind") == "unit_missing"


@pytest.mark.asyncio
async def test_start_services_kind_unit_failed_catchall(client, monkeypatch):
    """systemd stderr w/o sudoers/unit signals → kind=unit_failed catch-all."""
    _patch_exec(
        monkeypatch,
        captured=[],
        returncode=1,
        stdout=b"",
        stderr=b"Error: port 8000 already in use\n",
    )
    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 502, r.text
    assert r.json().get("kind") == "unit_failed"


@pytest.mark.asyncio
async def test_start_services_kind_binary_missing(client, monkeypatch):
    """FileNotFoundError path adds kind=binary_missing for UI routing."""
    _patch_exec(monkeypatch, captured=[], raise_fnf=True)
    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 502, r.text
    assert r.json().get("kind") == "binary_missing"


@pytest.mark.asyncio
async def test_start_services_kind_timeout(client, monkeypatch):
    """504 timeout adds kind=timeout so the UI picks the stall hint."""
    from backend.routers import bootstrap as _br

    _patch_exec(monkeypatch, captured=[], hang=True)
    monkeypatch.setattr(_br, "_START_TIMEOUT_SECS", 0.1)

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 504, r.text
    assert r.json().get("kind") == "timeout"


@pytest.mark.asyncio
async def test_start_services_kind_bad_mode(client):
    """Unknown mode override 422 adds kind=bad_mode for the UI banner."""
    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "kubernetes"},
        follow_redirects=False,
    )
    assert r.status_code == 422, r.text
    assert r.json().get("kind") == "bad_mode"


@pytest.mark.asyncio
async def test_start_services_kind_unit_failed_for_docker_compose(client, monkeypatch):
    """docker-compose rc!=0 routes to unit_failed (sudoers/unit branches are systemd-only)."""
    _patch_exec(
        monkeypatch,
        captured=[],
        returncode=2,
        stdout=b"",
        stderr=b"dependency failed to start: container omnisight-backend-1 is unhealthy\n",
    )
    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "docker-compose"},
        follow_redirects=False,
    )
    assert r.status_code == 502, r.text
    body = r.json()
    assert body.get("kind") == "unit_failed"
    assert body.get("mode") == "docker-compose"


# ── docker-compose mode ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_services_docker_compose_default_file(client, monkeypatch):
    """``docker-compose`` mode defaults to ``docker-compose.prod.yml``."""
    captured: list = []
    _patch_exec(monkeypatch, captured, returncode=0,
                stdout=b"Creating network\n", stderr=b"")

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "docker-compose"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["command"] == [
        "docker", "compose", "-f", "docker-compose.prod.yml", "up", "-d",
    ]


@pytest.mark.asyncio
async def test_start_services_docker_compose_custom_file(client, monkeypatch):
    """Operator can override the compose file via ``compose_file`` body field."""
    captured: list = []
    _patch_exec(monkeypatch, captured, returncode=0)

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "docker-compose", "compose_file": "docker-compose.edge.yml"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert captured[0] == [
        "docker", "compose", "-f", "docker-compose.edge.yml", "up", "-d",
    ]


# ── error paths ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_services_invalid_mode_returns_422(client):
    """Unknown mode rejected before any exec."""
    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "kubernetes"},
        follow_redirects=False,
    )
    assert r.status_code == 422, r.text
    assert "mode must be one of" in r.json()["detail"]


@pytest.mark.asyncio
async def test_start_services_binary_missing_returns_502(client, monkeypatch):
    """Missing launcher binary surfaces a clean 502 rather than a 500."""
    _patch_exec(monkeypatch, captured=[], raise_fnf=True)

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["mode"] == "systemd"
    assert "not found" in body["detail"]


@pytest.mark.asyncio
async def test_start_services_timeout_returns_504(client, monkeypatch):
    """Hung launcher tripped by ``asyncio.wait_for`` returns 504."""
    from backend.routers import bootstrap as _br

    captured: list = []
    _patch_exec(monkeypatch, captured, hang=True)
    # Shrink the timeout so the test runs in <1s.
    monkeypatch.setattr(_br, "_START_TIMEOUT_SECS", 0.1)

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 504, r.text
    body = r.json()
    assert body["mode"] == "systemd"
    assert "did not finish" in body["detail"]


# ── audit ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_services_emits_audit_row(client, monkeypatch):
    """Every invocation lands in the audit log with mode + status."""
    from backend import audit

    _patch_exec(monkeypatch, captured=[], returncode=0, stdout=b"ok\n", stderr=b"")

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={"mode": "systemd"},
        follow_redirects=False,
    )
    assert r.status_code == 200

    rows = await audit.query(limit=50)
    ss_rows = [r for r in rows if r.get("action") == "bootstrap.start_services"]
    assert len(ss_rows) == 1
    after = ss_rows[0].get("after") or {}
    assert after.get("mode") == "systemd"
    assert after.get("status") == "started"
    assert after.get("returncode") == 0


# ── auto-detect via env override ──────────────────────────────────────


@pytest.mark.asyncio
async def test_start_services_env_override_picks_mode(client, monkeypatch):
    """``OMNISIGHT_DEPLOY_MODE`` env override is honoured when body omits mode."""
    captured: list = []
    _patch_exec(monkeypatch, captured, returncode=0)
    monkeypatch.setenv("OMNISIGHT_DEPLOY_MODE", "docker-compose")

    r = await client.post(
        "/api/v1/bootstrap/start-services",
        json={},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "docker-compose"
    assert captured[0][0:3] == ["docker", "compose", "-f"]


@pytest.mark.asyncio
async def test_detect_deploy_mode_falls_back_to_dev(monkeypatch, tmp_path):
    """With no systemctl / docker / container signals, the L7 probe returns dev."""
    from backend.routers import bootstrap as _br
    from backend import deploy_mode as _dm

    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    monkeypatch.setattr(_dm.shutil, "which", lambda _name: None)
    # Point every filesystem probe at a pristine tmp_path so nothing is present.
    monkeypatch.setattr(_dm, "_DOCKERENV_MARKER", tmp_path / "nope-dockerenv")
    monkeypatch.setattr(_dm, "_CGROUP_PATH", tmp_path / "nope-cgroup")
    monkeypatch.setattr(_dm, "_SYSTEMD_RUN_DIR", tmp_path / "nope-systemd")
    monkeypatch.setattr(_dm, "_DOCKER_SOCKET", tmp_path / "nope-docker.sock")
    assert _br._detect_deploy_mode() == "dev"


# ── per-mode argv shape (unit-level, no subprocess) ───────────────────


def test_start_command_systemd_is_sudo_wrapped():
    """systemd branch prefixes ``sudo -n`` so the K1 sudoers NOPASSWD grant
    keeps the launcher non-interactive — no TTY prompt if the rule is missing.
    """
    from backend.routers import bootstrap as _br

    cmd = _br._start_command("systemd", "")
    assert cmd[:4] == ["sudo", "-n", "systemctl", "start"]
    # Every declared unit is present, in order.
    assert cmd[4:] == list(_br._SYSTEMD_UNITS)


def test_start_command_docker_compose_defaults_prod_compose():
    """docker-compose branch defaults to ``docker-compose.prod.yml``."""
    from backend.routers import bootstrap as _br

    assert _br._start_command("docker-compose", "") == [
        "docker", "compose", "-f", "docker-compose.prod.yml", "up", "-d",
    ]


def test_start_command_docker_compose_honours_override():
    """Operator-supplied ``compose_file`` overrides the default."""
    from backend.routers import bootstrap as _br

    cmd = _br._start_command("docker-compose", "docker-compose.edge.yml")
    assert cmd == [
        "docker", "compose", "-f", "docker-compose.edge.yml", "up", "-d",
    ]


def test_start_command_dev_is_empty_noop():
    """dev branch returns ``[]`` so the HTTP handler short-circuits cleanly."""
    from backend.routers import bootstrap as _br

    assert _br._start_command("dev", "") == []
    # compose_file is ignored in dev mode — still empty argv.
    assert _br._start_command("dev", "docker-compose.edge.yml") == []


# ── K1 sudoers snippet generator ──────────────────────────────────────


def test_generate_sudoers_snippet_covers_every_unit():
    """Snippet names every unit in ``_SYSTEMD_UNITS`` with absolute path.

    sudo refuses a rule where the command is a bare name, so the snippet
    *must* use ``/usr/bin/systemctl``. The ``omnisight ALL=(root)``
    prefix mirrors the cloudflared sudoers pattern operators already
    ship in ``/etc/sudoers.d/omnisight-cloudflared``.
    """
    from backend.routers import bootstrap as _br

    snippet = _br.generate_sudoers_snippet()

    # Header comment so a future reader can grep from the sudoers.d file
    # back to this generator.
    assert snippet.startswith("# /etc/sudoers.d/omnisight-bootstrap")
    # Principal + NOPASSWD grant with absolute systemctl path.
    assert "omnisight ALL=(root) NOPASSWD:" in snippet
    for unit in _br._SYSTEMD_UNITS:
        assert f"/usr/bin/systemctl start {unit}" in snippet
    # Only "start" is granted — the wizard never stops or restarts, so
    # the sudoers grant must stay equally narrow (K1 least-privilege).
    assert " stop " not in snippet
    assert " restart " not in snippet


def test_generate_sudoers_snippet_uses_shared_sudoers_line():
    """The snippet body should be exactly ``SUDOERS_LINE`` + newline wrap.

    Keeping the two in lockstep lets operators import ``SUDOERS_LINE``
    from the module for programmatic checks (e.g. the setup wizard
    verifying the on-disk rule matches) without parsing the comment
    header out of :func:`generate_sudoers_snippet`.
    """
    from backend.routers import bootstrap as _br

    snippet = _br.generate_sudoers_snippet()
    assert _br.SUDOERS_LINE in snippet
    # Trailing newline is required or ``visudo`` chokes on EOF-without-newline.
    assert snippet.endswith("\n")
