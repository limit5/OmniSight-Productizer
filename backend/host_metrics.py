"""M4 — Cgroup-based per-container + per-tenant metrics.

Samples cgroup v2 pseudo-files for every docker-launched container whose
name matches ``omnisight-agent-*`` and aggregates the readings by the
``tenant_id`` docker label (stamped on launch in ``container.py``):

    /sys/fs/cgroup/system.slice/docker-<cid>.scope/cpu.stat
    /sys/fs/cgroup/system.slice/docker-<cid>.scope/memory.current

On cgroup v1 hosts (legacy / WSL2 without v2 enabled) the module falls
back to ``docker stats --no-stream`` so the public API keeps returning
sensible numbers; samplers simply won't be able to hit the sub-second
granularity that v2 affords.

Public surface — callers should treat these as the single source of
truth for per-tenant resource state:

    - ``sample_once()``            → one-shot scrape → list[ContainerSample]
    - ``aggregate_by_tenant()``    → CPU%/mem/disk/sandbox_count per tenant
    - ``get_tenant_usage()``       → cached latest aggregation
    - ``get_culprit_tenant()``     → AIMD helper: whose CPU is the outlier?
    - ``run_sampling_loop()``      → lifespan task; samples + bumps gauges

Usage accounting (billing feed) sits alongside:

    - ``accumulate_usage()``       → updates cpu_seconds + mem_gb_seconds
    - ``snapshot_accounting()``    → read-only view for the report script
    - ``reset_accounting()``       → test helper

Design notes:
  * CPU% is computed from the *delta* of ``usage_usec`` between samples,
    divided by wall-clock delta, times 100. First sample primes the
    state and returns 0% for that tenant.
  * Memory is an instantaneous read (``memory.current`` on v2). It is
    *not* usage over time; ``mem_gb_seconds`` in accounting derives from
    integrating this value over the sample interval.
  * Disk usage comes from ``tenant_quota.measure_tenant_usage`` — the
    same source that drives M2's quota gate so numbers don't diverge.
  * Sampling loop intentionally swallows exceptions: a scrape failure
    should not tear down the backend. We log + count in
    ``metrics.persist_failure_total{module="host_metrics"}``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None  # type: ignore[assignment]

try:
    import docker as docker_sdk  # type: ignore[import-not-found]
except ImportError:
    docker_sdk = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CGROUP_ROOT = Path("/sys/fs/cgroup")
SAMPLE_INTERVAL_S = 5.0
HOST_HISTORY_SIZE = 60
"""Ring-buffer depth for the host-level sampling loop. At the 5s
``SAMPLE_INTERVAL_S`` cadence this holds 5 minutes of snapshots — long
enough for AIMD and the observability runbook to distinguish a single
noisy tick from a sustained overload, short enough that the whole
buffer fits in a few tens of KB and a fresh dashboard load still
finishes within one SSE frame."""

CULPRIT_CPU_MARGIN_PCT = 150.0
"""When overall host CPU is hot, derate *only* the tenant whose CPU% is
at least this margin above the next-highest tenant. Below this margin
the decision falls back to a flat multi-tenant derate."""

CULPRIT_MIN_CPU_PCT = 80.0
"""A tenant must itself be above this CPU% before it can be flagged as a
culprit. Prevents derating a quiet tenant when the host is being
pegged by a non-containerised workload."""

HIGH_PRESSURE_LOADAVG_RATIO = 0.9
"""WSL2 auxiliary high-pressure threshold on the normalised 1-minute
load average (``loadavg_1m / cpu_cores``).

On WSL2, processes running on the Windows host sit outside the Linux
VM and are invisible to ``psutil.cpu_percent`` — but the Linux kernel
still feels I/O and scheduler contention and reflects it in
``loadavg_1m``. A 1-minute loadavg above 90% of the allocated core
count is a reliable proxy for "host is hot" when native CPU% under-
reports. H2's coordinator ORs this signal with its CPU/mem precondition
so prewarm pauses even when psutil is clean but the WSL2 host isn't.

Comparison is strictly greater-than so a saturated-but-not-overloaded
host (ratio == 0.9) doesn't false-trigger the derate path."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True)
class HostBaseline:
    """Static description of the physical host the backend is provisioned on.

    H1 scope is a "baseline hardcode" — we don't auto-detect at boot because
    the surrounding capacity planner (AIMD admission / quota headroom) needs
    a stable ceiling that survives container restarts and doesn't drift when
    psutil reports a temporarily-degraded core count (e.g. hotplug on WSL2).
    Any future "runtime-detected baseline" work replaces ``HOST_BASELINE``
    with a detected instance of the same shape — callers should keep reading
    through ``HOST_BASELINE`` so that swap is a one-liner.
    """

    cpu_cores: int
    mem_total_gb: int
    disk_total_gb: int
    cpu_model: str


HOST_BASELINE = HostBaseline(
    cpu_cores=16,
    mem_total_gb=64,
    disk_total_gb=512,
    cpu_model="AMD Ryzen 9 9950X",
)


@dataclass
class ContainerSample:
    """One cgroup scrape for a single container."""
    container_id: str
    container_name: str
    tenant_id: str
    cpu_usage_usec: int          # cumulative CPU usage from cpu.stat
    memory_bytes: int            # instantaneous memory.current
    sampled_at: float            # wall-clock time.time()


