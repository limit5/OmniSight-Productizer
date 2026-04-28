"""W13.2 #XXX — Default breakpoints + custom-list resolver.

Pins the four production-grade breakpoints
(``mobile_375`` / ``tablet_768`` / ``desktop_1440`` / ``desktop_1920``)
into a single immutable :data:`DEFAULT_BREAKPOINTS` tuple and ships
:func:`resolve_breakpoints` — the policy layer that merges the defaults
with any caller-supplied custom :class:`backend.web.screenshot_capture.Viewport`
list before handing the final ordered tuple to
:meth:`backend.web.screenshot_capture.MultiContextScreenshotCapture.capture_multi`.

Why a separate module (not bolted onto the W13.1 engine)
--------------------------------------------------------
W13.1's engine deliberately stopped short of locking any default
viewport list — its docstring carries the receipt::

    The engine accepts *any* :class:`Viewport` list the caller supplies;
    defaults / custom-list policy belongs to the next row.

That separation matters: a future caller (e.g. a "diff a single PNG
against live preview" tool, or a one-off design-review capture at 4K)
should be able to call ``capture_multi`` with one bespoke viewport and
**never** drag the production-default policy into its surface area.
Putting the defaults into a sibling ``screenshot_breakpoints.py`` keeps
the engine reusable while giving the orchestrator one canonical place
to express the "375 / 768 / 1440 / 1920 plus the operator's extras" policy.

Why these four widths
---------------------
The four breakpoints map to the practical 2026-vintage device-class
floor in each viewport tier:

==================  ===========  =================================
``mobile_375``      375 × 812    iPhone 13 / 14 / 15 logical width
                                 (smallest mainstream non-mini iPhone;
                                 captures the "thinnest" mobile layout
                                 most production CSS still ships).
``tablet_768``      768 × 1024   iPad portrait (the "first column /
                                 second column" hinge most responsive
                                 grids flip at).
``desktop_1440``    1440 × 900   MacBook 13" / common laptop tier
                                 (the design-doc default in Figma /
                                 Sketch in 2026).
``desktop_1920``    1920 × 1080  Full HD desktop / external-monitor
                                 floor (the "is this design still
                                 honest at desktop scale" check).
==================  ===========  =================================

All four pin :attr:`Viewport.is_mobile` ``False``. Width alone steers
production CSS in 99 % of cases (W13.1 docstring carries the rationale);
toggling Playwright's mobile-emulation also flips touch events + UA
string, which surfaces *different* responsive paths than the operator
typically wants from a "what does my page look like at 375 px" capture.
Operators who need true mobile-emulation (touch / UA switch) can add a
custom :class:`Viewport` with ``is_mobile=True`` via the
``custom_viewports`` arg — the engine is uniform either way.

Module-global state audit (SOP §1)
----------------------------------
This module owns no module-level **mutable** state. The four named
constants (:data:`BREAKPOINT_MOBILE_375` / :data:`BREAKPOINT_TABLET_768`
/ :data:`BREAKPOINT_DESKTOP_1440` / :data:`BREAKPOINT_DESKTOP_1920`)
plus :data:`DEFAULT_BREAKPOINTS` are :class:`Viewport` instances —
``frozen=True`` dataclasses, immutable post-construction. Cross-worker
consistency falls under SOP answer #1 (each ``uvicorn`` worker derives
the same constants from the same source).

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A — pure-data module + a pure-function resolver, no DB, no shared
in-memory state, no concurrency surface.

Compat fingerprint grep (SOP §3)
--------------------------------
Clean — no SQL, no DB, no asyncpg pool / aiosqlite_compat references.

Scope (this row only)
---------------------
* Lock the four production breakpoints into a public, immutable tuple.
* Ship the resolver that merges defaults + custom viewports while
  enforcing name uniqueness (so W13.3's
  ``.omnisight/refs/{breakpoint}.png`` writer never sees a collision).
* Expose ``DEFAULT_BREAKPOINT_NAMES`` so docs / drift-guard tests can
  pin the exact name set without re-extracting from the tuple.

This row deliberately stops short of:

* W13.3 — ``.omnisight/refs/{name}.png`` + ``manifest.json`` writer.
  This module returns viewport specs; bytes-to-disk lives in W13.3.
* W13.4 — ghost-overlay diff against W14 live preview.
* W13.5 — the 5-URL × 4-breakpoint integration matrix.
"""

