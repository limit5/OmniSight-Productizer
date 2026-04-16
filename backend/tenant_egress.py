"""M6 — Per-tenant egress allow-list policy + approval workflow.

Replaces the single-tenant ``OMNISIGHT_T1_EGRESS_ALLOW_HOSTS`` env knob
with a database-backed per-tenant policy that the host-side iptables /
nftables installer reads to materialise per-uid OUTPUT rules.

Design overview
---------------

* **Source of truth**: the ``tenant_egress_policies`` row for a given
  tenant. ``allowed_hosts`` and ``allowed_cidrs`` are JSON arrays;
  ``default_action`` is ``"deny"`` (recommended) or ``"allow"`` (legacy
  escape hatch — emits a warning).

* **Read path** (cheap): :func:`get_policy` returns a typed
  :class:`EgressPolicy`. :func:`resolve_allow_targets` resolves hosts to
  IPs (5-min TTL cache, async-safe) and unions in CIDRs.

* **Write paths**:
    - :func:`upsert_policy` — admin direct edit.
    - :func:`submit_request` — viewer/operator files an addition.
    - :func:`approve_request` / :func:`reject_request` — admin acts on
      pending requests; approval merges into the live policy.

* **Audit**: every write emits an ``audit.log`` entry under the
  ``tenant_egress`` action namespace so post-incident review can answer
  "who allowed `evil.com` for tenant T".

* **Backward compat**: legacy callers that read
  ``settings.t1_egress_allow_hosts`` keep working — :func:`policy_for`
  falls back to the env CSV when no DB row exists.

The actual iptables / nftables side-effect lives in
``scripts/apply_tenant_egress.sh``; this module ships only the policy
data and a serialisable rule plan via :func:`build_rule_plan` so the
shell installer (and the optional Python applier in
``scripts/manage_tenant_egress.py``) stay in lockstep.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import json
import logging
import re
import socket
import time
import uuid
from typing import Iterable, Sequence

from backend.db import _conn
from backend.db_context import current_tenant_id

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "t-default"
ALLOWED_DEFAULT_ACTIONS = ("deny", "allow")

_HOST_RE = re.compile(r"^[a-zA-Z0-9._-]{1,253}(:\d{1,5})?$")
_TID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_DNS_TTL_S = 300.0

_dns_cache: dict[tuple[str, int | None], tuple[list[str], float]] = {}
_dns_lock = asyncio.Lock()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data shapes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclasses.dataclass(frozen=True)
class EgressPolicy:
    tenant_id: str
    allowed_hosts: tuple[str, ...]
    allowed_cidrs: tuple[str, ...]
    default_action: str = "deny"
    updated_at: str | None = None
    updated_by: str = "system"

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_cidrs": list(self.allowed_cidrs),
            "default_action": self.default_action,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


@dataclasses.dataclass(frozen=True)
class EgressRequest:
    id: str
    tenant_id: str
    requested_by: str
    kind: str
    value: str
    justification: str
    status: str
    decided_by: str | None
    decided_at: str | None
    decision_note: str
    created_at: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def validate_host(host: str) -> str:
    """Return a normalised host[:port] string or raise ValueError."""
    if not host or not isinstance(host, str):
        raise ValueError("host must be a non-empty string")
    h = host.strip().lower()
    if not _HOST_RE.match(h):
        raise ValueError(f"invalid host: {host!r}")
    if ":" in h:
        name, _, port = h.rpartition(":")
        try:
            port_i = int(port)
        except ValueError:
            raise ValueError(f"invalid port in {host!r}")
        if not 1 <= port_i <= 65535:
            raise ValueError(f"port out of range in {host!r}")
        if not name:
            raise ValueError(f"empty host in {host!r}")
    return h


def validate_cidr(cidr: str) -> str:
    """Return a normalised CIDR string or raise ValueError. Accepts both
    bare IPs (auto-/32 or /128) and full CIDR blocks."""
    if not cidr or not isinstance(cidr, str):
        raise ValueError("cidr must be a non-empty string")
    c = cidr.strip()
    try:
        net = ipaddress.ip_network(c, strict=False)
    except ValueError as exc:
        raise ValueError(f"invalid cidr: {cidr!r} ({exc})")
    return str(net)


def validate_default_action(action: str) -> str:
    a = (action or "").strip().lower()
    if a not in ALLOWED_DEFAULT_ACTIONS:
        raise ValueError(
            f"default_action must be one of {ALLOWED_DEFAULT_ACTIONS}, got {action!r}"
        )
    return a


def validate_tenant_id(tid: str) -> str:
    if not tid or not _TID_RE.match(tid):
        raise ValueError(f"invalid tenant_id: {tid!r}")
    return tid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Policy CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _row_to_policy(row) -> EgressPolicy:
    try:
        hosts = json.loads(row["allowed_hosts"] or "[]")
    except Exception:
        hosts = []
    try:
        cidrs = json.loads(row["allowed_cidrs"] or "[]")
    except Exception:
        cidrs = []
    return EgressPolicy(
        tenant_id=row["tenant_id"],
        allowed_hosts=tuple(str(h) for h in hosts if isinstance(h, str)),
        allowed_cidrs=tuple(str(c) for c in cidrs if isinstance(c, str)),
        default_action=row["default_action"] or "deny",
        updated_at=row["updated_at"],
        updated_by=row["updated_by"] or "system",
    )


async def get_policy(tenant_id: str | None = None) -> EgressPolicy:
    """Return the policy for a tenant. Falls back to a deny-by-default
    empty policy when the row is missing (tenant not yet onboarded)."""
    tid = validate_tenant_id(tenant_id or current_tenant_id() or DEFAULT_TENANT_ID)
    async with _conn().execute(
        "SELECT tenant_id, allowed_hosts, allowed_cidrs, default_action, "
        "updated_at, updated_by FROM tenant_egress_policies WHERE tenant_id=?",
        (tid,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return EgressPolicy(
            tenant_id=tid, allowed_hosts=(), allowed_cidrs=(),
            default_action="deny", updated_at=None, updated_by="system",
        )
    return _row_to_policy(row)


async def list_policies() -> list[EgressPolicy]:
    async with _conn().execute(
        "SELECT tenant_id, allowed_hosts, allowed_cidrs, default_action, "
        "updated_at, updated_by FROM tenant_egress_policies "
        "ORDER BY tenant_id"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_policy(r) for r in rows]


async def upsert_policy(
    tenant_id: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
    allowed_cidrs: Sequence[str] | None = None,
    default_action: str | None = None,
    actor: str = "system",
) -> EgressPolicy:
    """Replace the policy for a tenant. Validates every entry; a single
    bad value rejects the entire write to avoid partial state."""
    tid = validate_tenant_id(tenant_id)

    current = await get_policy(tid)

    if allowed_hosts is None:
        hosts: list[str] = list(current.allowed_hosts)
    else:
        hosts = []
        seen: set[str] = set()
        for h in allowed_hosts:
            v = validate_host(h)
            if v not in seen:
                seen.add(v)
                hosts.append(v)

    if allowed_cidrs is None:
        cidrs: list[str] = list(current.allowed_cidrs)
    else:
        cidrs = []
        seenc: set[str] = set()
        for c in allowed_cidrs:
            v = validate_cidr(c)
            if v not in seenc:
                seenc.add(v)
                cidrs.append(v)

    action = validate_default_action(default_action or current.default_action)
    if action == "allow":
        logger.warning(
            "tenant_egress: tenant=%s set default_action=allow — bypasses "
            "the M6 deny-by-default invariant. Operator confirmed via %s.",
            tid, actor,
        )

    db = _conn()
    await db.execute(
        "INSERT INTO tenant_egress_policies "
        "(tenant_id, allowed_hosts, allowed_cidrs, default_action, "
        " updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT(tenant_id) DO UPDATE SET "
        " allowed_hosts=excluded.allowed_hosts, "
        " allowed_cidrs=excluded.allowed_cidrs, "
        " default_action=excluded.default_action, "
        " updated_at=excluded.updated_at, "
        " updated_by=excluded.updated_by",
        (tid, json.dumps(hosts), json.dumps(cidrs), action, actor),
    )
    await db.commit()

    new = await get_policy(tid)
    await _audit("tenant_egress.upsert", tid, actor, before=current.to_dict(), after=new.to_dict())
    return new


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Request workflow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VALID_REQUEST_KINDS = ("host", "cidr")
VALID_REQUEST_STATUSES = ("pending", "approved", "rejected")


def _validate_request_value(kind: str, value: str) -> str:
    if kind == "host":
        return validate_host(value)
    if kind == "cidr":
        return validate_cidr(value)
    raise ValueError(f"invalid request kind: {kind!r}")


async def submit_request(
    tenant_id: str,
    *,
    requested_by: str,
    kind: str,
    value: str,
    justification: str = "",
) -> EgressRequest:
    """File a pending request. Idempotent — duplicate (tenant, kind, value,
    pending) entries collapse to the existing one."""
    tid = validate_tenant_id(tenant_id)
    if kind not in VALID_REQUEST_KINDS:
        raise ValueError(f"kind must be one of {VALID_REQUEST_KINDS}")
    norm_value = _validate_request_value(kind, value)

    db = _conn()
    async with db.execute(
        "SELECT id FROM tenant_egress_requests "
        "WHERE tenant_id=? AND kind=? AND value=? AND status='pending'",
        (tid, kind, norm_value),
    ) as cur:
        existing = await cur.fetchone()
    if existing:
        return await _get_request(existing["id"])

    rid = f"egr-{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO tenant_egress_requests "
        "(id, tenant_id, requested_by, kind, value, justification, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
        (rid, tid, requested_by, kind, norm_value, justification or ""),
    )
    await db.commit()

    req = await _get_request(rid)
    await _audit(
        "tenant_egress.request_submit", tid, requested_by,
        after={"request_id": rid, "kind": kind, "value": norm_value,
               "justification": justification or ""},
    )
    return req


async def list_requests(
    *, tenant_id: str | None = None, status: str | None = None,
) -> list[EgressRequest]:
    sql = (
        "SELECT id, tenant_id, requested_by, kind, value, justification, "
        "status, decided_by, decided_at, decision_note, created_at "
        "FROM tenant_egress_requests"
    )
    conds: list[str] = []
    params: list = []
    if tenant_id:
        conds.append("tenant_id=?")
        params.append(validate_tenant_id(tenant_id))
    if status:
        if status not in VALID_REQUEST_STATUSES:
            raise ValueError(f"status must be one of {VALID_REQUEST_STATUSES}")
        conds.append("status=?")
        params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC"
    async with _conn().execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_request(r) for r in rows]


async def _get_request(request_id: str) -> EgressRequest:
    async with _conn().execute(
        "SELECT id, tenant_id, requested_by, kind, value, justification, "
        "status, decided_by, decided_at, decision_note, created_at "
        "FROM tenant_egress_requests WHERE id=?",
        (request_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise KeyError(f"unknown egress request: {request_id}")
    return _row_to_request(row)


def _row_to_request(row) -> EgressRequest:
    return EgressRequest(
        id=row["id"],
        tenant_id=row["tenant_id"],
        requested_by=row["requested_by"],
        kind=row["kind"],
        value=row["value"],
        justification=row["justification"] or "",
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        decision_note=row["decision_note"] or "",
        created_at=row["created_at"],
    )


async def approve_request(
    request_id: str, *, actor: str, note: str = "",
) -> tuple[EgressRequest, EgressPolicy]:
    """Approve and merge into the policy. Idempotent against re-approval
    of the same id (raises if already decided)."""
    req = await _get_request(request_id)
    if req.status != "pending":
        raise ValueError(f"request {request_id} already {req.status}")

    policy = await get_policy(req.tenant_id)
    if req.kind == "host":
        merged = list(policy.allowed_hosts)
        if req.value not in merged:
            merged.append(req.value)
        new_policy = await upsert_policy(
            req.tenant_id, allowed_hosts=merged, actor=actor,
        )
    else:
        merged = list(policy.allowed_cidrs)
        if req.value not in merged:
            merged.append(req.value)
        new_policy = await upsert_policy(
            req.tenant_id, allowed_cidrs=merged, actor=actor,
        )

    db = _conn()
    await db.execute(
        "UPDATE tenant_egress_requests SET status='approved', "
        "decided_by=?, decided_at=datetime('now'), decision_note=? "
        "WHERE id=?",
        (actor, note or "", request_id),
    )
    await db.commit()

    decided = await _get_request(request_id)
    await _audit(
        "tenant_egress.request_approve", req.tenant_id, actor,
        before={"request_id": request_id, "kind": req.kind, "value": req.value},
        after={"note": note or ""},
    )
    return decided, new_policy


async def reject_request(
    request_id: str, *, actor: str, note: str = "",
) -> EgressRequest:
    req = await _get_request(request_id)
    if req.status != "pending":
        raise ValueError(f"request {request_id} already {req.status}")

    db = _conn()
    await db.execute(
        "UPDATE tenant_egress_requests SET status='rejected', "
        "decided_by=?, decided_at=datetime('now'), decision_note=? "
        "WHERE id=?",
        (actor, note or "", request_id),
    )
    await db.commit()

    decided = await _get_request(request_id)
    await _audit(
        "tenant_egress.request_reject", req.tenant_id, actor,
        before={"request_id": request_id, "kind": req.kind, "value": req.value},
        after={"note": note or ""},
    )
    return decided


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DNS resolution + rule plan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _parse_host_port(host: str) -> tuple[str, int | None]:
    if ":" in host:
        name, _, port = host.rpartition(":")
        try:
            return name, int(port)
        except ValueError:
            return host, None
    return host, None


async def resolve_allow_targets(
    policy: EgressPolicy, *, now: float | None = None,
) -> dict[str, list[str]]:
    """Return a {target_label: [ip, ...]} map. CIDRs map to themselves
    (single-element list); hosts map to A/AAAA records with a 5-min
    cache. Hostnames that fail DNS resolve to ``[]`` so callers can
    fold them into a UI warning rather than silently allowing nothing.
    """
    out: dict[str, list[str]] = {}
    for cidr in policy.allowed_cidrs:
        out[cidr] = [cidr]

    now = now or time.monotonic()
    async with _dns_lock:
        loop = asyncio.get_running_loop()
        for host in policy.allowed_hosts:
            name, port = _parse_host_port(host)
            key = (name, port)
            cached = _dns_cache.get(key)
            if cached and cached[1] > now:
                out[host] = list(cached[0])
                continue
            try:
                infos = await loop.getaddrinfo(name, port, type=socket.SOCK_STREAM)
                ips = sorted({ai[4][0] for ai in infos})
            except Exception as exc:
                logger.warning("DNS resolve %s failed: %s", name, exc)
                ips = []
            _dns_cache[key] = (ips, now + _DNS_TTL_S)
            out[host] = ips
    return out


def _reset_dns_cache_for_tests() -> None:
    _dns_cache.clear()


def build_rule_plan(
    policy: EgressPolicy,
    *,
    sandbox_uid: int,
    resolved: dict[str, list[str]] | None = None,
) -> dict:
    """Serialise an iptables / nftables rule plan for the host installer.

    Returns::

        {
          "tenant_id": "...",
          "sandbox_uid": 12345,
          "default_action": "deny",
          "rules": [
            {"action": "ACCEPT", "destination": "1.2.3.4", "label": "api.openai.com"},
            ...
            # implicit terminal "DROP" when default_action == "deny"
          ],
        }

    The shell installer iterates ``rules`` in order. The terminal DROP /
    ACCEPT is materialised by reading ``default_action`` so the file
    format remains forwards-compatible.
    """
    if sandbox_uid < 1:
        raise ValueError(f"sandbox_uid must be a positive int, got {sandbox_uid}")
    if resolved is None:
        resolved = {}
        for cidr in policy.allowed_cidrs:
            resolved[cidr] = [cidr]
        for host in policy.allowed_hosts:
            resolved.setdefault(host, [])

    rules: list[dict] = []
    seen_dests: set[str] = set()
    for label, ips in resolved.items():
        for ip in ips:
            if ip in seen_dests:
                continue
            seen_dests.add(ip)
            rules.append({
                "action": "ACCEPT",
                "destination": ip,
                "label": label,
                "uid_owner": sandbox_uid,
            })
    return {
        "tenant_id": policy.tenant_id,
        "sandbox_uid": sandbox_uid,
        "default_action": policy.default_action,
        "rules": rules,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Convenience helpers used by sandbox launch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def policy_for(tenant_id: str | None = None) -> EgressPolicy:
    """Read-or-fallback wrapper for the launch path.

    If the policy row is missing AND the legacy
    ``OMNISIGHT_T1_EGRESS_ALLOW_HOSTS`` env var is set, build an in-memory
    policy from it (without touching the DB). This lets pre-M6
    deployments keep running while the operator is still on the way to
    the new UI.
    """
    pol = await get_policy(tenant_id)
    if pol.allowed_hosts or pol.allowed_cidrs:
        return pol
    try:
        from backend.config import settings as _settings
    except Exception:
        return pol
    raw = (_settings.t1_egress_allow_hosts or "").strip()
    if not raw:
        return pol
    legacy_hosts: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            v = validate_host(item)
        except ValueError:
            logger.warning("policy_for: skipping invalid legacy host %r", item)
            continue
        if v not in seen:
            seen.add(v)
            legacy_hosts.append(v)
    if not legacy_hosts:
        return pol
    return EgressPolicy(
        tenant_id=pol.tenant_id,
        allowed_hosts=tuple(legacy_hosts),
        allowed_cidrs=(),
        default_action="deny",
        updated_at=None,
        updated_by="legacy-env",
    )


def _resolve_to_csv(resolved: dict[str, list[str]]) -> str:
    """Flatten resolved hosts/CIDRs into the CSV format the legacy
    iptables script expects (``host[:port]`` items)."""
    return ",".join(resolved.keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _audit(action: str, tenant_id: str, actor: str,
                 *, before: dict | None = None, after: dict | None = None) -> None:
    try:
        from backend import audit as _audit_mod
        await _audit_mod.log(
            action=action,
            entity_kind="tenant_egress",
            entity_id=tenant_id,
            before=before,
            after=after,
            actor=actor,
        )
    except Exception as exc:  # pragma: no cover - audit is best-effort
        logger.debug("tenant_egress audit log failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI for the host installer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _cli_emit_rules(tenant_id: str, sandbox_uid: int) -> None:
    """Print the rule plan as JSON. Called by
    ``scripts/apply_tenant_egress.sh``. Runs in a dedicated event loop
    because the host script is sync."""

    async def _go() -> dict:
        from backend import db as _db
        await _db.init()
        try:
            pol = await policy_for(tenant_id)
            resolved = await resolve_allow_targets(pol)
            return build_rule_plan(pol, sandbox_uid=sandbox_uid, resolved=resolved)
        finally:
            await _db.close()

    plan = asyncio.run(_go())
    print(json.dumps(plan, indent=2, sort_keys=True))


def _cli_dump_policies() -> None:
    async def _go() -> list[dict]:
        from backend import db as _db
        await _db.init()
        try:
            pols = await list_policies()
            return [p.to_dict() for p in pols]
        finally:
            await _db.close()

    out = asyncio.run(_go())
    print(json.dumps(out, indent=2, sort_keys=True))


def main(argv: Iterable[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m backend.tenant_egress")
    sub = p.add_subparsers(dest="cmd", required=True)
    er = sub.add_parser("emit-rules", help="Emit the JSON rule plan for one tenant.")
    er.add_argument("--tenant-id", required=True)
    er.add_argument("--sandbox-uid", required=True, type=int)
    sub.add_parser("dump-policies", help="Dump every tenant policy as JSON.")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "emit-rules":
        _cli_emit_rules(args.tenant_id, args.sandbox_uid)
        return 0
    if args.cmd == "dump-policies":
        _cli_dump_policies()
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
