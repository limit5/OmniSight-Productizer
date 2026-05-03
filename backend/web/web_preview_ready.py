"""W16.4 #XXX — Inline preview-iframe SSE event contract.

Where this slots into the W16 epic
----------------------------------

W16.1 surfaced URL-paste intent; W16.2 surfaced image-attachment intent;
W16.3 surfaced freeform build intent; W16.4 (this row) **closes the
loop** between the W14 web-preview sidecar and the orchestrator chat:
when the dev server inside the W14.1 sidecar reports ready (via
``POST /web-sandbox/preview/{workspace_id}/ready``), the backend
publishes a frozen ``preview.ready`` SSE event carrying the sandbox
URL so the chat surface can inline an ``<iframe>`` message without the
operator polling docker logs from the browser.

::

    Operator types:           "/scaffold landing --auto-preview"   (W16.3)
                                    ↓
    backend.routers.invoke planner runs scaffold + launches W14
                                    ↓
    W14.1 sidecar finishes ``pnpm install`` and the dev server binds
                                    ↓
    polling task POSTs /web-sandbox/preview/{ws}/ready (W14.2 endpoint)
                                    ↓
    backend.routers.web_sandbox.mark_preview_ready
                                    ↓
    backend.web.web_preview_ready.emit_preview_ready (← W16.4 entry)
                                    ↓
    EventBus.publish("preview.ready", {workspace_id, preview_url, ...})
                                    ↓
    Frontend SSE consumer appends a chat message carrying the embed
                                    ↓
    components/omnisight/workspace-chat renders <iframe src=preview_url>
                                    ↓
    Operator clicks the fullscreen toggle to expand the preview

Frozen wire shape
-----------------

The SSE payload is intentionally narrow so the W16.9 e2e tests can pin
it byte-equal: ``{event: "preview.ready", data: {workspace_id,
preview_url, label, sandbox_id?, ingress_url?, host_port?,
timestamp, ...}}``. Frontend consumers MUST treat unknown extra keys as
forward-compatible (per Q.4 SSE policy) — a future W16.* row can
sprinkle ``status_message`` / ``schema_version`` without bumping any
contract here.

Three explicit goals:

* **W16.4 frontend** consumes ``preview_url`` and ``workspace_id``
  to render the iframe + the "Open in new tab" deep link; ``label``
  is the human-facing chat-message body.
* **W16.5 edit-while-preview** will reuse the same event after an
  HMR-driven rebuild — the consumer detects the workspace_id is
  already mounted and refreshes the iframe rather than appending a
  fresh chat row.
* **W16.6 vite error in dialogue** will fire a sibling
  ``preview.error`` event that consumes the same workspace_id key
  for routing; W16.4 leaves that frame slot open by pinning the
  ``preview.`` namespace.

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  zero mutable module-level state — only frozen string
constants (4 ``PREVIEW_READY_*`` identifiers + ``PREVIEW_READY_DEFAULT_LABEL``),
int caps (5 ``MAX_*`` constants), a frozen :class:`PreviewReadyPayload`
dataclass, a typed :class:`PreviewReadyError` subclass, and stdlib
imports (``dataclasses`` / ``typing``).  Answer #1 — every uvicorn
worker reads the same constants from the same git checkout; the
emission is per-request, scoped via :func:`backend.events._resolve_scope`
which already enforces multi-worker delivery via Redis Pub/Sub when
configured.  No singleton, no in-memory cache, no shared mutable state.

Read-after-write timing audit (per SOP §2): N/A — pure projection from
:class:`backend.web_sandbox.WebSandboxInstance` to a wire dict, then
fire-and-forget ``bus.publish``.  No DB pool, no compat→pool conversion,
no ``asyncio.gather`` race surface.  ``preview.ready`` is intentionally
absent from :data:`backend.events._PERSIST_EVENT_TYPES` — the event is
high-cardinality (one per dev-server boot per workspace) and the
underlying state already lives on
:attr:`backend.web_sandbox.WebSandboxInstance.ready_at` so SSE durability
gives no extra value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


# ── Frozen wire-shape constants ──────────────────────────────────────

#: SSE event type emitted by :func:`emit_preview_ready`.  Matches the
#: ``preview.ready`` row-spec literal — pinned by drift guard so the
#: frontend's event router can switch on a string constant.
PREVIEW_READY_EVENT_NAME: str = "preview.ready"

#: Pipeline phase string used when callers want to emit a sibling
#: :func:`backend.events.emit_pipeline_phase` (e.g. for the operator's
#: pipeline timeline UI).  Frozen so the ``-snake_case`` style mirrors
#: the W14.* phases that already populate ``backend.routers.system``.
PREVIEW_READY_PIPELINE_PHASE: str = "preview_ready"

#: Default broadcast scope when callers don't pass one explicitly.
#: ``"session"`` because a preview URL is per-operator-session — the
#: W14.4 CF Access SSO gate locks ingress to the launching operator's
#: email, so broadcasting globally would mean other tenants see a URL
#: they cannot open.
PREVIEW_READY_DEFAULT_BROADCAST_SCOPE: str = "session"

#: Default human-facing chat-message body when the caller does not
#: pass an explicit ``label``.  Frozen so the W16.9 e2e tests grep
#: a stable substring.
PREVIEW_READY_DEFAULT_LABEL: str = "Live preview ready"


# ── Bound caps (defensive) ───────────────────────────────────────────

#: Hard cap on the workspace_id field carried in the event payload.
#: Mirrors :data:`backend.routers.web_sandbox.LaunchPreviewRequest`'s
#: ``max_length=128`` plus a defensive 2× headroom for unknown future
#: callers.  Validation raises :class:`PreviewReadyError`.
MAX_PREVIEW_READY_WORKSPACE_ID_BYTES: int = 256

#: Hard cap on the preview URL length.  RFC 3986 doesn't define a
#: maximum URL length but most browsers / proxies cap around 2048;
#: 4096 here gives 2× headroom for query-string-heavy frameworks
#: while still bounding the SSE payload.
MAX_PREVIEW_READY_URL_BYTES: int = 4096

#: Hard cap on the chat-message label.  120 chars matches the W16.1 /
#: W16.2 / W16.3 display-cap pattern so the rendered card stays
#: visually consistent across all four W16.* trigger families.
MAX_PREVIEW_READY_LABEL_BYTES: int = 120

#: Hard cap on the sandbox_id field — matches the
#: :func:`backend.web_sandbox.format_sandbox_id` output (16-hex SHA-256
#: prefix) plus 2× headroom for any future schema bump.
MAX_PREVIEW_READY_SANDBOX_ID_BYTES: int = 64

#: Hard cap on the ingress URL length.  Same reasoning as
#: :data:`MAX_PREVIEW_READY_URL_BYTES`.
MAX_PREVIEW_READY_INGRESS_URL_BYTES: int = 4096


# ── Public dataclass ─────────────────────────────────────────────────


class PreviewReadyError(ValueError):
    """Raised by :func:`build_preview_ready_payload` when a field
    violates the frozen contract (over-cap / non-string / empty
    workspace_id).  Subclasses :class:`ValueError` so callers that
    already except on bad URL inputs keep working unchanged.
    """


@dataclass(frozen=True)
class PreviewReadyPayload:
    """Frozen wire-shape for the ``preview.ready`` SSE event.

    Mirrors the dict that :func:`emit_preview_ready` publishes so
    callers (mostly tests) can construct the payload separately,
    inspect it, then hand it to :func:`emit_preview_ready` via
    :meth:`to_event_data`.

    Attributes
    ----------
    workspace_id:
        The W14 workspace id the preview belongs to.  Mandatory — the
        frontend uses this to route the iframe message to the right
        chat thread.
    preview_url:
        Operator-facing URL the iframe loads.  Prefer the W14.3 CF
        Tunnel ingress URL when present; falls back to the W14.2
        host-port URL on dev-only deployments.  Mandatory.
    label:
        Human-facing chat-message body.  Defaults to
        :data:`PREVIEW_READY_DEFAULT_LABEL`; callers that want a
        per-launch hint (``"Landing page ready"``) pass a custom one.
    sandbox_id:
        Optional W14.1 sandbox id (the docker container name suffix).
        Useful for the operator's debug panel cross-link; the frontend
        does not depend on it for iframe rendering.
    ingress_url:
        Optional W14.3 ingress URL distinct from ``preview_url``.
        Only set when the manager has both a host-port URL and a CF
        Tunnel hostname; populated for parity with
        :class:`backend.web_sandbox.WebSandboxInstance`.
    host_port:
        Optional W14.2 host port.  Same rationale as ``ingress_url``.
    """

    workspace_id: str
    preview_url: str
    label: str = PREVIEW_READY_DEFAULT_LABEL
    sandbox_id: str | None = None
    ingress_url: str | None = None
    host_port: int | None = None

    def to_event_data(self) -> dict[str, Any]:
        """Project the dataclass into the SSE event ``data`` dict.

        Optional fields are dropped when ``None`` so the wire payload
        stays tight (the W16.9 e2e tests grep for absence of unset
        keys to detect drift).
        """
        data: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "preview_url": self.preview_url,
            "label": self.label,
        }
        if self.sandbox_id is not None:
            data["sandbox_id"] = self.sandbox_id
        if self.ingress_url is not None:
            data["ingress_url"] = self.ingress_url
        if self.host_port is not None:
            data["host_port"] = self.host_port
        return data


# ── Validators / builders ────────────────────────────────────────────


def _require_str(name: str, value: Any, *, max_bytes: int,
                 allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PreviewReadyError(
            f"{name} must be str, got {type(value).__name__}"
        )
    if not allow_empty and not value:
        raise PreviewReadyError(f"{name} must be non-empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise PreviewReadyError(
            f"{name} exceeds {max_bytes}-byte cap"
        )
    return value


def build_preview_ready_payload(
    *,
    workspace_id: str,
    preview_url: str,
    label: str | None = None,
    sandbox_id: str | None = None,
    ingress_url: str | None = None,
    host_port: int | None = None,
) -> PreviewReadyPayload:
    """Validate inputs and return a frozen :class:`PreviewReadyPayload`.

    Raises :class:`PreviewReadyError` (a :class:`ValueError` subclass)
    on contract violations.  The validator is intentionally strict —
    :func:`emit_preview_ready` swallows nothing, so a bad call site
    surfaces in the operator's logs rather than silently dropping a
    chat message.
    """

    workspace_id = _require_str(
        "workspace_id", workspace_id,
        max_bytes=MAX_PREVIEW_READY_WORKSPACE_ID_BYTES,
    )
    preview_url = _require_str(
        "preview_url", preview_url,
        max_bytes=MAX_PREVIEW_READY_URL_BYTES,
    )
    if label is None:
        label_value = PREVIEW_READY_DEFAULT_LABEL
    else:
        label_value = _require_str(
            "label", label,
            max_bytes=MAX_PREVIEW_READY_LABEL_BYTES,
            allow_empty=True,
        ) or PREVIEW_READY_DEFAULT_LABEL
    if sandbox_id is not None:
        sandbox_id = _require_str(
            "sandbox_id", sandbox_id,
            max_bytes=MAX_PREVIEW_READY_SANDBOX_ID_BYTES,
        )
    if ingress_url is not None:
        ingress_url = _require_str(
            "ingress_url", ingress_url,
            max_bytes=MAX_PREVIEW_READY_INGRESS_URL_BYTES,
        )
    if host_port is not None:
        if not isinstance(host_port, int) or isinstance(host_port, bool):
            raise PreviewReadyError("host_port must be int")
        if not (1 <= host_port <= 65535):
            raise PreviewReadyError(
                f"host_port out of range: {host_port!r}"
            )
    return PreviewReadyPayload(
        workspace_id=workspace_id,
        preview_url=preview_url,
        label=label_value,
        sandbox_id=sandbox_id,
        ingress_url=ingress_url,
        host_port=host_port,
    )


def build_chat_message_for_preview_ready(
    payload: PreviewReadyPayload,
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Project a :class:`PreviewReadyPayload` into the chat-message
    shape the frontend ``WorkspaceChat`` consumes.

    The returned dict mirrors ``WorkspaceChatMessage`` (TypeScript) so
    the SSE consumer can ``messages = [...messages, msg]`` without any
    intermediate translator.  ``message_id`` is optional — when omitted
    the consumer is expected to mint one from ``crypto.randomUUID()``
    so server- and client-side ids don't collide.

    The ``previewEmbed`` field is the W16.4-specific extension on
    ``WorkspaceChatMessage`` — the iframe renderer in
    ``components/omnisight/workspace-chat.tsx`` switches on its
    presence to mount the iframe + fullscreen toggle.
    """

    msg: dict[str, Any] = {
        "id": message_id or "",
        "role": "system",
        "text": payload.label,
        "previewEmbed": {
            "url": payload.preview_url,
            "workspaceId": payload.workspace_id,
            "label": payload.label,
        },
    }
    return msg


