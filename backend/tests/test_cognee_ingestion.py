"""OP-903 F5 — tests for the Cognee initial ECL ingestion pipeline.

Seven cases per the ticket test plan:

  1. code ingest happy            — Python AST → PythonModule/Class/Function
  2. JIRA ingest happy            — issue → JiraTicket + JiraStatus + JiraLink
  3. ADR ingest                   — ADR-NNNN-*.md → ADR entity + ticket cross-refs
  4. lesson ingest                — lessons-learned.md slice → Lesson entity
  5. checkpoint resume            — interrupt + re-run does not duplicate
  6. ontology proposal accept+reject — gate routes unknown classes correctly
  7. runaway proposal halt        — >50 proposals/week raises and halts

The ingestion script is exercised end-to-end against an InMemoryBackend
and a temp checkpoint file — no network, no real Cognee instance, no
real JIRA endpoint.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load the script as a module (it lives in scripts/, not as a package).
_SCRIPT = REPO_ROOT / "scripts" / "cognee_initial_ingest.py"
_spec = importlib.util.spec_from_file_location("cognee_initial_ingest", _SCRIPT)
assert _spec and _spec.loader, "spec_from_file_location returned None"
cii = importlib.util.module_from_spec(_spec)
# Register before exec — dataclasses look up __module__ via sys.modules.
sys.modules["cognee_initial_ingest"] = cii
_spec.loader.exec_module(cii)  # type: ignore[union-attr]

from backend.agents.ontology_proposal import (
    EntityClassProposalRejected,
    EntityClassSpec,
    OntologyProposalGate,
    OntologyProposalRunaway,
    load_ontology,
)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a tiny repo skeleton with one of each corpus source."""
    # Python source
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "alpha.py").write_text(
        "from backend import beta\n"
        "class Alpha:\n"
        "    pass\n"
        "def run_alpha():\n"
        "    return Alpha()\n"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "smoke.py").write_text(
        "def smoke():\n    return 1\n"
    )
    # ADR
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-0001-fake-decision.md").write_text(
        "# ADR-0001 — Fake Decision\n\n"
        "Reference: OP-903.\n\n"
        "## Decision\nUse fake X over fake Y.\n"
    )
    # Lessons
    sop_dir = tmp_path / "docs" / "sop"
    sop_dir.mkdir(parents=True)
    (sop_dir / "lessons-learned.md").write_text(
        "# Lessons Learned\n\n"
        "## Lesson 1 — first lesson (2026-05-11)\n\n"
        "Situation: cited OP-903 during F5 bootstrap.\n"
        "Fix: write a test for it.\n"
        "Verification: pytest passes.\n\n"
        "## Lesson 2 — second lesson (2026-05-11)\n\n"
        "Situation: unrelated.\n"
    )
    # Retrospectives
    retro_dir = tmp_path / "docs" / "retrospectives"
    retro_dir.mkdir(parents=True)
    (retro_dir / "2026-05-11-fake-retro.md").write_text(
        "# Fake Retrospective\n\nReferences: OP-903 and OP-852.\n"
    )
    return tmp_path


@pytest.fixture
def starter_ontology() -> dict[str, EntityClassSpec]:
    """Subset of config/cognee_entity_classes.yaml — known classes only."""
    return {
        "PythonModule": EntityClassSpec("PythonModule", "python", ""),
        "PythonClass": EntityClassSpec("PythonClass", "python", ""),
        "PythonFunction": EntityClassSpec("PythonFunction", "python", ""),
        "PythonImport": EntityClassSpec("PythonImport", "python", ""),
        "JiraTicket": EntityClassSpec("JiraTicket", "jira", ""),
        "JiraComponent": EntityClassSpec("JiraComponent", "jira", ""),
        "JiraStatus": EntityClassSpec("JiraStatus", "jira", ""),
        "ADR": EntityClassSpec("ADR", "adr", ""),
        "Lesson": EntityClassSpec("Lesson", "lesson", ""),
        "Retrospective": EntityClassSpec("Retrospective", "retrospective", ""),
    }


def _accept_all(spec, known):  # noqa: ANN001
    return True, "test reviewer accepts everything"


def _reject_all(spec, known):  # noqa: ANN001
    return False, "test reviewer rejects everything"


def _propose_from_hint(sample, hint, source, known):  # noqa: ANN001
    return EntityClassSpec(name=hint, source=source, description=f"proposed for {sample}")


def _build_runner(
    fake_repo: Path,
    starter_ontology: dict[str, EntityClassSpec],
    *,
    review_fn=_accept_all,
    jira_reader=None,
) -> "cii.IngestRunner":
    gate = OntologyProposalGate(
        known=dict(starter_ontology),
        propose_fn=_propose_from_hint,
        review_fn=review_fn,
    )
    return cii.IngestRunner(
        repo_root=fake_repo,
        backend=cii.InMemoryBackend(),
        ontology=dict(starter_ontology),
        gate=gate,
        checkpoint_path=fake_repo / "var" / "ckpt.json",
        jira_reader=jira_reader,
    )


# ── Case 1 — code ingest happy ───────────────────────────────────


def test_code_ingest_happy(fake_repo, starter_ontology):
    runner = _build_runner(fake_repo, starter_ontology)
    stats = runner.run()

    classes = {(e.class_name, e.key) for e in runner.backend.entities}  # type: ignore[union-attr]
    # Module + class + function entities, dotted-qualname keyed.
    assert ("PythonModule", "backend.alpha") in classes
    assert ("PythonClass", "backend.alpha.Alpha") in classes
    assert ("PythonFunction", "backend.alpha.run_alpha") in classes
    assert ("PythonModule", "scripts.smoke") in classes
    assert ("PythonFunction", "scripts.smoke.smoke") in classes
    # The ``from backend import beta`` import yielded an edge.
    edges = {(r.edge, r.dst.split("::")[-1]) for r in runner.backend.relationships}  # type: ignore[union-attr]
    assert any(e == "imports" for e, _ in edges), edges
    assert stats.entities >= 5


