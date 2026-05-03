"""W16.2 #XXX — Image-attachment / paste detection + vision-LLM layout spec.

Where this slots into the W16 epic
----------------------------------

W16 wires the orchestrator chat so the operator never has to learn a new
slash command — natural-language input alone surfaces the W11–W15
capability menu via the coach.  W16.1 covers URL pastes; W16.2 (this
row) covers image attachments / clipboard pastes.

::

    Operator pastes screenshot / drag-drops PNG / pastes data: URL
                          ↓
        backend.web.image_attachment.detect_image_attachments_in_text  ← W16.2
                          ↓
              List[ImageAttachmentRef]  (deduped, capped, hashed)
                          ↓
        backend.routers.invoke._detect_coaching_triggers
                          ↓
            "image_in_message:<hash16>" trigger keys
                          ↓
        backend.routers.invoke._build_coach_context (LLM path)
        backend.routers.invoke._build_templated_coach_message (fallback)
                          ↓
                Coach card surfaces three options:
                  (a) component       → /clone-image <ref> --as=component
                  (b) full-page       → /clone-image <ref> --as=page
                  (c) brand reference → /brand-image <ref>
                          ↓
         Vision LLM (Claude Sonnet 4.6 vision / GPT-4 Vision)
         generates a :class:`LayoutSpec` consumed by the chosen branch.

Detection wire shape
--------------------

The orchestrator chat surfaces images as text-embedded references so
the existing INVOKE plumbing (which already passes the operator's
``command`` as a single string through the SSE / planner pipeline) does
not need new transport.  Two reference shapes are recognised:

  1. **Data URLs** — ``data:image/<mime>;base64,<payload>``.  The
     frontend inlines small clipboard pastes (≤ ``MAX_DATA_URL_BYTES``)
     directly into the command string so a roundtrip to a separate
     upload endpoint is not required for the common "screenshot for
     reference" flow.  Larger pastes the frontend must POST to
     ``/uploads`` first and embed an attachment marker instead.

  2. **Attachment markers** — ``[image: <filename>]``.  The frontend
     appends these to the command after a successful drag-drop / file
     picker upload so the planner sees the attachment without having
     to base64 the bytes through SSE.

Both shapes converge on a single :class:`ImageAttachmentRef` record
identified by a 16-hex-char SHA-256 prefix so the trigger key
(``image_in_message:<hash16>``) stays bounded for the W16.1-style
suppress-via-sessionStorage pattern (cf. ``backend.routers.invoke._
detect_coaching_triggers``).

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  every constant / regex / dataclass in this module is
frozen literal — every uvicorn worker derives the same value from
source code (Answer #1, per-worker stateless derivation).  The vision
LLM call is per-invocation and stateless; cross-worker concern does
not apply.  No singleton, no in-memory cache, no shared mutable state.

Read-after-write timing audit (per SOP §2): N/A — pure projection from
``str`` to ``list[ImageAttachmentRef]`` and from ``ImageAttachmentRef``
to ``LayoutSpec``.  No DB pool, no compat→pool conversion, no
``asyncio.gather`` race surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Optional, Sequence, Tuple


# ── Frozen wire-shape constants ──────────────────────────────────────

#: SHA-256 prefix length (hex chars) used as the stable identifier for
#: an :class:`ImageAttachmentRef`.  16 hex = 64 bits of entropy — plenty
#: for the per-session dedup / suppress use-case while keeping the
#: trigger-key URL param bounded.  Pinned by drift guard so the
#: frontend's ``image_in_message:<hash16>`` parser cannot drift.
IMAGE_HASH_HEX_LENGTH: int = 16

#: Hard cap on the number of distinct images detected in a single
#: command.  Mirrors the W16.1 ``_MAX_URL_TRIGGERS`` philosophy:
#: prevents a runaway paste (e.g. operator dropped 50 design comps in
#: one go) from ballooning the coach card / LLM context block.
MAX_IMAGE_ATTACHMENTS: int = 3

#: Maximum chars used when echoing the attachment label inside the
#: coach card.  The full label is preserved in the trigger key for
#: suppress accuracy; only the *display* form is truncated.
MAX_IMAGE_LABEL_DISPLAY_CHARS: int = 64

#: Hard cap on the data-URL size we will accept inline in a command
#: string.  4 MiB base64 = ~3 MiB raw, comfortably above the typical
#: clipboard-paste screenshot (~150 KiB).  Beyond this the frontend
#: must POST to ``/uploads`` and embed an attachment marker instead.
MAX_DATA_URL_BYTES: int = 4 * 1024 * 1024

#: Recognised image MIME subtypes.  Lower-cased; matched
#: case-insensitively against the data URL.  Lock-step pinned with the
#: drift guard so a future "we accept HEIC now" change has to update
#: this constant explicitly.
SUPPORTED_IMAGE_MIME_SUBTYPES: Tuple[str, ...] = (
    "png",
    "jpeg",
    "jpg",
    "gif",
    "webp",
    "svg+xml",
)

#: Vision-LLM-available coach option identifiers.  Frozen for the
#: row-spec literal "(a) component / (b) 整頁 / (c) brand reference".
#: Kept compile-time so the W16.9 e2e tests have a stable bucket key.
IMAGE_COACH_CLASS_COMPONENT: str = "component"
IMAGE_COACH_CLASS_FULL_PAGE: str = "full_page"
IMAGE_COACH_CLASS_BRAND_REFERENCE: str = "brand_reference"

#: Row-spec-ordered tuple — UIs that render the option set MUST iterate
#: this order so the coach card stays deterministic across renders.
IMAGE_COACH_CLASSES: Tuple[str, ...] = (
    IMAGE_COACH_CLASS_COMPONENT,
    IMAGE_COACH_CLASS_FULL_PAGE,
    IMAGE_COACH_CLASS_BRAND_REFERENCE,
)


# ── Reference-source identifiers ─────────────────────────────────────

#: Reference shape produced by an inline ``data:image/<mime>;base64,…``
#: paste.  The bytes are reachable directly from the command string —
#: vision LLM call can decode without a follow-up fetch.
IMAGE_REF_KIND_DATA_URL: str = "data_url"

#: Reference shape produced by a ``[image: <filename>]`` attachment
#: marker.  The bytes live in the frontend's upload store; the vision
#: LLM caller is responsible for resolving the filename to bytes.
IMAGE_REF_KIND_MARKER: str = "marker"

IMAGE_REF_KINDS: Tuple[str, ...] = (
    IMAGE_REF_KIND_DATA_URL,
    IMAGE_REF_KIND_MARKER,
)


# ── Trigger-key contract (consumed by backend.routers.invoke) ────────

#: Coach trigger key prefix.  The full key shape is
#: ``image_in_message:<hash16>``; the W16.1-style suppress system in
#: ``backend.routers.invoke._detect_coaching_triggers`` consumes this
#: prefix verbatim.  Pinned by drift guard.
IMAGE_COACH_TRIGGER_PREFIX: str = "image_in_message:"


# ── Vision-LLM prompt scaffold (frozen) ──────────────────────────────

#: System prompt for the vision LLM.  Frozen literal so the coach
#: contract tests can assert on the substring.  Deliberately concise
#: — vision LLMs are expensive and the layout spec is consumed by
#: downstream agents, not the operator, so we trade prose for a
#: structured shape.
VISION_LLM_SYSTEM_PROMPT: str = (
    "You are a vision-to-layout extractor for the OmniSight orchestrator. "
    "Given one screenshot of a UI / brand reference, emit a SHORT layout "
    "spec the orchestrator can hand to a frontend agent. Format: 1-line "
    "summary, then bullet list of components (one per line: 'Header', "
    "'Hero with CTA', 'Pricing table', etc.), then 'Colors:' with up to "
    "5 hex codes, then 'Fonts:' with up to 3 family names. Total under "
    "300 words. No prose, no explanations, no markdown headers."
)

#: Soft cap on the rendered :class:`LayoutSpec` summary string (used
#: when serialising into the coach context block / agent prompt).
MAX_LAYOUT_SPEC_SUMMARY_CHARS: int = 280

#: Hard cap on the bytes of the final LayoutSpec when encoded as a
#: prompt context block.  Defensive — protects the downstream agent's
#: context budget if the vision LLM ignores the system prompt's word
#: limit.
MAX_LAYOUT_SPEC_BYTES: int = 2048


# ── Detection regexes (compiled at module-import time) ───────────────

# Data URL form:
#   data:image/<mime>;base64,<base64payload>
# - Mime is restricted to SUPPORTED_IMAGE_MIME_SUBTYPES via post-match
#   validation (regex stays permissive so an unrecognised mime emits a
#   clean diagnostic instead of a silent miss).
# - Payload runs until a whitespace boundary OR until the
#   end of the string.  Operators rarely paste multiple data URLs in
#   one message, but we must terminate on whitespace so a sentence-
#   embedded data URL is correctly bounded.
_DATA_URL_PATTERN: re.Pattern[str] = re.compile(
    r"data:image/([a-zA-Z0-9+\-.]+);base64,([A-Za-z0-9+/=]+)",
    re.IGNORECASE,
)

# Attachment marker form:
#   [image: <filename>]
# - Filename can contain any non-bracket character (including spaces
#   and UTF-8 — design files often have CJK names).
# - Whitespace inside ``[image:`` and around the filename is tolerated
#   so a casual paste does not silently miss.
_ATTACHMENT_MARKER_PATTERN: re.Pattern[str] = re.compile(
    r"\[\s*image\s*:\s*([^\]\r\n]+?)\s*\]",
    re.IGNORECASE,
)


# ── Public dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True)
class ImageAttachmentRef:
    """A single image attachment detected in the operator's command.

    Attributes
    ----------
    kind:
        :data:`IMAGE_REF_KIND_DATA_URL` or :data:`IMAGE_REF_KIND_MARKER`.
        Branched on by the vision LLM caller to decide whether the
        bytes are inline or need an upload-store fetch.
    mime_subtype:
        Lower-cased MIME subtype (e.g. ``"png"``, ``"jpeg"``,
        ``"svg+xml"``).  Empty string for marker references where the
        upload store is the source of truth on MIME.
    image_hash:
        Stable 16-hex-char SHA-256 prefix used as the suppress / dedup
        key.  For data URLs the hash covers the base64 payload; for
        markers it covers the filename string verbatim.  Two calls
        with the same input produce byte-identical hashes — drift
        guard pins this.
    display_label:
        Operator-facing short label.  For data URLs:
        ``"<mime>:<hash16>"`` so the operator can disambiguate
        multiple pastes; for markers: the filename verbatim.  Used by
        the coach card; the trigger key uses ``image_hash`` not the
        label.
    raw_excerpt:
        Up to 80 chars of the matched substring.  Helps debug-finding
        consumers correlate the trigger back to the original command
        without echoing the full base64 payload.
    """

    kind: str
    mime_subtype: str
    image_hash: str
    display_label: str
    raw_excerpt: str

    def trigger_key(self) -> str:
        """Return the W16.2 coach trigger key for this attachment."""
        return f"{IMAGE_COACH_TRIGGER_PREFIX}{self.image_hash}"


@dataclass(frozen=True)
class LayoutSpec:
    """Vision-LLM-derived layout spec for a single image.

    Lifted out of the vision LLM response by
    :func:`generate_layout_spec_for_image`.  Frozen so the spec can be
    safely cached / re-used across coach-card renders and downstream
    agent prompt injections without defensive copies.

    Attributes
    ----------
    image_hash:
        Stable identifier — matches the originating
        :class:`ImageAttachmentRef.image_hash`.  Drift guard pins the
        round-trip.
    summary:
        One-line headline (≤ :data:`MAX_LAYOUT_SPEC_SUMMARY_CHARS`).
        Used by the coach card.
    components:
        Tuple of detected component names in top-down visual order
        (e.g. ``("Header", "Hero with CTA", "Pricing table", "Footer")``).
    colors:
        Tuple of detected hex colour codes (lower-cased, ``"#"``-
        prefixed) — up to 5.
    fonts:
        Tuple of detected font family names — up to 3.
    raw_text:
        The vision LLM response verbatim (truncated to
        :data:`MAX_LAYOUT_SPEC_BYTES`).  Preserved so debugging can
        reconstruct the parser path even when the structured fields
        end up empty.
    degraded:
        ``True`` when the spec was synthesised by
        :func:`build_layout_spec_fallback` because the vision LLM was
        unavailable / returned empty / raised.  Coach-card renderers
        use this flag to decide whether to surface a "vision LLM
        unavailable" hint alongside the three options.
    """

    image_hash: str
    summary: str
    components: Tuple[str, ...] = field(default_factory=tuple)
    colors: Tuple[str, ...] = field(default_factory=tuple)
    fonts: Tuple[str, ...] = field(default_factory=tuple)
    raw_text: str = ""
    degraded: bool = False


class LayoutSpecError(Exception):
    """Raised by :func:`generate_layout_spec_for_image` callers that
    explicitly opt into ``raise_on_failure=True``.  Default behaviour
    is degraded-fallback (no exception) so the coach card never 5xxs
    the planner."""


# ── Helpers ──────────────────────────────────────────────────────────


def _compute_image_hash(payload: str | bytes) -> str:
    """Return the 16-hex-char SHA-256 prefix for *payload*.

    Empty / None input returns the SHA-256 of the empty string —
    callers should pre-validate before relying on the hash being
    "meaningful" (it always returns a stable 16-char string regardless).
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    elif payload is None:  # type: ignore[unreachable]
        payload = b""
    return hashlib.sha256(payload).hexdigest()[:IMAGE_HASH_HEX_LENGTH]