def preview_ready_payload_from_instance_dict(
    instance: Mapping[str, Any],
    *,
    label: str | None = None,
) -> PreviewReadyPayload | None:
    """Best-effort projection from a
    :meth:`backend.web_sandbox.WebSandboxInstance.to_dict` snapshot to
    a :class:`PreviewReadyPayload`.

    Returns ``None`` when the instance has no usable URL — typical for
    the dev-only path where neither the W14.3 CF Tunnel ingress nor
    the W14.2 host-port URL has been populated yet.  Callers should
    treat ``None`` as "skip the SSE emit; the state isn't ready".

    The W14.3 ``ingress_url`` (CF Tunnel) is preferred over the W14.2
    ``preview_url`` (host port) when both are present, because the
    ingress URL is the one that survives the operator's browser
    visiting from outside the docker network.  Dev-only deploys with
    no tunnel knob fall back to the host-port URL transparently.
    """

    workspace_id = instance.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return None
    ingress_url = instance.get("ingress_url")
    host_url = instance.get("preview_url")
    if isinstance(ingress_url, str) and ingress_url:
        chosen_url = ingress_url
    elif isinstance(host_url, str) and host_url:
        chosen_url = host_url
    else:
        return None
    sandbox_id = instance.get("sandbox_id")
    host_port_raw = instance.get("host_port")
    host_port: int | None
    if isinstance(host_port_raw, int) and not isinstance(host_port_raw, bool):
        host_port = host_port_raw
    else:
        host_port = None
    return build_preview_ready_payload(
        workspace_id=workspace_id,
        preview_url=chosen_url,
        label=label,
        sandbox_id=sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None,
        ingress_url=ingress_url if isinstance(ingress_url, str) and ingress_url and ingress_url != chosen_url else None,
        host_port=host_port,
    )


