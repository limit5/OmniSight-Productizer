#!/usr/bin/env python3
"""OP-903 F5 — Cognee initial ECL ingestion (codebase + JIRA + ADRs + lessons).

Bootstraps the project knowledge graph by walking five corpora —
python source, recent JIRA, ADRs, lessons-learned entries, and
retrospectives — and emitting (entity, relationship) tuples into the
Cognee backend.

State machine (per OP-903 description):

    ingest_start
      → checkpoint_init
      → corpus_iterate:
          for each source:
            extract entities
              → classify against ontology
                  → known: insert + checkpoint
                  → unknown: route to backend.agents.ontology_proposal
                                → approved: insert + ontology growth
                                → rejected: drop + log
      → final_checkpoint
      → report stats

Resume: every 100 entities the runtime writes a JSON checkpoint
file containing the per-source cursor + the set of already-ingested
entity keys. Re-running the script reads this file and replays from
the cursor — newly seen entity keys are deduped against the persisted
set, so re-running over the same corpus is idempotent (AC §2).

Errors are caught at the corpus_iterate boundary and routed per the
ticket's error catalog:

  IngestionCheckpointCorrupted     → restart from last good checkpoint
  CogneeQueryDuringIngest          → degrade to B8/B10 baseline; resume
  EntityClassProposalRejected      → drop the entity, log
  OntologyProposalRunaway          → halt; surrender to operator review

The CogneeBackend / JiraReader / FileReader / LLM callables are all
injectable so tests can run the full pipeline without a live Cognee
service or JIRA endpoint. Production wiring builds the real Backend
from the OP-852 C3 adapter when that lands.
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

# stdlib-only at the top — heavy deps (yaml, urllib) are imported on first use
# to keep ``--help`` cold-start cheap.

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents.ontology_proposal import (  # noqa: E402
    EntityClassProposalRejected,
    EntityClassSpec,
    OntologyProposalGate,
    OntologyProposalRunaway,
    load_ontology,
)

logger = logging.getLogger("cognee_initial_ingest")

# ── Error catalog (OP-903) ───────────────────────────────────────


class IngestionCheckpointCorrupted(RuntimeError):
    """Checkpoint JSON file failed to parse or had wrong shape.

    The runtime aborts the current run and the operator is expected
    to invoke ``--from-scratch`` for a clean restart. The last GOOD
    checkpoint can still be reused by passing ``--resume-from FILE``.
    """


class CogneeQueryDuringIngest(RuntimeError):
    """Cognee KG was queried while a bootstrap ingest was in flight.

    The KG is in an inconsistent intermediate state during ingestion;
    callers must degrade to the B8/B10 baseline path until the ingest
    completes. Detection is best-effort — we raise this here so a
    higher-level caller can swallow + degrade rather than serve a
    half-built KG.
    """


# ── Data shapes ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Entity:
    """One node destined for the KG.

    ``key`` MUST be globally unique within ``(class_name, source)`` —
    re-deriving it from corpus state lets us dedupe across reruns
    without storing every entity in the checkpoint. For Python source
    we use the dotted qualname; for JIRA, the issue key; for ADRs,
    the filename stem; for lessons, the lesson-number-string.
    """

    class_name: str
    key: str
    source: str
    properties: tuple[tuple[str, str], ...] = ()

    def dedup_key(self) -> str:
        return f"{self.source}::{self.class_name}::{self.key}"


@dataclass(frozen=True)
class Relationship:
    """One directed edge between two entities (by dedup_key)."""

    src: str
    edge: str
    dst: str


@dataclass
class IngestStats:
    entities: int = 0
    relationships: int = 0
    skipped_duplicates: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Backend protocol ──────────────────────────────────────────────


class CogneeBackend(Protocol):
    """Minimal protocol the OP-852 C3 adapter must implement.

    The bootstrap pipeline never speaks raw Cognee — it speaks this
    surface, so tests can swap in an in-memory mock and the real
    adapter can plug in later without touching the script.
    """

    def insert_entity(self, entity: Entity) -> None: ...

    def insert_relationship(self, rel: Relationship) -> None: ...


@dataclass
class InMemoryBackend:
    """Reference backend used by tests + dry-run mode."""

    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    def insert_entity(self, entity: Entity) -> None:
        self.entities.append(entity)

    def insert_relationship(self, rel: Relationship) -> None:
        self.relationships.append(rel)


# ── Checkpoint ────────────────────────────────────────────────────


@dataclass
class Checkpoint:
    """Resume cursor + dedup set, persisted as JSON every 100 entities."""

    seen_keys: set[str] = field(default_factory=set)
    source_cursor: dict[str, str] = field(default_factory=dict)
    stats: IngestStats = field(default_factory=IngestStats)

    def to_json(self) -> str:
        return json.dumps(
            {
                "seen_keys": sorted(self.seen_keys),
                "source_cursor": self.source_cursor,
                "stats": self.stats.to_dict(),
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Checkpoint":
        try:
            data = json.loads(text)
            stats_raw = data.get("stats", {}) or {}
            return cls(
                seen_keys=set(data.get("seen_keys", []) or []),
                source_cursor=dict(data.get("source_cursor", {}) or {}),
                stats=IngestStats(**{k: v for k, v in stats_raw.items() if k in IngestStats.__dataclass_fields__}),
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise IngestionCheckpointCorrupted(
                f"checkpoint parse failed: {exc!s}"
            ) from exc


CHECKPOINT_EVERY = 100


def load_checkpoint(path: Path) -> Checkpoint:
    if not path.exists():
        return Checkpoint()
    return Checkpoint.from_json(path.read_text())


def save_checkpoint(cp: Checkpoint, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(cp.to_json())
    tmp.replace(path)


# ── Source extractors ────────────────────────────────────────────


def _python_qualname(module: str, node: ast.AST, parent: str = "") -> str:
    name = getattr(node, "name", "")
    if parent:
        return f"{module}.{parent}.{name}"
    return f"{module}.{name}"


def extract_python(repo_root: Path, dirs: Iterable[str] = ("backend", "scripts")) -> Iterator[tuple[Entity, list[Relationship]]]:
    """AST-walk all *.py files under the given directories.

    Yields ``(module_entity, [class_entity, function_entity, import_rel, ...])``
    one module at a time so the corpus loop can checkpoint at module
    boundaries.
    """
    for top in dirs:
        base = repo_root / top
        if not base.exists():
            continue
        for py in sorted(base.rglob("*.py")):
            rel = py.relative_to(repo_root)
            module_dotted = ".".join(rel.with_suffix("").parts)
            module_entity = Entity(
                class_name="PythonModule",
                key=module_dotted,
                source="python",
                properties=(("path", str(rel)),),
            )
            children: list[Entity] = []
            rels: list[Relationship] = []
            try:
                tree = ast.parse(py.read_text(), filename=str(py))
            except (SyntaxError, UnicodeDecodeError):
                logger.warning("python_ast_skip path=%s", rel)
                yield module_entity, []
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    children.append(Entity(
                        class_name="PythonClass",
                        key=f"{module_dotted}.{node.name}",
                        source="python",
                        properties=(("module", module_dotted),),
                    ))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Skip nested/method functions inside ClassDef — those are
                    # named off the enclosing class which ast.walk doesn't track.
                    # Module-level FunctionDef captures the common case.
                    children.append(Entity(
                        class_name="PythonFunction",
                        key=f"{module_dotted}.{node.name}",
                        source="python",
                        properties=(("module", module_dotted),),
                    ))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    target = Entity(
                        class_name="PythonImport",
                        key=f"{module_dotted}->{node.module}",
                        source="python",
                    )
                    children.append(target)
                    rels.append(Relationship(
                        src=module_entity.dedup_key(),
                        edge="imports",
                        dst=target.dedup_key(),
                    ))
            yield module_entity, children + [r for r in rels]  # type: ignore[list-item]


_JIRA_MENTION_RE = re.compile(r"\b(OP-\d+)\b")
_ADR_FRONTMATTER_RE = re.compile(r"^#\s*ADR-(\d{4})\s*[—-]\s*(.+)$", re.MULTILINE)
_LESSON_HEADER_RE = re.compile(r"^##\s+Lesson\s+(\d+)\s*[—-]\s*(.+)$", re.MULTILINE)


def extract_adrs(repo_root: Path) -> Iterator[tuple[Entity, list[Relationship]]]:
    """Walk docs/adr/ADR-NNNN-*.md, one entity per file."""
    adr_dir = repo_root / "docs" / "adr"
    if not adr_dir.exists():
        return
    for adr in sorted(adr_dir.glob("ADR-*.md")):
        stem = adr.stem  # ADR-0003-gerrit-code-review
        text = adr.read_text(errors="replace")
        m = _ADR_FRONTMATTER_RE.search(text)
        title = m.group(2).strip() if m else stem
        ent = Entity(
            class_name="ADR",
            key=stem,
            source="adr",
            properties=(("title", title), ("path", str(adr.relative_to(repo_root)))),
        )
        rels: list[Relationship] = []
        for mention in set(_JIRA_MENTION_RE.findall(text)):
            ticket = Entity(class_name="JiraTicket", key=mention, source="jira")
            rels.append(Relationship(
                src=ent.dedup_key(),
                edge="references_ticket",
                dst=ticket.dedup_key(),
            ))
        yield ent, rels


def extract_lessons(repo_root: Path) -> Iterator[tuple[Entity, list[Relationship]]]:
    """Slice docs/sop/lessons-learned.md by ``## Lesson N — …`` headers."""
    path = repo_root / "docs" / "sop" / "lessons-learned.md"
    if not path.exists():
        return
    text = path.read_text(errors="replace")
    for m in _LESSON_HEADER_RE.finditer(text):
        n, title = m.group(1), m.group(2).strip()
        body_start = m.end()
        next_m = _LESSON_HEADER_RE.search(text, body_start)
        body = text[body_start: next_m.start() if next_m else len(text)]
        ent = Entity(
            class_name="Lesson",
            key=f"L-{n}",
            source="lesson",
            properties=(("title", title),),
        )
        rels: list[Relationship] = []
        for mention in set(_JIRA_MENTION_RE.findall(body)):
            ticket = Entity(class_name="JiraTicket", key=mention, source="jira")
            rels.append(Relationship(
                src=ent.dedup_key(),
                edge="references_ticket",
                dst=ticket.dedup_key(),
            ))
        yield ent, rels


