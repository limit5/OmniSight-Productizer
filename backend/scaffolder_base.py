"""W2-1 (1C #1) — shared scaffolder base + render machinery.

The 12 ``backend/<stack>_scaffolder.py`` modules each re-declared the
same render plumbing — ``ScaffoldOptions`` / ``RenderOutcome`` /
``render_project`` / ``_iter_scaffold_files`` / ``_write_file`` /
``_build_jinja_env`` — roughly 6 kLOC of copy-paste that
``scaffold_reference.py`` (W12.4) flagged at lines 13 / 23 as the
missing shared base. This module extracts that base.

Design
------
* :class:`ScaffoldOptions` — the *common* knob surface: every scaffolder
  needs a ``project_name`` and validates it is non-empty. Per-stack
  scaffolders subclass and add their own knobs (``push`` / ``billing``
  / ``framework`` / …) plus a stricter :meth:`validate`.
* :class:`RenderOutcome` — files written, byte totals, warnings, and a
  ``profile_binding`` dict. Identical to the dataclass the per-stack
  scaffolders shipped, so a migrated scaffolder re-exports this type
  unchanged (``isinstance`` checks in the suites keep passing).
* :class:`ScaffolderBase` — owns the render loop and the three
  byte-level primitives (``_iter_scaffold_files`` / ``_build_jinja_env``
  / ``_write_file``). Per-stack behaviour is injected through three
  overridable hooks:

  - :meth:`should_skip` — knob-gating (default: skip nothing).
  - :meth:`build_context` — the Jinja render context (no default —
    every scaffolder must supply it).
  - :meth:`make_outcome` — seed the :class:`RenderOutcome` (default:
    an empty one; override to stamp ``profile_binding``).

Render contract (preserved byte-for-byte from the per-stack loop)
-----------------------------------------------------------------
* ``.j2`` files are Jinja-rendered with the suffix stripped from the
  destination path; everything else is copied byte-for-byte.
* :meth:`should_skip` receives the *raw* scaffold-relative path (with
  the ``.j2`` suffix still attached for templates) — the gating
  frozensets in the per-stack scaffolders are keyed that way.
* ``overwrite=False`` leaves existing files in place and records a
  ``skipped existing: <path>`` warning. Files outside the scaffold
  surface are never touched (idempotent re-render).

Scope (OP-1784): this module + the ``android`` migration are the
proof-of-pattern; the other 11 scaffolders migrate in a follow-on wave.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import jinja2

logger = logging.getLogger(__name__)

#: Canonical template suffix. A file ending in this is Jinja-rendered;
#: anything else is copied byte-for-byte.
TEMPLATE_SUFFIX = ".j2"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    """Common knob base shared by every scaffolder.

    Every scaffold render is parameterised by at least a project name.
    Per-stack scaffolders subclass this and add their own fields
    (``push`` / ``billing`` / ``framework`` / …); they override
    :meth:`validate` to layer stack-specific rules on top of the
    non-empty ``project_name`` check (call ``super().validate()`` first).
    """

    project_name: str

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")


@dataclass
class RenderOutcome:
    """Result of a scaffold render — files, bytes, warnings, binding."""

    out_dir: Path
    files_written: list[Path] = field(default_factory=list)
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)
    profile_binding: Any = field(default_factory=dict)

    def to_dict(self) -> dict:
        profile_binding = (
            dict(self.profile_binding)
            if isinstance(self.profile_binding, dict)
            else self.profile_binding
        )
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "profile_binding": profile_binding,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Render base
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ScaffolderBase:
    """Shared scaffold render machinery.

    A scaffolder is constructed with its ``scaffolds_dir`` (the root the
    ``.j2`` / static templates live under) and an optional human label
    used in the render log line. Subclasses customise behaviour by
    overriding :meth:`should_skip`, :meth:`build_context`, and
    :meth:`make_outcome`.
    """

    #: Template suffix used by this scaffolder. Override only if a stack
    #: ships templates under a non-``.j2`` extension (none do today).
    template_suffix: str = TEMPLATE_SUFFIX

    def __init__(self, scaffolds_dir: Path, *, skill_label: str = "scaffold") -> None:
        self.scaffolds_dir = Path(scaffolds_dir)
        self.skill_label = skill_label

    # ── Overridable hooks ──────────────────────────────────────────

    def should_skip(self, rel_path: str, options: ScaffoldOptions) -> bool:
        """Return True to omit ``rel_path`` from the render.

        ``rel_path`` is the scaffold-relative POSIX path **including**
        the ``.j2`` suffix for templates. Default: skip nothing.
        """
        return False

    def build_context(self, options: ScaffoldOptions) -> dict[str, Any]:
        """Return the Jinja render context for ``options``.

        No default — every scaffolder defines its own context. The base
        raises so a forgotten override fails loud rather than rendering
        an empty project.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement build_context()"
        )

    def make_outcome(self, out_dir: Path, context: dict[str, Any]) -> RenderOutcome:
        """Seed the :class:`RenderOutcome` before the render loop runs.

        Override to stamp ``profile_binding`` (or other pre-loop state)
        from the resolved ``context``. Default: a bare outcome.
        """
        return RenderOutcome(out_dir=out_dir)

    # ── Byte-level primitives ──────────────────────────────────────

    @staticmethod
    def _iter_scaffold_files(root: Path) -> Iterable[Path]:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                yield path

    def _build_jinja_env(self) -> jinja2.Environment:
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.scaffolds_dir)),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )

    @staticmethod
    def _write_file(dest: Path, content: bytes | str) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            dest.write_text(content, encoding="utf-8")
            return len(content.encode("utf-8"))
        dest.write_bytes(content)
        return len(content)

    # ── Public entry point ─────────────────────────────────────────

    def render_project(
        self,
        out_dir: Path,
        options: ScaffoldOptions,
        *,
        overwrite: bool = True,
    ) -> RenderOutcome:
        """Render the scaffold under :attr:`scaffolds_dir` into ``out_dir``.

        Parameters
        ----------
        out_dir : Path
            Destination project root. Created if missing.
        options : ScaffoldOptions
            Knob values — validated before any file is touched.
        overwrite : bool
            When ``True`` (default), existing files inside the scaffold
            surface are overwritten. Files OUTSIDE the scaffold surface
            are never touched.
        """
        options.validate()
        out_dir = Path(out_dir)
        if not self.scaffolds_dir.is_dir():
            raise FileNotFoundError(
                f"scaffolds directory missing: {self.scaffolds_dir}"
            )

        out_dir.mkdir(parents=True, exist_ok=True)

        env = self._build_jinja_env()
        ctx = self.build_context(options)
        outcome = self.make_outcome(out_dir, ctx)

        suffix = self.template_suffix
        for src in self._iter_scaffold_files(self.scaffolds_dir):
            rel = src.relative_to(self.scaffolds_dir).as_posix()
            if self.should_skip(rel, options):
                continue

            if rel.endswith(suffix):
                out_rel = rel[: -len(suffix)]
                dest = out_dir / out_rel
                if dest.exists() and not overwrite:
                    outcome.warnings.append(f"skipped existing: {out_rel}")
                    continue
                template = env.get_template(rel)
                rendered = template.render(**ctx)
                outcome.bytes_written += self._write_file(dest, rendered)
            else:
                dest = out_dir / rel
                if dest.exists() and not overwrite:
                    outcome.warnings.append(f"skipped existing: {rel}")
                    continue
                outcome.bytes_written += self._write_file(dest, src.read_bytes())
            outcome.files_written.append(dest)

        logger.info(
            "%s rendered %d files (%d bytes) into %s",
            self.skill_label,
            len(outcome.files_written),
            outcome.bytes_written,
            out_dir,
        )
        return outcome


__all__ = [
    "RenderOutcome",
    "ScaffoldOptions",
    "ScaffolderBase",
    "TEMPLATE_SUFFIX",
]
