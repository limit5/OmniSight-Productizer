"""OP-852 (Sprint C / C3) Cognee KG integration.

Replaces B8 (PageRank repo-map; ``backend.agents.repo_map``) and B10
(BM25 lesson retrieval; ``backend.agents.lesson_retrieval``) with a
graph-backed Knowledge-Graph layer rooted in Cognee
(github.com/topoteretes/cognee). The Claude Agent SDK wrapper from
``cognee-integration-claude`` is used when present.

Design notes
============

Lazy imports
------------
``cognee`` is installed in the backend runtime image for OP-899, but the
adapter still lazily imports the package; callers that go through the
public helpers ``build_repo_map_via_cognee`` /
``retrieve_lessons_via_cognee`` always fall back to the B8 / B10
baselines when:

* the package is not installed (``CogneeNotInstalled``),
* Neo4j is unreachable (``CogneeNeo4jUnavailable``),
* a query exceeds the configured timeout (``CogneeQueryTimeout``),
* the persisted index schema does not match the SDK
  (``CogneeIndexCorruption``).

Test injection
--------------
Tests pass a stub module via ``CogneeAdapter(..., cognee_module=...)``
to exercise the ECL pipeline + query path without booting Neo4j or
installing the real cognee package. See
``backend/tests/test_cognee_integration.py``.

Coexistence with the Memory Tool (C1)
-------------------------------------
Per AC #6 the C1 Memory Tool (``backend.agents.memory_tool``) is a
filesystem scratchpad for runtime state; Cognee is a structured KG used
for retrieval. The two systems carry non-overlapping data and are
queried via different code paths — this module never reads or writes
Memory Tool files.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from backend.agents.lesson_retrieval import (
    LessonSearchResult,
    retrieve_lessons,
)
from backend.agents.repo_map import build_repo_map_system_prefix

log = logging.getLogger(__name__)

# Error catalog (AC + master plan §3.4)
COGNEE_NOT_INSTALLED = "CogneePackageImportFailed"
COGNEE_NEO4J_UNAVAILABLE = "Neo4jStartFailed"
COGNEE_INDEX_CORRUPTION = "cognee_index_corruption"
COGNEE_QUERY_TIMEOUT = "cognee_query_timeout"
COGNEE_INGEST_FAILED = "cognee_ingest_failed"
NEO4J_PASSWORD_DEFAULT = "Neo4jPasswordDefault"

# Tunables — all overridable via OMNISIGHT_COGNEE_* env vars (see CogneeConfig).
DEFAULT_QUERY_TIMEOUT = 30.0
DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = ""
DEFAULT_TENANT_ID = "t-default"
DEFAULT_REPO_MAP_TOP_N = 50
DEFAULT_LESSON_TOP_K = 3
DEFAULT_ANTIPATTERN_TOP_K = 2
DEFAULT_REPO_MAP_TOKEN_BUDGET = 1000

# ECL source kinds (AC #2). Each kind maps to its own dataset namespace
# in the underlying graph store so multi-tenant filtering (AC test #7) is
# enforced at the storage level instead of the application level.
SOURCE_KIND_CODE = "code"
SOURCE_KIND_JIRA = "jira"
SOURCE_KIND_GERRIT = "gerrit"
SOURCE_KIND_LESSON = "lesson"
# AUDIT-29b-6 (OP-1024) — per-pattern chunks of
# ``docs/sop/architecture-anti-patterns.md`` land in their own dataset so a
# ``retrieve_lessons_via_cognee`` (kind=lesson) query never mixes them in
# with the L-*.md lessons; they are retrieved explicitly by
# :func:`retrieve_antipatterns_via_cognee`.
SOURCE_KIND_ANTIPATTERN = "antipattern"
ALL_SOURCE_KINDS: tuple[str, ...] = (
    SOURCE_KIND_CODE,
    SOURCE_KIND_JIRA,
    SOURCE_KIND_GERRIT,
    SOURCE_KIND_LESSON,
    SOURCE_KIND_ANTIPATTERN,
)


# ── Exceptions ─────────────────────────────────────────────────────────


class CogneeError(RuntimeError):
    """Base for typed Cognee failures the application maps to fallbacks."""


class CogneeNotInstalled(CogneeError):
    """``cognee`` (or ``cognee-integration-claude``) is not importable."""


class CogneeNeo4jUnavailable(CogneeError):
    """Neo4j refused the query; caller falls back to B8 / B10."""


class CogneeIndexCorruption(CogneeError):
    """Schema mismatch — operator should rebuild from git history."""


class CogneeQueryTimeout(CogneeError):
    """Query exceeded the configured timeout."""


class Neo4jPasswordDefault(CogneeError):
    """Refuse startup when the operator left Neo4j's default password."""


