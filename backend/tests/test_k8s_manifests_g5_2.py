"""G5 #2 — Kubernetes manifest contract tests.

TODO row 1370:
    Manifests：Deployment（replicas=2, maxUnavailable=0）、Service、
    Ingress、HPA（CPU 70%）

Pins the plain-YAML K8s manifests under ``deploy/k8s/`` that take
OmniSight from a single-host compose deployment to a multi-node
orchestrated one. Per the G5 #1 charter
(``docs/ops/orchestration_selection.md`` §7), five commitments must
hold across these files:

    * Deployment replicas=2 with RollingUpdate + maxUnavailable=0
      + maxSurge=1 — the only strategy delivering zero-downtime
      rollouts on a 2-replica backend.
    * Service ClusterIP backed by the Deployment's pod selector.
    * Ingress default ingressClassName=nginx; Gateway-API toggled
      by the G5 #5 Helm chart, not silent auto-detection here.
    * HPA apiVersion=autoscaling/v2 (NOT v2beta2 — that was removed
      in K8s 1.26) with targetCPUUtilizationPercentage=70.
    * All manifests in the ``omnisight`` namespace with consistent
      ``app.kubernetes.io/*`` recommended labels.

Sibling contracts (not asserted here — the row that owns each does
that):

    * G5 #3 row 1371 — PodDisruptionBudget (minAvailable=1).
    * G5 #4 row 1372 — readiness/liveness probe wiring to /readyz
      + /livez via httpGet.
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
CHARTER = PROJECT_ROOT / "docs" / "ops" / "orchestration_selection.md"

NAMESPACE_PATH = K8S_DIR / "00-namespace.yaml"
DEPLOYMENT_PATH = K8S_DIR / "10-deployment-backend.yaml"
SERVICE_PATH = K8S_DIR / "20-service-backend.yaml"
INGRESS_PATH = K8S_DIR / "30-ingress.yaml"
HPA_PATH = K8S_DIR / "40-hpa-backend.yaml"
README_PATH = K8S_DIR / "README.md"

ALL_MANIFEST_PATHS = [
    NAMESPACE_PATH,
    DEPLOYMENT_PATH,
    SERVICE_PATH,
    INGRESS_PATH,
    HPA_PATH,
]

EXPECTED_NAMESPACE = "omnisight"
BACKEND_NAME = "omnisight-backend"
BACKEND_PORT = 8000
SERVICE_PORT = 80
EXPECTED_CPU_TARGET = 70
EXPECTED_REPLICAS = 2


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, Mapping), f"{path.name}: top-level YAML must be a mapping"
    return dict(doc)


@pytest.fixture(scope="module")
def namespace_doc() -> dict[str, Any]:
    return _load(NAMESPACE_PATH)


@pytest.fixture(scope="module")
def deployment_doc() -> dict[str, Any]:
    return _load(DEPLOYMENT_PATH)


@pytest.fixture(scope="module")
def service_doc() -> dict[str, Any]:
    return _load(SERVICE_PATH)


@pytest.fixture(scope="module")
def ingress_doc() -> dict[str, Any]:
    return _load(INGRESS_PATH)


@pytest.fixture(scope="module")
def hpa_doc() -> dict[str, Any]:
    return _load(HPA_PATH)


# ---------------------------------------------------------------------------
# TestK8sManifestFilesShape — bundle presence + YAML parse + lexical apply order
# ---------------------------------------------------------------------------
class TestK8sManifestFilesShape:
    def test_k8s_directory_exists(self) -> None:
        assert K8S_DIR.is_dir(), "deploy/k8s/ directory must exist (G5 #2)"

    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS, ids=lambda p: p.name)
    def test_manifest_file_exists(self, path: Path) -> None:
        assert path.is_file(), f"{path.relative_to(PROJECT_ROOT)} must be tracked"

    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS, ids=lambda p: p.name)
    def test_manifest_file_is_yaml_extension(self, path: Path) -> None:
        assert path.suffix == ".yaml"

    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS, ids=lambda p: p.name)
    def test_manifest_yaml_parses(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        assert isinstance(doc, Mapping)

    def test_manifest_filenames_sorted_apply_order(self) -> None:
        files = sorted(K8S_DIR.glob("*.yaml"))
        # Namespace must be the first YAML kubectl apply -f walks so that
        # every namespaced object below can land into it.
        assert files[0].name == NAMESPACE_PATH.name
        # Backend stack order: namespace → deploy → service → ingress → hpa.
        expected = [
            NAMESPACE_PATH.name,
            DEPLOYMENT_PATH.name,
            SERVICE_PATH.name,
            INGRESS_PATH.name,
            HPA_PATH.name,
        ]
        actual = [f.name for f in files]
        assert actual == expected

    def test_readme_exists_and_mentions_charter(self) -> None:
        assert README_PATH.is_file()
        body = README_PATH.read_text(encoding="utf-8")
        assert "orchestration_selection.md" in body
        assert "kind" in body.lower()

    def test_readme_documents_scope_exclusions(self) -> None:
        body = README_PATH.read_text(encoding="utf-8")
        # Sibling rows must be mentioned so future readers know where
        # each adjacent concern lives.
        for needle in ("G5 #3", "G5 #4", "G5 #5", "G5 #6"):
            assert needle in body, f"README must cross-reference {needle}"

    def test_readme_forbids_silent_nomad_swarm(self) -> None:
        body = README_PATH.read_text(encoding="utf-8")
        lower = body.lower()
        # charter §7.8 says Nomad / Swarm are out-of-scope — the README
        # must acknowledge so a drive-by PR doesn't sneak a deploy/nomad/
        # into the same bundle.
        assert "out of scope" in lower or "out-of-scope" in lower
        assert "deploy/nomad/" in body or "nomad" in lower
        assert "deploy/swarm/" in body or "swarm" in lower


# ---------------------------------------------------------------------------
# TestNamespaceContract
# ---------------------------------------------------------------------------
class TestNamespaceContract:
    def test_kind_is_namespace(self, namespace_doc: dict[str, Any]) -> None:
        assert namespace_doc["kind"] == "Namespace"

    def test_api_version_v1(self, namespace_doc: dict[str, Any]) -> None:
        assert namespace_doc["apiVersion"] == "v1"

    def test_namespace_name(self, namespace_doc: dict[str, Any]) -> None:
        assert namespace_doc["metadata"]["name"] == EXPECTED_NAMESPACE

    def test_namespace_recommended_labels(self, namespace_doc: dict[str, Any]) -> None:
        labels = namespace_doc["metadata"].get("labels", {})
        assert labels.get("app.kubernetes.io/part-of") == "omnisight"
        assert labels.get("app.kubernetes.io/name") == "omnisight"


# ---------------------------------------------------------------------------
# TestDeploymentContract
# ---------------------------------------------------------------------------
class TestDeploymentContract:
    def test_kind_is_deployment(self, deployment_doc: dict[str, Any]) -> None:
        assert deployment_doc["kind"] == "Deployment"

    def test_api_version_apps_v1(self, deployment_doc: dict[str, Any]) -> None:
        # apps/v1 has been stable since 1.9 and is the only surviving
        # wire form today — no apps/v1beta1 or apps/v1beta2.
        assert deployment_doc["apiVersion"] == "apps/v1"

    def test_deployment_namespace(self, deployment_doc: dict[str, Any]) -> None:
        assert deployment_doc["metadata"]["namespace"] == EXPECTED_NAMESPACE

    def test_deployment_name(self, deployment_doc: dict[str, Any]) -> None:
        assert deployment_doc["metadata"]["name"] == BACKEND_NAME

    def test_replicas_is_two(self, deployment_doc: dict[str, Any]) -> None:
        # TODO row 1370 literal: replicas=2.
        assert deployment_doc["spec"]["replicas"] == EXPECTED_REPLICAS

    def test_strategy_is_rolling_update(self, deployment_doc: dict[str, Any]) -> None:
        strategy = deployment_doc["spec"]["strategy"]
        assert strategy["type"] == "RollingUpdate"

    def test_max_unavailable_is_zero(self, deployment_doc: dict[str, Any]) -> None:
        # Charter §7.4: zero-downtime rollout on 2-replica backend.
        rolling = deployment_doc["spec"]["strategy"]["rollingUpdate"]
        assert rolling["maxUnavailable"] == 0

    def test_max_surge_is_one(self, deployment_doc: dict[str, Any]) -> None:
        # Charter §7.4: one extra pod comes up before any existing pod
        # terminates. Together with maxUnavailable=0 this is the zero-
        # downtime lock.
        rolling = deployment_doc["spec"]["strategy"]["rollingUpdate"]
        assert rolling["maxSurge"] == 1

    def test_selector_match_labels_present(self, deployment_doc: dict[str, Any]) -> None:
        selector = deployment_doc["spec"]["selector"]["matchLabels"]
        assert selector.get("app.kubernetes.io/name") == BACKEND_NAME
        assert selector.get("app.kubernetes.io/component") == "backend"

    def test_pod_template_labels_match_selector(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        # If the pod template labels drift from the Deployment selector
        # K8s silently refuses to adopt the pod — contract must hold.
        selector = deployment_doc["spec"]["selector"]["matchLabels"]
        pod_labels = deployment_doc["spec"]["template"]["metadata"]["labels"]
        for key, value in selector.items():
            assert pod_labels.get(key) == value, (
                f"pod template label {key} must equal selector {value}"
            )

    def test_container_single(self, deployment_doc: dict[str, Any]) -> None:
        containers = deployment_doc["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 1

    def test_container_name_backend(self, deployment_doc: dict[str, Any]) -> None:
        container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
        assert container["name"] == "backend"

    def test_container_port_8000_named_http(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
        ports = container["ports"]
        assert len(ports) >= 1
        http_ports = [p for p in ports if p.get("name") == "http"]
        assert len(http_ports) == 1, "exactly one port named 'http' required"
        assert http_ports[0]["containerPort"] == BACKEND_PORT
        assert http_ports[0]["protocol"] == "TCP"

    def test_image_is_ghcr_omnisight_backend(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        # Charter + README commit to the ghcr.io/<ns>/omnisight-backend
        # image name (kept in sync with docker-compose.prod.yml).
        container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
        image = container["image"]
        assert "omnisight-backend" in image
        assert image.startswith("ghcr.io/")

    def test_resources_requests_cpu_present(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        # HPA CPU % requires a CPU request. Without it HPA silently
        # stays at minReplicas — this test is a guardrail.
        container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
        requests = container["resources"]["requests"]
        assert "cpu" in requests, "container must declare resources.requests.cpu"
        assert requests["cpu"], "resources.requests.cpu must be non-empty"

    def test_resources_requests_memory_present(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
        assert "memory" in container["resources"]["requests"]

    def test_revision_history_limit_bounded(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        # Default is 10; bounding it keeps old ReplicaSets from
        # accumulating between rolling restarts.
        limit = deployment_doc["spec"].get("revisionHistoryLimit")
        assert isinstance(limit, int) and 1 <= limit <= 10

    def test_instance_id_env_from_pod_name(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        # OMNISIGHT_INSTANCE_ID is consumed by Prometheus scraping. In
        # compose it's the static 'backend-a' / 'backend-b'; in K8s
        # we use the pod name (downward API) to stay per-replica unique.
        container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
        env = container.get("env", [])
        inst = [e for e in env if e.get("name") == "OMNISIGHT_INSTANCE_ID"]
        assert len(inst) == 1
        ref = inst[0].get("valueFrom", {}).get("fieldRef", {})
        assert ref.get("fieldPath") == "metadata.name"


# ---------------------------------------------------------------------------
# TestServiceContract
# ---------------------------------------------------------------------------
class TestServiceContract:
    def test_kind_is_service(self, service_doc: dict[str, Any]) -> None:
        assert service_doc["kind"] == "Service"

    def test_api_version_v1(self, service_doc: dict[str, Any]) -> None:
        assert service_doc["apiVersion"] == "v1"

    def test_service_namespace(self, service_doc: dict[str, Any]) -> None:
        assert service_doc["metadata"]["namespace"] == EXPECTED_NAMESPACE

    def test_service_name(self, service_doc: dict[str, Any]) -> None:
        assert service_doc["metadata"]["name"] == BACKEND_NAME

    def test_service_type_cluster_ip(self, service_doc: dict[str, Any]) -> None:
        # Ingress-fronted: no NodePort / LoadBalancer escape hatch here.
        assert service_doc["spec"]["type"] == "ClusterIP"

    def test_service_port_80(self, service_doc: dict[str, Any]) -> None:
        ports = service_doc["spec"]["ports"]
        http_ports = [p for p in ports if p.get("name") == "http"]
        assert len(http_ports) == 1
        assert http_ports[0]["port"] == SERVICE_PORT

    def test_service_target_port_is_named_http(
        self, service_doc: dict[str, Any]
    ) -> None:
        # Indirection via named port — G5 #5 Helm can change the
        # container port without editing the Service manifest.
        http_port = [p for p in service_doc["spec"]["ports"] if p["name"] == "http"][0]
        assert http_port["targetPort"] == "http"

    def test_service_protocol_tcp(self, service_doc: dict[str, Any]) -> None:
        http_port = [p for p in service_doc["spec"]["ports"] if p["name"] == "http"][0]
        assert http_port.get("protocol", "TCP") == "TCP"

    def test_service_selector_matches_deployment(
        self, service_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # If the Service selector drifts from the Deployment pod labels
        # the Service has zero endpoints — contract-hard.
        svc_selector = service_doc["spec"]["selector"]
        pod_labels = deployment_doc["spec"]["template"]["metadata"]["labels"]
        for key, value in svc_selector.items():
            assert pod_labels.get(key) == value, (
                f"Service selector {key}={value} must match Deployment pod label"
            )


# ---------------------------------------------------------------------------
# TestIngressContract
# ---------------------------------------------------------------------------
class TestIngressContract:
    def test_kind_is_ingress(self, ingress_doc: dict[str, Any]) -> None:
        assert ingress_doc["kind"] == "Ingress"

    def test_api_version_networking_v1(self, ingress_doc: dict[str, Any]) -> None:
        # networking.k8s.io/v1 is the stable form since 1.19 and the
        # only one on K8s 1.29 (v1beta1 was removed in 1.22).
        assert ingress_doc["apiVersion"] == "networking.k8s.io/v1"

    def test_ingress_namespace(self, ingress_doc: dict[str, Any]) -> None:
        assert ingress_doc["metadata"]["namespace"] == EXPECTED_NAMESPACE

    def test_ingress_name(self, ingress_doc: dict[str, Any]) -> None:
        assert ingress_doc["metadata"]["name"] == BACKEND_NAME

    def test_ingress_class_default_nginx(self, ingress_doc: dict[str, Any]) -> None:
        # Charter §7.6 — default is `ingressClassName: nginx`. Gateway-
        # API is a Helm toggle in G5 #5, not silent auto-detection.
        assert ingress_doc["spec"]["ingressClassName"] == "nginx"

    def test_ingress_has_at_least_one_rule(self, ingress_doc: dict[str, Any]) -> None:
        rules = ingress_doc["spec"].get("rules", [])
        assert len(rules) >= 1

    def test_ingress_path_prefix_root(self, ingress_doc: dict[str, Any]) -> None:
        rule = ingress_doc["spec"]["rules"][0]
        path = rule["http"]["paths"][0]
        assert path["path"] == "/"
        assert path["pathType"] == "Prefix"

    def test_ingress_backend_service_name(
        self, ingress_doc: dict[str, Any]
    ) -> None:
        rule = ingress_doc["spec"]["rules"][0]
        backend = rule["http"]["paths"][0]["backend"]
        assert backend["service"]["name"] == BACKEND_NAME

    def test_ingress_backend_service_port_matches_service(
        self, ingress_doc: dict[str, Any], service_doc: dict[str, Any]
    ) -> None:
        # Ingress → Service port hand-off must match. Accepts either
        # named port 'http' or numeric 80; both route to the same place.
        rule = ingress_doc["spec"]["rules"][0]
        port = rule["http"]["paths"][0]["backend"]["service"]["port"]
        svc_http = [p for p in service_doc["spec"]["ports"] if p["name"] == "http"][0]
        if "name" in port:
            assert port["name"] == svc_http["name"] == "http"
        elif "number" in port:
            assert port["number"] == svc_http["port"]
        else:
            pytest.fail("Ingress backend port must specify either name or number")


# ---------------------------------------------------------------------------
# TestHPAContract
# ---------------------------------------------------------------------------
class TestHPAContract:
    def test_kind_is_hpa(self, hpa_doc: dict[str, Any]) -> None:
        assert hpa_doc["kind"] == "HorizontalPodAutoscaler"

    def test_api_version_autoscaling_v2(self, hpa_doc: dict[str, Any]) -> None:
        # Charter §7.4 — autoscaling/v2 is the only surviving wire
        # form. v2beta2 was removed in K8s 1.26.
        assert hpa_doc["apiVersion"] == "autoscaling/v2"

    def test_hpa_namespace(self, hpa_doc: dict[str, Any]) -> None:
        assert hpa_doc["metadata"]["namespace"] == EXPECTED_NAMESPACE

    def test_hpa_name(self, hpa_doc: dict[str, Any]) -> None:
        assert hpa_doc["metadata"]["name"] == BACKEND_NAME

    def test_hpa_scale_target_is_backend_deployment(
        self, hpa_doc: dict[str, Any]
    ) -> None:
        ref = hpa_doc["spec"]["scaleTargetRef"]
        assert ref["apiVersion"] == "apps/v1"
        assert ref["kind"] == "Deployment"
        assert ref["name"] == BACKEND_NAME

    def test_hpa_min_replicas_matches_deployment_baseline(
        self, hpa_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # HPA should never scale below the HA-02 baseline.
        assert hpa_doc["spec"]["minReplicas"] == deployment_doc["spec"]["replicas"]
        assert hpa_doc["spec"]["minReplicas"] == EXPECTED_REPLICAS

    def test_hpa_max_replicas_above_min(self, hpa_doc: dict[str, Any]) -> None:
        spec = hpa_doc["spec"]
        assert spec["maxReplicas"] > spec["minReplicas"]

    def test_hpa_single_cpu_metric(self, hpa_doc: dict[str, Any]) -> None:
        metrics = hpa_doc["spec"]["metrics"]
        assert len(metrics) == 1
        metric = metrics[0]
        assert metric["type"] == "Resource"
        assert metric["resource"]["name"] == "cpu"

    def test_hpa_cpu_target_utilisation_70(self, hpa_doc: dict[str, Any]) -> None:
        # TODO row 1370 literal: HPA (CPU 70%).
        target = hpa_doc["spec"]["metrics"][0]["resource"]["target"]
        assert target["type"] == "Utilization"
        assert target["averageUtilization"] == EXPECTED_CPU_TARGET


# ---------------------------------------------------------------------------
# TestCrossManifestConsistency — things that only break when multiple
# files silently drift against each other.
# ---------------------------------------------------------------------------
class TestCrossManifestConsistency:
    def test_all_manifests_in_same_namespace(
        self,
        deployment_doc: dict[str, Any],
        service_doc: dict[str, Any],
        ingress_doc: dict[str, Any],
        hpa_doc: dict[str, Any],
    ) -> None:
        namespaces = {
            deployment_doc["metadata"]["namespace"],
            service_doc["metadata"]["namespace"],
            ingress_doc["metadata"]["namespace"],
            hpa_doc["metadata"]["namespace"],
        }
        assert namespaces == {EXPECTED_NAMESPACE}

    def test_all_manifests_share_part_of_label(
        self,
        deployment_doc: dict[str, Any],
        service_doc: dict[str, Any],
        ingress_doc: dict[str, Any],
        hpa_doc: dict[str, Any],
    ) -> None:
        for doc in (deployment_doc, service_doc, ingress_doc, hpa_doc):
            assert (
                doc["metadata"]["labels"]["app.kubernetes.io/part-of"] == "omnisight"
            )

    def test_all_backend_manifests_share_name_label(
        self,
        deployment_doc: dict[str, Any],
        service_doc: dict[str, Any],
        ingress_doc: dict[str, Any],
        hpa_doc: dict[str, Any],
    ) -> None:
        for doc in (deployment_doc, service_doc, ingress_doc, hpa_doc):
            assert (
                doc["metadata"]["labels"]["app.kubernetes.io/name"] == BACKEND_NAME
            )

    def test_hpa_target_deployment_name_matches(
        self, hpa_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        assert (
            hpa_doc["spec"]["scaleTargetRef"]["name"]
            == deployment_doc["metadata"]["name"]
        )

    def test_ingress_service_name_matches_service(
        self, ingress_doc: dict[str, Any], service_doc: dict[str, Any]
    ) -> None:
        ingress_service = ingress_doc["spec"]["rules"][0]["http"]["paths"][0][
            "backend"
        ]["service"]["name"]
        assert ingress_service == service_doc["metadata"]["name"]

    def test_service_target_port_resolves_on_deployment(
        self, service_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # named targetPort 'http' must resolve against a container port
        # with that name on the Deployment pod spec.
        http_port = [p for p in service_doc["spec"]["ports"] if p["name"] == "http"][0]
        target = http_port["targetPort"]
        if isinstance(target, str):
            container_ports = deployment_doc["spec"]["template"]["spec"][
                "containers"
            ][0]["ports"]
            names = {p.get("name") for p in container_ports}
            assert target in names
        else:
            assert target == BACKEND_PORT


# ---------------------------------------------------------------------------
# TestCharterAlignment — the 8 consequences in
# docs/ops/orchestration_selection.md §7 that this row commits to.
# ---------------------------------------------------------------------------
class TestCharterAlignment:
    @pytest.fixture(scope="class")
    def charter_text(self) -> str:
        assert CHARTER.is_file(), "G5 #1 charter must exist"
        return CHARTER.read_text(encoding="utf-8")

    def test_charter_commits_to_deploy_k8s(self, charter_text: str) -> None:
        assert "deploy/k8s/" in charter_text

    def test_charter_commits_to_max_unavailable_zero(
        self, charter_text: str
    ) -> None:
        assert "maxUnavailable: 0" in charter_text

    def test_charter_commits_to_max_surge_one(self, charter_text: str) -> None:
        assert "maxSurge: 1" in charter_text

    def test_charter_commits_to_autoscaling_v2(self, charter_text: str) -> None:
        assert "autoscaling/v2" in charter_text

    def test_charter_commits_to_cpu_70(self, charter_text: str) -> None:
        assert "targetCPUUtilizationPercentage: 70" in charter_text

    def test_charter_commits_to_ingress_class_nginx(self, charter_text: str) -> None:
        assert "ingressClassName: nginx" in charter_text

    def test_manifest_max_unavailable_matches_charter(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        rolling = deployment_doc["spec"]["strategy"]["rollingUpdate"]
        assert rolling["maxUnavailable"] == 0

    def test_manifest_max_surge_matches_charter(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        rolling = deployment_doc["spec"]["strategy"]["rollingUpdate"]
        assert rolling["maxSurge"] == 1

    def test_manifest_hpa_api_matches_charter(self, hpa_doc: dict[str, Any]) -> None:
        assert hpa_doc["apiVersion"] == "autoscaling/v2"

    def test_manifest_hpa_cpu_matches_charter(self, hpa_doc: dict[str, Any]) -> None:
        assert (
            hpa_doc["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"]
            == 70
        )

    def test_manifest_ingress_class_matches_charter(
        self, ingress_doc: dict[str, Any]
    ) -> None:
        assert ingress_doc["spec"]["ingressClassName"] == "nginx"


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — rows 1371/1372/1373/1374 own separate
# manifests; G5 #2 must NOT silently drag them in.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    def test_no_poddisruptionbudget_yet(self) -> None:
        # G5 #3 (row 1371) is a separate delivery. If a PDB shows up
        # here without the sibling row being checked, that row has
        # silently landed inside G5 #2 — visibility regression.
        for path in ALL_MANIFEST_PATHS:
            doc = _load(path)
            assert doc.get("kind") != "PodDisruptionBudget", (
                f"{path.name}: PDB belongs to G5 #3 row 1371, not G5 #2"
            )

    def test_no_helm_chart_dir_yet(self) -> None:
        # G5 #5 (row 1373) is the Helm chart. If it exists when G5 #2
        # lands, the scope line between the rows has blurred.
        chart_yaml = PROJECT_ROOT / "deploy" / "helm" / "omnisight" / "Chart.yaml"
        assert not chart_yaml.exists(), (
            "Helm chart must not land here — G5 #5 row 1373 owns it"
        )

    def test_no_nomad_or_swarm_manifests(self) -> None:
        # Charter §7.8 says Nomad / Swarm are out-of-scope. If a
        # sibling directory appears, re-open the decision.
        assert not (PROJECT_ROOT / "deploy" / "nomad").exists()
        assert not (PROJECT_ROOT / "deploy" / "swarm").exists()


# ---------------------------------------------------------------------------
# TestTodoRowMarker — the historical-note comment on row 1370 is how
# HANDOFF traces decisions back to the TODO. Checking the row anchor is
# present keeps the doc-index truth-source consistent.
# ---------------------------------------------------------------------------
class TestTodoRowMarker:
    @pytest.fixture(scope="class")
    def todo_text(self) -> str:
        todo = PROJECT_ROOT / "TODO.md"
        assert todo.is_file()
        return todo.read_text(encoding="utf-8")

    def test_row_1370_headline_present(self, todo_text: str) -> None:
        assert (
            "Manifests：Deployment（replicas=2, maxUnavailable=0）、"
            "Service、Ingress、HPA（CPU 70%）"
        ) in todo_text

    def test_row_1370_under_g5_section(self, todo_text: str) -> None:
        lines = todo_text.splitlines()
        g5_idx = next(
            (i for i, line in enumerate(lines) if "G5. HA-05" in line), None
        )
        assert g5_idx is not None, "G5 section header missing from TODO.md"
        row_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if "Manifests：Deployment（replicas=2, maxUnavailable=0" in line
            ),
            None,
        )
        assert row_idx is not None
        assert row_idx > g5_idx, "row 1370 must sit under the G5 section header"
