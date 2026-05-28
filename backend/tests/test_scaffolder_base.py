"""OP-1784 W2-1 (1C #1) — ScaffolderBase contract tests.

Exercises the shared render machinery extracted from the 12 per-stack
``backend/<stack>_scaffolder.py`` modules:

* :class:`ScaffoldOptions` — the common ``project_name`` knob + its
  non-empty validation, and that subclasses can layer extra rules.
* :class:`RenderOutcome` — the files/bytes/warnings/profile_binding
  record and its ``to_dict`` shape.
* :class:`ScaffolderBase` — the render loop: ``.j2`` rendering with the
  suffix stripped, byte-for-byte copy of non-templates, the
  ``should_skip`` gate (on the *raw* relative path), the
  ``overwrite=False`` skip-with-warning path, idempotent re-render that
  leaves out-of-surface files untouched, and the hooks
  (``build_context`` / ``make_outcome``).

These are pure-Python, fixture-built scaffolds — no skill pack on disk
is required, so the test isolates the base from any one stack.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.scaffolder_base import (
    RenderOutcome,
    ScaffoldOptions,
    ScaffolderBase,
    TEMPLATE_SUFFIX,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures: a throwaway scaffold tree + a tiny concrete scaffolder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class _Opts(ScaffoldOptions):
    """Concrete options for the fixture scaffolder."""

    feature: bool = True


class _DemoScaffolder(ScaffolderBase):
    """Minimal concrete scaffolder used to drive the base render loop."""

    def should_skip(self, rel_path: str, options: ScaffoldOptions) -> bool:
        # Gate the feature file on the raw relative path (with .j2).
        return rel_path == "src/feature.txt.j2" and not getattr(options, "feature", True)

    def build_context(self, options: ScaffoldOptions) -> dict[str, Any]:
        return {"project_name": options.project_name, "feature": getattr(options, "feature", True)}

    def make_outcome(self, out_dir: Path, context: dict[str, Any]) -> RenderOutcome:
        outcome = RenderOutcome(out_dir=out_dir)
        outcome.profile_binding = {"project_name": context["project_name"]}
        return outcome


def _build_scaffold_tree(root: Path) -> None:
    """Lay down a small scaffold: a template, a static file, a binary."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "README.md.j2").write_text("# {{ project_name }}\n", encoding="utf-8")
    (root / "src" / "feature.txt.j2").write_text(
        "feature={{ feature }}\n", encoding="utf-8"
    )
    # Static (non-template) file — copied byte-for-byte.
    (root / "static.txt").write_text("verbatim {{ not_rendered }}\n", encoding="utf-8")
    # Binary file — exercises the bytes path of _write_file.
    (root / "logo.bin").write_bytes(b"\x00\x01\x02\xff")


@pytest.fixture
def scaffold_dir():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "scaffolds"
        _build_scaffold_tree(root)
        yield root