def _truncate_label(label: str) -> str:
    """Trim *label* to :data:`MAX_IMAGE_LABEL_DISPLAY_CHARS` with ellipsis."""
    if len(label) <= MAX_IMAGE_LABEL_DISPLAY_CHARS:
        return label
    return label[: MAX_IMAGE_LABEL_DISPLAY_CHARS - 1].rstrip() + "…"


def _normalise_mime_subtype(raw: str) -> str:
    """Lower-case and validate against :data:`SUPPORTED_IMAGE_MIME_SUBTYPES`.

    Returns ``""`` for unsupported / empty input — callers treat empty
    as "skip this match" so an operator pasting a malformed data URL
    does not coach a bogus trigger.
    """
    if not raw:
        return ""
    norm = raw.strip().lower()
    if norm in SUPPORTED_IMAGE_MIME_SUBTYPES:
        return norm
    return ""


def detect_image_attachments_in_text(text: str | None) -> list[ImageAttachmentRef]:
    """Return up to :data:`MAX_IMAGE_ATTACHMENTS` distinct
    :class:`ImageAttachmentRef` records found in *text*.

    Detection covers two reference shapes:

      * ``data:image/<mime>;base64,<payload>`` — inline paste.
        Filtered by :data:`SUPPORTED_IMAGE_MIME_SUBTYPES` and by
        :data:`MAX_DATA_URL_BYTES` (oversized pastes are silently
        dropped — the frontend should have routed them through
        ``/uploads`` instead, this is the defensive belt).

      * ``[image: <filename>]`` — attachment marker following an
        out-of-band upload.

    Order is preserved across the two shapes (i.e. the first detected
    ref in paste order, regardless of kind, comes first).  Duplicates
    (same :attr:`ImageAttachmentRef.image_hash`) are dropped.  Cap is
    a hard cap that protects the LLM context block / coach card from
    a runaway paste of N images.

    Empty / ``None`` input returns ``[]`` so callers can pipe arbitrary
    corpora through without pre-filtering.
    """
    if not text:
        return []

    # First pass: walk both regexes, record (offset, kind, ref) so we
    # can sort by offset to preserve paste-order.  Skipping invalid
    # matches keeps the dedup loop tight.
    candidates: list[tuple[int, ImageAttachmentRef]] = []

    for match in _DATA_URL_PATTERN.finditer(text):
        full = match.group(0)
        if len(full) > MAX_DATA_URL_BYTES:
            continue
        mime = _normalise_mime_subtype(match.group(1))
        if not mime:
            continue
        payload = match.group(2)
        if not payload:
            continue
        h = _compute_image_hash(payload)
        ref = ImageAttachmentRef(
            kind=IMAGE_REF_KIND_DATA_URL,
            mime_subtype=mime,
            image_hash=h,
            display_label=f"{mime}:{h}",
            raw_excerpt=full[:80],
        )
        candidates.append((match.start(), ref))

    for match in _ATTACHMENT_MARKER_PATTERN.finditer(text):
        full = match.group(0)
        filename = (match.group(1) or "").strip()
        if not filename:
            continue
        h = _compute_image_hash(filename)
        ref = ImageAttachmentRef(
            kind=IMAGE_REF_KIND_MARKER,
            mime_subtype="",
            image_hash=h,
            display_label=filename,
            raw_excerpt=full[:80],
        )
        candidates.append((match.start(), ref))

    candidates.sort(key=lambda t: t[0])

    seen: set[str] = set()
    out: list[ImageAttachmentRef] = []
    for _, ref in candidates:
        if ref.image_hash in seen:
            continue
        seen.add(ref.image_hash)
        out.append(ref)
        if len(out) >= MAX_IMAGE_ATTACHMENTS:
            break
    return out