# ── Emission ─────────────────────────────────────────────────────────


def emit_preview_ready(
    *,
    workspace_id: str,
    preview_url: str,
    label: str | None = None,
    sandbox_id: str | None = None,
    ingress_url: str | None = None,
    host_port: int | None = None,
    session_id: str | None = None,
    broadcast_scope: str | None = None,
    tenant_id: str | None = None,
    **extra: Any,
) -> PreviewReadyPayload:
    """Publish a frozen ``preview.ready`` SSE event and return the
    validated :class:`PreviewReadyPayload`.

    Mirrors the kwargs shape of
    :func:`backend.events.emit_pipeline_phase` so the call site looks
    boringly identical to every other ``emit_*`` helper.  The function
    is sync-safe (the underlying :class:`backend.events.EventBus` does
    not require an event loop for the local-fanout path) and never
    raises on transport failure — Redis Pub/Sub fallback / persistence
    skip / etc. are all best-effort inside :class:`EventBus.publish`.

    Returns the validated payload so the caller can stash it in the
    sandbox snapshot / audit row without re-validating.

    The ``broadcast_scope`` defaults to
    :data:`PREVIEW_READY_DEFAULT_BROADCAST_SCOPE` (``"session"``) — see
    the module docstring for the rationale.  Callers that want a
    different scope (e.g. tenant-wide for a shared preview) pass it
    explicitly; the underlying ``_resolve_scope`` policy then surfaces
    the deprecation behaviour for unset scopes.
    """

    payload = build_preview_ready_payload(
        workspace_id=workspace_id,
        preview_url=preview_url,
        label=label,
        sandbox_id=sandbox_id,
        ingress_url=ingress_url,
        host_port=host_port,
    )
    # Late import to avoid a circular when ``backend.events`` itself
    # later wants to import from ``backend.web``.
    from backend.events import _resolve_scope, _auto_tenant, bus, _log
    resolved_scope = _resolve_scope(
        "emit_preview_ready",
        broadcast_scope,
        PREVIEW_READY_DEFAULT_BROADCAST_SCOPE,
    )
    data: dict[str, Any] = payload.to_event_data()
    if extra:
        # Caller-provided extras take lowest priority — we never let
        # them clobber the frozen contract keys (workspace_id /
        # preview_url / label / sandbox_id / ingress_url / host_port).
        for k, v in extra.items():
            data.setdefault(k, v)
    bus.publish(
        PREVIEW_READY_EVENT_NAME,
        data,
        session_id=session_id,
        broadcast_scope=resolved_scope,
        tenant_id=_auto_tenant(tenant_id),
    )
    _log(
        f"[PREVIEW] {payload.workspace_id} ready → {payload.preview_url}",
    )
    return payload


__all__ = [
    "MAX_PREVIEW_READY_INGRESS_URL_BYTES",
    "MAX_PREVIEW_READY_LABEL_BYTES",
    "MAX_PREVIEW_READY_SANDBOX_ID_BYTES",
    "MAX_PREVIEW_READY_URL_BYTES",
    "MAX_PREVIEW_READY_WORKSPACE_ID_BYTES",
    "PREVIEW_READY_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_READY_DEFAULT_LABEL",
    "PREVIEW_READY_EVENT_NAME",
    "PREVIEW_READY_PIPELINE_PHASE",
    "PreviewReadyError",
    "PreviewReadyPayload",
    "build_chat_message_for_preview_ready",
    "build_preview_ready_payload",
    "emit_preview_ready",
    "preview_ready_payload_from_instance_dict",
]