@dataclass(frozen=True)
class HostSample:
    """One host-level (whole-machine) sample.

    Sits alongside the per-container ``ContainerSample`` — capacity
    planning needs *both*: cgroup scrapes tell us which tenant is hot,
    while psutil numbers tell us whether the host itself is under
    pressure (including workloads outside our container registry).

    Memory "used" is derived as ``total - available`` rather than using
    psutil's ``.used`` attribute: on Linux, ``.used`` sums stale page
    cache and typically over-reports; ``.available`` is what the kernel
    itself considers reclaimable and matches the "free -h" intuition
    the TODO H1 spec is written against.

    Percentage fields are 0-100 floats. GB fields use 1024^3 bytes.
    ``loadavg_*`` are raw os.getloadavg() values (not normalised by
    core count — that derivation lives in the H2 coordinator).
    """
    cpu_percent: float
    mem_percent: float
    mem_used_gb: float
    mem_total_gb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    loadavg_1m: float
    loadavg_5m: float
    loadavg_15m: float
    sampled_at: float


@dataclass(frozen=True)
class DockerSample:
    """One Docker-daemon-wide sample.

    Counts every *running* container the daemon knows about (not just
    ``omnisight-agent-*``) because capacity planning needs to reason
    about total host contention — a developer running their own
    postgres/redis in Docker Desktop still eats memory we'd otherwise
    give to agents.

    Fields:
      * ``container_count``            — int, running containers only
      * ``total_mem_reservation_bytes`` — sum of each container's
        ``HostConfig.MemoryReservation`` (soft limit). Falls back to
        ``HostConfig.Memory`` (hard limit) when reservation is unset,
        and 0 when neither is set (unlimited container).
      * ``source`` — which path produced the sample:
          - ``"sdk"``         — docker-py SDK via unix socket
          - ``"cli"``         — ``docker stats --no-stream`` fallback
          - ``"unavailable"`` — both paths failed; counts default to 0
        Source is surfaced so dashboards / alerts can tell whether we
        still have the high-fidelity reservation number or only the
        best-effort stats-usage proxy.
      * ``sampled_at``                 — wall-clock ``time.time()``.

    The SDK path is preferred because ``containers.list()`` exposes
    ``HostConfig.MemoryReservation`` directly (what we *promised* the
    container, which is what AIMD admission should gate on). The CLI
    fallback — ``docker stats --no-stream`` — only reports current
    *usage*, so in that path we sum the usage column as a best-effort
    proxy. Dashboards should prefer the SDK number; the CLI number is
    there to keep the Docker Desktop/WSL2 case from going blind.
    """
    container_count: int
    total_mem_reservation_bytes: int
    source: str
    sampled_at: float


@dataclass(frozen=True)
class HostSnapshot:
    """One 5s tick of whole-host state — ring-buffer entry.

    Bundles ``HostSample`` (psutil / loadavg) with ``DockerSample``
    (container count + memory reservation) into a single immutable
    record so ring-buffer readers always see consistent pairs — never
    "CPU% from tick N paired with container count from tick N-1".

    ``sampled_at`` mirrors the host sample's timestamp so history can
    be sorted / windowed without peeking inside either sub-record.
    """
    host: HostSample
    docker: DockerSample
    sampled_at: float


@dataclass
class TenantUsage:
    """Aggregated per-tenant resource view (one snapshot)."""
    tenant_id: str
    cpu_percent: float = 0.0
    mem_used_gb: float = 0.0
    disk_used_gb: float = 0.0
    sandbox_count: int = 0


@dataclass
class UsageAccumulator:
    """Cumulative per-tenant usage for billing (cpu_seconds + mem_gb_seconds)."""
    tenant_id: str
    cpu_seconds_total: float = 0.0
    mem_gb_seconds_total: float = 0.0
    last_updated: float = 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module state (reset_for_tests() clears it)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_lock = threading.RLock()

# (container_id) → (cpu_usage_usec, sampled_at) of the previous sample.
# We need this to compute CPU% (a rate, not a counter).
_prev_cpu: dict[str, tuple[int, float]] = {}

# Latest snapshot — what GET /host/metrics renders and the AIMD helper
# reads. Keyed by tenant_id so a missing tenant returns an empty view.
_latest_by_tenant: dict[str, TenantUsage] = {}

# Usage accounting feed (billing). Keyed by tenant_id.
_accounting: dict[str, UsageAccumulator] = {}

# H1 ring buffer — last HOST_HISTORY_SIZE snapshots of whole-host state.
# deque(maxlen=...) rotates for free: append when full pops the oldest,
# so memory stays bounded even if the backend runs for weeks.
_host_history: deque[HostSnapshot] = deque(maxlen=HOST_HISTORY_SIZE)


def _reset_for_tests() -> None:
    with _lock:
        _prev_cpu.clear()
        _latest_by_tenant.clear()
        _accounting.clear()
        _host_history.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cgroup v2 readers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _cgroup_v2_available() -> bool:
    """Quick probe — /sys/fs/cgroup/cgroup.controllers exists on v2 hosts."""
    return (CGROUP_ROOT / "cgroup.controllers").is_file()


def _find_container_cgroup(container_id: str) -> Path | None:
    """Locate a container's cgroup dir.

    Docker on cgroup v2 typically places containers under::

        /sys/fs/cgroup/system.slice/docker-<full_cid>.scope
        /sys/fs/cgroup/docker/<full_cid>
        /sys/fs/cgroup/user.slice/.../docker-<full_cid>.scope

    We search a short list of well-known locations. Returns ``None`` if
    none are readable (container gone / different scheme).
    """
    candidates = [
        CGROUP_ROOT / "system.slice" / f"docker-{container_id}.scope",
        CGROUP_ROOT / "docker" / container_id,
    ]
    for path in candidates:
        if path.is_dir() and (path / "cpu.stat").is_file():
            return path
    # Fallback: glob for any docker-<cid>.scope anywhere under the root.
    try:
        for match in CGROUP_ROOT.rglob(f"docker-{container_id}.scope"):
            if (match / "cpu.stat").is_file():
                return match
    except OSError:
        pass
    return None


