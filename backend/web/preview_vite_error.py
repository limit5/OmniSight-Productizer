"""W16.6 #XXX — ``preview.vite_error`` / ``preview.vite_error_resolved`` SSE event contract.

Where this slots into the W16 epic
----------------------------------

W16.4 inlined the ``preview.ready`` SSE event so the chat surface
mounts an iframe the moment the dev server boots; W16.5 closed the
edit-while-preview live cycle by surfacing ``preview.hmr_reload`` for
agent edits.  W16.6 (this row) closes the **vite-error-in-dialogue
loop**: when the W14.1 sidecar's ``@omnisight/vite-plugin`` reports a
compile-time or runtime build error
(``POST /web-sandbox/preview/{ws}/error`` — the W15.1 endpoint), the
backend already feeds it into ``state.error_history`` (W15.2) and
quotes it back to the agent (W15.3) and the agent's specialist node
proposes a fix.  W16.6 adds the **operator-visible chat trace**:

::

    @omnisight/vite-plugin POSTs the error                       ← W15.1
                          ↓
        ViteErrorBuffer captures + dedupes                       ← W15.1
                          ↓
        backend.web.vite_error_relay folds into history          ← W15.2
                          ↓
        backend.web.preview_vite_error.emit_preview_vite_error   ← W16.6 (this row)
              status="detected"  →  preview.vite_error
                          ↓
        Frontend SSE consumer appends a chat message:
          "我看到 src/Header.tsx 有 syntax_error，正在修…"
                          ↓
        Agent edit pipeline writes the fix + vite HMR reloads
                          ↓
        backend.web.vite_self_fix.classify_vite_error_for_self_fix
        recognises the next history entry as the same signature
        but the file no longer triggers an error
                          ↓
        backend.web.preview_vite_error.emit_preview_vite_error   ← W16.6
              status="resolved"  →  preview.vite_error_resolved
                          ↓
        Frontend SSE consumer appends a chat message: "已修 ✓"

Two distinct event names so the FE can branch on the wire literal
without parsing a status field.  ``preview.vite_error`` and
``preview.vite_error_resolved`` share one payload shape so the chat
renderer can switch on the same ``previewViteError`` field with a
``status`` discriminator inside it.

Frozen wire shape
-----------------

The SSE payload mirrors the W16.4 / W16.5 sibling events::

    {event: "preview.vite_error",
     data: {workspace_id, status, label,
            error_class?, target?, error_signature?,
            source_path?, source_line?,
            timestamp, _session_id, _broadcast_scope, _tenant_id}}

Frontend consumers MUST treat unknown extra keys as forward-compatible
(per Q.4 SSE policy).

Three explicit goals:

* **W16.6 frontend** consumes ``workspace_id`` to scope the error
  trace to the right chat thread, ``status`` to render either the
  in-flight "正在修…" hint or the "已修 ✓" badge, ``label`` for the
  chat-message body, and ``error_class`` (one of
  :data:`backend.web.vite_self_fix.VITE_SELF_FIX_CLASSES` plus
  :data:`backend.web.vite_self_fix.VITE_SELF_FIX_UNCLASSIFIED_TOKEN`)
  to render an icon / colour bucket.
* **W16.7 next-step coaching** can branch on the resolved event to
  surface the "(a) deploy / (b) a11y / (c) commit / (d) keep editing"
  next-step menu after the error dust settles.
* **W16.9 e2e** can pin both event names + status enum as stable
  bucket keys.

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  zero mutable module-level state — only frozen string
constants, int caps, frozen tuples of statuses, a frozen
:class:`PreviewViteErrorPayload` dataclass, a typed
:class:`PreviewViteErrorError` subclass, and stdlib imports
(``dataclasses`` / ``typing``).  Answer #1 — every uvicorn worker
reads the same constants from the same git checkout; emission is
per-request, scoped via :func:`backend.events._resolve_scope` which
already enforces multi-worker delivery via Redis Pub/Sub when
configured.  No singleton, no in-memory cache.

Read-after-write timing audit (per SOP §2): N/A — pure projection
from a W15.2 history-entry string (or arbitrary kwargs) to a wire
dict, then fire-and-forget ``bus.publish``.  No DB pool, no
compat→pool conversion, no ``asyncio.gather`` race surface.
``preview.vite_error`` and ``preview.vite_error_resolved`` are
intentionally absent from :data:`backend.events._PERSIST_EVENT_TYPES`
— the events are bookended around the operator's chat session
(error appears, error is fixed) and replaying them out of order
post-recovery would surface a stale "正在修…" toast for an already-
resolved error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ── Frozen wire-shape constants ──────────────────────────────────────

#: SSE event type emitted by :func:`emit_preview_vite_error` when
#: ``status="detected"``.  Frozen — frontend SSE consumer switches on
#: this literal to render the "正在修…" hint card.
PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME: str = "preview.vite_error"

#: SSE event type emitted by :func:`emit_preview_vite_error` when
#: ``status="resolved"``.  Frozen — frontend SSE consumer switches on
#: this literal to render the "已修 ✓" badge card.  Distinct event
#: name (rather than the same name with a status field) so a stray
#: replay of a stale resolved event does not flicker the in-flight
#: hint card off prematurely.
PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME: str = "preview.vite_error_resolved"

#: Pipeline phase string used when callers want to emit a sibling
#: :func:`backend.events.emit_pipeline_phase` (e.g. for the operator's
#: pipeline timeline UI).  Frozen so the snake_case style mirrors the
#: W14.* / W16.4 / W16.5 phases that already populate
#: :mod:`backend.routers.system`.
PREVIEW_VITE_ERROR_PIPELINE_PHASE: str = "preview_vite_error"

#: Default broadcast scope when callers don't pass one explicitly.
#: ``"session"`` because the underlying preview iframe is per-operator-
#: session (matches W16.4/W16.5 rationale: CF Access SSO gates ingress
#: to the launching operator's email, so broadcasting globally would
#: mean other tenants see an error trace for a workspace they cannot
#: open).
PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE: str = "session"


# ── Status discriminator ─────────────────────────────────────────────

#: Status string carried inside the payload distinguishing the
#: "agent has noticed and is working on it" tick from the "agent
#: shipped a fix and the next vite build is clean" tick.  Pinned by
#: drift guard.
PREVIEW_VITE_ERROR_STATUS_DETECTED: str = "detected"

#: Sibling status — see :data:`PREVIEW_VITE_ERROR_STATUS_DETECTED`.
PREVIEW_VITE_ERROR_STATUS_RESOLVED: str = "resolved"

#: Ordered tuple of all recognised statuses.  Order is detection
#: lifecycle (detected → resolved); frozen tuple so callers may rely
#: on index-based iteration without defensive copies.
PREVIEW_VITE_ERROR_STATUSES: tuple[str, ...] = (
    PREVIEW_VITE_ERROR_STATUS_DETECTED,
    PREVIEW_VITE_ERROR_STATUS_RESOLVED,
)


# ── Default human-facing labels ──────────────────────────────────────

#: Default chat-message body for the detection event.  Bilingual
#: ("我看到 preview 有 error，正在修…" — row spec verbatim) so the
#: W16.9 e2e tests grep a stable substring.  Callers that want a
#: per-error hint pass a custom one via
#: :func:`format_preview_vite_error_detected_label`.
PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL: str = (
    "我看到 preview 有 error，正在修…"
)

#: Default chat-message body for the resolved event.  Row spec
#: verbatim ("已修 ✓") so the W16.9 e2e tests grep a stable substring.
PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL: str = "已修 ✓"


# ── Bound caps (defensive) ───────────────────────────────────────────

#: Hard cap on the workspace_id field — mirrors the W16.4 /
#: W16.5 caps so a payload that round-trips through the three events
#: validates with the same byte budget.
MAX_PREVIEW_VITE_ERROR_WORKSPACE_ID_BYTES: int = 256

#: Hard cap on the chat-message label.  Slightly looser than the
#: W16.4 / W16.5 120-byte cap because the detection label embeds
#: the file path + error-class identifier (``"我看到 src/components/
#: Header.tsx 有 syntax_error，正在修…"`` blows past 120 chars when
#: the file path is deep but is still a reasonable single line).
MAX_PREVIEW_VITE_ERROR_LABEL_BYTES: int = 200

#: Hard cap on the optional ``target`` field — the human-friendly
#: identifier the chat embeds in the label (typically a relative
#: file path; degraded payloads default to ``"preview"``).  Sized
#: generously so deep monorepo paths don't truncate, but bounded so
#: a pathological JS plugin payload cannot blow up the SSE frame.
MAX_PREVIEW_VITE_ERROR_TARGET_BYTES: int = 200

#: Hard cap on the optional ``error_class`` field — must fit any
#: entry in :data:`backend.web.vite_self_fix.VITE_SELF_FIX_CLASSES`
#: plus :data:`backend.web.vite_self_fix.VITE_SELF_FIX_UNCLASSIFIED_TOKEN`
#: with 2× headroom for any future class.
MAX_PREVIEW_VITE_ERROR_ERROR_CLASS_BYTES: int = 32

#: Hard cap on the optional ``error_signature`` field — the W15.4
#: head-only signature ``vite[<phase>] <file>:<line>: <kind>:`` plus
#: 2× headroom for the message-prefix the FE may render in a tooltip.
#: Mirrors :data:`backend.web.vite_error_relay.MAX_VITE_ERROR_HISTORY_LINE_BYTES`.
MAX_PREVIEW_VITE_ERROR_ERROR_SIGNATURE_BYTES: int = 280

#: Hard cap on the optional ``source_path`` field — POSIX paths can
#: be up to ``PATH_MAX`` (typically 4096); 4096 here matches the
#: W16.5 :data:`backend.web.preview_hmr_reload.MAX_PREVIEW_HMR_RELOAD_SOURCE_PATH_BYTES`
#: ceiling so even pathological monorepos with deep nesting survive.
MAX_PREVIEW_VITE_ERROR_SOURCE_PATH_BYTES: int = 4096


# ── Public dataclass ─────────────────────────────────────────────────


class PreviewViteErrorError(ValueError):
    """Raised by :func:`build_preview_vite_error_payload` when a field
    violates the frozen contract (over-cap / non-string / empty
    workspace_id / unknown status).  Subclasses :class:`ValueError`
    so callers that already except on bad input keep working unchanged.
    """


@dataclass(frozen=True)
class PreviewViteErrorPayload:
    """Frozen wire-shape for the ``preview.vite_error`` /
    ``preview.vite_error_resolved`` SSE events.

    Attributes
    ----------
    workspace_id:
        The W14 workspace id the error belongs to.  Mandatory — the
        frontend uses this to scope the trace to the right chat
        thread / iframe.
    status:
        One of :data:`PREVIEW_VITE_ERROR_STATUSES`.  The two distinct
        event names share this dataclass; callers usually go via
        :func:`emit_preview_vite_error` which sets the matching event
        name automatically.
    label:
        Human-facing chat-message body.  Defaults to
        :data:`PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL` when
        ``status="detected"`` and
        :data:`PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL` when
        ``status="resolved"``.  Callers that want a per-error hint
        ("我看到 src/Header.tsx 有 syntax_error，正在修…") pass a
        custom one (see :func:`format_preview_vite_error_detected_label`).
    error_class:
        Optional one of :data:`backend.web.vite_self_fix.VITE_SELF_FIX_CLASSES`
        plus :data:`backend.web.vite_self_fix.VITE_SELF_FIX_UNCLASSIFIED_TOKEN`.
        The frontend can branch on this to render an icon / colour
        bucket.  ``None`` for resolved events that omit the class
        (the operator already saw the class on the detection card).
    target:
        Optional human-friendly identifier the chat embeds in the
        label — typically a relative file path.  ``None`` falls back
        to the row-spec literal ``"preview"`` inside
        :func:`format_preview_vite_error_detected_label`.
    error_signature:
        Optional W15.4 head-only signature
        (``vite[<phase>] <file>:<line>: <kind>:``) so the FE can
        correlate the resolved event with the matching detection
        card without parsing the full label.
    source_path:
        Optional repo-relative path of the file vite reported.
        Useful for the operator's debug panel; the chat renderer
        does not depend on it for label rendering.
    source_line:
        Optional 1-based line number inside :attr:`source_path`.
    """

    workspace_id: str
    status: str = PREVIEW_VITE_ERROR_STATUS_DETECTED
    label: str = PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL
    error_class: str | None = None
    target: str | None = None
    error_signature: str | None = None
    source_path: str | None = None
    source_line: int | None = None

    def event_name(self) -> str:
        """Return the SSE event name matching this payload's status.

        Centralised so the dispatch in :func:`emit_preview_vite_error`
        is a one-liner and so test fixtures asserting on round-trip
        through both events can use the same projection.
        """
        if self.status == PREVIEW_VITE_ERROR_STATUS_RESOLVED:
            return PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME
        return PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME

    def to_event_data(self) -> dict[str, Any]:
        """Project the dataclass into the SSE event ``data`` dict.

        Optional fields are dropped when ``None`` so the wire payload
        stays tight (the W16.9 e2e tests grep for absence of unset
        keys to detect drift).
        """
        data: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "status": self.status,
            "label": self.label,
        }
        if self.error_class is not None:
            data["error_class"] = self.error_class
        if self.target is not None:
            data["target"] = self.target
        if self.error_signature is not None:
            data["error_signature"] = self.error_signature
        if self.source_path is not None:
            data["source_path"] = self.source_path
        if self.source_line is not None:
            data["source_line"] = self.source_line
        return data


# ── Validators / builders ────────────────────────────────────────────


def _require_str(name: str, value: Any, *, max_bytes: int,
                 allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PreviewViteErrorError(
            f"{name} must be str, got {type(value).__name__}"
        )
    if not allow_empty and not value:
        raise PreviewViteErrorError(f"{name} must be non-empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise PreviewViteErrorError(
            f"{name} exceeds {max_bytes}-byte cap"
        )
    return value


def _default_label_for_status(status: str) -> str:
    if status == PREVIEW_VITE_ERROR_STATUS_RESOLVED:
        return PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL
    return PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL


def format_preview_vite_error_detected_label(
    *,
    target: str | None = None,
    error_class: str | None = None,
) -> str:
    """Render the detection chat-message body with optional ``target``
    and ``error_class`` substitution.

    Bilingual literal — Chinese narration ("我看到 X 有 Y，正在修…")
    matches the row spec.  When both ``target`` and ``error_class``
    are ``None`` the function returns
    :data:`PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL` byte-equal so
    drift-guard tests can pin the default.

    Always renders within :data:`MAX_PREVIEW_VITE_ERROR_LABEL_BYTES`
    by clipping the substitutions if needed (W16.6 prefers a
    truncated label to a missing message — the chat is meant to be
    informational, not a debug log).
    """
    target_str = (target or "").strip() or "preview"
    error_class_str = (error_class or "").strip()
    if error_class_str:
        # Don't double-print "error" when the class identifier
        # already ends in "error" (e.g. "syntax_error" reads as
        # "syntax_error" not "syntax_error error").
        if error_class_str.endswith("error"):
            body = f"我看到 {target_str} 有 {error_class_str}，正在修…"
        else:
            body = f"我看到 {target_str} 有 {error_class_str} error，正在修…"
    elif target:
        body = f"我看到 {target_str} 有 error，正在修…"
    else:
        body = PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_PREVIEW_VITE_ERROR_LABEL_BYTES:
        return body
    cut = MAX_PREVIEW_VITE_ERROR_LABEL_BYTES
    while cut > 0 and (encoded[cut] & 0b1100_0000) == 0b1000_0000:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore")


def build_preview_vite_error_payload(
    *,
    workspace_id: str,
    status: str | None = None,
    label: str | None = None,
    error_class: str | None = None,
    target: str | None = None,
    error_signature: str | None = None,
    source_path: str | None = None,
    source_line: int | None = None,
) -> PreviewViteErrorPayload:
    """Validate inputs and return a frozen
    :class:`PreviewViteErrorPayload`.

    Raises :class:`PreviewViteErrorError` (a :class:`ValueError`
    subclass) on contract violations.  The validator is intentionally
    strict — :func:`emit_preview_vite_error` swallows nothing, so a
    bad call site surfaces in the operator's logs rather than
    silently dropping a chat message.
    """

    workspace_id = _require_str(
        "workspace_id", workspace_id,
        max_bytes=MAX_PREVIEW_VITE_ERROR_WORKSPACE_ID_BYTES,
    )
    if status is None:
        status_value = PREVIEW_VITE_ERROR_STATUS_DETECTED
    else:
        status_value = _require_str(
            "status", status,
            max_bytes=64,
        )
        if status_value not in PREVIEW_VITE_ERROR_STATUSES:
            raise PreviewViteErrorError(
                f"unknown status {status_value!r}; expected one of "
                f"{sorted(PREVIEW_VITE_ERROR_STATUSES)}"
            )
    if label is None:
        label_value = _default_label_for_status(status_value)
    else:
        label_value = _require_str(
            "label", label,
            max_bytes=MAX_PREVIEW_VITE_ERROR_LABEL_BYTES,
            allow_empty=True,
        ) or _default_label_for_status(status_value)
    if error_class is not None:
        error_class = _require_str(
            "error_class", error_class,
            max_bytes=MAX_PREVIEW_VITE_ERROR_ERROR_CLASS_BYTES,
        )
    if target is not None:
        target = _require_str(
            "target", target,
            max_bytes=MAX_PREVIEW_VITE_ERROR_TARGET_BYTES,
        )
    if error_signature is not None:
        error_signature = _require_str(
            "error_signature", error_signature,
            max_bytes=MAX_PREVIEW_VITE_ERROR_ERROR_SIGNATURE_BYTES,
        )
    if source_path is not None:
        source_path = _require_str(
            "source_path", source_path,
            max_bytes=MAX_PREVIEW_VITE_ERROR_SOURCE_PATH_BYTES,
        )
    if source_line is not None:
        if not isinstance(source_line, int) or isinstance(source_line, bool):
            raise PreviewViteErrorError("source_line must be int")
        if source_line < 0:
            raise PreviewViteErrorError(
                f"source_line must be >= 0, got {source_line}"
            )
    return PreviewViteErrorPayload(
        workspace_id=workspace_id,
        status=status_value,
        label=label_value,
        error_class=error_class,
        target=target,
        error_signature=error_signature,
        source_path=source_path,
        source_line=source_line,
    )


def build_chat_message_for_preview_vite_error(
    payload: PreviewViteErrorPayload,
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Project a :class:`PreviewViteErrorPayload` into the chat-message
    shape the frontend ``WorkspaceChat`` consumes.

    Returns a dict mirroring ``WorkspaceChatMessage`` (TypeScript) so
    the SSE consumer can ``messages = [...messages, msg]`` without
    any intermediate translator.

    The ``previewViteError`` field is the W16.6-specific extension on
    ``WorkspaceChatMessage`` — sibling field to W16.4's ``previewEmbed``
    and W16.5's ``previewHmrReload``.  The three never co-exist on a
    single message because the FE renderer treats them as mount /
    refresh / error-trace respectively.
    """

    msg: dict[str, Any] = {
        "id": message_id or "",
        "role": "system",
        "text": payload.label,
        "previewViteError": {
            "workspaceId": payload.workspace_id,
            "status": payload.status,
            "label": payload.label,
        },
    }
    if payload.error_class is not None:
        msg["previewViteError"]["errorClass"] = payload.error_class
    if payload.target is not None:
        msg["previewViteError"]["target"] = payload.target
    if payload.error_signature is not None:
        msg["previewViteError"]["errorSignature"] = payload.error_signature
    if payload.source_path is not None:
        msg["previewViteError"]["sourcePath"] = payload.source_path
    if payload.source_line is not None:
        msg["previewViteError"]["sourceLine"] = payload.source_line
    return msg