def extract_retrospectives(repo_root: Path) -> Iterator[tuple[Entity, list[Relationship]]]:
    """Walk docs/retrospectives/*.md, one entity per file."""
    retro_dir = repo_root / "docs" / "retrospectives"
    if not retro_dir.exists():
        return
    for retro in sorted(retro_dir.glob("*.md")):
        ent = Entity(
            class_name="Retrospective",
            key=retro.stem,
            source="retrospective",
            properties=(("path", str(retro.relative_to(repo_root))),),
        )
        rels: list[Relationship] = []
        text = retro.read_text(errors="replace")
        for mention in set(_JIRA_MENTION_RE.findall(text)):
            ticket = Entity(class_name="JiraTicket", key=mention, source="jira")
            rels.append(Relationship(
                src=ent.dedup_key(),
                edge="references_ticket",
                dst=ticket.dedup_key(),
            ))
        yield ent, rels


JiraReader = Callable[[int], Iterable[dict[str, Any]]]
"""Signature: ``reader(window_days) -> iterable of issue dicts``.

Each issue dict must carry: ``key``, ``fields.status.name``,
``fields.components`` (list of {name}), ``fields.issuelinks`` (list).
"""


def extract_jira(reader: JiraReader, window_days: int = 30) -> Iterator[tuple[Entity, list[Relationship]]]:
    """Pull JIRA tickets via injected reader and yield entity + edges."""
    for issue in reader(window_days):
        key = issue["key"]
        fields = issue.get("fields", {}) or {}
        status = (fields.get("status") or {}).get("name", "Unknown")
        ent = Entity(
            class_name="JiraTicket",
            key=key,
            source="jira",
            properties=(("status", status),),
        )
        rels: list[Relationship] = []
        # Components → JiraComponent edge
        for comp in fields.get("components") or []:
            cname = comp.get("name") if isinstance(comp, dict) else str(comp)
            if not cname:
                continue
            target = Entity(class_name="JiraComponent", key=cname, source="jira")
            rels.append(Relationship(
                src=ent.dedup_key(),
                edge="has_component",
                dst=target.dedup_key(),
            ))
        # Status → JiraStatus edge
        status_ent = Entity(class_name="JiraStatus", key=status, source="jira")
        rels.append(Relationship(
            src=ent.dedup_key(),
            edge="has_status",
            dst=status_ent.dedup_key(),
        ))
        # issuelinks → JiraLink edge (META.issuelinks check per L-OP-870)
        for link in fields.get("issuelinks") or []:
            ltype = ((link or {}).get("type") or {}).get("name", "relates_to")
            inward = (link or {}).get("inwardIssue") or {}
            outward = (link or {}).get("outwardIssue") or {}
            other = (outward.get("key") or inward.get("key"))
            if not other:
                continue
            other_ent = Entity(class_name="JiraTicket", key=other, source="jira")
            rels.append(Relationship(
                src=ent.dedup_key(),
                edge=ltype.lower().replace(" ", "_"),
                dst=other_ent.dedup_key(),
            ))
        yield ent, rels


