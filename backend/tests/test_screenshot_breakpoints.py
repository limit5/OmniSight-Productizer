"""W13.2 #XXX — Unit tests for ``backend.web.screenshot_breakpoints``.

Pins the policy layer that sits on top of the W13.1 multi-context
engine:

* The four production breakpoints (``mobile_375`` / ``tablet_768`` /
  ``desktop_1440`` / ``desktop_1920``) — exact widths / heights / order
  / immutability.
* :func:`resolve_breakpoints` defaults-plus-custom merge semantics —
  order, dedup, name-collision rejection, ``include_defaults=False``
  override path.
* Drift guards — names match the values W13.3 will use to write
  ``.omnisight/refs/{name}.png`` (filename-safe alphabet),
  :data:`DEFAULT_BREAKPOINT_NAMES` matches :data:`DEFAULT_BREAKPOINTS`,
  re-exports through ``backend.web`` package surface.

No real Playwright / browser / network — the resolver is pure-data.
"""

from __future__ import annotations

import pytest

from backend.web.screenshot_breakpoints import (
    BREAKPOINT_DESKTOP_1440,
    BREAKPOINT_DESKTOP_1920,
    BREAKPOINT_MOBILE_375,
    BREAKPOINT_TABLET_768,
    DEFAULT_BREAKPOINT_NAMES,
    DEFAULT_BREAKPOINTS,
    resolve_breakpoints,
)
from backend.web.screenshot_capture import ScreenshotConfigError, Viewport


# ── DEFAULT_BREAKPOINTS contract ─────────────────────────────────────

def test_default_breakpoints_count_is_four():
    # The W13.2 row in TODO.md says "4 預設斷點" — pin the count so a
    # future "let's add 4K to the defaults" change has to also update
    # this assertion + TODO row + HANDOFF.
    assert len(DEFAULT_BREAKPOINTS) == 4


def test_default_breakpoints_is_tuple_immutable():
    # tuple, not list — defaults are public + worker-shared, must not
    # be mutated in place by a downstream caller.
    assert isinstance(DEFAULT_BREAKPOINTS, tuple)
    assert all(isinstance(vp, Viewport) for vp in DEFAULT_BREAKPOINTS)


def test_default_breakpoints_order_is_small_to_large_width():
    # Ordering is a contract — W13.4 ghost overlay + W13.5 reference
    # matrix iterate in this order, so byte-stable downstream artefacts
    # depend on it.
    widths = [vp.width for vp in DEFAULT_BREAKPOINTS]
    assert widths == sorted(widths)
    assert widths == [375, 768, 1440, 1920]


def test_default_breakpoint_names_pinned():
    # Pin the exact names — W13.3 will write
    # ``.omnisight/refs/{name}.png`` so any rename here is a public
    # API break + has to surface in CI red.
    names = tuple(vp.name for vp in DEFAULT_BREAKPOINTS)
    assert names == ("mobile_375", "tablet_768", "desktop_1440", "desktop_1920")


def test_default_breakpoint_names_constant_matches_tuple():
    # Drift guard: DEFAULT_BREAKPOINT_NAMES is published as a separate
    # constant so docs / tests can pin the name set without re-extracting
    # it. It must stay in sync with DEFAULT_BREAKPOINTS — assert here
    # so a future change to one without the other fails CI.
    assert DEFAULT_BREAKPOINT_NAMES == tuple(
        vp.name for vp in DEFAULT_BREAKPOINTS
    )


def test_default_breakpoint_names_unique():
    assert len(set(DEFAULT_BREAKPOINT_NAMES)) == len(DEFAULT_BREAKPOINT_NAMES)


def test_default_breakpoint_names_filename_safe():
    # Lowercase + digits + - / _ only — same alphabet Viewport's
    # __post_init__ enforces. W13.3 will write
    # .omnisight/refs/{name}.png; an unsafe char here would either
    # collide on case-insensitive FS (macOS / Windows) or refuse to
    # serialise.
    for name in DEFAULT_BREAKPOINT_NAMES:
        assert name
        assert all(
            (ch.islower() and ch.isascii()) or ch.isdigit() or ch in ("-", "_")
            for ch in name
        ), f"name {name!r} contains a non-filename-safe character"


def test_default_breakpoints_individual_constants_match_tuple():
    # The four named constants and the tuple must be the same objects —
    # callers may grab one named constant directly + expect identity
    # equality with the tuple element (e.g. for membership tests).
    assert DEFAULT_BREAKPOINTS[0] is BREAKPOINT_MOBILE_375
    assert DEFAULT_BREAKPOINTS[1] is BREAKPOINT_TABLET_768
    assert DEFAULT_BREAKPOINTS[2] is BREAKPOINT_DESKTOP_1440
    assert DEFAULT_BREAKPOINTS[3] is BREAKPOINT_DESKTOP_1920


@pytest.mark.parametrize(
    "vp,expected_width,expected_height",
    [
        (BREAKPOINT_MOBILE_375, 375, 812),
        (BREAKPOINT_TABLET_768, 768, 1024),
        (BREAKPOINT_DESKTOP_1440, 1440, 900),
        (BREAKPOINT_DESKTOP_1920, 1920, 1080),
    ],
)
def test_default_breakpoint_dimensions_pinned(vp, expected_width, expected_height):
    assert vp.width == expected_width
    assert vp.height == expected_height


