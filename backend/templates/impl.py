"""BP.A.3 — ImplTemplate Pydantic schema (Coder Guild output).

The third of the four Blueprint-v2 templates. An ``ImplTemplate`` is the
contract emitted by the Coder Guild, downstream of the PM Guild's
``TaskTemplate`` (BP.A.2) and consumed by the Auditor Guild which
produces ``ReviewTemplate`` (BP.A.4). It pins exactly *what was built*
for a single Coder dispatch — the source payload, the proof it
compiled, the asymptotic-complexity claim, and the ABI target it was
compiled for.

Required fields (mirrors ``docs/design/blueprint-v2-implementation-plan.md``
Appendix B, lines 908-913):

  - ``schema_version``           — pinned to ``"1.0.0"`` so future
                                   revisions must add a discriminator
                                   instead of mutating in place.
  - ``source_code_payload``      — Whole source body the Coder Guild
                                   produced. Non-empty (a zero-byte
                                   payload is a contract violation —
                                   the Coder must emit something or
                                   raise, never return the empty
                                   string). Dialect-aware syntactic
                                   validation is left to BP.A.6
                                   ``template_validator.py``.
  - ``compiled_exit_code``       — ``Literal[0]``. The Blueprint v2
                                   appendix is explicit: "必須為 0".
                                   Pinning this at the schema layer
                                   makes it impossible for a Coder to
                                   emit a "compiled with errors"
                                   ImplTemplate at all — the
                                   ``ValidationError`` fires before
                                   the Auditor Guild ever sees it.
  - ``time_complexity``          — Big-O declaration. Enforced shape:
                                   leading ``O`` / ``o`` / ``Θ`` /
                                   ``θ`` / ``Ω`` / ``ω`` followed by
                                   a parenthesised body (so
                                   ``"fast"`` or ``"n^2"`` are
                                   rejected; ``"O(n log n)"`` /
                                   ``"O(n*log(n))"`` are accepted).
                                   Semantic comparison against the
                                   TaskTemplate hint is BP.A.6's job.
  - ``target_triple``            — Rust-style ``arch-vendor-os[-env]``.
                                   Same grammar as
                                   ``TaskTemplate.target_triple`` —
                                   the regex is intentionally
                                   redefined here (rather than
                                   imported from ``task.py``) so the
                                   two template modules stay
                                   independently importable. BP.A.6
                                   middleware cross-checks
                                   ``ImplTemplate.target_triple ==
                                   TaskTemplate.target_triple`` at
                                   dispatch time.

Cross-worker safety (SOP Step 1 強制問題 — module-global state audit):
this module declares no module-level mutable state, no singletons, no
in-memory cache. ``SCHEMA_VERSION``, the two compiled-regex
constants, and the class itself are all immutable values derived
from the same source on every worker — falls under SOP Step 1
acceptable answer #1 ("不共享，因為每 worker 從同樣來源推導出同樣的
值"). Safe under ``uvicorn --workers N`` by construction.

Cross-references:
  - BP.A.1 ``backend/templates/spec.py``     — SpecTemplate
  - BP.A.2 ``backend/templates/task.py``     — TaskTemplate (whose
    ``target_triple`` ImplTemplate must match)
  - BP.A.4 ``backend/templates/review.py``   — ReviewTemplate
  - BP.A.5 ``backend/cognitive_load.py``     — Cognitive Load Scanner
  - BP.A.6 ``backend/template_validator.py`` — FastAPI middleware that
    turns Pydantic ``ValidationError`` into the cognitive-penalty
    prompt and cross-checks ``target_triple`` alignment between
    sibling templates.
  - BP.A.7 ``backend/tests/test_templates.py`` — unified ~150-test
    suite that will fold a superset of these checks.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"

# ``arch-vendor-os`` (3 segments) or ``arch-vendor-os-env`` (4 segments).
# Each segment is alnum + ``_``; segments separated by single ``-``.
# Anchored on both ends so trailing whitespace already stripped by
# ``str_strip_whitespace`` cannot sneak past as part of a segment.
# Intentionally a verbatim copy of ``task.py``'s pattern — the two
# template modules stay independently importable; BP.A.7 will fold
# both into a shared regression that asserts the two patterns match.
_TARGET_TRIPLE_PATTERN = r"^[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+){2,3}$"
_TARGET_TRIPLE_RE = re.compile(_TARGET_TRIPLE_PATTERN)

# Big-O declaration shape: must start with O / o / Θ / θ / Ω / ω
# (Bachmann–Landau symbols), then a parenthesised body. Greedy ``.+``
# allows nested ``)`` — e.g. ``O(n*log(n))`` — as long as the string
# ends on a ``)``. Permissive on body content so schema does not
# reject legitimate exponentials, factorials, multi-variable forms.
_BIG_O_PATTERN = r"^[OoΘθΩω]\(.+\)$"
_BIG_O_RE = re.compile(_BIG_O_PATTERN)


class ImplTemplate(BaseModel):
    """Coder-Guild implementation contract. Frozen, JSON-serialisable."""

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
    source_code_payload: str = Field(
        ...,
        min_length=1,
        description=(
            "Whole source body the Coder Guild produced. Non-empty — "
            "the Coder must either emit something or raise, never "
            "return ``\"\"``. Dialect-aware syntactic validation is "
            "deferred to BP.A.6 template_validator."
        ),
    )
    compiled_exit_code: Literal[0] = Field(
        ...,
        description=(
            "Compiler exit code. Pinned to ``0`` at the schema layer — "
            "an ImplTemplate that did not compile cleanly cannot exist "
            "as a valid object. Blueprint v2 Appendix B: 必須為 0."
        ),
    )
    time_complexity: str = Field(
        ...,
        min_length=1,
        pattern=_BIG_O_PATTERN,
        description=(
            "Big-O / Big-Theta / Big-Omega declaration. Must start "
            "with one of ``O o Θ θ Ω ω`` followed by a parenthesised "
            "body. Examples: ``O(1)``, ``O(n log n)``, ``O(n*log(n))``, "
            "``Θ(n)``. Semantic comparison against the TaskTemplate "
            "hint is BP.A.6's responsibility."
        ),
    )
    target_triple: str = Field(
        ...,
        min_length=1,
        pattern=_TARGET_TRIPLE_PATTERN,
        description=(
            "Rust-style target triple ``arch-vendor-os[-env]``. Must "
            "match the sibling ``TaskTemplate.target_triple``; the "
            "cross-template equality check is enforced by BP.A.6 "
            "middleware (kept out of the schema layer so the two "
            "template modules stay independently importable)."
        ),
    )


__all__ = ["SCHEMA_VERSION", "ImplTemplate"]