def image_attachment_trigger_key(ref: ImageAttachmentRef) -> str:
    """Convenience wrapper around :meth:`ImageAttachmentRef.trigger_key`.

    Kept as a top-level function so call-sites that want the
    coach trigger key without unpacking the ref (e.g. test fixtures
    that use dict-style attachments) have a stable entry point.
    """
    return ref.trigger_key()


def trigger_keys_for_attachments(refs: Sequence[ImageAttachmentRef]) -> list[str]:
    """Return ``[ref.trigger_key() for ref in refs]`` preserving order."""
    return [ref.trigger_key() for ref in refs]


def build_layout_spec_fallback(ref: ImageAttachmentRef) -> LayoutSpec:
    """Synthesise a degraded :class:`LayoutSpec` for *ref* when no
    vision LLM is available.

    The fallback summary is operator-actionable — it explicitly tells
    the agent that no vision pass ran, so any downstream prompt that
    consumes the spec can decide whether to abort or proceed with the
    raw image bytes / filename only.
    """
    summary = (
        f"[degraded] Vision LLM unavailable — no layout extracted from "
        f"{ref.display_label}. Agent should fall back to operator-asked "
        "structure or skip the layout spec."
    )
    return LayoutSpec(
        image_hash=ref.image_hash,
        summary=summary[:MAX_LAYOUT_SPEC_SUMMARY_CHARS],
        components=(),
        colors=(),
        fonts=(),
        raw_text="",
        degraded=True,
    )


