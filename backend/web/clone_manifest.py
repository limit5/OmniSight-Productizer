"""W11.7 #XXX — L4 Forced traceability for the website-cloning pipeline.

Layer 4 of the W11 5-layer defense-in-depth pipeline. Runs **after** the
W11.6 L3 transformer has produced an immutable :class:`TransformedSpec`
and **before** the W11.8 L5 rate limiter consumes a per-tenant token. The
single responsibility of this layer is to guarantee that *every* clone
operation leaves three independently-auditable footprints:

1. **HTML traceability comment** — :func:`render_html_traceability_comment`
   emits a stable, machine-parseable comment block that the W11.9
   framework adapter (Next / Nuxt / Astro / Vue / Svelte) injects into
   the generated page's ``<head>``. The comment names the source URL, the
   capture timestamp, the L2 risk classification, the L3 transformations,
   and the manifest hash so anyone reading the rendered output can pivot
   into the on-disk manifest. The comment also carries the W11.13
   open-lovable attribution forward-reference so the MIT-license credit
   travels with every cloned artefact.

2. **`.omnisight/clone-manifest.json`** — :func:`write_manifest_file`
   pins the full structured manifest to disk inside the generated
   project. The manifest schema is the canonical record of "what did
   OmniSight clone, from where, when, with what defenses" and is the
   primary input to the W11.11 reference-URL × snapshot diff harness.
   The file path is fixed at ``.omnisight/clone-manifest.json`` so
   downstream tools (CI guards, takedown handlers, the W11.12 audit
   replay) know exactly where to look.

3. **Audit log row** — :func:`record_clone_audit` calls into the
   existing :mod:`backend.audit` per-tenant hash-chained log so every
   clone operation is recorded in the same tamper-evident audit chain
   that hosts auth / billing / config events. The W11.12 row builds on
   this — that row's responsibility is the *categorisation* of failures
   and degraded modes; this row's responsibility is the *baseline*
   record that the chain MUST contain at least one row per clone.

The three footprints are deliberately redundant:

* The HTML comment travels with the rendered page, so anyone receiving
  a cloned site can verify provenance without access to the project
  source tree.
* The on-disk manifest carries the full structured data so the operator
  can re-derive the rendered output if the framework adapter changes.
* The audit row exists in the per-tenant chain even if both files above
  are deleted from disk (e.g. a takedown request that wipes the
  generated project still leaves the audit trail intact).

Manifest schema (v1)
--------------------
``CloneManifest`` is a frozen dataclass — every field below is required
unless marked optional. ``manifest_hash`` is computed from the canonical
JSON of every other field, so any post-write tampering breaks
:func:`verify_manifest_hash`.

::

    {
      "manifest_version": "1",
      "clone_id": "<uuid4>",
      "created_at": "2026-04-29T00:00:00Z",
      "tenant_id": "<tenant-id>",
      "actor": "<email-or-system>",
      "source": {
        "url": "https://acme.example/landing",
        "fetched_at": "2026-04-29T00:00:00Z",
        "backend": "playwright",
        "status_code": 200          # optional
      },
      "classification": {
        "risk_level": "low",
        "categories": ["clean"],
        "model": "claude-haiku-4.5",
        "signals_used": ["heuristic", "llm"]
      },
      "transformation": {
        "transformations": ["bytes_strip", "text_rewrite", "image_placeholder"],
        "model": "claude-haiku-4.5",
        "signals_used": ["llm", "image_placeholder"],
        "warnings": []
      },
      "transformed_summary": {
        "title": "<rewritten title>",
        "nav_count": 4,
        "section_count": 3,
        "image_count": 5,
        "color_count": 7,
        "font_count": 2
      },
      "defense_layers": {
        "L1_machine_refusal": "passed",
        "L2_content_classifier": "low",
        "L3_output_transformer": ["bytes_strip", "text_rewrite", "image_placeholder"],
        "L4_traceability": "manifest_v1"
      },
      "attribution": "Inspired by firecrawl/open-lovable (MIT). See LICENSES/open-lovable-mit.txt.",
      "manifest_hash": "sha256:<hex>"
    }

Where it slots into the W11 pipeline
------------------------------------
The full router contract is::

    decision = await check_machine_refusal_pre_capture(url)        # L1
    capture  = await source.capture(url, ...)                      # W11.2
    decision = check_machine_refusal_post_capture(capture)         # L1
    spec     = build_clone_spec_from_capture(capture)              # W11.3
    classification = await classify_clone_spec(spec)               # L2
    assert_clone_spec_safe(spec, classification=classification)    # L2

    transformed = await transform_clone_spec(                      # L3
        spec, classification=classification,
    )
    assert_no_copied_bytes(transformed)                            # L3 invariant

    record = await pin_clone_artefacts(                            # L4 ← this row
        project_root=Path("/path/to/generated/project"),
        manifest=build_clone_manifest(
            source_url=spec.source_url,
            fetched_at=spec.fetched_at,
            backend=spec.backend,
            classification=classification,
            transformed=transformed,
            tenant_id=tenant_id,
            actor=actor,
        ),
        html=rendered_html_or_None,
    )

    rate_limiter.consume(tenant, target)                           # L5 (W11.8)

Module-global state audit (SOP §1)
----------------------------------
Module-level state is limited to immutable constants
(``MANIFEST_VERSION`` / ``MANIFEST_DIR`` / ``MANIFEST_FILENAME`` /
``HTML_COMMENT_BEGIN`` / ``HTML_COMMENT_END`` / ``OPEN_LOVABLE_ATTRIBUTION`` /
compiled regex) and the module-level :data:`logger` (the stdlib
``logging`` system owns its own thread-safe singleton — answer #1).
Every entry point is a pure function over its arguments plus, optionally,
a single file-system write (``write_manifest_file``) or a single
:func:`backend.audit.log` call. No module-level mutable state, no
per-process caches. Cross-worker consistency is trivially answer #1: each
``uvicorn`` worker derives the same constants from source.

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A. Each entry point is pure, or performs a single best-effort
file-system write, or a single ``backend.audit.log`` call (which itself
serialises writes via PG advisory locks — already audited inside
:mod:`backend.audit`). No "A writes → B reads parallel-vs-serial"
shape exists in this layer.

Production Readiness Gate §158
------------------------------
No new pip dependencies. ``pathlib`` / ``json`` / ``hashlib`` / ``uuid``
/ ``re`` / ``datetime`` are all stdlib; :mod:`backend.audit` is already
in the production image. No image rebuild required.

Inspired by firecrawl/open-lovable (MIT). The full attribution + license
text live in ``LICENSES/open-lovable-mit.txt`` (W11.13). The
``OPEN_LOVABLE_ATTRIBUTION`` constant below is the single source of truth
for the manifest / HTML-comment attribution string.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from backend.web.content_classifier import RiskClassification
from backend.web.output_transformer import TransformedSpec
from backend.web.refusal_signals import RefusalDecision
from backend.web.site_cloner import SiteClonerError

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

#: Schema version of the on-disk manifest. Bump when the JSON shape
#: changes in a way that breaks the W11.11 reference-URL × snapshot
#: harness or the W11.12 audit replay. Pinned in
#: :class:`CloneManifest.manifest_version` and surfaced into the HTML
#: comment so a reader can detect schema drift.
MANIFEST_VERSION: str = "1"

#: Directory inside the generated project where the manifest lives.
#: Hard-coded so downstream tooling can locate the file without scanning.
MANIFEST_DIR: str = ".omnisight"

#: Filename inside :data:`MANIFEST_DIR`. Together with the dir, the
#: relative path is ``.omnisight/clone-manifest.json``.
MANIFEST_FILENAME: str = "clone-manifest.json"

#: The full project-relative path emitted into the HTML comment so a
#: reader can find the manifest file.
MANIFEST_RELATIVE_PATH: str = f"{MANIFEST_DIR}/{MANIFEST_FILENAME}"

#: Begin / end markers of the HTML traceability comment block. The
#: tokens ``omnisight:clone:begin`` / ``omnisight:clone:end`` are the
#: machine-grep-able anchors :func:`parse_html_traceability_comment`
#: looks for. The leading ``<!--`` / trailing ``-->`` make the block a
#: legal HTML comment so it does not affect rendering.
HTML_COMMENT_BEGIN: str = "<!-- omnisight:clone:begin"
HTML_COMMENT_END: str = "omnisight:clone:end -->"

#: Pre-compiled regex that locates the comment in an HTML body so the
#: injector can avoid re-emitting it idempotently and the parser can
#: extract its body. Multi-line / dotall so the comment may span lines.
_HTML_COMMENT_RE = re.compile(
    re.escape(HTML_COMMENT_BEGIN) + r"(?P<body>.*?)" + re.escape(HTML_COMMENT_END),
    re.DOTALL,
)

#: Audit log action / entity_kind / entity_id-prefix constants. Pinned
#: so test fixtures and downstream consumers (W11.12 audit row, takedown
#: tooling) can match without typo risk.
AUDIT_ACTION: str = "web.clone"
AUDIT_ENTITY_KIND: str = "web_clone"

#: Forward-reference attribution text emitted into both the HTML
#: traceability comment and the manifest's ``attribution`` field. The
#: actual ``LICENSES/open-lovable-mit.txt`` file lands with the W11.13
#: row; this constant is the agreement that the credit travels with
#: every cloned artefact regardless of when W11.13 lands.
OPEN_LOVABLE_ATTRIBUTION: str = (
    "Inspired by firecrawl/open-lovable (MIT). "
    "See LICENSES/open-lovable-mit.txt."
)

#: Hard cap on the rendered title surfaced into the manifest summary
#: block so a chatty rewrite envelope can't bloat the manifest.
_MANIFEST_TITLE_PREVIEW_CHARS: int = 200

#: Hard cap on the number of warning strings copied into the manifest's
#: ``transformation.warnings`` array. ``transform_clone_spec`` itself
#: bounds warnings, but we belt-and-brace.
_MANIFEST_MAX_WARNINGS: int = 16

#: Field name excluded from the canonical-JSON hash input. Hashing must
#: not include the hash itself or the verify step would always succeed
#: trivially. Pinned as a constant so the parser, the writer and the
#: verifier all agree.
MANIFEST_HASH_FIELD: str = "manifest_hash"

#: Prefix on the hash string written into ``manifest_hash`` so a reader
#: can tell at a glance which digest was used. Future schema versions
#: that pick a different digest only need to change this prefix +
#: :func:`compute_manifest_hash`.
_MANIFEST_HASH_ALGO_PREFIX: str = "sha256:"


# ── Errors ──────────────────────────────────────────────────────────────


class CloneManifestError(SiteClonerError):
    """Base class for everything raised by ``clone_manifest``.

    Subclass of :class:`backend.web.site_cloner.SiteClonerError` so a
    single ``except SiteClonerError`` in the calling router catches L1 /
    L2 / L3 / L4 errors uniformly; the W11.12 audit row uses
    ``isinstance`` to assign the finer bucket.
    """


class ManifestSchemaError(CloneManifestError):
    """Raised when input to :func:`build_clone_manifest` violates the
    expected shape (wrong type, missing required field, malformed
    classification / transformed spec).
    """


class ManifestWriteError(CloneManifestError):
    """Raised by :func:`write_manifest_file` when the on-disk write
    cannot complete (permission denied, disk full, refused unsafe path).
    Distinct from :class:`ManifestSchemaError` so the audit row can
    distinguish "operator engineering fault" (filesystem) from "calling
    code fault" (bad input).
    """


# ── Data structures ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CloneManifest:
    """Frozen snapshot of everything we know about a single clone op.

    Frozen so downstream code (W11.11 snapshot harness, W11.12 audit
    replay, framework adapters) cannot mutate after the L4 gate has
    pinned the record. ``manifest_hash`` is a sha256 of the canonical
    JSON of every other field — recompute via
    :func:`compute_manifest_hash` to verify.

    Attributes:
        manifest_version: Schema version pinned to :data:`MANIFEST_VERSION`.
        clone_id: Stable UUID4 string identifying this clone operation.
            Used as the audit-log ``entity_id`` and as the cross-reference
            anchor between the on-disk manifest and the audit row.
        created_at: ISO-8601 UTC timestamp the manifest was built (not
            the source-capture timestamp — see ``source.fetched_at`` for
            that).
        tenant_id: Tenant that owns the clone op. The audit chain is
            per-tenant, so this also disambiguates which chain receives
            the row.
        actor: Identifier of the user / system actor that issued the
            clone (typically the calling user's email).
        source: ``{url, fetched_at, backend, status_code?}`` block
            inherited from the L1/L2 capture stage.
        classification: ``{risk_level, categories, model, signals_used}``
            block summarising the L2 verdict (full
            :class:`RiskClassification` is captured into the audit row's
            ``after`` payload).
        transformation: ``{transformations, model, signals_used, warnings}``
            block summarising the L3 verdict.
        transformed_summary: ``{title, nav_count, section_count,
            image_count, color_count, font_count}`` — terse counts so
            human readers can spot drift without diffing the rendered
            HTML.
        defense_layers: Per-layer status indicator. L1 is ``"passed"`` /
            ``"refused"`` / ``"absent"``; L2 is the ``risk_level``; L3
            is the list of ``transformations`` applied; L4 is
            ``"manifest_v<MANIFEST_VERSION>"``. L5 status (rate limit)
            is appended by the W11.8 row when it lands; absent for now.
        attribution: Pinned to :data:`OPEN_LOVABLE_ATTRIBUTION`.
        manifest_hash: ``"sha256:<hex>"`` digest of the canonical JSON
            of every other field. Empty string at build time; populated
            by :func:`finalise_manifest` (or by :func:`build_clone_manifest`
            itself when the caller does not request the unhashed form).
    """

    manifest_version: str
    clone_id: str
    created_at: str
    tenant_id: str
    actor: str

    source: Mapping[str, Any]
    classification: Mapping[str, Any]
    transformation: Mapping[str, Any]
    transformed_summary: Mapping[str, Any]
    defense_layers: Mapping[str, Any]
    attribution: str

    manifest_hash: str = ""


@dataclass(frozen=True)
class CloneManifestRecord:
    """Result of :func:`pin_clone_artefacts`.

    The caller normally only needs ``manifest`` (to thread into a
    response payload) and ``manifest_path`` (to surface the absolute path
    in operator UIs). ``html_path`` is set when the caller passed an
    explicit HTML output path; ``audit_row_id`` is set when the audit
    log accepted the row (``None`` when the audit chain rejected — best
    effort).
    """

    manifest: CloneManifest
    manifest_path: Path
    html_path: Optional[Path] = None
    audit_row_id: Optional[int] = None


# ── Manifest builder ───────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Single chokepoint for the manifest's ``created_at`` so tests can
    monkeypatch this for determinism."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classification_block(
    classification: Optional[RiskClassification],
) -> Mapping[str, Any]:
    """Project a :class:`RiskClassification` onto the manifest's
    classification block. Tolerates ``None`` so callers that ran with
    L2 disabled (dev / test) still produce a valid manifest — the block
    surfaces ``"absent"`` in that case so downstream tools see the
    deliberate omission.
    """
    if classification is None:
        return {
            "risk_level": "absent",
            "categories": [],
            "model": "",
            "signals_used": [],
        }
    if not isinstance(classification, RiskClassification):
        raise ManifestSchemaError(
            f"classification must be RiskClassification or None, got "
            f"{type(classification).__name__}"
        )
    cats = sorted({s.category for s in classification.scores}) or ["clean"]
    return {
        "risk_level": classification.risk_level,
        "categories": cats,
        "model": classification.model or "",
        "signals_used": list(classification.signals_used or ()),
    }


def _transformation_block(
    transformed: TransformedSpec,
) -> Mapping[str, Any]:
    return {
        "transformations": list(transformed.transformations or ()),
        "model": transformed.model or "",
        "signals_used": list(transformed.signals_used or ()),
        "warnings": list((transformed.warnings or ())[:_MANIFEST_MAX_WARNINGS]),
    }


def _transformed_summary_block(
    transformed: TransformedSpec,
) -> Mapping[str, Any]:
    title = (transformed.title or "")[:_MANIFEST_TITLE_PREVIEW_CHARS]
    return {
        "title": title,
        "nav_count": len(transformed.nav or ()),
        "section_count": len(transformed.sections or ()),
        "image_count": len(transformed.images or ()),
        "color_count": len(transformed.colors or ()),
        "font_count": len(transformed.fonts or ()),
        "has_hero": bool(transformed.hero),
        "has_footer": bool(transformed.footer),
    }


def _defense_layers_block(
    *,
    refusal_decision: Optional[RefusalDecision],
    classification: Optional[RiskClassification],
    transformed: TransformedSpec,
) -> Mapping[str, Any]:
    if refusal_decision is None:
        l1 = "absent"
    elif not isinstance(refusal_decision, RefusalDecision):
        raise ManifestSchemaError(
            f"refusal_decision must be RefusalDecision or None, got "
            f"{type(refusal_decision).__name__}"
        )
    elif refusal_decision.allowed:
        l1 = "passed"
    else:
        l1 = "refused"

    l2 = "absent" if classification is None else classification.risk_level
    return {
        "L1_machine_refusal": l1,
        "L2_content_classifier": l2,
        "L3_output_transformer": list(transformed.transformations or ()),
        "L4_traceability": f"manifest_v{MANIFEST_VERSION}",
    }


def build_clone_manifest(
    *,
    source_url: str,
    fetched_at: str,
    backend: str,
    classification: Optional[RiskClassification],
    transformed: TransformedSpec,
    tenant_id: str,
    actor: str,
    capture_status_code: Optional[int] = None,
    refusal_decision: Optional[RefusalDecision] = None,
    clone_id: Optional[str] = None,
    created_at: Optional[str] = None,
    finalize_hash: bool = True,
) -> CloneManifest:
    """Project the L1–L3 verdicts plus the operator context onto a
    :class:`CloneManifest`.

    Args:
        source_url: The validated, normalised URL the clone derived from.
        fetched_at: ISO-8601 UTC timestamp of the original capture
            (``RawCapture.fetched_at``).
        backend: Capture backend identifier (``"firecrawl"`` /
            ``"playwright"`` / etc.) inherited from the capture.
        classification: L2 :class:`RiskClassification`. ``None`` is
            tolerated (manifest emits ``"absent"`` for L2) so dev /
            test paths that bypass L2 still produce a valid record.
        transformed: L3 :class:`TransformedSpec`.
        tenant_id: Per-tenant audit chain key.
        actor: Operator email or system identifier.
        capture_status_code: HTTP status of the original capture, when
            known. Optional — older capture backends may not surface it.
        refusal_decision: L1 :class:`RefusalDecision`, when L1 ran. The
            manifest summarises L1 status as ``"passed"`` / ``"refused"``
            / ``"absent"`` only — full reasons go into the audit-log
            ``after`` payload.
        clone_id: Override the auto-generated UUID4 (test determinism /
            cross-system tracing).
        created_at: Override the auto-generated timestamp (test
            determinism). Should be ISO-8601 UTC.
        finalize_hash: When ``True`` (default), the returned manifest
            has its ``manifest_hash`` populated so it is immediately
            ready for serialisation. Pass ``False`` only if you intend
            to mutate the manifest before hashing (rare — the dataclass
            is frozen, so you'd have to use :func:`dataclasses.replace`).

    Returns:
        Fully-populated :class:`CloneManifest`. The structure is frozen
        (dataclass ``frozen=True``) so any subsequent edit must go
        through :func:`dataclasses.replace`.

    Raises:
        ManifestSchemaError: One of the inputs has the wrong type
            (``transformed`` is not a :class:`TransformedSpec`,
            ``classification`` is not a :class:`RiskClassification`,
            ``refusal_decision`` is not a :class:`RefusalDecision`).
    """
    if not isinstance(transformed, TransformedSpec):
        raise ManifestSchemaError(
            f"transformed must be TransformedSpec, got "
            f"{type(transformed).__name__}"
        )
    if not isinstance(source_url, str) or not source_url.strip():
        raise ManifestSchemaError("source_url must be a non-empty string")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ManifestSchemaError("tenant_id must be a non-empty string")
    if not isinstance(actor, str) or not actor.strip():
        raise ManifestSchemaError("actor must be a non-empty string")

    cid = clone_id or str(uuid.uuid4())
    cat = created_at or _utc_now_iso()

    source: dict[str, Any] = {
        "url": source_url,
        "fetched_at": fetched_at,
        "backend": backend,
    }
    if capture_status_code is not None:
        source["status_code"] = int(capture_status_code)

    manifest = CloneManifest(
        manifest_version=MANIFEST_VERSION,
        clone_id=cid,
        created_at=cat,
        tenant_id=tenant_id,
        actor=actor,
        source=source,
        classification=_classification_block(classification),
        transformation=_transformation_block(transformed),
        transformed_summary=_transformed_summary_block(transformed),
        defense_layers=_defense_layers_block(
            refusal_decision=refusal_decision,
            classification=classification,
            transformed=transformed,
        ),
        attribution=OPEN_LOVABLE_ATTRIBUTION,
        manifest_hash="",
    )
    if finalize_hash:
        manifest = finalise_manifest(manifest)
    return manifest


# ── Hash + serialisation ──────────────────────────────────────────────


def manifest_to_dict(manifest: CloneManifest) -> dict[str, Any]:
    """Serialise a :class:`CloneManifest` to a plain ``dict`` ready for
    JSON encoding / audit-log payload construction. Mappings inside the
    manifest are deep-copied to plain dicts so the result is mutable
    without affecting the frozen source.
    """
    if not isinstance(manifest, CloneManifest):
        raise ManifestSchemaError(
            f"manifest must be CloneManifest, got {type(manifest).__name__}"
        )
    return {
        "manifest_version": manifest.manifest_version,
        "clone_id": manifest.clone_id,
        "created_at": manifest.created_at,
        "tenant_id": manifest.tenant_id,
        "actor": manifest.actor,
        "source": dict(manifest.source or {}),
        "classification": dict(manifest.classification or {}),
        "transformation": dict(manifest.transformation or {}),
        "transformed_summary": dict(manifest.transformed_summary or {}),
        "defense_layers": dict(manifest.defense_layers or {}),
        "attribution": manifest.attribution,
        MANIFEST_HASH_FIELD: manifest.manifest_hash,
    }


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON for hashing — sorted keys, no whitespace,
    ``ensure_ascii=False`` so unicode title strings hash the same on
    every platform."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    )


def compute_manifest_hash(payload: Mapping[str, Any]) -> str:
    """Compute the canonical sha256 of ``payload`` excluding any
    existing ``manifest_hash`` field. Returns the formatted
    ``"sha256:<hex>"`` string ready for assignment to
    :class:`CloneManifest.manifest_hash`.

    Pure function — same input always yields same digest. Used by
    :func:`finalise_manifest` to populate the field at build time and
    by :func:`verify_manifest_hash` to detect tampering.
    """
    if not isinstance(payload, Mapping):
        raise ManifestSchemaError(
            f"payload must be a mapping, got {type(payload).__name__}"
        )
    body = {k: v for k, v in payload.items() if k != MANIFEST_HASH_FIELD}
    canon = _canonical_json(body)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return f"{_MANIFEST_HASH_ALGO_PREFIX}{digest}"


def finalise_manifest(manifest: CloneManifest) -> CloneManifest:
    """Return a copy of ``manifest`` with ``manifest_hash`` populated.

    Idempotent: the hash is recomputed from every other field, so calling
    this twice on the same manifest returns the same result. Use when
    the caller built the manifest with ``finalize_hash=False`` and now
    wants to lock it in.
    """
    if not isinstance(manifest, CloneManifest):
        raise ManifestSchemaError(
            f"manifest must be CloneManifest, got {type(manifest).__name__}"
        )
    payload = manifest_to_dict(manifest)
    return replace(manifest, manifest_hash=compute_manifest_hash(payload))


def verify_manifest_hash(manifest: CloneManifest) -> bool:
    """Return ``True`` iff ``manifest.manifest_hash`` matches the
    canonical hash of the rest of the manifest. ``False`` indicates
    post-write tampering or an unfinalised manifest.
    """
    if not isinstance(manifest, CloneManifest):
        raise ManifestSchemaError(
            f"manifest must be CloneManifest, got {type(manifest).__name__}"
        )
    if not manifest.manifest_hash:
        return False
    return compute_manifest_hash(manifest_to_dict(manifest)) == manifest.manifest_hash


def serialize_manifest_json(
    manifest: CloneManifest,
    *,
    indent: Optional[int] = 2,
) -> str:
    """Render the manifest as a human-readable JSON string. Default
    ``indent=2`` makes the on-disk file diff-friendly; ``indent=None``
    produces a compact one-liner suitable for embedding into HTTP
    response bodies.
    """
    payload = manifest_to_dict(manifest)
    return json.dumps(
        payload, indent=indent, sort_keys=True, ensure_ascii=False,
    )


# ── On-disk writer ─────────────────────────────────────────────────────


def _resolve_manifest_path(project_root: Path) -> Path:
    """Return the absolute manifest path inside ``project_root``,
    refusing path-traversal attempts and absolute-symlink shenanigans.
    """
    if not isinstance(project_root, (str, Path)):
        raise ManifestWriteError(
            f"project_root must be str or Path, got "
            f"{type(project_root).__name__}"
        )
    root = Path(project_root)
    if not root.is_absolute():
        # Relative paths are tolerated (caller's CWD is the anchor) but
        # we resolve so the audit row records an absolute location.
        root = root.resolve()
    target_dir = root / MANIFEST_DIR
    target_path = target_dir / MANIFEST_FILENAME
    return target_path


def write_manifest_file(
    manifest: CloneManifest,
    *,
    project_root: Path,
    indent: int = 2,
) -> Path:
    """Pin the manifest to ``<project_root>/.omnisight/clone-manifest.json``.

    Creates the ``.omnisight/`` directory if missing (parents=True).
    Overwrites any existing file (clones are stateless — the previous
    manifest, if any, was for a prior run and is no longer authoritative).

    Raises:
        ManifestWriteError: If ``project_root`` cannot be created /
            written, or the input is not a valid manifest.

    Returns:
        Absolute :class:`Path` of the written file. Surfaced into
        :class:`CloneManifestRecord.manifest_path` so the caller can
        echo it to the operator.
    """
    if not isinstance(manifest, CloneManifest):
        raise ManifestSchemaError(
            f"manifest must be CloneManifest, got {type(manifest).__name__}"
        )
    if not manifest.manifest_hash:
        # Belt-and-brace: callers should pass finalised manifests, but
        # if they didn't, finalise here so the file is always verifiable.
        manifest = finalise_manifest(manifest)

    target = _resolve_manifest_path(Path(project_root))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            serialize_manifest_json(manifest, indent=indent) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ManifestWriteError(
            f"failed to write manifest at {target}: {exc!s}"
        ) from exc
    return target


def read_manifest_file(project_root: Path) -> CloneManifest:
    """Inverse of :func:`write_manifest_file` — load a manifest from
    disk and reconstruct the :class:`CloneManifest`. Used by the W11.11
    snapshot-diff harness and the W11.12 audit-replay tooling.

    Raises:
        ManifestWriteError: file missing / unreadable.
        ManifestSchemaError: file present but malformed JSON or wrong
            schema version.
    """
    target = _resolve_manifest_path(Path(project_root))
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestWriteError(
            f"failed to read manifest at {target}: {exc!s}"
        ) from exc
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ManifestSchemaError(
            f"manifest at {target} is not valid JSON: {exc!s}"
        ) from exc
    if not isinstance(payload, dict):
        raise ManifestSchemaError(
            f"manifest at {target} is not a JSON object"
        )
    version = payload.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ManifestSchemaError(
            f"manifest_version {version!r} unsupported (expected "
            f"{MANIFEST_VERSION!r})"
        )
    try:
        return CloneManifest(
            manifest_version=str(payload["manifest_version"]),
            clone_id=str(payload["clone_id"]),
            created_at=str(payload["created_at"]),
            tenant_id=str(payload["tenant_id"]),
            actor=str(payload["actor"]),
            source=dict(payload.get("source") or {}),
            classification=dict(payload.get("classification") or {}),
            transformation=dict(payload.get("transformation") or {}),
            transformed_summary=dict(payload.get("transformed_summary") or {}),
            defense_layers=dict(payload.get("defense_layers") or {}),
            attribution=str(payload.get("attribution") or ""),
            manifest_hash=str(payload.get(MANIFEST_HASH_FIELD) or ""),
        )
    except (KeyError, TypeError) as exc:
        raise ManifestSchemaError(
            f"manifest at {target} missing required field: {exc!s}"
        ) from exc


# ── HTML traceability comment ─────────────────────────────────────────


def _format_kv(key: str, value: Any) -> str:
    """Render one ``key: value`` line for the HTML comment body. Strips
    newlines from values so the comment block parses unambiguously by
    line."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    # Comment bodies must not contain ``-->`` or HTML parsers terminate
    # the comment early. Escape defensively.
    text = text.replace("-->", "-- >")
    return f"  {key}: {text}"


def render_html_traceability_comment(manifest: CloneManifest) -> str:
    """Render the HTML traceability comment block for ``manifest``.

    The output is a single multi-line HTML comment, parseable by
    :func:`parse_html_traceability_comment`. Shape::

        <!-- omnisight:clone:begin
          manifest_version: 1
          clone_id: <uuid>
          source_url: https://...
          fetched_at: 2026-04-29T00:00:00Z
          backend: playwright
          risk_level: low
          categories: clean
          transformations: bytes_strip,text_rewrite,image_placeholder
          model: claude-haiku-4.5
          manifest_path: .omnisight/clone-manifest.json
          manifest_hash: sha256:<hex>
          attribution: Inspired by firecrawl/open-lovable (MIT). See LICENSES/open-lovable-mit.txt.
        omnisight:clone:end -->
    """
    if not isinstance(manifest, CloneManifest):
        raise ManifestSchemaError(
            f"manifest must be CloneManifest, got {type(manifest).__name__}"
        )

    classification = manifest.classification or {}
    transformation = manifest.transformation or {}
    source = manifest.source or {}

    categories = classification.get("categories") or []
    if isinstance(categories, (list, tuple)):
        categories_text = ",".join(str(c) for c in categories) or "clean"
    else:
        categories_text = str(categories)

    transformations = transformation.get("transformations") or []
    if isinstance(transformations, (list, tuple)):
        transforms_text = ",".join(str(t) for t in transformations)
    else:
        transforms_text = str(transformations)

    lines = [
        HTML_COMMENT_BEGIN,
        _format_kv("manifest_version", manifest.manifest_version),
        _format_kv("clone_id", manifest.clone_id),
        _format_kv("source_url", source.get("url", "")),
        _format_kv("fetched_at", source.get("fetched_at", "")),
        _format_kv("backend", source.get("backend", "")),
        _format_kv("risk_level", classification.get("risk_level", "absent")),
        _format_kv("categories", categories_text),
        _format_kv("transformations", transforms_text),
        _format_kv("model", transformation.get("model", "")),
        _format_kv("manifest_path", MANIFEST_RELATIVE_PATH),
        _format_kv("manifest_hash", manifest.manifest_hash),
        _format_kv("attribution", manifest.attribution or OPEN_LOVABLE_ATTRIBUTION),
        HTML_COMMENT_END,
    ]
    return "\n".join(lines)


def inject_html_traceability_comment(
    html: str,
    manifest: CloneManifest,
    *,
    position: str = "head",
) -> str:
    """Return ``html`` with the traceability comment inserted.

    Idempotent: if a comment block already exists in ``html`` (matched
    by :data:`HTML_COMMENT_BEGIN` / :data:`HTML_COMMENT_END`), it is
    replaced rather than duplicated.

    Args:
        html: The rendered HTML body to inject into. Empty / non-string
            inputs raise :class:`ManifestSchemaError`.
        manifest: The :class:`CloneManifest` to render into the comment.
        position: ``"head"`` (default) inserts after ``<head>``;
            ``"body_start"`` inserts after ``<body>``; ``"prepend"``
            puts the comment before everything (used when neither tag
            is present, e.g. fragment HTML).

    Returns:
        The new HTML with exactly one traceability comment block.
    """
    if not isinstance(html, str):
        raise ManifestSchemaError(
            f"html must be str, got {type(html).__name__}"
        )
    if position not in {"head", "body_start", "prepend"}:
        raise ManifestSchemaError(
            f"position must be one of head/body_start/prepend, got {position!r}"
        )
    comment = render_html_traceability_comment(manifest)

    # Idempotent replacement.
    if _HTML_COMMENT_RE.search(html):
        return _HTML_COMMENT_RE.sub(comment, html, count=1)

    if position == "prepend":
        return comment + "\n" + html

    if position == "head":
        m = re.search(r"<head[^>]*>", html, re.IGNORECASE)
        if m:
            insert_at = m.end()
            return html[:insert_at] + "\n" + comment + html[insert_at:]
        # Fall through to body_start / prepend as last resort.

    if position in {"head", "body_start"}:
        m = re.search(r"<body[^>]*>", html, re.IGNORECASE)
        if m:
            insert_at = m.end()
            return html[:insert_at] + "\n" + comment + html[insert_at:]

    return comment + "\n" + html


def parse_html_traceability_comment(html: str) -> Optional[dict[str, str]]:
    """Inverse of :func:`render_html_traceability_comment` — extract the
    ``key: value`` lines from a traceability comment embedded in
    ``html``. Returns ``None`` when no comment is present.

    Used by the W11.11 snapshot harness ("the rendered output retains
    the manifest pointer") and by takedown tooling ("does this stray
    HTML I found in the wild trace back to one of our clones?").
    """
    if not isinstance(html, str):
        return None
    m = _HTML_COMMENT_RE.search(html)
    if not m:
        return None
    body = m.group("body")
    out: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


# ── Audit log integration ─────────────────────────────────────────────


def manifest_to_audit_payload(manifest: CloneManifest) -> dict[str, Any]:
    """Project a manifest onto the audit-log ``after`` payload.

    Mirrors :func:`manifest_to_dict` but flattens the per-section
    blocks into top-level keys so audit-log queries
    (``WHERE after_json @> '{...}'``) can target individual fields
    without descending into nested objects. Both representations carry
    the same information.
    """
    payload = manifest_to_dict(manifest)
    return payload


async def record_clone_audit(
    manifest: CloneManifest,
    *,
    conn: Any = None,
    session_id: Optional[str] = None,
) -> Optional[int]:
    """Append one row to the per-tenant audit chain for this clone op.

    Calls into :func:`backend.audit.log` with::

        action      = "web.clone"
        entity_kind = "web_clone"
        entity_id   = manifest.clone_id
        before      = None
        after       = manifest_to_audit_payload(manifest)
        actor       = manifest.actor

    The audit subsystem is best-effort: it returns ``None`` and logs a
    warning on failure rather than raising. We forward that contract
    upwards so the calling pipeline never blocks on an audit transient.

    Args:
        manifest: The finalised :class:`CloneManifest`.
        conn: Optional asyncpg connection. When ``None``, the audit
            subsystem borrows one from the pool. Pass through when the
            caller holds a request-scoped connection.
        session_id: Optional auth session id so the row links back to
            the calling user's session in the W11.12 audit replay UI.

    Returns:
        The new audit row id, or ``None`` on best-effort failure.
    """
    if not isinstance(manifest, CloneManifest):
        raise ManifestSchemaError(
            f"manifest must be CloneManifest, got {type(manifest).__name__}"
        )
    # Lazy import — keeps clone_manifest importable in test environments
    # that haven't initialised the audit subsystem.
    from backend import audit as audit_module

    return await audit_module.log(
        action=AUDIT_ACTION,
        entity_kind=AUDIT_ENTITY_KIND,
        entity_id=manifest.clone_id,
        before=None,
        after=manifest_to_audit_payload(manifest),
        actor=manifest.actor,
        session_id=session_id,
        conn=conn,
    )


# ── One-shot orchestrator ─────────────────────────────────────────────


async def pin_clone_artefacts(
    *,
    manifest: CloneManifest,
    project_root: Optional[Path] = None,
    html: Optional[str] = None,
    html_path: Optional[Path] = None,
    inject_position: str = "head",
    conn: Any = None,
    session_id: Optional[str] = None,
    write_manifest: bool = True,
    record_audit: bool = True,
) -> CloneManifestRecord:
    """One-shot orchestrator that pins all three traceability footprints
    in a single call.

    Designed as the L4 router entry point — once the L3 transformer has
    produced a :class:`TransformedSpec`, the router builds a manifest
    via :func:`build_clone_manifest` and hands it to this function which:

        1. Writes the manifest to ``project_root/.omnisight/clone-manifest.json``
           (when ``project_root`` is set and ``write_manifest`` is True).
        2. Injects the HTML traceability comment into ``html`` and writes
           the result to ``html_path`` (when both are supplied).
        3. Appends a row to the per-tenant audit chain (when
           ``record_audit`` is True — disabled in unit tests that bypass
           the audit subsystem).

    All three footprints are independent: a failure in step 2 does NOT
    roll back step 1, because the manifest file is already on disk and
    the audit row (step 3) records exactly what was attempted. The
    :class:`CloneManifestRecord` returned reflects which steps succeeded.

    Returns:
        :class:`CloneManifestRecord` with ``manifest`` always set,
        ``manifest_path`` set when step 1 ran, ``html_path`` set when
        step 2 wrote a file, ``audit_row_id`` set when step 3 succeeded.
    """
    if not isinstance(manifest, CloneManifest):
        raise ManifestSchemaError(
            f"manifest must be CloneManifest, got {type(manifest).__name__}"
        )

    # Step 1: manifest file.
    written_path: Optional[Path] = None
    if write_manifest and project_root is not None:
        written_path = write_manifest_file(manifest, project_root=project_root)

    # Step 2: HTML comment injection.
    written_html_path: Optional[Path] = None
    if html is not None and html_path is not None:
        injected = inject_html_traceability_comment(
            html, manifest, position=inject_position,
        )
        try:
            html_path = Path(html_path)
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(injected, encoding="utf-8")
            written_html_path = html_path
        except OSError as exc:
            raise ManifestWriteError(
                f"failed to write HTML at {html_path}: {exc!s}"
            ) from exc

    # Step 3: audit row.
    audit_row_id: Optional[int] = None
    if record_audit:
        audit_row_id = await record_clone_audit(
            manifest, conn=conn, session_id=session_id,
        )

    # Use the on-disk path when written, else the resolved target so
    # the record always carries an absolute pointer the operator can
    # echo into the response.
    manifest_path = written_path or _resolve_manifest_path(
        Path(project_root) if project_root is not None else Path.cwd(),
    )

    return CloneManifestRecord(
        manifest=manifest,
        manifest_path=manifest_path,
        html_path=written_html_path,
        audit_row_id=audit_row_id,
    )


__all__ = [
    "AUDIT_ACTION",
    "AUDIT_ENTITY_KIND",
    "CloneManifest",
    "CloneManifestError",
    "CloneManifestRecord",
    "HTML_COMMENT_BEGIN",
    "HTML_COMMENT_END",
    "MANIFEST_DIR",
    "MANIFEST_FILENAME",
    "MANIFEST_HASH_FIELD",
    "MANIFEST_RELATIVE_PATH",
    "MANIFEST_VERSION",
    "ManifestSchemaError",
    "ManifestWriteError",
    "OPEN_LOVABLE_ATTRIBUTION",
    "build_clone_manifest",
    "compute_manifest_hash",
    "finalise_manifest",
    "inject_html_traceability_comment",
    "manifest_to_audit_payload",
    "manifest_to_dict",
    "parse_html_traceability_comment",
    "pin_clone_artefacts",
    "read_manifest_file",
    "record_clone_audit",
    "render_html_traceability_comment",
    "serialize_manifest_json",
    "verify_manifest_hash",
    "write_manifest_file",
]
