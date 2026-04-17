"""V1 #4 (issue #317) — component consistency linter contract tests.

Pins ``backend/component_consistency_linter.py`` against:

  * structural invariants of :class:`LintRule` /
    :class:`LintViolation` / :class:`LintReport` (frozen, validated,
    JSON-safe);
  * the rule catalogue (stable ids, stable severities,
    auto-fixability flag matches the auto-fix tag list);
  * each detector's positive-case + negative-case contract;
  * comment stripping (violations in ``{/* … */}``, ``/* … */`` and
    ``// …`` don't fire);
  * determinism of ``lint_code`` (same input → identical violation
    ordering) and idempotency of ``auto_fix_code``;
  * the JSON-safe shape of :func:`run_consistency_linter`'s return
    value;
  * the ``components/ui/`` exclude rule in ``lint_directory`` so we
    don't flag vendored shadcn source.

If a rule id or severity needs to change, flag it in the module
docstring *and* update the test — don't silently drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import component_consistency_linter as lint


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Structural invariants ────────────────────────────────────────────


class TestRuleCatalogue:
    def test_schema_version_semver(self):
        parts = lint.LINTER_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_severities_ordered_and_fixed(self):
        assert lint.SEVERITIES == ("error", "warn", "info")

    def test_rules_is_immutable_mapping(self):
        with pytest.raises(TypeError):
            lint.RULES["raw-button"] = None  # type: ignore[misc]

    def test_all_expected_rules_present(self):
        expected = {
            "raw-button",
            "raw-input",
            "raw-textarea",
            "raw-select",
            "raw-dialog",
            "raw-progress",
            "div-onclick",
            "role-button-on-div",
            "img-without-alt",
            "tabindex-positive",
            "focus-outline-none-unsafe",
            "inline-hex-color",
            "hard-pinned-palette",
            "arbitrary-size",
            "arbitrary-breakpoint",
            "important-hack",
            "dark-prefix-on-dark-only",
        }
        assert set(lint.RULES.keys()) == expected

    @pytest.mark.parametrize("rule_id", sorted(lint.RULES.keys()))
    def test_rule_fields_valid(self, rule_id):
        rule = lint.RULES[rule_id]
        assert rule.rule_id == rule_id
        assert rule.severity in lint.SEVERITIES
        assert rule.summary.strip()
        assert isinstance(rule.auto_fixable, bool)

    def test_rule_validation_rejects_unknown_severity(self):
        with pytest.raises(ValueError):
            lint.LintRule("x", "critical", "s")

    def test_rule_validation_rejects_empty_id_or_summary(self):
        with pytest.raises(ValueError):
            lint.LintRule("", "error", "s")
        with pytest.raises(ValueError):
            lint.LintRule("x", "error", "")

    def test_auto_fixable_rules_match_tag_swaps(self):
        """Every rule marked auto_fixable must map to a tag in the swap table."""
        fixable = {r for r in lint.RULES if lint.RULES[r].auto_fixable}
        for rid in fixable:
            assert rid.startswith("raw-"), rid
            tag = rid.removeprefix("raw-")
            assert tag in lint._TAG_SWAPS


# ── LintViolation / LintReport invariants ────────────────────────────


class TestLintViolation:
    def test_is_frozen(self):
        v = lint.LintViolation(
            rule_id="raw-button", severity="error", line=1, column=1,
            message="x",
        )
        with pytest.raises(Exception):
            v.line = 2  # type: ignore[misc]

    def test_unknown_rule_id_rejected(self):
        with pytest.raises(ValueError):
            lint.LintViolation(
                rule_id="does-not-exist",
                severity="error", line=1, column=1, message="x",
            )

    def test_non_positive_line_rejected(self):
        with pytest.raises(ValueError):
            lint.LintViolation(
                rule_id="raw-button", severity="error",
                line=0, column=1, message="x",
            )
        with pytest.raises(ValueError):
            lint.LintViolation(
                rule_id="raw-button", severity="error",
                line=1, column=0, message="x",
            )


class TestLintReport:
    def test_empty_report_is_clean(self):
        r = lint.LintReport()
        assert r.is_clean is True
        assert r.violations == ()
        assert r.severity_counts["error"] == 0

    def test_warn_only_report_still_clean(self):
        r = lint.LintReport(
            violations=(
                lint.LintViolation(
                    rule_id="hard-pinned-palette",
                    severity="warn", line=1, column=1, message="x",
                ),
            ),
        )
        assert r.is_clean is True
        assert r.severity_counts["warn"] == 1

    def test_error_blocks_cleanliness(self):
        r = lint.LintReport(
            violations=(
                lint.LintViolation(
                    rule_id="raw-button",
                    severity="error", line=1, column=1, message="x",
                ),
            ),
        )
        assert r.is_clean is False

    def test_to_dict_is_json_serialisable(self):
        r = lint.lint_code('<button onClick={x}>x</button>')
        data = r.to_dict()
        # Full JSON roundtrip — no dataclass instances leaked.
        text = json.dumps(data)
        restored = json.loads(text)
        assert restored["is_clean"] is False
        assert restored["schema_version"] == lint.LINTER_SCHEMA_VERSION
        assert restored["violations"][0]["rule_id"] == "raw-button"

    def test_counts_views_are_immutable(self):
        r = lint.lint_code('<button>x</button>')
        with pytest.raises(TypeError):
            r.severity_counts["error"] = 999  # type: ignore[misc]
        with pytest.raises(TypeError):
            r.rule_counts["raw-button"] = 999  # type: ignore[misc]


# ── Detector: raw HTML tag rules ─────────────────────────────────────


class TestRawTagDetectors:
    @pytest.mark.parametrize(
        ("tag", "rule_id"),
        [
            ("button", "raw-button"),
            ("input", "raw-input"),
            ("textarea", "raw-textarea"),
            ("select", "raw-select"),
            ("dialog", "raw-dialog"),
            ("progress", "raw-progress"),
        ],
    )
    def test_positive(self, tag, rule_id):
        code = f"<{tag}>x</{tag}>" if tag not in {"input"} else f"<{tag} />"
        r = lint.lint_code(code)
        rule_ids = {v.rule_id for v in r.violations}
        assert rule_id in rule_ids, f"expected {rule_id} in {rule_ids}"
        v = next(v for v in r.violations if v.rule_id == rule_id)
        assert v.severity == "error"
        assert v.line == 1
        assert v.column >= 1

    def test_shadcn_component_not_flagged(self):
        code = "<Button>click</Button>"
        r = lint.lint_code(code)
        assert r.is_clean

    def test_suggested_fix_mentions_component_and_path(self):
        r = lint.lint_code("<button>x</button>")
        v = next(v for v in r.violations if v.rule_id == "raw-button")
        assert "Button" in (v.suggested_fix or "")
        assert "@/components/ui/button" in (v.suggested_fix or "")

    def test_native_input_opt_out(self):
        """Input with data-slot=\"native-input\" is an internal shadcn slot."""
        code = '<input data-slot="native-input" type="text" />'
        r = lint.lint_code(code)
        assert not any(v.rule_id == "raw-input" for v in r.violations)


