#!/usr/bin/env python3
"""W3 (1C) #1787 — unified ``scripts/scaffold.py`` skill→scaffolder dispatcher.

``backend/scaffold_reference.py:13,23`` documents this module as the
*future unified ``scripts/scaffold.py`` dispatcher* that turns a skill
name into the matching ``backend/<stack>_scaffolder.py`` entry point.
Before this, every scaffolder shipped its own CLI and ``skill.yaml`` was
decorative for dispatch. This is the single entry point that makes
"render skill X into directory Y" one command, manifest-gated.

Resolution lives in :func:`backend.skill_registry.resolve_scaffolder`,
which reads ``skill.yaml`` and refuses to dispatch a pack that does not
declare a ``scaffolds`` artifact. This script is the thin CLI on top:
it introspects the resolved scaffolder's ``ScaffoldOptions`` dataclass
to build per-knob flags dynamically, so it dispatches to the *existing*
scaffolders with no change to their output (additive only).

Usage
-----
::

    scripts/scaffold.py --list
    scripts/scaffold.py skill-android --out-dir ./PilotApp --project-name PilotApp
    scripts/scaffold.py skill-android -o ./App --project-name App --no-billing
    scripts/scaffold.py skill-ios -o ./App --project-name App --json

Each scaffolder declares its own knobs (``push`` / ``billing`` /
``framework`` / …); the flags are generated from the dataclass fields:
``bool`` fields become ``--knob/--no-knob`` and string fields become
``--knob VALUE``. ``--project-name`` is required (it is the one field
with no default on every scaffolder).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.skill_registry import (  # noqa: E402  (after sys.path shim)
    ScaffolderHandle,
    ScaffolderResolutionError,
    list_scaffoldable_skills,
    resolve_scaffolder,
)

_MISSING = dataclasses.MISSING


def _field_default(f: dataclasses.Field) -> tuple[bool, Any]:
    """Return ``(has_default, default_value)`` for a dataclass field."""
    if f.default is not _MISSING:
        return True, f.default
    if f.default_factory is not _MISSING:  # type: ignore[misc]
        return True, f.default_factory()  # type: ignore[misc]
    return False, None


def _is_bool_field(f: dataclasses.Field, default: Any) -> bool:
    """Best-effort detection of a boolean knob.

    Under ``from __future__ import annotations`` (every scaffolder uses
    it) ``f.type`` is a *string* like ``"bool"`` / ``"str"`` /
    ``"Optional[str]"``, so we lean on both the annotation text and the
    default's runtime type.
    """
    if isinstance(default, bool):
        return True
    type_str = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
    return type_str == "bool"


def build_options_parser(
    options_cls: type, skill_name: str
) -> argparse.ArgumentParser:
    """Build a sub-parser whose flags mirror ``options_cls`` fields.

    One flag per dataclass field: ``bool`` → ``--knob/--no-knob``
    (``BooleanOptionalAction``), everything else → ``--knob VALUE``.
    Fields with no default (``project_name``) are ``required``.
    """
    parser = argparse.ArgumentParser(
        prog=f"scaffold.py {skill_name}",
        description=f"Knobs for {skill_name} ({options_cls.__module__}.{options_cls.__name__})",
        add_help=False,
    )
    for f in dataclasses.fields(options_cls):
        flag = "--" + f.name.replace("_", "-")
        has_default, default = _field_default(f)
        if _is_bool_field(f, default):
            parser.add_argument(
                flag,
                dest=f.name,
                action=argparse.BooleanOptionalAction,
                default=default if has_default else None,
                help=f"(default: {default})" if has_default else None,
            )
        else:
            parser.add_argument(
                flag,
                dest=f.name,
                default=default,
                required=not has_default,
                metavar=f.name.upper(),
                help=f"(default: {default!r})" if has_default else "(required)",
            )
    return parser


def build_options(
    handle: ScaffolderHandle, knob_argv: Sequence[str]
) -> Any:
    """Parse ``knob_argv`` into an instance of the scaffolder's options."""
    parser = build_options_parser(handle.options_cls, handle.skill_name)
    ns = parser.parse_args(list(knob_argv))
    kwargs = {f.name: getattr(ns, f.name) for f in dataclasses.fields(handle.options_cls)}
    return handle.options_cls(**kwargs)


def _outcome_to_dict(outcome: Any) -> dict:
    if hasattr(outcome, "to_dict"):
        return outcome.to_dict()
    # Fallback for an outcome without to_dict — best-effort summary.
    return {
        "out_dir": str(getattr(outcome, "out_dir", "")),
        "files_written": [str(p) for p in getattr(outcome, "files_written", [])],
        "bytes_written": getattr(outcome, "bytes_written", 0),
        "warnings": list(getattr(outcome, "warnings", [])),
    }


def run_scaffold(
    skill_name: str,
    out_dir: Path,
    knob_argv: Sequence[str],
    *,
    overwrite: bool = True,
    skills_dir: Path | None = None,
) -> dict:
    """Resolve ``skill_name`` and render it into ``out_dir``.

    Returns the render outcome as a dict. Raises
    :class:`ScaffolderResolutionError` if the skill cannot be bound and
    ``ValueError`` if the knob values fail the scaffolder's
    ``ScaffoldOptions.validate()``.
    """
    handle = resolve_scaffolder(skill_name, skills_dir)
    options = build_options(handle, knob_argv)
    outcome = handle.render(out_dir, options, overwrite=overwrite)
    return _outcome_to_dict(outcome)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    top = argparse.ArgumentParser(
        prog="scaffold.py",
        description=(
            "Unified skill→scaffolder dispatcher. Resolves a skill name to "
            "its scaffolder via backend.skill_registry (reads skill.yaml) "
            "and renders it into --out-dir."
        ),
        add_help=True,
    )
    top.add_argument(
        "skill",
        nargs="?",
        help="Skill pack name to scaffold (e.g. skill-android). "
        "Omit with --list to enumerate scaffoldable packs.",
    )
    top.add_argument(
        "-l", "--list",
        action="store_true",
        help="List installed packs that declare a scaffolder and exit.",
    )
    top.add_argument(
        "-o", "--out-dir",
        type=Path,
        help="Destination project root (created if missing).",
    )
    top.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Leave existing files in place (records a skipped warning) "
        "instead of overwriting scaffold files.",
    )
    top.add_argument(
        "--json",
        action="store_true",
        help="Emit the render outcome as JSON instead of a human summary.",
    )

    # Split top-level args from per-skill knob flags. parse_known_args
    # lets the dynamically-built options parser own the remaining flags.
    args, knob_argv = top.parse_known_args(argv)

    if args.list:
        names = list_scaffoldable_skills()
        if args.json:
            print(json.dumps({"scaffoldable": names}, indent=2))
        else:
            for name in names:
                print(name)
        return 0

    if not args.skill:
        top.error("a skill name is required (or pass --list)")
    if args.out_dir is None:
        top.error("--out-dir/-o is required when scaffolding a skill")

    try:
        result = run_scaffold(
            args.skill,
            args.out_dir,
            knob_argv,
            overwrite=not args.no_overwrite,
        )
    except ScaffolderResolutionError as exc:
        print(f"scaffold: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        # ScaffoldOptions.validate() rejected the knob values.
        print(f"scaffold: invalid options for {args.skill}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"scaffolded {args.skill} → {result['out_dir']} "
            f"({len(result['files_written'])} files, "
            f"{result['bytes_written']} bytes)"
        )
        for warning in result.get("warnings", []):
            print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
