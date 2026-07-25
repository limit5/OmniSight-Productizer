#!/usr/bin/env python3
"""OP-2728 AC4 / OP-2734 — host free-space floor, routed to the JIRA alert channel.

WHY THIS IS NOT A PROMETHEUS ALERTING RULE
------------------------------------------
The prod Prometheus loads rules from an EXPLICIT single-entry `rule_files` list
(`/etc/prometheus/obs-rules/project_state_health.yml`) inside
`/home/user/omnisight-prod/configs/prometheus.yml` — and that whole tree is the
RELEASE-PINNED prod checkout. Adding an operator-owned rule there would mean either
dirtying the pinned checkout (which hard-blocks `deploy-prod.sh:96` AND emergency
digest rollback) or mounting a replacement `prometheus.yml` that silently masks the
release's own config. Both are the invisible-drift class this sweep exists to remove.
Two rule files already sit unloaded in that directory (`alerts.yml`,
`frontend-freshness-alerts.yml`) — a live example of the same failure mode.

So the threshold lives here, operator-owned, decoupled from the release artifact,
and reaches the same idempotent JIRA channel as systemd unit failures. This unit's
OWN failure is wired to that channel too, so the watcher is watched.

BACKGROUND: on 2026-07-20 the filesystem climbed 87.5% -> 100% over nine days with
no floor and no alarm, filling the disk and killing pipeline-coordinator (ENOSPC).
A floor at 85% would have alerted roughly nine days before the outage.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROM = os.environ.get("OMNISIGHT_PROM_URL", "http://localhost:9090")
METRIC = "omnisight_host_disk_percent"
WARN_PCT = float(os.environ.get("DISK_WARN_PCT", "85"))
CRIT_PCT = float(os.environ.get("DISK_CRIT_PCT", "92"))
SOURCE = "host-disk"

# Sibling resolution, not an absolute path: the two files always ship together
# (both in the repo's scripts/, both installed into ~/.local/bin), so the repo copy
# and the installed copy stay byte-identical and a drift check is a sha256 compare.
_spec = importlib.util.spec_from_file_location(
    "alert_notify", str(Path(__file__).resolve().parent / "omnisight-alert-notify.py")
)
alert = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alert)


def _sh(cmd: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"<{cmd[0]}: {exc}>"


def current_pct() -> float | None:
    url = f"{PROM}/api/v1/query?query={urllib.parse.quote(METRIC)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    results = data.get("data", {}).get("result") or []
    values = [float(r["value"][1]) for r in results if r.get("value")]
    return max(values) if values else None


def evidence() -> str:
    """Actionable context, so the ticket answers 'what is eating the disk?'."""
    return (
        "df -h /\n" + _sh(["df", "-h", "/"])
        + "\ndocker system df\n" + _sh(["docker", "system", "df"])
        + "\nlargest under /home/user (depth 1)\n"
        + _sh(["bash", "-c", "du -x -d1 /home/user 2>/dev/null | sort -rn | head -12"], timeout=120)
    )


def main() -> int:
    try:
        pct = current_pct()
    except Exception as exc:  # noqa: BLE001
        # Prometheus itself unreachable is a real signal, not a reason to die quietly.
        alert.notify_source(
            f"{SOURCE}-probe",
            f"host disk floor check cannot reach Prometheus at {PROM}",
            f"{type(exc).__name__}: {exc}",
            priority="Medium",
        )
        return 0

    if pct is None:
        alert.notify_source(
            f"{SOURCE}-probe",
            f"metric {METRIC} returned no series",
            f"Prometheus responded but {METRIC} has no samples. Are the backend targets up?",
            priority="Medium",
        )
        return 0

    alert.clear_source(f"{SOURCE}-probe")

    if pct >= CRIT_PCT:
        os.environ["ALERT_THROTTLE_SECONDS"] = "3600"
        alert.THROTTLE_S = 3600
        alert.notify_source(
            SOURCE,
            f"host disk at {pct:.1f}% — CRITICAL (floor {CRIT_PCT}%)",
            f"{METRIC} = {pct:.1f}%\n\n{evidence()}",
            priority="Highest",
        )
    elif pct >= WARN_PCT:
        alert.THROTTLE_S = 21600  # 6h — a slow climb does not need hourly comments
        alert.notify_source(
            SOURCE,
            f"host disk at {pct:.1f}% — above floor ({WARN_PCT}%)",
            f"{METRIC} = {pct:.1f}%\n\n{evidence()}",
            priority="High",
        )
    else:
        # Back under the floor: drop the throttle so the next breach alerts at once.
        alert.clear_source(SOURCE)
    print(f"{METRIC}={pct:.1f} warn={WARN_PCT} crit={CRIT_PCT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
