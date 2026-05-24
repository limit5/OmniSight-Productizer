#!/usr/bin/env python3
"""dag-executor smoke fixture run-test step (OP-1673).

Consumes the compile task's artifact (build/firmware.bin) and writes a
self-test log to logs/test.log — the run-test task's declared
expected_output. Runs in the task's scratch workspace (cwd), where
the dag-executor has materialised both build/firmware.bin (the upstream
compile output, staged by the executor) and this script (an
external: source seed).

Exits non-zero if the firmware artifact is missing, so a broken
inter-task hand-off FAILS the run-test task honestly rather than
writing a green log over nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FIRMWARE = Path("build/firmware.bin")
LOG = Path("logs/test.log")


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)

    if not FIRMWARE.exists():
        LOG.write_text(
            "FAIL: expected upstream artifact build/firmware.bin not found "
            "in workspace\n"
        )
        print(f"run_test: missing {FIRMWARE}", file=sys.stderr)
        return 1

    size = FIRMWARE.stat().st_size
    executable = os.access(FIRMWARE, os.X_OK)
    LOG.write_text(
        "PASS: build/firmware.bin present "
        f"(size={size} bytes, executable={executable})\n"
    )
    print(f"run_test: PASS — firmware {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