def preview_vite_error_payload_from_history_entry(
    entry: str,
    *,
    workspace_id: str,
    status: str = PREVIEW_VITE_ERROR_STATUS_DETECTED,
    classify: Any = None,
) -> PreviewViteErrorPayload | None:
    """Best-effort projection from a W15.2-formatted history entry to
    a :class:`PreviewViteErrorPayload`.

    Returns ``None`` when the entry is not a W15.2 history entry
    (i.e. does not start with
    :data:`backend.web.vite_error_relay.VITE_ERROR_HISTORY_KEY_PREFIX`).

    ``classify`` is optional — callers that have already imported
    :func:`backend.web.vite_self_fix.classify_vite_error_for_self_fix`
    can pass it in to avoid the late-import cost; ``None`` falls back
    to the canonical classifier.  Tests pass a stub to assert the
    projection is decoupled from the classifier identity.

    The projection extracts:
      * ``error_signature`` — the W15.4 head-only signature
        ``vite[<phase>] <file>:<line>: <kind>:``
      * ``error_class`` — one of
        :data:`backend.web.vite_self_fix.VITE_SELF_FIX_CLASSES`
        plus
        :data:`backend.web.vite_self_fix.VITE_SELF_FIX_UNCLASSIFIED_TOKEN`
      * ``target`` / ``source_path`` — the file token
      * ``source_line`` — the line number when present (``"?"``
        means unknown → ``None``)
      * ``label`` — pre-rendered detection label embedding the
        target + error_class

    For ``status="resolved"`` the function still returns the same
    metadata (so the FE can correlate via ``error_signature``) but
    swaps in :data:`PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL` for
    the chat body.
    """

    # Late imports — keep the W15.2 / W15.6 modules out of the import
    # graph for callers that don't need projection from a history
    # entry (most tests construct payloads directly).
    from backend.web.vite_error_relay import (
        VITE_ERROR_HISTORY_KEY_PREFIX,
        VITE_ERROR_HISTORY_NO_FILE_TOKEN,
        vite_error_history_signature,
    )
    if classify is None:
        from backend.web.vite_self_fix import (
            VITE_SELF_FIX_UNCLASSIFIED_TOKEN,
            classify_vite_error_for_self_fix as _classify,
        )
    else:
        from backend.web.vite_self_fix import VITE_SELF_FIX_UNCLASSIFIED_TOKEN
        _classify = classify

    if not isinstance(entry, str) or not entry.startswith(
        VITE_ERROR_HISTORY_KEY_PREFIX
    ):
        return None

    # Extract error signature (head only) — matches the W15.4 pattern
    # detector so resolution events correlate with detection events.
    sig_tuple = vite_error_history_signature([entry])
    error_signature = sig_tuple[0] if sig_tuple else None
    error_class = _classify(entry) or VITE_SELF_FIX_UNCLASSIFIED_TOKEN

    # Extract file token + line — best-effort split on the W15.2
    # ``vite[<phase>] <file>:<line>: <kind>: <message>`` shape.
    target: str | None = None
    source_path: str | None = None
    source_line: int | None = None
    try:
        after_phase = entry.split("] ", 1)[1]
    except IndexError:
        after_phase = ""
    if after_phase:
        # Split limit 2 so the first colon separates file from
        # line+rest, the second colon separates line from kind+rest.
        head_parts = after_phase.split(":", 2)
        if len(head_parts) >= 2:
            file_tok = head_parts[0].strip()
            line_tok = head_parts[1].strip()
            if file_tok and file_tok != VITE_ERROR_HISTORY_NO_FILE_TOKEN:
                target = file_tok
                source_path = file_tok
            if line_tok and line_tok != "?":
                try:
                    source_line = int(line_tok)
                    if source_line < 0:
                        source_line = None
                except ValueError:
                    source_line = None

    if status == PREVIEW_VITE_ERROR_STATUS_RESOLVED:
        label = PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL
    else:
        label = format_preview_vite_error_detected_label(
            target=target, error_class=error_class,
        )

    return build_preview_vite_error_payload(
        workspace_id=workspace_id,
        status=status,
        label=label,
        error_class=error_class,
        target=target,
        error_signature=error_signature,
        source_path=source_path,
        source_line=source_line,
    )


