"""W14.12 — full-lifecycle / resource-limit-hit / CF-Access-bypass tests.

Lands the W14 epic's verification row: drives the launcher / manager
/ idle-reaper / CF ingress / CF Access stack through three end-to-end
scenarios that the cumulative coverage of W14.1-W14.11 left implicit.

§A — **Lifecycle**: cold launch (with PEP HOLD auto-approved) →
``installing`` → ``ready`` → ``touch`` (bump ``last_request_at``) →
``stop`` (operator request) → cascaded CF ingress + CF Access cleanup.
Drives both the manager surface and the router surface so the wire
shape is pinned at both layers.

§B — **Resource limit hit**: the manager reads docker ``inspect`` on
the way out of ``stop()``, spots ``State.OOMKilled=True``, overrides
the caller-supplied reason with the W14.9 ``cgroup_oom`` literal and
appends the ``cgroup_oom_detected:`` warning. Beats both
``operator_request`` and ``idle_timeout`` reasons (kernel verdict
wins over caller guess). Exercises the W14.5 idle reaper → W14.9 OOM
detection cascade.

§C — **CF Access bypass attempts**: `jwt_claims_align_with_session`
rejects (1) email mismatch, (2) wrong audience, (3) wrong issuer,
(4) malformed token, (5) re-used token from another operator's
session, (6) AUD UUID minted for a sibling sandbox's app. Defence-
in-depth on top of CF edge signature verification — the CF edge
already drops unauthenticated requests; this row pins the **email-to-
session alignment** check that the W14.6 panel / W14.7 HMR proxy
will run on every downstream API call.

These tests reuse the W14.2 / W14.3 / W14.4 fakes
(``FakeDockerClient``, ``FakeCFIngressClient``, ``FakeCFAccessClient``)
from sibling test modules so the contract surface is byte-equivalent.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend import web_sandbox as ws_mod
from backend import web_sandbox_pep as _wsp
from backend import workspace as _ws
from backend.cf_access import (
    CFAccessConfig,
    CFAccessError,
    CFAccessManager,
    extract_jwt_claims,
    jwt_claims_align_with_session,
)
from backend.cf_ingress import (
    CFIngressConfig,
    CFIngressManager,
)
from backend.routers import web_sandbox as web_sandbox_router
from backend.tests.test_cf_access import (
    FakeCFAccessClient,
    _ok_config_kwargs as _ok_access_config_kwargs,
)
from backend.tests.test_cf_ingress import (
    FakeCFIngressClient,
    _ok_config_kwargs as _ok_ingress_config_kwargs,
)
from backend.tests.test_web_sandbox import (
    FakeClock,
    FakeDockerClient,
    RecordingEventCallback,
)
from backend.web_sandbox import (
    DEFAULT_RESOURCE_LIMITS,
    WebSandboxConfig,
    WebSandboxInstance,
    WebSandboxManager,
    WebSandboxStatus,
)
from backend.web_sandbox_idle_reaper import (
    IdleReaperConfig,
    WebSandboxIdleReaper,
)
from backend.web_sandbox_resource_limits import (
    CGROUP_OOM_REASON,
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEMORY_LIMIT_BYTES,
    DEFAULT_STORAGE_LIMIT_BYTES,
    WebPreviewResourceLimits,
)


# ───────────────────────────────────────────────────────────────────
#  Helpers — full-stack manager wiring
# ───────────────────────────────────────────────────────────────────


def _make_cf_ingress(
    *, ingress: list[dict[str, Any]] | None = None
) -> tuple[CFIngressManager, FakeCFIngressClient]:
    config = CFIngressConfig(**_ok_ingress_config_kwargs())
    fake = FakeCFIngressClient(ingress=ingress)
    return CFIngressManager(config=config, client=fake), fake


def _make_cf_access(
    *,
    default_emails: tuple[str, ...] = (),
    apps: list[dict[str, Any]] | None = None,
) -> tuple[CFAccessManager, FakeCFAccessClient]:
    config = CFAccessConfig(
        **_ok_access_config_kwargs(),
        default_emails=default_emails,
    )
    fake = FakeCFAccessClient(apps=apps)
    return CFAccessManager(config=config, client=fake), fake


def _make_full_manager(
    workspace: Path,
    *,
    docker: FakeDockerClient | None = None,
    clock: FakeClock | None = None,
    events: RecordingEventCallback | None = None,
    resource_limits: WebPreviewResourceLimits | None = None,
    cf_ingress_default_emails: tuple[str, ...] = (),
) -> tuple[
    WebSandboxManager,
    FakeDockerClient,
    FakeClock,
    RecordingEventCallback,
    FakeCFIngressClient,
    FakeCFAccessClient,
]:
    """Wire a manager with every W14 dependency populated.

    Returns the manager plus every fake the test might want to assert
    against. The CF Access manager is seeded with
    ``default_emails`` so launches with empty ``allowed_emails`` still
    have *some* email to allow (avoids the W14.4 ``cf_access_skipped``
    warning unless the caller deliberately wants it).
    """

    docker = docker if docker is not None else FakeDockerClient()
    clock = clock if clock is not None else FakeClock()
    events = events if events is not None else RecordingEventCallback()
    cf_ingress_mgr, cf_ingress_fake = _make_cf_ingress()
    cf_access_mgr, cf_access_fake = _make_cf_access(
        default_emails=cf_ingress_default_emails or ("admin@example.com",),
    )
    mgr = WebSandboxManager(
        docker_client=docker,
        clock=clock,
        event_cb=events,
        cf_ingress_manager=cf_ingress_mgr,
        cf_access_manager=cf_access_mgr,
        resource_limits=resource_limits,
    )
    return mgr, docker, clock, events, cf_ingress_fake, cf_access_fake


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwt(claims: dict[str, Any]) -> str:
    """Mint a fake CF Access JWT with the given claims block.

    Signature segment is opaque garbage — :func:`extract_jwt_claims`
    deliberately does not verify it (the helper trusts the CF edge to
    have done that). Header is the canonical CF Access shape.
    """

    header = _b64url(b'{"alg":"RS256","kid":"test-key"}')
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signature = _b64url(b"opaque-not-verified-by-extract_jwt_claims")
    return f"{header}.{payload}.{signature}"


# ───────────────────────────────────────────────────────────────────
#  Test app — router-level lifecycle wiring
# ───────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def reset_workspace_registry() -> Any:
    """Y6 #282 workspace registry is module-global — reset per test."""

    saved = dict(_ws._workspaces)
    _ws._workspaces.clear()
    yield
    _ws._workspaces.clear()
    _ws._workspaces.update(saved)