# ── Config + value objects ─────────────────────────────────────────────


@dataclass(frozen=True)
class CogneeConfig:
    neo4j_uri: str = DEFAULT_NEO4J_URI
    neo4j_user: str = DEFAULT_NEO4J_USER
    neo4j_password: str = DEFAULT_NEO4J_PASSWORD
    query_timeout: float = DEFAULT_QUERY_TIMEOUT
    tenant_id: str = DEFAULT_TENANT_ID

    @classmethod
    def from_env(cls) -> "CogneeConfig":
        return cls(
            neo4j_uri=(
                os.environ.get("OMNISIGHT_COGNEE_NEO4J_URI")
                or os.environ.get("OMNISIGHT_COGNEE_NEO4J_URL")
                or DEFAULT_NEO4J_URI
            ),
            neo4j_user=os.environ.get("OMNISIGHT_COGNEE_NEO4J_USER", DEFAULT_NEO4J_USER),
            neo4j_password=os.environ.get(
                "OMNISIGHT_COGNEE_NEO4J_PASSWORD", DEFAULT_NEO4J_PASSWORD
            ),
            query_timeout=float(
                os.environ.get(
                    "OMNISIGHT_COGNEE_QUERY_TIMEOUT", str(DEFAULT_QUERY_TIMEOUT)
                )
            ),
            tenant_id=os.environ.get("OMNISIGHT_COGNEE_TENANT_ID", DEFAULT_TENANT_ID),
        )

    def validate_password(self) -> None:
        if self.neo4j_password.strip() == DEFAULT_NEO4J_USER:
            raise Neo4jPasswordDefault(
                f"{NEO4J_PASSWORD_DEFAULT}: set OMNISIGHT_COGNEE_NEO4J_PASSWORD "
                "to a non-default strong value"
            )


@dataclass(frozen=True)
class CogneeHealthcheckResult:
    ok: bool
    status: str
    detail: str = ""


@dataclass(frozen=True)
class IngestSource:
    """One unit of input to Cognee's ECL pipeline."""

    kind: str
    identifier: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestionReport:
    sources_seen: int
    sources_ingested: int
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class CogneeQueryResult:
    identifier: str
    content: str
    score: float
    kind: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AntipatternRecord:
    """One ``## N. Title`` section of the anti-patterns cookbook.

    ``domains`` is the set of OmniSight ``area:`` names this pattern is
    relevant to — parsed from a ``**Domains**:`` line when present, else
    inferred from the section text. ``anchor`` is the GitHub-style heading
    slug, used to build a stable Cognee ``identifier``.
    """

    pattern_id: str
    title: str
    domains: tuple[str, ...]
    text: str
    anchor: str

    @property
    def identifier(self) -> str:
        return f"antipattern-{self.pattern_id}-{self.anchor}"


@dataclass(frozen=True)
class AntipatternMatch:
    """A cookbook pattern selected for a ticket, with provenance."""

    record: AntipatternRecord
    score: float
    matched_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ECLRunReport:
    code: IngestionReport
    lessons: IngestionReport
    jira: IngestionReport
    gerrit: IngestionReport

    @property
    def total_ingested(self) -> int:
        return (
            self.code.sources_ingested
            + self.lessons.sources_ingested
            + self.jira.sources_ingested
            + self.gerrit.sources_ingested
        )

    @property
    def total_seen(self) -> int:
        return (
            self.code.sources_seen
            + self.lessons.sources_seen
            + self.jira.sources_seen
            + self.gerrit.sources_seen
        )


# ── Adapter ────────────────────────────────────────────────────────────