# ── Emission ─────────────────────────────────────────────────────────


def emit_preview_vite_error(
    *,
    workspace_id: str,
    status: str | None = None,
    label: str | None = None,
    error_class: str | None = None,
    target: str | None = None,
    error_signature: str | None = None,
    source_path: str | None = None,
    source_line: int | None = None,
    session_id: str | None = None,
    broadcast_scope: str | None = None,
    tenant_id: str | None = None,
    **extra: Any,
) -> PreviewViteErrorPayload:
    """Publish a frozen ``preview.vite_error`` /
    ``preview.vite_error_resolved`` SSE event and return the
    validated :class:`PreviewViteErrorPayload`.

    The SSE event name is dispatched via
    :meth:`PreviewViteErrorPayload.event_name` so callers only deal
    with one entry-point.

    Mirrors the kwargs shape of W16.4 / W16.5 ``emit_*`` siblings so
    the call site looks boringly identical.  Sync-safe; the underlying
    :class:`backend.events.EventBus` does not require an event loop
    for the local-fanout path and never raises on transport failure
    (best-effort inside :class:`EventBus.publish`).

    Returns the validated payload so the caller can stash it in the
    sandbox snapshot / audit row without re-validating.

    The ``broadcast_scope`` defaults to
    :data:`PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE` (``"session"``).
    """

    payload = build_preview_vite_error_payload(
        workspace_id=workspace_id,
        status=status,
        label=label,
        error_class=error_class,
        target=target,
        error_signature=error_signature,
        source_path=source_path,
        source_line=source_line,
    )
    # Late import to avoid a circular when ``backend.events`` itself
    # later wants to import from ``backend.web``.
    from backend.events import _resolve_scope, _auto_tenant, bus, _log
    resolved_scope = _resolve_scope(
        "emit_preview_vite_error",
        broadcast_scope,
        PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE,
    )
    data: dict[str, Any] = payload.to_event_data()
    if extra:
        # Caller-provided extras take lowest priority — never clobber
        # the frozen contract keys.
        for k, v in extra.items():
            data.setdefault(k, v)
    bus.publish(
        payload.event_name(),
        data,
        session_id=session_id,
        broadcast_scope=resolved_scope,
        tenant_id=_auto_tenant(tenant_id),
    )
    _log(
        f"[PREVIEW VITE ERROR] {payload.workspace_id} "
        f"{payload.status}: {payload.label}",
    )
    return payload


