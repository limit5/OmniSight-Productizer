"""G5 #5 — Helm chart contract tests.

TODO row 1373:
    Helm chart `deploy/helm/omnisight/` (values.yaml for staging/prod)

Pins the chart shape that lands under ``deploy/helm/omnisight/`` per
charter ``docs/ops/orchestration_selection.md`` §7.1, §7.5, §7.6 — and
the §7.2 / §7.3 / §7.4 commitments that the chart must template
faithfully (one source of truth: the chart and the plain
``deploy/k8s/*.yaml`` manifests must agree on apiVersion, named ports,
probe paths, HPA target, PDB API version, etc.).

Helm CLI is intentionally NOT a test prerequisite — most of the contract
holds on the chart files themselves (templates as text + values as YAML)
which keeps these tests fast and runnable in CI without Helm install.
The G5 #6 row 1374 CI smoke job will add a `helm template` + `kubectl
apply` round-trip.

Sibling contracts (not asserted here):
    * G5 #2 row 1370 — plain Deployment / Service / Ingress / HPA
      manifests in ``test_k8s_manifests_g5_2.py``.
    * G5 #3 row 1371 — plain PDB in ``test_k8s_pdb_g5_3.py``.
    * G5 #4 row 1372 — plain probes wired in ``test_k8s_probes_g5_4.py``.
    * G5 #6 row 1374 — delivery bundle + kind 1.29 CI smoke.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHART_DIR = PROJECT_ROOT / "deploy" / "helm" / "omnisight"
TEMPLATES_DIR = CHART_DIR / "templates"

CHART_YAML = CHART_DIR / "Chart.yaml"
VALUES_YAML = CHART_DIR / "values.yaml"
VALUES_STAGING = CHART_DIR / "values-staging.yaml"
VALUES_PROD = CHART_DIR / "values-prod.yaml"
HELMIGNORE = CHART_DIR / ".helmignore"
README = CHART_DIR / "README.md"

HELPERS_TPL = TEMPLATES_DIR / "_helpers.tpl"
TPL_NAMESPACE = TEMPLATES_DIR / "namespace.yaml"
TPL_DEPLOYMENT = TEMPLATES_DIR / "deployment.yaml"
TPL_SERVICE = TEMPLATES_DIR / "service.yaml"
TPL_INGRESS = TEMPLATES_DIR / "ingress.yaml"
TPL_PDB = TEMPLATES_DIR / "pdb.yaml"
TPL_HPA = TEMPLATES_DIR / "hpa.yaml"
TPL_NOTES = TEMPLATES_DIR / "NOTES.txt"

K8S_DIR = PROJECT_ROOT / "deploy" / "k8s"
K8S_DEPLOY = K8S_DIR / "10-deployment-backend.yaml"
K8S_SVC = K8S_DIR / "20-service-backend.yaml"
K8S_INGRESS = K8S_DIR / "30-ingress.yaml"
K8S_PDB = K8S_DIR / "15-pdb-backend.yaml"
K8S_HPA = K8S_DIR / "40-hpa-backend.yaml"
K8S_README = K8S_DIR / "README.md"

CHARTER = PROJECT_ROOT / "docs" / "ops" / "orchestration_selection.md"
TODO = PROJECT_ROOT / "TODO.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(_read(path))
    assert isinstance(doc, Mapping), f"{path.name}: top-level YAML must be a mapping"
    return dict(doc)


# ---------------------------------------------------------------------------
# TestChartFileShape — every file the chart needs is in place; nothing
# extra; everything parses where parseable.
# ---------------------------------------------------------------------------
class TestChartFileShape:
    def test_chart_dir_exists(self) -> None:
        assert CHART_DIR.is_dir(), "deploy/helm/omnisight/ must exist"

    def test_chart_yaml_exists(self) -> None:
        assert CHART_YAML.is_file()

    def test_values_yaml_exists(self) -> None:
        assert VALUES_YAML.is_file()

    def test_values_staging_exists(self) -> None:
        # Charter §7.5 — environment overrides MUST split per env file.
        assert VALUES_STAGING.is_file()

    def test_values_prod_exists(self) -> None:
        # Charter §7.5 — environment overrides MUST split per env file.
        assert VALUES_PROD.is_file()

    def test_readme_exists(self) -> None:
        assert README.is_file()

    def test_helmignore_exists(self) -> None:
        assert HELMIGNORE.is_file()

    def test_templates_dir_exists(self) -> None:
        assert TEMPLATES_DIR.is_dir()

    def test_helpers_tpl_exists(self) -> None:
        assert HELPERS_TPL.is_file()

    def test_namespace_template_exists(self) -> None:
        assert TPL_NAMESPACE.is_file()

    def test_deployment_template_exists(self) -> None:
        assert TPL_DEPLOYMENT.is_file()

    def test_service_template_exists(self) -> None:
        assert TPL_SERVICE.is_file()

    def test_ingress_template_exists(self) -> None:
        assert TPL_INGRESS.is_file()

    def test_pdb_template_exists(self) -> None:
        assert TPL_PDB.is_file()

    def test_hpa_template_exists(self) -> None:
        assert TPL_HPA.is_file()

    def test_notes_txt_exists(self) -> None:
        # NOTES.txt is the operator-facing post-install message — losing
        # it removes the only visible spot the chart cites the charter
        # at install time.
        assert TPL_NOTES.is_file()

    def test_chart_yaml_parses(self) -> None:
        # Chart.yaml is YAML through-and-through (no Go templating).
        _yaml(CHART_YAML)

    def test_values_yaml_parses(self) -> None:
        _yaml(VALUES_YAML)

    def test_values_staging_parses(self) -> None:
        _yaml(VALUES_STAGING)

    def test_values_prod_parses(self) -> None:
        _yaml(VALUES_PROD)


# ---------------------------------------------------------------------------
# TestChartMetadata — Chart.yaml fields that operators rely on.
# ---------------------------------------------------------------------------
class TestChartMetadata:
    @pytest.fixture(scope="class")
    def chart(self) -> dict[str, Any]:
        return _yaml(CHART_YAML)

    def test_apiversion_v2(self, chart: dict[str, Any]) -> None:
        # apiVersion v2 is the only Helm 3 wire form. v1 charts have
        # different semantics (requirements.yaml etc.) — never silently
        # downgrade.
        assert chart["apiVersion"] == "v2"

    def test_name(self, chart: dict[str, Any]) -> None:
        assert chart["name"] == "omnisight"

    def test_type_application(self, chart: dict[str, Any]) -> None:
        assert chart["type"] == "application"

    def test_version_semver(self, chart: dict[str, Any]) -> None:
        # Chart version drives `helm diff upgrade` ordering.
        version = str(chart["version"])
        assert re.match(r"^\d+\.\d+\.\d+", version), version

    def test_appversion_string(self, chart: dict[str, Any]) -> None:
        # appVersion MUST be a string per Helm spec; bare numbers
        # silently coerce to int and break templates that quote it.
        assert isinstance(chart["appVersion"], str)

    def test_kubeversion_pins_1_29_floor(self, chart: dict[str, Any]) -> None:
        # Charter §1 — minimum target K8s 1.29.
        kv = chart.get("kubeVersion", "")
        assert "1.29" in kv, f"kubeVersion must reference 1.29 floor: got {kv!r}"

    def test_charter_annotation_present(self, chart: dict[str, Any]) -> None:
        annotations = chart.get("annotations") or {}
        assert annotations.get("ops.omnisight.io/charter") == (
            "docs/ops/orchestration_selection.md"
        )

    def test_g5_row_annotation_present(self, chart: dict[str, Any]) -> None:
        annotations = chart.get("annotations") or {}
        assert str(annotations.get("ops.omnisight.io/g5-row")) == "1373"


# ---------------------------------------------------------------------------
# TestValuesYamlContract — defaults must mirror the plain manifests for
# the fields the chart owns. If `deploy/k8s/*.yaml` and `values.yaml`
# disagree the chart no longer renders to "the same" object — the
# charter §7.1 "two surfaces, one truth" promise breaks.
# ---------------------------------------------------------------------------
class TestValuesYamlContract:
    @pytest.fixture(scope="class")
    def values(self) -> dict[str, Any]:
        return _yaml(VALUES_YAML)

    def test_namespace_default_omnisight(self, values: dict[str, Any]) -> None:
        assert values["namespace"] == "omnisight"

    def test_create_namespace_default_true(self, values: dict[str, Any]) -> None:
        # `helm install` to a fresh cluster must work without an extra
        # `kubectl create ns` step. Operators turn this off explicitly
        # when their tooling owns the namespace.
        assert values["createNamespace"] is True

    def test_image_repository_placeholder(self, values: dict[str, Any]) -> None:
        # Matches deploy/k8s/10-deployment-backend.yaml placeholder.
        assert values["image"]["repository"] == "ghcr.io/your-org/omnisight-backend"

    def test_image_tag_default_latest(self, values: dict[str, Any]) -> None:
        assert values["image"]["tag"] == "latest"

    def test_image_pull_policy_default(self, values: dict[str, Any]) -> None:
        assert values["image"]["pullPolicy"] == "IfNotPresent"

    def test_replica_count_2(self, values: dict[str, Any]) -> None:
        # HA-02 baseline — must equal deploy/k8s/10-deployment-backend.yaml.
        assert values["replicaCount"] == 2

    def test_strategy_max_unavailable_zero(self, values: dict[str, Any]) -> None:
        # Charter §7.4 — zero-downtime rollout on 2 replicas demands
        # maxUnavailable=0.
        assert values["strategy"]["rollingUpdate"]["maxUnavailable"] == 0

    def test_strategy_max_surge_one(self, values: dict[str, Any]) -> None:
        assert values["strategy"]["rollingUpdate"]["maxSurge"] == 1

    def test_resources_cpu_request_present(self, values: dict[str, Any]) -> None:
        # Required for HPA % computation. Without it, HPA silently
        # stays at minReplicas.
        assert values["resources"]["requests"]["cpu"]

    def test_resources_memory_request_present(self, values: dict[str, Any]) -> None:
        assert values["resources"]["requests"]["memory"]

    def test_container_port_8000(self, values: dict[str, Any]) -> None:
        # Matches deploy/k8s/10-deployment-backend.yaml container port.
        assert values["containerPort"] == 8000

    def test_service_type_clusterip(self, values: dict[str, Any]) -> None:
        assert values["service"]["type"] == "ClusterIP"

    def test_service_port_80(self, values: dict[str, Any]) -> None:
        assert values["service"]["port"] == 80

    def test_service_target_port_named_http(self, values: dict[str, Any]) -> None:
        # Named-port indirection — see deploy/k8s/20-service-backend.yaml.
        assert values["service"]["targetPort"] == "http"

    def test_probe_readiness_path_readyz(self, values: dict[str, Any]) -> None:
        assert values["probes"]["readiness"]["path"] == "/readyz"

    def test_probe_liveness_path_livez(self, values: dict[str, Any]) -> None:
        # Charter §7.3 — probe spelling is "/livez", not "/healthz".
        assert values["probes"]["liveness"]["path"] == "/livez"

    def test_probe_readiness_drain_window_under_30s(
        self, values: dict[str, Any]
    ) -> None:
        # Drain detection window must be < 30s (G1 Retry-After), so the
        # LB can pull the replica before in-flight requests time out.
        r = values["probes"]["readiness"]
        window = r["failureThreshold"] * r["periodSeconds"]
        assert window < 30, (
            f"readiness drain window {window}s must stay < 30s G1 Retry-After"
        )

    def test_probe_liveness_restart_window_at_least_30s(
        self, values: dict[str, Any]
    ) -> None:
        # Restart window must be >= 30s (G1 shutdown budget), so K8s
        # never restarts a healthy-but-draining pod.
        l = values["probes"]["liveness"]
        window = l["failureThreshold"] * l["periodSeconds"]
        assert window >= 30, (
            f"liveness restart window {window}s must stay >= 30s G1 budget"
        )

    def test_ingress_enabled_default_true(self, values: dict[str, Any]) -> None:
        assert values["ingress"]["enabled"] is True

    def test_ingress_class_default_nginx(self, values: dict[str, Any]) -> None:
        # Charter §7.6 default.
        assert values["ingress"]["className"] == "nginx"

    def test_gateway_api_default_disabled(self, values: dict[str, Any]) -> None:
        # Charter §7.6 — Gateway-API is EXPLICIT; default must be off
        # so operators flip it on knowingly.
        assert values["ingress"]["gatewayApi"]["enabled"] is False

    def test_pdb_enabled_default_true(self, values: dict[str, Any]) -> None:
        assert values["pdb"]["enabled"] is True

    def test_pdb_min_available_one(self, values: dict[str, Any]) -> None:
        # Mirrors deploy/k8s/15-pdb-backend.yaml. Integer, not %
        # (charter §7.2 — % silently drifts under replicas bumps).
        assert values["pdb"]["minAvailable"] == 1
        assert isinstance(values["pdb"]["minAvailable"], int)

    def test_pdb_max_unavailable_default_null(self, values: dict[str, Any]) -> None:
        # PDB API forbids both being set; the default keeps
        # maxUnavailable null so minAvailable is the only live setting.
        assert values["pdb"].get("maxUnavailable") is None

    def test_autoscaling_enabled_default_true(self, values: dict[str, Any]) -> None:
        assert values["autoscaling"]["enabled"] is True

    def test_autoscaling_min_replicas_matches_replica_count(
        self, values: dict[str, Any]
    ) -> None:
        # HPA never scales below the HA-02 baseline.
        assert values["autoscaling"]["minReplicas"] == values["replicaCount"]

    def test_autoscaling_max_replicas_above_min(self, values: dict[str, Any]) -> None:
        a = values["autoscaling"]
        assert a["maxReplicas"] > a["minReplicas"]

    def test_autoscaling_target_cpu_70(self, values: dict[str, Any]) -> None:
        # Charter §7.4 literal.
        assert values["autoscaling"]["targetCPUUtilizationPercentage"] == 70

    def test_persistence_storageclass_default_blank(
        self, values: dict[str, Any]
    ) -> None:
        # Charter §8 — no default StorageClass; operators set per
        # cluster.
        assert values["persistence"]["storageClassName"] == ""


# ---------------------------------------------------------------------------
# TestValuesStagingContract — staging overrides shape.
# ---------------------------------------------------------------------------
class TestValuesStagingContract:
    @pytest.fixture(scope="class")
    def staging(self) -> dict[str, Any]:
        return _yaml(VALUES_STAGING)

    def test_image_tag_overridden(self, staging: dict[str, Any]) -> None:
        # Staging pins a different tag than prod so an upgrade ladder
        # is auditable.
        assert staging["image"]["tag"] == "staging"

    def test_ingress_host_staging_subdomain(
        self, staging: dict[str, Any]
    ) -> None:
        host = staging["ingress"]["host"]
        assert "staging" in host

    def test_resources_lighter_than_default(self, staging: dict[str, Any]) -> None:
        # Staging asks for less CPU than the default (which is prod-leaning).
        assert staging["resources"]["requests"]["cpu"] in {"100m", "200m"}

    def test_autoscaling_max_below_prod(self, staging: dict[str, Any]) -> None:
        # Staging cluster is smaller — cap at 4.
        assert staging["autoscaling"]["maxReplicas"] <= 4

    def test_autoscaling_min_still_two(self, staging: dict[str, Any]) -> None:
        # HA-02 contract is tested in staging too.
        assert staging["autoscaling"]["minReplicas"] == 2

    def test_pdb_enabled_in_staging(self, staging: dict[str, Any]) -> None:
        # Staging is where we catch PDB regressions before prod.
        assert staging["pdb"]["enabled"] is True

    def test_no_inline_environment_conditionals(self) -> None:
        # Charter §7.5 — overrides must NOT live as `if eq .Values.env
        # "staging"` blocks back in values.yaml. Cheap proxy: the
        # template files don't reference an `.environment` value.
        for tpl in TEMPLATES_DIR.glob("*.yaml"):
            assert ".Values.environment" not in _read(tpl), (
                f"{tpl.name}: charter §7.5 forbids env conditionals — "
                f"split overrides into values-staging/values-prod files"
            )


# ---------------------------------------------------------------------------
# TestValuesProdContract — prod overrides shape.
# ---------------------------------------------------------------------------
class TestValuesProdContract:
    @pytest.fixture(scope="class")
    def prod(self) -> dict[str, Any]:
        return _yaml(VALUES_PROD)

    def test_resources_heavier_than_staging(self, prod: dict[str, Any]) -> None:
        cpu = prod["resources"]["requests"]["cpu"]
        # Prod CPU request must be at least 250m (the values.yaml floor).
        assert cpu in {"250m", "500m", "1"}

    def test_autoscaling_max_replicas_at_least_10(
        self, prod: dict[str, Any]
    ) -> None:
        assert prod["autoscaling"]["maxReplicas"] >= 10

    def test_autoscaling_target_cpu_70(self, prod: dict[str, Any]) -> None:
        # Charter §7.4 literal — never drift the prod target without
        # touching the charter.
        assert prod["autoscaling"]["targetCPUUtilizationPercentage"] == 70

    def test_pdb_enabled_in_prod(self, prod: dict[str, Any]) -> None:
        assert prod["pdb"]["enabled"] is True

    def test_pdb_min_available_one_in_prod(self, prod: dict[str, Any]) -> None:
        assert prod["pdb"]["minAvailable"] == 1

    def test_topology_spread_present(self, prod: dict[str, Any]) -> None:
        # Single-node loss must not drop the cluster below HA-02 — the
        # topology-spread constraint is the chart-level mechanism.
        spread = prod.get("topologySpreadConstraints") or []
        assert spread, "prod must define a topologySpreadConstraints entry"
        assert any(
            entry.get("topologyKey") == "kubernetes.io/hostname"
            for entry in spread
        )


# ---------------------------------------------------------------------------
# TestTemplateGoSyntax — every template references the helpers / values
# we expect; nothing slipped to a hard-coded value.
# ---------------------------------------------------------------------------
class TestTemplateGoSyntax:
    def test_helpers_define_fullname(self) -> None:
        text = _read(HELPERS_TPL)
        assert 'define "omnisight.fullname"' in text

    def test_helpers_define_labels(self) -> None:
        text = _read(HELPERS_TPL)
        assert 'define "omnisight.labels"' in text

    def test_helpers_define_selector_labels(self) -> None:
        text = _read(HELPERS_TPL)
        assert 'define "omnisight.selectorLabels"' in text

    def test_helpers_define_chart(self) -> None:
        text = _read(HELPERS_TPL)
        assert 'define "omnisight.chart"' in text

    def test_namespace_gated_by_create_namespace(self) -> None:
        text = _read(TPL_NAMESPACE)
        assert "{{- if .Values.createNamespace -}}" in text
        assert "kind: Namespace" in text

    def test_deployment_uses_image_repository_and_tag(self) -> None:
        text = _read(TPL_DEPLOYMENT)
        assert ".Values.image.repository" in text
        assert ".Values.image.tag" in text

    def test_deployment_uses_replica_count(self) -> None:
        text = _read(TPL_DEPLOYMENT)
        assert ".Values.replicaCount" in text

    def test_deployment_uses_strategy_values(self) -> None:
        text = _read(TPL_DEPLOYMENT)
        assert ".Values.strategy.rollingUpdate.maxUnavailable" in text
        assert ".Values.strategy.rollingUpdate.maxSurge" in text

    def test_deployment_uses_probe_values(self) -> None:
        text = _read(TPL_DEPLOYMENT)
        assert ".Values.probes.readiness.path" in text
        assert ".Values.probes.liveness.path" in text

    def test_deployment_probes_use_named_http_port(self) -> None:
        text = _read(TPL_DEPLOYMENT)
        # Named port is part of the contract — the plain manifest sets
        # `port: http` on both probes; the chart must too.
        readiness_block = re.search(
            r"readinessProbe:\s*\n(?:\s+.*\n)+",
            text,
        )
        liveness_block = re.search(
            r"livenessProbe:\s*\n(?:\s+.*\n)+",
            text,
        )
        assert readiness_block and "port: http" in readiness_block.group(0)
        assert liveness_block and "port: http" in liveness_block.group(0)

    def test_deployment_emits_downward_api_instance_id(self) -> None:
        text = _read(TPL_DEPLOYMENT)
        assert "OMNISIGHT_INSTANCE_ID" in text
        assert "fieldPath: metadata.name" in text

    def test_service_target_port_value_path(self) -> None:
        text = _read(TPL_SERVICE)
        assert ".Values.service.targetPort" in text

    def test_service_kind_is_service(self) -> None:
        text = _read(TPL_SERVICE)
        assert "kind: Service" in text

    def test_ingress_template_renders_either_route_or_ingress(self) -> None:
        text = _read(TPL_INGRESS)
        # Charter §7.6 — explicit toggle on gatewayApi.enabled, never
        # silent auto-detect.
        assert "{{- if .Values.ingress.gatewayApi.enabled -}}" in text
        assert "kind: HTTPRoute" in text
        assert "kind: Ingress" in text
        assert ".Values.ingress.className" in text

    def test_ingress_gated_by_ingress_enabled(self) -> None:
        text = _read(TPL_INGRESS)
        assert "{{- if .Values.ingress.enabled -}}" in text

    def test_pdb_gated_by_pdb_enabled(self) -> None:
        text = _read(TPL_PDB)
        assert "{{- if .Values.pdb.enabled -}}" in text

    def test_pdb_uses_policy_v1(self) -> None:
        # Charter §7.2 literal — must not silently emit policy/v1beta1.
        text = _read(TPL_PDB)
        assert "apiVersion: policy/v1" in text
        assert "apiVersion: policy/v1beta1" not in text

    def test_pdb_mutual_exclusion_guard(self) -> None:
        # The chart must `fail` at render time when both fields are set
        # — operator catches the mistake before apply.
        text = _read(TPL_PDB)
        assert "fail " in text

    def test_hpa_gated_by_autoscaling_enabled(self) -> None:
        text = _read(TPL_HPA)
        assert "{{- if .Values.autoscaling.enabled -}}" in text

    def test_hpa_uses_autoscaling_v2(self) -> None:
        # Charter §7.4 literal — autoscaling/v2beta2 was removed in 1.26.
        text = _read(TPL_HPA)
        assert "apiVersion: autoscaling/v2" in text
        assert "apiVersion: autoscaling/v2beta2" not in text

    def test_hpa_renders_cpu_target(self) -> None:
        text = _read(TPL_HPA)
        assert ".Values.autoscaling.targetCPUUtilizationPercentage" in text


# ---------------------------------------------------------------------------
# TestTemplateAlignmentWithPlainManifests — chart and plain manifests
# must agree on the wire-shape fields (apiVersions, named ports, probe
# paths, pdb policy version, hpa target). Drift here breaks the
# "two surfaces, one truth" promise.
# ---------------------------------------------------------------------------
class TestTemplateAlignmentWithPlainManifests:
    def test_deployment_apiversion_matches(self) -> None:
        plain = _yaml(K8S_DEPLOY)["apiVersion"]
        text = _read(TPL_DEPLOYMENT)
        assert plain == "apps/v1"
        assert "apiVersion: apps/v1" in text

    def test_pdb_apiversion_matches(self) -> None:
        plain = _yaml(K8S_PDB)["apiVersion"]
        text = _read(TPL_PDB)
        assert plain == "policy/v1"
        assert "apiVersion: policy/v1" in text

    def test_hpa_apiversion_matches(self) -> None:
        plain = _yaml(K8S_HPA)["apiVersion"]
        text = _read(TPL_HPA)
        assert plain == "autoscaling/v2"
        assert "apiVersion: autoscaling/v2" in text

    def test_service_port_matches(self) -> None:
        plain = _yaml(K8S_SVC)["spec"]["ports"][0]
        values = _yaml(VALUES_YAML)["service"]
        assert plain["port"] == values["port"]
        assert plain["targetPort"] == values["targetPort"]

    def test_container_port_matches(self) -> None:
        plain = _yaml(K8S_DEPLOY)["spec"]["template"]["spec"]["containers"][0]
        plain_port = plain["ports"][0]
        values_port = _yaml(VALUES_YAML)["containerPort"]
        assert plain_port["containerPort"] == values_port
        assert plain_port["name"] == "http"

    def test_pdb_min_available_matches(self) -> None:
        plain = _yaml(K8S_PDB)["spec"]
        values = _yaml(VALUES_YAML)["pdb"]
        assert plain["minAvailable"] == values["minAvailable"]

    def test_hpa_target_matches(self) -> None:
        plain = _yaml(K8S_HPA)["spec"]["metrics"][0]["resource"]["target"]
        values = _yaml(VALUES_YAML)["autoscaling"]
        assert plain["averageUtilization"] == values["targetCPUUtilizationPercentage"]

    def test_ingress_class_matches(self) -> None:
        plain = _yaml(K8S_INGRESS)["spec"]["ingressClassName"]
        values = _yaml(VALUES_YAML)["ingress"]["className"]
        assert plain == values == "nginx"

    def test_deployment_replicas_matches(self) -> None:
        plain = _yaml(K8S_DEPLOY)["spec"]["replicas"]
        values = _yaml(VALUES_YAML)["replicaCount"]
        assert plain == values == 2

    def test_strategy_max_unavailable_matches(self) -> None:
        plain = _yaml(K8S_DEPLOY)["spec"]["strategy"]["rollingUpdate"]
        values = _yaml(VALUES_YAML)["strategy"]["rollingUpdate"]
        assert plain["maxUnavailable"] == values["maxUnavailable"] == 0
        assert plain["maxSurge"] == values["maxSurge"] == 1

    def test_probe_paths_match(self) -> None:
        plain = _yaml(K8S_DEPLOY)["spec"]["template"]["spec"]["containers"][0]
        readiness = plain["readinessProbe"]["httpGet"]["path"]
        liveness = plain["livenessProbe"]["httpGet"]["path"]
        values = _yaml(VALUES_YAML)["probes"]
        assert readiness == values["readiness"]["path"] == "/readyz"
        assert liveness == values["liveness"]["path"] == "/livez"


# ---------------------------------------------------------------------------
# TestCharterAlignment — the charter §7 commitments and the chart must
# agree. Two-way pin: charter literals appear in chart; chart references
# the charter for traceability.
# ---------------------------------------------------------------------------
class TestCharterAlignment:
    @pytest.fixture(scope="class")
    def charter_text(self) -> str:
        assert CHARTER.is_file(), "G5 #1 charter must exist"
        return _read(CHARTER)

    def test_charter_mentions_chart_dir(self, charter_text: str) -> None:
        assert "deploy/helm/omnisight/" in charter_text

    def test_charter_mentions_values_staging(self, charter_text: str) -> None:
        assert "values-staging.yaml" in charter_text

    def test_charter_mentions_values_prod(self, charter_text: str) -> None:
        assert "values-prod.yaml" in charter_text

    def test_charter_mentions_gateway_api_toggle(self, charter_text: str) -> None:
        # Charter §7.6 literal.
        assert "ingress.gatewayApi.enabled" in charter_text

    def test_charter_mentions_g5_5_or_row_1373(self, charter_text: str) -> None:
        assert "G5 #5" in charter_text or "row 1373" in charter_text

    def test_chart_readme_references_charter(self) -> None:
        text = _read(README)
        assert "docs/ops/orchestration_selection.md" in text

    def test_chart_readme_mentions_section_7(self) -> None:
        text = _read(README)
        # README cites individual §7 subsections so operators can trace.
        assert "§7" in text


# ---------------------------------------------------------------------------
# TestK8sReadmeAlignment — the deploy/k8s/README.md must reflect that
# the Helm chart now ships, so operators reading EITHER surface land
# at the other.
# ---------------------------------------------------------------------------
class TestK8sReadmeAlignment:
    @pytest.fixture(scope="class")
    def readme_text(self) -> str:
        assert K8S_README.is_file()
        return _read(K8S_README)

    def test_k8s_readme_no_longer_lists_chart_as_not_included(
        self, readme_text: str
    ) -> None:
        # Pre-G5 #5 the K8s README had a bullet:
        #   "Helm chart templates + values-staging.yaml / values-prod.yaml → G5 #5 row 1373."
        # Landing G5 #5 must remove that bullet so operators don't think
        # the chart is still pending.
        assert "Helm chart templates + `values-staging.yaml`" not in readme_text

    def test_k8s_readme_points_to_helm_chart(self, readme_text: str) -> None:
        assert "deploy/helm/omnisight" in readme_text


# ---------------------------------------------------------------------------
# TestTodoRowMarker — TODO row 1373 must flip to [x] so HANDOFF can
# trace the chart back to the row. Anti-regression: reverting the chart
# without re-opening row 1373 fails here.
# ---------------------------------------------------------------------------
class TestTodoRowMarker:
    @pytest.fixture(scope="class")
    def todo_text(self) -> str:
        assert TODO.is_file()
        return _read(TODO)

    def test_row_1373_headline_present(self, todo_text: str) -> None:
        assert "Helm chart `deploy/helm/omnisight/`" in todo_text

    def test_row_1373_marked_done(self, todo_text: str) -> None:
        assert "- [x] Helm chart `deploy/helm/omnisight/`" in todo_text

    def test_row_1373_under_g5_section(self, todo_text: str) -> None:
        lines = todo_text.splitlines()
        g5_idx = next(
            (i for i, line in enumerate(lines) if "G5. HA-05" in line), None
        )
        assert g5_idx is not None
        row_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if "Helm chart `deploy/helm/omnisight/`" in line
            ),
            None,
        )
        assert row_idx is not None
        assert row_idx > g5_idx


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — G5 #5 must not silently drag in
# G5 #6 (CI smoke + delivery bundle). If those land, it's a separate
# commit and this guard flips.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    def test_no_nomad_or_swarm_manifests(self) -> None:
        # Charter §7.8 — Nomad / Swarm are out-of-scope for G5.
        assert not (PROJECT_ROOT / "deploy" / "nomad").exists()
        assert not (PROJECT_ROOT / "deploy" / "swarm").exists()

    def test_no_ci_smoke_workflow_yet(self) -> None:
        # G5 #6 (row 1374) owns the CI smoke. If a workflow file with
        # the kind 1.29 + helm template gating lands here, the scope
        # line between rows has blurred.
        workflows_dir = PROJECT_ROOT / ".github" / "workflows"
        if not workflows_dir.is_dir():
            return  # nothing to check
        for wf in workflows_dir.glob("*.y*ml"):
            text = _read(wf)
            if "deploy/helm/omnisight" in text and "kind" in text.lower():
                pytest.fail(
                    f"G5 #6 (row 1374) owns the kind/helm CI smoke — "
                    f"saw it in {wf.name}; flip row 1374 in the same commit"
                )

    def test_no_extra_chart_under_deploy_helm(self) -> None:
        # If a sibling chart lands (e.g. deploy/helm/omnisight-frontend/)
        # it's a fresh decision — re-open the charter, don't quietly
        # pile it on under the same row.
        helm_dir = PROJECT_ROOT / "deploy" / "helm"
        children = sorted(p.name for p in helm_dir.iterdir() if p.is_dir())
        assert children == ["omnisight"], (
            f"Only deploy/helm/omnisight/ should exist under G5 #5; "
            f"found {children}"
        )


# ---------------------------------------------------------------------------
# TestG5SiblingTestsScopeMigration — the G5 sibling test files
# previously asserted "no Helm chart yet". Landing G5 #5 means those
# guards must have been REMOVED in this same commit (explicit migration
# pattern carried forward from G5 #3 -> G5 #4). If any of them still
# exist, the chart will silently fail those tests when run.
# ---------------------------------------------------------------------------
class TestG5SiblingTestsScopeMigration:
    G5_2_TEST = PROJECT_ROOT / "backend" / "tests" / "test_k8s_manifests_g5_2.py"
    G5_3_TEST = PROJECT_ROOT / "backend" / "tests" / "test_k8s_pdb_g5_3.py"
    G5_4_TEST = PROJECT_ROOT / "backend" / "tests" / "test_k8s_probes_g5_4.py"

    def test_g5_2_no_helm_guard_removed(self) -> None:
        text = _read(self.G5_2_TEST)
        assert "def test_no_helm_chart_dir_yet" not in text, (
            "Remove G5 #2 sibling guard `test_no_helm_chart_dir_yet` in the "
            "same commit that lands G5 #5 — explicit migration."
        )

    def test_g5_3_no_helm_guard_removed(self) -> None:
        text = _read(self.G5_3_TEST)
        assert "def test_no_helm_chart_dir_yet" not in text, (
            "Remove G5 #3 sibling guard `test_no_helm_chart_dir_yet` in the "
            "same commit that lands G5 #5 — explicit migration."
        )

    def test_g5_4_no_helm_guard_removed(self) -> None:
        text = _read(self.G5_4_TEST)
        assert "def test_no_helm_chart_dir_yet" not in text, (
            "Remove G5 #4 sibling guard `test_no_helm_chart_dir_yet` in the "
            "same commit that lands G5 #5 — explicit migration."
        )
