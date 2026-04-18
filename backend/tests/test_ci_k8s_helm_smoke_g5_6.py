"""G5 #6 — K8s + Helm CI smoke contract tests.

TODO row 1374:
    交付：`deploy/k8s/` 或 `deploy/nomad/`、決策文件

Pins the CI smoke workflow that closes the G5 bundle. Charter
``docs/ops/orchestration_selection.md`` §7.7 commits:

    "CI smoke uses kind (Kubernetes IN Docker) for parity: every G5
     manifest must render + apply cleanly against a vanilla kind 1.29
     cluster — this pins the minimum version claim."

Sibling artefacts already shipped:

    * G5 #1 row 1369 — selection charter (`docs/ops/orchestration_selection.md`).
    * G5 #2 row 1370 — `deploy/k8s/` plain manifests.
    * G5 #3 row 1371 — PodDisruptionBudget (`policy/v1`).
    * G5 #4 row 1372 — readiness/liveness probes (`/readyz` + `/livez`).
    * G5 #5 row 1373 — Helm chart (`deploy/helm/omnisight/`).

Helm / kind / kubectl CLIs are NOT a prerequisite for these tests —
the contract is on the workflow file's *text* and the manifest /
chart files it triggers on. The expensive round-trip lives in CI; the
contract here is "the workflow exists, hits the right paths, pins the
right versions, and closes G5 #5's sibling-guard migration explicitly".

Why an explicit migration of the G5 #5 sibling guard:
    `test_helm_chart_g5_5.py::TestScopeDisciplineSiblingRows
    ::test_no_ci_smoke_workflow_yet` was written to RED-flag any
    workflow that landed kind+helm gating before this row existed.
    Landing this row REQUIRES removing that guard in the same commit
    — the explicit-migration pattern carried forward from G5 #3 → #4
    → #5 → #6. If the guard still exists after this row lands, the
    chart's contract test will incorrectly fire.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "k8s-helm-smoke.yml"
WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"

K8S_DIR = PROJECT_ROOT / "deploy" / "k8s"
K8S_README = K8S_DIR / "README.md"

CHART_DIR = PROJECT_ROOT / "deploy" / "helm" / "omnisight"
CHART_README = CHART_DIR / "README.md"

CHARTER = PROJECT_ROOT / "docs" / "ops" / "orchestration_selection.md"
TODO = PROJECT_ROOT / "TODO.md"

G5_5_TEST = PROJECT_ROOT / "backend" / "tests" / "test_helm_chart_g5_5.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(_read(path))
    assert isinstance(doc, Mapping), f"{path.name}: top-level YAML must be a mapping"
    return dict(doc)


# ---------------------------------------------------------------------------
# TestWorkflowFileShape — file is on disk, parses, has known top-level keys.
# ---------------------------------------------------------------------------
class TestWorkflowFileShape:
    def test_workflow_file_exists(self) -> None:
        assert WORKFLOW.is_file(), (
            "G5 #6 (row 1374) ships .github/workflows/k8s-helm-smoke.yml — "
            "missing means the bundle CI smoke is not landed"
        )

    def test_workflow_path_is_known(self) -> None:
        # Pin the path so a rename surfaces here (and the chart README
        # alignment test below catches the mismatched literal).
        rel = WORKFLOW.relative_to(PROJECT_ROOT)
        assert str(rel) == ".github/workflows/k8s-helm-smoke.yml"

    def test_workflow_yaml_parses(self) -> None:
        doc = _yaml(WORKFLOW)
        assert "name" in doc
        # PyYAML parses bare `on:` as Python True (boolean trigger key);
        # accept either the literal string "on" or True.
        assert ("on" in doc) or (True in doc)
        assert "jobs" in doc

    def test_workflow_name_marks_g5(self) -> None:
        doc = _yaml(WORKFLOW)
        # Human-readable name must mention K8s + Helm so it shows up
        # legibly in the GitHub Actions UI tab.
        assert "K8s" in doc["name"] or "k8s" in doc["name"]
        assert "Helm" in doc["name"] or "helm" in doc["name"]


# ---------------------------------------------------------------------------
# TestWorkflowTriggers — fires on changes to the bundle (and only those).
# ---------------------------------------------------------------------------
class TestWorkflowTriggers:
    def _on(self) -> dict[str, Any]:
        doc = _yaml(WORKFLOW)
        return doc.get("on") or doc.get(True) or {}

    def test_push_trigger_present(self) -> None:
        on = self._on()
        assert "push" in on, "needs `on.push` so master branch lands trigger smoke"

    def test_pull_request_trigger_present(self) -> None:
        on = self._on()
        assert "pull_request" in on, (
            "needs `on.pull_request` so PRs touching the bundle gate on smoke"
        )

    def test_workflow_dispatch_present(self) -> None:
        on = self._on()
        # workflow_dispatch enables manual reruns when bumping kind /
        # helm / kubectl pins; mandatory by convention with other
        # OmniSight workflows.
        assert "workflow_dispatch" in on

    def test_push_paths_cover_bundle(self) -> None:
        on = self._on()
        paths = on["push"].get("paths", [])
        assert any("deploy/k8s" in p for p in paths), (
            "push trigger must include deploy/k8s/** so manifest changes "
            "kick the smoke"
        )
        assert any("deploy/helm/omnisight" in p for p in paths), (
            "push trigger must include deploy/helm/omnisight/** so chart "
            "changes kick the smoke"
        )
        assert any("k8s-helm-smoke" in p for p in paths), (
            "push trigger must self-include the workflow file so workflow "
            "edits also kick a smoke run"
        )

    def test_pull_request_paths_cover_bundle(self) -> None:
        on = self._on()
        paths = on["pull_request"].get("paths", [])
        assert any("deploy/k8s" in p for p in paths)
        assert any("deploy/helm/omnisight" in p for p in paths)
        assert any("k8s-helm-smoke" in p for p in paths)


# ---------------------------------------------------------------------------
# TestWorkflowJobsContract — the three jobs the charter §7.7 commits.
# ---------------------------------------------------------------------------
class TestWorkflowJobsContract:
    REQUIRED_JOBS = ("helm-render", "kubectl-validate", "kind-smoke")

    def _jobs(self) -> dict[str, Any]:
        doc = _yaml(WORKFLOW)
        jobs = doc.get("jobs")
        assert isinstance(jobs, Mapping)
        return dict(jobs)

    def test_three_required_jobs_present(self) -> None:
        jobs = self._jobs()
        for name in self.REQUIRED_JOBS:
            assert name in jobs, (
                f"job {name!r} missing — charter §7.7 needs render + "
                f"client-validate + live kind round-trip"
            )

    def test_kind_smoke_depends_on_render_and_validate(self) -> None:
        # Cheap jobs run first so a typo fails fast before kind boot.
        kind = self._jobs()["kind-smoke"]
        needs = kind.get("needs")
        if isinstance(needs, str):
            needs = [needs]
        assert needs is not None
        assert "helm-render" in needs
        assert "kubectl-validate" in needs

    def test_jobs_have_timeouts(self) -> None:
        # Hung steps should not pin a runner indefinitely.
        for name, job in self._jobs().items():
            assert "timeout-minutes" in job, (
                f"job {name!r} missing timeout-minutes — required by "
                f"OmniSight workflow conventions"
            )

    def test_runs_on_ubuntu(self) -> None:
        for name, job in self._jobs().items():
            runs_on = job.get("runs-on")
            assert runs_on == "ubuntu-latest" or (
                isinstance(runs_on, list) and "ubuntu-latest" in runs_on
            )


# ---------------------------------------------------------------------------
# TestKindVersionPin — charter §1 minimum target is K8s 1.29.
# ---------------------------------------------------------------------------
class TestKindVersionPin:
    def test_kind_node_image_pinned_to_1_29(self) -> None:
        doc = _yaml(WORKFLOW)
        env = doc.get("env", {})
        node_image = env.get("KIND_NODE_IMAGE", "")
        assert "kindest/node" in node_image, (
            "KIND_NODE_IMAGE must use kindest/node (charter §7.7 anchor)"
        )
        assert "v1.29" in node_image, (
            f"charter §1 minimum K8s 1.29 — got {node_image!r}; "
            f"downgrade is a charter violation"
        )

    def test_kind_action_used_for_cluster_boot(self) -> None:
        # helm/kind-action is the canonical wrapper; using it (vs raw
        # `kind create cluster`) means kind binary version + kubeconfig
        # exposure are handled consistently across runs.
        text = _read(WORKFLOW)
        assert "helm/kind-action@v1" in text, (
            "kind boot must use helm/kind-action@v1 (canonical wrapper)"
        )

    def test_kind_node_image_referenced_in_step(self) -> None:
        # The env var must actually be wired into the kind-action step,
        # not just declared. `${{ env.KIND_NODE_IMAGE }}` is the marker.
        text = _read(WORKFLOW)
        assert "node_image: ${{ env.KIND_NODE_IMAGE }}" in text


# ---------------------------------------------------------------------------
# TestHelmRenderJob — `helm template` exercised against all 3 value combos.
# ---------------------------------------------------------------------------
class TestHelmRenderJob:
    def test_uses_setup_helm_action(self) -> None:
        text = _read(WORKFLOW)
        assert "azure/setup-helm@v4" in text

    def test_helm_lint_step_present(self) -> None:
        text = _read(WORKFLOW)
        assert "helm lint deploy/helm/omnisight" in text

    def test_renders_defaults_overlay(self) -> None:
        text = _read(WORKFLOW)
        # default render = no -f overlay (charter §7.5 — defaults work)
        assert "helm template omnisight deploy/helm/omnisight" in text

    def test_renders_staging_overlay(self) -> None:
        text = _read(WORKFLOW)
        assert "values-staging.yaml" in text, (
            "staging values overlay must be rendered (charter §7.5)"
        )

    def test_renders_prod_overlay(self) -> None:
        text = _read(WORKFLOW)
        assert "values-prod.yaml" in text, (
            "prod values overlay must be rendered (charter §7.5)"
        )

    def test_exercises_gateway_api_toggle(self) -> None:
        # Charter §7.6 — both Ingress branches must render. The
        # explicit toggle (not silent auto-detect) means the chart's
        # if/else is the only thing keeping the two kinds mutually
        # exclusive; CI must exercise the non-default branch.
        text = _read(WORKFLOW)
        assert "ingress.gatewayApi.enabled=true" in text
        assert "HTTPRoute" in text


# ---------------------------------------------------------------------------
# TestKubectlValidateJob — schema-validates plain YAML against 1.29.
# ---------------------------------------------------------------------------
class TestKubectlValidateJob:
    def test_uses_setup_kubectl_action(self) -> None:
        text = _read(WORKFLOW)
        assert "azure/setup-kubectl@v4" in text

    def test_kubectl_version_pinned(self) -> None:
        doc = _yaml(WORKFLOW)
        env = doc.get("env", {})
        version = env.get("KUBECTL_VERSION", "")
        assert version.startswith("v1.29"), (
            f"kubectl pin must match kind node K8s 1.29 minor; got {version!r}"
        )

    def test_helm_version_pinned(self) -> None:
        doc = _yaml(WORKFLOW)
        env = doc.get("env", {})
        version = env.get("HELM_VERSION", "")
        assert version.startswith("v3."), (
            f"helm v3 is the only supported major; got {version!r}"
        )

    def test_client_dry_run_against_k8s_dir(self) -> None:
        text = _read(WORKFLOW)
        assert "kubectl apply --dry-run=client -f deploy/k8s/" in text


# ---------------------------------------------------------------------------
# TestKindSmokeJob — live API server validation against kind 1.29.
# ---------------------------------------------------------------------------
class TestKindSmokeJob:
    def test_server_dry_run_against_k8s_dir(self) -> None:
        # --dry-run=server hits admission webhooks + defaulting +
        # deprecation warnings; --dry-run=client misses all three.
        text = _read(WORKFLOW)
        assert "kubectl apply --dry-run=server -f deploy/k8s/" in text

    def test_server_dry_run_against_chart_default(self) -> None:
        text = _read(WORKFLOW)
        # `helm template … | kubectl apply --dry-run=server -f -` is
        # the chart's symmetric round-trip.
        assert "kubectl apply --dry-run=server -f -" in text

    def test_server_dry_run_against_staging_chart(self) -> None:
        text = _read(WORKFLOW)
        # The staging values overlay must be exercised against the
        # live API server, not just at render time.
        assert text.count("values-staging.yaml") >= 2, (
            "staging overlay must appear in BOTH helm-render AND kind-smoke "
            "jobs — render-only doesn't catch admission warnings"
        )

    def test_server_dry_run_against_prod_chart(self) -> None:
        text = _read(WORKFLOW)
        assert text.count("values-prod.yaml") >= 2, (
            "prod overlay must appear in BOTH helm-render AND kind-smoke jobs"
        )

    def test_collects_kind_logs_on_failure(self) -> None:
        # `kind export logs` on failure is the operator's only diagnostic
        # if a future K8s release breaks our manifests in CI.
        text = _read(WORKFLOW)
        assert "kind export logs" in text
        assert "if: failure()" in text


# ---------------------------------------------------------------------------
# TestConcurrencyAndPermissions — match the OmniSight workflow conventions.
# ---------------------------------------------------------------------------
class TestConcurrencyAndPermissions:
    def test_permissions_least_privilege(self) -> None:
        # Only `contents: read` is needed for checkout + manifest validation.
        # Anything broader leaks blast radius.
        doc = _yaml(WORKFLOW)
        perms = doc.get("permissions", {})
        assert perms == {"contents": "read"}, (
            f"least-privilege: only contents: read needed; got {perms!r}"
        )

    def test_concurrency_group_per_ref(self) -> None:
        doc = _yaml(WORKFLOW)
        conc = doc.get("concurrency", {})
        assert "group" in conc
        assert "${{ github.ref }}" in conc["group"]
        assert conc.get("cancel-in-progress") is True


# ---------------------------------------------------------------------------
# TestCharterAlignment — workflow + charter must agree on the §7.7 spec.
# ---------------------------------------------------------------------------
class TestCharterAlignment:
    def test_charter_commits_kind_1_29(self) -> None:
        text = _read(CHARTER)
        assert "kind" in text.lower()
        assert "1.29" in text

    def test_charter_section_7_7_present(self) -> None:
        text = _read(CHARTER)
        # §7.7 is the load-bearing literal — workflow header + charter
        # cross-reference each other so a renumbering breaks both ends.
        assert re.search(r"7\.\s*CI smoke", text) or "§7.7" in text or (
            "CI smoke uses `kind`" in text
        )

    def test_workflow_references_charter(self) -> None:
        text = _read(WORKFLOW)
        assert "orchestration_selection.md" in text
        assert "§7.7" in text or "section 7" in text.lower()


# ---------------------------------------------------------------------------
# TestK8sReadmeAlignment — the deploy/k8s README told operators
# "G5 #6 will land the CI job". Now that it has, the README must
# reflect that or it lies to operators.
# ---------------------------------------------------------------------------
class TestK8sReadmeAlignment:
    def test_k8s_readme_no_longer_says_will_land(self) -> None:
        text = _read(K8S_README)
        # "will land the CI job" was the G5 #5-era text. After G5 #6
        # lands the workflow, that sentence is a lie. The README must
        # be updated in the same commit.
        assert "G5 #6 delivery bundle (row 1374) will land" not in text, (
            "deploy/k8s/README.md still claims the CI job has not landed; "
            "update it in the same commit that lands G5 #6"
        )

    def test_k8s_readme_references_workflow_file(self) -> None:
        text = _read(K8S_README)
        assert "k8s-helm-smoke.yml" in text or "k8s-helm-smoke" in text, (
            "deploy/k8s/README.md must point operators at the new "
            "workflow file"
        )

    def test_k8s_readme_no_longer_lists_ci_smoke_as_not_included(self) -> None:
        # The Scope/NOT-included section had a bullet for the CI smoke;
        # that bullet must be removed (the smoke is now part of the
        # bundle). Pattern: "CI smoke workflow + kind harness → G5 #6 row 1374"
        text = _read(K8S_README)
        assert "CI smoke workflow + kind harness → G5 #6 row 1374" not in text, (
            "deploy/k8s/README.md still lists CI smoke as out-of-scope; "
            "move it from the NOT-included list to the bundle list"
        )


# ---------------------------------------------------------------------------
# TestChartReadmeAlignment — same surgical update for the chart README.
# ---------------------------------------------------------------------------
class TestChartReadmeAlignment:
    def test_chart_readme_no_longer_says_until_g5_6_lands(self) -> None:
        text = _read(CHART_README)
        # The "Until the G5 #6 CI job lands a smoke check, run this
        # locally…" paragraph is a lie once the job exists.
        assert "Until the G5 #6 CI job lands" not in text, (
            "chart README still tells operators to run the diff locally "
            "because the CI job hasn't landed; update the same commit"
        )

    def test_chart_readme_no_longer_says_lands_in_g5_6(self) -> None:
        text = _read(CHART_README)
        # Charter cross-reference list bullet was "§7.7 — CI smoke
        # against kind 1.29 (lands in G5 #6, row 1374)." That changes
        # to "(landed in G5 #6, row 1374)." or similar past-tense.
        assert "(lands in G5 #6, row 1374)" not in text, (
            "chart README still uses 'lands in' (future tense); switch "
            "to past tense in the same commit"
        )

    def test_chart_readme_no_longer_lists_ci_smoke_as_not_included(self) -> None:
        text = _read(CHART_README)
        # The "Scope — what this chart does NOT include" section had
        # "CI smoke workflow (kind 1.29 + helm template | kubectl diff)
        # lands in G5 #6 row 1374."
        assert "CI smoke workflow (kind 1.29 + `helm template | kubectl diff`) lands" not in text, (
            "chart README still lists CI smoke as not included; move it"
        )


# ---------------------------------------------------------------------------
# TestG5SiblingGuardMigration — the explicit-migration pattern the
# G5 series has carried forward at every row boundary. G5 #5's
# `test_no_ci_smoke_workflow_yet` MUST be removed in this commit.
# ---------------------------------------------------------------------------
class TestG5SiblingGuardMigration:
    def test_g5_5_ci_smoke_guard_removed(self) -> None:
        text = _read(G5_5_TEST)
        assert "def test_no_ci_smoke_workflow_yet" not in text, (
            "G5 #5 sibling guard `test_no_ci_smoke_workflow_yet` must be "
            "REMOVED in the same commit that lands G5 #6 — explicit "
            "migration pattern (G5 #3 → #4 → #5 → #6)"
        )


# ---------------------------------------------------------------------------
# TestTodoRowMarker — tracker hygiene: row 1374 flipped + headline literal.
# ---------------------------------------------------------------------------
class TestTodoRowMarker:
    def test_row_headline_present(self) -> None:
        text = _read(TODO)
        assert "交付：`deploy/k8s/` 或 `deploy/nomad/`、決策文件" in text, (
            "row 1374 headline literal missing — TODO row may have been "
            "renamed (would silently mask the [x] flip below)"
        )

    def test_row_marked_complete(self) -> None:
        text = _read(TODO)
        # Past-tense [x] must appear next to the headline. Tolerate
        # historical-note suffix via the `<!--` HTML comment prefix.
        assert (
            "- [x] 交付：`deploy/k8s/` 或 `deploy/nomad/`、決策文件"
            in text
        ), (
            "row 1374 must flip from [ ] to [x] in the same commit "
            "that lands the workflow"
        )

    def test_row_under_g5_section(self) -> None:
        text = _read(TODO)
        lines = text.splitlines()
        g5_header_idx = None
        row_idx = None
        for i, line in enumerate(lines):
            if "### G5." in line and g5_header_idx is None:
                g5_header_idx = i
            if "交付：`deploy/k8s/` 或 `deploy/nomad/`、決策文件" in line:
                row_idx = i
        assert g5_header_idx is not None, "G5 section header missing"
        assert row_idx is not None, "row 1374 line missing"
        assert row_idx > g5_header_idx, (
            "row 1374 must appear AFTER the G5 section header — file "
            "ordering regression check"
        )


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — guard against silent scope creep
# into adjacent G-buckets. Pattern carried forward from G5 #2/#3/#4/#5.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    def test_no_nomad_or_swarm_manifests(self) -> None:
        # Charter §7.8 — Nomad / Swarm are out-of-scope for G5.
        # If they appear, it's a separate G-bucket, not a G5 #6 hand-off.
        assert not (PROJECT_ROOT / "deploy" / "nomad").exists()
        assert not (PROJECT_ROOT / "deploy" / "swarm").exists()

    # NOTE: `test_no_g6_dr_drill_workflow` was removed in the commit
    # that landed G6 #1 (TODO row 1379) —
    # `.github/workflows/dr-drill-daily.yml` now owns the daily DR
    # drill CI surface and references `scripts/backup_selftest.py` by
    # name, which is the exact literal this guard used to forbid. The
    # G6-side contract pinning lives in
    # `backend/tests/test_dr_drill_daily_g6_1.py` — explicit-migration
    # pattern, carried forward from G5 #3 → #4 → #5 → #6 → G6 #1.

    # NOTE: `test_no_g7_grafana_dashboard` was removed in the commit
    # that landed G7 #2 (TODO row 1387) —
    # `deploy/observability/grafana/ha.json` now owns the G7 HA-07
    # dashboard surface, which is the exact literal this guard used
    # to forbid. The G7 #2-side contract pinning lives in
    # `backend/tests/test_ha_grafana_dashboard_g7_2.py`. Explicit-
    # migration pattern, carried forward from
    # G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6 #5 → G7 #2
    # (10th continuation).

    def test_workflow_does_not_reference_other_g_buckets(self) -> None:
        # A G5 workflow that mentions G4 / G6 / G7 has either drifted
        # scope or is a copy-paste accident.
        text = _read(WORKFLOW)
        assert "G4" not in text or "G4 " not in text or text.count("G4") == 0, (
            "G5 workflow should not reference G4 — separate bucket"
        )
        # G6 / G7 references are unambiguous regressions.
        assert "G6" not in text, "G5 workflow must not reference G6"
        assert "G7" not in text, "G5 workflow must not reference G7"