def parse_layout_spec_response(
    ref: ImageAttachmentRef, raw_text: str,
) -> LayoutSpec:
    """Parse the vision LLM's freeform response into a :class:`LayoutSpec`.

    Best-effort heuristic parser — the system prompt asks for a stable
    shape (summary line + bullets + ``Colors:`` line + ``Fonts:`` line)
    but vision LLMs occasionally omit sections.  Missing sections
    surface as empty tuples; the ``raw_text`` field always carries the
    full response so the downstream agent can re-parse if it needs to.

    Empty / whitespace-only input returns the same shape as
    :func:`build_layout_spec_fallback` but with ``degraded=False`` —
    callers that explicitly want the degraded marker should call the
    fallback helper directly.
    """
    text = (raw_text or "").strip()
    if not text:
        return LayoutSpec(
            image_hash=ref.image_hash,
            summary="",
            components=(),
            colors=(),
            fonts=(),
            raw_text="",
            degraded=False,
        )

    # Truncate very long responses defensively.
    if len(text.encode("utf-8")) > MAX_LAYOUT_SPEC_BYTES:
        # Truncate on a codepoint boundary so the stored text stays decodable.
        encoded = text.encode("utf-8")[:MAX_LAYOUT_SPEC_BYTES]
        text = encoded.decode("utf-8", errors="ignore")

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    summary = lines[0][:MAX_LAYOUT_SPEC_SUMMARY_CHARS] if lines else ""

    components: list[str] = []
    colors: list[str] = []
    fonts: list[str] = []

    for line in lines[1:]:
        stripped = line.lstrip("-* \t").strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith("colors:") or lower.startswith("colours:"):
            payload = stripped.split(":", 1)[1] if ":" in stripped else ""
            colors.extend(_extract_hex_colors(payload))
        elif lower.startswith("fonts:") or lower.startswith("typeface:"):
            payload = stripped.split(":", 1)[1] if ":" in stripped else ""
            fonts.extend(_extract_font_families(payload))
        else:
            # Treat as a component bullet.
            components.append(stripped[:120])

    return LayoutSpec(
        image_hash=ref.image_hash,
        summary=summary,
        components=tuple(components[:20]),
        colors=tuple(colors[:5]),
        fonts=tuple(fonts[:3]),
        raw_text=text,
        degraded=False,
    )


