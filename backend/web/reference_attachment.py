"""W16.8 #XXX — Reference attachment persistence + agent prompt context.

Where this slots into the W16 epic
----------------------------------

W11/W12/W13 each produce a *spec* the operator captured from an external
source — a URL clone (W11 :class:`backend.web.output_transformer.TransformedSpec`),
an image attachment (W12 :class:`backend.web.image_attachment.LayoutSpec`),
or a multi-breakpoint screenshot capture (W13
:class:`backend.web.screenshot_writer.ScreenshotManifest`).  Until W16.8
those specs lived only in the in-memory request that produced them
(plus, for W11.7 + W13.3, a single on-disk artefact under
``.omnisight/clone-manifest.json`` / ``.omnisight/refs/manifest.json``).

W16.8 (this row) **closes the loop** by adding a thin per-workspace
*reference attachment* index — one row per spec the operator has
captured for a workspace — under
``<project_root>/.omnisight/references/index.json``.  The index makes
two things possible:

* **Cross-spec discoverability.**  An agent can ask "what reference
  material has the operator captured for this workspace?" and get a
  single answer regardless of whether the spec came from W11 / W12 /
  W13.
* **Auto prompt-context injection.**  When
  :func:`backend.routers.invoke._run_agent_task` invokes an agent, it
  can call :func:`render_reference_attachment_context` to render a
  short markdown block summarising every captured reference and prepend
  it to the agent's pre-fetched codebase context.  The agent then
  edits / scaffolds with the operator's reference material in scope,
  without the operator having to re-paste a URL or re-attach an image.

::

    Operator clones URL (W11) →
        .omnisight/clone-manifest.json  ← W11.7 still owns this artefact
        .omnisight/references/index.json
            └─ row {ref_id, kind:"clone", payload_path:"clone-manifest.json", ...}

    Operator pastes screenshot (W12) →
        .omnisight/references/<ref_id>.json   ← W16.8 owns this payload
        .omnisight/references/index.json
            └─ row {ref_id, kind:"image", payload_path:"references/<ref_id>.json", ...}

    Operator captures multi-breakpoint screenshots (W13) →
        .omnisight/refs/manifest.json   ← W13.3 still owns this artefact
        .omnisight/references/index.json
            └─ row {ref_id, kind:"screenshot", payload_path:"refs/manifest.json", ...}

    Agent invocation →
        backend.routers.invoke._run_agent_task
            └─ _load_reference_attachment_context(workspace_path)
                └─ render_reference_attachment_context(project_root=ws)
                    └─ markdown block prepended to handoff_ctx

Frozen wire shape
-----------------

The on-disk index file is JSON and pinned by drift guards so the W16.9
e2e tests can grep for stable substrings:

::

    {
      "index_version": "1",
      "created_at": "2026-05-03T00:00:00Z",
      "updated_at": "2026-05-03T00:00:00Z",
      "attachments": [
        {
          "ref_id":       "ref_<sha256-prefix>",
          "kind":         "clone" | "image" | "screenshot",
          "created_at":   "2026-05-03T00:00:00Z",
          "source_url":   "https://acme.example/landing",   # optional
          "summary":      "<one-line operator-facing summary>",
          "payload_path": "<relative-to-.omnisight/, e.g. clone-manifest.json>"
        }
      ]
    }

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  zero mutable module-level state — only frozen string
constants (``REFERENCE_ATTACHMENT_*`` identifiers + ``REFERENCE_KIND_*``
values + ``REFERENCE_KINDS`` tuple), int caps (``MAX_REFERENCE_*``
constants), frozen :class:`ReferenceAttachment` + :class:`ReferenceIndex`
dataclasses, a typed :class:`ReferenceAttachmentError` subclass, and
stdlib imports (``hashlib`` / ``json`` / ``dataclasses`` / ``pathlib`` /
``datetime`` / ``typing``).  Answer #1 — every uvicorn worker reads the
same constants from the same git checkout; the on-disk index is the
single source of truth across workers (multiple workers writing the
same index race on the index file's atomic-rename, mirroring W11.7's
``.omnisight/clone-manifest.json`` write, so the last writer wins; the
write is idempotent because :func:`register_reference_attachment` re-
reads the existing index and merges).

Read-after-write timing audit (per SOP §2): N/A — pure file-system
projection from caller kwargs to a JSON blob.  No DB pool, no
compat→pool conversion, no asyncio.gather race surface.  The optional
SSE event (:func:`emit_reference_attached`) is fire-and-forget and not
in :data:`backend.events._PERSIST_EVENT_TYPES` because the on-disk
index already provides durability — replaying a stale ``reference.
attached`` event would just re-emit the chat-message render.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


# ── Frozen wire-shape constants ──────────────────────────────────────

#: Top-level metadata directory inside a workspace project root.  Already
#: used by W11.7 (``clone-manifest.json``) and W13.3 (``refs/``).  Pinned
#: here as a constant so :func:`resolve_reference_index_path` and the
#: W16.9 e2e tests can grep without scanning every consumer.
REFERENCE_ATTACHMENT_DIR_NAME: str = ".omnisight"

#: Sub-directory of :data:`REFERENCE_ATTACHMENT_DIR_NAME` where the
#: W16.8 index + standalone payload JSONs live.  Distinct from
#: ``refs/`` (W13.3) so the screenshot writer's atomic-rename pattern
#: does not collide with the index-file write.
REFERENCE_ATTACHMENT_SUBDIR: str = "references"

#: Filename of the index JSON inside :data:`REFERENCE_ATTACHMENT_SUBDIR`.
REFERENCE_ATTACHMENT_INDEX_FILENAME: str = "index.json"

#: Schema version of the index JSON.  Bump when the on-disk shape
#: changes in a way that breaks the agent prompt-context renderer or
#: the W16.9 e2e tests.  Pinned in :class:`ReferenceIndex.index_version`
#: + the drift guard at module import time.
REFERENCE_ATTACHMENT_INDEX_VERSION: str = "1"

#: Stable kind identifiers.  Pinned so the FE / agent prompt / drift
#: guards can grep for a string literal without round-tripping through
#: an enum.
REFERENCE_KIND_CLONE: str = "clone"
REFERENCE_KIND_IMAGE: str = "image"
REFERENCE_KIND_SCREENSHOT: str = "screenshot"

#: Row-spec-ordered tuple of kinds — capture (W11) first, paste (W12)
#: second, screenshot (W13) third.  The renderer iterates in tuple order
#: so the agent sees the operator's canonical-source references before
#: derived ones.
REFERENCE_KINDS: tuple[str, str, str] = (
    REFERENCE_KIND_CLONE,
    REFERENCE_KIND_IMAGE,
    REFERENCE_KIND_SCREENSHOT,
)

#: Prefix on every ref_id minted by :func:`mint_reference_id` so a
#: human reading the index can tell what flavour of identifier is in
#: ``ref_id`` (vs e.g. clone_id / image_hash which use different
#: schemes).  16-hex-char SHA-256 prefix mirrors W16.2's image_hash
#: shape so consumers can dedupe across the two ID spaces by suffix.
REFERENCE_ID_PREFIX: str = "ref_"

#: SSE event published by :func:`emit_reference_attached` whenever a
#: new attachment is registered.  ``"reference.attached"`` matches the
#: W16.4–W16.7 ``preview.<verb>`` namespacing — the leading word is the
#: subject, the second the verb.  Pinned by drift guard.
REFERENCE_ATTACHED_EVENT_NAME: str = "reference.attached"

#: Pipeline phase string used when callers want to emit a sibling
#: :func:`backend.events.emit_pipeline_phase`.  Frozen so the
#: ``-snake_case`` style mirrors the W14.* phases that already populate
#: ``backend.routers.system``.
REFERENCE_ATTACHMENT_PIPELINE_PHASE: str = "reference_attachment"

#: Default broadcast scope when callers don't pass one explicitly.
#: ``"session"`` because reference attachments are per-operator-session
#: — mirrors the W16.4–W16.7 reasoning (CF Access SSO gates ingress to
#: the launching operator's email; broadcasting globally would mean
#: other tenants see a chat card for a workspace they cannot open).
REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE: str = "session"

#: Default human-facing chat-message body when the caller does not pass
#: an explicit ``label``.  Frozen so the W16.9 e2e tests grep a stable
#: substring.
REFERENCE_ATTACHED_DEFAULT_LABEL: str = "Reference attached"


# ── Bound caps (defensive) ───────────────────────────────────────────

#: Hard cap on the ``ref_id`` field.  16-hex-char SHA-256 prefix +
#: the ``ref_`` literal + 2× headroom for any future schema bump.
MAX_REFERENCE_REF_ID_BYTES: int = 64

#: Hard cap on the optional ``source_url`` field.  Mirrors
#: ``MAX_PREVIEW_READY_URL_BYTES`` (W16.4).
MAX_REFERENCE_SOURCE_URL_BYTES: int = 4096

#: Hard cap on the operator-facing ``summary`` field.  ~400 bytes is
#: enough for a 1–2 sentence preview headline plus a short colour-
#: count tail.  Tighter than the 1024-byte handoff-context bytes-cap
#: because the prompt renderer joins many summaries together and we
#: don't want one row to dominate.
MAX_REFERENCE_SUMMARY_BYTES: int = 400

#: Hard cap on the ``payload_path`` field (relative to ``.omnisight/``).
MAX_REFERENCE_PAYLOAD_PATH_BYTES: int = 512

#: Hard cap on a single payload-JSON blob written under
#: ``references/<ref_id>.json``.  128 KiB is generous for a layout-spec
#: dump but bounds disk-space + read-time blow-up.
MAX_REFERENCE_PAYLOAD_BYTES: int = 131_072

#: Hard cap on the number of attachments retained per project_root.
#: When :func:`register_reference_attachment` would push the count past
#: this, the *oldest* row by ``created_at`` is evicted (FIFO).  Bounds
#: index file growth + agent prompt context length.
MAX_REFERENCE_ATTACHMENTS_PER_PROJECT: int = 64

#: Hard cap on how many attachments
#: :func:`render_reference_attachment_context` injects into the agent
#: prompt.  Lower than :data:`MAX_REFERENCE_ATTACHMENTS_PER_PROJECT`
#: because the prompt has its own token budget — the rendered block
#: shows the *most recent* N attachments (newest-first by
#: ``created_at``).
DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT: int = 10


# ── Public dataclasses ───────────────────────────────────────────────


class ReferenceAttachmentError(ValueError):
    """Raised on every contract violation in :mod:`backend.web.
    reference_attachment` — bad ``kind`` / over-cap field / unreadable
    index file / etc.  Subclasses :class:`ValueError` so callers that
    already except on bad URL inputs keep working unchanged.
    """


@dataclass(frozen=True)
class ReferenceAttachment:
    """Frozen index row for a single captured reference spec.

    Attributes
    ----------
    ref_id:
        Stable identifier (``ref_<sha256-hex16>``).  Used as the file
        name when ``payload_path`` is ``None``; used as the agent
        prompt's per-row anchor regardless.
    kind:
        One of :data:`REFERENCE_KIND_CLONE` / :data:`REFERENCE_KIND_IMAGE`
        / :data:`REFERENCE_KIND_SCREENSHOT`.  Other values raise.
    created_at:
        ISO-8601 UTC timestamp the attachment was registered.  Used by
        the renderer to sort newest-first.
    source_url:
        Optional canonical source URL for the attachment.  Set for
        ``clone`` / ``screenshot`` (the URL the operator pasted) and
        for ``image`` if the image was fetched from a URL.
    summary:
        One-line operator-facing summary of the captured spec.  Shown
        in the agent prompt context block.
    payload_path:
        Path *relative* to ``<project_root>/.omnisight/`` pointing at
        the on-disk JSON payload that backs this attachment.  For
        ``clone`` it points at the existing W11.7 manifest
        (``clone-manifest.json``); for ``screenshot`` it points at
        W13.3 (``refs/manifest.json``); for ``image`` it points at a
        W16.8-owned blob under ``references/<ref_id>.json``.  Pure
        relative path so the index file remains portable across tenant
        / workspace renames.
    """

    ref_id: str
    kind: str
    created_at: str
    summary: str
    payload_path: str
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Project the dataclass into the on-disk JSON shape.

        The ``source_url`` field is dropped when ``None`` so the wire
        payload stays tight (the W16.9 e2e tests grep for absence of
        unset keys to detect drift).
        """
        data: dict[str, Any] = {
            "ref_id": self.ref_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "summary": self.summary,
            "payload_path": self.payload_path,
        }
        if self.source_url is not None:
            data["source_url"] = self.source_url
        return data