# ── Detector: div onClick / role="button" / img alt / tabIndex ───────


class TestSemanticA11yDetectors:
    def test_div_onclick(self):
        r = lint.lint_code('<div onClick={h}>x</div>')
        rule_ids = {v.rule_id for v in r.violations}
        assert "div-onclick" in rule_ids

    def test_div_onclick_with_role_button_still_triggers_role_rule(self):
        r = lint.lint_code('<div role="button" onClick={h}>x</div>')
        rule_ids = {v.rule_id for v in r.violations}
        assert "role-button-on-div" in rule_ids

    def test_span_role_button(self):
        r = lint.lint_code('<span role="button">x</span>')
        rule_ids = {v.rule_id for v in r.violations}
        assert "role-button-on-div" in rule_ids

    def test_img_without_alt(self):
        r = lint.lint_code('<img src="/a.png" />')
        rule_ids = {v.rule_id for v in r.violations}
        assert "img-without-alt" in rule_ids

    def test_img_with_empty_alt_accepted(self):
        r = lint.lint_code('<img src="/a.png" alt="" />')
        assert not any(v.rule_id == "img-without-alt" for v in r.violations)

    def test_img_with_described_alt_accepted(self):
        r = lint.lint_code('<img src="/logo.png" alt="Company logo" />')
        assert not any(v.rule_id == "img-without-alt" for v in r.violations)

    @pytest.mark.parametrize("val", [1, 2, 99])
    def test_positive_tabindex_flagged(self, val):
        r = lint.lint_code(f'<button tabIndex={{{val}}}>x</button>')
        assert any(v.rule_id == "tabindex-positive" for v in r.violations)

    @pytest.mark.parametrize("val", [0, -1])
    def test_non_positive_tabindex_accepted(self, val):
        r = lint.lint_code(f'<Button tabIndex={{{val}}}>x</Button>')
        assert not any(v.rule_id == "tabindex-positive" for v in r.violations)