_HEX_COLOR_PATTERN: re.Pattern[str] = re.compile(
    r"#?[0-9A-Fa-f]{6}\b",
)


def _extract_hex_colors(payload: str) -> list[str]:
    """Pull hex codes out of a comma / space-separated payload."""
    out: list[str] = []
    for match in _HEX_COLOR_PATTERN.finditer(payload or ""):
        token = match.group(0).lower()
        if not token.startswith("#"):
            token = f"#{token}"
        if token not in out:
            out.append(token)
    return out


def _extract_font_families(payload: str) -> list[str]:
    """Pull font family names out of a comma-separated payload.

    Vision LLMs sometimes wrap names in quotes (``"Inter"``); we strip
    matching quote pairs before recording.  Empty tokens are dropped.
    """
    out: list[str] = []
    for token in (payload or "").split(","):
        t = token.strip().strip('"').strip("'").strip()
        if t and t not in out:
            out.append(t)
    return out


def generate_layout_spec_for_image(
    ref: ImageAttachmentRef,
    *,
    vision_llm: Optional[object] = None,
    raise_on_failure: bool = False,
) -> LayoutSpec:
    """Run *ref* through the vision LLM and return a :class:`LayoutSpec`.

    *vision_llm* must be a callable / object exposing one of:

      * ``invoke(messages: list)`` returning an object with a ``content``
        attribute (langchain-style chat-model surface), OR
      * ``generate_layout_spec(ref)`` returning a :class:`LayoutSpec`
        directly (for test fakes that want full control).

    When *vision_llm* is ``None`` the function returns the degraded
    :func:`build_layout_spec_fallback` result without raising — the
    coach card still has a useful structure to render.

    *raise_on_failure*: when ``True``, any LLM error is re-raised as
    :class:`LayoutSpecError` instead of producing a degraded result.
    Default is ``False`` so the planner / coach surface stays
    degradable.

    Module-global / cross-worker state audit: pure per-call delegation
    to the supplied LLM handle.  No shared cache, no module-level
    state — every call computes fresh.  Cross-worker concern N/A.
    """
    if vision_llm is None:
        return build_layout_spec_fallback(ref)

    try:
        # Prefer the explicit test-fake protocol so test code does not
        # have to mimic langchain message surfaces.
        if hasattr(vision_llm, "generate_layout_spec"):
            spec = vision_llm.generate_layout_spec(ref)
            if isinstance(spec, LayoutSpec):
                return spec
            # Wrong return type → degrade rather than crash the planner.
            if raise_on_failure:
                raise LayoutSpecError(
                    "vision_llm.generate_layout_spec returned non-LayoutSpec",
                )
            return build_layout_spec_fallback(ref)

        if hasattr(vision_llm, "invoke"):
            # Langchain-style: hand a system + user message.  We do NOT
            # encode the image bytes here — the caller is responsible
            # for binding them to the LLM (different vendors have
            # different image-content shapes).  This keeps the helper
            # vendor-agnostic.
            from langchain_core.messages import HumanMessage, SystemMessage

            messages = [
                SystemMessage(content=VISION_LLM_SYSTEM_PROMPT),
                HumanMessage(content=_render_user_prompt_for_ref(ref)),
            ]
            resp = vision_llm.invoke(messages)
            raw = getattr(resp, "content", "") or ""
            if not isinstance(raw, str):
                raw = str(raw)
            if not raw.strip():
                if raise_on_failure:
                    raise LayoutSpecError("vision_llm returned empty content")
                return build_layout_spec_fallback(ref)
            return parse_layout_spec_response(ref, raw)

        # Unknown LLM shape → degrade.
        if raise_on_failure:
            raise LayoutSpecError(
                "vision_llm does not implement generate_layout_spec or invoke",
            )
        return build_layout_spec_fallback(ref)
    except LayoutSpecError:
        raise
    except Exception as exc:
        if raise_on_failure:
            raise LayoutSpecError(f"vision LLM call failed: {exc}") from exc
        return build_layout_spec_fallback(ref)