from __future__ import annotations

from typing import Optional, Sequence

from backend.web.screenshot_capture import (
    ScreenshotConfigError,
    Viewport,
)


# ── The four production breakpoints ───────────────────────────────────

#: Mobile floor — iPhone 13 / 14 / 15 logical width. ``is_mobile=False``
#: by design: width alone carries the responsive-CSS signal in nearly
#: every production stack we target, and flipping mobile emulation
#: changes the UA string + touch events which surfaces a *different*
#: code path than "my desktop browser sized down to 375 px" typically
#: wants. Callers needing true touch / mobile-UA emulation pass an
#: explicit :class:`Viewport` with ``is_mobile=True`` via the resolver's
#: ``custom_viewports``.
BREAKPOINT_MOBILE_375: Viewport = Viewport(
    name="mobile_375",
    width=375,
    height=812,
)

#: Tablet portrait — iPad logical width. The grid-flip hinge most
#: responsive layouts target (one-column → two-column).
BREAKPOINT_TABLET_768: Viewport = Viewport(
    name="tablet_768",
    width=768,
    height=1024,
)

#: Laptop / "design-doc default" — MacBook-13"-class. The width Figma /
#: Sketch designs are typically authored at, so the capture matches the
#: source design canvas before any responsive degradation.
BREAKPOINT_DESKTOP_1440: Viewport = Viewport(
    name="desktop_1440",
    width=1440,
    height=900,
)

#: Full HD desktop floor — external monitor / common Windows / Linux
#: desktop class. The "is this still honest at full desktop" check that
#: catches white-space-overflow / image-stretching regressions hidden at
#: 1440 px.
BREAKPOINT_DESKTOP_1920: Viewport = Viewport(
    name="desktop_1920",
    width=1920,
    height=1080,
)


#: Canonical ordered tuple of the four production breakpoints. **Order
#: matters** — small-to-large width is also the order W13.4 / W13.5
#: ghost-overlay diff and reference matrix iterate, so a deterministic
#: ordering keeps the downstream artefacts byte-stable.
DEFAULT_BREAKPOINTS: tuple[Viewport, ...] = (
    BREAKPOINT_MOBILE_375,
    BREAKPOINT_TABLET_768,
    BREAKPOINT_DESKTOP_1440,
    BREAKPOINT_DESKTOP_1920,
)


#: Name set of the four production breakpoints, in the same order as
#: :data:`DEFAULT_BREAKPOINTS`. Pinned separately so docs / drift-guard
#: tests can assert against the exact name list without re-extracting it
#: from the tuple every time. Any future re-name (e.g. ``mobile_375`` →
#: ``phone_375``) MUST surface as a CI red here, since W13.3's filenames
#: + W13.4's ghost-overlay keys read from this set.
DEFAULT_BREAKPOINT_NAMES: tuple[str, ...] = tuple(
    vp.name for vp in DEFAULT_BREAKPOINTS
)


# ── Resolver ──────────────────────────────────────────────────────────

