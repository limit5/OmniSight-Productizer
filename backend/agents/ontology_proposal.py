"""Multi-LLM ontology proposal gate for the Cognee F5 ingestion.

When the ECL pipeline encounters an entity whose extracted class is
NOT present in ``config/cognee_entity_classes.yaml``, it cannot
silently promote that class into the KG — the bootstrap would drift
in a single run. Instead, the unknown class is routed through this
module, which performs a two-step LLM review:

  1. **Proposer LLM** drafts a class spec
     (name + description + 2-3 examples).
  2. **Reviewer LLM** (different model / vendor when configured)
     scores the proposal: accept / reject / defer-to-operator.

If both LLMs agree on accept, the class is appended to the in-memory
ontology AND emitted into the weekly digest queue (AC §4). If the
reviewer rejects, the entity is dropped and an
``EntityClassProposalRejected`` is logged. If the per-week proposal
count crosses the runaway threshold, the pipeline halts with
``OntologyProposalRunaway`` (AC error catalog).

Reference: OP-903 acceptance §3-4 + arXiv 2604.23090
("multi-agent ontology generation"). The dual-LLM construction
mirrors the existing dual-reviewer pattern in
``backend/agents/gerrit_jira_bridge.py``.

The two callables (``propose_fn``, ``review_fn``) are injected — the
production wiring uses ``backend.agents.llm.get_llm`` with a
Claude → GPT cross-vendor pair, but tests pass deterministic stubs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

logger = logging.getLogger(__name__)


# ── Errors (per OP-903 error catalog) ────────────────────────────


class EntityClassProposalRejected(RuntimeError):
    """Raised when the reviewer LLM rejects a proposed entity class.

    The ECL caller catches this, drops the originating entity, and
    logs the rejection (AC error catalog row 3).
    """


class OntologyProposalRunaway(RuntimeError):
    """Raised when proposals/week exceed RUNAWAY_THRESHOLD.

    Signals likely schema drift or extractor bug — halts the
    pipeline and surrenders to operator review (AC error catalog
    row 4).
    """


# ── Data shapes ───────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityClassSpec:
    """The minimal spec for a single ontology class.

    Mirrors the YAML row shape in ``cognee_entity_classes.yaml`` so
    that approved proposals can be serialised back into the config
    by the operator-approval digest step.
    """

    name: str
    source: str
    description: str
    examples: tuple[str, ...] = ()

    def to_yaml_row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "examples": list(self.examples),
        }


@dataclass
class ProposalDecision:
    """Outcome of routing one unknown entity through the gate."""

    proposed: EntityClassSpec
    approved: bool
    proposer_rationale: str
    reviewer_rationale: str
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Constants ─────────────────────────────────────────────────────

RUNAWAY_THRESHOLD = 50
"""Per-week proposal ceiling. >50 implies extractor drift or schema bug."""

DEFAULT_ONTOLOGY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "cognee_entity_classes.yaml"
)


# ── Ontology load helper ──────────────────────────────────────────


def load_ontology(path: Path | None = None) -> dict[str, EntityClassSpec]:
    """Read the YAML registry into ``{name: EntityClassSpec}``.

    Returns an empty dict if the file is absent — callers MUST treat
    missing as a bootstrap error rather than silently accepting all
    proposals.
    """
    p = path or DEFAULT_ONTOLOGY_PATH
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    out: dict[str, EntityClassSpec] = {}
    for row in raw.get("classes", []) or []:
        spec = EntityClassSpec(
            name=row["name"],
            source=row.get("source", "all"),
            description=row.get("description", ""),
            examples=tuple(row.get("examples", []) or []),
        )
        out[spec.name] = spec
    return out


def write_ontology(
    classes: dict[str, EntityClassSpec],
    path: Path | None = None,
    *,
    version: int = 1,
) -> None:
    """Serialise the ontology back to YAML.

    Used by the operator-approval digest step — NOT called by the
    inline gate (which only proposes; promotion to the YAML file is
    a weekly batch action by the operator).
    """
    p = path or DEFAULT_ONTOLOGY_PATH
    payload = {
        "version": version,
        "last_reviewed": datetime.now(timezone.utc).date().isoformat(),
        "classes": [classes[name].to_yaml_row() for name in sorted(classes)],
    }
    p.write_text(yaml.safe_dump(payload, sort_keys=False))


# ── Gate ──────────────────────────────────────────────────────────


@dataclass
class OntologyProposalGate:
    """Stateful gate that tracks per-week proposal volume + decisions.

    Inject ``propose_fn`` and ``review_fn`` — both must accept the
    same 4-tuple ``(entity_sample, hint_name, source, known_classes)``
    and return a string-shaped LLM response. The gate parses the
    response into an ``EntityClassSpec`` (proposer) or an
    ``(approve_bool, rationale_str)`` (reviewer).

    The gate does NOT mutate the YAML file. Approved proposals are
    appended to ``pending_promotions`` for the weekly digest cron.
    """

    known: dict[str, EntityClassSpec]
    propose_fn: Callable[[str, str, str, Iterable[str]], EntityClassSpec]
    review_fn: Callable[[EntityClassSpec, Iterable[str]], tuple[bool, str]]
    runaway_threshold: int = RUNAWAY_THRESHOLD

    pending_promotions: list[ProposalDecision] = field(default_factory=list)
    rejected_log: list[ProposalDecision] = field(default_factory=list)
    _proposals_this_week: int = 0

    def reset_week(self) -> None:
        """Operator-callable: zero the runaway counter at digest time."""
        self._proposals_this_week = 0

    def propose(
        self,
        entity_sample: str,
        hint_name: str,
        source: str,
    ) -> ProposalDecision:
        """Run the two-step gate for one unknown entity.

        Raises:
          OntologyProposalRunaway: if the per-week count exceeds
            the threshold AFTER incrementing for this call.
          EntityClassProposalRejected: if the reviewer LLM rejects.
            Caller is responsible for dropping + logging the
            originating entity (AC error catalog row 3).
        """
        # AC error catalog row 4 — halt-and-surrender on drift.
        self._proposals_this_week += 1
        if self._proposals_this_week > self.runaway_threshold:
            raise OntologyProposalRunaway(
                f"{self._proposals_this_week} proposals this week "
                f"(threshold {self.runaway_threshold}); likely extractor "
                f"drift or schema regression — halting for operator review."
            )

        spec = self.propose_fn(entity_sample, hint_name, source, self.known.keys())
        if spec.name in self.known:
            decision = ProposalDecision(
                proposed=spec,
                approved=True,
                proposer_rationale="already-known; treated as no-op",
                reviewer_rationale="skipped — class exists",
            )
            return decision

        approved, rationale = self.review_fn(spec, self.known.keys())
        decision = ProposalDecision(
            proposed=spec,
            approved=approved,
            proposer_rationale=f"proposer suggested {spec.name} for source={source}",
            reviewer_rationale=rationale,
        )
        if approved:
            self.pending_promotions.append(decision)
            logger.info(
                "ontology_proposal accepted name=%s source=%s",
                spec.name,
                spec.source,
            )
            return decision

        self.rejected_log.append(decision)
        logger.info(
            "ontology_proposal rejected name=%s reason=%s",
            spec.name,
            rationale,
        )
        raise EntityClassProposalRejected(
            f"reviewer rejected proposed class '{spec.name}': {rationale}"
        )

    def digest(self) -> list[ProposalDecision]:
        """Return + clear the pending-promotions queue for the weekly digest.

        AC §4: operator approves/rejects in batch. After calling
        ``digest()`` the gate hands the operator the list; the
        operator decides which to merge into the YAML and calls
        :func:`write_ontology` for the survivors.
        """
        out = list(self.pending_promotions)
        self.pending_promotions.clear()
        return out
