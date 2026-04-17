"""G5 #3 — PodDisruptionBudget manifest contract tests.

TODO row 1371:
    PDB（PodDisruptionBudget minAvailable=1）

Pins the PDB manifest under ``deploy/k8s/15-pdb-backend.yaml`` that
guards the OmniSight backend against voluntary disruptions (drain,
eviction API, rolling updates) below the HA-02 baseline of one pod.

Per the G5 #1 charter (``docs/ops/orchestration_selection.md`` §7.2)
the PDB locks four invariants:

    * apiVersion is ``policy/v1`` (not ``policy/v1beta1`` — that was
      removed in K8s 1.25; v1 is the only surviving wire form on
      K8s 1.29).
    * kind is ``PodDisruptionBudget``.
    * spec.minAvailable is ``1`` (NOT a percentage / NOT
      maxUnavailable — see manifest comment for the rationale).
    * spec.selector.matchLabels mirrors the Deployment's
      spec.selector.matchLabels byte-equally; if the Deployment's
      selector ever drifts, the PDB stops protecting the right pods.

Sibling contracts (not asserted here — the row that owns each does
that):

    * G5 #2 row 1370 — Deployment / Service / Ingress / HPA contracts
      live in ``test_k8s_manifests_g5_2.py``.
    * G5 #4 row 1372 — readiness / liveness probe wiring to G1
      ``/readyz`` + ``/livez`` via httpGet.
    * G5 #5 row 1373 — Helm chart under ``deploy/helm/omnisight/``
      with ``pdb.enabled`` toggle.
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
TODO = PROJECT_ROOT / "TODO.md"
README_PATH = K8S_DIR / "README.md"

PDB_PATH = K8S_DIR / "15-pdb-backend.yaml"
DEPLOYMENT_PATH = K8S_DIR / "10-deployment-backend.yaml"

EXPECTED_NAMESPACE = "omnisight"
BACKEND_NAME = "omnisight-backend"
EXPECTED_MIN_AVAILABLE = 1


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, Mapping), f"{path.name}: top-level YAML must be a mapping"
    return dict(doc)


@pytest.fixture(scope="module")
def pdb_doc() -> dict[str, Any]:
    return _load(PDB_PATH)


@pytest.fixture(scope="module")
def deployment_doc() -> dict[str, Any]:
    return _load(DEPLOYMENT_PATH)


# ---------------------------------------------------------------------------
# TestPdbFileShape — file presence + YAML parse + apply-order placement.
# ---------------------------------------------------------------------------
class TestPdbFileShape:
    def test_pdb_file_exists(self) -> None:
        assert PDB_PATH.is_file(), (
            "deploy/k8s/15-pdb-backend.yaml must be tracked (G5 #3)"
        )

    def test_pdb_file_extension_yaml(self) -> None:
        assert PDB_PATH.suffix == ".yaml"

    def test_pdb_yaml_parses(self) -> None:
        with PDB_PATH.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        assert isinstance(doc, Mapping)

    def test_pdb_lexically_after_deployment(self) -> None:
        # PDB filename must sort after Deployment so kubectl apply -f
        # walks Deployment first. Not a hard K8s requirement but the
        # operational read is cleaner.
        files = sorted(K8S_DIR.glob("*.yaml"))
        names = [f.name for f in files]
        assert PDB_PATH.name in names
        assert names.index(DEPLOYMENT_PATH.name) < names.index(PDB_PATH.name)

    def test_pdb_lexically_before_service(self) -> None:
        # PDB groups with the Deployment in the apply order, ahead of
        # the network-layer manifests (Service / Ingress / HPA).
        files = sorted(K8S_DIR.glob("*.yaml"))
        names = [f.name for f in files]
        assert names.index(PDB_PATH.name) < names.index("20-service-backend.yaml")

    def test_pdb_filename_anchored_g5_3(self) -> None:
        # The "15-" prefix is a structural commit; if anyone renames it
        # the apply order test in G5 #2 will also break — twin guard.
        assert PDB_PATH.name == "15-pdb-backend.yaml"


# ---------------------------------------------------------------------------
# TestPdbApiContract — kind / apiVersion / metadata.
# ---------------------------------------------------------------------------
class TestPdbApiContract:
    def test_kind_is_poddisruptionbudget(self, pdb_doc: dict[str, Any]) -> None:
        assert pdb_doc["kind"] == "PodDisruptionBudget"

    def test_api_version_is_policy_v1(self, pdb_doc: dict[str, Any]) -> None:
        # Charter §7.2 — policy/v1 is the only surviving wire form;
        # policy/v1beta1 was removed in K8s 1.25. Pinning here prevents
        # silent regression to the deprecated API.
        assert pdb_doc["apiVersion"] == "policy/v1"

    def test_api_version_is_not_v1beta1(self, pdb_doc: dict[str, Any]) -> None:
        # Explicit anti-regression: never let a "fix" downgrade us.
        assert "v1beta1" not in pdb_doc["apiVersion"]

    def test_metadata_namespace_is_omnisight(self, pdb_doc: dict[str, Any]) -> None:
        assert pdb_doc["metadata"]["namespace"] == EXPECTED_NAMESPACE

    def test_metadata_name_is_backend(self, pdb_doc: dict[str, Any]) -> None:
        # Mirrors the Deployment / Service / Ingress / HPA name so
        # operators see a consistent object name across the stack.
        assert pdb_doc["metadata"]["name"] == BACKEND_NAME

    def test_recommended_labels_part_of(self, pdb_doc: dict[str, Any]) -> None:
        labels = pdb_doc["metadata"].get("labels", {})
        assert labels.get("app.kubernetes.io/part-of") == "omnisight"

    def test_recommended_labels_name(self, pdb_doc: dict[str, Any]) -> None:
        labels = pdb_doc["metadata"].get("labels", {})
        assert labels.get("app.kubernetes.io/name") == BACKEND_NAME

    def test_recommended_labels_component(self, pdb_doc: dict[str, Any]) -> None:
        labels = pdb_doc["metadata"].get("labels", {})
        assert labels.get("app.kubernetes.io/component") == "backend"

    def test_recommended_labels_managed_by(self, pdb_doc: dict[str, Any]) -> None:
        labels = pdb_doc["metadata"].get("labels", {})
        assert labels.get("app.kubernetes.io/managed-by") == "kubectl"


# ---------------------------------------------------------------------------
# TestPdbSpecContract — minAvailable / selector / mutual exclusion.
# ---------------------------------------------------------------------------
class TestPdbSpecContract:
    def test_spec_present(self, pdb_doc: dict[str, Any]) -> None:
        assert isinstance(pdb_doc.get("spec"), Mapping)

    def test_spec_min_available_is_one(self, pdb_doc: dict[str, Any]) -> None:
        # TODO row 1371 literal: minAvailable=1.
        assert pdb_doc["spec"]["minAvailable"] == EXPECTED_MIN_AVAILABLE

    def test_spec_min_available_is_integer_not_percentage(
        self, pdb_doc: dict[str, Any]
    ) -> None:
        # K8s accepts either "50%" or 1; on a 2-replica baseline we
        # want the integer form so a future replicas bump (e.g. 2 → 4)
        # keeps the floor honest. "50%" of 4 = 2 silently weakens the
        # contract from "≥1 pod" to "≥2 pods" — usually not what we
        # want for the cheapest-to-protect HA promise.
        value = pdb_doc["spec"]["minAvailable"]
        assert isinstance(value, int) and not isinstance(value, bool)

    def test_spec_does_not_set_max_unavailable(
        self, pdb_doc: dict[str, Any]
    ) -> None:
        # PDB rejects both fields being set. minAvailable is the chosen
        # half of the mutual exclusion (see manifest comment for why).
        assert "maxUnavailable" not in pdb_doc["spec"]

    def test_spec_selector_present(self, pdb_doc: dict[str, Any]) -> None:
        selector = pdb_doc["spec"].get("selector")
        assert isinstance(selector, Mapping)

    def test_spec_selector_uses_match_labels(
        self, pdb_doc: dict[str, Any]
    ) -> None:
        # matchExpressions is allowed by the K8s API but matchLabels is
        # the byte-readable form we commit to so the diff against the
        # Deployment selector is mechanical.
        selector = pdb_doc["spec"]["selector"]
        assert "matchLabels" in selector
        assert isinstance(selector["matchLabels"], Mapping)
        assert "matchExpressions" not in selector

    def test_spec_selector_targets_backend_name(
        self, pdb_doc: dict[str, Any]
    ) -> None:
        ml = pdb_doc["spec"]["selector"]["matchLabels"]
        assert ml.get("app.kubernetes.io/name") == BACKEND_NAME

    def test_spec_selector_targets_backend_component(
        self, pdb_doc: dict[str, Any]
    ) -> None:
        ml = pdb_doc["spec"]["selector"]["matchLabels"]
        assert ml.get("app.kubernetes.io/component") == "backend"

    def test_spec_min_available_below_replicas(
        self, pdb_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # PDB minAvailable must be strictly below replicas, otherwise
        # the budget blocks ALL voluntary disruptions including normal
        # rolling restarts — that's the failure mode the historical
        # post-mortems all warn about ("PDB locked us out of draining").
        replicas = deployment_doc["spec"]["replicas"]
        assert pdb_doc["spec"]["minAvailable"] < replicas


# ---------------------------------------------------------------------------
# TestPdbDeploymentAlignment — selector must mirror the Deployment's so the
# PDB protects the same pods the Deployment owns.
# ---------------------------------------------------------------------------
class TestPdbDeploymentAlignment:
    def test_selector_match_labels_byte_equal_to_deployment(
        self, pdb_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # If these drift, the PDB stops protecting the right pods.
        # Byte-equal is the only safe contract.
        pdb_ml = dict(pdb_doc["spec"]["selector"]["matchLabels"])
        dep_ml = dict(deployment_doc["spec"]["selector"]["matchLabels"])
        assert pdb_ml == dep_ml

    def test_selector_resolves_to_pod_template_labels(
        self, pdb_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # The PDB selector must also match the Deployment's pod
        # template labels (which is what K8s actually evaluates against
        # running pods). G5 #2 already pins these to mirror the
        # Deployment selector — we re-assert here to defend the PDB
        # contract independently.
        pdb_ml = pdb_doc["spec"]["selector"]["matchLabels"]
        pod_labels = deployment_doc["spec"]["template"]["metadata"]["labels"]
        for key, value in pdb_ml.items():
            assert pod_labels.get(key) == value, (
                f"PDB selector {key}={value} must match Deployment pod label"
            )

    def test_pdb_namespace_matches_deployment(
        self, pdb_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # PDB is namespaced; it can only guard pods in its own
        # namespace. Cross-namespace mismatch = silent failure.
        assert (
            pdb_doc["metadata"]["namespace"]
            == deployment_doc["metadata"]["namespace"]
        )

    def test_pdb_part_of_label_matches_deployment(
        self, pdb_doc: dict[str, Any], deployment_doc: dict[str, Any]
    ) -> None:
        # Recommended-label cohesion across the bundle.
        assert (
            pdb_doc["metadata"]["labels"]["app.kubernetes.io/part-of"]
            == deployment_doc["metadata"]["labels"]["app.kubernetes.io/part-of"]
        )


# ---------------------------------------------------------------------------
# TestPdbCharterAlignment — the §7.2 PDB commitment in
# docs/ops/orchestration_selection.md must align with this manifest.
# ---------------------------------------------------------------------------
class TestPdbCharterAlignment:
    @pytest.fixture(scope="class")
    def charter_text(self) -> str:
        assert CHARTER.is_file(), "G5 #1 charter must exist"
        return CHARTER.read_text(encoding="utf-8")

    def test_charter_commits_to_policy_v1(self, charter_text: str) -> None:
        # Both §7.2 and §3.2 mention this; we only need the literal to
        # be present somewhere in the charter.
        assert "policy/v1" in charter_text

    def test_charter_commits_to_poddisruptionbudget(
        self, charter_text: str
    ) -> None:
        assert "PodDisruptionBudget" in charter_text

    def test_charter_g5_3_row_anchor_present(self, charter_text: str) -> None:
        # G5 #1 charter §7 lists the PDB commitment under a "G5 #3"
        # bullet — required for the manifest to find its truth source.
        assert "G5 #3" in charter_text

    def test_manifest_api_matches_charter(
        self, pdb_doc: dict[str, Any], charter_text: str
    ) -> None:
        # Both sides must say "policy/v1" — twin pin.
        assert "policy/v1" in charter_text
        assert pdb_doc["apiVersion"] == "policy/v1"

    def test_manifest_kind_matches_charter(
        self, pdb_doc: dict[str, Any], charter_text: str
    ) -> None:
        assert "PodDisruptionBudget" in charter_text
        assert pdb_doc["kind"] == "PodDisruptionBudget"


# ---------------------------------------------------------------------------
# TestPdbReadmeAlignment — the deploy/k8s/README.md files table must
# include the new PDB manifest so operators can see it without spelunking.
# ---------------------------------------------------------------------------
class TestPdbReadmeAlignment:
    @pytest.fixture(scope="class")
    def readme_text(self) -> str:
        assert README_PATH.is_file()
        return README_PATH.read_text(encoding="utf-8")

    def test_readme_lists_pdb_filename(self, readme_text: str) -> None:
        assert "15-pdb-backend.yaml" in readme_text

    def test_readme_mentions_poddisruptionbudget_kind(
        self, readme_text: str
    ) -> None:
        assert "PodDisruptionBudget" in readme_text

    def test_readme_mentions_policy_v1(self, readme_text: str) -> None:
        assert "policy/v1" in readme_text

    def test_readme_mentions_min_available(self, readme_text: str) -> None:
        # Either the "minAvailable: 1" literal or "minAvailable=1" prose
        # form is acceptable — both are unambiguous.
        assert ("minAvailable: 1" in readme_text) or (
            "minAvailable=1" in readme_text
        )


# ---------------------------------------------------------------------------
# TestTodoRowMarker — TODO row 1371 must be flipped to [x] when this
# manifest lands. Without the flip, future readers can't trace the
# decision back from the TODO to the manifest.
# ---------------------------------------------------------------------------
class TestTodoRowMarker:
    @pytest.fixture(scope="class")
    def todo_text(self) -> str:
        assert TODO.is_file()
        return TODO.read_text(encoding="utf-8")

    def test_row_1371_headline_present(self, todo_text: str) -> None:
        assert "PDB（PodDisruptionBudget minAvailable=1）" in todo_text

    def test_row_1371_marked_done(self, todo_text: str) -> None:
        # Hard-pin the post-landing state. If anyone reverts the manifest
        # without flipping the row back, this test fails.
        assert "- [x] PDB（PodDisruptionBudget minAvailable=1）" in todo_text

    def test_row_1371_under_g5_section(self, todo_text: str) -> None:
        lines = todo_text.splitlines()
        g5_idx = next(
            (i for i, line in enumerate(lines) if "G5. HA-05" in line), None
        )
        assert g5_idx is not None, "G5 section header missing from TODO.md"
        row_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if "PDB（PodDisruptionBudget minAvailable=1）" in line
            ),
            None,
        )
        assert row_idx is not None
        assert row_idx > g5_idx, "row 1371 must sit under the G5 section header"


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — G5 #3 is the PDB row. Other G5 sibling
# rows still own their own deliveries; this test guards against G5 #3
# silently dragging them in.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    def test_no_helm_chart_dir_yet(self) -> None:
        # G5 #5 (row 1373) is the Helm chart. If it lands when G5 #3
        # ships, the scope line between the rows has blurred.
        chart_yaml = PROJECT_ROOT / "deploy" / "helm" / "omnisight" / "Chart.yaml"
        assert not chart_yaml.exists(), (
            "Helm chart must not land here — G5 #5 row 1373 owns it"
        )

    def test_no_probes_in_deployment_yet(
        self, deployment_doc: dict[str, Any]
    ) -> None:
        # G5 #4 (row 1372) owns readiness/liveness probes. If they show
        # up in the Deployment when G5 #3 ships, that row has silently
        # landed inside G5 #3 — visibility regression.
        container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
        assert "readinessProbe" not in container, (
            "readinessProbe belongs to G5 #4 row 1372, not G5 #3"
        )
        assert "livenessProbe" not in container, (
            "livenessProbe belongs to G5 #4 row 1372, not G5 #3"
        )

    def test_no_nomad_or_swarm_manifests(self) -> None:
        # Charter §7.8 — Nomad / Swarm are out-of-scope for G5.
        assert not (PROJECT_ROOT / "deploy" / "nomad").exists()
        assert not (PROJECT_ROOT / "deploy" / "swarm").exists()

    def test_pdb_is_a_separate_file_not_merged_into_deployment(self) -> None:
        # Anti-regression: someone might "tidy up" by inlining the PDB
        # into the Deployment YAML as a multi-doc stream. The G5 #3
        # contract is that PDB ships as its own file so the Helm chart
        # toggle (G5 #5 `pdb.enabled`) is a one-template flip.
        with PDB_PATH.open("r", encoding="utf-8") as fh:
            docs = list(yaml.safe_load_all(fh))
        assert len(docs) == 1, "PDB file must contain exactly one YAML document"
        assert docs[0]["kind"] == "PodDisruptionBudget"