def resolve_breakpoints(
    custom_viewports: Optional[Sequence[Viewport]] = None,
    *,
    include_defaults: bool = True,
) -> tuple[Viewport, ...]:
    """Resolve the final ordered viewport list for a multi-breakpoint capture.

    The orchestration policy: emit the four production defaults first,
    then any caller-supplied custom viewports — in input order, dedup
    enforced — to give callers a "defaults plus extras" knob without
    having to retype the four-row block every site.

    Args:
        custom_viewports: Ordered sequence of additional
            :class:`Viewport` instances. Pass ``None`` (or omit) for the
            common "just the four defaults" case. Pass a non-empty
            sequence to add extras (e.g. a 4K-class
            ``Viewport(name="ultrawide_3840", width=3840, height=1600)``).
            Each must already be a valid :class:`Viewport` — the
            resolver does not re-validate field-level shape, that's
            the dataclass's `__post_init__` job. The resolver only
            polices **inter-viewport** invariants (uniqueness, override
            behaviour).
        include_defaults: When ``True`` (default), the four production
            breakpoints lead the returned tuple. When ``False``, the
            resolver returns *only* ``custom_viewports`` — the escape
            hatch for one-off captures that should NOT include the
            production defaults (e.g. "design review at 4K only").
            ``include_defaults=False`` requires a non-empty
            ``custom_viewports`` — the engine refuses an empty viewport
            list anyway, but failing here gives a more specific error.

    Returns:
        Ordered tuple of :class:`Viewport`. When
        ``include_defaults=True`` and ``custom_viewports`` is empty /
        ``None``, the return value is :data:`DEFAULT_BREAKPOINTS`
        verbatim (same object, not a copy — the tuple is immutable so
        identity sharing is safe and cheap).

    Raises:
        ScreenshotConfigError: when (a) ``include_defaults=False`` and
            ``custom_viewports`` is empty / ``None`` (no viewports
            survived); (b) a ``custom_viewports`` entry isn't a
            :class:`Viewport` instance; (c) a ``custom_viewports`` name
            collides with another custom entry; (d) a ``custom_viewports``
            name collides with one of the four production-default names
            (the operator wanted to override a default — see "How to
            override a default" below).

    How to override a default
    -------------------------
    Names must be globally unique because W13.3 will write
    ``.omnisight/refs/{name}.png`` — a name collision would silently
    overwrite one capture with another. The resolver enforces this here
    where the caller still has the input in hand. Two patterns:

    * **Add a new size next to the defaults** — pick a fresh name
      (e.g. ``ultrawide_3840``) and pass it in ``custom_viewports``.
    * **Replace a default** — set ``include_defaults=False`` and supply
      the full bespoke list (the operator's caller now owns the policy).

    A future row can add a finer-grained "override these N defaults
    with these N replacements" knob, but the two patterns above cover
    every case the W13 epic plans for. Keep the resolver narrow.
    """
    customs: tuple[Viewport, ...] = (
        tuple(custom_viewports) if custom_viewports is not None else ()
    )

    # Validate custom entries' types eagerly — the engine would catch
    # this on capture_multi, but failing here surfaces the bug before
    # we even spin a browser.
    for vp in customs:
        if not isinstance(vp, Viewport):
            raise ScreenshotConfigError(
                f"custom_viewports entries must be Viewport instances, "
                f"got {type(vp).__name__}"
            )

    # Reject duplicate names within custom_viewports up-front.
    seen_custom: set[str] = set()
    for vp in customs:
        if vp.name in seen_custom:
            raise ScreenshotConfigError(
                f"custom_viewports contains duplicate name {vp.name!r}"
            )
        seen_custom.add(vp.name)

    if include_defaults:
        # Reject any custom name colliding with a production-default
        # name. W13.3's writer addresses files by viewport name; a
        # collision would silently overwrite. Force the operator to
        # pick a new name, or to flip ``include_defaults=False`` if
        # they actually want to replace the default outright.
        default_names = set(DEFAULT_BREAKPOINT_NAMES)
        clashes = sorted(default_names & seen_custom)
        if clashes:
            raise ScreenshotConfigError(
                f"custom_viewports name(s) {clashes!r} collide with the "
                f"production-default breakpoints; pick a different name "
                f"or pass include_defaults=False to replace the defaults "
                f"outright"
            )
        if not customs:
            # Hot path — return the canonical tuple as-is (same object).
            return DEFAULT_BREAKPOINTS
        return DEFAULT_BREAKPOINTS + customs

    # include_defaults=False — caller wants only the bespoke list.
    if not customs:
        raise ScreenshotConfigError(
            "resolve_breakpoints requires at least one custom viewport "
            "when include_defaults=False"
        )
    return customs


__all__ = [
    "BREAKPOINT_DESKTOP_1440",
    "BREAKPOINT_DESKTOP_1920",
    "BREAKPOINT_MOBILE_375",
    "BREAKPOINT_TABLET_768",
    "DEFAULT_BREAKPOINTS",
    "DEFAULT_BREAKPOINT_NAMES",
    "resolve_breakpoints",
]
