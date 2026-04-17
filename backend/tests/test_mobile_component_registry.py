"""V5 #2 (issue #321) — mobile component registry contract tests.

Pins ``backend/mobile_component_registry.py`` against:

  * structural invariants of every entry (platform / category /
    summary / signature / example / min_version all present);
  * cross-platform parity — every category populated by every
    platform so the agent can offer SwiftUI / Compose / Flutter
    parity for any layout intent;
  * the JSON-serialisability contract for
    ``get_mobile_components()`` (so the agent tool boundary never
    leaks a dataclass);
  * platform / category filter contracts;
  * determinism of ``render_agent_context_block()`` (same inputs →
    identical output byte-for-byte; required for prompt-caching);
  * the deprecated-API guard — entries that flag deprecation MUST
    name the legacy form they replace, and the legacy form MUST NOT
    appear as its own registry entry (otherwise the agent will
    happily resurrect it from training memory).
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from backend import mobile_component_registry as r


# ── Structural invariants ────────────────────────────────────────────


class TestRegistryStructure:
    def test_registry_is_not_empty(self):
        assert len(r.REGISTRY) > 0

    def test_schema_version_is_semver_string(self):
        parts = r.REGISTRY_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_platforms_tuple_is_immutable_and_finite(self):
        assert isinstance(r.PLATFORMS, tuple)
        assert set(r.PLATFORMS) == {"swiftui", "compose", "flutter"}

    def test_categories_tuple_is_immutable_and_finite(self):
        assert isinstance(r.CATEGORIES, tuple)
        assert set(r.CATEGORIES) == {
            "layout",
            "navigation",
            "inputs",
            "overlay",
            "feedback",
            "data",
        }

    def test_platform_labels_cover_every_platform(self):
        for p in r.PLATFORMS:
            assert p in r.PLATFORM_LABELS
            assert r.PLATFORM_LABELS[p].strip()

    @pytest.mark.parametrize("key", sorted(r.REGISTRY.keys()))
    def test_every_entry_has_required_fields(self, key):
        comp = r.REGISTRY[key]
        assert comp.platform in r.PLATFORMS
        assert comp.category in r.CATEGORIES
        assert comp.name.strip()
        assert comp.summary.strip(), f"{key}: summary"
        assert comp.signature.strip(), f"{key}: signature"
        assert comp.example.strip(), f"{key}: example"
        assert comp.min_version.strip(), f"{key}: min_version"

    @pytest.mark.parametrize("key", sorted(r.REGISTRY.keys()))
    def test_every_entry_key_matches_platform_and_name(self, key):
        comp = r.REGISTRY[key]
        assert comp.key == key
        assert key == f"{comp.platform}:{comp.name}"

    def test_platform_validation_rejects_unknown(self):
        with pytest.raises(ValueError):
            r.MobileComponent(
                name="Fake",
                platform="not-a-platform",
                category="layout",
                summary="x",
                signature="x",
                example="x",
                min_version="iOS 16",
            )

    def test_category_validation_rejects_unknown(self):
        with pytest.raises(ValueError):
            r.MobileComponent(
                name="Fake",
                platform="swiftui",
                category="not-a-category",
                summary="x",
                signature="x",
                example="x",
                min_version="iOS 16",
            )

    @pytest.mark.parametrize(
        "field_name",
        ["summary", "signature", "example", "min_version"],
    )
    def test_empty_required_field_rejected(self, field_name):
        kwargs = {
            "name": "Fake",
            "platform": "swiftui",
            "category": "layout",
            "summary": "x",
            "signature": "x",
            "example": "x",
            "min_version": "iOS 16",
        }
        kwargs[field_name] = "  "
        with pytest.raises(ValueError):
            r.MobileComponent(**kwargs)

    def test_dataclass_is_frozen(self):
        comp = next(iter(r.REGISTRY.values()))
        with pytest.raises(dataclasses.FrozenInstanceError):
            comp.summary = "mutated"  # type: ignore[misc]


# ── Cross-platform parity ────────────────────────────────────────────


class TestPlatformParity:
    @pytest.mark.parametrize("platform", sorted(r.PLATFORMS))
    def test_every_platform_has_entries(self, platform):
        entries = r.get_components_by_platform(platform)
        assert len(entries) >= 10, (
            f"{platform}: at least 10 components expected — the mobile "
            f"designer agent emits cross-platform parity by default and "
            f"needs a usable surface per platform."
        )

    @pytest.mark.parametrize("platform", sorted(r.PLATFORMS))
    @pytest.mark.parametrize("category", sorted(r.CATEGORIES))
    def test_every_platform_populates_every_category(self, platform, category):
        entries = r.get_components_by_category(category, platform=platform)
        assert entries, (
            f"{platform} has no {category} entries — the registry must "
            f"offer cross-platform parity so the agent can suggest the "
            f"same intent (e.g. 'show a sheet') on all three targets."
        )

    @pytest.mark.parametrize("platform", sorted(r.PLATFORMS))
    def test_no_duplicate_names_within_platform(self, platform):
        names = [c.name for c in r.REGISTRY.values() if c.platform == platform]
        assert len(names) == len(set(names)), (
            f"{platform} has duplicate entry names: {sorted(names)}"
        )


# ── Public API contract ──────────────────────────────────────────────


class TestGetMobileComponents:
    def test_returns_list_of_dicts(self):
        out = r.get_mobile_components()
        assert isinstance(out, list)
        assert all(isinstance(c, dict) for c in out)
        assert len(out) == len(r.REGISTRY)

    def test_output_is_json_serialisable(self):
        # The agent tool boundary serialises through JSON — dataclass
        # or tuple leaks would crash the call.
        out = r.get_mobile_components()
        encoded = json.dumps(out)
        assert len(encoded) > 0

    def test_every_dict_has_required_keys(self):
        out = r.get_mobile_components()
        required = {
            "name",
            "platform",
            "category",
            "summary",
            "signature",
            "example",
            "min_version",
            "a11y",
            "variants",
            "notes",
            "deprecates",
            "key",
        }
        for entry in out:
            assert required <= set(entry.keys()), entry["name"]

    def test_collection_fields_are_lists_not_tuples(self):
        out = r.get_mobile_components()
        for entry in out:
            assert isinstance(entry["variants"], list)
            assert isinstance(entry["notes"], list)
            assert isinstance(entry["deprecates"], list)

    def test_filter_by_platform(self):
        for plat in r.PLATFORMS:
            out = r.get_mobile_components(platform=plat)
            assert out, f"expected ≥ 1 entry for platform {plat}"
            assert all(c["platform"] == plat for c in out)

    def test_filter_by_category(self):
        for cat in r.CATEGORIES:
            out = r.get_mobile_components(category=cat)
            assert out, f"expected ≥ 1 entry for category {cat}"
            assert all(c["category"] == cat for c in out)

    def test_filter_by_platform_and_category(self):
        out = r.get_mobile_components(platform="swiftui", category="layout")
        assert out
        assert all(
            c["platform"] == "swiftui" and c["category"] == "layout"
            for c in out
        )

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            r.get_mobile_components(platform="windows-phone")

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError):
            r.get_mobile_components(category="not-a-category")

    def test_results_are_sorted_by_platform_then_name(self):
        out = r.get_mobile_components()
        plat_order = {p: i for i, p in enumerate(r.PLATFORMS)}
        keys = [(plat_order[c["platform"]], c["name"]) for c in out]
        assert keys == sorted(keys)


class TestComponentLookup:
    def test_get_component_returns_known(self):
        comp = r.get_component("swiftui", "NavigationStack")
        assert comp is not None
        assert comp.platform == "swiftui"
        assert comp.name == "NavigationStack"

    def test_get_component_returns_none_for_unknown(self):
        assert r.get_component("swiftui", "DoesNotExist") is None

    def test_get_component_rejects_unknown_platform(self):
        with pytest.raises(ValueError):
            r.get_component("windows-phone", "Anything")

    def test_list_component_names_full(self):
        names = r.list_component_names()
        # Order follows PLATFORMS order, then name within platform
        # (matches the canonical order render_agent_context_block uses).
        plat_order = {p: i for i, p in enumerate(r.PLATFORMS)}
        assert names == sorted(
            names, key=lambda n: (plat_order[n.split(":")[0]], n.split(":", 1)[1])
        )
        assert len(names) == len(r.REGISTRY)

    def test_list_component_names_per_platform(self):
        names = r.list_component_names(platform="compose")
        assert names == sorted(names)
        for n in names:
            assert ":" not in n  # bare name when platform-scoped
            assert r.get_component("compose", n) is not None


# ── Spot-checks: the entries the agent reaches for most often ────────


class TestSwiftUISpotChecks:
    def test_navigationstack_replaces_navigationview(self):
        ns = r.get_component("swiftui", "NavigationStack")
        assert ns is not None
        assert "NavigationView" in ns.deprecates

    def test_navigationview_is_not_in_registry(self):
        # Critical: the agent must not be offered the deprecated form,
        # otherwise it will resurrect it from training memory.
        assert r.get_component("swiftui", "NavigationView") is None

    def test_observable_macro_present(self):
        obs = r.get_component("swiftui", "@Observable")
        assert obs is not None
        assert "ObservableObject" in " ".join(obs.deprecates)

    def test_alert_is_overlay_with_a11y_note(self):
        a = r.get_component("swiftui", "alert")
        assert a is not None
        assert a.category == "overlay"
        assert "VoiceOver" in a.a11y or "focus" in a.a11y.lower()

    def test_button_has_buttonstyle_variants(self):
        b = r.get_component("swiftui", "Button")
        assert b is not None
        joined = " ".join(b.variants).lower()
        assert "borderedprominent" in joined or "bordered" in joined


class TestComposeSpotChecks:
    def test_scaffold_signature_includes_all_slots(self):
        s = r.get_component("compose", "Scaffold")
        assert s is not None
        for slot in ("topBar", "bottomBar", "snackbarHost", "floatingActionButton"):
            assert slot in s.signature, f"{slot} missing from Scaffold signature"

    def test_navigationbar_supersedes_m2_bottomnavigation(self):
        nb = r.get_component("compose", "NavigationBar")
        assert nb is not None
        assert any("BottomNavigation" in d for d in nb.deprecates)

    def test_lazycolumn_warns_about_keys(self):
        lc = r.get_component("compose", "LazyColumn")
        assert lc is not None
        joined = " ".join(lc.notes).lower()
        assert "key" in joined

    def test_iconbutton_a11y_contentdescription(self):
        ib = r.get_component("compose", "IconButton")
        assert ib is not None
        assert "contentDescription" in ib.a11y

    def test_text_warns_against_hardcoded_sp(self):
        t = r.get_component("compose", "Text")
        assert t is not None
        joined = " ".join(t.notes).lower()
        assert "sp" in joined or "type scale" in joined or "token" in joined


class TestFlutterSpotChecks:
    def test_navigationbar_supersedes_bottomnavigationbar(self):
        nb = r.get_component("flutter", "NavigationBar")
        assert nb is not None
        assert any("BottomNavigationBar" in d for d in nb.deprecates)

    def test_iconbutton_requires_tooltip(self):
        ib = r.get_component("flutter", "IconButton")
        assert ib is not None
        assert "tooltip" in ib.a11y.lower()

    def test_text_warns_against_hardcoded_fontsize(self):
        t = r.get_component("flutter", "Text")
        assert t is not None
        joined = " ".join(t.notes).lower()
        assert "fontsize" in joined or "textscaler" in joined or "token" in joined

    def test_padding_warns_against_nontoken_values(self):
        p = r.get_component("flutter", "Padding")
        assert p is not None
        joined = " ".join(p.notes).lower()
        assert "token" in joined or "appspacing" in joined

    def test_tooltip_flags_mobile_caveat(self):
        t = r.get_component("flutter", "Tooltip")
        assert t is not None
        joined = (t.a11y + " " + " ".join(t.notes)).lower()
        assert "touch" in joined or "hover" in joined or "mobile" in joined


# ── Anti-pattern guards ──────────────────────────────────────────────


class TestNoHardcodedTokens:
    """Examples must use design tokens, not hex / pt / sp / px hard-codes.

    The mobile-ui-designer skill bans hard-coded styling values.  The
    registry itself is the agent's ground truth, so its examples have
    to obey the same rule — otherwise the agent learns to copy the
    bad pattern.

    Note: ``dp`` is allowed in Compose / Flutter examples for spacing
    primitives (``Modifier.padding(16.dp)``) and ``pt`` shows up only
    in framework names (``compileSdk``); we ban hex literals (``#ff..``
    or ``0xFF...`` for *colors*) and raw RGB constructors.
    """

    HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
    KOTLIN_HEX_COLOR = re.compile(r"\bColor\(0x[0-9a-fA-F]{6,8}\)")
    SWIFT_RGB = re.compile(r"Color\(\s*red:\s*[0-9.]+\s*,")
    DART_HEX_COLOR = re.compile(r"\bColor\(0x[0-9a-fA-F]{6,8}\)")
    SWIFT_FONT_SIZE = re.compile(r"\.font\(\.system\(size:\s*\d+")
    DART_FONT_SIZE = re.compile(r"\bfontSize:\s*\d+")

    @pytest.mark.parametrize("key", sorted(r.REGISTRY.keys()))
    def test_example_has_no_hardcoded_color(self, key):
        comp = r.REGISTRY[key]
        assert not self.HEX_COLOR.search(comp.example), (
            f"{key}: hex color literal in example — use design token"
        )
        assert not self.KOTLIN_HEX_COLOR.search(comp.example), (
            f"{key}: Color(0xFF...) literal — use MaterialTheme.colorScheme.*"
        )
        assert not self.DART_HEX_COLOR.search(comp.example), (
            f"{key}: Color(0xFF...) literal — use Theme.of(context)"
        )
        assert not self.SWIFT_RGB.search(comp.example), (
            f"{key}: Color(red:green:blue:) — use Asset Catalog"
        )

    @pytest.mark.parametrize("key", sorted(r.REGISTRY.keys()))
    def test_example_has_no_hardcoded_font_size(self, key):
        comp = r.REGISTRY[key]
        assert not self.SWIFT_FONT_SIZE.search(comp.example), (
            f"{key}: .font(.system(size:)) breaks Dynamic Type — use .font(.body)"
        )
        assert not self.DART_FONT_SIZE.search(comp.example), (
            f"{key}: hard-coded fontSize breaks textScaler — use textTheme token"
        )


class TestDeprecatedFormsNotRegistered:
    """For each entry that names a deprecated predecessor, that
    predecessor must NOT be registered as its own callable component
    on the same platform — the agent should never see it in the menu.
    """

    @pytest.mark.parametrize("key", sorted(r.REGISTRY.keys()))
    def test_deprecated_predecessor_absent(self, key):
        comp = r.REGISTRY[key]
        for dep in comp.deprecates:
            # The deprecated string is human-readable
            # ("BottomNavigation (M2)") — extract the canonical name.
            canonical = dep.split(" ")[0].split("(")[0].strip()
            if not canonical:
                continue
            assert (
                r.get_component(comp.platform, canonical) is None
            ), (
                f"{key} deprecates {canonical!r} but {canonical} is still "
                f"registered as a {comp.platform} entry — remove it so the "
                f"agent does not pick the legacy form."
            )


# ── Agent-facing context block ───────────────────────────────────────


class TestAgentContextBlock:
    def test_output_is_nonempty_markdown(self):
        block = r.render_agent_context_block()
        assert block.startswith("# Mobile component registry")
        assert block.endswith("\n")

    def test_deterministic(self):
        # Anthropic prompt-cache stability — two identical calls must
        # produce byte-identical output.
        a = r.render_agent_context_block()
        b = r.render_agent_context_block()
        assert a == b

    def test_all_platforms_present_by_default(self):
        block = r.render_agent_context_block()
        for plat in r.PLATFORMS:
            assert r.PLATFORM_LABELS[plat] in block

    def test_all_categories_present_by_default(self):
        block = r.render_agent_context_block()
        for cat in r.CATEGORIES:
            assert f"### {cat}" in block

    def test_platform_subset(self):
        block = r.render_agent_context_block(platforms=["swiftui"])
        assert r.PLATFORM_LABELS["swiftui"] in block
        assert r.PLATFORM_LABELS["compose"] not in block
        assert r.PLATFORM_LABELS["flutter"] not in block

    def test_category_subset(self):
        block = r.render_agent_context_block(categories=["overlay"])
        assert "### overlay" in block
        assert "### data" not in block

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            r.render_agent_context_block(platforms=["windows-phone"])

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError):
            r.render_agent_context_block(categories=["not-a-category"])

    def test_signatures_appear_in_output(self):
        block = r.render_agent_context_block(platforms=["swiftui"])
        ns = r.get_component("swiftui", "NavigationStack")
        assert ns is not None
        # Signature is rendered inside backticks; first 20 chars suffice.
        assert ns.signature[:20] in block


# ── Serialisation ────────────────────────────────────────────────────


class TestSerialisation:
    def test_serialise_round_trips_through_json(self):
        for comp in r.REGISTRY.values():
            d = r._serialise(comp)
            encoded = json.dumps(d)
            decoded = json.loads(encoded)
            assert decoded["name"] == comp.name
            assert decoded["platform"] == comp.platform
            assert decoded["key"] == comp.key

    def test_serialise_does_not_leak_tuples(self):
        for comp in r.REGISTRY.values():
            d = r._serialise(comp)
            assert isinstance(d["variants"], list)
            assert isinstance(d["notes"], list)
            assert isinstance(d["deprecates"], list)
