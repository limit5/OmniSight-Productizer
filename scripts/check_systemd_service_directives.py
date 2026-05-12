"""OP-1028 — reject new systemd service units missing startup directives.

AUDIT-29c defines the daemon-startup contract for newly added ``.service``
files: every unit must declare ``Restart=``, ``StandardOutput=``, and
``ExecStart=`` in its ``[Service]`` section. This script is intentionally
stdlib-only so it can run from pre-commit and CI before project dependencies
are installed.

Default mode checks staged newly-added ``.service`` files. Use ``--all`` in
tests or one-off audits to check the explicit files passed on the command line.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REQUIRED_SERVICE_DIRECTIVES = ("Restart", "StandardOutput", "ExecStart")


def _service_block(unit_text: str) -> str | None:
    match = re.search(r"^\[Service\]\s*\n(.*?)(?=^\[|\Z)", unit_text, re.S | re.M)
    if match is None:
        return None
    return match.group(1)


def _has_directive(service_block: str, directive: str) -> bool:
    return (
        re.search(rf"^{re.escape(directive)}\s*=", service_block, re.M)
        is not None
    )


def check_file(path: Path) -> list[str]:
    """Return validation errors for one systemd service file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: cannot read file ({exc})"]

    block = _service_block(text)
    if block is None:
        return [f"{path}: missing [Service] section"]

    missing = [
        directive
        for directive in REQUIRED_SERVICE_DIRECTIVES
        if not _has_directive(block, directive)
    ]
    if not missing:
        return []

    return [
        f"{path}: [Service] missing required directive(s): "
        f"{', '.join(f'{directive}=' for directive in missing)}"
    ]


def _staged_added_service_files() -> set[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=A", "--", "*.service"],
        cwd=Path.cwd(),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return set()
    return {Path(line) for line in result.stdout.splitlines() if line}


def _candidate_files(files: list[str], check_all: bool) -> list[Path]:
    paths = [Path(file) for file in files if file.endswith(".service")]
    if check_all:
        return paths

    staged_added = _staged_added_service_files()
    if not paths:
        return sorted(staged_added)

    return [path for path in paths if path in staged_added]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate new systemd .service files declare required directives."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="check all explicit files instead of only staged added files",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)

    errors: list[str] = []
    for path in _candidate_files(args.files, args.all):
        errors.extend(check_file(path))

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