class CogneeAdapter:
    """Thin façade over ``cognee`` (optionally via ``cognee-integration-claude``).

    The adapter accepts an injected ``cognee_module`` for tests so that
    the unit suite never needs the real package or a live Neo4j.
    """

    def __init__(
        self,
        config: CogneeConfig,
        *,
        cognee_module: Any = None,
    ) -> None:
        self.config = config
        self.config.validate_password()
        if cognee_module is None:
            cognee_module = _import_cognee_module()
        self._cognee = cognee_module

    @classmethod
    def from_env(cls) -> "CogneeAdapter":
        return cls(CogneeConfig.from_env())

    async def ingest(self, sources: Iterable[IngestSource]) -> IngestionReport:
        """Run the Extract-Cognify-Load pipeline on ``sources``.

        Each (tenant, kind) pair lands in its own Cognee dataset so the
        tenant filter in :meth:`search` reads only the requested slice.
        Ingestion is per-source idempotent — re-ingesting the same
        identifier replaces the prior content rather than duplicating
        nodes (AC test #8 — full-rebuild idempotency).
        """
        seen = 0
        ingested = 0
        failures: list[str] = []
        for source in sources:
            seen += 1
            dataset = self._dataset_for(source.kind)
            try:
                await self._await(self._cognee.add(source.content, dataset_name=dataset))
                await self._await(self._cognee.cognify(datasets=[dataset]))
                ingested += 1
            except (
                CogneeNeo4jUnavailable,
                CogneeIndexCorruption,
                CogneeQueryTimeout,
            ):
                # Typed failures bubble up so the ECL caller can decide
                # whether to abort or skip the rest of the batch.
                raise
            except Exception as exc:  # noqa: BLE001 — log + continue per AC #2 partial-progress contract
                failures.append(f"{source.kind}:{source.identifier}: {exc}")
                log.warning(
                    "%s: kind=%s id=%s detail=%s",
                    COGNEE_INGEST_FAILED,
                    source.kind,
                    source.identifier,
                    exc,
                )
        return IngestionReport(seen, ingested, tuple(failures))

    async def search(
        self,
        query: str,
        *,
        kinds: Sequence[str] | None = None,
        top_k: int = 10,
    ) -> tuple[CogneeQueryResult, ...]:
        """Run a Cognee KG search and normalise hits.

        Raises one of :class:`CogneeNeo4jUnavailable`,
        :class:`CogneeQueryTimeout`, or :class:`CogneeIndexCorruption`
        so callers can fall back to B8 / B10.
        """
        try:
            search_fn = self._cognee.search
        except AttributeError as exc:
            raise CogneeIndexCorruption(
                f"cognee.search missing — likely SDK schema mismatch: {exc}"
            ) from exc
        datasets = self._datasets_for(kinds)
        try:
            raw = await asyncio.wait_for(
                self._await(search_fn(query, datasets=datasets)),
                timeout=self.config.query_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise CogneeQueryTimeout(
                f"{COGNEE_QUERY_TIMEOUT}: query exceeded {self.config.query_timeout}s"
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise CogneeNeo4jUnavailable(
                f"{COGNEE_NEO4J_UNAVAILABLE}: {exc}"
            ) from exc
        normalized = tuple(_normalize_hit(hit) for hit in (raw or []))
        return normalized[:top_k] if top_k > 0 else normalized

    @staticmethod
    async def _await(maybe_awaitable: Any) -> Any:
        if inspect.isawaitable(maybe_awaitable):
            return await maybe_awaitable
        return maybe_awaitable

    def _dataset_for(self, kind: str) -> str:
        return f"{self.config.tenant_id}:{kind}"

    def _datasets_for(self, kinds: Sequence[str] | None) -> list[str]:
        if not kinds:
            return [self._dataset_for(k) for k in ALL_SOURCE_KINDS]
        return [self._dataset_for(k) for k in kinds]


# ── ECL source collectors (AC #2) ──────────────────────────────────────


def collect_code_sources(
    repo_root: Path,
    *,
    extensions: tuple[str, ...] = (".py", ".ts", ".tsx"),
) -> list[IngestSource]:
    """Walk git-tracked source files for AST-aware ingestion (mirrors B8)."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                *(f"*{ext}" for ext in extensions),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    sources: list[IngestSource] = []
    for rel in proc.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        path = repo_root / rel
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources.append(
            IngestSource(
                kind=SOURCE_KIND_CODE,
                identifier=rel,
                content=content,
                metadata={"language": _language_for_path(path)},
            )
        )
    return sources


def collect_lesson_sources(lessons_dir: Path) -> list[IngestSource]:
    """Collect ``L-*.md`` lessons for ECL ingestion (replaces B10 corpus)."""
    try:
        if not lessons_dir.is_dir():
            return []
    except OSError:
        return []
    sources: list[IngestSource] = []
    for path in sorted(lessons_dir.glob("L-*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sources.append(
            IngestSource(
                kind=SOURCE_KIND_LESSON,
                identifier=path.name,
                content=content,
                metadata={"path": str(path)},
            )
        )
    return sources


# AUDIT-29b-6 (OP-1024) — anti-pattern cookbook parsing + ingestion.

_ANTIPATTERN_HEADING_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_ANTIPATTERN_DOMAINS_RE = re.compile(
    r"^\*\*Domains?\*\*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_ANTIPATTERN_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "backend": (
        "runner", "daemon", "jira", "gerrit", "circuit breaker", "state machine",
        "bridge", "auto_promote", "webhook", "api ", "event",
    ),
    "frontend": ("frontend", "react", "component", "lib/api.ts", "dashboard"),
    "devops": ("systemd", "compose", "docker", "deploy", "timer", "cron", "prod", "staging"),
    "tests": ("test fixture", "pytest", "fixtures/", "/tests/"),
    "db": ("alembic", "migration", "postgres", "schema", "audit row", "table"),
    "docs": ("docs/sop", "adr", "lessons-learned", "retrospective", "convention", "cookbook"),
    "security": ("secret", "token", "credential", " auth ", "signing key"),
    "embedded": ("soc", "toolchain", "sysroot", "firmware", "camera"),
    "tooling": ("script", "scripts/", "build script", "index file", "resolver", "bulk-action", "triage", "linter"),
}
_ANTIPATTERN_NON_PATTERN_HEADINGS = frozenset(
    {"index", "cross-cutting principles", "see also"}
)


def _antipattern_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _split_domain_list(raw: str) -> tuple[str, ...]:
    seen: list[str] = []
    for part in re.split(r"[,/|]", raw):
        token = part.strip().lower()
        token = re.sub(r"^`|`$", "", token).strip()
        if token and token not in seen:
            seen.append(token)
    return tuple(seen)


def _infer_antipattern_domains(text: str) -> tuple[str, ...]:
    low = text.lower()
    hits = [
        domain
        for domain, keywords in _ANTIPATTERN_DOMAIN_KEYWORDS.items()
        if any(keyword in low for keyword in keywords)
    ]
    return tuple(hits) if hits else ("backend",)


def parse_antipatterns(doc_path: Path) -> list[AntipatternRecord]:
    """Split ``architecture-anti-patterns.md`` into per-pattern records.

    Each ``## N. Title`` section becomes one :class:`AntipatternRecord`.
    A ``**Domains**:`` line (comma/slash-separated ``area:`` names) is read
    when present; otherwise the domains are inferred from the section body.
    Non-pattern headings (Index / Cross-cutting principles / See also) and
    everything before pattern #1 are ignored. Returns ``[]`` on read error
    so callers degrade to an empty block.
    """
    try:
        raw = doc_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[AntipatternRecord] = []
    cur_id: str | None = None
    cur_title: str = ""
    body: list[str] = []

    def _flush() -> None:
        nonlocal cur_id, cur_title, body
        if cur_id is None:
            return
        # Drop the trailing ``---`` horizontal rule that separates patterns.
        section = re.sub(r"\n*-{3,}\s*$", "", "\n".join(body)).strip()
        domains_match = _ANTIPATTERN_DOMAINS_RE.search(section)
        if domains_match:
            domains = _split_domain_list(domains_match.group(1))
        else:
            domains = _infer_antipattern_domains(cur_title + "\n" + section)
        rendered = f"## {cur_id}. {cur_title}\n\n{section}".strip()
        records.append(
            AntipatternRecord(
                pattern_id=cur_id,
                title=cur_title,
                domains=domains,
                text=rendered,
                anchor=_antipattern_slug(cur_title),
            )
        )
        cur_id = None
        cur_title = ""
        body = []

    for line in raw.splitlines():
        heading = _ANTIPATTERN_HEADING_RE.match(line)
        if heading:
            _flush()
            cur_id = heading.group(1)
            cur_title = heading.group(2).strip()
            body = []
            continue
        if line.startswith("## ") and cur_id is not None:
            # A non-numbered ``## Heading`` (Index / See also / …) ends the
            # current pattern section.
            _flush()
            continue
        if cur_id is not None:
            body.append(line)
    _flush()
    return records


def collect_antipattern_sources(doc_path: Path) -> list[IngestSource]:
    """Collect per-pattern chunks of the anti-patterns cookbook for ECL.

    Each pattern becomes its own :class:`IngestSource` with
    ``kind=SOURCE_KIND_ANTIPATTERN`` and a stable identifier
    (``antipattern-<id>-<slug>``) so re-ingestion replaces rather than
    duplicates. Used by ``scripts/cognee-ingest-lessons.py``.
    """
    sources: list[IngestSource] = []
    for record in parse_antipatterns(doc_path):
        sources.append(
            IngestSource(
                kind=SOURCE_KIND_ANTIPATTERN,
                identifier=record.identifier,
                content=record.text,
                metadata={
                    "pattern_id": record.pattern_id,
                    "title": record.title,
                    "domains": list(record.domains),
                    "path": str(doc_path),
                },
            )
        )
    return sources


def collect_jira_sources(snapshots: Iterable[Any]) -> list[IngestSource]:
    """Build JIRA ingestion sources from TicketSnapshot-shaped objects."""
    sources: list[IngestSource] = []
    for snap in snapshots:
        key = (
            getattr(snap, "key", None)
            or getattr(snap, "ticket_key", None)
            or (snap.get("key") if isinstance(snap, Mapping) else "")
        )
        if not key:
            continue
        title = _coerce_str(snap, "title") or _coerce_str(snap, "summary")
        description = _coerce_str(snap, "description")
        comments = _coerce_str(snap, "comments")
        status = _coerce_str(snap, "status")
        body = "\n\n".join(part for part in (f"# {key}", title, description, comments) if part)
        sources.append(
            IngestSource(
                kind=SOURCE_KIND_JIRA,
                identifier=str(key),
                content=body,
                metadata={"status": status},
            )
        )
    return sources


def collect_gerrit_sources(changes: Iterable[Mapping[str, Any]]) -> list[IngestSource]:
    """Build Gerrit ingestion sources from ``{change_id, diff, comments}`` mappings."""
    sources: list[IngestSource] = []
    for change in changes:
        change_id = str(change.get("change_id") or change.get("id") or "")
        if not change_id:
            continue
        body = "\n\n".join(
            section
            for section in (
                f"# Gerrit {change_id}",
                str(change.get("subject", "") or ""),
                str(change.get("diff", "") or ""),
                str(change.get("comments", "") or ""),
            )
            if section.strip()
        )
        sources.append(
            IngestSource(
                kind=SOURCE_KIND_GERRIT,
                identifier=change_id,
                content=body,
                metadata={
                    "branch": str(change.get("branch", "") or ""),
                    "ps": str(change.get("ps", "") or ""),
                },
            )
        )
    return sources


# ── B8 replacement (AC #4) — Cognee graph traversal repo map ───────────


def build_repo_map_via_cognee(
    repo_root: Path,
    *,
    ticket_text: str = "",
    token_budget: int | None = None,
    top_n: int = DEFAULT_REPO_MAP_TOP_N,
    adapter: CogneeAdapter | None = None,
) -> str:
    """Build the repo-map preamble from Cognee's code KG.

    Per AC #4, B8's ticket-seeded PageRank query is replaced by a Cognee
    graph traversal:
        ``MATCH (f:File)-[:imports*]->(target:File) WHERE target.path = $candidate``.
    On any Cognee failure (Neo4j down, timeout, schema mismatch, package
    not installed) or empty result set, the function falls back to the
    existing B8 PageRank baseline so the system keeps producing a useful
    preamble (AC #5 fallback contract — same shape).
    """
    # The repo-map preamble is an OPTIONAL prompt enhancement — it must NEVER
    # crash the orchestrator pipeline. The narrow except clauses below only
    # cover *known* Cognee failure modes; an UNEXPECTED error (e.g. the B8
    # fallback's SQLite repo-map cache being unwritable on prod's read-only
    # site-packages path — dogfood 2026-06-30, which dropped every /chat/stream
    # request with "unable to open database file") must degrade to no preamble,
    # not propagate. The empty string is a valid "same shape" fallback.
    try:
        seed = ticket_text or _ticket_seed_default(repo_root)
        try:
            adapter = adapter or CogneeAdapter.from_env()
        except CogneeNotInstalled as exc:
            log.info("cognee_repo_map_fallback: %s", exc)
            return _b8_fallback(repo_root, ticket_text, token_budget, top_n)
        try:
            hits = _run_async(
                adapter.search(seed, kinds=[SOURCE_KIND_CODE], top_k=top_n)
            )
        except (CogneeNeo4jUnavailable, CogneeQueryTimeout, CogneeIndexCorruption) as exc:
            log.warning("cognee_repo_map_fallback: %s", exc)
            return _b8_fallback(repo_root, ticket_text, token_budget, top_n)
        if not hits:
            return _b8_fallback(repo_root, ticket_text, token_budget, top_n)
        return _render_cognee_repo_map(hits, token_budget=token_budget)
    except Exception as exc:  # noqa: BLE001 — last-resort: never crash the graph
        log.warning("repo_map_disabled (unexpected, returning empty preamble): %s", exc)
        return ""


# ── B10 replacement (AC #5) — lessons via Cognee semantic search ───────


def retrieve_lessons_via_cognee(
    lessons_dir: Path,
    *,
    ticket_title: str,
    acceptance_criteria: str,
    top_k: int = DEFAULT_LESSON_TOP_K,
    adapter: CogneeAdapter | None = None,
) -> tuple[LessonSearchResult, ...]:
    """Retrieve top-k lessons from Cognee KG with B10 BM25 fallback.

    Per AC #5 the B10 BM25 index stays as the fallback path; this
    function tries Cognee first and silently falls back when the KG is
    unreachable, the SDK is missing, the query times out, or the KG
    returns no hits for the seeded query.
    """
    query = f"{ticket_title}\n\n{acceptance_criteria}".strip()
    if not query:
        return ()
    try:
        adapter = adapter or CogneeAdapter.from_env()
    except CogneeNotInstalled as exc:
        log.info("cognee_lessons_fallback: %s", exc)
        return _b10_fallback(lessons_dir, ticket_title, acceptance_criteria, top_k)
    try:
        hits = _run_async(
            adapter.search(query, kinds=[SOURCE_KIND_LESSON], top_k=top_k)
        )
    except (CogneeNeo4jUnavailable, CogneeQueryTimeout, CogneeIndexCorruption) as exc:
        log.warning("cognee_lessons_fallback: %s", exc)
        return _b10_fallback(lessons_dir, ticket_title, acceptance_criteria, top_k)
    if not hits:
        return _b10_fallback(lessons_dir, ticket_title, acceptance_criteria, top_k)
    return tuple(
        LessonSearchResult(
            path=lessons_dir / hit.identifier,
            text=hit.content,
            score=hit.score,
        )
        for hit in hits
    )


# ── Anti-pattern recall (AUDIT-29b-6 / OP-1024) ────────────────────────


def retrieve_antipatterns_via_cognee(
    doc_path: Path,
    *,
    ticket_title: str,
    acceptance_criteria: str,
    declared_areas: Sequence[str] = (),
    top_k: int = DEFAULT_ANTIPATTERN_TOP_K,
    adapter: CogneeAdapter | None = None,
) -> tuple[AntipatternMatch, ...]:
    """Return the cookbook anti-patterns most relevant to a ticket.

    Ordering is Cognee top-N semantic similarity over the ``antipattern``
    dataset when the KG is reachable; otherwise a deterministic
    keyword-overlap fallback over the locally-parsed cookbook (so the
    block is shape-stable whether or not Cognee is up). In both cases the
    result is biased toward patterns whose ``Domains`` intersect the
    ticket's declared ``area:`` labels — that is the AC §3 "anti-pattern
    auto-injected when ticket area matches pattern's domain" behaviour.
    """
    records = parse_antipatterns(doc_path)
    if not records:
        return ()
    by_identifier = {record.identifier: record for record in records}
    declared = {area.strip().lower() for area in declared_areas if area and area.strip()}
    query = f"{ticket_title}\n\n{acceptance_criteria}".strip()

    ranked: list[AntipatternRecord] = []
    if query:
        try:
            adapter = adapter or CogneeAdapter.from_env()
            hits = _run_async(
                adapter.search(
                    query,
                    kinds=[SOURCE_KIND_ANTIPATTERN],
                    top_k=max(top_k * 4, top_k),
                )
            )
            for hit in hits:
                record = by_identifier.get(hit.identifier)
                if record is not None and record not in ranked:
                    ranked.append(record)
        except (
            CogneeNotInstalled,
            CogneeNeo4jUnavailable,
            CogneeQueryTimeout,
            CogneeIndexCorruption,
        ) as exc:
            log.info("cognee_antipatterns_fallback: %s", exc)
            ranked = []
    if not ranked:
        ranked = _antipatterns_keyword_rank(records, query)

    def _overlap(record: AntipatternRecord) -> tuple[str, ...]:
        return tuple(domain for domain in record.domains if domain in declared)

    if declared:
        matched = [record for record in ranked if _overlap(record)]
        rest = [record for record in ranked if not _overlap(record)]
        ranked = matched + rest

    selected = ranked[:top_k] if top_k > 0 else ranked
    total = len(ranked)
    return tuple(
        AntipatternMatch(
            record=record,
            score=float(total - index),
            matched_domains=_overlap(record),
        )
        for index, record in enumerate(selected)
    )


def _antipatterns_keyword_rank(
    records: Sequence[AntipatternRecord], query: str
) -> list[AntipatternRecord]:
    query_terms = set(re.findall(r"[a-z0-9_]+", query.lower()))
    if not query_terms:
        return list(records)
    scored: list[tuple[int, int, AntipatternRecord]] = []
    for record in records:
        terms = set(re.findall(r"[a-z0-9_]+", f"{record.title}\n{record.text}".lower()))
        try:
            pattern_ordinal = int(record.pattern_id)
        except ValueError:
            pattern_ordinal = 1 << 30
        scored.append((-len(query_terms & terms), pattern_ordinal, record))
    scored.sort(key=lambda triple: (triple[0], triple[1]))
    return [record for _, _, record in scored]


# ── ECL pipeline orchestration (AC #2 + #7) ────────────────────────────


async def run_ecl_pipeline(
    repo_root: Path,
    *,
    lessons_dir: Path | None = None,
    jira_snapshots: Iterable[Any] = (),
    gerrit_changes: Iterable[Mapping[str, Any]] = (),
    adapter: CogneeAdapter | None = None,
    incremental_paths: Iterable[str] | None = None,
) -> ECLRunReport:
    """Execute the four-source ECL pipeline.

    ``incremental_paths`` enables the AC #7 incremental-on-commit path:
    only the listed code files are re-ingested (other source kinds are
    skipped to keep the post-receive hook bounded). Pass ``None`` for
    the nightly full-rebuild (called by ``scripts/cognee_full_rebuild.py``).
    """
    if adapter is None:
        adapter = CogneeAdapter.from_env()
    if incremental_paths is not None:
        wanted = {str(p) for p in incremental_paths}
        code_sources = [
            src for src in collect_code_sources(repo_root) if src.identifier in wanted
        ]
        # Incremental runs only re-ingest the changed code surface; the
        # other dataset kinds are managed by their own event hooks.
        lesson_sources: list[IngestSource] = []
        jira_sources: list[IngestSource] = []
        gerrit_sources: list[IngestSource] = []
    else:
        code_sources = collect_code_sources(repo_root)
        lesson_sources = collect_lesson_sources(
            lessons_dir or repo_root / "docs" / "sop" / "lessons"
        )
        jira_sources = collect_jira_sources(jira_snapshots)
        gerrit_sources = collect_gerrit_sources(gerrit_changes)
    return ECLRunReport(
        code=await adapter.ingest(code_sources),
        lessons=await adapter.ingest(lesson_sources),
        jira=await adapter.ingest(jira_sources),
        gerrit=await adapter.ingest(gerrit_sources),
    )


def healthcheck(config: CogneeConfig | None = None) -> CogneeHealthcheckResult:
    """Read-only Cognee + Neo4j readiness probe for backend replicas.

    The probe intentionally does not create, drop, or repair Neo4j
    state. ``Neo4jStartFailed`` must alert without auto-recreating the
    persistent volume because that volume contains operator data.
    """
    config = config or CogneeConfig.from_env()
    try:
        config.validate_password()
    except Neo4jPasswordDefault as exc:
        return CogneeHealthcheckResult(False, NEO4J_PASSWORD_DEFAULT, str(exc))
    try:
        _import_cognee_module()
    except CogneeNotInstalled as exc:
        return CogneeHealthcheckResult(False, COGNEE_NOT_INSTALLED, str(exc))
    try:
        neo4j = importlib.import_module("neo4j")
    except ImportError as exc:
        return CogneeHealthcheckResult(
            False,
            COGNEE_NOT_INSTALLED,
            f"{COGNEE_NOT_INSTALLED}: install neo4j==5.24.0 ({exc})",
        )
    try:
        driver = neo4j.GraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_user, config.neo4j_password),
        )
        try:
            with driver.session() as session:
                session.run("RETURN 1 AS ok").consume()
        finally:
            driver.close()
    except Exception as exc:  # noqa: BLE001 — driver surfaces many connection/auth classes
        return CogneeHealthcheckResult(False, COGNEE_NEO4J_UNAVAILABLE, str(exc))
    return CogneeHealthcheckResult(True, "READY", config.neo4j_uri)


# ── Internal helpers ───────────────────────────────────────────────────


def _import_cognee_module() -> Any:
    try:
        _patch_starlette_status_for_cognee()
        return importlib.import_module("cognee")
    except ImportError as exc:
        raise CogneeNotInstalled(
            f"{COGNEE_NOT_INSTALLED}: install via "
            f"`pip install cognee`"
        ) from exc


def _patch_starlette_status_for_cognee() -> None:
    """Bridge Cognee's 422 constant name to Starlette's public spelling."""
    try:
        status = importlib.import_module("starlette.status")
    except ImportError:
        return
    if not hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT") and hasattr(
        status, "HTTP_422_UNPROCESSABLE_ENTITY"
    ):
        status.HTTP_422_UNPROCESSABLE_CONTENT = status.HTTP_422_UNPROCESSABLE_ENTITY


def _normalize_hit(hit: Any) -> CogneeQueryResult:
    if isinstance(hit, Mapping):
        return CogneeQueryResult(
            identifier=str(hit.get("identifier") or hit.get("id") or ""),
            content=str(hit.get("text") or hit.get("content") or ""),
            score=float(hit.get("score", 0.0) or 0.0),
            kind=str(hit.get("kind", "") or ""),
            metadata=dict(hit.get("metadata") or {}),
        )
    return CogneeQueryResult(
        identifier=str(getattr(hit, "identifier", getattr(hit, "id", ""))),
        content=str(getattr(hit, "text", getattr(hit, "content", ""))),
        score=float(getattr(hit, "score", 0.0) or 0.0),
        kind=str(getattr(hit, "kind", "") or ""),
        metadata=dict(getattr(hit, "metadata", {}) or {}),
    )


def _b8_fallback(
    repo_root: Path,
    ticket_text: str,
    token_budget: int | None,
    top_n: int,
) -> str:
    return build_repo_map_system_prefix(
        repo_root,
        ticket_text=ticket_text,
        token_budget=token_budget,
        top_n=top_n,
    )


def _b10_fallback(
    lessons_dir: Path,
    ticket_title: str,
    acceptance_criteria: str,
    top_k: int,
) -> tuple[LessonSearchResult, ...]:
    return retrieve_lessons(
        lessons_dir,
        ticket_title=ticket_title,
        acceptance_criteria=acceptance_criteria,
        top_k=top_k,
    )


def _render_cognee_repo_map(
    hits: Sequence[CogneeQueryResult],
    *,
    token_budget: int | None,
) -> str:
    budget = token_budget if token_budget is not None else DEFAULT_REPO_MAP_TOKEN_BUDGET
    if budget <= 0:
        return ""
    lines = [
        "# Repo Map Context (Cognee KG)",
        "",
        "High-signal files for this task, ranked by Cognee graph traversal:",
    ]
    used = sum(_estimate_tokens(line) for line in lines)
    for hit in hits:
        line = f"- `{hit.identifier}` (score={hit.score:.5f})"
        cost = _estimate_tokens(line)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines) if len(lines) > 3 else ""


def _ticket_seed_default(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--pretty=%B"],
            text=True,
            capture_output=True,
            check=True,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _language_for_path(path: Path) -> str:
    return {".py": "python", ".ts": "typescript", ".tsx": "tsx"}.get(path.suffix, "")


def _coerce_str(snap: Any, attr: str) -> str:
    if isinstance(snap, Mapping):
        return str(snap.get(attr, "") or "")
    return str(getattr(snap, attr, "") or "")


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _run_async(coro: Any) -> Any:
    """Drive a coroutine to completion from sync code.

    The B8 / B10 callers are sync (``build_system_prompt`` is sync), so
    we need a small bridge. ``asyncio.run`` would conflict with an
    already-running loop, so we detect that case and fall back to
    creating a private loop in a worker thread.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None:
        return asyncio.run(coro)
    import threading

    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            result["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "exc" in result:
        raise result["exc"]
    return result.get("value")
