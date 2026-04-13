"""Phase 47C — Ambiguity handling.

When an agent / planner encounters a decision point it cannot confidently
resolve (multiple equally-valid implementations, conflicting tool results,
unclear user intent), call `ambiguity.propose_options()`. It funnels through
the DecisionEngine:

    - Manual / Supervised: queued with a timeout; the 47D background loop
      will auto-select `safe_default_id` once `deadline_at` passes.
    - Full Auto / Turbo: auto-executes immediately with the safest option.

The "safest" option is whichever the caller tagged as `is_safe_default=True`,
falling back to the first option. This keeps the safety heuristic in the
caller's hands — the module only enforces the plumbing.
"""

from __future__ import annotations

from typing import Any

from backend import decision_engine as de


DEFAULT_AMBIGUITY_TIMEOUT_S = 90.0


def propose_options(
    kind: str,
    title: str,
    options: list[dict[str, Any]],
    *,
    detail: str = "",
    severity: de.DecisionSeverity | str = de.DecisionSeverity.routine,
    timeout_s: float = DEFAULT_AMBIGUITY_TIMEOUT_S,
    source: dict[str, Any] | None = None,
) -> de.Decision:
    """Register an ambiguous choice.

    Each option dict should carry at minimum::

        {"id": "opt_a", "label": "Use library X", "description": "..."}

    Optionally mark one as `is_safe_default: True` — that option becomes
    the default and the timeout-fallback pick.
    """
    if not options:
        raise ValueError("ambiguity.propose_options: options must be non-empty")

    # Validate shape & dedupe ids
    seen: set[str] = set()
    for opt in options:
        oid = opt.get("id")
        if not oid or not isinstance(oid, str):
            raise ValueError("each option needs a non-empty string 'id'")
        if oid in seen:
            raise ValueError(f"duplicate option id: {oid}")
        seen.add(oid)

    default_id = next(
        (o["id"] for o in options if o.get("is_safe_default")),
        options[0]["id"],
    )

    return de.propose(
        kind=f"ambiguity/{kind}",
        title=title,
        detail=detail,
        options=options,
        default_option_id=default_id,
        severity=severity,
        timeout_s=timeout_s,
        source=dict(source or {}),
    )
