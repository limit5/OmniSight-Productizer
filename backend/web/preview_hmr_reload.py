"""W16.5 #XXX — ``preview.hmr_reload`` SSE event contract.

Where this slots into the W16 epic
----------------------------------

W16.4 inlined the ``preview.ready`` SSE event so the chat surface
mounts an iframe the moment the dev server boots.  W16.5 (this row)
closes the **edit-while-preview live cycle**: when the agent edits a
file inside an active sandbox workspace, vite's built-in HMR detects
the change via inotify and patches the in-iframe page directly.  The
backend doesn't need to *trigger* the HMR (vite owns that), but it
*does* need to surface a "preview just updated" signal in the chat so
the operator can correlate their request ("header 大一點") with the
visual change.

::

    Operator types: "header 大一點"
                          ↓
        backend.web.edit_intent.detect_edit_intents_in_text   (W16.5)
                          ↓
        Coach card surfaces the /edit-preview slash menu      (W16.5)
                          ↓
        Operator picks "Apply now" → /edit-preview <ws> "..."
                          ↓
        Agent edit pipeline writes Header.tsx (consumer-side)
                          ↓
        vite (the W14.1 sidecar's dev server) detects the change
        via chokidar / inotify and ships the HMR patch to the iframe
                          ↓
        backend.web.preview_hmr_reload.emit_preview_hmr_reload  ← THIS
                          ↓
        EventBus.publish("preview.hmr_reload", {workspace_id, ...})
                          ↓
        Frontend SSE consumer matches workspace_id to a mounted
        iframe and bumps its reload counter so a stale frame can
        force a full reload (HMR-stuck edge cases like vite plugin
        crashes still let the operator escape).

Frozen wire shape
-----------------

The SSE payload mirrors the ``preview.ready`` shape so the frontend
event router can switch on a single event-name literal: ``{event:
"preview.hmr_reload", data: {workspace_id, source_path?, change_kind?,
label?, timestamp, ...}}``.  Frontend consumers MUST treat unknown
extra keys as forward-compatible (per Q.4 SSE policy) — a future
W16.6 (vite error in dialogue) row can sprinkle ``error_message`` /
``severity`` without bumping any contract here.

Three explicit goals:

* **W16.5 frontend** consumes ``workspace_id`` to scope the reload
  signal to the right iframe and ``label`` for the chat-message body
  ("Preview updated: header bigger").
* **W16.6 vite error** will fire a sibling ``preview.error`` event
  that consumes the same workspace_id key for routing; W16.5 leaves
  that frame slot open by pinning the ``preview.`` namespace.
* **W16.9 e2e** can pin the event name as a stable bucket key.

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  zero mutable module-level state — only frozen string
constants (3 ``PREVIEW_HMR_*`` identifiers + ``PREVIEW_HMR_RELOAD_DEFAULT_LABEL``),
int caps (5 ``MAX_*`` constants), a frozen :class:`PreviewHmrReloadPayload`
dataclass, a typed :class:`PreviewHmrReloadError` subclass, and stdlib
imports (``dataclasses`` / ``typing``).  Answer #1 — every uvicorn
worker reads the same constants from the same git checkout; the
emission is per-request, scoped via :func:`backend.events._resolve_scope`
which already enforces multi-worker delivery via Redis Pub/Sub when
configured.  No singleton, no in-memory cache, no shared mutable state.

Read-after-write timing audit (per SOP §2): N/A — pure projection from
edit-pipeline metadata to a wire dict, then fire-and-forget
``bus.publish``.  No DB pool, no compat→pool conversion, no
``asyncio.gather`` race surface.  ``preview.hmr_reload`` is intentionally
absent from :data:`backend.events._PERSIST_EVENT_TYPES` — the event is
high-cardinality (one per save) and replaying old reload signals would
just re-flicker stale frames without recreating the underlying file
edits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ── Frozen wire-shape constants ──────────────────────────────────────

#: SSE event type emitted by :func:`emit_preview_hmr_reload`.  Matches
#: the ``preview.hmr_reload`` row-spec literal — pinned by drift guard
#: so the frontend's event router can switch on a string constant.
PREVIEW_HMR_RELOAD_EVENT_NAME: str = "preview.hmr_reload"

#: Pipeline phase string used when callers want to emit a sibling
#: :func:`backend.events.emit_pipeline_phase` (e.g. for the operator's
#: pipeline timeline UI).  Frozen so the snake_case style mirrors the
#: W14.* / W16.4 phases that already populate
#: :mod:`backend.routers.system`.
PREVIEW_HMR_RELOAD_PIPELINE_PHASE: str = "preview_hmr_reload"

#: Default broadcast scope when callers don't pass one explicitly.
#: ``"session"`` because the underlying preview URL is per-operator-
#: session (matches W16.4's :data:`PREVIEW_READY_DEFAULT_BROADCAST_SCOPE`
#: rationale: CF Access SSO gates ingress to the launching operator's
#: email, so broadcasting globally would mean other tenants see a
#: reload signal for a workspace they cannot open).
PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE: str = "session"

#: Default human-facing chat-message body when the caller does not
#: pass an explicit ``label``.  Frozen so the W16.9 e2e tests grep a
#: stable substring.
PREVIEW_HMR_RELOAD_DEFAULT_LABEL: str = "Preview updated"

#: Default change-kind when the caller does not classify the edit.
#: Mirrors the ``vite-plugin`` HMR update payload shape — vite uses
#: ``"update"`` / ``"full-reload"`` / ``"prune"``; we keep ``"update"``
#: as the fallback because it's the most common HMR case (single-file
#: hot-swap).
PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND: str = "update"

#: Whitelist of recognised change-kind values the frontend can branch
#: on.  Drift-guarded at module-import time.  Unknown values are
#: rejected by :func:`build_preview_hmr_reload_payload` so a typo in
#: a downstream emitter does not silently send an unactionable signal.
PREVIEW_HMR_RELOAD_CHANGE_KINDS: tuple[str, ...] = (
    "update",       # Standard HMR module replacement
    "full-reload",  # vite forced a full page refresh
    "prune",        # vite removed an obsolete module
    "error-clear",  # the W16.6 hook says "the error you saw is fixed"
)


# ── Bound caps (defensive) ───────────────────────────────────────────

#: Hard cap on the workspace_id field — mirrors the W16.4
#: :data:`backend.web.web_preview_ready.MAX_PREVIEW_READY_WORKSPACE_ID_BYTES`
#: so a payload that round-trips through both events validates with
#: the same byte budget.
MAX_PREVIEW_HMR_RELOAD_WORKSPACE_ID_BYTES: int = 256

#: Hard cap on the optional ``source_path`` field — POSIX paths can be
#: up to ``PATH_MAX`` (typically 4096 bytes); 4096 here matches that
#: ceiling so even pathological monorepos with deep nesting survive.
MAX_PREVIEW_HMR_RELOAD_SOURCE_PATH_BYTES: int = 4096

#: Hard cap on the change_kind field — matches the longest entry in
#: :data:`PREVIEW_HMR_RELOAD_CHANGE_KINDS` plus 2× headroom for any
#: future schema bump.
MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES: int = 32

#: Hard cap on the chat-message label — matches the W16.4
#: :data:`backend.web.web_preview_ready.MAX_PREVIEW_READY_LABEL_BYTES`
#: so the rendered card stays visually consistent across both events.
MAX_PREVIEW_HMR_RELOAD_LABEL_BYTES: int = 120

#: Hard cap on the optional ``edit_hash`` correlation field — matches
#: :data:`backend.web.edit_intent.EDIT_INTENT_HASH_HEX_LENGTH` (16
#: hex chars) plus 2× headroom for any future schema bump.
MAX_PREVIEW_HMR_RELOAD_EDIT_HASH_BYTES: int = 64


# ── Public dataclass ─────────────────────────────────────────────────


class PreviewHmrReloadError(ValueError):
    """Raised by :func:`build_preview_hmr_reload_payload` when a field
    violates the frozen contract (over-cap / non-string / empty
    workspace_id / unknown change_kind).  Subclasses :class:`ValueError`
    so callers that already except on bad input keep working unchanged.
    """


@dataclass(frozen=True)
class PreviewHmrReloadPayload:
    """Frozen wire-shape for the ``preview.hmr_reload`` SSE event.

    Mirrors the dict that :func:`emit_preview_hmr_reload` publishes so
    callers (mostly tests) can construct the payload separately,
    inspect it, then hand it to :func:`emit_preview_hmr_reload` via
    :meth:`to_event_data`.

    Attributes
    ----------
    workspace_id:
        The W14 workspace id the preview belongs to.  Mandatory — the
        frontend uses this to scope the reload signal to the right
        mounted iframe.
    label:
        Human-facing chat-message body.  Defaults to
        :data:`PREVIEW_HMR_RELOAD_DEFAULT_LABEL`; callers that want a
        per-edit hint ("Preview updated: header bigger") pass a custom
        one.
    change_kind:
        One of :data:`PREVIEW_HMR_RELOAD_CHANGE_KINDS`.  Defaults to
        :data:`PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND` (``"update"``);
        the frontend can branch on this to render a finer status icon.
    source_path:
        Optional repo-relative path of the file vite reported as
        changed.  Useful for the operator's debug panel; the iframe
        renderer does not depend on it for reload semantics.
    edit_hash:
        Optional 16-hex-char SHA-256 prefix from the originating
        :class:`backend.web.edit_intent.EditIntentRef` so the FE can
        correlate "Preview updated" with the matching coach card.
    """

    workspace_id: str
    label: str = PREVIEW_HMR_RELOAD_DEFAULT_LABEL
    change_kind: str = PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND
    source_path: str | None = None
    edit_hash: str | None = None

    def to_event_data(self) -> dict[str, Any]:
        """Project the dataclass into the SSE event ``data`` dict.

        Optional fields are dropped when ``None`` so the wire payload
        stays tight (the W16.9 e2e tests grep for absence of unset
        keys to detect drift).
        """
        data: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "label": self.label,
            "change_kind": self.change_kind,
        }
        if self.source_path is not None:
            data["source_path"] = self.source_path
        if self.edit_hash is not None:
            data["edit_hash"] = self.edit_hash
        return data


# ── Validators / builders ────────────────────────────────────────────


def _require_str(name: str, value: Any, *, max_bytes: int,
                 allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PreviewHmrReloadError(
            f"{name} must be str, got {type(value).__name__}"
        )
    if not allow_empty and not value:
        raise PreviewHmrReloadError(f"{name} must be non-empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise PreviewHmrReloadError(
            f"{name} exceeds {max_bytes}-byte cap"
        )
    return value


def build_preview_hmr_reload_payload(
    *,
    workspace_id: str,
    label: str | None = None,
    change_kind: str | None = None,
    source_path: str | None = None,
    edit_hash: str | None = None,
) -> PreviewHmrReloadPayload:
    """Validate inputs and return a frozen
    :class:`PreviewHmrReloadPayload`.

    Raises :class:`PreviewHmrReloadError` (a :class:`ValueError`
    subclass) on contract violations.  The validator is intentionally
    strict — :func:`emit_preview_hmr_reload` swallows nothing, so a bad
    call site surfaces in the operator's logs rather than silently
    dropping a reload signal.
    """

    workspace_id = _require_str(
        "workspace_id", workspace_id,
        max_bytes=MAX_PREVIEW_HMR_RELOAD_WORKSPACE_ID_BYTES,
    )
    if label is None:
        label_value = PREVIEW_HMR_RELOAD_DEFAULT_LABEL
    else:
        label_value = _require_str(
            "label", label,
            max_bytes=MAX_PREVIEW_HMR_RELOAD_LABEL_BYTES,
            allow_empty=True,
        ) or PREVIEW_HMR_RELOAD_DEFAULT_LABEL
    if change_kind is None:
        change_kind_value = PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND
    else:
        change_kind_value = _require_str(
            "change_kind", change_kind,
            max_bytes=MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES,
        )
        if change_kind_value not in PREVIEW_HMR_RELOAD_CHANGE_KINDS:
            raise PreviewHmrReloadError(
                f"unknown change_kind {change_kind_value!r}; expected "
                f"one of {sorted(PREVIEW_HMR_RELOAD_CHANGE_KINDS)}"
            )
    if source_path is not None:
        source_path = _require_str(
            "source_path", source_path,
            max_bytes=MAX_PREVIEW_HMR_RELOAD_SOURCE_PATH_BYTES,
        )
    if edit_hash is not None:
        edit_hash = _require_str(
            "edit_hash", edit_hash,
            max_bytes=MAX_PREVIEW_HMR_RELOAD_EDIT_HASH_BYTES,
        )
    return PreviewHmrReloadPayload(
        workspace_id=workspace_id,
        label=label_value,
        change_kind=change_kind_value,
        source_path=source_path,
        edit_hash=edit_hash,
    )


def build_chat_message_for_preview_hmr_reload(
    payload: PreviewHmrReloadPayload,
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Project a :class:`PreviewHmrReloadPayload` into the chat-message
    shape the frontend ``WorkspaceChat`` consumes.

    The returned dict mirrors ``WorkspaceChatMessage`` (TypeScript) so
    the SSE consumer can ``messages = [...messages, msg]`` without any
    intermediate translator.  ``message_id`` is optional — when omitted
    the consumer is expected to mint one from ``crypto.randomUUID()``
    so server- and client-side ids don't collide.

    The ``previewHmrReload`` field is the W16.5-specific extension on
    ``WorkspaceChatMessage`` — the existing W16.4 ``ChatPreviewEmbed``
    component switches on the matching ``workspaceId`` to bump its
    iframe-reload counter.  Sibling field to W16.4's ``previewEmbed``;
    the two never co-exist on a single message because the FE renderer
    treats one as "mount" and one as "refresh".
    """

    msg: dict[str, Any] = {
        "id": message_id or "",
        "role": "system",
        "text": payload.label,
        "previewHmrReload": {
            "workspaceId": payload.workspace_id,
            "label": payload.label,
            "changeKind": payload.change_kind,
        },
    }
    if payload.source_path is not None:
        msg["previewHmrReload"]["sourcePath"] = payload.source_path
    if payload.edit_hash is not None:
        msg["previewHmrReload"]["editHash"] = payload.edit_hash
    return msg


