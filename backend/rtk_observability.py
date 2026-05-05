"""BP.R.6 — RTK Prometheus metric publishers.

This module owns the small bits of runtime logic behind RTK metrics in
``backend.metrics``. Import has no side effects; startup calls
``probe_install_status()`` once per worker and Prometheus aggregates the
per-worker gauges/counters across replicas.

Module-global state audit: no mutable process state is stored here. Each
worker probes the same RTK binary path independently and publishes its
own in-process Prometheus sample.

Read-after-write timing audit: N/A. The module only executes a local
``rtk --version`` probe and updates Prometheus collectors.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable

from backend import metrics as _metrics
from backend.config import settings

logger = logging.getLogger(__name__)

RunFn = Callable[..., subprocess.CompletedProcess[str]]


def set_install_status(installed: bool) -> bool:
    """Publish ``omnisight_rtk_install_status`` and return the same bool."""

    _metrics.rtk_install_status.set(1 if installed else 0)
    return installed


def probe_install_status(*, runner: RunFn = subprocess.run) -> bool:
    """Probe whether the RTK binary is available and runnable."""

    try:
        result = runner(
            ["rtk", "--version"],
            capture_output=True,
            text=True,
            timeout=settings.rtk_binary_timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("RTK install probe failed: %s", exc)
        return set_install_status(False)

    installed = result.returncode == 0
    if not installed:
        logger.warning(
            "RTK install probe returned rc=%s stderr=%s",
            result.returncode,
            (result.stderr or "").strip()[:200],
        )
    return set_install_status(installed)
