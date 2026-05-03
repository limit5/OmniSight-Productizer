"""W16.7 — Next-step coaching after preview live.

Where this slots into the W16 epic
----------------------------------

W16.4 inlined the ``preview.ready`` SSE event so the chat surface
mounts an iframe the moment the dev server reports ready; W16.5 closed
the edit-while-preview live cycle; W16.6 surfaces vite errors as
in-flight / resolved chat trace cards.  W16.7 (this row) closes the
**after-preview-live coaching loop**: once the operator can actually see
the rendered preview, they want to know what to do next.  The four
high-leverage next steps the chat proactively surfaces are:

* **(a) Vercel deploy** — push the rendered project to Vercel for a
  shareable preview URL outside the W14 sandbox.
* **(b) a11y scan** — run an accessibility scan over the preview to
  surface WCAG violations before the operator commits.
* **(c) commit + PR** — drop the rendered project into git and open a
  PR so the work survives the workspace.
* **(d) continue edit** — keep iterating in chat (the operator may
  not be done designing yet; W16.5's edit-while-preview is the path).

::

    Operator types: "/scaffold landing --auto-preview"   (W16.3)
                          ↓
        backend.routers.invoke runs scaffold + launches W14 sidecar
                          ↓
        W14.1 sidecar finishes ``pnpm install`` + dev server binds
                          ↓
        polling task POSTs /web-sandbox/preview/{ws}/ready (W14.2)
                          ↓
        backend.routers.web_sandbox.mark_preview_ready
                          ↓
        backend.web.web_preview_ready.emit_preview_ready  (W16.4)
                          ↓
        backend.web.preview_next_steps.emit_preview_next_steps  ← THIS
                          ↓
        EventBus.publish("preview.next_steps", {workspace_id, options, ...})
                          ↓
        Frontend SSE consumer appends a chat message carrying the
        four-option menu so the operator can pick the next move.

Frozen wire shape
-----------------

Payload:  ``{event: "preview.next_steps", data: {workspace_id, label,
options[], preview_url?, timestamp, _session_id, _broadcast_scope,
_tenant_id}}``.  Each ``options[i]`` is a frozen dict of ``{kind, label,
slash_command, recommended?}``; ``kind`` is one of
:data:`PREVIEW_NEXT_STEP_KINDS`.  Frontend consumers MUST treat unknown
extra keys as forward-compatible (per Q.4 SSE policy) — a future W16.*
row can sprinkle ``schema_version`` / ``status_message`` without
bumping any contract here.

Three explicit goals:

* **Frontend renderer** consumes ``options[]`` to render four
  buttons with bilingual labels + the slash command each one runs.
* **W16.9 e2e** can pin the event name as a stable bucket key; the
  ``kind`` enum lets the test assert "all four options present".
* **Dismissal posture** — like every coach card, the operator MAY
  ignore it.  The next-step menu is purely a hint; the agent does
  not block on the operator's pick.

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  zero mutable module-level state — only frozen string
constants, frozen tuple :data:`PREVIEW_NEXT_STEP_KINDS`, frozen
:class:`PreviewNextStepOption` + :class:`PreviewNextStepsPayload`
dataclasses, typed :class:`PreviewNextStepsError` subclass, and stdlib
imports (``dataclasses`` / ``typing``).  Answer #1 — every uvicorn
worker reads the same constants from the same git checkout; the
emission is per-request, scoped via :func:`backend.events._resolve_scope`
which already enforces multi-worker delivery via Redis Pub/Sub when
configured.  No singleton, no in-memory cache, no shared mutable state.

Read-after-write timing audit (per SOP §2): N/A — pure projection from
caller-supplied workspace metadata to a wire dict, then fire-and-forget
``bus.publish``.  No DB pool, no compat→pool conversion, no
``asyncio.gather`` race surface.  ``preview.next_steps`` is intentionally
absent from :data:`backend.events._PERSIST_EVENT_TYPES` — the event is
bookended around the operator's chat session and replaying a stale
"what next?" prompt after the operator has already moved on would just
clutter the log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Frozen wire-shape constants ──────────────────────────────────────

#: SSE event type emitted by :func:`emit_preview_next_steps`.  Pinned
#: by drift guard so the frontend's event router can switch on a
#: string constant.
PREVIEW_NEXT_STEPS_EVENT_NAME: str = "preview.next_steps"

#: Pipeline phase string used when callers want to emit a sibling
#: :func:`backend.events.emit_pipeline_phase` (e.g. for the operator's
#: pipeline timeline UI).  Frozen so the snake_case style mirrors the
#: W14.* / W16.4 / W16.5 / W16.6 phases.
PREVIEW_NEXT_STEPS_PIPELINE_PHASE: str = "preview_next_steps"

#: Default broadcast scope when callers don't pass one explicitly.
#: ``"session"`` because the underlying preview URL is per-operator-
#: session (matches W16.4's :data:`PREVIEW_READY_DEFAULT_BROADCAST_SCOPE`
#: rationale: CF Access SSO gates ingress to the launching operator's
#: email, so broadcasting globally would expose a coaching card for a
#: workspace other tenants cannot open).
PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE: str = "session"

#: Default human-facing chat-message body when the caller does not
#: pass an explicit ``label``.  Frozen so the W16.9 e2e tests grep a
#: stable substring.
PREVIEW_NEXT_STEPS_DEFAULT_LABEL: str = "Preview is live — what next?"


# ── Next-step kind identifiers ──────────────────────────────────────

#: Push the rendered project to Vercel for a shareable preview URL
#: outside the W14 sandbox.  Recommended default — the operator's most
#: common ask after seeing the iframe is "make this URL shareable".
PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY: str = "vercel_deploy"

#: Run an accessibility scan over the preview surfacing WCAG
#: violations before the operator commits.
PREVIEW_NEXT_STEP_KIND_A11Y_SCAN: str = "a11y_scan"

#: Drop the rendered project into git, push, and open a PR so the
#: work survives the workspace.
PREVIEW_NEXT_STEP_KIND_COMMIT_PR: str = "commit_pr"

#: Keep iterating in chat — the operator may still want to refine the
#: design.  W16.5's edit-while-preview is the path.
PREVIEW_NEXT_STEP_KIND_CONTINUE_EDIT: str = "continue_edit"

#: Row-spec-ordered tuple of recognised next-step kinds.  Frontend
#: iterates over this for deterministic rendering; backend validators
#: gate ``options[i]["kind"]`` against it.
PREVIEW_NEXT_STEP_KINDS: tuple[str, ...] = (
    PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY,
    PREVIEW_NEXT_STEP_KIND_A11Y_SCAN,
    PREVIEW_NEXT_STEP_KIND_COMMIT_PR,
    PREVIEW_NEXT_STEP_KIND_CONTINUE_EDIT,
)


# ── Slash command literals ──────────────────────────────────────────

#: Slash command the FE renders for the Vercel-deploy option.  The
#: downstream router that consumes this is filed under W16.9 (e2e).
PREVIEW_NEXT_STEP_DEPLOY_SLASH_COMMAND: str = "/deploy-preview"

#: Slash command for the a11y-scan option.  Reuses the existing
#: ``/a11y-scan`` family (consumer-side router lives outside W16.7).
PREVIEW_NEXT_STEP_A11Y_SLASH_COMMAND: str = "/a11y-scan"

#: Slash command for the commit-+-PR option.  ``/commit-and-pr``
#: rather than ``/commit`` to keep the namespace self-describing.
PREVIEW_NEXT_STEP_COMMIT_PR_SLASH_COMMAND: str = "/commit-and-pr"

#: Slash command for the continue-edit option.  Reuses W16.5's
#: ``/edit-preview`` slash so the menu doesn't introduce a new verb;
#: the operator typing freeform "header 大一點" still triggers W16.5
#: detection — the slash is just the explicit form.
PREVIEW_NEXT_STEP_CONTINUE_EDIT_SLASH_COMMAND: str = "/edit-preview"

#: Default kind that gets the ``recommended`` flag when callers don't
#: override.  Vercel-deploy is the most common ask after preview live.
PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND: str = (
    PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY
)


# ── Bilingual labels ────────────────────────────────────────────────

#: Frozen bilingual labels for each kind.  Row-spec-pinned so the
#: W16.9 e2e tests can grep a stable substring.
PREVIEW_NEXT_STEP_LABELS: dict[str, str] = {
    PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY: "Vercel 部署 / Deploy to Vercel",
    PREVIEW_NEXT_STEP_KIND_A11Y_SCAN: "無障礙掃描 / a11y scan",
    PREVIEW_NEXT_STEP_KIND_COMMIT_PR: "Commit + PR / Create commit & PR",
    PREVIEW_NEXT_STEP_KIND_CONTINUE_EDIT: "繼續編輯 / Keep editing",
}


# ── Bound caps (defensive) ──────────────────────────────────────────

#: Hard cap on the workspace_id field — mirrors the W16.4 cap so a
#: payload that round-trips through both events validates with the
#: same byte budget.
MAX_PREVIEW_NEXT_STEPS_WORKSPACE_ID_BYTES: int = 256

#: Hard cap on the chat-message label — matches the W16.4 cap so the
#: rendered card stays visually consistent across all preview events.
MAX_PREVIEW_NEXT_STEPS_LABEL_BYTES: int = 120

#: Hard cap on each option's display label.
MAX_PREVIEW_NEXT_STEP_OPTION_LABEL_BYTES: int = 120

#: Hard cap on each option's slash-command string.  Slash commands
#: are short by convention; 256 bytes is more than generous.
MAX_PREVIEW_NEXT_STEP_SLASH_COMMAND_BYTES: int = 256

#: Hard cap on the preview-URL field in the payload.  Same reasoning
#: as W16.4's :data:`MAX_PREVIEW_READY_URL_BYTES`.
MAX_PREVIEW_NEXT_STEPS_URL_BYTES: int = 4096

#: Hard cap on the kind enum string.  Mirrors W16.5's
#: :data:`MAX_PREVIEW_HMR_RELOAD_CHANGE_KIND_BYTES` reasoning — long
#: enough for the longest enum member with 2× headroom.
MAX_PREVIEW_NEXT_STEP_KIND_BYTES: int = 32


# ── Public dataclasses ──────────────────────────────────────────────


class PreviewNextStepsError(ValueError):
    """Raised by :func:`build_preview_next_steps_payload` when a field
    violates the frozen contract (over-cap / non-string / empty
    workspace_id / unknown kind).  Subclasses :class:`ValueError` so
    callers that already except on bad input keep working unchanged.
    """


@dataclass(frozen=True)
class PreviewNextStepOption:
    """Frozen wire-shape for a single next-step option in the payload.

    Attributes
    ----------
    kind:
        One of :data:`PREVIEW_NEXT_STEP_KINDS`.  Frontend branches on
        this for icon / colour bucket; backend validators gate it.
    label:
        Bilingual human-facing display label.
    slash_command:
        The slash-command-shaped string the FE pre-fills when the
        operator clicks.  Includes the ``<workspace_id>`` argument so
        the consumer-side router has everything it needs.
    recommended:
        Whether this option is the agent's primary suggestion.  At
        most one option per payload should carry ``True``; the FE
        renders a ★ marker on the recommended row.
    """

    kind: str
    label: str
    slash_command: str
    recommended: bool = False

    def to_event_data(self) -> dict[str, Any]:
        """Project the dataclass into the SSE event ``data["options"]``
        dict.  ``recommended`` is dropped when ``False`` so the wire
        payload stays tight (the W16.9 e2e tests grep for absence of
        unset keys to detect drift).
        """
        data: dict[str, Any] = {
            "kind": self.kind,
            "label": self.label,
            "slash_command": self.slash_command,
        }
        if self.recommended:
            data["recommended"] = True
        return data


@dataclass(frozen=True)
class PreviewNextStepsPayload:
    """Frozen wire-shape for the ``preview.next_steps`` SSE event.

    Mirrors the dict that :func:`emit_preview_next_steps` publishes so
    callers (mostly tests) can construct the payload separately,
    inspect it, then hand it to :func:`emit_preview_next_steps` via
    :meth:`to_event_data`.

    Attributes
    ----------
    workspace_id:
        The W14 workspace id the preview belongs to.  Mandatory — the
        frontend uses this to scope the coach card to the right chat
        thread and thread the workspace_id into each slash command.
    label:
        Human-facing chat-message body.  Defaults to
        :data:`PREVIEW_NEXT_STEPS_DEFAULT_LABEL`; callers that want a
        per-launch hint pass a custom one.
    options:
        Tuple of :class:`PreviewNextStepOption` records — one per
        kind.  The frontend renders them in order; constructors pin
        the row-spec order to keep the menu deterministic.
    preview_url:
        Optional sandbox URL (mirrors the W16.4 ``preview.ready``
        payload) so the FE can deep-link the card without re-querying
        the workspace.  Useful for the operator's debug panel; the
        consumer does not depend on it for menu rendering.
    """

    workspace_id: str
    label: str = PREVIEW_NEXT_STEPS_DEFAULT_LABEL
    options: tuple[PreviewNextStepOption, ...] = field(default_factory=tuple)
    preview_url: str | None = None

    def to_event_data(self) -> dict[str, Any]:
        """Project the dataclass into the SSE event ``data`` dict.

        ``preview_url`` is dropped when ``None`` so the wire payload
        stays tight (the W16.9 e2e tests grep for absence of unset
        keys to detect drift).
        """
        data: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "label": self.label,
            "options": [opt.to_event_data() for opt in self.options],
        }
        if self.preview_url is not None:
            data["preview_url"] = self.preview_url
        return data


# ── Validators / builders ────────────────────────────────────────────


def _require_str(name: str, value: Any, *, max_bytes: int,
                 allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PreviewNextStepsError(
            f"{name} must be str, got {type(value).__name__}"
        )
    if not allow_empty and not value:
        raise PreviewNextStepsError(f"{name} must be non-empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise PreviewNextStepsError(
            f"{name} exceeds {max_bytes}-byte cap"
        )
    return value


def render_default_slash_command(kind: str, workspace_id: str) -> str:
    """Render the default slash-command string for *kind* threaded with
    *workspace_id*.

    Used by :func:`build_default_next_step_options` to pre-render the
    four canonical options.  Callers may pass a custom slash command
    via :func:`build_preview_next_step_option` directly when they want
    to override (e.g. surface a tenant-specific deploy target).

    Raises :class:`PreviewNextStepsError` when *kind* is not in
    :data:`PREVIEW_NEXT_STEP_KINDS`.
    """

    if kind == PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY:
        return (
            f"{PREVIEW_NEXT_STEP_DEPLOY_SLASH_COMMAND} "
            f"{workspace_id} --target=vercel"
        )
    if kind == PREVIEW_NEXT_STEP_KIND_A11Y_SCAN:
        return (
            f"{PREVIEW_NEXT_STEP_A11Y_SLASH_COMMAND} {workspace_id}"
        )
    if kind == PREVIEW_NEXT_STEP_KIND_COMMIT_PR:
        return (
            f"{PREVIEW_NEXT_STEP_COMMIT_PR_SLASH_COMMAND} {workspace_id}"
        )
    if kind == PREVIEW_NEXT_STEP_KIND_CONTINUE_EDIT:
        return (
            f"{PREVIEW_NEXT_STEP_CONTINUE_EDIT_SLASH_COMMAND} "
            f"{workspace_id}"
        )
    raise PreviewNextStepsError(
        f"unknown kind {kind!r}; expected one of "
        f"{sorted(PREVIEW_NEXT_STEP_KINDS)}"
    )


def build_preview_next_step_option(
    *,
    kind: str,
    workspace_id: str,
    label: str | None = None,
    slash_command: str | None = None,
    recommended: bool = False,
) -> PreviewNextStepOption:
    """Validate inputs and return a frozen
    :class:`PreviewNextStepOption`.

    Defaults to the row-spec bilingual label and the canonical slash
    command for *kind* threaded with *workspace_id*; callers override
    either via the kwargs.
    """

    if kind not in PREVIEW_NEXT_STEP_KINDS:
        raise PreviewNextStepsError(
            f"unknown kind {kind!r}; expected one of "
            f"{sorted(PREVIEW_NEXT_STEP_KINDS)}"
        )
    workspace_id = _require_str(
        "workspace_id", workspace_id,
        max_bytes=MAX_PREVIEW_NEXT_STEPS_WORKSPACE_ID_BYTES,
    )
    if label is None:
        label_value = PREVIEW_NEXT_STEP_LABELS[kind]
    else:
        label_value = _require_str(
            "label", label,
            max_bytes=MAX_PREVIEW_NEXT_STEP_OPTION_LABEL_BYTES,
        )
    if slash_command is None:
        slash_command_value = render_default_slash_command(kind, workspace_id)
    else:
        slash_command_value = _require_str(
            "slash_command", slash_command,
            max_bytes=MAX_PREVIEW_NEXT_STEP_SLASH_COMMAND_BYTES,
        )
        if not slash_command_value.startswith("/"):
            raise PreviewNextStepsError(
                f"slash_command must start with '/', got "
                f"{slash_command_value!r}"
            )
    if not isinstance(recommended, bool):
        raise PreviewNextStepsError("recommended must be bool")
    return PreviewNextStepOption(
        kind=kind,
        label=label_value,
        slash_command=slash_command_value,
        recommended=recommended,
    )


def build_default_next_step_options(
    workspace_id: str,
    *,
    recommended_kind: str | None = None,
) -> tuple[PreviewNextStepOption, ...]:
    """Build the canonical four-option tuple in row-spec order.

    Marks *recommended_kind* with the ``recommended`` flag (defaults
    to :data:`PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND`, ``vercel_deploy``).
    Pass ``recommended_kind=None`` explicitly to skip the marker
    entirely (e.g. when no option is the obvious primary suggestion).
    Pass an empty string or a custom kind otherwise.
    """

    if recommended_kind is None:
        recommended_kind = PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND
    if recommended_kind and recommended_kind not in PREVIEW_NEXT_STEP_KINDS:
        raise PreviewNextStepsError(
            f"unknown recommended_kind {recommended_kind!r}; expected "
            f"one of {sorted(PREVIEW_NEXT_STEP_KINDS)} or empty / None"
        )
    return tuple(
        build_preview_next_step_option(
            kind=k,
            workspace_id=workspace_id,
            recommended=(k == recommended_kind),
        )
        for k in PREVIEW_NEXT_STEP_KINDS
    )


def build_preview_next_steps_payload(
    *,
    workspace_id: str,
    label: str | None = None,
    options: tuple[PreviewNextStepOption, ...] | None = None,
    preview_url: str | None = None,
    recommended_kind: str | None = None,
) -> PreviewNextStepsPayload:
    """Validate inputs and return a frozen
    :class:`PreviewNextStepsPayload`.

    When *options* is omitted, builds the canonical four-option tuple
    via :func:`build_default_next_step_options`.  Callers that want a
    custom set (e.g. only commit+PR + continue edit because Vercel
    isn't configured) supply their own pre-built tuple.

    Raises :class:`PreviewNextStepsError` (a :class:`ValueError`
    subclass) on contract violations.  Validator is intentionally
    strict — :func:`emit_preview_next_steps` swallows nothing, so a
    bad call site surfaces in the operator's logs rather than
    silently dropping a coach card.
    """

    workspace_id = _require_str(
        "workspace_id", workspace_id,
        max_bytes=MAX_PREVIEW_NEXT_STEPS_WORKSPACE_ID_BYTES,
    )
    if label is None:
        label_value = PREVIEW_NEXT_STEPS_DEFAULT_LABEL
    else:
        label_value = _require_str(
            "label", label,
            max_bytes=MAX_PREVIEW_NEXT_STEPS_LABEL_BYTES,
            allow_empty=True,
        ) or PREVIEW_NEXT_STEPS_DEFAULT_LABEL
    if options is None:
        options_value = build_default_next_step_options(
            workspace_id, recommended_kind=recommended_kind,
        )
    else:
        if not isinstance(options, tuple):
            raise PreviewNextStepsError(
                f"options must be tuple, got {type(options).__name__}"
            )
        for opt in options:
            if not isinstance(opt, PreviewNextStepOption):
                raise PreviewNextStepsError(
                    "options must contain PreviewNextStepOption "
                    f"instances; got {type(opt).__name__}"
                )
            if opt.kind not in PREVIEW_NEXT_STEP_KINDS:
                raise PreviewNextStepsError(
                    f"option.kind {opt.kind!r} not in "
                    f"PREVIEW_NEXT_STEP_KINDS"
                )
        options_value = options
    if preview_url is not None:
        preview_url = _require_str(
            "preview_url", preview_url,
            max_bytes=MAX_PREVIEW_NEXT_STEPS_URL_BYTES,
        )
    return PreviewNextStepsPayload(
        workspace_id=workspace_id,
        label=label_value,
        options=options_value,
        preview_url=preview_url,
    )


def build_chat_message_for_preview_next_steps(
    payload: PreviewNextStepsPayload,
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Project a :class:`PreviewNextStepsPayload` into the chat-message
    shape the frontend ``WorkspaceChat`` consumes.

    The returned dict mirrors ``WorkspaceChatMessage`` (TypeScript) so
    the SSE consumer can ``messages = [...messages, msg]`` without any
    intermediate translator.  ``message_id`` is optional — when omitted
    the consumer is expected to mint one from ``crypto.randomUUID()``
    so server- and client-side ids don't collide.

    The ``previewNextSteps`` field is the W16.7-specific extension on
    ``WorkspaceChatMessage`` — the chat renderer in
    ``components/omnisight/workspace-chat.tsx`` switches on its
    presence to mount the four-option coach menu.
    """

    msg: dict[str, Any] = {
        "id": message_id or "",
        "role": "system",
        "text": payload.label,
        "previewNextSteps": {
            "workspaceId": payload.workspace_id,
            "label": payload.label,
            "options": [
                {
                    "kind": opt.kind,
                    "label": opt.label,
                    "slashCommand": opt.slash_command,
                    **({"recommended": True} if opt.recommended else {}),
                }
                for opt in payload.options
            ],
            **(
                {"previewUrl": payload.preview_url}
                if payload.preview_url is not None
                else {}
            ),
        },
    }
    return msg


# ── Emission ─────────────────────────────────────────────────────────


def emit_preview_next_steps(
    *,
    workspace_id: str,
    label: str | None = None,
    options: tuple[PreviewNextStepOption, ...] | None = None,
    preview_url: str | None = None,
    recommended_kind: str | None = None,
    session_id: str | None = None,
    broadcast_scope: str | None = None,
    tenant_id: str | None = None,
    **extra: Any,
) -> PreviewNextStepsPayload:
    """Publish a frozen ``preview.next_steps`` SSE event and return the
    validated :class:`PreviewNextStepsPayload`.

    Mirrors the kwargs shape of
    :func:`backend.web.web_preview_ready.emit_preview_ready` so the
    call site looks boringly identical to its W16.4 sibling.  The
    function is sync-safe (the underlying :class:`backend.events.EventBus`
    does not require an event loop for the local-fanout path) and
    never raises on transport failure — Redis Pub/Sub fallback /
    persistence skip / etc. are all best-effort inside
    :class:`EventBus.publish`.

    Returns the validated payload so the caller can stash it in the
    sandbox snapshot / audit row without re-validating.

    The ``broadcast_scope`` defaults to
    :data:`PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE` (``"session"``)
    — see the module docstring for the rationale.
    """

    payload = build_preview_next_steps_payload(
        workspace_id=workspace_id,
        label=label,
        options=options,
        preview_url=preview_url,
        recommended_kind=recommended_kind,
    )
    # Late import to avoid a circular when ``backend.events`` itself
    # later wants to import from ``backend.web``.
    from backend.events import _resolve_scope, _auto_tenant, bus, _log
    resolved_scope = _resolve_scope(
        "emit_preview_next_steps",
        broadcast_scope,
        PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE,
    )
    data: dict[str, Any] = payload.to_event_data()
    if extra:
        # Caller-provided extras take lowest priority — we never let
        # them clobber the frozen contract keys.
        for k, v in extra.items():
            data.setdefault(k, v)
    bus.publish(
        PREVIEW_NEXT_STEPS_EVENT_NAME,
        data,
        session_id=session_id,
        broadcast_scope=resolved_scope,
        tenant_id=_auto_tenant(tenant_id),
    )
    _log(
        f"[PREVIEW NEXT] {payload.workspace_id} → "
        f"{len(payload.options)} options coached",
    )
    return payload


# ── Drift guards (assert at module-import time) ──────────────────────

assert PREVIEW_NEXT_STEPS_EVENT_NAME == "preview.next_steps", (
    "PREVIEW_NEXT_STEPS_EVENT_NAME drift — frontend SSE consumer "
    "switches on this literal"
)

assert PREVIEW_NEXT_STEPS_PIPELINE_PHASE == "preview_next_steps", (
    "PREVIEW_NEXT_STEPS_PIPELINE_PHASE drift — pipeline-timeline UI "
    "switches on this literal"
)

assert PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE == "session", (
    "PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE must be 'session' — "
    "preview URLs are per-operator-session"
)

assert PREVIEW_NEXT_STEPS_DEFAULT_LABEL.strip(), (
    "PREVIEW_NEXT_STEPS_DEFAULT_LABEL cannot be empty / whitespace"
)

assert PREVIEW_NEXT_STEP_KINDS == (
    "vercel_deploy", "a11y_scan", "commit_pr", "continue_edit",
), (
    "PREVIEW_NEXT_STEP_KINDS drift — row-spec order locked: "
    "(a) Vercel deploy / (b) a11y scan / (c) commit+PR / (d) 繼續編輯"
)

assert len(set(PREVIEW_NEXT_STEP_KINDS)) == len(PREVIEW_NEXT_STEP_KINDS), (
    "PREVIEW_NEXT_STEP_KINDS must be unique"
)

assert PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND in PREVIEW_NEXT_STEP_KINDS, (
    "PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND must be one of "
    "PREVIEW_NEXT_STEP_KINDS"
)

assert set(PREVIEW_NEXT_STEP_LABELS.keys()) == set(PREVIEW_NEXT_STEP_KINDS), (
    "PREVIEW_NEXT_STEP_LABELS keys must exactly match "
    "PREVIEW_NEXT_STEP_KINDS — every kind needs a bilingual label"
)

# Each kind enum value must fit inside the per-field byte cap so a
# future emitter that picks the longest enum entry doesn't trip the
# validator.
assert all(
    len(k.encode("utf-8")) <= MAX_PREVIEW_NEXT_STEP_KIND_BYTES
    for k in PREVIEW_NEXT_STEP_KINDS
), (
    "PREVIEW_NEXT_STEP_KINDS contains an entry over "
    "MAX_PREVIEW_NEXT_STEP_KIND_BYTES"
)

# Every default slash command must start with "/" so the FE renderer
# can treat the field as opaque-but-clickable.
assert all(
    c.startswith("/") for c in (
        PREVIEW_NEXT_STEP_DEPLOY_SLASH_COMMAND,
        PREVIEW_NEXT_STEP_A11Y_SLASH_COMMAND,
        PREVIEW_NEXT_STEP_COMMIT_PR_SLASH_COMMAND,
        PREVIEW_NEXT_STEP_CONTINUE_EDIT_SLASH_COMMAND,
    )
), "Every PREVIEW_NEXT_STEP_*_SLASH_COMMAND must start with '/'"

assert issubclass(PreviewNextStepsError, ValueError), (
    "PreviewNextStepsError must subclass ValueError — back-compat for "
    "callers that except on bad input"
)


__all__ = [
    "MAX_PREVIEW_NEXT_STEPS_LABEL_BYTES",
    "MAX_PREVIEW_NEXT_STEPS_URL_BYTES",
    "MAX_PREVIEW_NEXT_STEPS_WORKSPACE_ID_BYTES",
    "MAX_PREVIEW_NEXT_STEP_KIND_BYTES",
    "MAX_PREVIEW_NEXT_STEP_OPTION_LABEL_BYTES",
    "MAX_PREVIEW_NEXT_STEP_SLASH_COMMAND_BYTES",
    "PREVIEW_NEXT_STEPS_DEFAULT_BROADCAST_SCOPE",
    "PREVIEW_NEXT_STEPS_DEFAULT_LABEL",
    "PREVIEW_NEXT_STEPS_EVENT_NAME",
    "PREVIEW_NEXT_STEPS_PIPELINE_PHASE",
    "PREVIEW_NEXT_STEP_A11Y_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_COMMIT_PR_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_CONTINUE_EDIT_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND",
    "PREVIEW_NEXT_STEP_DEPLOY_SLASH_COMMAND",
    "PREVIEW_NEXT_STEP_KINDS",
    "PREVIEW_NEXT_STEP_KIND_A11Y_SCAN",
    "PREVIEW_NEXT_STEP_KIND_COMMIT_PR",
    "PREVIEW_NEXT_STEP_KIND_CONTINUE_EDIT",
    "PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY",
    "PREVIEW_NEXT_STEP_LABELS",
    "PreviewNextStepOption",
    "PreviewNextStepsError",
    "PreviewNextStepsPayload",
    "build_chat_message_for_preview_next_steps",
    "build_default_next_step_options",
    "build_preview_next_step_option",
    "build_preview_next_steps_payload",
    "emit_preview_next_steps",
    "render_default_slash_command",
]
