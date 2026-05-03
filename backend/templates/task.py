"""BP.A.2 — TaskTemplate Pydantic schema (PM Guild output).

The second of the four Blueprint-v2 templates. A ``TaskTemplate`` is the
contract emitted by the PM Guild, downstream of the Architect Guild's
``SpecTemplate`` (BP.A.1) and consumed by the Coder Guild which produces
``ImplTemplate`` (BP.A.3). It pins exactly *what* a single Coder
dispatch is allowed to touch — ABI target, dependency whitelist,
cognitive-load ceiling, owning Guild, and size class.

Required fields (mirrors ``docs/design/blueprint-v2-implementation-plan.md``
Appendix B, lines 900-906):

  - ``schema_version``             — pinned to ``"1.0.0"`` so future
                                     revisions must add a discriminator
                                     instead of mutating in place.
  - ``target_triple``              — Rust-style ``arch-vendor-os[-env]``
                                     triple; ``x86_64-pc-linux-gnu`` /
                                     ``aarch64-vendor-linux``. Format is
                                     enforced at the schema layer so a
                                     Coder dispatch can fan out to the
                                     correct toolchain without a
                                     downstream regex re-check.
  - ``allowed_dependencies``       — Whitelist of contract files /
                                     module paths the Coder Guild is
                                     permitted to read (see Blueprint v2
                                     §3 "Coder 唯一可讀的合約檔"). May
                                     be empty for trivial tasks; each
                                     entry must be a non-empty stripped
                                     string.
  - ``max_cognitive_load_tokens``  — Hard ceiling. Cognitive Load
                                     Scanner (BP.A.5) compares its
                                     measured value against this; if
                                     measured > ceiling the task is
                                     bounced back to PM Guild for
                                     re-decomposition. Must be ``> 0``.
  - ``guild_id``                   — Owning Guild identifier (one of the
                                     21 guilds enumerated in BP.B). The
                                     value is validated as a non-empty
                                     stripped string here; the
                                     Phase-B-and-later guild registry
                                     will enforce the actual enum at
                                     ``template_validator.py`` layer
                                     (BP.A.6) so this schema does not
                                     have to re-ship every time the
                                     Guild list grows.
  - ``size``                       — ``Literal["S", "M", "XL"]``.

Cross-worker safety (SOP Step 1 強制問題 — module-global state audit):
this module declares no module-level mutable state, no singletons, no
in-memory cache. Every ``TaskTemplate`` is a frozen plain Pydantic
value object — safe under ``uvicorn --workers N`` because each worker
constructs its own instances from the same JSON payload. Falls under
SOP Step 1 acceptable answer #1 ("不共享，因為每 worker 從同樣來源推導
出同樣的值").

Cross-references:
  - BP.A.1 ``backend/templates/spec.py``     — SpecTemplate
  - BP.A.3 ``backend/templates/impl.py``     — ImplTemplate (will reuse
    the same target_triple for cross-template alignment)
  - BP.A.4 ``backend/templates/review.py``   — ReviewTemplate
  - BP.A.5 ``backend/cognitive_load.py``     — Cognitive Load Scanner
    that consumes ``max_cognitive_load_tokens`` as the hard ceiling.
  - BP.A.6 ``backend/template_validator.py`` — FastAPI middleware that
    turns Pydantic ``ValidationError`` into the cognitive-penalty
    prompt and (later) cross-references ``guild_id`` against the live
    Guild registry.
  - BP.A.7 ``backend/tests/test_templates.py`` — unified ~150-test
    suite that will fold a superset of these checks.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"

# ``arch-vendor-os`` (3 segments) or ``arch-vendor-os-env`` (4 segments).
# Each segment is alnum + ``_``; segments separated by single ``-``.
# Anchored on both ends so trailing whitespace already stripped by
# ``str_strip_whitespace`` cannot sneak past as part of a segment.
_TARGET_TRIPLE_PATTERN = r"^[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+){2,3}$"
_TARGET_TRIPLE_RE = re.compile(_TARGET_TRIPLE_PATTERN)


NonEmptyStr = Annotated[
    str, StringConstraints(min_length=1, strip_whitespace=True)
]


class TaskTemplate(BaseModel):
    """PM-Guild task contract. Frozen, JSON-serialisable."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    schema_version: Literal["1.0.0"] = Field(
        default=SCHEMA_VERSION,
        description=(
            "Pinned schema version. Bump via discriminated union, "
            "never in-place."
        ),
    )
    target_triple: str = Field(
        ...,
        min_length=1,
        pattern=_TARGET_TRIPLE_PATTERN,
        description=(
            "Rust-style target triple ``arch-vendor-os[-env]``. "
            "Examples: ``x86_64-pc-linux-gnu``, ``aarch64-vendor-linux``. "
            "Pinned at the schema layer so the Coder dispatcher can "
            "fan-out to the correct toolchain without a downstream "
            "regex re-check."
        ),
    )
    allowed_dependencies: list[NonEmptyStr] = Field(
        ...,
        description=(
            "Whitelist of contract files / module paths the Coder "
            "Guild is permitted to read. May be an empty list for "
            "trivial tasks; each entry is enforced non-empty so a "
            "stray ``\"\"`` cannot accidentally widen the contract."
        ),
    )
    max_cognitive_load_tokens: int = Field(
        ...,
        gt=0,
        description=(
            "Hard ceiling consumed by the Cognitive Load Scanner "
            "(BP.A.5). If the measured load exceeds this value the "
            "task is bounced back to PM Guild for re-decomposition. "
            "Must be ``> 0``."
        ),
    )
    guild_id: NonEmptyStr = Field(
        ...,
        description=(
            "Owning Guild identifier (one of the 21 guilds enumerated "
            "in BP.B). The actual enum is validated at the "
            "template_validator middleware (BP.A.6) so this schema "
            "does not have to re-ship every time the Guild list grows."
        ),
    )
    size: Literal["S", "M", "XL"] = Field(
        ...,
        description=(
            "Coarse size class. ``S`` ≈ single-file change, ``M`` ≈ "
            "multi-file but single module, ``XL`` ≈ cross-module — "
            "feeds the dispatcher's parallelism heuristic."
        ),
    )


__all__ = ["SCHEMA_VERSION", "TaskTemplate"]
