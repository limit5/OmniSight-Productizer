"""Deprecated OP-772 SLO monitor entry point.

The canonical production SLO monitor is
``backend.orchestrator.slo_monitor`` (OP-883). This module remains as a
compatibility shim for old imports and manual commands.
"""

from __future__ import annotations

from backend.orchestrator.slo_monitor import *  # noqa: F403
from backend.orchestrator.slo_monitor import main


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
