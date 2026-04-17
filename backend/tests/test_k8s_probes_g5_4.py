"""G5 #4 — readiness / liveness probe wiring contract tests.

TODO row 1372:
    readiness/liveness probe 對接 G1 endpoint

Pins the probe stanzas added to ``deploy/k8s/10-deployment-backend.yaml``
that wire the K8s Deployment to the G1 health endpoints. Per the
charter (``docs/ops/orchestration_selection.md`` §7.3), three
commitments must hold:

    * ``readinessProbe`` uses ``httpGet`` against ``/readyz`` on the
      Deployment's named ``http`` port.
    * ``livenessProbe`` uses ``httpGet`` against ``/livez`` on the same
      named port. ``/livez`` is a byte-identical alias of
      ``/healthz`` added in G1 so the K8s probes can follow the charter
      spelling without introducing a separate code path.
    * Probes reach the G1 router — i.e., the backend actually serves
      these paths and returns the documented payloads. This is why we
      cross-assert against ``backend.routers.health`` rather than only
      YAML-shape-checking the manifest.

Sibling contracts (not asserted here — the row that owns each does
that):

    * G5 #2 row 1370 — Deployment / Service / Ingress / HPA shape lives
      in ``test_k8s_manifests_g5_2.py``.
    * G5 #3 row 1371 — PodDisruptionBudget lives in
      ``test_k8s_pdb_g5_3.py``.
    * G5 #5 row 1373 — Helm chart under ``deploy/helm/omnisight/``
      with split ``values-staging.yaml`` / ``values-prod.yaml``.
    * G5 #6 row 1374 — delivery bundle + kind 1.29 CI smoke.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = PROJECT_ROOT / "deploy" / "k8s"
DEPLOYMENT_PATH = K8S_DIR / "10-deployment-backend.yaml"
README_PATH = K8S_DIR / "README.md"
CHARTER = PROJECT_ROOT / "docs" / "ops" / "orchestration_selection.md"
HEALTH_ROUTER = PROJECT_ROOT / "backend" / "routers" / "health.py"
HEALTH_TEST = PROJECT_ROOT / "backend" / "tests" / "test_healthz_readyz.py"
TODO = PROJECT_ROOT / "TODO.md"
MAIN = PROJECT_ROOT / "backend" / "main.py"

READINESS_PATH = "/readyz"
LIVENESS_PATH = "/livez"
NAMED_PORT = "http"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, Mapping), f"{path.name}: top-level YAML must be a mapping"
    return dict(doc)


@pytest.fixture(scope="module")
def deployment_doc() -> dict[str, Any]:
    return _load(DEPLOYMENT_PATH)


@pytest.fixture(scope="module")
def backend_container(deployment_doc: dict[str, Any]) -> dict[str, Any]:
    containers = deployment_doc["spec"]["template"]["spec"]["containers"]
    assert isinstance(containers, list) and len(containers) == 1
    container = containers[0]
    assert container["name"] == "backend"
    return dict(container)


@pytest.fixture(scope="module")
def readiness_probe(backend_container: dict[str, Any]) -> dict[str, Any]:
    probe = backend_container.get("readinessProbe")
    assert isinstance(probe, Mapping), (
        "Deployment backend container must define readinessProbe (G5 #4)"
    )
    return dict(probe)


@pytest.fixture(scope="module")
def liveness_probe(backend_container: dict[str, Any]) -> dict[str, Any]:
    probe = backend_container.get("livenessProbe")
    assert isinstance(probe, Mapping), (
        "Deployment backend container must define livenessProbe (G5 #4)"
    )
    return dict(probe)


# ---------------------------------------------------------------------------
# TestReadinessProbeContract — /readyz wiring on the named port.
# ---------------------------------------------------------------------------
class TestReadinessProbeContract:
    def test_readiness_probe_present(
        self, backend_container: dict[str, Any]
    ) -> None:
        assert "readinessProbe" in backend_container

    def test_readiness_uses_httpget(self, readiness_probe: dict[str, Any]) -> None:
        # Charter §7.3 — httpGet only; exec / tcpSocket lose the payload
        # granularity that the G1 /readyz endpoint exposes.
        assert "httpGet" in readiness_probe
        assert "exec" not in readiness_probe
        assert "tcpSocket" not in readiness_probe

    def test_readiness_path_is_readyz(
        self, readiness_probe: dict[str, Any]
    ) -> None:
        assert readiness_probe["httpGet"]["path"] == READINESS_PATH

    def test_readiness_targets_named_port(
        self, readiness_probe: dict[str, Any]
    ) -> None:
        # Named port decouples the probe from the container port number
        # (G5 #5 Helm may override containerPort; the probe must track).
        assert readiness_probe["httpGet"]["port"] == NAMED_PORT

    def test_readiness_scheme_is_http(
        self, readiness_probe: dict[str, Any]
    ) -> None:
        # Scheme is explicit so a future TLS flip is a visible diff, not
        # a silent kubelet-default change.
        assert readiness_probe["httpGet"].get("scheme", "HTTP") == "HTTP"

    def test_readiness_initial_delay_bounded(
        self, readiness_probe: dict[str, Any]
    ) -> None:
        delay = readiness_probe.get("initialDelaySeconds", 0)
        # Short initial delay — the whole point of readiness is to mark
        # the pod NotReady quickly during drain. A >30s warmup would
        # leak stale replicas into the LB rotation.
        assert 0 <= delay <= 30

    def test_readiness_period_tight_enough_for_drain(
        self, readiness_probe: dict[str, Any]
    ) -> None:
        # G1 draining sets Retry-After: 30. We must detect drain well
        # inside that window: failureThreshold * periodSeconds < 30.
        period = readiness_probe.get("periodSeconds", 10)
        threshold = readiness_probe.get("failureThreshold", 3)
        assert period * threshold < 30, (
            f"readiness drain-detection window "
            f"{period * threshold}s must be < 30s G1 Retry-After"
        )

    def test_readiness_timeout_bounded(
        self, readiness_probe: dict[str, Any]
    ) -> None:
        timeout = readiness_probe.get("timeoutSeconds", 1)
        period = readiness_probe.get("periodSeconds", 10)
        # timeout ≤ period, otherwise overlapping probes queue up.
        assert 0 < timeout <= period

    def test_readiness_success_threshold_is_one(
        self, readiness_probe: dict[str, Any]
    ) -> None:
        # K8s only accepts successThreshold=1 on readinessProbe
        # (implicit default); explicitly setting anything else is
        # either illegal or a silent misconfiguration.
        threshold = readiness_probe.get("successThreshold", 1)
        assert threshold == 1


# ---------------------------------------------------------------------------
# TestLivenessProbeContract — /livez wiring on the named port.
# ---------------------------------------------------------------------------
class TestLivenessProbeContract:
    def test_liveness_probe_present(
        self, backend_container: dict[str, Any]
    ) -> None:
        assert "livenessProbe" in backend_container

    def test_liveness_uses_httpget(self, liveness_probe: dict[str, Any]) -> None:
        assert "httpGet" in liveness_probe
        assert "exec" not in liveness_probe
        assert "tcpSocket" not in liveness_probe

    def test_liveness_path_is_livez(
        self, liveness_probe: dict[str, Any]
    ) -> None:
        # Charter §7.3 commits K8s liveness to /livez (the G1 charter
        # spelling); /healthz remains available as an alias for compose
        # / systemd callers, but this manifest must use /livez.
        assert liveness_probe["httpGet"]["path"] == LIVENESS_PATH

    def test_liveness_targets_named_port(
        self, liveness_probe: dict[str, Any]
    ) -> None:
        assert liveness_probe["httpGet"]["port"] == NAMED_PORT

    def test_liveness_scheme_is_http(
        self, liveness_probe: dict[str, Any]
    ) -> None:
        assert liveness_probe["httpGet"].get("scheme", "HTTP") == "HTTP"

    def test_liveness_initial_delay_large_enough(
        self, liveness_probe: dict[str, Any]
    ) -> None:
        delay = liveness_probe.get("initialDelaySeconds", 0)
        # Liveness failures cause pod restart — must tolerate the
        # Python import + uvicorn bind warmup window. <5s would restart
        # cold-starting pods into an infinite loop.
        assert delay >= 5

    def test_liveness_restart_window_matches_g1_budget(
        self, liveness_probe: dict[str, Any]
    ) -> None:
        # G1 graceful shutdown grants in-flight work up to 30s.
        # Liveness must not restart a healthy-but-draining pod before
        # that budget expires — failureThreshold * periodSeconds >= 30s.
        period = liveness_probe.get("periodSeconds", 10)
        threshold = liveness_probe.get("failureThreshold", 3)
        assert period * threshold >= 30, (
            f"liveness restart window "
            f"{period * threshold}s must be >= 30s G1 shutdown budget"
        )

    def test_liveness_timeout_bounded(
        self, liveness_probe: dict[str, Any]
    ) -> None:
        timeout = liveness_probe.get("timeoutSeconds", 1)
        period = liveness_probe.get("periodSeconds", 10)
        assert 0 < timeout <= period


# ---------------------------------------------------------------------------
# TestProbePortResolvesToContainerPort — the named port both probes
# target must actually exist on the container, otherwise kubelet will
# silently skip the probe entirely (it becomes no-op).
# ---------------------------------------------------------------------------
class TestProbePortResolvesToContainerPort:
    def test_container_exposes_named_http_port(
        self, backend_container: dict[str, Any]
    ) -> None:
        ports = backend_container.get("ports", [])
        names = [p.get("name") for p in ports if isinstance(p, Mapping)]
        assert NAMED_PORT in names, (
            f"container must expose a port named {NAMED_PORT!r} for probes"
        )

    def test_named_port_is_8000(
        self, backend_container: dict[str, Any]
    ) -> None:
        # Named port 'http' must map to 8000 — the backend's uvicorn
        # listen port. Silent drift here would make probes pass while
        # the real traffic port is broken.
        ports = backend_container["ports"]
        http_port = next(p for p in ports if p["name"] == NAMED_PORT)
        assert http_port["containerPort"] == 8000

    def test_readiness_and_liveness_share_port(
        self,
        readiness_probe: dict[str, Any],
        liveness_probe: dict[str, Any],
    ) -> None:
        # Both probes hit the same backend; if they disagree on port,
        # something has been copy-pasted wrong.
        assert (
            readiness_probe["httpGet"]["port"]
            == liveness_probe["httpGet"]["port"]
        )


# ---------------------------------------------------------------------------
# TestG1EndpointsServeProbePaths — the two paths the probes target
# must actually be served by the G1 router module. Without this
# cross-check, a future G1 refactor could rename /livez or /readyz
# while the K8s manifest keeps pointing at the old path.
# ---------------------------------------------------------------------------
class TestG1EndpointsServeProbePaths:
    @pytest.fixture(scope="class")
    def health_router_source(self) -> str:
        assert HEALTH_ROUTER.is_file(), "G1 health router must exist"
        return HEALTH_ROUTER.read_text(encoding="utf-8")

    def test_readyz_route_declared_in_g1_router(
        self, health_router_source: str
    ) -> None:
        # The handler decorator is the truth source; we look for the
        # literal route declaration rather than the path string alone.
        assert '"/readyz"' in health_router_source

    def test_livez_route_declared_in_g1_router(
        self, health_router_source: str
    ) -> None:
        # /livez must be an actual route on the G1 probe router.
        # Without this, the K8s probe would 404 and the Deployment
        # would silently restart-loop.
        assert '"/livez"' in health_router_source

    def test_livez_delegates_to_healthz_handler(
        self, health_router_source: str
    ) -> None:
        # The G1 charter + K8s charter require /livez and /healthz to
        # return the same payload. Wiring /livez through the healthz()
        # handler is how we avoid payload drift between the two
        # spellings.
        import re

        livez_blocks = re.findall(
            r"def livez[a-z_]*\s*\([^)]*\)[^:]*:\s*(?:[^\n]*\n){0,8}",
            health_router_source,
        )
        assert livez_blocks, "livez handler(s) must exist in G1 router"
        for block in livez_blocks:
            assert "healthz" in block, (
                "livez handler must delegate to the healthz handler"
            )


# ---------------------------------------------------------------------------
# TestG1HealthTestCoverage — the G1 contract test file must cover the
# /livez alias so a future refactor can't drop it silently.
# ---------------------------------------------------------------------------
class TestG1HealthTestCoverage:
    @pytest.fixture(scope="class")
    def g1_test_source(self) -> str:
        assert HEALTH_TEST.is_file()
        return HEALTH_TEST.read_text(encoding="utf-8")

    def test_g1_tests_cover_livez_alias(self, g1_test_source: str) -> None:
        # Some assertion against /livez must exist in the G1 test file.
        assert "/livez" in g1_test_source or "livez(" in g1_test_source

    def test_g1_tests_cover_livez_draining_behavior(
        self, g1_test_source: str
    ) -> None:
        # Draining behavior for /livez matters: K8s must NOT restart a
        # draining pod (that would pull the carpet out from under
        # in-flight requests). The G1 test file must pin this.
        assert "livez_stays_200_while_draining" in g1_test_source


# ---------------------------------------------------------------------------
# TestMiddlewareExemptionsCoverLivez — /livez must be exempt from the
# same middleware gates that /healthz and /readyz are exempt from,
# otherwise the K8s probe will 503 during bootstrap / rate-limit /
# force-password-change / draining.
# ---------------------------------------------------------------------------
class TestMiddlewareExemptionsCoverLivez:
    @pytest.fixture(scope="class")
    def main_source(self) -> str:
        assert MAIN.is_file()
        return MAIN.read_text(encoding="utf-8")

    def test_rate_limit_exempt_includes_livez(self, main_source: str) -> None:
        # Rate-limit exemption is the same reason we exempt /readyz:
        # probes must not be denied when the backend is under pressure.
        assert "_RATE_LIMIT_EXEMPT" in main_source
        # Locate the specific set literal.
        import re

        match = re.search(
            r"_RATE_LIMIT_EXEMPT\s*=\s*\{[^}]*\}", main_source, re.DOTALL
        )
        assert match is not None
        assert '"/livez"' in match.group(0)

    def test_bootstrap_exempt_includes_livez(self, main_source: str) -> None:
        # The bootstrap redirect would otherwise 307 /livez into the
        # wizard and the probe would fail.
        import re

        match = re.search(
            r"_BOOTSTRAP_EXEMPT_REL\s*=\s*\{[^}]*\}", main_source, re.DOTALL
        )
        assert match is not None
        assert '"/livez"' in match.group(0)

    def test_graceful_shutdown_exempt_includes_livez(
        self, main_source: str
    ) -> None:
        # Liveness must keep answering 200 during graceful shutdown —
        # if it falls into the 503 gate, K8s restarts a draining pod.
        import re

        match = re.search(
            r"_GRACEFUL_SHUTDOWN_EXEMPT_RAW\s*=\s*\{[^}]*\}", main_source
        )
        assert match is not None
        assert '"/livez"' in match.group(0)

    def test_password_change_exempt_includes_livez(
        self, main_source: str
    ) -> None:
        # Consistent with /healthz / /readyz — no forced-password
        # redirect on probe endpoints.
        import re

        match = re.search(
            r"_PASSWORD_CHANGE_EXEMPT\s*=\s*\{[^}]*\}", main_source, re.DOTALL
        )
        assert match is not None
        assert '"/livez"' in match.group(0)


# ---------------------------------------------------------------------------
# TestCharterAlignment — the §7.3 charter commitment must match the
# manifest wiring, and vice versa.
# ---------------------------------------------------------------------------
class TestCharterAlignment:
    @pytest.fixture(scope="class")
    def charter_text(self) -> str:
        assert CHARTER.is_file(), "G5 #1 charter must exist"
        return CHARTER.read_text(encoding="utf-8")

    def test_charter_mentions_readyz(self, charter_text: str) -> None:
        assert "/readyz" in charter_text

    def test_charter_mentions_livez(self, charter_text: str) -> None:
        assert "/livez" in charter_text

    def test_charter_mentions_httpget(self, charter_text: str) -> None:
        # §7.3 literal: "via `httpGet` probes (not exec, not tcpSocket…)".
        assert "httpGet" in charter_text

    def test_charter_mentions_g5_4(self, charter_text: str) -> None:
        assert "G5 #4" in charter_text


# ---------------------------------------------------------------------------
# TestReadmeAlignment — the deploy/k8s/README.md must reflect the G5 #4
# wiring so operators see probe coverage without reading every manifest.
# ---------------------------------------------------------------------------
class TestReadmeAlignment:
    @pytest.fixture(scope="class")
    def readme_text(self) -> str:
        assert README_PATH.is_file()
        return README_PATH.read_text(encoding="utf-8")

    def test_readme_mentions_readyz(self, readme_text: str) -> None:
        assert "/readyz" in readme_text

    def test_readme_mentions_livez(self, readme_text: str) -> None:
        assert "/livez" in readme_text

    def test_readme_probes_no_longer_listed_as_not_included(
        self, readme_text: str
    ) -> None:
        # The pre-G5 #4 README listed probes under
        # "## Scope — what this bundle does NOT include". Landing G5 #4
        # must remove that bullet; leaving it there would mislead
        # operators into thinking probes are still pending.
        # We search for the literal bullet string that historically
        # sat in that section.
        assert (
            "Readiness / liveness probes → G5 #4" not in readme_text
        ), "README must remove the 'probes not included' bullet in G5 #4"

    def test_readme_mentions_httpget_wiring(self, readme_text: str) -> None:
        # Document that the probe mechanism is httpGet (not exec /
        # tcpSocket) so a future operator doesn't rip open the
        # manifest to check.
        assert "httpGet" in readme_text


# ---------------------------------------------------------------------------
# TestTodoRowMarker — the TODO row 1372 must be flipped to [x] so
# HANDOFF can trace the manifest back to the row. Anti-regression:
# reverting the Deployment edit without re-opening row 1372 fails here.
# ---------------------------------------------------------------------------
class TestTodoRowMarker:
    @pytest.fixture(scope="class")
    def todo_text(self) -> str:
        assert TODO.is_file()
        return TODO.read_text(encoding="utf-8")

    def test_row_1372_headline_present(self, todo_text: str) -> None:
        assert "readiness/liveness probe 對接 G1 endpoint" in todo_text

    def test_row_1372_marked_done(self, todo_text: str) -> None:
        assert (
            "- [x] readiness/liveness probe 對接 G1 endpoint" in todo_text
        )

    def test_row_1372_under_g5_section(self, todo_text: str) -> None:
        lines = todo_text.splitlines()
        g5_idx = next(
            (i for i, line in enumerate(lines) if "G5. HA-05" in line), None
        )
        assert g5_idx is not None
        row_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if "readiness/liveness probe 對接 G1 endpoint" in line
            ),
            None,
        )
        assert row_idx is not None
        assert row_idx > g5_idx


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — G5 #4 must not silently drag in G5
# #5 (Helm) / G5 #6 (CI smoke). If any of those land, it's a separate
# commit and this guard flips.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    # `test_no_helm_chart_dir_yet` previously here was removed in the
    # G5 #5 commit per the explicit-migration pattern (the Helm chart is
    # now pinned by test_helm_chart_g5_5.py).

    def test_no_nomad_or_swarm_manifests(self) -> None:
        # Charter §7.8 — Nomad / Swarm are out-of-scope for G5.
        assert not (PROJECT_ROOT / "deploy" / "nomad").exists()
        assert not (PROJECT_ROOT / "deploy" / "swarm").exists()

    def test_no_startup_probe_silent_creep(
        self, backend_container: dict[str, Any]
    ) -> None:
        # The charter §7.3 scopes G5 #4 to readiness + liveness only.
        # A startupProbe is a reasonable future addition but belongs in
        # its own row — adding it silently here blurs the scope.
        assert "startupProbe" not in backend_container
