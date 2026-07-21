"""U6-0 T5a ProvenanceSnapshot substrate (dormant, stdlib-only).

A ``ProvenanceSnapshot`` records the memory/retrieval/tool SOURCES that
were exposed to the model before a turn — for INV-3 AUDIT and (later, in
T9/T10) for a grant to bind. It is NOT a byte-exact request
reconstruction.

HARD INVARIANT — provenance is DESCRIPTIVE: no field in this module ever
authorizes a side effect, and the kernel verdict is independent of
provenance content. The kernel (``authorization_kernel.authorize_action``)
only reads tool metadata + principal; ``provenance_snapshot_ids`` is a
pure passthrough there, and the anti-forge test in
``backend/tests/test_provenance.py`` proves the verdict is invariant to
adversarial provenance.

Attestation attests the provenance CHAIN (the row came via a trusted
writer), NEVER content authorship — a ``DB_VERIFIED_GERRIT`` row's text
is still contributor-authored. Records store digests + ids only, never
bodies; never log a record's fields raw.

Cache invariants (``SnapshotCache``):
  * The cache is process-local and NON-authoritative. A cache miss is
    neither denial nor approval; cache resolution NEVER determines
    authorization.
  * The durable, transaction-aware store that a grant binds is SEPARATE
    from this cache (``PgSnapshotRepository``); later T9/T10 leaves wire it
    into the grant path.
  * A grant binds the snapshot VALUE paired with the response, never a
    ``cache.get(id)`` re-lookup.

This module is ADDITIVE and DORMANT: nothing imports it yet (capture
wiring is T5b). It is a stdlib-only leaf — it imports nothing from
``backend``; the import-direction test enforces this.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import logging
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Protocol, Union


logger = logging.getLogger(__name__)

# ── A. Records + attestation ───────────────────────────────────────────

# Source-kind constants for ProvenanceRecord.source_kind.
EPISODIC = "episodic"
CHAT_HISTORY = "chat_history"
RAG_DOC = "rag_doc"
RUNNER_MEMORY_FILE = "runner_memory_file"
TOOL_RESULT = "tool_result"
READ_FILE = "read_file"
MCP_RESULT = "mcp_result"
A2A_RESULT = "a2a_result"
STALE_REFRESH = "stale_refresh"
# β-3a (leg-2): published learned-item cards injected into a prompt. A
# MEMORY kind — auto-auth downgrades on it (INV-2), same as EPISODIC.
LEARNED_ITEM = "learned_item"

SOURCE_KINDS = frozenset({
    EPISODIC,
    CHAT_HISTORY,
    RAG_DOC,
    RUNNER_MEMORY_FILE,
    TOOL_RESULT,
    READ_FILE,
    MCP_RESULT,
    A2A_RESULT,
    STALE_REFRESH,
    LEARNED_ITEM,
})


class Attestation(str, Enum):
    UNATTESTED = "unattested"
    DB_VERIFIED_GERRIT = "db_verified_gerrit"
    DB_QUARANTINED = "db_quarantined"


@dataclass(frozen=True)
class ProvenanceRecord:
    """One exposed source. Digest + ids only — never a content body."""

    source_kind: str
    source_id: str
    content_digest: str
    attestation: Attestation
    verification_status: str
    verification_authority: str
    tenant_id: str
    visibility: str


# Cap on the ENCODED bytes fed to sha256 — slice the bytes, not the str,
# so a huge frame can never stall the caller.
_MAX_HASH_BYTES = 1 * 1024 * 1024


def digest(text: str) -> str:
    """Bare hex sha256 of the first ``_MAX_HASH_BYTES`` encoded bytes.

    ALWAYS returns a bare ``str`` (never a tuple — ``content_digest`` is
    a ``str`` field). Truncation is surfaced by :func:`record_content`
    via the ``"digest_truncated"`` omission tag, not by this function.
    """
    enc = text.encode("utf-8", "surrogatepass")
    return hashlib.sha256(enc[:_MAX_HASH_BYTES]).hexdigest()


def _encoded_over_cap(text: str) -> bool:
    return len(text.encode("utf-8", "surrogatepass")) > _MAX_HASH_BYTES


def attested_episodic_record(row: dict, exposed_text: str) -> ProvenanceRecord:
    """The ONLY constructor that may set a ``DB_*`` attestation.

    Derived from the row's REAL values — the episodic schema has
    ``verified`` + ``source`` (there is NO ``state`` column). The 0260
    CHECK enforces ``verified ⇒ source='service_gerrit_merge' AND
    verification_authority='gerrit'``, so trusting ``row["verified"]``
    for the ``DB_VERIFIED_GERRIT`` label is sound — the row cannot be
    verified without the gerrit authority.
    """
    verified = bool(row.get("verified"))
    return ProvenanceRecord(
        source_kind=EPISODIC,
        source_id=str(row.get("id") or ""),
        content_digest=digest(exposed_text),
        attestation=(
            Attestation.DB_VERIFIED_GERRIT if verified else Attestation.DB_QUARANTINED
        ),
        verification_status="verified" if verified else "quarantined",
        verification_authority=str(row.get("verification_authority") or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        visibility=str(row.get("visibility") or ""),
    )


def untrusted_record(
    source_kind: str,
    source_id: str,
    exposed_text: str,
    *,
    tenant_id: str = "",
    visibility: str = "",
) -> ProvenanceRecord:
    """Constructor for every non-episodic source. NEVER yields ``DB_*``."""
    return ProvenanceRecord(
        source_kind=source_kind,
        source_id=source_id,
        content_digest=digest(exposed_text),
        attestation=Attestation.UNATTESTED,
        verification_status="none",
        verification_authority="",
        tenant_id=tenant_id,
        visibility=visibility,
    )


# ── B. Snapshot ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProvenanceSnapshot:
    snapshot_id: str
    records: tuple[ProvenanceRecord, ...]
    completeness: str  # "complete" | "partial"
    omissions: tuple[str, ...]
    manifest_digest: str


def _frame(part: str) -> bytes:
    # Length-framed so ("a","bc") != ("ab","c").
    b = part.encode("utf-8", "surrogatepass")
    return str(len(b)).encode("ascii") + b":" + b


def _manifest_digest(
    snapshot_id: str,
    records: tuple[ProvenanceRecord, ...],
    completeness: str,
    omissions: tuple[str, ...],
) -> str:
    """Canonical digest over ALL immutable snapshot fields.

    This is CORRUPTION-DETECTION, not a signature — a modifier can
    recompute it. Omissions are sorted so order never affects the digest.
    """
    h = hashlib.sha256()
    h.update(_frame(snapshot_id))
    for rec in records:
        for f in dataclasses.fields(rec):
            # Attestation is a str subclass, so this frames its value.
            h.update(_frame(getattr(rec, f.name)))
    h.update(_frame(completeness))
    for tag in sorted(omissions):
        h.update(_frame(tag))
    return h.hexdigest()


def _build_snapshot(
    records: tuple[ProvenanceRecord, ...],
    *,
    completeness: str,
    omissions: tuple[str, ...],
    snapshot_id: str | None = None,
) -> ProvenanceSnapshot:
    sid = snapshot_id if snapshot_id is not None else "psnap-" + uuid.uuid4().hex
    return ProvenanceSnapshot(
        snapshot_id=sid,
        records=tuple(records),
        completeness=completeness,
        omissions=tuple(omissions),
        manifest_digest=_manifest_digest(sid, tuple(records), completeness, tuple(omissions)),
    )


# ── C. Tagged turn-provenance (threaded WHOLE, not erased to an id) ────


@dataclass(frozen=True)
class ModelSnapshot:
    snapshot: ProvenanceSnapshot


@dataclass(frozen=True)
class NoModelInput:
    authorization_source: str


@dataclass(frozen=True)
class CaptureUnavailable:
    reason: str


TurnProvenance = Union[ModelSnapshot, NoModelInput, CaptureUnavailable]


def audit_ids(tp: TurnProvenance) -> tuple[str, ...]:
    if isinstance(tp, ModelSnapshot):
        return (tp.snapshot.snapshot_id,)
    return ()


def is_grant_eligible(tp: TurnProvenance) -> bool:
    return isinstance(tp, ModelSnapshot) and tp.snapshot.completeness == "complete"


def for_model_response(sealed: ModelSnapshot | CaptureUnavailable) -> TurnProvenance:
    """Model/tool-adapter entry point.

    A model turn can never be "no model input" — rejecting it here keeps
    the deterministic-command label unforgeable from the model path.
    """
    if isinstance(sealed, NoModelInput):
        raise ValueError("a model turn can never carry NoModelInput provenance")
    if not isinstance(sealed, (ModelSnapshot, CaptureUnavailable)):
        raise TypeError(f"not a sealed turn provenance: {type(sealed).__name__}")
    return sealed


def for_deterministic_command(authorization_source: str) -> NoModelInput:
    """The ONLY constructor of ``NoModelInput`` — deterministic
    slash-command path, called only after a command actually matched."""
    return NoModelInput(authorization_source=authorization_source)


# ── D. Cache (NOT the durable grant seam) ──────────────────────────────

_CACHE_CAP = 2048


class DurableSnapshotRepository(Protocol):
    """Interface for the T9/T10 durable, tenant-scoped snapshot store."""

    async def persist(
        self,
        snapshot: ProvenanceSnapshot,
        *,
        tenant_id: str,
        model_call_id: str,
        request_id: str = "",
    ) -> None: ...

    async def get(
        self,
        snapshot_id: str,
        *,
        tenant_id: str,
    ) -> ProvenanceSnapshot | None: ...


def _records_to_json(records: tuple[ProvenanceRecord, ...]) -> str:
    """Serialize records with all eight fields and stable key ordering."""
    payload = [
        {field.name: getattr(record, field.name) for field in dataclasses.fields(record)}
        for record in records
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _records_from_json(s: str) -> tuple[ProvenanceRecord, ...]:
    """Reconstruct the immutable record tuple from its JSON representation."""
    payload = json.loads(s)
    return tuple(
        ProvenanceRecord(
            source_kind=item["source_kind"],
            source_id=item["source_id"],
            content_digest=item["content_digest"],
            attestation=Attestation(item["attestation"]),
            verification_status=item["verification_status"],
            verification_authority=item["verification_authority"],
            tenant_id=item["tenant_id"],
            visibility=item["visibility"],
        )
        for item in payload
    )


class PgSnapshotRepository:
    """PostgreSQL-backed implementation of :class:`DurableSnapshotRepository`."""

    def __init__(self, pool):
        self._pool = pool

    async def persist(
        self,
        snap: ProvenanceSnapshot,
        *,
        tenant_id: str,
        model_call_id: str,
        request_id: str = "",
    ) -> None:
        if snap.completeness != "complete":
            raise ValueError("only complete provenance snapshots may be persisted")

        # Resolve lazily through stdlib importlib: importing this leaf must not
        # import any backend module (enforced by test_provenance.py).
        db = importlib.import_module("backend.db")
        async with self._pool.acquire() as conn:
            await db.insert_provenance_snapshot(
                conn,
                snapshot_id=snap.snapshot_id,
                tenant_id=tenant_id,
                model_call_id=model_call_id,
                request_id=request_id,
                records_json=_records_to_json(snap.records),
                omissions_json=json.dumps(
                    snap.omissions, sort_keys=True, separators=(",", ":")
                ),
                manifest_digest=snap.manifest_digest,
            )

    async def get(
        self,
        snapshot_id: str,
        *,
        tenant_id: str,
    ) -> ProvenanceSnapshot | None:
        db = importlib.import_module("backend.db")
        async with self._pool.acquire() as conn:
            row = await db.get_provenance_snapshot(
                conn, snapshot_id, tenant_id=tenant_id
            )
        if row is None:
            return None

        records = _records_from_json(row["records"])
        omissions = tuple(json.loads(row["omissions"]))
        reconstructed = ProvenanceSnapshot(
            snapshot_id=row["snapshot_id"],
            records=records,
            completeness=row["completeness"],
            omissions=omissions,
            manifest_digest=row["manifest_digest"],
        )
        expected = _manifest_digest(
            reconstructed.snapshot_id,
            reconstructed.records,
            reconstructed.completeness,
            reconstructed.omissions,
        )
        if expected != reconstructed.manifest_digest:
            logger.warning(
                "provenance snapshot manifest mismatch on read: %s", snapshot_id
            )
            return None
        return reconstructed


class SnapshotCache:
    """Process-local, NON-authoritative snapshot cache (see module docstring)."""

    def __init__(self, cap: int = _CACHE_CAP) -> None:
        self._cap = cap
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, ProvenanceSnapshot] = OrderedDict()

    def put(self, snap: ProvenanceSnapshot) -> None:
        collision = False
        with self._lock:  # no logging while holding the lock
            existing = self._entries.get(snap.snapshot_id)
            if existing is not None:
                if existing.manifest_digest == snap.manifest_digest:
                    return  # identical re-put is a no-op
                collision = True
            else:
                self._entries[snap.snapshot_id] = snap
                while len(self._entries) > self._cap:
                    self._entries.popitem(last=False)  # O(1) FIFO evict
        if collision:
            # Same id, different manifest ⇒ a bug (122-bit ids don't collide).
            raise ValueError(f"snapshot id collision: {snap.snapshot_id}")

    def get(self, snapshot_id: str) -> ProvenanceSnapshot | None:
        with self._lock:
            return self._entries.get(snapshot_id)


# ── E. Collector (latches failure; seal DERIVES eligibility) ───────────


def _record_size(rec: ProvenanceRecord) -> int:
    return sum(len(getattr(rec, f.name)) for f in dataclasses.fields(rec))


class ProvenanceCollector:
    _CAP_RECORDS = 512
    _CAP_BYTES = 256 * 1024

    def __init__(self) -> None:
        self._records: list[ProvenanceRecord] = []
        self._seen: set[ProvenanceRecord] = set()
        self._bytes = 0
        self._failed = False
        self._omissions: set[str] = set()

    def record(self, rec: ProvenanceRecord) -> None:
        if rec in self._seen:  # dedupe on ALL fields
            return
        if len(self._records) + 1 > self._CAP_RECORDS:
            self._omissions.add("count_cap")
            return
        size = _record_size(rec)
        if self._bytes + size > self._CAP_BYTES:
            self._omissions.add("byte_cap")
            return
        self._seen.add(rec)
        self._records.append(rec)
        self._bytes += size

    def add_omission(self, tag: str) -> None:
        """Mark the capture lossy WITHOUT failing it (⇒ partial, not
        CaptureUnavailable)."""
        self._omissions.add(tag)

    def latch_failure(self, tag: str) -> None:
        # Monotonic: once failed, always failed.
        self._failed = True
        self._omissions.add(tag)

    def seal(
        self,
        cache: SnapshotCache,
        *,
        extra_omissions: tuple[str, ...] = (),
    ) -> ModelSnapshot | CaptureUnavailable:
        """DERIVE the sealed provenance. The caller can NEVER pass
        ``completeness`` — it is derived, so a swallowed capture failure
        cannot mint a "complete" snapshot."""
        if self._failed:
            return CaptureUnavailable("capture_failed")
        omissions = tuple(sorted(self._omissions | set(extra_omissions)))
        completeness = "complete" if not omissions else "partial"
        snap = _build_snapshot(
            tuple(self._records), completeness=completeness, omissions=omissions
        )
        cache.put(snap)
        return ModelSnapshot(snap)


def record_content(
    collector: ProvenanceCollector | None,
    source_kind: str,
    source_id: str,
    exposed_text: str,
    *,
    factory=None,
    **meta,
) -> None:
    """Best-effort capture hook. NEVER raises; no ``await``.

    ``collector is None`` ⇒ no-op (a capture point never has to know if
    capture is active). ``factory`` overrides :func:`untrusted_record`
    and must accept ``(source_kind, source_id, exposed_text, **meta)``.
    """
    if collector is None:
        return
    try:
        if _encoded_over_cap(exposed_text):
            collector.add_omission("digest_truncated")
        build = factory if factory is not None else untrusted_record
        rec = build(source_kind, source_id, exposed_text, **meta)
        collector.record(rec)
    except Exception:
        try:
            collector.latch_failure("record_error")
        except Exception:
            pass


def record_episodic(
    collector: ProvenanceCollector | None,
    row: dict,
    exposed_text: str,
) -> None:
    """Best-effort: record an ATTESTED episodic row (the only DB_* path).

    ``collector is None`` ⇒ no-op. NEVER raises; no await.
    """
    if collector is None:
        return
    try:
        collector.record(attested_episodic_record(row, exposed_text))
    except Exception:
        try:
            collector.latch_failure("record_error")
        except Exception:
            pass


# ── F. Scope (ContextVar; spans native + LangChain paths) ──────────────

# Mirrors the set/reset-token pattern of the active-execution-context
# ContextVar in the native client: nesting-safe + task-safe.
_active_provenance_collector: ContextVar[ProvenanceCollector | None] = ContextVar(
    "omnisight_active_provenance_collector", default=None
)


@contextmanager
def provenance_scope() -> Iterator[ProvenanceCollector]:
    collector = ProvenanceCollector()
    token = _active_provenance_collector.set(collector)
    try:
        yield collector
    finally:
        _active_provenance_collector.reset(token)


def active_collector() -> ProvenanceCollector | None:
    return _active_provenance_collector.get()
