"""M1 — cgroup v2 verification helper for per-tenant CPU weight.

Reads `cpu.weight` (cgroup v2) or `cpu.shares` (cgroup v1) for a
running container and lets the test harness assert that the kernel
actually scheduled CPU time in the expected ratio.

Used by `tests/test_container_tenant_budget.py` and the operator
diagnostic CLI:

    python -m backend.cgroup_verify <container1> <container2>

Returns exit code 0 on ratio-within-tolerance, 1 on miss, 2 on error.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Tolerance for the live ratio test. 20% slack covers the noise from
# the scheduler period boundaries (CFS bandwidth control runs in 100ms
# windows by default) and a single host-side housekeeping process.
DEFAULT_TOLERANCE = 0.20


async def _container_id(name_or_id: str) -> str | None:
    """Resolve a container name to its full sha256 ID."""
    proc = await asyncio.create_subprocess_shell(
        f"docker inspect --format '{{{{.Id}}}}' {name_or_id}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="replace").strip().strip("'\"") or None


def _candidate_cgroup_paths(cid: str) -> list[Path]:
    """Layouts the daemon may write to. Order = most likely first."""
    return [
        Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"),
        Path(f"/sys/fs/cgroup/docker/{cid}"),
        # cgroupns + nested user.slice (rootless docker)
        Path(f"/sys/fs/cgroup/user.slice/user-1000.slice/"
             f"user@1000.service/app.slice/docker-{cid}.scope"),
    ]


def _read_weight_at(path: Path) -> tuple[int | None, str]:
    """Return (weight, source) for one cgroup directory.

    `source` distinguishes v2 ("cpu.weight": 1..10000) from v1
    ("cpu.shares": 2..262144). Returns (None, "") if neither exists.
    """
    v2 = path / "cpu.weight"
    if v2.is_file():
        try:
            return int(v2.read_text().strip()), "cpu.weight"
        except Exception:
            return None, ""
    v1 = path / "cpu.shares"
    if v1.is_file():
        try:
            return int(v1.read_text().strip()), "cpu.shares"
        except Exception:
            return None, ""
    return None, ""


async def read_cpu_weight(name_or_id: str) -> tuple[int | None, str]:
    """Return (weight, source) for a running container, or (None, "")."""
    cid = await _container_id(name_or_id)
    if cid is None:
        return None, ""
    for cand in _candidate_cgroup_paths(cid):
        if cand.is_dir():
            w, src = _read_weight_at(cand)
            if w is not None:
                return w, src
    return None, ""


async def verify_weight_ratio(
    name_a: str,
    name_b: str,
    expected_ratio: float,
    tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[bool, float, dict]:
    """Compare cpu.weight(a) / cpu.weight(b) to *expected_ratio*.

    Returns (ok, actual_ratio, details). `details` carries the raw
    weights + source so the test harness can print a useful failure.
    """
    wa, sa = await read_cpu_weight(name_a)
    wb, sb = await read_cpu_weight(name_b)
    details = {
        "container_a": name_a, "weight_a": wa, "source_a": sa,
        "container_b": name_b, "weight_b": wb, "source_b": sb,
        "expected_ratio": expected_ratio, "tolerance": tolerance,
    }
    if wa is None or wb is None or wb == 0:
        return False, 0.0, details
    actual = wa / wb
    ok = abs(actual - expected_ratio) <= expected_ratio * tolerance
    details["actual_ratio"] = actual
    return ok, actual, details


def _main() -> int:
    if len(sys.argv) < 3:
        print("usage: python -m backend.cgroup_verify "
              "<container_a> <container_b> [expected_ratio=4.0] "
              "[tolerance=0.2]", file=sys.stderr)
        return 2
    a, b = sys.argv[1], sys.argv[2]
    expected = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    tolerance = float(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_TOLERANCE
    ok, actual, details = asyncio.run(
        verify_weight_ratio(a, b, expected, tolerance)
    )
    import json as _json
    print(_json.dumps({
        "ok": ok, "actual_ratio": actual, **details,
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