@dataclass(frozen=True)
class ReferenceIndex:
    """Frozen on-disk shape of ``references/index.json``.

    ``attachments`` is a tuple (frozen sequence) so downstream callers
    cannot mutate the index in place.  :func:`register_reference_attachment`
    rebuilds the index every call and atomically replaces the file.
    """

    index_version: str
    created_at: str
    updated_at: str
    attachments: tuple[ReferenceAttachment, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_version": self.index_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attachments": [a.to_dict() for a in self.attachments],
        }


# ── Validators ───────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Single chokepoint for ``created_at`` / ``updated_at`` so tests
    can monkeypatch this for determinism.  Format mirrors
    :func:`backend.web.clone_manifest._utc_now_iso` so cross-row
    timestamp diffs are not tripped by stringification drift."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_str(name: str, value: Any, *, max_bytes: int,
                 allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ReferenceAttachmentError(
            f"{name} must be str, got {type(value).__name__}"
        )
    if not allow_empty and not value:
        raise ReferenceAttachmentError(f"{name} must be non-empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise ReferenceAttachmentError(
            f"{name} exceeds {max_bytes}-byte cap"
        )
    return value


def _require_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise ReferenceAttachmentError(
            f"kind must be str, got {type(value).__name__}"
        )
    if value not in REFERENCE_KINDS:
        raise ReferenceAttachmentError(
            f"kind must be one of {REFERENCE_KINDS}, got {value!r}"
        )
    return value