def _render_user_prompt_for_ref(ref: ImageAttachmentRef) -> str:
    """Tiny user-side prompt accompanying the image binding.

    Frontend / vision-vendor adapter is responsible for attaching the
    actual image bytes to the LLM call (vendor-specific
    multi-modal content shape).  This helper just hands the LLM a
    one-line context label so the response stays grounded in the
    operator's pasted reference.
    """
    if ref.kind == IMAGE_REF_KIND_DATA_URL:
        return (
            f"Image attached as inline data URL "
            f"({ref.mime_subtype}, hash={ref.image_hash}). "
            "Extract the layout spec per the system prompt."
        )
    return (
        f"Image attached as upload-store reference "
        f"(filename={ref.display_label!r}, hash={ref.image_hash}). "
        "Extract the layout spec per the system prompt."
    )


# ── Drift guards (assert at module-import time) ──────────────────────

# Hash length guard — if a future PR shortens / lengthens the prefix
# without updating the trigger-key contract, the frontend's parser
# will silently misalign.  Surfacing the mismatch at import time
# pushes it to CI red.
assert IMAGE_HASH_HEX_LENGTH == 16, (
    f"IMAGE_HASH_HEX_LENGTH drift: expected 16, got {IMAGE_HASH_HEX_LENGTH!r}"
)