def test_default_breakpoints_default_dsf_is_one():
    # All four defaults at DSF=1.0. Operators that need 2x retina
    # captures pass a custom Viewport. Pinned so a future "let's bump
    # everything to 2x" change has to also update tests.
    for vp in DEFAULT_BREAKPOINTS:
        assert vp.device_scale_factor == 1.0


def test_default_breakpoints_is_mobile_false():
    # The W13.1 docstring carries the rationale: width alone steers
    # production CSS, mobile=True flips UA + touch which surfaces a
    # different code path than callers usually want from the four
    # production breakpoints. Operators needing true mobile-emulation
    # add a custom Viewport with is_mobile=True via the resolver.
    for vp in DEFAULT_BREAKPOINTS:
        assert vp.is_mobile is False


def test_default_breakpoints_are_frozen():
    # Viewport is frozen=True; a future change that drops `frozen` would
    # let a downstream caller mutate the shared production defaults —
    # would silently break every concurrent worker. Lock it here.
    with pytest.raises(Exception):
        BREAKPOINT_MOBILE_375.width = 999  # type: ignore[misc]


# ── resolve_breakpoints — happy paths ────────────────────────────────

def test_resolve_no_args_returns_defaults_object_identity():
    # Hot path — same object, not a copy. The tuple is immutable so
    # identity sharing is safe + cheap.
    assert resolve_breakpoints() is DEFAULT_BREAKPOINTS


def test_resolve_none_returns_defaults_object_identity():
    assert resolve_breakpoints(None) is DEFAULT_BREAKPOINTS


def test_resolve_empty_list_returns_defaults():
    # Empty custom list ≡ "just defaults". Identity sharing is fine.
    assert resolve_breakpoints([]) is DEFAULT_BREAKPOINTS
    assert resolve_breakpoints(()) is DEFAULT_BREAKPOINTS


def test_resolve_with_custom_appends_after_defaults():
    extra = Viewport(name="ultrawide_3840", width=3840, height=1600)
    result = resolve_breakpoints([extra])
    assert len(result) == 5
    assert result[:4] == DEFAULT_BREAKPOINTS
    assert result[4] is extra


def test_resolve_preserves_custom_input_order():
    # Caller-supplied order is preserved verbatim — any reordering
    # would break determinism for W13.5's reference matrix.
    a = Viewport(name="extra_a", width=320, height=480)
    b = Viewport(name="extra_b", width=2560, height=1440)
    c = Viewport(name="extra_c", width=3840, height=2160)
    result = resolve_breakpoints([c, a, b])  # intentionally unsorted
    assert result[4:] == (c, a, b)


def test_resolve_returns_tuple():
    result = resolve_breakpoints([Viewport(name="x", width=320, height=480)])
    assert isinstance(result, tuple)


def test_resolve_accepts_sequence_protocol():
    # tuple input also accepted — Sequence type hint covers both.
    extra = Viewport(name="ultrawide_3840", width=3840, height=1600)
    result = resolve_breakpoints((extra,))
    assert result[-1] is extra


# ── resolve_breakpoints — include_defaults=False ─────────────────────

def test_resolve_without_defaults_returns_only_custom():
    a = Viewport(name="only_a", width=320, height=480)
    b = Viewport(name="only_b", width=480, height=640)
    result = resolve_breakpoints([a, b], include_defaults=False)
    assert result == (a, b)


def test_resolve_without_defaults_preserves_order():
    a = Viewport(name="first", width=320, height=480)
    b = Viewport(name="second", width=2560, height=1440)
    result = resolve_breakpoints([b, a], include_defaults=False)
    assert result == (b, a)


def test_resolve_without_defaults_empty_custom_raises():
    # When the operator opted out of defaults, an empty custom list
    # would yield an empty viewport tuple — the engine refuses that
    # downstream anyway, but we want the error to point at the policy
    # layer with a specific message.
    with pytest.raises(ScreenshotConfigError) as exc:
        resolve_breakpoints([], include_defaults=False)
    assert "include_defaults=False" in str(exc.value)


def test_resolve_without_defaults_none_custom_raises():
    with pytest.raises(ScreenshotConfigError):
        resolve_breakpoints(None, include_defaults=False)


def test_resolve_without_defaults_lets_caller_use_default_names_outright():
    # When include_defaults=False, name-vs-default-default collision
    # is irrelevant (the defaults aren't in the result), so an
    # operator wanting a bespoke "mobile_375" capture (e.g. with
    # is_mobile=True) can pass it through unchanged.
    bespoke = Viewport(
        name="mobile_375", width=375, height=812, is_mobile=True
    )
    result = resolve_breakpoints([bespoke], include_defaults=False)
    assert result == (bespoke,)
    assert result[0].is_mobile is True


# ── resolve_breakpoints — error paths ────────────────────────────────