@pytest.fixture(autouse=True)
def stub_pep_evaluator() -> Any:
    """W14.8 — auto-approve every preview HOLD so lifecycle tests do
    not have to plumb a real propose_fn into every call site."""

    async def _auto_approve(**_kwargs: Any) -> _wsp.WebPreviewPepResult:
        return _wsp.WebPreviewPepResult(
            action="approved",
            reason="auto-approved by W14.12 stub",
            decision_id="stub-w14-12",
            rule="tier_unlisted",
        )

    web_sandbox_router.set_pep_evaluator_for_tests(_auto_approve)
    yield
    web_sandbox_router.set_pep_evaluator_for_tests(None)


@pytest.fixture
def lifecycle_app(workspace: Path, reset_workspace_registry: Any) -> tuple[
    TestClient,
    WebSandboxManager,
    FakeDockerClient,
    FakeCFIngressClient,
    FakeCFAccessClient,
]:
    """FastAPI TestClient + manager wired through the router with
    every W14 dependency (CF ingress + CF Access + resource limits)
    populated. Every endpoint runs as ``operator``."""

    mgr, docker, _, _, cf_ingress_fake, cf_access_fake = _make_full_manager(
        workspace,
    )
    app = FastAPI()
    app.include_router(web_sandbox_router.router)
    app.dependency_overrides[_au.require_operator] = lambda: _au.User(
        id="u-op",
        email="op@example.com",
        name="Op",
        role="operator",
    )
    app.dependency_overrides[_au.require_viewer] = lambda: _au.User(
        id="u-viewer",
        email="viewer@example.com",
        name="V",
        role="viewer",
    )
    app.dependency_overrides[web_sandbox_router.get_manager] = lambda: mgr
    return TestClient(app), mgr, docker, cf_ingress_fake, cf_access_fake


# ═══════════════════════════════════════════════════════════════════
#  §A — Full lifecycle
# ═══════════════════════════════════════════════════════════════════


def test_lifecycle_manager_cold_launch_pins_ingress_and_access(
    workspace: Path,
) -> None:
    """Cold launch with both CF managers wired in pins ``ingress_url``
    + ``access_app_id`` + ``preview_url`` + ``container_id`` and emits
    a single ``web_sandbox.launched`` event with all three populated."""

    mgr, docker, _, events, cf_ing, cf_acc = _make_full_manager(workspace)
    cfg = WebSandboxConfig(
        workspace_id="ws-life-1",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)
    assert inst.status is WebSandboxStatus.installing
    assert inst.container_id == "fake-cid-0001"
    assert inst.host_port is not None
    assert inst.preview_url is not None and inst.preview_url.endswith(
        f":{inst.host_port}/"
    )
    assert inst.ingress_url == f"https://preview-{inst.sandbox_id}.ai.sora-dev.app"
    assert inst.access_app_id is not None and inst.access_app_id.startswith("app-")
    assert inst.warnings == ()
    assert len(docker.run_calls) == 1
    assert cf_ing.gets >= 1 and len(cf_ing.puts) == 1
    assert len(cf_acc.create_calls) == 1
    types = [t for t, _ in events.events]
    assert types == ["web_sandbox.launched"]