# ── Drift guards (assert at module-import time) ──────────────────────

assert PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME == "preview.vite_error", (
    "PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME drift — frontend SSE "
    "consumer switches on this literal"
)

assert PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME == "preview.vite_error_resolved", (
    "PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME drift — frontend SSE "
    "consumer switches on this literal"
)

assert PREVIEW_VITE_ERROR_PIPELINE_PHASE == "preview_vite_error", (
    "PREVIEW_VITE_ERROR_PIPELINE_PHASE drift — pipeline-timeline UI "
    "switches on this literal"
)

assert PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE == "session", (
    "PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE must be 'session' — "
    "preview iframes are per-operator-session"
)

assert PREVIEW_VITE_ERROR_STATUS_DETECTED == "detected", (
    "PREVIEW_VITE_ERROR_STATUS_DETECTED drift"
)

assert PREVIEW_VITE_ERROR_STATUS_RESOLVED == "resolved", (
    "PREVIEW_VITE_ERROR_STATUS_RESOLVED drift"
)

assert PREVIEW_VITE_ERROR_STATUSES == (
    PREVIEW_VITE_ERROR_STATUS_DETECTED,
    PREVIEW_VITE_ERROR_STATUS_RESOLVED,
), "PREVIEW_VITE_ERROR_STATUSES drift — order must be lifecycle"

