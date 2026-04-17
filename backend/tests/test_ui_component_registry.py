"""V1 #2 (issue #317) — shadcn/ui component registry contract tests.

Pins the ``backend/ui_component_registry.py`` module against:

  * structural invariants of every registry entry (category, exports,
    example non-empty, ARIA pattern where required);
  * on-disk parity — every ``components/ui/*.tsx`` component has a
    registry entry and vice-versa;
  * the JSON-serialisability contract for
    ``get_available_components()`` (so the agent tool boundary never
    leaks a dataclass);
  * the category-filter contract;
  * determinism of ``render_agent_context_block()`` (same inputs →
    identical output byte-for-byte; required for LLM prompt caching).

These tests exist so a future shadcn add/remove/rename commit cannot
silently desync the registry from the actual UI surface — the agent
would otherwise start hallucinating import paths.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from backend import ui_component_registry as r


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
UI_DIR = PROJECT_ROOT / "components" / "ui"


# ── Structural invariants ────────────────────────────────────────────


class TestRegistryStructure:
    def test_registry_is_not_empty(self):
        assert len(r.REGISTRY) > 0

    def test_schema_version_is_semver_string(self):
        parts = r.REGISTRY_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_categories_tuple_is_immutable_and_finite(self):
        assert isinstance(r.CATEGORIES, tuple)
        assert set(r.CATEGORIES) == {
            "inputs",
            "form",
            "layout",
            "navigation",
            "overlay",
            "feedback",
            "data",
        }

    @pytest.mark.parametrize("name", sorted(r.REGISTRY.keys()))
    def test_every_entry_has_required_fields(self, name):
        comp = r.REGISTRY[name]
        assert comp.name == name, "key must match dataclass name"
        assert comp.category in r.CATEGORIES
        assert comp.exports, f"{name}: exports must be non-empty"
        assert comp.summary.strip(), f"{name}: summary must be non-empty"
        assert comp.example.strip(), f"{name}: example must be non-empty"

    @pytest.mark.parametrize("name", sorted(r.REGISTRY.keys()))
    def test_every_entry_import_path_is_correct(self, name):
        assert r.REGISTRY[name].import_path == f"@/components/ui/{name}"

    def test_category_validation_rejects_unknown(self):
        with pytest.raises(ValueError):
            r.ShadcnComponent(
                name="fake",
                category="not-a-category",
                summary="x",
                exports=("X",),
                example="<X />",
            )

    def test_empty_exports_rejected(self):
        with pytest.raises(ValueError):
            r.ShadcnComponent(
                name="fake",
                category="inputs",
                summary="x",
                exports=(),
                example="<X />",
            )

    def test_empty_example_rejected(self):
        with pytest.raises(ValueError):
            r.ShadcnComponent(
                name="fake",
                category="inputs",
                summary="x",
                exports=("X",),
                example="   ",
            )

    def test_dataclass_is_frozen(self):
        comp = next(iter(r.REGISTRY.values()))
        with pytest.raises(dataclasses.FrozenInstanceError):
            comp.summary = "mutated"  # type: ignore[misc]


# ── On-disk parity ───────────────────────────────────────────────────


class TestDiskParity:
    def test_ui_dir_exists(self):
        assert UI_DIR.is_dir(), f"expected {UI_DIR} to exist in the repo"

    def test_every_component_on_disk_has_registry_entry(self):
        missing = r.find_missing_on_disk(PROJECT_ROOT)
        assert missing == [], (
            f"These files under components/ui/ have no registry entry — "
            f"update backend/ui_component_registry.py: {missing}"
        )

    def test_every_registry_entry_has_file_on_disk(self):
        stems = {p.stem for p in UI_DIR.glob("*.tsx")}
        registered = set(r.REGISTRY.keys())
        orphaned = sorted(registered - stems)
        assert orphaned == [], (
            f"Registry entries with no *.tsx on disk "
            f"(stale entries, remove them): {orphaned}"
        )

    def test_utility_files_are_not_registered(self):
        # use-mobile.tsx and use-toast.ts are hooks, not components.
        # They must NOT appear in REGISTRY otherwise the agent will
        # try to <UseMobile /> them.
        for util in ("use-mobile", "use-toast"):
            assert util not in r.REGISTRY


# ── Public API contract ──────────────────────────────────────────────


class TestGetAvailableComponents:
    def test_returns_list_of_dicts(self):
        out = r.get_available_components()
        assert isinstance(out, list)
        assert all(isinstance(c, dict) for c in out)
        assert len(out) == len(r.REGISTRY)

    def test_output_is_json_serialisable(self):
        # The agent tool boundary serialises through JSON — dataclass
        # or tuple leaks would crash the call.
        out = r.get_available_components()
        encoded = json.dumps(out)
        assert len(encoded) > 0

    def test_every_dict_has_required_keys(self):
        out = r.get_available_components()
        required = {
            "name",
            "category",
            "summary",
            "exports",
            "example",
            "props",
            "variants",
            "aria_pattern",
            "notes",
            "import_path",
        }
        for entry in out:
            assert required <= set(entry.keys()), entry["name"]

    def test_props_are_plain_dicts(self):
        out = r.get_available_components()
        for entry in out:
            for prop in entry["props"]:
                assert isinstance(prop, dict)
                assert "name" in prop and "type" in prop

    def test_variants_are_plain_dicts_with_list_values(self):
        out = r.get_available_components()
        for entry in out:
            for var in entry["variants"]:
                assert isinstance(var, dict)
                assert isinstance(var["values"], list)

    def test_exports_field_is_list_not_tuple(self):
        out = r.get_available_components()
        for entry in out:
            assert isinstance(entry["exports"], list)

    def test_filter_by_project_root_matches_unfiltered(self):
        # All 55 components are installed in this repo.
        scoped = r.get_available_components(project_root=PROJECT_ROOT)
        unscoped = r.get_available_components()
        assert len(scoped) == len(unscoped)
        assert {c["name"] for c in scoped} == {c["name"] for c in unscoped}

    def test_filter_by_missing_project_root_returns_full_catalogue(self, tmp_path):
        # No components/ui under tmp_path — we treat this as "no
        # scan data available" and fall back to the full catalogue
        # rather than returning an empty list (which would starve
        # the agent context).
        out = r.get_available_components(project_root=tmp_path)
        assert len(out) == len(r.REGISTRY)

    def test_category_filter(self):
        out = r.get_available_components(category="inputs")
        assert out, "expected at least one inputs component"
        assert all(c["category"] == "inputs" for c in out)

    def test_category_filter_rejects_unknown(self):
        with pytest.raises(ValueError):
            r.get_available_components(category="not-a-category")

    def test_results_are_sorted_by_name(self):
        out = r.get_available_components()
        names = [c["name"] for c in out]
        assert names == sorted(names)


class TestSpotChecks:
    """Pin a few high-impact components by content, not just existence.

    These are the components the UI Designer agent will reach for most
    often — if their canonical example regresses, the agent will emit
    broken code.
    """

    def test_button_variants(self):
        btn = r.get_component("button")
        assert btn is not None
        variant_axes = {v.name for v in btn.variants}
        assert variant_axes == {"variant", "size"}
        variant_values = next(v for v in btn.variants if v.name == "variant").values
        assert "destructive" in variant_values
        assert "ghost" in variant_values

    def test_form_example_references_useform_and_zod(self):
        form = r.get_component("form")
        assert form is not None
        assert "useForm" in form.example
        assert "zodResolver" in form.example
        assert "FormMessage" in form.exports

    def test_dialog_has_required_parts(self):
        dlg = r.get_component("dialog")
        assert dlg is not None
        assert "DialogTitle" in dlg.exports
        assert "DialogContent" in dlg.exports
        assert dlg.category == "layout"

    def test_tooltip_flags_mobile_caveat(self):
        tt = r.get_component("tooltip")
        assert tt is not None
        joined = " ".join(tt.notes).lower()
        assert "mobile" in joined or "touch" in joined

    def test_carousel_flags_pause_requirement(self):
        car = r.get_component("carousel")
        assert car is not None
        joined = " ".join(car.notes).lower()
        assert "pause" in joined, "carousel entry must warn about WCAG 2.2.2 pause"

    def test_alert_dialog_destructive_pattern(self):
        ad = r.get_component("alert-dialog")
        assert ad is not None
        assert "AlertDialogCancel" in ad.exports
        assert "AlertDialogAction" in ad.exports

    def test_chart_warns_against_hex_colors(self):
        ch = r.get_component("chart")
        assert ch is not None
        joined = " ".join(ch.notes).lower()
        assert "hex" in joined or "token" in joined or "config" in joined

    def test_unknown_component_returns_none(self):
        assert r.get_component("does-not-exist") is None


class TestCategoryHelpers:
    def test_list_component_names_is_sorted(self):
        names = r.list_component_names()
        assert names == sorted(names)

    def test_list_component_names_matches_registry(self):
        assert set(r.list_component_names()) == set(r.REGISTRY.keys())

    def test_get_components_by_category(self):
        out = r.get_components_by_category("feedback")
        names = {c.name for c in out}
        # A minimum set that must always be categorised as feedback.
        assert {"alert", "progress", "skeleton", "spinner", "toast"} <= names

    def test_get_components_by_category_rejects_unknown(self):
        with pytest.raises(ValueError):
            r.get_components_by_category("not-real")

    def test_every_category_has_at_least_one_component(self):
        for cat in r.CATEGORIES:
            assert r.get_components_by_category(cat), (
                f"category {cat!r} has zero components — either drop it "
                f"from CATEGORIES or populate it"
            )


class TestAgentContextBlock:
    def test_output_is_nonempty_markdown(self):
        block = r.render_agent_context_block()
        assert block.startswith("# shadcn/ui component registry")
        assert block.endswith("\n")

    def test_deterministic(self):
        # LLM prompt-cache stability — two identical calls must
        # produce byte-identical output.
        a = r.render_agent_context_block()
        b = r.render_agent_context_block()
        assert a == b

    def test_all_categories_present(self):
        block = r.render_agent_context_block()
        for cat in r.CATEGORIES:
            assert f"## {cat}" in block

    def test_project_root_scoping(self):
        block = r.render_agent_context_block(project_root=PROJECT_ROOT)
        # Every registered component should surface when scoped to
        # the live repo (all 55 are installed).
        for name in r.REGISTRY:
            assert f"**{name}**" in block

    def test_category_subset(self):
        block = r.render_agent_context_block(categories=["inputs"])
        assert "## inputs" in block
        assert "## layout" not in block


class TestSerialisation:
    def test_serialise_round_trips_through_json(self):
        for comp in r.REGISTRY.values():
            d = r._serialise(comp)
            encoded = json.dumps(d)
            decoded = json.loads(encoded)
            assert decoded["name"] == comp.name
            assert decoded["import_path"] == comp.import_path

    def test_serialise_does_not_leak_tuples(self):
        for comp in r.REGISTRY.values():
            d = r._serialise(comp)
            assert isinstance(d["exports"], list)
            assert isinstance(d["notes"], list)
            assert isinstance(d["props"], list)
            assert isinstance(d["variants"], list)