# ── Emission ─────────────────────────────────────────────────────────


def emit_preview_hmr_reload(
    *,
    workspace_id: str,
    label: str | None = None,
    change_kind: str | None = None,
    source_path: str | None = None,
    edit_hash: str | None = None,
    session_id: str | None = None,
    broadcast_scope: str | None = None,
    tenant_id: str | None = None,
    **extra: Any,
) -> PreviewHmrReloadPayload:
    """Publish a frozen ``preview.hmr_reload`` SSE event and return the
    validated :class:`PreviewHmrReloadPayload`.

    Mirrors the kwargs shape of :func:`backend.web.web_preview_ready.
    emit_preview_ready` so the call site looks boringly identical to
    its W16.4 sibling.  The function is sync-safe (the underlying
    :class:`backend.events.EventBus` does not require an event loop for
    the local-fanout path) and never raises on transport failure —
    Redis Pub/Sub fallback / persistence skip / etc. are all best-
    effort inside :class:`EventBus.publish`.

    Returns the validated payload so the caller can stash it in the
    sandbox snapshot / audit row without re-validating.

    The ``broadcast_scope`` defaults to
    :data:`PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE` (``"session"``)
    — see the module docstring for the rationale.  Callers that want a
    different scope (e.g. tenant-wide for a shared preview) pass it
    explicitly; the underlying ``_resolve_scope`` policy then surfaces
    the deprecation behaviour for unset scopes.
    """

    payload = build_preview_hmr_reload_payload(
        workspace_id=workspace_id,
        label=label,
        change_kind=change_kind,
        source_path=source_path,
        edit_hash=edit_hash,
    )
    # Late import to avoid a circular when ``backend.events`` itself
    # later wants to import from ``backend.web``.
    from backend.events import _resolve_scope, _auto_tenant, bus, _log
    resolved_scope = _resolve_scope(
        "emit_preview_hmr_reload",
        broadcast_scope,
        PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE,
    )
    data: dict[str, Any] = payload.to_event_data()
    if extra:
        # Caller-provided extras take lowest priority — we never let
        # them clobber the frozen contract keys (workspace_id /
        # label / change_kind / source_path / edit_hash).
        for k, v in extra.items():
            data.setdefault(k, v)
    bus.publish(
        PREVIEW_HMR_RELOAD_EVENT_NAME,
        data,
        session_id=session_id,
        broadcast_scope=resolved_scope,
        tenant_id=_auto_tenant(tenant_id),
    )
    _log(
        f"[PREVIEW HMR] {payload.workspace_id} "
        f"reload ({payload.change_kind}): {payload.label}",
    )
    return payload


