"""G5 #1 — `docs/ops/orchestration_selection.md` contract (TODO row 1369).

Opening deliverable of G5 (HA-05 Multi-node orchestration). The decision
doc locks the orchestrator choice for rows 1370–1374 (Deployment / Service
/ Ingress / HPA manifests, PDB, probe wiring, Helm chart, delivery bundle).
Once this lands, every follow-on G5 artefact inherits the commitments
enumerated in §7 of the doc.

This file pins the contract so the doc cannot silently drift:

    (1) File exists at the canonical path operators + the G5 sibling
        rows (1370–1374) link by.
    (2) All required evaluation sections are present, in order, so an
        operator scanning top-to-bottom sees rationale before decision
        consequences.
    (3) All three candidates (Kubernetes, Nomad, Docker Swarm) are
        genuinely evaluated — not just named — by appearing in each of
        the five axes rows of the §6 summary table.
    (4) The five burden axes the TODO row calls out (比較運維負擔)
        are all present in §2 AND referenced in §6 so the scoring
        is linked to definitions.
    (5) §1 TL;DR fields operators paste into deploy tickets are
        machine-readable (chosen orchestrator, manifest home, minimum
        version) — drift here breaks the downstream Helm chart layout
        contract.
    (6) §7 consequences commitments align with the G5 TODO rows
        (replicas=2, maxUnavailable=0 / CPU 70% HPA / PDB minAvailable=1
        / Helm chart path / readiness endpoint wiring).
    (7) Cross-references to sibling runbooks exist and point to files
        that actually live on disk (no broken copy-paste references).
    (8) The anti-decision ("Nomad + Swarm are out-of-scope for G5")
        is explicit, so a future PR adding a second orchestrator
        under deploy/ has to revisit this doc first.

Siblings (future — not delivered yet, do not verify on-disk here):
    * G5 #2 row 1370 — Deployment / Service / Ingress / HPA manifests
    * G5 #3 row 1371 — PodDisruptionBudget
    * G5 #4 row 1372 — readiness / liveness probe wiring
    * G5 #5 row 1373 — Helm chart deploy/helm/omnisight/
    * G5 #6 row 1374 — delivery bundle deploy/k8s/ (or deploy/nomad/)
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DECISION_DOC = PROJECT_ROOT / "docs" / "ops" / "orchestration_selection.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DECISION_DOC.exists(), (
        f"G5 #1 deliverable missing at {DECISION_DOC} — downstream "
        "rows 1370–1374 have no charter to anchor against"
    )
    return DECISION_DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) File exists at canonical path
# ---------------------------------------------------------------------------


class TestDecisionDocFileShape:
    def test_decision_doc_exists(self) -> None:
        assert DECISION_DOC.exists(), (
            f"G5 #1 row 1369 deliverable missing: {DECISION_DOC}"
        )

    def test_decision_doc_in_docs_ops(self) -> None:
        assert DECISION_DOC.parent == PROJECT_ROOT / "docs" / "ops", (
            "decision doc must live at docs/ops/orchestration_selection.md "
            "(siblings in the G5 rows link by exact path)"
        )

    def test_decision_doc_nonempty(self, doc_text: str) -> None:
        assert len(doc_text) > 4000, (
            "decision doc suspiciously short — a placeholder won't "
            "lock G5 #2–#6 artefacts"
        )

    def test_decision_doc_has_top_level_title(self, doc_text: str) -> None:
        first_line = doc_text.lstrip().splitlines()[0]
        assert first_line.startswith("# "), "decision doc must start with H1"
        # Anchor to the G5 / HA-05 bucket so tree-grep `## G5` lands here.
        assert "G5" in first_line or "HA-05" in first_line, (
            "H1 must anchor to the G5 / HA-05 TODO bucket"
        )
        # Anchor to the three-way comparison.
        assert "K8s" in first_line or "Kubernetes" in first_line, (
            "H1 must mention Kubernetes / K8s"
        )
        assert "Nomad" in first_line, "H1 must mention Nomad"
        assert "swarm" in first_line.lower(), "H1 must mention Swarm"


# ---------------------------------------------------------------------------
# (2) All required sections present, in order
# ---------------------------------------------------------------------------


REQUIRED_SECTIONS_IN_ORDER: list[str] = [
    "## 1. TL;DR",
    "## 2. Evaluation axes",
    "## 3. Option A",
    "## 4. Option B",
    "## 5. Option C",
    "## 6. Scoring summary",
    "## 7. Consequences",
    "## 8. Open questions",
    "## 9. Cross-references",
]


class TestDecisionDocSections:
    @pytest.mark.parametrize("title", REQUIRED_SECTIONS_IN_ORDER)
    def test_section_present(self, doc_text: str, title: str) -> None:
        assert title in doc_text, (
            f"decision doc missing required section heading prefix: "
            f"{title!r}"
        )

    def test_sections_in_order(self, doc_text: str) -> None:
        positions = [doc_text.find(t) for t in REQUIRED_SECTIONS_IN_ORDER]
        assert all(p >= 0 for p in positions), (
            "all sections must be present (covered by per-section tests)"
        )
        assert positions == sorted(positions), (
            f"sections out of order — got {positions}"
        )


# ---------------------------------------------------------------------------
# (3) All three candidates genuinely evaluated, not just named
# ---------------------------------------------------------------------------


CANDIDATES: list[str] = ["Kubernetes", "Nomad", "Docker Swarm"]


class TestCandidateCoverage:
    @pytest.mark.parametrize("candidate", CANDIDATES)
    def test_candidate_named_in_tldr(self, doc_text: str, candidate: str) -> None:
        # Every candidate must appear in §1 so an operator doing a quick
        # skim sees all three options at once.
        tldr_slice = doc_text.split("## 2. Evaluation axes", 1)[0]
        assert candidate in tldr_slice, (
            f"candidate {candidate!r} not named in §1 TL;DR — operator "
            f"quick-skim misses an option"
        )

    @pytest.mark.parametrize("candidate", CANDIDATES)
    def test_candidate_named_in_scoring_table(
        self, doc_text: str, candidate: str
    ) -> None:
        # The §6 scoring summary must name every candidate in the
        # header row.
        scoring_slice = doc_text.split("## 6. Scoring summary", 1)[1]
        scoring_slice = scoring_slice.split("## 7. Consequences", 1)[0]
        assert candidate in scoring_slice, (
            f"candidate {candidate!r} missing from §6 scoring table — "
            f"comparison is incomplete"
        )

    def test_each_candidate_has_dedicated_section(self, doc_text: str) -> None:
        # §3 / §4 / §5 each dedicate a full section to one candidate.
        # We verify by checking that each candidate name appears inside
        # its section.
        option_a = doc_text.split("## 3. Option A", 1)[1].split(
            "## 4. Option B", 1
        )[0]
        option_b = doc_text.split("## 4. Option B", 1)[1].split(
            "## 5. Option C", 1
        )[0]
        option_c = doc_text.split("## 5. Option C", 1)[1].split(
            "## 6. Scoring summary", 1
        )[0]
        assert "Kubernetes" in option_a, "§3 must evaluate Kubernetes"
        assert "Nomad" in option_b, "§4 must evaluate Nomad"
        assert "Swarm" in option_c, "§5 must evaluate Docker Swarm"


# ---------------------------------------------------------------------------
# (4) All five burden axes present and referenced in the scoring summary
# ---------------------------------------------------------------------------


BURDEN_AXES: list[str] = [
    "install-burden",
    "day2-burden",
    "observability-burden",
    "upgrade-burden",
    "recovery-burden",
]


class TestBurdenAxisCoverage:
    @pytest.mark.parametrize("axis", BURDEN_AXES)
    def test_axis_defined_in_section_2(self, doc_text: str, axis: str) -> None:
        # §2 defines the axes; they must literally appear so the scoring
        # table rows can refer back to the definitions.
        section_2 = doc_text.split("## 2. Evaluation axes", 1)[1].split(
            "## 3. Option A", 1
        )[0]
        assert axis in section_2, (
            f"burden axis {axis!r} not defined in §2 — scoring in §6 "
            f"has no definition to point at"
        )

    @pytest.mark.parametrize("axis", BURDEN_AXES)
    def test_axis_in_scoring_summary(self, doc_text: str, axis: str) -> None:
        scoring = doc_text.split("## 6. Scoring summary", 1)[1].split(
            "## 7. Consequences", 1
        )[0]
        assert axis in scoring, (
            f"burden axis {axis!r} not scored in §6 — comparison "
            f"between candidates is incomplete"
        )


# ---------------------------------------------------------------------------
# (5) TL;DR machine-readable fields present
# ---------------------------------------------------------------------------


TLDR_REQUIRED_FIELDS: list[str] = [
    "Chosen orchestrator",
    "Manifest home",
    "Minimum target version",
    "Alternatives considered",
    "Decision reversibility",
]


class TestTldrFields:
    @pytest.mark.parametrize("field", TLDR_REQUIRED_FIELDS)
    def test_tldr_field_present(self, doc_text: str, field: str) -> None:
        tldr = doc_text.split("## 1. TL;DR", 1)[1].split(
            "## 2. Evaluation axes", 1
        )[0]
        assert field in tldr, (
            f"§1 TL;DR missing field {field!r} — downstream PR "
            f"templates paste these values into tickets"
        )

    def test_chosen_is_kubernetes(self, doc_text: str) -> None:
        # The decision must be explicit. A doc that says "one of the three"
        # in the TL;DR would defeat the point.
        tldr = doc_text.split("## 1. TL;DR", 1)[1].split(
            "## 2. Evaluation axes", 1
        )[0]
        # The "Chosen orchestrator" row must contain "Kubernetes".
        lines = [l for l in tldr.splitlines() if "Chosen orchestrator" in l]
        assert lines, "no 'Chosen orchestrator' row in TL;DR table"
        assert any("Kubernetes" in l for l in lines), (
            "TL;DR must lock Kubernetes as the chosen orchestrator — "
            "rows 1370–1374 expect deploy/k8s/ + deploy/helm/omnisight/"
        )

    def test_manifest_home_matches_deploy_paths(self, doc_text: str) -> None:
        tldr = doc_text.split("## 1. TL;DR", 1)[1].split(
            "## 2. Evaluation axes", 1
        )[0]
        # The manifest home must point at deploy/k8s/ AND deploy/helm/omnisight/
        # — these are the exact paths G5 #5 and G5 #6 rows commit to.
        assert "deploy/k8s/" in tldr, (
            "TL;DR manifest-home must reference deploy/k8s/ "
            "(row 1374 commits to this path)"
        )
        assert "deploy/helm/omnisight/" in tldr, (
            "TL;DR manifest-home must reference deploy/helm/omnisight/ "
            "(row 1373 commits to this path)"
        )


# ---------------------------------------------------------------------------
# (6) §7 consequences align with concrete G5 TODO-row commitments
# ---------------------------------------------------------------------------


REQUIRED_CONSEQUENCES: list[str] = [
    # Row 1370: replicas=2, maxUnavailable=0, HPA CPU 70%
    "maxUnavailable",
    "CPU",
    "70",
    # Row 1371: PodDisruptionBudget
    "PodDisruptionBudget",
    # Row 1372: readiness / liveness probe against G1 endpoint
    "/readyz",
    "/livez",
    # Row 1373: Helm chart values split
    "values-staging.yaml",
    "values-prod.yaml",
    # Row 1374: deploy/k8s/ layout
    "deploy/k8s/",
    "deploy/helm/omnisight/",
    # API version pins required by the chart
    "policy/v1",
    "autoscaling/v2",
]


class TestConsequencesAlignment:
    @pytest.mark.parametrize("marker", REQUIRED_CONSEQUENCES)
    def test_consequence_present(self, doc_text: str, marker: str) -> None:
        consequences = doc_text.split("## 7. Consequences", 1)[1].split(
            "## 8. Open questions", 1
        )[0]
        assert marker in consequences, (
            f"§7 consequences missing commitment {marker!r} — "
            f"downstream G5 row will ship without a charter anchor"
        )


# ---------------------------------------------------------------------------
# (7) Cross-reference targets exist on disk
# ---------------------------------------------------------------------------


CROSS_REF_PATHS: list[tuple[str, str]] = [
    # (doc-path as written, relative to PROJECT_ROOT)
    ("TODO.md", "TODO.md"),
    ("docs/ops/blue_green_runbook.md", "docs/ops/blue_green_runbook.md"),
    ("docs/ops/orchestration_migration.md", "docs/ops/orchestration_migration.md"),
    ("deploy/postgres-ha/", "deploy/postgres-ha"),
    (
        "deploy/prometheus/orchestration_alerts.rules.yml",
        "deploy/prometheus/orchestration_alerts.rules.yml",
    ),
]


class TestCrossReferenceTargetsExist:
    @pytest.mark.parametrize(
        "written_path,fs_path",
        CROSS_REF_PATHS,
        ids=[p[0] for p in CROSS_REF_PATHS],
    )
    def test_cross_ref_mentioned_and_exists(
        self, doc_text: str, written_path: str, fs_path: str
    ) -> None:
        assert written_path in doc_text, (
            f"cross-reference {written_path!r} missing from §9 — "
            f"either the doc forgot it or this test is stale"
        )
        target = PROJECT_ROOT / fs_path
        assert target.exists(), (
            f"cross-reference {written_path!r} points at {fs_path} "
            f"which does not exist on disk — broken link"
        )


# ---------------------------------------------------------------------------
# (8) Anti-decision (Nomad + Swarm out-of-scope for G5) explicit
# ---------------------------------------------------------------------------


class TestAntiDecisionExplicit:
    def test_nomad_and_swarm_explicitly_out_of_scope(self, doc_text: str) -> None:
        # A future PR adding deploy/nomad/ or deploy/swarm/ silently
        # under G5 has to revisit this doc first. The §7 consequences
        # row names both by name as out-of-scope.
        consequences = doc_text.split("## 7. Consequences", 1)[1].split(
            "## 8. Open questions", 1
        )[0]
        lowered = consequences.lower()
        assert "nomad" in lowered, "§7 must explicitly out-of-scope Nomad"
        assert "swarm" in lowered, "§7 must explicitly out-of-scope Swarm"
        assert "out-of-scope" in lowered or "out of scope" in lowered, (
            "§7 must use the words 'out-of-scope' / 'out of scope' when "
            "ruling Nomad and Swarm out — future PRs need a grep-able "
            "anchor"
        )

    def test_swarm_maintenance_mode_called_out(self, doc_text: str) -> None:
        # The single load-bearing reason Swarm is a no-go is that
        # classic swarm-mode has been in maintenance since 2020.
        # If the doc loses this fact, a future reviewer might think the
        # case against Swarm is weaker than it is.
        option_c = doc_text.split("## 5. Option C", 1)[1].split(
            "## 6. Scoring summary", 1
        )[0]
        assert "maintenance" in option_c.lower(), (
            "§5 must call out Swarm's maintenance-mode status — "
            "this is the load-bearing disqualifier"
        )


# ---------------------------------------------------------------------------
# (9) Sibling TODO rows reachable from this doc (so future reviewers can
#     walk G5 #1 → G5 #2–#6 without leaving the file)
# ---------------------------------------------------------------------------


G5_SIBLING_ROW_MARKERS: list[str] = [
    "G5 #2",
    "G5 #3",
    "G5 #4",
    "G5 #5",
    "G5 #6",
]


class TestG5SiblingNavigation:
    @pytest.mark.parametrize("marker", G5_SIBLING_ROW_MARKERS)
    def test_sibling_row_mentioned(self, doc_text: str, marker: str) -> None:
        assert marker in doc_text, (
            f"sibling row marker {marker!r} missing — future reviewers "
            f"can't navigate G5 #1 → {marker} within this doc"
        )