# ── Case 2 — JIRA ingest happy ───────────────────────────────────


def test_jira_ingest_happy(fake_repo, starter_ontology):
    def fake_reader(window_days: int):
        assert window_days == 30
        yield {
            "key": "OP-903",
            "fields": {
                "status": {"name": "In Progress"},
                "components": [{"name": "HIGH"}],
                "issuelinks": [
                    {
                        "type": {"name": "Blocks"},
                        "outwardIssue": {"key": "OP-906"},
                    },
                    {
                        "type": {"name": "Blocks"},
                        "inwardIssue": {"key": "OP-852"},
                    },
                ],
            },
        }

    runner = _build_runner(fake_repo, starter_ontology, jira_reader=fake_reader)
    runner.run()

    keys = {(e.class_name, e.key) for e in runner.backend.entities}  # type: ignore[union-attr]
    assert ("JiraTicket", "OP-903") in keys
    # Tail entities created via classify_and_insert(rel=Entity).
    edge_kinds = {r.edge for r in runner.backend.relationships}  # type: ignore[union-attr]
    assert "has_status" in edge_kinds
    assert "has_component" in edge_kinds
    assert "blocks" in edge_kinds


# ── Case 3 — ADR ingest ──────────────────────────────────────────


def test_adr_ingest(fake_repo, starter_ontology):
    runner = _build_runner(fake_repo, starter_ontology)
    runner.run()

    keys = {(e.class_name, e.key) for e in runner.backend.entities}  # type: ignore[union-attr]
    assert ("ADR", "ADR-0001-fake-decision") in keys
    # The OP-903 mention inside the ADR body produced a cross-corpus edge.
    refs = [r for r in runner.backend.relationships if r.edge == "references_ticket"]  # type: ignore[union-attr]
    assert any("OP-903" in r.dst for r in refs), refs


# ── Case 4 — lesson ingest ───────────────────────────────────────


def test_lesson_ingest(fake_repo, starter_ontology):
    runner = _build_runner(fake_repo, starter_ontology)
    runner.run()

    keys = {(e.class_name, e.key) for e in runner.backend.entities}  # type: ignore[union-attr]
    assert ("Lesson", "L-1") in keys
    assert ("Lesson", "L-2") in keys


# ── Case 5 — checkpoint resume ───────────────────────────────────


def test_checkpoint_resume_dedupes(fake_repo, starter_ontology):
    # First run — full ingest.
    runner1 = _build_runner(fake_repo, starter_ontology)
    stats1 = runner1.run()
    assert stats1.entities > 0

    # Second run — same repo, same checkpoint, same starter ontology.
    # Should produce zero new entities (entirely deduped) but should
    # still emit a stats record.
    runner2 = _build_runner(fake_repo, starter_ontology)
    stats2 = runner2.run()
    # Backend on runner2 is fresh — nothing was inserted.
    assert len(runner2.backend.entities) == 0, runner2.backend.entities  # type: ignore[union-attr]
    # And the checkpoint stats stayed continuous (resumed, not restarted).
    assert stats2.skipped_duplicates > 0


# ── Case 6 — ontology proposal accept + reject ──────────────────


def test_ontology_proposal_accept(starter_ontology):
    gate = OntologyProposalGate(
        known=dict(starter_ontology),
        propose_fn=_propose_from_hint,
        review_fn=_accept_all,
    )
    decision = gate.propose(
        entity_sample="payload=42",
        hint_name="WeirdNewThing",
        source="python",
    )
    assert decision.approved is True
    assert "WeirdNewThing" == decision.proposed.name
    digest = gate.digest()
    assert len(digest) == 1
    assert digest[0].proposed.name == "WeirdNewThing"


def test_ontology_proposal_reject(starter_ontology):
    gate = OntologyProposalGate(
        known=dict(starter_ontology),
        propose_fn=_propose_from_hint,
        review_fn=_reject_all,
    )
    with pytest.raises(EntityClassProposalRejected):
        gate.propose(
            entity_sample="payload=99",
            hint_name="AnotherWeirdThing",
            source="python",
        )
    assert gate.digest() == []  # no promotion queued
    assert len(gate.rejected_log) == 1


# ── Case 7 — runaway proposal halt ───────────────────────────────


def test_runaway_proposal_halts(starter_ontology):
    gate = OntologyProposalGate(
        known=dict(starter_ontology),
        propose_fn=_propose_from_hint,
        review_fn=_accept_all,
        runaway_threshold=3,  # tight for the test
    )
    gate.propose("a", "ClassA", "python")
    gate.propose("b", "ClassB", "python")
    gate.propose("c", "ClassC", "python")
    with pytest.raises(OntologyProposalRunaway):
        gate.propose("d", "ClassD", "python")


# ── Bonus — config sanity check (AC §1, §3) ──────────────────────


def test_starter_yaml_loads_with_minimum_classes():
    """The shipped config must cover all five corpora at minimum."""
    onto = load_ontology()
    assert onto, "config/cognee_entity_classes.yaml failed to load"
    by_source: dict[str, int] = {}
    for spec in onto.values():
        by_source[spec.source] = by_source.get(spec.source, 0) + 1
    # Every corpus mentioned in AC §1 must have at least one class.
    for required_source in ("python", "jira", "adr", "lesson", "retrospective"):
        assert required_source in by_source, (required_source, by_source)