# ── Detector: design-token rules ─────────────────────────────────────


class TestDesignTokenDetectors:
    @pytest.mark.parametrize(
        "hex_code",
        ["#fff", "#abc", "#38bdf8", "#FEF2F2", "#12345678"],
    )
    def test_inline_hex_flagged(self, hex_code):
        code = f"<div style={{{{ color: '{hex_code}' }}}}>x</div>"
        r = lint.lint_code(code)
        assert any(v.rule_id == "inline-hex-color" for v in r.violations)

    def test_hex_in_classname_bracket_flagged(self):
        code = '<p className="text-[#38bdf8]">x</p>'
        r = lint.lint_code(code)
        rule_ids = {v.rule_id for v in r.violations}
        assert "inline-hex-color" in rule_ids

    def test_tailwind_palette_flagged(self):
        code = '<div className="bg-slate-900 text-zinc-100">x</div>'
        r = lint.lint_code(code)
        rule_ids = {v.rule_id for v in r.violations}
        assert "hard-pinned-palette" in rule_ids

    def test_semantic_tokens_not_flagged(self):
        code = '<div className="bg-background text-foreground bg-primary">x</div>'
        r = lint.lint_code(code)
        assert not any(
            v.rule_id == "hard-pinned-palette" for v in r.violations
        )

    @pytest.mark.parametrize(
        "cls",
        ["text-[13px]", "p-[5px]", "gap-[7px]", "w-[47px]", "min-h-[300px]"],
    )
    def test_arbitrary_size_flagged(self, cls):
        r = lint.lint_code(f'<div className="{cls}">x</div>')
        assert any(v.rule_id == "arbitrary-size" for v in r.violations)

    def test_grid_cols_arbitrary_not_flagged(self):
        """`grid-cols-[…]` is legit for complex templates — excluded."""
        r = lint.lint_code(
            '<div className="grid grid-cols-[1fr_auto_1fr]">x</div>'
        )
        assert not any(v.rule_id == "arbitrary-size" for v in r.violations)

    def test_arbitrary_breakpoint_flagged(self):
        r = lint.lint_code('<div className="min-[412px]:flex">x</div>')
        assert any(v.rule_id == "arbitrary-breakpoint" for v in r.violations)

    def test_standard_breakpoint_not_flagged(self):
        r = lint.lint_code('<div className="md:flex lg:grid 2xl:block">x</div>')
        assert not any(
            v.rule_id == "arbitrary-breakpoint" for v in r.violations
        )

    def test_important_flagged(self):
        r = lint.lint_code('<div className="!text-red-500">x</div>')
        assert any(v.rule_id == "important-hack" for v in r.violations)

    def test_dark_prefix_flagged(self):
        r = lint.lint_code('<div className="bg-background dark:bg-black">x</div>')
        rule_ids = {v.rule_id for v in r.violations}
        assert "dark-prefix-on-dark-only" in rule_ids

    def test_no_dark_prefix_not_flagged(self):
        r = lint.lint_code('<div className="bg-background">x</div>')
        assert not any(
            v.rule_id == "dark-prefix-on-dark-only" for v in r.violations
        )


# ── Detector: focus outline ──────────────────────────────────────────


class TestOutlineNone:
    def test_outline_none_alone_flagged(self):
        r = lint.lint_code('<Button className="outline-none">x</Button>')
        assert any(
            v.rule_id == "focus-outline-none-unsafe" for v in r.violations
        )

    def test_outline_none_with_replacement_ring_accepted(self):
        code = (
            '<Button className="outline-none focus-visible:ring-2 '
            'focus-visible:ring-ring">x</Button>'
        )
        r = lint.lint_code(code)
        assert not any(
            v.rule_id == "focus-outline-none-unsafe" for v in r.violations
        )

    def test_style_outline_none_flagged(self):
        r = lint.lint_code('<div style={{ outline: "none" }}>x</div>')
        assert any(
            v.rule_id == "focus-outline-none-unsafe" for v in r.violations
        )


# ── Comment stripping ────────────────────────────────────────────────