def test_lifecycle_manager_full_path_through_stop(workspace: Path) -> None:
    """Drive launch → mark_ready → touch → stop and assert that every
    intermediate transition fires the right event + populates the
    right timestamps + cascades CF cleanup."""

    mgr, _, _, events, cf_ing, cf_acc = _make_full_manager(workspace)
    cfg = WebSandboxConfig(
        workspace_id="ws-life-2",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    launched = mgr.launch(cfg)
    assert launched.status is WebSandboxStatus.installing
    assert launched.started_at is not None

    ready = mgr.mark_ready("ws-life-2")
    assert ready.status is WebSandboxStatus.running
    assert ready.ready_at is not None and ready.ready_at >= launched.started_at

    bumped = mgr.touch("ws-life-2")
    assert bumped.last_request_at >= ready.last_request_at

    stopped = mgr.stop("ws-life-2", reason="operator_request")
    assert stopped.status is WebSandboxStatus.stopped
    assert stopped.stopped_at is not None
    assert stopped.killed_reason == "operator_request"
    # CF cleanup ran once each.
    cf_ing_puts_after = len(cf_ing.puts)
    cf_acc_deletes_after = len(cf_acc.delete_calls)
    assert cf_ing_puts_after == 2  # 1 create + 1 delete = 2 PUTs
    assert cf_acc_deletes_after == 1
    # Event sequence matches the lifecycle.
    types = [t for t, _ in events.events]
    assert types == [
        "web_sandbox.launched",
        "web_sandbox.ready",
        "web_sandbox.stopped",
    ]


def test_lifecycle_router_full_path_through_delete(
    lifecycle_app: tuple[
        TestClient,
        WebSandboxManager,
        FakeDockerClient,
        FakeCFIngressClient,
        FakeCFAccessClient,
    ],
    workspace: Path,
) -> None:
    """Same lifecycle as the manager-level test but driven entirely
    through the public REST surface. Pins the wire shape end-to-end."""

    client, mgr, docker, cf_ing, cf_acc = lifecycle_app
    # POST cold launch.
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-life-3",
            "workspace_path": str(workspace),
            "allowed_emails": ["alice@example.com"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == WebSandboxStatus.installing.value
    assert body["pep_decision_id"] == "stub-w14-12"
    assert body["ingress_url"] is not None
    assert body["access_app_id"] is not None and body["access_app_id"].startswith(
        "app-"
    )
    assert body["warnings"] == []

    # POST touch.
    resp = client.post("/web-sandbox/preview/ws-life-3/touch")
    assert resp.status_code == 200, resp.text
    touched = resp.json()
    assert touched["last_request_at"] >= body["last_request_at"]

    # POST ready.
    resp = client.post("/web-sandbox/preview/ws-life-3/ready")
    assert resp.status_code == 200, resp.text
    ready = resp.json()
    assert ready["status"] == WebSandboxStatus.running.value
    assert ready["ready_at"] is not None

    # GET snapshot.
    resp = client.get("/web-sandbox/preview/ws-life-3")
    assert resp.status_code == 200, resp.text
    snapshot = resp.json()
    assert snapshot["status"] == WebSandboxStatus.running.value

    # DELETE with explicit reason.
    resp = client.delete(
        "/web-sandbox/preview/ws-life-3",
        params={"reason": "operator_request"},
    )
    assert resp.status_code == 200, resp.text
    stopped = resp.json()
    assert stopped["status"] == WebSandboxStatus.stopped.value
    assert stopped["killed_reason"] == "operator_request"
    # CF cleanup wired in.
    assert len(cf_ing.puts) == 2  # 1 create + 1 delete
    assert len(cf_acc.delete_calls) == 1
    # docker stop + remove ran.
    assert len(docker.stop_calls) == 1
    assert len(docker.remove_calls) == 1


def test_lifecycle_idempotent_relaunch_skips_pep_and_docker(
    lifecycle_app: tuple[
        TestClient, WebSandboxManager, FakeDockerClient,
        FakeCFIngressClient, FakeCFAccessClient,
    ],
    workspace: Path,
) -> None:
    """W14.2 idempotency contract: re-POSTing the same workspace_id
    while still running returns the existing instance without
    triggering another docker run or another CF rule create."""

    client, _mgr, docker, cf_ing, cf_acc = lifecycle_app
    payload = {
        "workspace_id": "ws-life-4",
        "workspace_path": str(workspace),
        "allowed_emails": ["op@example.com"],
    }
    resp1 = client.post("/web-sandbox/preview", json=payload)
    assert resp1.status_code == 200, resp1.text
    sandbox_id_1 = resp1.json()["sandbox_id"]

    resp2 = client.post("/web-sandbox/preview", json=payload)
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["sandbox_id"] == sandbox_id_1
    # Docker only ran once.
    assert len(docker.run_calls) == 1
    # CF ingress only PUT once (one create).
    assert len(cf_ing.puts) == 1
    # CF Access only created once.
    assert len(cf_acc.create_calls) == 1


def test_lifecycle_relaunch_after_stop_creates_fresh_sandbox(
    lifecycle_app: tuple[
        TestClient, WebSandboxManager, FakeDockerClient,
        FakeCFIngressClient, FakeCFAccessClient,
    ],
    workspace: Path,
) -> None:
    """After DELETE, the next POST for the same workspace_id launches
    a fresh sandbox with a new sandbox_id. Audit history accumulates
    via separate sandbox_id values rather than overwriting."""

    client, _mgr, _docker, _cf_ing, _cf_acc = lifecycle_app
    payload = {
        "workspace_id": "ws-life-5",
        "workspace_path": str(workspace),
        "allowed_emails": ["op@example.com"],
    }
    first = client.post("/web-sandbox/preview", json=payload).json()
    client.delete(
        "/web-sandbox/preview/ws-life-5",
        params={"reason": "operator_request"},
    )
    second = client.post("/web-sandbox/preview", json=payload).json()
    assert second["sandbox_id"] == first["sandbox_id"]  # deterministic
    # But the new instance is fresh (status flipped back to installing).
    assert second["status"] == WebSandboxStatus.installing.value


def test_lifecycle_idle_reaper_stops_idle_sandbox_with_idle_timeout_reason(
    workspace: Path,
) -> None:
    """W14.5 idle reaper picks up a sandbox whose ``last_request_at``
    is older than the timeout and calls ``stop(reason="idle_timeout")``
    which cascades the W14.3 ingress + W14.4 access cleanup."""

    clock = FakeClock(start=1_000_000.0)
    mgr, _, _, _, cf_ing, cf_acc = _make_full_manager(workspace, clock=clock)
    cfg = WebSandboxConfig(
        workspace_id="ws-idle-1",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)
    assert inst.status is WebSandboxStatus.installing
    # Mark ready so the reaper sees a running sandbox.
    mgr.mark_ready("ws-idle-1")

    # Build a reaper with a tight 30s idle window so the test can
    # advance the (fake) clock past it without sleeping. The reaper
    # uses its own clock — wire one that returns "now" past the
    # timeout so select_idle_workspaces fires.
    reaper_clock = FakeClock(start=1_000_999.0)  # 999s ahead of launch
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=30.0, reap_interval_s=1.0),
        clock=reaper_clock,
    )
    result = reaper.tick()
    assert result.reaped == ("ws-idle-1",)
    assert result.errors == ()

    snapshot = mgr.get("ws-idle-1")
    assert snapshot is not None
    assert snapshot.status is WebSandboxStatus.stopped
    assert snapshot.killed_reason == "idle_timeout"
    # Cascaded CF cleanup.
    assert len(cf_ing.puts) == 2  # create + delete
    assert len(cf_acc.delete_calls) == 1