assert len(set(PREVIEW_VITE_ERROR_STATUSES)) == len(
    PREVIEW_VITE_ERROR_STATUSES
), "PREVIEW_VITE_ERROR_STATUSES must be unique"

assert PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL.strip(), (
    "PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL cannot be empty / whitespace"
)

assert PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL.strip(), (
    "PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL cannot be empty / whitespace"
)

assert (
    len(PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL.encode("utf-8"))
    <= MAX_PREVIEW_VITE_ERROR_LABEL_BYTES
), (
    "PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL exceeds "
    "MAX_PREVIEW_VITE_ERROR_LABEL_BYTES"
)

assert (
    len(PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL.encode("utf-8"))
    <= MAX_PREVIEW_VITE_ERROR_LABEL_BYTES
), (
    "PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL exceeds "
    "MAX_PREVIEW_VITE_ERROR_LABEL_BYTES"
)

assert issubclass(PreviewViteErrorError, ValueError), (
    "PreviewViteErrorError must subclass ValueError — back-compat for "
    "callers that except on bad input"
)


__all__ = [
    "MAX_PREVIEW_VITE_ERROR_ERROR_CLASS_BYTES",
    "MAX_PREVIEW_VITE_ERROR_ERROR_SIGNATURE_BYTES",
    "MAX_PREVIEW_VITE_ERROR_LABEL_BYTES",
    "MAX_PREVIEW_VITE_ERROR_SOURCE_PATH_BYTES",
    "MAX_PREVIEW_VITE_ERROR_TARGET_BYTES",
    "MAX_PREVIEW_VITE_ERROR_WORKSPACE_ID_BYTES",
    "PREVIEW_VITE_ERROR_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_VITE_ERROR_DEFAULT_DETECTED_LABEL",
    "PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL",
    "PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME",
    "PREVIEW_VITE_ERROR_PIPELINE_PHASE",
    "PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME",
    "PREVIEW_VITE_ERROR_STATUSES",
    "PREVIEW_VITE_ERROR_STATUS_DETECTED",
    "PREVIEW_VITE_ERROR_STATUS_RESOLVED",
    "PreviewViteErrorError",
    "PreviewViteErrorPayload",
    "build_chat_message_for_preview_vite_error",
    "build_preview_vite_error_payload",
    "emit_preview_vite_error",
    "format_preview_vite_error_detected_label",
    "preview_vite_error_payload_from_history_entry",
]