def _read_cpu_usage_usec(cgroup_dir: Path) -> int:
    """Parse ``cpu.stat``'s ``usage_usec`` line. Returns 0 on any failure."""
    try:
        text = (cgroup_dir / "cpu.stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("usage_usec "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


def _read_memory_bytes(cgroup_dir: Path) -> int:
    """Read ``memory.current`` (bytes). Returns 0 on any failure."""
    try:
        return int((cgroup_dir / "memory.current").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Host-level sampling (psutil + os.getloadavg)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _read_loadavg() -> tuple[float, float, float]:
    """``os.getloadavg()`` with a zeroed fallback for platforms that
    don't expose it (Windows native, some minimal chroots). Kept
    separate so tests can monkey-patch it without touching psutil.
    """
    try:
        la1, la5, la15 = os.getloadavg()
        return float(la1), float(la5), float(la15)
    except (OSError, AttributeError):
        return 0.0, 0.0, 0.0


def sample_host_once(*, cpu_interval: float = 1.0) -> HostSample:
    """One-shot host-level sample via ``psutil`` + ``os.getloadavg()``.

    Fields populated:
      * ``cpu_percent``          — ``psutil.cpu_percent(interval=cpu_interval)``
      * ``mem_percent``/``mem_used_gb``/``mem_total_gb`` — derived from
        ``psutil.virtual_memory()``; "used" is ``total - available`` so
        the number matches the kernel's reclaimable-memory view rather
        than over-counting page cache.
      * ``disk_percent``/``disk_used_gb``/``disk_total_gb`` —
        ``psutil.disk_usage('/')``.
      * ``loadavg_{1m,5m,15m}``  — ``os.getloadavg()`` (stdlib, works
        even when psutil is absent).

    ``cpu_interval`` defaults to 1.0s per TODO H1 spec. Pass 0 to get a
    non-blocking read that uses the previous psutil invocation's
    timestamp as the delta baseline — useful inside the 5s sampling
    loop where we don't want to spend a whole second blocking.

    If psutil is not importable (dev environments without the optional
    dependency), the psutil-sourced fields fall back to ``0.0`` /
    ``HOST_BASELINE.*`` totals; loadavg still works because it's
    stdlib. Callers treat the return as best-effort and never raise.
    """
    now = time.time()
    la1, la5, la15 = _read_loadavg()

    if psutil is None:
        logger.debug("psutil unavailable; returning baseline-only HostSample")
        return HostSample(
            cpu_percent=0.0,
            mem_percent=0.0,
            mem_used_gb=0.0,
            mem_total_gb=float(HOST_BASELINE.mem_total_gb),
            disk_percent=0.0,
            disk_used_gb=0.0,
            disk_total_gb=float(HOST_BASELINE.disk_total_gb),
            loadavg_1m=la1,
            loadavg_5m=la5,
            loadavg_15m=la15,
            sampled_at=now,
        )

    try:
        cpu_pct = float(psutil.cpu_percent(interval=cpu_interval))
    except Exception as exc:
        logger.debug("psutil.cpu_percent failed: %s", exc)
        cpu_pct = 0.0

    try:
        vm = psutil.virtual_memory()
        mem_total_gb = float(vm.total) / (1024 ** 3)
        # Use vm.available (kernel-reclaimable) not vm.used — the latter
        # over-reports on Linux by counting stale page cache.
        mem_used_gb = float(vm.total - vm.available) / (1024 ** 3)
        mem_pct = (1.0 - float(vm.available) / float(vm.total)) * 100.0 if vm.total else 0.0
    except Exception as exc:
        logger.debug("psutil.virtual_memory failed: %s", exc)
        mem_total_gb = float(HOST_BASELINE.mem_total_gb)
        mem_used_gb = 0.0
        mem_pct = 0.0

    try:
        du = psutil.disk_usage("/")
        disk_total_gb = float(du.total) / (1024 ** 3)
        disk_used_gb = float(du.used) / (1024 ** 3)
        disk_pct = float(du.percent)
    except Exception as exc:
        logger.debug("psutil.disk_usage('/') failed: %s", exc)
        disk_total_gb = float(HOST_BASELINE.disk_total_gb)
        disk_used_gb = 0.0
        disk_pct = 0.0

    return HostSample(
        cpu_percent=cpu_pct,
        mem_percent=mem_pct,
        mem_used_gb=mem_used_gb,
        mem_total_gb=mem_total_gb,
        disk_percent=disk_pct,
        disk_used_gb=disk_used_gb,
        disk_total_gb=disk_total_gb,
        loadavg_1m=la1,
        loadavg_5m=la5,
        loadavg_15m=la15,
        sampled_at=now,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Docker-daemon sampling (SDK primary, CLI fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DOCKER_STATS_TIMEOUT_S = 10
"""Max wall time for the ``docker stats --no-stream`` fallback.

``--no-stream`` is usually sub-second, but on a cold Docker Desktop the
first invocation has to spin up the engine bridge which can take several
seconds. 10s is a generous ceiling — we'd rather time out and return
``source='unavailable'`` than block the 5s sampling loop."""


def _sdk_mem_reservation_bytes(container) -> int:
    """Extract ``HostConfig.MemoryReservation`` (soft limit) from a
    docker-py container object, falling back to ``HostConfig.Memory``
    (hard limit) when reservation is unset. Returns 0 when both are
    unset (unlimited container).

    ``container.attrs`` is the cached ``docker inspect`` payload. We
    read it lazily (``containers.list()`` already warms the cache) so
    we don't pay an extra round-trip per container.
    """
    try:
        host_config = container.attrs.get("HostConfig", {}) or {}
    except Exception:
        return 0
    reservation = int(host_config.get("MemoryReservation") or 0)
    if reservation > 0:
        return reservation
    # Fall back to the hard limit — if the user capped the container at
    # 2 GB, the reservation is effectively 2 GB even if unset.
    return int(host_config.get("Memory") or 0)


def _sample_docker_via_sdk() -> tuple[int, int] | None:
    """Primary path — docker-py SDK.

    Returns ``(container_count, total_mem_reservation_bytes)`` or
    ``None`` if the SDK isn't installed / the daemon isn't reachable
    / any per-container read raises. Callers treat ``None`` as "fall
    back to the CLI path".
    """
    if docker_sdk is None:
        return None
    try:
        client = docker_sdk.from_env(timeout=5)  # type: ignore[union-attr]
        # status filter ensures we only see *running* containers; stopped
        # ones don't consume scheduler slots so they don't count toward
        # admission pressure.
        containers = client.containers.list(filters={"status": "running"})
    except Exception as exc:
        logger.debug("docker SDK unavailable: %s", exc)
        return None
    count = len(containers)
    total = 0
    for c in containers:
        total += _sdk_mem_reservation_bytes(c)
    return count, total


def _parse_docker_stats_mem_column(col: str) -> int:
    """Parse the LHS of a ``docker stats`` MemUsage column.

    docker stats renders memory as ``"<used> / <limit>"``, e.g.:
        ``"127.5MiB / 1.95GiB"``
        ``"512MB / 0B"``          (no limit set)
        ``"0B / 0B"``             (container just started)

    We take the part before the ``/`` and convert to bytes. Returns 0
    on any parse failure so a malformed row doesn't poison the total.
    """
    try:
        left = col.split("/", 1)[0].strip()
    except Exception:
        return 0
    if not left:
        return 0
    # Strip the unit suffix. Order matters — check longer suffixes first
    # so "KiB" doesn't match as "B".
    units = [
        ("PiB", 1024 ** 5), ("TiB", 1024 ** 4), ("GiB", 1024 ** 3),
        ("MiB", 1024 ** 2), ("KiB", 1024),
        ("PB", 10 ** 15), ("TB", 10 ** 12), ("GB", 10 ** 9),
        ("MB", 10 ** 6), ("kB", 10 ** 3), ("KB", 10 ** 3),
        ("B", 1),
    ]
    for suffix, mult in units:
        if left.endswith(suffix):
            num_part = left[: -len(suffix)].strip()
            try:
                return int(float(num_part) * mult)
            except ValueError:
                return 0
    # No unit → assume bytes.
    try:
        return int(float(left))
    except ValueError:
        return 0


def _sample_docker_via_cli() -> tuple[int, int] | None:
    """Fallback path — ``docker stats --no-stream``.

    Used on Docker Desktop / WSL2 where the docker-py SDK can't reach
    the engine over the local unix socket (the CLI shells through
    ``npipe`` or a relocated socket). The CLI does NOT expose
    MemoryReservation, so we return *current usage* as a best-effort
    proxy for reservation — flagged as ``source='cli'`` on the
    returned ``DockerSample``.

    Returns ``(container_count, total_mem_usage_bytes)`` or ``None`` if
    the CLI isn't on PATH or the invocation fails.
    """
    if shutil.which("docker") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "docker", "stats", "--no-stream",
                "--format", "{{.ID}}\t{{.MemUsage}}",
            ],
            capture_output=True,
            text=True,
            timeout=DOCKER_STATS_TIMEOUT_S,
            check=False,
        )
    except Exception as exc:
        # Broad except is intentional — the sampler is a best-effort
        # observer and must not let a subprocess quirk (TimeoutExpired,
        # OSError, or a monkey-patched grenade in tests) tear down the
        # lifespan task.
        logger.debug("docker stats fallback failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.debug(
            "docker stats returned rc=%d stderr=%s",
            proc.returncode, (proc.stderr or "").strip(),
        )
        return None
    count = 0
    total = 0
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        count += 1
        total += _parse_docker_stats_mem_column(parts[1])
    return count, total


def sample_docker_once() -> DockerSample:
    """One-shot Docker-daemon sample — SDK primary, CLI fallback.

    Returns a ``DockerSample`` with ``source='unavailable'`` (and zero
    counts) when neither path succeeds — e.g. Docker isn't installed,
    or we're running inside a container without access to the docker
    socket. Callers treat that as "zero pressure from docker" rather
    than raising; the psutil sample still tells the capacity planner
    whether the host itself is hot.
    """
    now = time.time()
    sdk_result = _sample_docker_via_sdk()
    if sdk_result is not None:
        count, total = sdk_result
        return DockerSample(
            container_count=count,
            total_mem_reservation_bytes=total,
            source="sdk",
            sampled_at=now,
        )
    cli_result = _sample_docker_via_cli()
    if cli_result is not None:
        count, total = cli_result
        return DockerSample(
            container_count=count,
            total_mem_reservation_bytes=total,
            source="cli",
            sampled_at=now,
        )
    return DockerSample(
        container_count=0,
        total_mem_reservation_bytes=0,
        source="unavailable",
        sampled_at=now,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sample collection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _enumerate_agent_containers() -> list[dict]:
    """Return the subset of ``list_containers()`` that are actually running.

    Uses the in-memory registry from ``backend.container`` (stamped with
    tenant_id) rather than ``docker ps`` so we don't pay the subprocess
    cost on every sample. The registry is authoritative because
    ``start_container`` writes + ``stop_container`` deletes.
    """
    try:
        from backend.container import list_containers
    except Exception:
        return []
    out: list[dict] = []
    for info in list_containers():
        if getattr(info, "status", "running") != "running":
            continue
        out.append({
            "container_id": info.container_id,
            "container_name": info.container_name,
            "tenant_id": info.tenant_id or "t-default",
        })
    return out


def sample_once() -> list[ContainerSample]:
    """Scrape cgroup pseudo-files for every tracked running container.

    Returns an empty list if cgroup v2 is unavailable or docker isn't
    the active runtime. Callers that need a v1 / WSL fallback should
    layer ``docker stats`` on top (tracked as a future enhancement —
    see TODO H1 for the fallback story).
    """
    samples: list[ContainerSample] = []
    if not _cgroup_v2_available():
        logger.debug("cgroup v2 not available; sample_once() returning empty")
        return samples
    now = time.time()
    for c in _enumerate_agent_containers():
        cgroup = _find_container_cgroup(c["container_id"])
        if cgroup is None:
            continue
        samples.append(ContainerSample(
            container_id=c["container_id"],
            container_name=c["container_name"],
            tenant_id=c["tenant_id"],
            cpu_usage_usec=_read_cpu_usage_usec(cgroup),
            memory_bytes=_read_memory_bytes(cgroup),
            sampled_at=now,
        ))
    return samples


def _compute_cpu_percent(sample: ContainerSample) -> float:
    """Convert the absolute cpu_usage_usec → per-second CPU% using the
    previous sample for the same container. First sample primes state
    and returns 0%.

    Capped at (num_cores * 100) and floored at 0.
    """
    prev = _prev_cpu.get(sample.container_id)
    _prev_cpu[sample.container_id] = (sample.cpu_usage_usec, sample.sampled_at)
    if prev is None:
        return 0.0
    prev_usec, prev_t = prev
    dt_s = sample.sampled_at - prev_t
    if dt_s <= 0:
        return 0.0
    d_usec = max(0, sample.cpu_usage_usec - prev_usec)
    # cpu_usage_usec is "CPU microseconds consumed across all cores".
    # → CPU% = (d_usec / 1e6) / dt_s * 100
    pct = (d_usec / 1_000_000.0) / dt_s * 100.0
    cores = max(1, os.cpu_count() or 1)
    return max(0.0, min(pct, cores * 100.0))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Aggregation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _measure_disk_gb(tenant_id: str) -> float:
    """Delegate to M2's ``tenant_quota.measure_tenant_usage``."""
    try:
        from backend import tenant_quota as _tq
        usage = _tq.measure_tenant_usage(tenant_id)
        return usage.get("total_bytes", 0) / (1024 ** 3)
    except Exception as exc:
        logger.debug("disk usage read failed for %s: %s", tenant_id, exc)
        return 0.0


def aggregate_by_tenant(samples: list[ContainerSample] | None = None,
                        *, include_disk: bool = True) -> dict[str, TenantUsage]:
    """Group samples by tenant_id, attach disk + sandbox count.

    If ``samples`` is None, performs a fresh ``sample_once()``. Passing
    pre-collected samples is useful in tests + in the AIMD path where
    the caller needs both the aggregate *and* the raw list.
    """
    if samples is None:
        samples = sample_once()

    by_tenant: dict[str, TenantUsage] = {}
    for s in samples:
        usage = by_tenant.setdefault(s.tenant_id, TenantUsage(tenant_id=s.tenant_id))
        usage.cpu_percent += _compute_cpu_percent(s)
        usage.mem_used_gb += s.memory_bytes / (1024 ** 3)
        usage.sandbox_count += 1

    if include_disk:
        for tid, usage in by_tenant.items():
            usage.disk_used_gb = _measure_disk_gb(tid)

    return by_tenant


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Snapshot / culprit accessors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_tenant_usage(tenant_id: str) -> TenantUsage:
    """Return the latest cached aggregation for a single tenant.

    Returns an empty ``TenantUsage`` if the sampler hasn't run yet or
    the tenant has no running containers. Never raises — callers treat
    missing data as "zero usage".
    """
    with _lock:
        cached = _latest_by_tenant.get(tenant_id)
        if cached:
            return TenantUsage(
                tenant_id=cached.tenant_id,
                cpu_percent=cached.cpu_percent,
                mem_used_gb=cached.mem_used_gb,
                disk_used_gb=cached.disk_used_gb,
                sandbox_count=cached.sandbox_count,
            )
    # Fallback — compute disk even when there are no samples, so quota
    # pages can still render the disk bar for idle tenants.
    return TenantUsage(
        tenant_id=tenant_id,
        disk_used_gb=_measure_disk_gb(tenant_id),
    )


def get_all_tenant_usage() -> list[TenantUsage]:
    with _lock:
        return [TenantUsage(
            tenant_id=u.tenant_id,
            cpu_percent=u.cpu_percent,
            mem_used_gb=u.mem_used_gb,
            disk_used_gb=u.disk_used_gb,
            sandbox_count=u.sandbox_count,
        ) for u in _latest_by_tenant.values()]


def get_culprit_tenant(usage_by_tenant: dict[str, TenantUsage] | None = None,
                       *, min_cpu_pct: float = CULPRIT_MIN_CPU_PCT,
                       margin_pct: float = CULPRIT_CPU_MARGIN_PCT) -> str | None:
    """Identify the tenant whose CPU% is dominating.

    Returns a tenant_id if exactly one tenant is (a) above
    ``min_cpu_pct`` in absolute terms AND (b) at least ``margin_pct``
    above the *next-highest* tenant — the "outlier" rule. If two
    tenants are both hot, returns None and the AIMD caller should fall
    back to flat derate.

    M4 acceptance test:
        A=400%  B=20%  → culprit=A (A is 20× B)
        A=200%  B=180% → culprit=None (both hot, flat derate)
        A=60%   B=10%  → culprit=None (A below min_cpu_pct)
    """
    if usage_by_tenant is None:
        with _lock:
            usage_by_tenant = {
                u.tenant_id: TenantUsage(
                    tenant_id=u.tenant_id,
                    cpu_percent=u.cpu_percent,
                    mem_used_gb=u.mem_used_gb,
                    disk_used_gb=u.disk_used_gb,
                    sandbox_count=u.sandbox_count,
                ) for u in _latest_by_tenant.values()
            }
    sorted_by_cpu = sorted(
        usage_by_tenant.values(), key=lambda u: u.cpu_percent, reverse=True,
    )
    if not sorted_by_cpu:
        return None
    top = sorted_by_cpu[0]
    if top.cpu_percent < min_cpu_pct:
        return None
    if len(sorted_by_cpu) == 1:
        return top.tenant_id
    second = sorted_by_cpu[1]
    if top.cpu_percent >= second.cpu_percent + margin_pct:
        return top.tenant_id
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Usage accounting (billing feed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def accumulate_usage(usage_by_tenant: dict[str, TenantUsage],
                     interval_s: float) -> None:
    """Fold one sample interval's usage into the running accumulators.

    * ``cpu_seconds`` = cpu_percent / 100 * interval_s
    * ``mem_gb_seconds`` = mem_used_gb * interval_s

    Safe to call when ``interval_s`` is ~0 (e.g. right after a manual
    probe); the contribution rounds to 0 without underflowing.
    """
    if interval_s <= 0:
        return
    now = time.time()
    with _lock:
        for tid, usage in usage_by_tenant.items():
            acc = _accounting.setdefault(tid, UsageAccumulator(tenant_id=tid))
            acc.cpu_seconds_total += (usage.cpu_percent / 100.0) * interval_s
            acc.mem_gb_seconds_total += usage.mem_used_gb * interval_s
            acc.last_updated = now


def snapshot_accounting() -> list[UsageAccumulator]:
    """Return a point-in-time copy of all accumulators. Used by
    ``scripts/usage_report.py`` and admin dashboards."""
    with _lock:
        return [UsageAccumulator(
            tenant_id=a.tenant_id,
            cpu_seconds_total=a.cpu_seconds_total,
            mem_gb_seconds_total=a.mem_gb_seconds_total,
            last_updated=a.last_updated,
        ) for a in _accounting.values()]


def reset_accounting(tenant_id: str | None = None) -> None:
    """Clear the accumulator for a single tenant (e.g. end-of-month
    billing rollover) or for all tenants when ``tenant_id`` is None."""
    with _lock:
        if tenant_id is None:
            _accounting.clear()
        else:
            _accounting.pop(tenant_id, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prometheus metrics update
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _publish_prom_metrics(usage_by_tenant: dict[str, TenantUsage]) -> None:
    """Push the freshest aggregation into the Prometheus gauges."""
    try:
        from backend import metrics as _m
    except Exception:
        return
    for tid, u in usage_by_tenant.items():
        try:
            _m.tenant_cpu_percent.labels(tenant_id=tid).set(u.cpu_percent)
            _m.tenant_mem_used_gb.labels(tenant_id=tid).set(u.mem_used_gb)
            _m.tenant_disk_used_gb.labels(tenant_id=tid).set(u.disk_used_gb)
            _m.tenant_sandbox_count.labels(tenant_id=tid).set(u.sandbox_count)
        except Exception as exc:
            logger.debug("metrics publish failed for %s: %s", tid, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sampling loop (lifespan task)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _update_latest(usage_by_tenant: dict[str, TenantUsage]) -> None:
    with _lock:
        _latest_by_tenant.clear()
        _latest_by_tenant.update(usage_by_tenant)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — Host ring buffer + sampling loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def sample_host_snapshot(*, cpu_interval: float = 1.0) -> HostSnapshot:
    """One-shot combined host + docker sample — ring-buffer entry factory.

    Runs ``sample_host_once`` (psutil + loadavg) followed by
    ``sample_docker_once`` (SDK → CLI fallback) and bundles the two
    immutable sub-samples into a ``HostSnapshot``. Never raises: each
    sub-sampler has its own degradation path, so a missing psutil /
    unreachable docker daemon yields a snapshot with zeroed / baseline
    fields rather than tearing the sampling loop down.
    """
    host = sample_host_once(cpu_interval=cpu_interval)
    docker = sample_docker_once()
    return HostSnapshot(host=host, docker=docker, sampled_at=host.sampled_at)


def _record_host_snapshot(snap: HostSnapshot) -> None:
    """Append ``snap`` to the ring buffer (rotates oldest out when full)."""
    with _lock:
        _host_history.append(snap)


def _snapshot_to_sse_payload(snap: HostSnapshot) -> dict:
    """Serialise a ``HostSnapshot`` for the ``host.metrics.tick`` SSE event.

    Mirrors the per-snapshot shape of ``GET /api/v1/host/metrics`` (router
    helper ``_snapshot_to_dict``) so the UI can use the same parser for
    both the initial fetch and subsequent tick deltas. We additionally
    include:

      * ``baseline``       — the static ``HOST_BASELINE`` (cpu_cores /
        mem_total_gb / disk_total_gb / cpu_model). Sent on every tick
        because clients that re-subscribe mid-stream wouldn't otherwise
        see it; the cost is ~80 bytes per tick.
      * ``high_pressure``  — pre-computed loadavg ratio test so the UI
        doesn't need to know the WSL2 0.9 threshold; also lets the
        Coordinator (H2) consume the same field without recomputing.

    Floats are rounded to the same precision the REST endpoint uses so
    SSE consumers can compare/diff snapshots from the two sources without
    floating-point drift.
    """
    return {
        "host": {
            "cpu_percent": round(snap.host.cpu_percent, 2),
            "mem_percent": round(snap.host.mem_percent, 2),
            "mem_used_gb": round(snap.host.mem_used_gb, 3),
            "mem_total_gb": round(snap.host.mem_total_gb, 3),
            "disk_percent": round(snap.host.disk_percent, 2),
            "disk_used_gb": round(snap.host.disk_used_gb, 3),
            "disk_total_gb": round(snap.host.disk_total_gb, 3),
            "loadavg_1m": round(snap.host.loadavg_1m, 3),
            "loadavg_5m": round(snap.host.loadavg_5m, 3),
            "loadavg_15m": round(snap.host.loadavg_15m, 3),
            "sampled_at": snap.host.sampled_at,
        },
        "docker": {
            "container_count": snap.docker.container_count,
            "total_mem_reservation_bytes": snap.docker.total_mem_reservation_bytes,
            "source": snap.docker.source,
            "sampled_at": snap.docker.sampled_at,
        },
        "baseline": {
            "cpu_cores": HOST_BASELINE.cpu_cores,
            "mem_total_gb": HOST_BASELINE.mem_total_gb,
            "disk_total_gb": HOST_BASELINE.disk_total_gb,
            "cpu_model": HOST_BASELINE.cpu_model,
        },
        "high_pressure": is_high_pressure_loadavg(snap.host.loadavg_1m),
        "sampled_at": snap.sampled_at,
    }


def _publish_host_sse_tick(snap: HostSnapshot) -> None:
    """Push one ``host.metrics.tick`` SSE event for the freshest snapshot.

    Best-effort: an event-bus glitch (no running loop, JSON serialisation
    failure, Redis pub/sub flake) must never tear down the sampling
    lifespan task — observability is allowed to degrade silently before
    it's allowed to take down the backend. Exceptions are logged at
    debug level so chronic outages remain greppable in container logs.
    """
    try:
        from backend.events import bus
    except Exception as exc:
        logger.debug("host SSE bus import failed: %s", exc)
        return
    try:
        payload = _snapshot_to_sse_payload(snap)
    except Exception as exc:
        logger.debug("host SSE payload build failed: %s", exc)
        return
    try:
        bus.publish("host.metrics.tick", payload)
    except Exception as exc:
        logger.debug("host SSE publish failed: %s", exc)


def _publish_host_prom_metrics(snap: HostSnapshot) -> None:
    """Push the freshest host snapshot into the H1 Prometheus gauges.

    Five gauges track whole-host pressure:
      * ``host_cpu_percent``       — psutil cpu_percent (0-100)
      * ``host_mem_percent``       — derived from (total-available)/total
      * ``host_disk_percent``      — root fs usage (0-100)
      * ``host_loadavg_1m``        — raw 1m load average (not normalised)
      * ``host_container_count``   — running docker containers, labelled
        by ``source`` (sdk / cli / unavailable) so dashboards can tell
        whether the count is authoritative or best-effort.

    Swallows exceptions: the sampling loop is best-effort observability
    and must never tear down the backend if Prometheus client is not
    installed or a gauge set fails.
    """
    try:
        from backend import metrics as _m
    except Exception:
        return
    try:
        _m.host_cpu_percent.set(snap.host.cpu_percent)
        _m.host_mem_percent.set(snap.host.mem_percent)
        _m.host_disk_percent.set(snap.host.disk_percent)
        _m.host_loadavg_1m.set(snap.host.loadavg_1m)
        _m.host_container_count.labels(source=snap.docker.source).set(
            snap.docker.container_count,
        )
    except Exception as exc:
        logger.debug("host metrics publish failed: %s", exc)


def get_host_history() -> list[HostSnapshot]:
    """Return a point-in-time copy of the host history, oldest first.

    Copying under the lock means callers never observe a partially-
    rotated buffer. The ring is capped at ``HOST_HISTORY_SIZE`` so the
    returned list is short enough (≤60 items) to serialise in one JSON
    payload or one SSE frame without chunking.
    """
    with _lock:
        return list(_host_history)


def get_latest_host_snapshot() -> HostSnapshot | None:
    """Return the most recent snapshot, or ``None`` if the sampler
    hasn't produced one yet (cold-start grace window)."""
    with _lock:
        if not _host_history:
            return None
        return _host_history[-1]


def is_high_pressure_loadavg(loadavg_1m: float,
                             cpu_cores: int | None = None,
                             *, threshold: float = HIGH_PRESSURE_LOADAVG_RATIO) -> bool:
    """Return True when the normalised 1m load average exceeds ``threshold``.

    Pure function — tests ``loadavg_1m / cpu_cores > threshold`` with
    the WSL2 rationale documented on ``HIGH_PRESSURE_LOADAVG_RATIO``.

    ``cpu_cores`` defaults to ``HOST_BASELINE.cpu_cores`` (16 under the
    current baseline). Lookup happens at call time — if a future runtime
    detector swaps ``HOST_BASELINE`` the threshold automatically follows
    without re-binding callers.

    Edge cases:
      * ``cpu_cores <= 0``            — returns False (avoids ZeroDivision
        and nonsensical negative-core hosts rather than raising).
      * ``loadavg_1m`` negative / NaN — returns False (treated as
        "no reading" so a ``_read_loadavg`` fallback on a platform
        without loadavg doesn't false-trigger derate).
    """
    cores = cpu_cores if cpu_cores is not None else HOST_BASELINE.cpu_cores
    if cores <= 0:
        return False
    try:
        ratio = loadavg_1m / cores
    except (TypeError, ZeroDivisionError):
        return False
    # NaN compares False against anything → also short-circuits negative.
    if not (ratio > 0.0):
        return False
    return ratio > threshold


def is_host_high_pressure(snapshot: "HostSnapshot | HostSample | None" = None) -> bool:
    """High-pressure check over the ring buffer (or an explicit sample).

    Thin convenience wrapper the coordinator / prewarm paths call on
    every decision cycle: pulls the latest ``HostSnapshot`` out of the
    ring buffer and runs the loadavg ratio test. Pass an explicit
    ``HostSnapshot`` / ``HostSample`` to check a specific tick (tests,
    replay from history).

    Returns ``False`` when no sample has landed yet (cold-start grace) —
    callers treat "no data" as "no pressure known" so the first few
    seconds after boot don't look artificially derated.
    """
    if snapshot is None:
        snapshot = get_latest_host_snapshot()
    if snapshot is None:
        return False
    host = snapshot.host if isinstance(snapshot, HostSnapshot) else snapshot
    return is_high_pressure_loadavg(host.loadavg_1m)


async def run_host_sampling_loop(interval_s: float = SAMPLE_INTERVAL_S,
                                  *, cpu_interval: float = 1.0) -> None:
    """Lifespan task — sample host + docker every ``interval_s`` seconds
    and push into the ring buffer.

    The loop targets a *wall-clock* cadence of ``interval_s``: it
    measures how long the sample itself took (``sample_host_once``
    blocks for ``cpu_interval`` seconds inside ``psutil.cpu_percent``,
    and the CLI docker fallback can occasionally be slow) and subtracts
    that from the sleep. If a sample overruns ``interval_s`` the loop
    fires the next sample immediately rather than drifting further
    behind.

    Swallows per-iteration exceptions so a transient psutil / docker
    glitch never cancels the lifespan task — failures are counted in
    ``metrics.persist_failure_total{module="host_metrics"}`` for the
    observability runbook's alert on sustained collection loss.
    """
    logger.info(
        "host_metrics: host sampling loop starting (interval=%.1fs, history=%d)",
        interval_s, HOST_HISTORY_SIZE,
    )
    try:
        while True:
            tick_start = time.monotonic()
            try:
                snap = sample_host_snapshot(cpu_interval=cpu_interval)
                _record_host_snapshot(snap)
                _publish_host_prom_metrics(snap)
                _publish_host_sse_tick(snap)
                # H2 row 1513: advance the Coordinator's turbo
                # auto-derate state machine with this tick's CPU
                # reading. Triggers derate / recover transitions on
                # the 5s cadence without waiting for an acquire().
                try:
                    from backend import decision_engine as _de
                    _de.evaluate_turbo_derate(cpu_percent=snap.host.cpu_percent)
                except Exception as exc:
                    logger.debug("turbo-derate evaluate failed: %s", exc)
            except Exception as exc:
                logger.warning(
                    "host_metrics host-sample iteration failed: %s", exc,
                )
                try:
                    from backend import metrics as _m
                    _m.persist_failure_total.labels(module="host_metrics").inc()
                except Exception:
                    pass
            elapsed = time.monotonic() - tick_start
            await asyncio.sleep(max(0.0, interval_s - elapsed))
    except asyncio.CancelledError:
        logger.info("host_metrics: host sampling loop cancelled")
        raise


async def run_sampling_loop(interval_s: float = SAMPLE_INTERVAL_S) -> None:
    """Lifespan task — samples every ``interval_s`` seconds, bumps
    Prometheus gauges, accumulates billing, updates the cached
    snapshot. Swallows per-iteration exceptions so a transient cgroup
    glitch doesn't crash the backend.
    """
    logger.info("host_metrics: sampling loop starting (interval=%.1fs)", interval_s)
    last_sample_at = time.time()
    try:
        while True:
            try:
                samples = sample_once()
                usage = aggregate_by_tenant(samples, include_disk=True)
                _update_latest(usage)
                _publish_prom_metrics(usage)
                now = time.time()
                accumulate_usage(usage, now - last_sample_at)
                last_sample_at = now
            except Exception as exc:
                logger.warning("host_metrics sample iteration failed: %s", exc)
                try:
                    from backend import metrics as _m
                    _m.persist_failure_total.labels(module="host_metrics").inc()
                except Exception:
                    pass
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("host_metrics: sampling loop cancelled")
        raise