@pytest.fixture
def out_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "out"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ScaffoldOptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldOptions:
    def test_valid_project_name_passes(self):
        ScaffoldOptions(project_name="MyApp").validate()  # no raise

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="").validate()

    def test_whitespace_project_name_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="   ").validate()

    def test_subclass_inherits_project_name_check(self):
        # The subclass adds a field but inherits the base non-empty rule.
        with pytest.raises(ValueError):
            _Opts(project_name="  ", feature=True).validate()

    def test_subclass_carries_extra_field(self):
        opts = _Opts(project_name="X", feature=False)
        opts.validate()  # no raise
        assert opts.feature is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RenderOutcome
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderOutcome:
    def test_defaults_are_empty(self):
        outcome = RenderOutcome(out_dir=Path("/tmp/x"))
        assert outcome.files_written == []
        assert outcome.bytes_written == 0
        assert outcome.warnings == []
        assert outcome.profile_binding == {}

    def test_to_dict_shape(self):
        outcome = RenderOutcome(
            out_dir=Path("/tmp/x"),
            files_written=[Path("/tmp/x/a")],
            bytes_written=3,
            warnings=["w"],
            profile_binding={"k": "v"},
        )
        d = outcome.to_dict()
        assert d == {
            "out_dir": "/tmp/x",
            "files_written": ["/tmp/x/a"],
            "bytes_written": 3,
            "warnings": ["w"],
            "profile_binding": {"k": "v"},
        }

    def test_to_dict_preserves_string_profile_binding(self):
        outcome = RenderOutcome(
            out_dir=Path("/tmp/x"),
            profile_binding="linux-x86_64-native",
        )
        assert outcome.to_dict()["profile_binding"] == "linux-x86_64-native"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ScaffolderBase render loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderLoop:
    def test_renders_template_and_strips_suffix(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir, skill_label="DEMO")
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        readme = out_dir / "README.md"
        assert readme.is_file()
        assert readme.read_text() == "# MyApp\n"
        # The .j2 source must not leak into the output tree.
        assert not (out_dir / "README.md.j2").exists()

    def test_static_file_copied_verbatim(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        static = out_dir / "static.txt"
        assert static.is_file()
        # Non-template: Jinja markers survive untouched.
        assert static.read_text() == "verbatim {{ not_rendered }}\n"

    def test_binary_file_copied_byte_for_byte(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        assert (out_dir / "logo.bin").read_bytes() == b"\x00\x01\x02\xff"

    def test_outcome_tracks_files_and_bytes(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        outcome = scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        assert isinstance(outcome, RenderOutcome)
        assert outcome.out_dir == out_dir
        assert len(outcome.files_written) == 4
        assert outcome.bytes_written > 0
        assert outcome.warnings == []

    def test_make_outcome_hook_seeds_profile_binding(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        outcome = scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        assert outcome.profile_binding == {"project_name": "MyApp"}

    def test_should_skip_gate_omits_file(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp", feature=False))
        assert not (out_dir / "src" / "feature.txt").exists()
        # The non-gated files still render.
        assert (out_dir / "README.md").is_file()

    def test_should_skip_default_keeps_file(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp", feature=True))
        assert (out_dir / "src" / "feature.txt").read_text() == "feature=True\n"

    def test_validate_runs_before_render(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        with pytest.raises(ValueError):
            scaffolder.render_project(out_dir, _Opts(project_name=""))
        # Nothing should have been written.
        assert not out_dir.exists() or not any(out_dir.rglob("*"))

    def test_missing_scaffolds_dir_raises(self, out_dir):
        scaffolder = _DemoScaffolder(Path("/nonexistent/scaffolds"))
        with pytest.raises(FileNotFoundError):
            scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))

    def test_build_context_not_implemented_by_default(self, scaffold_dir, out_dir):
        bare = ScaffolderBase(scaffold_dir)
        with pytest.raises(NotImplementedError):
            bare.render_project(out_dir, ScaffoldOptions(project_name="MyApp"))


class TestOverwriteSemantics:
    def test_overwrite_false_skips_existing_with_warning(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="First"))
        # Re-render with a different name + overwrite=False: existing
        # files stay, each is reported as skipped.
        outcome = scaffolder.render_project(
            out_dir, _Opts(project_name="Second"), overwrite=False
        )
        assert (out_dir / "README.md").read_text() == "# First\n"
        assert any("README.md" in w for w in outcome.warnings)
        assert outcome.files_written == []

    def test_overwrite_true_replaces_scaffold_files(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="First"))
        scaffolder.render_project(out_dir, _Opts(project_name="Second"))
        assert (out_dir / "README.md").read_text() == "# Second\n"

    def test_idempotent_rerender_same_file_set(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        first = sorted(p.name for p in out_dir.rglob("*") if p.is_file())
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        second = sorted(p.name for p in out_dir.rglob("*") if p.is_file())
        assert first == second

    def test_out_of_surface_files_preserved(self, scaffold_dir, out_dir):
        scaffolder = _DemoScaffolder(scaffold_dir)
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        custom = out_dir / "src" / "user_added.txt"
        custom.write_text("do not clobber\n")
        scaffolder.render_project(out_dir, _Opts(project_name="MyApp"))
        assert custom.read_text() == "do not clobber\n"


def test_template_suffix_constant():
    assert TEMPLATE_SUFFIX == ".j2"