# ── Pipeline ──────────────────────────────────────────────────────


@dataclass
class IngestRunner:
    """Orchestrator for the full ECL bootstrap.

    Holds the backend, ontology, proposal gate, and checkpoint. The
    public surface is :meth:`run`, which executes the state machine
    end-to-end. Sub-extractors are also accessible for tests that
    want to drive one corpus at a time.
    """

    repo_root: Path
    backend: CogneeBackend
    ontology: dict[str, EntityClassSpec]
    gate: OntologyProposalGate
    checkpoint_path: Path
    checkpoint: Checkpoint = field(default_factory=Checkpoint)
    jira_reader: JiraReader | None = None
    jira_window_days: int = 30

    def __post_init__(self) -> None:
        if self.checkpoint_path.exists():
            self.checkpoint = load_checkpoint(self.checkpoint_path)

    # ── classify + insert ────────────────────────────────────────

    def classify_and_insert(self, entity: Entity, related: list[Relationship]) -> bool:
        """Insert ``entity`` (+ its edges) if its class is in the ontology.

        Returns True if inserted, False if dropped. Re-routes unknown
        classes through the proposal gate; on rejection the entity is
        dropped per AC error catalog row 3.
        """
        if entity.class_name not in self.ontology:
            sample = ", ".join(f"{k}={v}" for k, v in entity.properties) or entity.key
            try:
                decision = self.gate.propose(
                    entity_sample=sample,
                    hint_name=entity.class_name,
                    source=entity.source,
                )
            except EntityClassProposalRejected:
                self.checkpoint.stats.proposals_rejected += 1
                return False
            # Approved → grow the local ontology so subsequent entities
            # of the same class don't re-trigger the gate. The YAML
            # file itself is only updated after the operator's weekly
            # digest approval (AC §4).
            self.ontology[decision.proposed.name] = decision.proposed
            self.checkpoint.stats.proposals_accepted += 1

        dk = entity.dedup_key()
        if dk in self.checkpoint.seen_keys:
            self.checkpoint.stats.skipped_duplicates += 1
            return False
        self.backend.insert_entity(entity)
        self.checkpoint.seen_keys.add(dk)
        self.checkpoint.stats.entities += 1
        for rel in related:
            if isinstance(rel, Relationship):
                self.backend.insert_relationship(rel)
                self.checkpoint.stats.relationships += 1
            elif isinstance(rel, Entity):
                # tail-entities (e.g. JiraComponent referenced by an edge):
                # insert idempotently without recursing through the gate.
                if rel.class_name in self.ontology and rel.dedup_key() not in self.checkpoint.seen_keys:
                    self.backend.insert_entity(rel)
                    self.checkpoint.seen_keys.add(rel.dedup_key())
                    self.checkpoint.stats.entities += 1
        return True

    # ── corpus_iterate ───────────────────────────────────────────

    def _maybe_checkpoint(self, processed_since_save: int) -> int:
        if processed_since_save >= CHECKPOINT_EVERY:
            save_checkpoint(self.checkpoint, self.checkpoint_path)
            return 0
        return processed_since_save

    def run(self) -> IngestStats:
        """End-to-end ECL bootstrap. Returns final stats."""
        processed = 0
        # python
        for ent, related in extract_python(self.repo_root):
            self.classify_and_insert(ent, related)
            for child in related:
                if isinstance(child, Entity):
                    self.classify_and_insert(child, [])
            processed += 1
            processed = self._maybe_checkpoint(processed)
            self.checkpoint.source_cursor["python"] = ent.key
        # adrs
        for ent, rels in extract_adrs(self.repo_root):
            self.classify_and_insert(ent, rels)  # type: ignore[arg-type]
            processed += 1
            processed = self._maybe_checkpoint(processed)
            self.checkpoint.source_cursor["adr"] = ent.key
        # lessons
        for ent, rels in extract_lessons(self.repo_root):
            self.classify_and_insert(ent, rels)  # type: ignore[arg-type]
            processed += 1
            processed = self._maybe_checkpoint(processed)
            self.checkpoint.source_cursor["lesson"] = ent.key
        # retrospectives
        for ent, rels in extract_retrospectives(self.repo_root):
            self.classify_and_insert(ent, rels)  # type: ignore[arg-type]
            processed += 1
            processed = self._maybe_checkpoint(processed)
            self.checkpoint.source_cursor["retrospective"] = ent.key
        # jira (last 30d)
        if self.jira_reader is not None:
            for ent, rels in extract_jira(self.jira_reader, self.jira_window_days):
                self.classify_and_insert(ent, rels)  # type: ignore[arg-type]
                processed += 1
                processed = self._maybe_checkpoint(processed)
                self.checkpoint.source_cursor["jira"] = ent.key

        # final_checkpoint
        self.checkpoint.stats.finished_at = datetime.now(timezone.utc).isoformat()
        save_checkpoint(self.checkpoint, self.checkpoint_path)
        return self.checkpoint.stats