class TestCommentStripping:
    def test_jsx_block_comment_is_stripped(self):
        code = "{/* <button onClick={h}>x</button> */}\nconst y = 1"
        r = lint.lint_code(code)
        assert r.is_clean, r.violations

    def test_js_block_comment_is_stripped(self):
        code = "/* <input /> <button>x</button> */\nconst y = 1"
        r = lint.lint_code(code)
        assert r.is_clean, r.violations

    def test_line_comment_is_stripped(self):
        code = "// <button>x</button>\nconst y = 1"
        r = lint.lint_code(code)
        assert r.is_clean, r.violations

    def test_comment_stripping_preserves_line_numbers(self):
        code = (
            "// leader comment with <button>fake</button>\n"
            "<button>real</button>\n"
        )
        r = lint.lint_code(code)
        v = next(v for v in r.violations if v.rule_id == "raw-button")
        assert v.line == 2


# ── Ordering + determinism ───────────────────────────────────────────


class TestDeterminism:
    def test_violations_sorted_by_line_column(self):
        code = (
            '<input />\n'
            '<button className="bg-slate-900">x</button>\n'
            '<textarea />\n'
        )
        r = lint.lint_code(code)
        lines = [v.line for v in r.violations]
        assert lines == sorted(lines)

    def test_same_input_same_output(self):
        code = (
            '<button onClick={h} className="bg-slate-900 text-[13px]">x</button>'
        )
        r1 = lint.lint_code(code).to_dict()
        r2 = lint.lint_code(code).to_dict()
        assert r1 == r2


# ── Auto-fix ─────────────────────────────────────────────────────────


class TestAutoFix:
    def test_rewrites_raw_button(self):
        code = "<button onClick={h}>Save</button>"
        fixed, rep = lint.auto_fix_code(code)
        assert "<Button onClick={h}>Save</Button>" in fixed
        assert 'from "@/components/ui/button"' in fixed
        assert rep.is_clean

    def test_rewrites_multiple_tags(self):
        code = "<button>a</button>\n<input />\n<textarea />"
        fixed, _ = lint.auto_fix_code(code)
        assert "<Button>" in fixed
        assert "<Input" in fixed
        assert "<Textarea" in fixed

    def test_rewrites_progress_tag(self):
        code = '<progress value={50} max={100}>50</progress>'
        fixed, rep = lint.auto_fix_code(code)
        assert "<Progress" in fixed
        assert "</Progress>" in fixed
        assert 'from "@/components/ui/progress"' in fixed

    def test_is_idempotent(self):
        code = "<button>x</button>\n<input />"
        once, _ = lint.auto_fix_code(code)
        twice, _ = lint.auto_fix_code(once)
        assert once == twice

    def test_inserts_import_after_use_client(self):
        code = (
            '"use client"\n'
            '\n'
            'export function X() { return <button>y</button> }\n'
        )
        fixed, _ = lint.auto_fix_code(code)
        lines = fixed.splitlines()
        # "use client" must remain the first non-empty line.
        first_non_empty = next(l for l in lines if l.strip())
        assert first_non_empty == '"use client"'
        assert any('from "@/components/ui/button"' in l for l in lines)

    def test_merges_into_existing_import(self):
        code = (
            'import { Card } from "@/components/ui/card"\n'
            'export function X() { return <button>y</button> }\n'
        )
        fixed, _ = lint.auto_fix_code(code)
        # Adds a new import line for button; must not duplicate the Card import.
        import_lines = [l for l in fixed.splitlines() if l.lstrip().startswith("import")]
        assert any('Button' in l for l in import_lines)
        assert sum(1 for l in import_lines if '"@/components/ui/card"' in l) == 1

    def test_does_not_duplicate_existing_imports(self):
        code = (
            'import { Button } from "@/components/ui/button"\n'
            '<button>y</button>\n'
        )
        fixed, _ = lint.auto_fix_code(code)
        button_imports = [
            l for l in fixed.splitlines()
            if l.lstrip().startswith("import") and "Button" in l
        ]
        assert len(button_imports) == 1

    def test_clean_code_untouched(self):
        code = 'import { Button } from "@/components/ui/button"\n<Button>x</Button>'
        fixed, rep = lint.auto_fix_code(code)
        assert fixed == code
        assert rep.is_clean


# ── File / directory API ─────────────────────────────────────────────


