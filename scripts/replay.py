#!/usr/bin/env python3
"""Deprecation shim for the legacy ``scripts/replay_*.py`` family.

The OP-820 / OP-830 replay mechanism (CLI-driven re-run of a failed
runner ticket) is superseded by OP-854's failure-class-indexed memory
recall — the runner now auto-injects prior incidents at pickup time
instead of relying on a manual replay step.

This module exists so any cron / systemd unit / operator muscle memory
that still invokes ``python scripts/replay.py …`` (or imports a name
from the legacy family) sees a clear deprecation warning pointing at
the new path. Removal is scheduled 30 days after the OP-854 merge —
see ``docs/operations/replay-deprecation.md`` for the cut-over plan.

The legacy entry-point intentionally returns a nonzero exit code so
automation that depends on a successful replay run fails loudly during
the deprecation window; this is the cue for the operator to switch to
the C2 recall path.
"""
from __future__ import annotations

import sys
import warnings

DEPRECATION_MESSAGE = (
    "scripts/replay*.py is deprecated by OP-854 (failure-class-indexed "
    "memory recall). The runner now auto-injects prior incidents at "
    "pickup; manual replay is no longer needed. Removal scheduled 30 "
    "days post-merge. See docs/operations/replay-deprecation.md."
)


def emit_deprecation_warning() -> None:
    """Emit the canonical DeprecationWarning for the legacy entry-point."""
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)


def main(argv: list[str] | None = None) -> int:
    emit_deprecation_warning()
    sys.stderr.write(f"[replay-deprecation] {DEPRECATION_MESSAGE}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