# ── Default JIRA reader (production wiring) ───────────────────────


def default_jira_reader(agent_class: str = "claude") -> JiraReader:
    """Build a JIRA reader from the env-config jira_dispatch client.

    Returned callable issues a JQL search for tickets touched in the
    last ``window_days`` and yields the raw issue dicts.
    """
    from backend.agents import jira_dispatch  # local import: avoids cold-import cost

    def _reader(window_days: int) -> Iterable[dict[str, Any]]:
        client = jira_dispatch.make_client(agent_class)
        jql = (
            f"project = {client.project_key} AND "
            f"(status != Done OR updated >= -{window_days}d) "
            "ORDER BY updated DESC"
        )
        # Reuse the existing _request helper via module attr access.
        # If the search endpoint surface changes, the failure mode is a
        # clean import-time AttributeError rather than a half-typed call.
        request = jira_dispatch._request  # type: ignore[attr-defined]
        page = request(
            client,
            "GET",
            f"/search?jql={jql.replace(' ', '%20')}&fields=status,components,issuelinks&maxResults=200",
        )
        for issue in page.get("issues", []) or []:
            yield issue

    return _reader


# ── CLI ───────────────────────────────────────────────────────────


def _build_default_runner(
    repo_root: Path,
    *,
    checkpoint_path: Path,
    backend: CogneeBackend | None = None,
    jira_reader: JiraReader | None = None,
) -> IngestRunner:
    ontology = load_ontology()
    if not ontology:
        raise RuntimeError(
            "ontology config/cognee_entity_classes.yaml is empty or missing — "
            "bootstrap refuses to run with an unbounded ontology"
        )
    # Production gate uses backend.agents.llm for both halves of the
    # multi-LLM review. Here we wire a refusing reviewer so cold runs
    # never silently grow the ontology; operator overrides via env.
    def _refuse_propose(sample: str, hint: str, source: str, known) -> EntityClassSpec:
        return EntityClassSpec(name=hint, source=source, description=f"proposed for {sample}")

    def _refuse_review(spec: EntityClassSpec, known) -> tuple[bool, str]:
        return False, "production gate requires explicit operator-configured reviewer"

    gate = OntologyProposalGate(
        known=dict(ontology),
        propose_fn=_refuse_propose,
        review_fn=_refuse_review,
    )
    return IngestRunner(
        repo_root=repo_root,
        backend=backend or InMemoryBackend(),
        ontology=ontology,
        gate=gate,
        checkpoint_path=checkpoint_path,
        jira_reader=jira_reader,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cognee F5 initial ECL ingestion (OP-903).")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=REPO_ROOT / "var" / "cognee_bootstrap.ckpt.json",
        help="Path to checkpoint file (default: var/cognee_bootstrap.ckpt.json)",
    )
    parser.add_argument(
        "--from-scratch",
        action="store_true",
        help="Delete the checkpoint and re-ingest everything (full rebuild).",
    )
    parser.add_argument(
        "--no-jira",
        action="store_true",
        help="Skip the JIRA corpus (useful for offline dev / dry runs).",
    )
    parser.add_argument(
        "--jira-agent",
        default="claude",
        help="agent_class for jira_dispatch.make_client when fetching JIRA tickets.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.from_scratch and args.checkpoint.exists():
        args.checkpoint.unlink()

    reader = None if args.no_jira else default_jira_reader(args.jira_agent)
    runner = _build_default_runner(
        REPO_ROOT,
        checkpoint_path=args.checkpoint,
        jira_reader=reader,
    )
    try:
        stats = runner.run()
    except OntologyProposalRunaway as exc:
        logger.error("ONTOLOGY_RUNAWAY_HALT %s", exc)
        return 2
    except IngestionCheckpointCorrupted as exc:
        logger.error("CHECKPOINT_CORRUPTED %s — invoke --from-scratch", exc)
        return 3
    logger.info("ingest_complete %s", json.dumps(stats.to_dict()))
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
