"""BP.G.2 -- validate Gerrit ``Depends-On`` commit-message trailers.

Gerrit accepts dependency declarations as commit-message footers:

    Depends-On: I0123456789abcdef0123456789abcdef01234567

This hook is intentionally local and syntax-only. It does not query Gerrit,
because commit creation should remain fast and offline-capable; submit-time
existence / mergeability checks belong to the Gerrit submit rule.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CHANGE_ID_RE = re.compile(r"^I[0-9a-fA-F]{40}$")
TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]*(.*)$")
DEPENDS_ON_KEY = "Depends-On"
CHANGE_ID_KEY = "Change-Id"


@dataclass(frozen=True)
class ValidationError:
    """One commit-message validation failure."""

    line: int
    message: str


def validate_message(message: str) -> list[ValidationError]:
    """Return validation errors for Gerrit ``Depends-On`` trailers.

    Rules:
    * no ``Depends-On`` footer is valid;
    * every present value must be a Gerrit Change-Id (``I`` + 40 hex chars);
    * duplicate dependency values are rejected;
    * a commit may not depend on its own ``Change-Id``.
    """

    trailers = _parse_trailers(message)
    change_ids = [
        value
        for key, value, _line in trailers
        if key.lower() == CHANGE_ID_KEY.lower() and value
    ]
    own_change_id = change_ids[-1].lower() if change_ids else None

    errors: list[ValidationError] = []
    seen_depends_on: dict[str, int] = {}
    for key, value, line in trailers:
        if key.lower() != DEPENDS_ON_KEY.lower():
            continue
        if key != DEPENDS_ON_KEY:
            errors.append(
                ValidationError(
                    line,
                    f"use exact trailer name '{DEPENDS_ON_KEY}', got '{key}'",
                )
            )
        if not value:
            errors.append(ValidationError(line, "Depends-On value is empty"))
            continue
        if not CHANGE_ID_RE.fullmatch(value):
            errors.append(
                ValidationError(
                    line,
                    "Depends-On value must be a Gerrit Change-Id "
                    "(I followed by 40 hex characters)",
                )
            )
            continue

        dep_key = value.lower()
        if dep_key in seen_depends_on:
            errors.append(
                ValidationError(
                    line,
                    f"duplicate Depends-On value first declared on line "
                    f"{seen_depends_on[dep_key]}",
                )
            )
            continue
        seen_depends_on[dep_key] = line

        if own_change_id is not None and dep_key == own_change_id:
            errors.append(
                ValidationError(line, "Depends-On may not reference Change-Id")
            )

    return errors


def _parse_trailers(message: str) -> list[tuple[str, str, int]]:
    """Parse the final Git trailer block from ``message``.

    This mirrors Git's footer shape closely enough for local validation:
    scan upward from the end, ignore commit-template comments, stop at the
    first blank line before the contiguous trailer block.
    """

    lines = message.splitlines()
    end = len(lines)
    while end > 0 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1

    start = end
    trailers: list[tuple[str, str, int]] = []
    while start > 0:
        raw = lines[start - 1]
        if raw.startswith("#"):
            start -= 1
            continue
        if not raw.strip():
            break
        match = TRAILER_RE.fullmatch(raw)
        if not match:
            trailers.clear()
            break
        trailers.append((match.group(1), match.group(2).strip(), start))
        start -= 1

    trailers.reverse()
    return trailers


def format_errors(errors: Iterable[ValidationError]) -> str:
    """Return human-readable validation output for stderr / CI logs."""

    return "\n".join(
        f"gerrit_depends_on: line {err.line}: {err.message}" for err in errors
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.hooks.gerrit_depends_on",
        description="Validate Gerrit Depends-On commit-message trailers.",
    )
    parser.add_argument(
        "commit_msg",
        type=Path,
        help="Path to the commit message file, as passed by git commit-msg.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        message = args.commit_msg.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"gerrit_depends_on: cannot read {args.commit_msg}: {exc}\n")
        return 2

    errors = validate_message(message)
    if errors:
        sys.stderr.write(format_errors(errors) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