# ── Resolvers ────────────────────────────────────────────────────────


def resolve_reference_dir(project_root: Path | str) -> Path:
    """Return ``<project_root>/.omnisight/references`` as an absolute
    path.  Pure resolver: never touches the filesystem.
    """
    if not isinstance(project_root, (str, Path)):
        raise ReferenceAttachmentError(
            "project_root must be str or Path, got "
            f"{type(project_root).__name__}"
        )
    root = Path(project_root)
    if not root.is_absolute():
        root = root.resolve()
    return root / REFERENCE_ATTACHMENT_DIR_NAME / REFERENCE_ATTACHMENT_SUBDIR


def resolve_reference_index_path(project_root: Path | str) -> Path:
    """Return the absolute path of the index JSON file."""
    return resolve_reference_dir(project_root) / REFERENCE_ATTACHMENT_INDEX_FILENAME


def resolve_reference_payload_path(
    project_root: Path | str, ref_id: str,
) -> Path:
    """Return the absolute path of a W16.8-owned standalone payload
    blob.  Used only for ``image`` kind; ``clone`` / ``screenshot``
    point ``payload_path`` at existing W11.7 / W13.3 artefacts.
    """
    _require_str("ref_id", ref_id, max_bytes=MAX_REFERENCE_REF_ID_BYTES)
    return resolve_reference_dir(project_root) / f"{ref_id}.json"


# ── ID minting ───────────────────────────────────────────────────────


def mint_reference_id(*, kind: str, seed: str) -> str:
    """Compute a stable ``ref_<sha256-hex16>`` identifier.

    ``seed`` should be the most stable per-attachment string available
    to the caller — the source URL for ``clone`` / ``screenshot``, the
    ``image_hash`` (a SHA-256 prefix already) for ``image``.  The
    resulting ref_id is deterministic for the same (kind, seed) pair
    so re-registering an attachment is idempotent (same row replaces
    the existing one rather than appending a duplicate).
    """
    _require_kind(kind)
    _require_str("seed", seed, max_bytes=MAX_REFERENCE_SOURCE_URL_BYTES)
    hex16 = hashlib.sha256(f"{kind}|{seed}".encode("utf-8")).hexdigest()[:16]
    return f"{REFERENCE_ID_PREFIX}{hex16}"


