"""Phase 64-A S2 — Tier 1 sandbox egress helpers.

The Tier-1 sandbox defaults to `--network none` (full air-gap). When a
caller (e.g. a build that needs `git clone`) absolutely needs egress,
they can opt in by setting BOTH:

    OMNISIGHT_T1_ALLOW_EGRESS=true
    OMNISIGHT_T1_EGRESS_ALLOW_HOSTS=github.com,gerrit.internal:29418

We then place the container on a dedicated docker bridge network
(`omnisight-egress-t1`). Iptables rules that further restrict OUTPUT
to the resolved IPs of those hosts live in
`scripts/setup_t1_egress_iptables.sh` — they need CAP_NET_ADMIN on the
host and so are not part of the Python startup path.

This module is therefore Python-only:

  * Resolves the configured hostnames to A/AAAA records (5 min cache).
  * Ensures the docker bridge exists (idempotent `docker network create`).
  * Returns the right `--network ...` argument for `docker run`.

It NEVER mutates iptables. The operator runs the shell script once at
host setup; subsequent agent containers reuse the bridge.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Iterable

logger = logging.getLogger(__name__)

T1_NETWORK_NAME = "omnisight-egress-t1"
T2_NETWORK_NAME = "omnisight-egress-t2"  # Phase 64-B
_DNS_CACHE_TTL_S = 300.0  # 5 min — long enough to amortise lookups, short
                          # enough that DNS rotation eventually catches up.

# (hostname, port) -> (ips, expires_at)
_dns_cache: dict[tuple[str, int | None], tuple[list[str], float]] = {}
_dns_cache_lock = asyncio.Lock()


def _parse_host_list(raw: str) -> list[tuple[str, int | None]]:
    """Parse "github.com,gerrit.internal:29418" → [("github.com", None),
    ("gerrit.internal", 29418)]. Empty or whitespace-only → []."""
    out: list[tuple[str, int | None]] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            host, _, port = item.rpartition(":")
            try:
                out.append((host.strip(), int(port)))
            except ValueError:
                logger.warning("invalid port in egress allow-list entry %r; skipping", item)
        else:
            out.append((item, None))
    return out


async def resolve_allow_ips(
    hosts: Iterable[tuple[str, int | None]] | None = None,
    *, now: float | None = None,
) -> dict[str, list[str]]:
    """Resolve hostnames to IPs with a TTL cache.

    Returns {hostname: [ip, ...]}. Hostnames that fail DNS map to [];
    callers should treat empty as "unreachable" rather than "deny all".
    """
    if hosts is None:
        from backend.config import settings as _settings
        hosts = _parse_host_list(_settings.t1_egress_allow_hosts)
    now = now or time.monotonic()
    out: dict[str, list[str]] = {}
    async with _dns_cache_lock:
        loop = asyncio.get_running_loop()
        for host, port in hosts:
            key = (host, port)
            cached = _dns_cache.get(key)
            if cached and cached[1] > now:
                out[host] = cached[0]
                continue
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                ips = sorted({ai[4][0] for ai in infos})
            except Exception as exc:
                logger.warning("DNS resolve %s failed: %s", host, exc)
                ips = []
            _dns_cache[key] = (ips, now + _DNS_CACHE_TTL_S)
            out[host] = ips
    return out


def _reset_dns_cache_for_tests() -> None:
    _dns_cache.clear()


async def _docker_network_exists(name: str, *, runner=None) -> bool:
    """Best-effort check via `docker network ls`. `runner` is injectable
    for tests."""
    if runner is None:
        from backend.container import _run as runner  # late import to dodge cycle
    rc, out, _ = await runner(f"docker network ls --filter name=^{name}$ --format '{{{{.Name}}}}'", timeout=10)
    if rc != 0:
        return False
    return name in {l.strip() for l in out.splitlines()}


async def ensure_egress_network(*, runner=None) -> str:
    """Create `omnisight-egress-t1` bridge if missing. Returns its name.

    Idempotent: a second call is a cheap network-ls + early return.
    Failures here are non-fatal — caller can decide to fall back to
    `--network none`. We log loudly so the operator sees the issue.
    """
    if runner is None:
        from backend.container import _run as runner
    if await _docker_network_exists(T1_NETWORK_NAME, runner=runner):
        return T1_NETWORK_NAME
    # bridge driver, no inter-container DNS leakage to other networks.
    rc, _, err = await runner(
        f"docker network create --driver bridge "
        f"--label omnisight.tier=1 {T1_NETWORK_NAME}",
        timeout=15,
    )
    if rc != 0:
        logger.error(
            "ensure_egress_network: failed to create %s: %s — caller will "
            "fall back to --network none",
            T1_NETWORK_NAME, err,
        )
        raise RuntimeError(f"docker network create failed: {err}")
    logger.info("created docker network %s for Tier-1 egress", T1_NETWORK_NAME)
    return T1_NETWORK_NAME


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 64-B — Tier 2 Networked Sandbox
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# T2 inverts T1's policy: egress to the public internet is allowed
# (ACCEPT-by-default), but all RFC1918 + link-local + ULA destinations
# are DROPped at iptables. The Python side just owns the docker bridge
# `omnisight-egress-t2`; the iptables hardening lives in
# `scripts/setup_t2_network.sh` and must be run once per host with
# CAP_NET_ADMIN.

async def ensure_t2_network(*, runner=None) -> str:
    """Idempotent `docker network create omnisight-egress-t2` (bridge)."""
    if runner is None:
        from backend.container import _run as runner
    if await _docker_network_exists(T2_NETWORK_NAME, runner=runner):
        return T2_NETWORK_NAME
    rc, _, err = await runner(
        f"docker network create --driver bridge "
        f"--label omnisight.tier=2 {T2_NETWORK_NAME}",
        timeout=15,
    )
    if rc != 0:
        logger.error(
            "ensure_t2_network: failed to create %s: %s",
            T2_NETWORK_NAME, err,
        )
        raise RuntimeError(f"docker network create failed: {err}")
    logger.info("created docker network %s for Tier-2 egress", T2_NETWORK_NAME)
    return T2_NETWORK_NAME


async def resolve_t2_network_arg(*, runner=None) -> str:
    """Always returns ``--network omnisight-egress-t2`` once the bridge
    is up. T2 is opt-in at the *call site* (via start_networked_container);
    there is no env double-gate here — the caller is the gate."""
    try:
        await ensure_t2_network(runner=runner)
    except Exception as exc:
        logger.error(
            "T2 bridge unavailable (%s) — refusing to launch", exc,
        )
        raise
    return f"--network {T2_NETWORK_NAME}"


async def resolve_network_arg(
    *, runner=None, tenant_id: str | None = None,
) -> str:
    """Decide what `--network ...` flag to pass `docker run` for a
    Tier-1 sandbox. Honours the double-gate AND the M6 per-tenant
    egress policy.

    Per-tenant policy (DB-backed) wins when present; falls back to the
    legacy global ``OMNISIGHT_T1_*`` env knobs otherwise so pre-M6
    deployments don't change behaviour.

    Returns either ``"--network none"`` (default, hardened) or
    ``f"--network {T1_NETWORK_NAME}"`` when egress is permitted.
    """
    # ── M6: per-tenant policy gate ─────────────────────────
    # Empty allow-list (hosts AND cidrs) under default deny means the
    # tenant is air-gapped regardless of global env. If either source
    # has entries, we open the bridge so iptables can selectively allow.
    db_policy_grants_egress = False
    try:
        from backend import tenant_egress as _te
        pol = await _te.policy_for(tenant_id)
        if pol.allowed_hosts or pol.allowed_cidrs or pol.default_action == "allow":
            db_policy_grants_egress = True
            # Warm DNS cache so the iptables installer is in lockstep.
            resolved = await _te.resolve_allow_targets(pol)
            unresolved = [h for h in pol.allowed_hosts
                          if not resolved.get(h)]
            if unresolved:
                logger.warning(
                    "Tier-1 egress (tenant=%s) hosts that failed DNS: %s — "
                    "they will be unreachable from the sandbox.",
                    pol.tenant_id, unresolved,
                )
    except Exception as exc:  # DB not initialised in some test paths
        logger.debug("tenant_egress policy lookup skipped: %s", exc)

    from backend.config import settings as _settings
    allow_env = bool(_settings.t1_allow_egress)
    hosts_raw = (_settings.t1_egress_allow_hosts or "").strip()
    legacy_grants_egress = bool(allow_env and hosts_raw)

    if not (db_policy_grants_egress or legacy_grants_egress):
        # Air-gap path. Keep the legacy warnings so operators noticing
        # an env-vs-flag mismatch still get the hint.
        if allow_env and not hosts_raw:
            logger.warning(
                "OMNISIGHT_T1_ALLOW_EGRESS=true but allow-hosts is empty — "
                "keeping air-gap (--network none).",
            )
        if hosts_raw and not allow_env:
            logger.warning(
                "OMNISIGHT_T1_EGRESS_ALLOW_HOSTS configured but "
                "OMNISIGHT_T1_ALLOW_EGRESS is false — keeping air-gap.",
            )
        return "--network none"
    try:
        await ensure_egress_network(runner=runner)
    except Exception as exc:
        logger.error(
            "egress bridge unavailable (%s) — falling back to --network none",
            exc,
        )
        return "--network none"
    # Legacy DNS warm-up still runs when the legacy CSV is the source
    # (the M6 policy path warmed its own cache above).
    if legacy_grants_egress and not db_policy_grants_egress:
        resolved = await resolve_allow_ips()
        unresolved = [h for h, ips in resolved.items() if not ips]
        if unresolved:
            logger.warning(
                "Tier-1 egress hosts that failed DNS: %s — they will be "
                "unreachable from the sandbox.", unresolved,
            )
    return f"--network {T1_NETWORK_NAME}"