class TestFileApi:
    def test_lint_missing_file_returns_clean(self, tmp_path):
        r = lint.lint_file(tmp_path / "missing.tsx")
        assert r.is_clean
        assert r.violations == ()

    def test_lint_file_round_trip(self, tmp_path):
        p = tmp_path / "x.tsx"
        p.write_text('<button>x</button>', encoding="utf-8")
        r = lint.lint_file(p)
        assert not r.is_clean
        assert r.source == str(p)

    def test_auto_fix_file_writes_back(self, tmp_path):
        p = tmp_path / "x.tsx"
        p.write_text('<button>x</button>', encoding="utf-8")
        fixed, _ = lint.auto_fix_file(p, write=True)
        assert "<Button>" in p.read_text(encoding="utf-8")
        assert fixed == p.read_text(encoding="utf-8")

    def test_auto_fix_file_no_write(self, tmp_path):
        p = tmp_path / "x.tsx"
        original = '<button>x</button>'
        p.write_text(original, encoding="utf-8")
        fixed, _ = lint.auto_fix_file(p, write=False)
        assert "<Button>" in fixed
        # File on disk unchanged.
        assert p.read_text(encoding="utf-8") == original

    def test_lint_directory_skips_missing_root(self, tmp_path):
        assert lint.lint_directory(tmp_path / "missing") == ()

    def test_lint_directory_excludes_node_modules(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "bad.tsx").write_text("<button>x</button>")
        (tmp_path / "ok.tsx").write_text("<Button>x</Button>")
        reports = lint.lint_directory(tmp_path)
        sources = [r.source for r in reports]
        assert not any("node_modules" in s for s in sources)

    def test_lint_directory_excludes_components_ui_by_default(self, tmp_path):
        (tmp_path / "components" / "ui").mkdir(parents=True)
        (tmp_path / "components" / "ui" / "button.tsx").write_text("<button>x</button>")
        (tmp_path / "app.tsx").write_text("<Button>x</Button>")
        reports = lint.lint_directory(tmp_path)
        sources = [r.source for r in reports]
        assert not any("components/ui" in s.replace("\\", "/") for s in sources)


# ── Agent-facing entry point ─────────────────────────────────────────


class TestAgentEntryPoint:
    def test_requires_exactly_one_of_code_or_path(self):
        with pytest.raises(ValueError):
            lint.run_consistency_linter()
        with pytest.raises(ValueError):
            lint.run_consistency_linter(code="x", path="/tmp/x")

    def test_returns_json_safe_dict(self):
        result = lint.run_consistency_linter(code="<button>x</button>")
        json.dumps(result)  # must not raise
        assert result["schema_version"] == lint.LINTER_SCHEMA_VERSION
        assert "violations" in result
        assert "markdown" in result
        assert result["auto_fix_applied"] is False

    def test_auto_fix_returns_fixed_code(self):
        result = lint.run_consistency_linter(
            code="<button>x</button>", auto_fix=True,
        )
        assert result["auto_fix_applied"] is True
        assert "<Button>" in result["fixed_code"]
        assert result["is_clean"] is True

    def test_path_mode_reads_file(self, tmp_path):
        p = tmp_path / "x.tsx"
        p.write_text("<button>y</button>")
        result = lint.run_consistency_linter(path=p)
        assert result["source"] == str(p)
        assert result["is_clean"] is False


# ── Render report ────────────────────────────────────────────────────


class TestRenderReport:
    def test_clean_report_renders_all_clean(self):
        r = lint.lint_code("<Button>x</Button>")
        out = lint.render_report(r)
        assert "clean" in out.lower()
        assert out.endswith("\n")

    def test_dirty_report_lists_violations(self):
        r = lint.lint_code('<button onClick={h}>x</button>')
        out = lint.render_report(r)
        assert "raw-button" in out
        assert "error" in out

    def test_empty_iterable_returns_no_files_scanned(self):
        out = lint.render_report([])
        assert "No files scanned" in out

    def test_determinism(self):
        r = lint.lint_code(
            '<button onClick={h} className="bg-slate-900">x</button>'
        )
        assert lint.render_report(r) == lint.render_report(r)


# ── Live-project regression (skip gracefully if layout changes) ──────


class TestLiveProject:
    """Make sure the linter doesn't false-flag the project's own surface."""

    def test_ui_component_registry_has_matching_swaps(self):
        """Each tag swap must reference a real registry entry by stem."""
        from backend import ui_component_registry as r

        for tag, swap in lint._TAG_SWAPS.items():
            # Component stem matches the registry key.
            assert tag in r.REGISTRY, f"swap for <{tag}> not in component registry"

    def test_malformed_input_does_not_raise(self):
        # Truncated JSX, unmatched braces, weird escapes — linter must
        # still return a well-formed report (possibly empty).
        for sample in [
            "",
            "   \n\t  ",
            "<div",
            "}}{{><<><<",
            "<button\n<input\n</textarea",
            "const x = '<button>y</button>'\n",  # inside a string literal
        ]:
            r = lint.lint_code(sample)
            assert isinstance(r, lint.LintReport)