assert IMAGE_COACH_CLASSES == (
    IMAGE_COACH_CLASS_COMPONENT,
    IMAGE_COACH_CLASS_FULL_PAGE,
    IMAGE_COACH_CLASS_BRAND_REFERENCE,
), "IMAGE_COACH_CLASSES drift vs row-spec ordering"

assert IMAGE_COACH_TRIGGER_PREFIX.endswith(":"), (
    "IMAGE_COACH_TRIGGER_PREFIX must end in ':' for "
    "backend.routers.invoke._detect_coaching_triggers parsing"
)

assert SUPPORTED_IMAGE_MIME_SUBTYPES, (
    "SUPPORTED_IMAGE_MIME_SUBTYPES cannot be empty"
)


__all__ = [
    "IMAGE_COACH_CLASSES",
    "IMAGE_COACH_CLASS_BRAND_REFERENCE",
    "IMAGE_COACH_CLASS_COMPONENT",
    "IMAGE_COACH_CLASS_FULL_PAGE",
    "IMAGE_COACH_TRIGGER_PREFIX",
    "IMAGE_HASH_HEX_LENGTH",
    "IMAGE_REF_KINDS",
    "IMAGE_REF_KIND_DATA_URL",
    "IMAGE_REF_KIND_MARKER",
    "ImageAttachmentRef",
    "LayoutSpec",
    "LayoutSpecError",
    "MAX_DATA_URL_BYTES",
    "MAX_IMAGE_ATTACHMENTS",
    "MAX_IMAGE_LABEL_DISPLAY_CHARS",
    "MAX_LAYOUT_SPEC_BYTES",
    "MAX_LAYOUT_SPEC_SUMMARY_CHARS",
    "SUPPORTED_IMAGE_MIME_SUBTYPES",
    "VISION_LLM_SYSTEM_PROMPT",
    "build_layout_spec_fallback",
    "detect_image_attachments_in_text",
    "generate_layout_spec_for_image",
    "image_attachment_trigger_key",
    "parse_layout_spec_response",
    "trigger_keys_for_attachments",
]
