#!/usr/bin/env python3
"""[OP-965] AUDIT-17 — CLI shim for the continuous staging gate emitters.

Thin wrapper around :mod:`backend.agents.staging_gate` so the systemd
timers (``deploy/systemd/staging-gate-canary.{service,timer}`` and
``staging-gate-smoke.{service,timer}``) have a stable ``scripts/`` entry
point, mirroring the ``scripts/auto_promote_develop_to_main.sh`` →
``backend.agents.auto_promote_main`` split.

Usage::

    python3 scripts/staging_gate.py --suite canary
    python3 scripts/staging_gate.py --suite smoke  [--base-url https://staging.sora.services]

Exit codes: 0 = suite green, 2 = suite red (a JSONL line is still written),
3 = configuration / environment error (e.g. cannot resolve develop tip).
See the module docstring for the full record contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents.staging_gate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