# ── Drift guards (assert at module-import time) ──────────────────────

assert PREVIEW_HMR_RELOAD_EVENT_NAME == "preview.hmr_reload", (
    "PREVIEW_HMR_RELOAD_EVENT_NAME drift — frontend SSE consumer "
    "switches on this literal"
)

assert PREVIEW_HMR_RELOAD_PIPELINE_PHASE == "preview_hmr_reload", (
    "PREVIEW_HMR_RELOAD_PIPELINE_PHASE drift — pipeline-timeline UI "
    "switches on this literal"
)

assert PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE == "session", (
    "PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE must be 'session' — "
    "preview URLs are per-operator-session"
)

assert PREVIEW_HMR_RELOAD_DEFAULT_LABEL.strip(), (
    "PREVIEW_HMR_RELOAD_DEFAULT_LABEL cannot be empty / whitespace"
)

assert PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND in PREVIEW_HMR_RELOAD_CHANGE_KINDS, (
    "PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND drift — must be one of "
    "PREVIEW_HMR_RELOAD_CHANGE_KINDS"
)

assert len(set(PREVIEW_HMR_RELOAD_CHANGE_KINDS)) == len(
    PREVIEW_HMR_RELOAD_CHANGE_KINDS
), "PREVIEW_HMR_RELOAD_CHANGE_KINDS must be unique"