def test_resolve_rejects_non_viewport_in_custom():
    with pytest.raises(ScreenshotConfigError) as exc:
        resolve_breakpoints([{"name": "x", "width": 320, "height": 480}])  # type: ignore[list-item]
    assert "Viewport" in str(exc.value)


def test_resolve_rejects_string_in_custom():
    with pytest.raises(ScreenshotConfigError):
        resolve_breakpoints(["mobile_375"])  # type: ignore[list-item]


def test_resolve_rejects_duplicate_names_in_custom():
    a = Viewport(name="dup", width=320, height=480)
    b = Viewport(name="dup", width=2560, height=1440)
    with pytest.raises(ScreenshotConfigError) as exc:
        resolve_breakpoints([a, b])
    assert "duplicate" in str(exc.value).lower()


def test_resolve_rejects_duplicate_in_custom_under_include_defaults_false():
    # Same uniqueness rule applies even when defaults excluded — W13.3
    # writer collision is the same hazard either way.
    a = Viewport(name="dup", width=320, height=480)
    b = Viewport(name="dup", width=2560, height=1440)
    with pytest.raises(ScreenshotConfigError):
        resolve_breakpoints([a, b], include_defaults=False)


@pytest.mark.parametrize(
    "default_name",
    ["mobile_375", "tablet_768", "desktop_1440", "desktop_1920"],
)
def test_resolve_rejects_custom_name_colliding_with_default(default_name):
    custom = Viewport(name=default_name, width=320, height=480)
    with pytest.raises(ScreenshotConfigError) as exc:
        resolve_breakpoints([custom])
    msg = str(exc.value)
    assert default_name in msg
    assert "include_defaults=False" in msg


def test_resolve_collision_error_lists_all_clashes_sorted():
    # Multiple clashes — error message lists all of them in sorted
    # order so the operator can fix everything in one pass instead of
    # whack-a-mole across N reruns.
    customs = [
        Viewport(name="desktop_1920", width=320, height=480),
        Viewport(name="mobile_375", width=320, height=480),
    ]
    with pytest.raises(ScreenshotConfigError) as exc:
        resolve_breakpoints(customs)
    msg = str(exc.value)
    assert "desktop_1920" in msg
    assert "mobile_375" in msg
    # Sorted order — mobile_375 should appear before desktop_1920.
    assert msg.index("desktop_1920") < msg.index("mobile_375") or \
        "['desktop_1920', 'mobile_375']" in msg


# ── Engine compatibility ─────────────────────────────────────────────

def test_resolved_tuple_is_engine_consumable_shape():
    # The resolver's output is fed straight into capture_multi's
    # `viewports=` arg — duck-test that every element looks like a
    # Viewport so the engine's per-call validation never has to
    # second-guess us.
    extra = Viewport(name="extra_320", width=320, height=480)
    result = resolve_breakpoints([extra])
    for vp in result:
        assert isinstance(vp, Viewport)
        # Width / height ints in engine-acceptable range
        assert vp.width >= 200 and vp.width <= 7680
        assert vp.height >= 200 and vp.height <= 7680


def test_resolved_default_only_path_is_engine_consumable():
    # The engine refuses an empty viewport list. Defaults-only path
    # must always yield ≥ 1 viewport.
    result = resolve_breakpoints()
    assert len(result) >= 1


# ── Public surface ───────────────────────────────────────────────────

def test_module_all_lists_expected_names():
    from backend.web import screenshot_breakpoints as mod
    expected = {
        "BREAKPOINT_DESKTOP_1440",
        "BREAKPOINT_DESKTOP_1920",
        "BREAKPOINT_MOBILE_375",
        "BREAKPOINT_TABLET_768",
        "DEFAULT_BREAKPOINTS",
        "DEFAULT_BREAKPOINT_NAMES",
        "resolve_breakpoints",
    }
    assert set(mod.__all__) == expected
    # Alphabetised — keep public-surface listings stable + reviewable.
    assert mod.__all__ == sorted(mod.__all__)


def test_package_reexports_breakpoints():
    # backend.web package surface should re-export everything from the
    # screenshot_breakpoints module so callers don't reach into the
    # sub-module path.
    import backend.web as pkg

    for name in (
        "BREAKPOINT_DESKTOP_1440",
        "BREAKPOINT_DESKTOP_1920",
        "BREAKPOINT_MOBILE_375",
        "BREAKPOINT_TABLET_768",
        "DEFAULT_BREAKPOINTS",
        "DEFAULT_BREAKPOINT_NAMES",
        "resolve_breakpoints",
    ):
        assert hasattr(pkg, name), f"backend.web missing re-export {name}"
        assert name in pkg.__all__, f"{name!r} absent from backend.web.__all__"


def test_package_reexports_share_identity_with_module():
    # Re-exports must be the *same* objects — a future patch to the
    # sub-module propagates without divergence.
    import backend.web as pkg
    from backend.web import screenshot_breakpoints as mod

    assert pkg.DEFAULT_BREAKPOINTS is mod.DEFAULT_BREAKPOINTS
    assert pkg.resolve_breakpoints is mod.resolve_breakpoints
    assert pkg.BREAKPOINT_MOBILE_375 is mod.BREAKPOINT_MOBILE_375