def test_lifecycle_list_excludes_stopped_after_terminal_transition(
    workspace: Path,
) -> None:
    """``manager.list()`` returns every instance the manager has ever
    seen — terminal rows are still part of the audit but ``is_running``
    is False, so the W14.6 panel can render history without confusing
    them with live sandboxes."""

    mgr, _, _, _, _, _ = _make_full_manager(workspace)
    cfg = WebSandboxConfig(
        workspace_id="ws-life-6",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    inst = mgr.launch(cfg)
    assert inst in mgr.list()
    mgr.stop("ws-life-6", reason="operator_request")
    after = mgr.list()
    assert len(after) == 1
    assert after[0].status is WebSandboxStatus.stopped
    assert not after[0].is_running


# ═══════════════════════════════════════════════════════════════════
#  §B — Resource limit hit (cgroup OOM detection)
# ═══════════════════════════════════════════════════════════════════


def test_resource_default_limits_match_row_spec(workspace: Path) -> None:
    """The W14.9 row spec literals (2 GiB / 1 CPU / 5 GiB) reach
    docker run as kwargs on every cold launch. Drift guard against
    the row spec being silently changed."""

    mgr, docker, _, _, _, _ = _make_full_manager(workspace)
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-1",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    call = docker.run_calls[0]
    assert call["memory_limit_bytes"] == DEFAULT_MEMORY_LIMIT_BYTES
    assert call["cpu_limit"] == DEFAULT_CPU_LIMIT
    assert call["storage_limit_bytes"] == DEFAULT_STORAGE_LIMIT_BYTES
    assert call["memory_swap_disabled"] is True


def test_resource_manager_policy_override_reaches_docker(workspace: Path) -> None:
    """Operator-policy resource_limits passed to the manager ctor
    flows through to docker run — per-launch override defaults to
    None so the manager policy wins."""

    custom = WebPreviewResourceLimits(
        memory_limit_bytes=1 * 1024 * 1024 * 1024,  # 1 GiB
        cpu_limit=0.5,
        storage_limit_bytes=2 * 1024 * 1024 * 1024,  # 2 GiB
    )
    mgr, docker, _, _, _, _ = _make_full_manager(
        workspace, resource_limits=custom
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-2",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    call = docker.run_calls[0]
    assert call["memory_limit_bytes"] == custom.memory_limit_bytes
    assert call["cpu_limit"] == custom.cpu_limit
    assert call["storage_limit_bytes"] == custom.storage_limit_bytes


def test_resource_per_launch_override_beats_manager_policy(
    workspace: Path,
) -> None:
    """``WebSandboxConfig.resource_limits`` (per-launch) beats the
    manager-level policy when both are set — W14.10 audit row needs
    to record the actual cgroup contract on this specific launch."""

    manager_default = WebPreviewResourceLimits(
        memory_limit_bytes=4 * 1024 * 1024 * 1024,
        cpu_limit=2.0,
    )
    per_launch = WebPreviewResourceLimits(
        memory_limit_bytes=512 * 1024 * 1024,
        cpu_limit=0.25,
    )
    mgr, docker, _, _, _, _ = _make_full_manager(
        workspace, resource_limits=manager_default
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-3",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
        resource_limits=per_launch,
    )
    mgr.launch(cfg)
    call = docker.run_calls[0]
    assert call["memory_limit_bytes"] == per_launch.memory_limit_bytes
    assert call["cpu_limit"] == per_launch.cpu_limit


def test_resource_oom_kill_overrides_caller_reason(workspace: Path) -> None:
    """W14.9 contract: docker reports OOMKilled=True ⇒ manager
    overrides the caller-supplied reason with ``cgroup_oom`` and
    appends the ``cgroup_oom_detected:`` warning. Operator's
    ``operator_request`` reason is the caller-side guess; kernel
    verdict wins."""

    docker = FakeDockerClient(
        inspect_payload={"State": {"OOMKilled": True, "ExitCode": 137}},
    )
    mgr, _, _, _, _, _ = _make_full_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-4",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    stopped = mgr.stop("ws-rl-4", reason="operator_request")
    assert stopped.killed_reason == CGROUP_OOM_REASON
    assert any(
        w.startswith("cgroup_oom_detected:") for w in stopped.warnings
    ), f"missing OOM warning: {stopped.warnings!r}"


def test_resource_oom_kill_overrides_idle_timeout_reason(workspace: Path) -> None:
    """The W14.5 idle reaper might call ``stop(reason="idle_timeout")``
    on a container that actually died of OOM minutes ago. The W14.9
    inspect-on-the-way-out check overrides ``idle_timeout`` with
    ``cgroup_oom`` so the audit row is honest about why it died."""

    docker = FakeDockerClient(
        inspect_payload={"State": {"OOMKilled": True}},
    )
    mgr, _, _, _, _, _ = _make_full_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-5",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    stopped = mgr.stop("ws-rl-5", reason="idle_timeout")
    assert stopped.killed_reason == CGROUP_OOM_REASON
    assert stopped.killed_reason != "idle_timeout"


def test_resource_oom_inspect_runs_before_docker_stop(workspace: Path) -> None:
    """Sequence assertion: the inspect call must happen BEFORE the
    docker stop+rm tears down the container — it is the only window
    where ``State.OOMKilled`` can still be read."""

    docker = FakeDockerClient(
        inspect_payload={"State": {"OOMKilled": True}},
    )
    mgr, _, _, _, _, _ = _make_full_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-6",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    mgr.stop("ws-rl-6", reason="operator_request")
    # inspect was called once on the way out of stop().
    assert len(docker.inspect_calls) == 1
    # docker stop ran after the inspect (we only know "stop ran" but
    # the FakeDockerClient does not raise on inspect, so the only way
    # killed_reason became cgroup_oom is if inspect read OOMKilled
    # before remove() destroyed the container).
    assert len(docker.stop_calls) == 1
    assert len(docker.remove_calls) == 1


def test_resource_no_oom_keeps_caller_reason(workspace: Path) -> None:
    """Negative path — when docker says ``OOMKilled=False`` the
    manager keeps the caller's reason. No false positives — better to
    record the caller's guess than fabricate a kernel event."""

    docker = FakeDockerClient(
        inspect_payload={"State": {"OOMKilled": False, "ExitCode": 0}},
    )
    mgr, _, _, _, _, _ = _make_full_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-7",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    stopped = mgr.stop("ws-rl-7", reason="operator_request")
    assert stopped.killed_reason == "operator_request"
    assert not any(
        w.startswith("cgroup_oom_detected:") for w in stopped.warnings
    )


def test_resource_oom_inspect_failure_falls_through_silently(
    workspace: Path,
) -> None:
    """When docker inspect raises (network blip, race with rm) the
    manager keeps the caller's reason. The OOM detection is best-
    effort by design."""

    class BoomDockerClient(FakeDockerClient):
        def inspect(self, container_id: str) -> dict[str, Any]:
            self.inspect_calls.append(container_id)
            raise RuntimeError("inspect_unavailable")

    docker = BoomDockerClient()
    mgr, _, _, _, _, _ = _make_full_manager(workspace, docker=docker)
    cfg = WebSandboxConfig(
        workspace_id="ws-rl-8",
        workspace_path=str(workspace),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    stopped = mgr.stop("ws-rl-8", reason="idle_timeout")
    assert stopped.killed_reason == "idle_timeout"
    # inspect was attempted (best-effort).
    assert len(docker.inspect_calls) == 1


def test_resource_router_delete_carries_cgroup_oom_in_response(
    lifecycle_app: tuple[
        TestClient, WebSandboxManager, FakeDockerClient,
        FakeCFIngressClient, FakeCFAccessClient,
    ],
    workspace: Path,
) -> None:
    """End-to-end through the router: when docker reports OOM, the
    DELETE response body carries ``killed_reason=cgroup_oom`` + the
    ``cgroup_oom_detected:`` warning. W14.6 panel reads these fields
    to render the right operator-facing copy."""

    client, _mgr, docker, _cf_ing, _cf_acc = lifecycle_app
    docker.inspect_payload = {"State": {"OOMKilled": True}}
    client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-rl-9",
            "workspace_path": str(workspace),
            "allowed_emails": ["op@example.com"],
        },
    )
    resp = client.delete(
        "/web-sandbox/preview/ws-rl-9",
        params={"reason": "operator_request"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["killed_reason"] == CGROUP_OOM_REASON
    assert any(w.startswith("cgroup_oom_detected:") for w in body["warnings"])


def test_resource_default_limits_singleton_pinned_to_row_spec() -> None:
    """``DEFAULT_RESOURCE_LIMITS`` (the module-level singleton used as
    the manager's default policy) matches the W14.9 row spec exactly.
    Drift guard against a quiet defaults change."""

    assert DEFAULT_RESOURCE_LIMITS.memory_limit_bytes == 2 * 1024 ** 3
    assert DEFAULT_RESOURCE_LIMITS.cpu_limit == 1.0
    assert DEFAULT_RESOURCE_LIMITS.storage_limit_bytes == 5 * 1024 ** 3


# ═══════════════════════════════════════════════════════════════════
#  §C — Cloudflare Access bypass attempts
# ═══════════════════════════════════════════════════════════════════


_OP_EMAIL = "op@example.com"
_APP_AUD = "11111111-1111-1111-1111-111111111111"
_TEAM_ISS = "https://acme.cloudflareaccess.com"


def _ok_jwt(
    *,
    email: str = _OP_EMAIL,
    aud: str | list[str] = _APP_AUD,
    iss: str = _TEAM_ISS,
    extra: dict[str, Any] | None = None,
) -> str:
    claims: dict[str, Any] = {"email": email, "aud": aud, "iss": iss}
    if extra:
        claims.update(extra)
    return _make_jwt(claims)


def test_bypass_happy_path_passes_alignment() -> None:
    """Sanity baseline: a well-formed token signed for the right app
    + with the right email + iss passes alignment. Confirms the test
    fixtures are correct before running negative cases."""

    claims = extract_jwt_claims(_ok_jwt())
    assert jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_email_mismatch_rejected() -> None:
    """Operator A's session presents operator B's JWT — alignment
    fails on the ``email`` check. This is the primary defence: even
    if CF edge issues a valid token to A, A cannot present it to
    impersonate B downstream."""

    claims = extract_jwt_claims(_ok_jwt(email="other@example.com"))
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_email_case_insensitive_stops_capitalisation_trick() -> None:
    """``Op@Example.COM`` vs ``op@example.com`` — case-insensitive
    comparison defeats the trivial casing bypass."""

    claims = extract_jwt_claims(_ok_jwt(email="OP@EXAMPLE.COM"))
    assert jwt_claims_align_with_session(
        claims,
        session_email="op@example.com",
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_missing_email_claim_rejected() -> None:
    """A JWT with no ``email`` claim cannot be aligned — defence
    against IdPs that don't issue email."""

    bad = _make_jwt({"aud": _APP_AUD, "iss": _TEAM_ISS})  # no email
    claims = extract_jwt_claims(bad)
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_empty_session_email_rejected() -> None:
    """Even a valid JWT cannot align with an empty session — defence
    against unauthenticated callers passing through a misconfigured
    session middleware."""

    claims = extract_jwt_claims(_ok_jwt())
    assert not jwt_claims_align_with_session(
        claims,
        session_email="",
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_wrong_audience_string_rejected() -> None:
    """A JWT minted for sandbox A's CF Access app (``aud=A_UUID``)
    presented to sandbox B's gate (``expected_aud=B_UUID``) is
    rejected. This is the cross-sandbox bypass defence."""

    claims = extract_jwt_claims(
        _ok_jwt(aud="22222222-2222-2222-2222-222222222222")
    )
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_wrong_audience_in_list_rejected() -> None:
    """Same defence when CF Access issues an aud-as-list token —
    the expected aud must be present in the list, otherwise reject."""

    claims = extract_jwt_claims(
        _ok_jwt(
            aud=[
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            ]
        )
    )
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_audience_in_list_allowed_when_present() -> None:
    """Positive control for the list-aud path: when the expected aud
    IS in the list, alignment passes."""

    claims = extract_jwt_claims(
        _ok_jwt(
            aud=[
                "22222222-2222-2222-2222-222222222222",
                _APP_AUD,
            ]
        )
    )
    assert jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_missing_aud_claim_rejected_when_aud_expected() -> None:
    """If the verifier expects an ``aud`` but the JWT carries none,
    reject. CF Access tokens always carry ``aud`` so a missing claim
    is a strong signal of a forged or unrelated token."""

    claims = extract_jwt_claims(_make_jwt({"email": _OP_EMAIL, "iss": _TEAM_ISS}))
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_wrong_issuer_rejected() -> None:
    """A JWT minted by another team's CF Access (different
    ``team_domain`` ⇒ different ``iss``) is rejected. Defence against
    multi-tenant CF Access account spillover."""

    claims = extract_jwt_claims(
        _ok_jwt(iss="https://other-team.cloudflareaccess.com")
    )
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_missing_issuer_rejected_when_iss_expected() -> None:
    """If the verifier expects an ``iss`` but the JWT carries none,
    reject. CF Access tokens always carry ``iss``."""

    claims = extract_jwt_claims(
        _make_jwt({"email": _OP_EMAIL, "aud": _APP_AUD})
    )
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_malformed_token_one_segment_rejected() -> None:
    """A non-JWT string is rejected by ``extract_jwt_claims``."""

    with pytest.raises(CFAccessError):
        extract_jwt_claims("not-a-jwt")


def test_bypass_malformed_token_two_segments_rejected() -> None:
    """A two-segment token (header.payload, no signature) is rejected
    — defence against truncated tokens leaking through."""

    with pytest.raises(CFAccessError):
        extract_jwt_claims("aGVhZGVy.cGF5bG9hZA")


def test_bypass_malformed_token_non_base64_payload_rejected() -> None:
    """The middle segment must be base64url. Non-base64 payload is
    rejected."""

    with pytest.raises(CFAccessError):
        extract_jwt_claims("aGVhZGVy.???.c2ln")


def test_bypass_malformed_token_non_json_payload_rejected() -> None:
    """The decoded payload must be a JSON object. Non-JSON or non-
    object payload is rejected."""

    with pytest.raises(CFAccessError):
        extract_jwt_claims(
            f"aGVhZGVy.{_b64url(b'not json')}.c2ln"
        )


def test_bypass_empty_token_rejected() -> None:
    """An empty / whitespace token (e.g. operator forgot the
    ``Cf-Access-Jwt-Assertion`` header) is rejected."""

    with pytest.raises(CFAccessError):
        extract_jwt_claims("")
    with pytest.raises(CFAccessError):
        extract_jwt_claims("   ")


def test_bypass_replay_across_operators_caught_by_email_check() -> None:
    """End-to-end attack scenario: operator A captures their own JWT
    (e.g. via browser DevTools), forwards it to operator B's session
    (via XSS, leaked tab, MITM upstream of CF). The email check on
    B's side rejects the replay."""

    op_a_token = _ok_jwt(email="alice@example.com")
    claims = extract_jwt_claims(op_a_token)
    # Operator B's session is bob@example.com — same team, same app,
    # same iss. The only difference is the email claim.
    assert not jwt_claims_align_with_session(
        claims,
        session_email="bob@example.com",
        expected_aud=_APP_AUD,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_replay_across_sandboxes_caught_by_aud_check() -> None:
    """End-to-end attack scenario: operator A holds a valid JWT for
    sandbox X (``aud=X_UUID``). A presents it to sandbox Y's gate
    (``expected_aud=Y_UUID``) hoping to reuse the session. The aud
    check rejects."""

    sandbox_x_aud = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sandbox_y_aud = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    claims = extract_jwt_claims(_ok_jwt(aud=sandbox_x_aud))
    assert not jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=sandbox_y_aud,
        expected_iss=_TEAM_ISS,
    )


def test_bypass_empty_emails_does_not_lock_out_silently(
    workspace: Path,
) -> None:
    """W14.4 defensive design: when ``allowed_emails`` is empty AND
    the manager has no default emails, launch surfaces a
    ``cf_access_skipped`` warning rather than POSTing an empty-include
    policy that would lock everyone out. A locked-out preview is
    indistinguishable from a successful preview from the operator's
    point of view — the warning forces visibility."""

    # Manager wired with a CF Access manager BUT no default_emails.
    config = CFAccessConfig(
        **_ok_access_config_kwargs(),
        default_emails=(),  # explicitly empty
    )
    cf_access_fake = FakeCFAccessClient()
    cf_access_mgr = CFAccessManager(config=config, client=cf_access_fake)
    cf_ingress_mgr, _ = _make_cf_ingress()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_ingress_mgr,
        cf_access_manager=cf_access_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-bypass-empty",
        workspace_path=str(workspace),
        allowed_emails=(),  # no caller emails either
    )
    inst = mgr.launch(cfg)
    # No CF Access app was created.
    assert inst.access_app_id is None
    assert len(cf_access_fake.create_calls) == 0
    # Warning surfaces the skip.
    assert any(
        w.startswith("cf_access_skipped:") for w in inst.warnings
    ), f"missing skip warning: {inst.warnings!r}"


def test_bypass_jwt_claims_non_mapping_rejected() -> None:
    """``jwt_claims_align_with_session`` rejects non-mapping claims —
    defence against callers passing through raw JSON arrays / strings
    / None."""

    assert not jwt_claims_align_with_session(
        "not-a-dict",  # type: ignore[arg-type]
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
    )
    assert not jwt_claims_align_with_session(
        None,  # type: ignore[arg-type]
        session_email=_OP_EMAIL,
        expected_aud=_APP_AUD,
    )


def test_bypass_aud_check_skipped_when_expected_aud_none() -> None:
    """When the verifier passes ``expected_aud=None`` it deliberately
    declines to enforce the audience check (e.g. tests / triage paths).
    Drift guard: this is a tri-state and ``None`` must continue to
    mean "skip" rather than "must be missing"."""

    claims = extract_jwt_claims(
        _ok_jwt(aud="22222222-2222-2222-2222-222222222222")
    )
    assert jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=None,
        expected_iss=None,
    )


def test_bypass_iss_check_skipped_when_expected_iss_none() -> None:
    """Same tri-state for ``expected_iss``. Defence against the
    "absence of expectation must not be confused with absence of
    claim" trap."""

    claims = extract_jwt_claims(
        _ok_jwt(iss="https://other-team.cloudflareaccess.com")
    )
    assert jwt_claims_align_with_session(
        claims,
        session_email=_OP_EMAIL,
        expected_aud=None,
        expected_iss=None,
    )


# ═══════════════════════════════════════════════════════════════════
#  §D — Schema / drift guards
# ═══════════════════════════════════════════════════════════════════


def test_w14_12_module_schema_version_pinned() -> None:
    """The web_sandbox + resource limits + cf_access schema versions
    are 1.0.0 (pinned across W14.x). Drift guard against a quiet
    minor bump that would silently break wire compat with W14.6."""

    assert ws_mod.WEB_SANDBOX_SCHEMA_VERSION == "1.0.0"


def test_w14_12_cgroup_oom_reason_string_pinned() -> None:
    """``cgroup_oom`` is the literal the W14.10 audit row queries on
    (`SELECT count(*) WHERE killed_reason = 'cgroup_oom'`). Renaming
    breaks the risk register query templates in W14.11. Drift guard."""

    assert CGROUP_OOM_REASON == "cgroup_oom"


def test_w14_12_warning_prefixes_pinned(workspace: Path) -> None:
    """The exact warning prefixes documented in W14.11's threat model
    must remain stable so operator triage runbooks keep matching.
    This test exercises three of them in one go."""

    # Path 1: cf_access_skipped (no emails to allow).
    config = CFAccessConfig(
        **_ok_access_config_kwargs(),
        default_emails=(),
    )
    cf_acc_fake = FakeCFAccessClient()
    cf_acc_mgr = CFAccessManager(config=config, client=cf_acc_fake)
    cf_ing_mgr, _ = _make_cf_ingress()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(
            inspect_payload={"State": {"OOMKilled": True}},
        ),
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
        cf_ingress_manager=cf_ing_mgr,
        cf_access_manager=cf_acc_mgr,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-warn-1",
        workspace_path=str(workspace),
    )
    inst = mgr.launch(cfg)
    # cf_access_skipped warning fired on launch.
    assert any(w.startswith("cf_access_skipped:") for w in inst.warnings)

    # Path 2: cgroup_oom_detected fires on stop().
    stopped = mgr.stop("ws-warn-1", reason="operator_request")
    assert any(
        w.startswith("cgroup_oom_detected:") for w in stopped.warnings
    )

    # Path 3: killed_reason override is the cgroup_oom literal.
    assert stopped.killed_reason == CGROUP_OOM_REASON


def test_w14_12_instance_to_dict_carries_lifecycle_fields() -> None:
    """``WebSandboxInstance.to_dict()`` carries every field the W14.6
    panel + W14.10 audit row need. Drift guard on the JSON wire
    shape — silent drops would break the frontend's status branch."""

    cfg = WebSandboxConfig(
        workspace_id="ws-dict-1",
        workspace_path="/tmp",
    )
    inst = WebSandboxInstance(
        workspace_id="ws-dict-1",
        sandbox_id="ws-deadbeef",
        container_name="omnisight-web-preview-ws-deadbeef",
        config=cfg,
    )
    d = inst.to_dict()
    expected_keys = {
        "schema_version",
        "workspace_id",
        "sandbox_id",
        "container_name",
        "config",
        "status",
        "container_id",
        "host_port",
        "preview_url",
        "ingress_url",
        "access_app_id",
        "created_at",
        "started_at",
        "ready_at",
        "stopped_at",
        "last_request_at",
        "error",
        "killed_reason",
        "warnings",
    }
    assert expected_keys.issubset(d.keys()), (
        f"missing keys: {expected_keys - d.keys()}"
    )