# ── Index I/O ────────────────────────────────────────────────────────


def load_reference_index(project_root: Path | str) -> ReferenceIndex:
    """Read the index JSON at ``<project_root>/.omnisight/references/
    index.json``.  Returns an empty :class:`ReferenceIndex` (with a
    fresh ``created_at`` / ``updated_at``) when the file is missing —
    callers shouldn't have to special-case "first attachment".

    Raises :class:`ReferenceAttachmentError` only when the file exists
    but is unreadable / malformed.
    """
    target = resolve_reference_index_path(project_root)
    if not target.exists():
        now = _utc_now_iso()
        return ReferenceIndex(
            index_version=REFERENCE_ATTACHMENT_INDEX_VERSION,
            created_at=now,
            updated_at=now,
            attachments=(),
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReferenceAttachmentError(
            f"failed to read reference index at {target}: {exc!s}"
        ) from exc
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReferenceAttachmentError(
            f"reference index at {target} is not valid JSON: {exc!s}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReferenceAttachmentError(
            f"reference index at {target} is not a JSON object"
        )
    version = payload.get("index_version")
    if version != REFERENCE_ATTACHMENT_INDEX_VERSION:
        raise ReferenceAttachmentError(
            f"reference index_version {version!r} unsupported "
            f"(expected {REFERENCE_ATTACHMENT_INDEX_VERSION!r})"
        )
    rows_raw = payload.get("attachments")
    if rows_raw is None:
        rows_raw = []
    if not isinstance(rows_raw, list):
        raise ReferenceAttachmentError(
            "reference index attachments field must be a list"
        )
    rows: list[ReferenceAttachment] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            raise ReferenceAttachmentError(
                "reference index attachments row must be a JSON object"
            )
        try:
            rows.append(
                ReferenceAttachment(
                    ref_id=str(row["ref_id"]),
                    kind=str(row["kind"]),
                    created_at=str(row["created_at"]),
                    summary=str(row["summary"]),
                    payload_path=str(row["payload_path"]),
                    source_url=(
                        str(row["source_url"])
                        if row.get("source_url") is not None
                        else None
                    ),
                )
            )
        except KeyError as exc:
            raise ReferenceAttachmentError(
                f"reference index attachments row missing key: {exc!s}"
            ) from exc
    return ReferenceIndex(
        index_version=str(payload.get("index_version") or REFERENCE_ATTACHMENT_INDEX_VERSION),
        created_at=str(payload.get("created_at") or _utc_now_iso()),
        updated_at=str(payload.get("updated_at") or _utc_now_iso()),
        attachments=tuple(rows),
    )


def _serialize_index_json(index: ReferenceIndex, *, indent: int = 2) -> str:
    return json.dumps(
        index.to_dict(),
        indent=indent,
        sort_keys=False,
        ensure_ascii=False,
    )


def write_reference_index(
    index: ReferenceIndex, *, project_root: Path | str, indent: int = 2,
) -> Path:
    """Atomically pin the index to ``<project_root>/.omnisight/
    references/index.json``.  Creates the directory if missing.
    Overwrites any existing file (W16.8 is the single writer of the
    index — concurrent writers race on os.replace's rename atomicity,
    last writer wins, which mirrors W11.7's manifest write posture).
    """
    if not isinstance(index, ReferenceIndex):
        raise ReferenceAttachmentError(
            f"index must be ReferenceIndex, got {type(index).__name__}"
        )
    target = resolve_reference_index_path(project_root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            _serialize_index_json(index, indent=indent) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
    except OSError as exc:
        raise ReferenceAttachmentError(
            f"failed to write reference index at {target}: {exc!s}"
        ) from exc
    return target


# ── Standalone payload I/O (image kind) ──────────────────────────────


def write_reference_payload(
    *, project_root: Path | str, ref_id: str, payload: Mapping[str, Any],
    indent: int = 2,
) -> Path:
    """Persist a JSON-serialisable ``payload`` to
    ``<project_root>/.omnisight/references/<ref_id>.json``.

    Used by :func:`register_reference_attachment` when the caller did
    not pass a pre-existing ``payload_path`` (e.g. W12 image LayoutSpec
    does not have a sibling on-disk artefact like W11.7 / W13.3).

    Raises :class:`ReferenceAttachmentError` on over-cap / IO failure.
    """
    if not isinstance(payload, Mapping):
        raise ReferenceAttachmentError(
            f"payload must be a mapping, got {type(payload).__name__}"
        )
    target = resolve_reference_payload_path(project_root, ref_id)
    try:
        body = json.dumps(
            dict(payload), indent=indent, sort_keys=False, ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReferenceAttachmentError(
            f"payload for {ref_id!r} is not JSON-serialisable: {exc!s}"
        ) from exc
    if len(body.encode("utf-8")) > MAX_REFERENCE_PAYLOAD_BYTES:
        raise ReferenceAttachmentError(
            f"payload for {ref_id!r} exceeds "
            f"{MAX_REFERENCE_PAYLOAD_BYTES}-byte cap"
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise ReferenceAttachmentError(
            f"failed to write reference payload at {target}: {exc!s}"
        ) from exc
    return target


def read_reference_payload(
    *, project_root: Path | str, ref_id: str,
) -> dict[str, Any]:
    """Inverse of :func:`write_reference_payload`.  Returns the JSON
    decoded into a plain dict.  Raises :class:`ReferenceAttachmentError`
    when the file is missing / unreadable / malformed."""
    target = resolve_reference_payload_path(project_root, ref_id)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReferenceAttachmentError(
            f"failed to read reference payload at {target}: {exc!s}"
        ) from exc
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReferenceAttachmentError(
            f"reference payload at {target} is not valid JSON: {exc!s}"
        ) from exc
    if not isinstance(decoded, dict):
        raise ReferenceAttachmentError(
            f"reference payload at {target} is not a JSON object"
        )
    return decoded


# ── Registration ─────────────────────────────────────────────────────


def register_reference_attachment(
    *,
    project_root: Path | str,
    kind: str,
    summary: str,
    source_url: str | None = None,
    payload_path: str | None = None,
    payload_dict: Mapping[str, Any] | None = None,
    ref_id: str | None = None,
    created_at: str | None = None,
) -> ReferenceAttachment:
    """Register a captured spec under ``<project_root>/.omnisight/
    references/index.json`` and (when ``payload_dict`` is given) write
    the standalone payload blob.

    Exactly one of ``payload_path`` / ``payload_dict`` must be set:

    * ``payload_path``  — relative-to-``.omnisight/`` path of an
      existing on-disk artefact (W11.7 ``clone-manifest.json`` / W13.3
      ``refs/manifest.json``).  The index just points at it.
    * ``payload_dict``  — JSON-serialisable mapping (W12 LayoutSpec).
      Written to ``references/<ref_id>.json`` and the resulting
      relative path is recorded in ``payload_path``.

    ``ref_id`` is auto-minted from ``(kind, source_url or summary)``
    when ``None`` so callers can stay terse.  Pass an explicit
    ``ref_id`` to dedupe against an external identity (e.g. W12
    image_hash).

    De-duplication: when the index already contains a row with the
    same ``ref_id``, the existing row is *replaced* (not appended) so
    re-registering an attachment is idempotent.  When the resulting
    index would exceed
    :data:`MAX_REFERENCE_ATTACHMENTS_PER_PROJECT`, the oldest row by
    ``created_at`` is evicted (FIFO).
    """

    kind = _require_kind(kind)
    summary = _require_str(
        "summary", summary, max_bytes=MAX_REFERENCE_SUMMARY_BYTES,
    )
    if source_url is not None:
        source_url = _require_str(
            "source_url", source_url,
            max_bytes=MAX_REFERENCE_SOURCE_URL_BYTES,
        )
    if (payload_path is None) == (payload_dict is None):
        raise ReferenceAttachmentError(
            "exactly one of payload_path / payload_dict must be set"
        )
    if ref_id is None:
        seed = source_url if source_url else summary
        ref_id = mint_reference_id(kind=kind, seed=seed)
    else:
        ref_id = _require_str(
            "ref_id", ref_id, max_bytes=MAX_REFERENCE_REF_ID_BYTES,
        )
    if not ref_id.startswith(REFERENCE_ID_PREFIX):
        raise ReferenceAttachmentError(
            f"ref_id must start with {REFERENCE_ID_PREFIX!r}, got "
            f"{ref_id!r}"
        )
    created_at_value = created_at or _utc_now_iso()

    if payload_dict is not None:
        write_reference_payload(
            project_root=project_root,
            ref_id=ref_id,
            payload=payload_dict,
        )
        payload_path = f"{REFERENCE_ATTACHMENT_SUBDIR}/{ref_id}.json"

    payload_path = _require_str(
        "payload_path", payload_path,
        max_bytes=MAX_REFERENCE_PAYLOAD_PATH_BYTES,
    )

    attachment = ReferenceAttachment(
        ref_id=ref_id,
        kind=kind,
        created_at=created_at_value,
        summary=summary,
        payload_path=payload_path,
        source_url=source_url,
    )

    existing = load_reference_index(project_root)
    rows = [a for a in existing.attachments if a.ref_id != ref_id]
    rows.append(attachment)
    # Keep newest-last so the FIFO eviction trims oldest rows.
    rows.sort(key=lambda a: a.created_at)
    if len(rows) > MAX_REFERENCE_ATTACHMENTS_PER_PROJECT:
        rows = rows[-MAX_REFERENCE_ATTACHMENTS_PER_PROJECT:]
    new_index = ReferenceIndex(
        index_version=REFERENCE_ATTACHMENT_INDEX_VERSION,
        created_at=existing.created_at if existing.attachments else created_at_value,
        updated_at=created_at_value,
        attachments=tuple(rows),
    )
    write_reference_index(new_index, project_root=project_root)
    return attachment


def list_reference_attachments(
    project_root: Path | str,
) -> tuple[ReferenceAttachment, ...]:
    """Return the index's ``attachments`` tuple.  Convenience wrapper
    around :func:`load_reference_index` for callers that don't need the
    surrounding metadata."""
    return load_reference_index(project_root).attachments


def load_reference_attachment(
    *, project_root: Path | str, ref_id: str,
) -> ReferenceAttachment:
    """Look up a single attachment by ``ref_id``.  Raises
    :class:`ReferenceAttachmentError` when the index is missing the
    requested row.
    """
    _require_str("ref_id", ref_id, max_bytes=MAX_REFERENCE_REF_ID_BYTES)
    for attachment in list_reference_attachments(project_root):
        if attachment.ref_id == ref_id:
            return attachment
    raise ReferenceAttachmentError(
        f"reference attachment {ref_id!r} not found in index"
    )


# ── Factory helpers (W11/W12/W13 → ReferenceAttachment) ─────────────


def _summary_for_clone(transformed_summary: Mapping[str, Any]) -> str:
    """One-line summary for a W11 clone manifest's
    ``transformed_summary`` block."""
    title = str(transformed_summary.get("title") or "(untitled)")
    nav = transformed_summary.get("nav_count", 0)
    sec = transformed_summary.get("section_count", 0)
    img = transformed_summary.get("image_count", 0)
    return (
        f"Cloned page '{title}' "
        f"(nav={nav}, sections={sec}, images={img})"
    )


def register_reference_from_clone_manifest(
    *,
    project_root: Path | str,
    manifest: Mapping[str, Any],
    payload_path: str | None = None,
    created_at: str | None = None,
) -> ReferenceAttachment:
    """Project a W11.7 clone manifest dict (the
    :meth:`backend.web.clone_manifest.CloneManifest.to_dict` result, or
    the on-disk JSON) into a :class:`ReferenceAttachment` and register
    it in the index.

    ``payload_path`` defaults to W11.7's pinned location
    (``clone-manifest.json``) so the index just points at the existing
    artefact.  Override only when the caller has copied the manifest
    elsewhere (uncommon).
    """
    if not isinstance(manifest, Mapping):
        raise ReferenceAttachmentError(
            f"manifest must be a mapping, got {type(manifest).__name__}"
        )
    source = manifest.get("source") or {}
    if not isinstance(source, Mapping):
        raise ReferenceAttachmentError(
            "manifest 'source' field must be a mapping"
        )
    transformed_summary = manifest.get("transformed_summary") or {}
    if not isinstance(transformed_summary, Mapping):
        raise ReferenceAttachmentError(
            "manifest 'transformed_summary' field must be a mapping"
        )
    source_url = str(source.get("url") or "") or None
    summary = _summary_for_clone(transformed_summary)
    if payload_path is None:
        payload_path = "clone-manifest.json"
    return register_reference_attachment(
        project_root=project_root,
        kind=REFERENCE_KIND_CLONE,
        summary=summary,
        source_url=source_url,
        payload_path=payload_path,
        ref_id=None,
        created_at=created_at,
    )


def _summary_for_layout_spec(layout_spec: Mapping[str, Any]) -> str:
    """One-line summary for a W12 image LayoutSpec dict."""
    headline = str(layout_spec.get("summary") or "(no summary)")
    components = layout_spec.get("components") or ()
    colors = layout_spec.get("colors") or ()
    fonts = layout_spec.get("fonts") or ()
    if isinstance(components, Sequence):
        comp_count = len(components)
    else:
        comp_count = 0
    if isinstance(colors, Sequence):
        color_count = len(colors)
    else:
        color_count = 0
    if isinstance(fonts, Sequence):
        font_count = len(fonts)
    else:
        font_count = 0
    return (
        f"Pasted image: {headline} "
        f"(components={comp_count}, colors={color_count}, fonts={font_count})"
    )


def register_reference_from_layout_spec(
    *,
    project_root: Path | str,
    layout_spec: Mapping[str, Any],
    source_url: str | None = None,
    created_at: str | None = None,
) -> ReferenceAttachment:
    """Project a W12 image LayoutSpec dict (the
    :meth:`backend.web.image_attachment.LayoutSpec.to_dict`-equivalent)
    into a :class:`ReferenceAttachment`, write the payload blob under
    ``references/<ref_id>.json``, and register it in the index.

    The W12 module already returns LayoutSpec dataclasses; callers
    should call :func:`backend.web.image_attachment.layout_spec_to_dict`
    or hand-build the mapping before calling this helper.
    """
    if not isinstance(layout_spec, Mapping):
        raise ReferenceAttachmentError(
            f"layout_spec must be a mapping, got "
            f"{type(layout_spec).__name__}"
        )
    image_hash = str(layout_spec.get("image_hash") or "")
    if not image_hash:
        raise ReferenceAttachmentError(
            "layout_spec must include a non-empty 'image_hash'"
        )
    ref_id = mint_reference_id(
        kind=REFERENCE_KIND_IMAGE, seed=image_hash,
    )
    summary = _summary_for_layout_spec(layout_spec)
    return register_reference_attachment(
        project_root=project_root,
        kind=REFERENCE_KIND_IMAGE,
        summary=summary,
        source_url=source_url,
        payload_dict=layout_spec,
        ref_id=ref_id,
        created_at=created_at,
    )


def _summary_for_screenshot_manifest(
    screenshot_manifest: Mapping[str, Any],
) -> str:
    """One-line summary for a W13.3 screenshot manifest dict."""
    source_url = str(screenshot_manifest.get("source_url") or "")
    breakpoints = screenshot_manifest.get("breakpoints") or ()
    if isinstance(breakpoints, Sequence):
        bp_count = len(breakpoints)
    else:
        bp_count = 0
    if source_url:
        return (
            f"Captured screenshots from {source_url} "
            f"(breakpoints={bp_count})"
        )
    return f"Captured screenshots (breakpoints={bp_count})"


def register_reference_from_screenshot_manifest(
    *,
    project_root: Path | str,
    screenshot_manifest: Mapping[str, Any],
    payload_path: str | None = None,
    created_at: str | None = None,
) -> ReferenceAttachment:
    """Project a W13.3 screenshot manifest dict into a
    :class:`ReferenceAttachment` and register it in the index.

    ``payload_path`` defaults to W13.3's pinned location
    (``refs/manifest.json``) so the index just points at the existing
    artefact.
    """
    if not isinstance(screenshot_manifest, Mapping):
        raise ReferenceAttachmentError(
            f"screenshot_manifest must be a mapping, got "
            f"{type(screenshot_manifest).__name__}"
        )
    source_url = str(screenshot_manifest.get("source_url") or "") or None
    summary = _summary_for_screenshot_manifest(screenshot_manifest)
    if payload_path is None:
        payload_path = "refs/manifest.json"
    return register_reference_attachment(
        project_root=project_root,
        kind=REFERENCE_KIND_SCREENSHOT,
        summary=summary,
        source_url=source_url,
        payload_path=payload_path,
        ref_id=None,
        created_at=created_at,
    )


# ── Agent prompt context renderer ────────────────────────────────────


def render_reference_attachment_context(
    *,
    project_root: Path | str,
    max_attachments: int = DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT,
) -> str:
    """Render a markdown block summarising the workspace's reference
    attachments, suitable for prepending to an agent's pre-fetched
    codebase-context prompt.

    Returns ``""`` when the index is empty / missing — callers can then
    skip prepending without a special case.

    The block is bounded:
    * Most-recent-first by ``created_at``.
    * At most ``max_attachments`` rows (default
      :data:`DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT`) so the prompt
      stays in budget.
    * Each row is a single bullet — the agent can dereference
      ``payload_path`` if it wants the full spec.

    Format::

        ## Reference Attachments

        - [<kind>] <summary> (ref_id=<ref_id>, source=<source_url>, payload=<payload_path>)
        - [<kind>] <summary> (ref_id=<ref_id>, payload=<payload_path>)

    The W16.9 e2e tests grep for the ``## Reference Attachments``
    heading so the literal is pinned by drift guard.
    """
    if not isinstance(max_attachments, int) or isinstance(max_attachments, bool):
        raise ReferenceAttachmentError(
            "max_attachments must be int"
        )
    if max_attachments <= 0:
        raise ReferenceAttachmentError(
            f"max_attachments must be positive, got {max_attachments!r}"
        )
    try:
        attachments = list_reference_attachments(project_root)
    except ReferenceAttachmentError:
        # A malformed index should not break the agent invocation —
        # the operator can still proceed without reference context.
        return ""
    if not attachments:
        return ""
    # Newest-first.  Stable sort so equal timestamps preserve the
    # registration order.
    sorted_rows = sorted(
        attachments, key=lambda a: a.created_at, reverse=True,
    )
    lines = ["## Reference Attachments", ""]
    for row in sorted_rows[:max_attachments]:
        bits = [
            f"ref_id={row.ref_id}",
        ]
        if row.source_url:
            bits.append(f"source={row.source_url}")
        bits.append(f"payload={row.payload_path}")
        lines.append(
            f"- [{row.kind}] {row.summary} ({', '.join(bits)})"
        )
    return "\n".join(lines) + "\n"


def build_chat_message_for_reference_attached(
    attachment: ReferenceAttachment,
    *,
    label: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Project a :class:`ReferenceAttachment` into the chat-message
    shape the frontend ``WorkspaceChat`` consumes.

    Mirrors :func:`backend.web.web_preview_ready.
    build_chat_message_for_preview_ready` so the SSE consumer can
    ``messages = [...messages, msg]`` without an intermediate
    translator.  ``label`` defaults to
    :data:`REFERENCE_ATTACHED_DEFAULT_LABEL`.
    """
    if not isinstance(attachment, ReferenceAttachment):
        raise ReferenceAttachmentError(
            "attachment must be ReferenceAttachment, got "
            f"{type(attachment).__name__}"
        )
    label_value = label or REFERENCE_ATTACHED_DEFAULT_LABEL
    msg: dict[str, Any] = {
        "id": message_id or "",
        "role": "system",
        "text": f"{label_value}: {attachment.summary}",
        "referenceAttachment": {
            "refId": attachment.ref_id,
            "kind": attachment.kind,
            "createdAt": attachment.created_at,
            "summary": attachment.summary,
            "payloadPath": attachment.payload_path,
        },
    }
    if attachment.source_url is not None:
        msg["referenceAttachment"]["sourceUrl"] = attachment.source_url
    return msg


# ── SSE emission ─────────────────────────────────────────────────────


def emit_reference_attached(
    attachment: ReferenceAttachment,
    *,
    workspace_id: str | None = None,
    label: str | None = None,
    session_id: str | None = None,
    broadcast_scope: str | None = None,
    tenant_id: str | None = None,
    **extra: Any,
) -> ReferenceAttachment:
    """Publish a frozen ``reference.attached`` SSE event and return
    the attachment unchanged.

    Mirrors :func:`backend.web.web_preview_ready.emit_preview_ready`
    so the call site looks identical to every other ``emit_*`` helper.
    Sync-safe; never raises on transport failure.

    The ``broadcast_scope`` defaults to
    :data:`REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE` (``"session"``).
    """
    if not isinstance(attachment, ReferenceAttachment):
        raise ReferenceAttachmentError(
            "attachment must be ReferenceAttachment, got "
            f"{type(attachment).__name__}"
        )
    label_value = label or REFERENCE_ATTACHED_DEFAULT_LABEL
    from backend.events import _resolve_scope, _auto_tenant, bus, _log
    resolved_scope = _resolve_scope(
        "emit_reference_attached",
        broadcast_scope,
        REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE,
    )
    data: dict[str, Any] = {
        "ref_id": attachment.ref_id,
        "kind": attachment.kind,
        "created_at": attachment.created_at,
        "summary": attachment.summary,
        "payload_path": attachment.payload_path,
        "label": label_value,
    }
    if attachment.source_url is not None:
        data["source_url"] = attachment.source_url
    if workspace_id is not None:
        data["workspace_id"] = workspace_id
    if extra:
        for k, v in extra.items():
            data.setdefault(k, v)
    bus.publish(
        REFERENCE_ATTACHED_EVENT_NAME,
        data,
        session_id=session_id,
        broadcast_scope=resolved_scope,
        tenant_id=_auto_tenant(tenant_id),
    )
    _log(
        f"[REFERENCE] {attachment.ref_id} ({attachment.kind}) attached"
    )
    return attachment


# ── Drift guards (module-import time) ────────────────────────────────


# The W11.7 / W13.3 / W16.4–W16.7 modules all assert their frozen
# constants at import time so a typo / refactor that drifts the wire
# shape surfaces as `ImportError` during CI rather than a downstream
# behavioural drift months later.  Mirror that posture here.

assert REFERENCE_ATTACHMENT_INDEX_VERSION == "1", (
    "REFERENCE_ATTACHMENT_INDEX_VERSION drift — bump in lock-step with "
    "the on-disk schema and update the W16.9 e2e test pin."
)
assert REFERENCE_KINDS == (
    REFERENCE_KIND_CLONE, REFERENCE_KIND_IMAGE, REFERENCE_KIND_SCREENSHOT,
), (
    "REFERENCE_KINDS tuple drift — order is binding (renderer iterates "
    "in tuple order)."
)
assert REFERENCE_ID_PREFIX.endswith("_"), (
    "REFERENCE_ID_PREFIX must end with an underscore so the prefix and "
    "the SHA-256 suffix are visually separable."
)
assert REFERENCE_ATTACHED_EVENT_NAME == "reference.attached", (
    "REFERENCE_ATTACHED_EVENT_NAME drift — the FE SSE consumer + W16.9 "
    "e2e tests pin this literal."
)
assert REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE == "session", (
    "REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE drift — preview surface "
    "is per-operator-session, mirrors W16.4–W16.7."
)
assert REFERENCE_ATTACHMENT_PIPELINE_PHASE == "reference_attachment", (
    "REFERENCE_ATTACHMENT_PIPELINE_PHASE drift."
)
assert MAX_REFERENCE_REF_ID_BYTES > 0, "MAX_REFERENCE_REF_ID_BYTES must be positive"
assert MAX_REFERENCE_SOURCE_URL_BYTES > 0
assert MAX_REFERENCE_SUMMARY_BYTES > 0
assert MAX_REFERENCE_PAYLOAD_PATH_BYTES > 0
assert MAX_REFERENCE_PAYLOAD_BYTES > 0
assert MAX_REFERENCE_ATTACHMENTS_PER_PROJECT > 0
assert (
    DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT
    <= MAX_REFERENCE_ATTACHMENTS_PER_PROJECT
), (
    "DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT must not exceed "
    "MAX_REFERENCE_ATTACHMENTS_PER_PROJECT — the renderer can never "
    "ask for more than the index can hold."
)


__all__ = [
    "DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT",
    "MAX_REFERENCE_ATTACHMENTS_PER_PROJECT",
    "MAX_REFERENCE_PAYLOAD_BYTES",
    "MAX_REFERENCE_PAYLOAD_PATH_BYTES",
    "MAX_REFERENCE_REF_ID_BYTES",
    "MAX_REFERENCE_SOURCE_URL_BYTES",
    "MAX_REFERENCE_SUMMARY_BYTES",
    "REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE",
    "REFERENCE_ATTACHED_DEFAULT_LABEL",
    "REFERENCE_ATTACHED_EVENT_NAME",
    "REFERENCE_ATTACHMENT_DIR_NAME",
    "REFERENCE_ATTACHMENT_INDEX_FILENAME",
    "REFERENCE_ATTACHMENT_INDEX_VERSION",
    "REFERENCE_ATTACHMENT_PIPELINE_PHASE",
    "REFERENCE_ATTACHMENT_SUBDIR",
    "REFERENCE_ID_PREFIX",
    "REFERENCE_KINDS",
    "REFERENCE_KIND_CLONE",
    "REFERENCE_KIND_IMAGE",
    "REFERENCE_KIND_SCREENSHOT",
    "ReferenceAttachment",
    "ReferenceAttachmentError",
    "ReferenceIndex",
    "build_chat_message_for_reference_attached",
    "emit_reference_attached",
    "list_reference_attachments",
    "load_reference_attachment",
    "load_reference_index",
    "mint_reference_id",
    "read_reference_payload",
    "register_reference_attachment",
    "register_reference_from_clone_manifest",
    "register_reference_from_layout_spec",
    "register_reference_from_screenshot_manifest",
    "render_reference_attachment_context",
    "resolve_reference_dir",
    "resolve_reference_index_path",
    "resolve_reference_payload_path",
    "write_reference_index",
    "write_reference_payload",
]