# Each change-kind must fit inside the per-field byte cap so a future
# emitter that picks the longest enum entry doesn't trip the validator.
assert all(
    len(k.encode("utf-8")) <= MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES
    for k in PREVIEW_HMR_RELOAD_CHANGE_KINDS
), (
    "PREVIEW_HMR_RELOAD_CHANGE_KINDS contains an entry over "
    "MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES — bump the cap or "
    "shorten the literal"
)

assert issubclass(PreviewHmrReloadError, ValueError), (
    "PreviewHmrReloadError must subclass ValueError — back-compat for "
    "callers that except on bad input"
)


__all__ = [
    "MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_EDIT_HASH_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_LABEL_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_SOURCE_PATH_BYTES",
    "MAX_PREVIEW_HMR_RELOAD_WORKSPACE_ID_BYTES",
    "PREVIEW_HMR_RELOAD_CHANGE_KINDS",
    "PREVIEW_HMR_RELOAD_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_HMR_RELOAD_DEFAULT_CHANGE_KIND",
    "PREVIEW_HMR_RELOAD_DEFAULT_LABEL",
    "PREVIEW_HMR_RELOAD_EVENT_NAME",
    "PREVIEW_HMR_RELOAD_PIPELINE_PHASE",
    "PreviewHmrReloadError",
    "PreviewHmrReloadPayload",
    "build_chat_message_for_preview_hmr_reload",
    "build_preview_hmr_reload_payload",
    "emit_preview_hmr_reload",
]
