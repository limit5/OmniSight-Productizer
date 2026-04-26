"""BS.1.3 — JSON-Schema lint for ``configs/embedded_catalog/*.yaml``.

Loads ``configs/embedded_catalog/_schema.yaml`` (the JSON-Schema
authored alongside the data) and validates every other ``*.yaml`` in
the directory against it.  Wired into CI as a hard gate
(``.github/workflows/ci.yml`` → ``catalog-schema``) so a malformed
operator-edited entry surfaces in the PR sidebar before merge.

What this script enforces (alongside the schema's structural rules):

  1. Every per-family yaml validates against the schema.
  2. The file's stem matches the file's top-level ``family``
     (mobile.yaml carries family: mobile, etc.).
  3. ``id`` values are unique across the *entire* catalog (cross-file
     collision is the most plausible operator-edit mistake — two
     people add ``nodejs-lts-20`` simultaneously, one to web.yaml,
     one to software.yaml).
  4. Every ``depends_on`` target resolves to a known catalog id
     (typo guard — references a non-existent dependency).

These four invariants are deliberately split between the schema (#1)
and this script (#2, #3, #4) because JSON Schema can't express
cross-file constraints cleanly without ``$dynamicRef`` gymnastics.

Why a CI lint and not just BS.1.5's drift-guard test:
The drift guard (``backend/tests/test_catalog_schema.py``) runs in
the ``backend-tests`` shard which takes minutes and lives behind a
multi-job dependency.  This script runs in <2s with stdlib + yaml +
jsonschema, fits in the ``lockfile-drift`` slot, and gives the
catalog-editor a quick, focused error message ("size_bytes must be
integer, got string") instead of a wall of pytest output.

Exit codes:
    0 — every yaml validates, every cross-file invariant holds
    1 — one or more validation failures (CI-red)
    2 — script-environment error (yaml/jsonschema missing, paths
        unreadable, schema itself malformed)

Usage:
    python3 scripts/check_catalog_schema.py
    python3 scripts/check_catalog_schema.py --root configs/embedded_catalog
    python3 scripts/check_catalog_schema.py --only mobile.yaml web.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    print(f"ERROR: PyYAML required but not importable ({exc})",
          file=sys.stderr)
    sys.exit(2)

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError as exc:
    print(f"ERROR: jsonschema required but not importable ({exc})",
          file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / "configs" / "embedded_catalog"
SCHEMA_FILENAME = "_schema.yaml"


def _load_yaml(path: Path) -> Any:
    """Load a single yaml file, propagating yaml errors with file context."""
    with path.open("r", encoding="utf-8") as handle:
        try:
            return yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            print(f"::error file={path}::yaml parse error: {exc}",
                  file=sys.stderr)
            raise


def _format_validation_error(
    path: Path, error: jsonschema.ValidationError
) -> str:
    """Render a ValidationError as a GitHub Actions ``::error::`` line.

    JSON-Schema errors carry an ``absolute_path`` deque — render it
    as ``family.entries[3].install_method`` so the operator sees
    exactly which entry failed without scrolling.
    """
    if error.absolute_path:
        crumbs: list[str] = []
        for part in error.absolute_path:
            if isinstance(part, int):
                crumbs.append(f"[{part}]")
            else:
                crumbs.append(f".{part}" if crumbs else str(part))
        location = "".join(crumbs)
    else:
        location = "<root>"
    return (
        f"::error file={path}::schema violation at {location}: "
        f"{error.message}"
    )


def _check_family_matches_filename(
    path: Path, doc: dict[str, Any]
) -> list[str]:
    """One file = one family; the file stem must equal ``doc['family']``.

    The schema can't express this (it has no access to the filename),
    so the script enforces it.  Mismatch is the single most likely
    operator mistake — copy-pasting an entry from one family file to
    another and forgetting to change the top-level ``family:`` key.
    """
    expected_family = path.stem
    actual_family = doc.get("family")
    if actual_family != expected_family:
        return [
            f"::error file={path}::family mismatch: filename stem "
            f"is '{expected_family}' but top-level family is "
            f"'{actual_family}' — rename the file or fix the family key"
        ]
    return []


def _check_cross_file_invariants(
    docs: dict[Path, dict[str, Any]],
) -> list[str]:
    """Cross-file checks: id uniqueness + depends_on resolution.

    Run once after every file has parsed and validated individually,
    so we never report duplicate-id or missing-dep on a file that
    already failed the structural lint (those errors would be noise).
    """
    errors: list[str] = []

    id_to_path: dict[str, Path] = {}
    for path, doc in docs.items():
        for entry in doc.get("entries", []):
            entry_id = entry.get("id")
            if not isinstance(entry_id, str):
                continue  # already reported by the structural lint
            if entry_id in id_to_path:
                errors.append(
                    f"::error file={path}::duplicate entry id "
                    f"'{entry_id}' — also defined in "
                    f"{id_to_path[entry_id]}"
                )
            else:
                id_to_path[entry_id] = path

    known_ids = set(id_to_path)
    for path, doc in docs.items():
        for entry in doc.get("entries", []):
            entry_id = entry.get("id", "<unknown>")
            for dep in entry.get("depends_on", []) or []:
                if not isinstance(dep, str):
                    continue
                if dep not in known_ids:
                    errors.append(
                        f"::error file={path}::entry '{entry_id}' "
                        f"depends_on '{dep}' which is not defined in "
                        f"any catalog yaml"
                    )

    return errors


def _list_target_files(root: Path, only: list[str] | None) -> list[Path]:
    """Return the yaml files to lint, excluding the schema file itself."""
    if only:
        targets = [root / name for name in only]
        for target in targets:
            if not target.exists():
                print(f"ERROR: {target} not found", file=sys.stderr)
                sys.exit(2)
        return targets
    return sorted(
        p for p in root.glob("*.yaml") if p.name != SCHEMA_FILENAME
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT,
        help="Catalog directory containing _schema.yaml + family yamls",
    )
    parser.add_argument(
        "--only", nargs="*",
        help="Restrict to specific filenames (relative to --root)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Reserved for future tightening (currently a no-op — "
             "every check is already a hard gate).",
    )
    args = parser.parse_args(argv)

    schema_path = args.root / SCHEMA_FILENAME
    if not schema_path.exists():
        print(f"ERROR: schema file not found at {schema_path}",
              file=sys.stderr)
        return 2

    try:
        schema = _load_yaml(schema_path)
    except yaml.YAMLError:
        return 2

    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        print(f"ERROR: {schema_path} is itself an invalid JSON Schema: "
              f"{exc}", file=sys.stderr)
        return 2

    validator = Draft202012Validator(schema)

    targets = _list_target_files(args.root, args.only)
    if not targets:
        print(f"ERROR: no yaml files found under {args.root}",
              file=sys.stderr)
        return 2

    error_lines: list[str] = []
    parsed_docs: dict[Path, dict[str, Any]] = {}

    for path in targets:
        try:
            doc = _load_yaml(path)
        except yaml.YAMLError:
            error_lines.append(f"::error file={path}::yaml parse error")
            continue

        if not isinstance(doc, dict):
            error_lines.append(
                f"::error file={path}::top-level yaml must be a "
                f"mapping; got {type(doc).__name__}"
            )
            continue

        validation_errors = sorted(
            validator.iter_errors(doc),
            key=lambda e: tuple(str(p) for p in e.absolute_path),
        )
        if validation_errors:
            for err in validation_errors:
                error_lines.append(_format_validation_error(path, err))
            # Skip cross-file checks for files that didn't structurally
            # validate — duplicate-id reports on a file that's already
            # missing required keys are noise.
            continue

        error_lines.extend(_check_family_matches_filename(path, doc))
        parsed_docs[path] = doc

    error_lines.extend(_check_cross_file_invariants(parsed_docs))

    if error_lines:
        for line in error_lines:
            print(line)
        print(
            f"\ncatalog-schema: {len(error_lines)} violation(s) "
            f"across {len(targets)} file(s) — see ::error:: lines above",
            file=sys.stderr,
        )
        return 1

    print(
        f"catalog-schema: OK ({len(targets)} file(s), "
        f"{sum(len(d.get('entries', [])) for d in parsed_docs.values())} "
        f"entry(ies) validated)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
