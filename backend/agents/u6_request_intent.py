"""U6-7 — request-local-intent run-state (INV-4, frozen design §2.A + §11 RB4b).

The NET-NEW run-state that distinguishes *request-local intent* (the human asked
for something in THIS request) from a *memory-sourced "standing policy"* (a
persistent-memory line claiming something is pre-approved). INV-4: sensitive
actions require explicit request-local intent OR an independent human approval;
a standing policy in memory can NEVER satisfy them. Before this module there was
NO field that could even represent the distinction — this is new substrate, not
a wiring-up.

Why memory can never mint this state (the structural argument, test-pinned):

  1. The ONLY constructor is :func:`declare_request_local_intent`, which
     requires a server-constructed, BOUND, **human** ``ExecutionContext`` — a
     rendered memory line is a string, and no code path parses text into an
     intent.
  2. The intent is bound to the context's ``request_id`` and satisfied ONLY for
     a context carrying that same id — it cannot outlive its request (nothing
     "standing" is representable) and cannot be replayed across requests.
  3. The run-state lives in a ContextVar set by the server entry point (the
     chat route) around the pipeline; model/tool/memory code inside the
     pipeline has no declare path (declaring requires the route's context
     object, and a non-human or unbound principal is refused).

Consumption in U6-7 is OBSERVATIONAL: the action guard's stage 1 computes
:func:`current_intent_satisfied` for the dispatch principal and carries it on
its outcome (``request_local_intent``) without changing any verdict — the
action layer can now *distinguish* the two cases on every dispatch. Enforcement
(denying a sensitive lane without intent) is a later, explicitly-flipped
increment in the T11 family; every mutating operation today already requires
the independent human-approval arm of INV-4, so the requirement itself is
never open in the meantime.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from backend.agents.execution_context import ExecutionContext, is_unbound

#: The CLOSED set of channels that may declare request-local intent. v1 has
#: exactly one: a live human turn. A memory/model/tool channel is deliberately
#: unrepresentable — extending this set is a reviewed boundary change.
INTENT_CHANNELS: frozenset[str] = frozenset({"user_turn"})


class RequestIntentError(Exception):
    """A request-intent invariant was violated — fail closed."""


@dataclass(frozen=True, slots=True)
class RequestLocalIntent:
    """Evidence that the CURRENT request's human turn is the intent source.

    Carries identifiers only — never content. Invalid instances are
    unrepresentable (``__post_init__`` validates and raises)."""

    request_id: str
    principal_type: str
    channel: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise RequestIntentError("request_id must be a non-empty str")
        if self.principal_type != "human":
            raise RequestIntentError("only a human principal can carry intent (v1)")
        if self.channel not in INTENT_CHANNELS:
            raise RequestIntentError(f"channel not in the closed set: {self.channel!r}")


def declare_request_local_intent(ctx: ExecutionContext) -> RequestLocalIntent:
    """THE only constructor path. Requires a server-constructed, bound, HUMAN
    principal with a non-empty ``request_id``; anything else is refused —
    a service/machine/unbound principal (and, a fortiori, any memory or model
    content) cannot declare intent."""
    if not isinstance(ctx, ExecutionContext):
        raise RequestIntentError("ctx must be a server-constructed ExecutionContext")
    if is_unbound(ctx):
        raise RequestIntentError("an unbound principal cannot declare intent")
    if ctx.principal_type != "human":
        raise RequestIntentError("only a human principal may declare intent (v1)")
    if not isinstance(ctx.request_id, str) or not ctx.request_id:
        raise RequestIntentError("ctx.request_id must be a non-empty str")
    return RequestLocalIntent(
        request_id=ctx.request_id, principal_type="human", channel="user_turn"
    )


def declare_for_human_turn_or_none(ctx: object) -> RequestLocalIntent | None:
    """Total helper for entry points: declare if the context qualifies, else
    ``None`` — NEVER raises (a route must not fail its turn over the intent
    layer; absence of intent is the fail-closed default everywhere)."""
    try:
        return declare_request_local_intent(ctx)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — total by contract; absence is fail-closed
        return None


def intent_satisfied(intent: object, ctx: object) -> bool:
    """Pure INV-4 predicate: does *intent* prove request-local intent for the
    principal *ctx* is acting as RIGHT NOW? True ONLY for a validated
    :class:`RequestLocalIntent` whose ``request_id`` equals the bound context's
    ``request_id``. Everything else — ``None``, a rendered memory string, a
    duck-typed lookalike, a stale intent from a previous request, an unbound
    context — is ``False``. Total: never raises."""
    try:
        if type(intent) is not RequestLocalIntent:
            return False
        if intent.channel not in INTENT_CHANNELS:
            return False
        if not isinstance(ctx, ExecutionContext) or is_unbound(ctx):
            return False
        if not isinstance(ctx.request_id, str) or not ctx.request_id:
            return False
        return intent.request_id == ctx.request_id
    except Exception:  # noqa: BLE001 — any doubt is the fail-closed False
        return False


# ── Run-state scope (ContextVar; same pattern as the provenance collector) ───

_active_request_intent: ContextVar[RequestLocalIntent | None] = ContextVar(
    "omnisight_active_request_intent", default=None
)


@contextmanager
def request_intent_scope(intent: RequestLocalIntent | None) -> Iterator[None]:
    """Bind *intent* as the active run-state for the enclosed request work.
    ``None`` is allowed (an intent-less scope — e.g. a channel that could not
    declare) and simply keeps the fail-closed default active. Nesting-safe and
    task-safe (token reset; child asyncio tasks copy the context)."""
    token = _active_request_intent.set(intent)
    try:
        yield
    finally:
        _active_request_intent.reset(token)


def active_request_intent() -> RequestLocalIntent | None:
    """The intent bound to the current execution context tree, if any."""
    return _active_request_intent.get()


def current_intent_satisfied(ctx: object) -> bool:
    """The guard-facing read: is the ACTIVE run-state a valid request-local
    intent for *ctx*? Total; ``False`` on any doubt."""
    try:
        return intent_satisfied(_active_request_intent.get(), ctx)
    except Exception:  # noqa: BLE001 — fail closed
        return False
