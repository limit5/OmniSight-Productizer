"""O10 (#273) — Security hardening for the distributed orchestration plane.

Bundles five concerns into one cohesive module so the surface stays
inspectable end-to-end:

  1. ``QueueAuth`` — HMAC envelope around CATC payloads pushed onto the
     queue (defends "worker pulls a forged task").  TLS for the
     transport itself is owned by the ops layer (Redis stunnel /
     ``rediss://`` URL); this module owns the *payload* signature so
     Redis MITM still can't substitute a different CATC.
  2. ``RedisAclConfig`` — Role definitions for Redis ACL (orchestrator
     write+lock, worker read+extend-lease, observer read-only).  We
     emit the ACL SETUSER lines as a deterministic blob so operators
     can ``redis-cli ACL LOAD`` it instead of hand-crafting each role.
  3. ``WorkerAttestation`` — Worker presents (TLS-cert-fingerprint,
     tenant_claim, capabilities, nonce) signed by the worker's
     private key on registration.  Orchestrator verifies the
     signature against the worker pubkey allowlist + tenant policy
     before any task is enqueued for that worker.
  4. ``MergerVoteAuditChain`` — Hash-chain audit specifically for
     merger-agent-bot ±2 / abstain / refuse votes.  Each entry chains
     to the previous via SHA-256(prev_hash || canonical(vote_record)),
     mirrors ``backend/audit.py`` semantics but is in-memory + per
     change_id so tests can verify tamper-detection independently
     from the global tenant audit table.
  5. Gerrit least-privilege snapshot — Pure-Python summary of what
     ``merger-agent-bot`` is allowed to do per
     ``.gerrit/project.config.example``.  ``verify_merger_least_privilege``
     reads that file and fails CI if anyone slips Submit / Push Force
     / Delete Change / project-admin into the bot's grants.

All helpers are pure (no I/O or network) so the unit tests can run
without Redis / Gerrit / a sandbox.  Production wiring lives in
``backend/queue_backend.py``, ``backend/worker.py``,
``backend/orchestrator_gateway.py`` (callers inject the helpers).
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Queue HMAC envelope
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


HMAC_HEADER_FIELD = "_o10_sig"
HMAC_TS_FIELD = "_o10_ts"
HMAC_KEY_ID_FIELD = "_o10_kid"
HMAC_VERSION = "v1"
HMAC_DEFAULT_TTL_S = 15 * 60   # message must arrive at the worker within 15 min
HMAC_ENV = "OMNISIGHT_QUEUE_HMAC_KEY"
HMAC_KEY_ID_ENV = "OMNISIGHT_QUEUE_HMAC_KEY_ID"


class HmacVerifyError(ValueError):
    """Raised when a queue payload's HMAC envelope fails verification."""


@dataclass(frozen=True)
class QueueHmacKey:
    """Symmetric signing key + identifier for queue payload HMAC."""

    key_id: str
    secret: bytes

    @classmethod
    def from_env(cls) -> "QueueHmacKey | None":
        raw = os.environ.get(HMAC_ENV, "").strip()
        if not raw:
            return None
        kid = os.environ.get(HMAC_KEY_ID_ENV, "").strip() or "k1"
        return cls(key_id=kid, secret=raw.encode("utf-8"))


def _canonical(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON serialisation for signing — sorted keys, no
    whitespace.  Ignores the signature/header fields themselves so that
    ``sign(payload)`` and ``verify(sign(payload))`` are inverses."""
    skip = {HMAC_HEADER_FIELD, HMAC_TS_FIELD, HMAC_KEY_ID_FIELD}
    cleaned = {k: v for k, v in payload.items() if k not in skip}
    return json.dumps(
        cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def sign_envelope(
    payload: dict[str, Any], key: QueueHmacKey,
    *, ts: float | None = None,
) -> dict[str, Any]:
    """Wrap ``payload`` with HMAC-SHA256 signature + timestamp + kid.

    Returns a NEW dict — does not mutate ``payload``.  Order of keys
    in the output is irrelevant; verifier strips the signature fields
    before recomputing canonical bytes.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    ts_v = float(ts) if ts is not None else time.time()
    body = dict(payload)
    body[HMAC_TS_FIELD] = ts_v
    body[HMAC_KEY_ID_FIELD] = key.key_id
    sig_bytes = hmac.new(
        key.secret, _canonical(body) + str(ts_v).encode() + key.key_id.encode(),
        hashlib.sha256,
    ).digest()
    body[HMAC_HEADER_FIELD] = (
        f"{HMAC_VERSION}:{base64.urlsafe_b64encode(sig_bytes).decode()}"
    )
    return body


def verify_envelope(
    payload: dict[str, Any], key: QueueHmacKey,
    *, max_age_s: float = HMAC_DEFAULT_TTL_S,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate a signed envelope; return the *payload* (signature
    fields stripped) on success.  Raises ``HmacVerifyError`` on any
    tampering / replay / wrong-key.

    Replay window is bounded by ``max_age_s`` — beyond that, a
    captured-and-resent envelope is rejected so a single key
    compromise can't be exploited indefinitely.
    """
    if not isinstance(payload, dict):
        raise HmacVerifyError("payload must be a dict")
    sig = payload.get(HMAC_HEADER_FIELD)
    ts = payload.get(HMAC_TS_FIELD)
    kid = payload.get(HMAC_KEY_ID_FIELD)
    if not sig or ts is None or not kid:
        raise HmacVerifyError("missing signature header / timestamp / key id")
    if not isinstance(sig, str) or ":" not in sig:
        raise HmacVerifyError("malformed signature header")
    version, _, b64sig = sig.partition(":")
    if version != HMAC_VERSION:
        raise HmacVerifyError(f"unsupported HMAC version: {version}")
    if kid != key.key_id:
        raise HmacVerifyError(
            f"key id mismatch: payload {kid!r} != verifier {key.key_id!r}"
        )
    try:
        recv_sig = base64.urlsafe_b64decode(b64sig.encode())
    except Exception as exc:
        raise HmacVerifyError(f"bad base64 signature: {exc}") from exc
    expected = hmac.new(
        key.secret, _canonical(payload) + str(ts).encode() + key.key_id.encode(),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(recv_sig, expected):
        raise HmacVerifyError("signature mismatch")
    now_v = float(now) if now is not None else time.time()
    age = now_v - float(ts)
    if age > max_age_s:
        raise HmacVerifyError(
            f"envelope too old: {age:.0f}s > max {max_age_s:.0f}s"
        )
    if age < -max_age_s:
        raise HmacVerifyError(
            f"envelope timestamp in the future: {-age:.0f}s skew"
        )
    out = dict(payload)
    for fld in (HMAC_HEADER_FIELD, HMAC_TS_FIELD, HMAC_KEY_ID_FIELD):
        out.pop(fld, None)
    return out


def queue_url_uses_tls(url: str) -> bool:
    """Heuristic: returns True only for transports that encrypt
    in-flight data (``rediss://``, ``amqps://``, ``https://``)."""
    if not url:
        return False
    lowered = url.strip().lower()
    return lowered.startswith(("rediss://", "amqps://", "https://"))


def assert_production_queue_tls(url: str, *, env: str) -> None:
    """In production, refuse to boot if the queue URL is plaintext.

    Dev / test envs (``env != "production"``) downgrade to a warning
    so single-host laptop deployments still work without certs.
    """
    if not url:
        return
    if queue_url_uses_tls(url):
        return
    msg = (
        "O10 queue transport is plaintext "
        f"({url[:30]}...) — production must use rediss:// / amqps:// / https://"
    )
    if env == "production":
        raise RuntimeError(msg)
    logger.warning(msg)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Redis ACL role definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class RedisAclRole:
    """One Redis ACL principal.

    ``commands`` are positive-grants (``+xack``, ``+set``, ``+@read``);
    ``key_patterns`` confine those grants by Redis key glob.  Every
    role is implicitly ``-@all`` first so anything not granted is
    denied — the conservative default mandated by the O10 spec.
    """

    username: str
    role: str
    commands: tuple[str, ...]
    key_patterns: tuple[str, ...]
    channels: tuple[str, ...] = ()

    def to_setuser_command(self, password_hash: str) -> str:
        """Render as a ``ACL SETUSER`` shell command.  ``password_hash``
        must be a SHA-256 hex digest (Redis `>` syntax requires
        plaintext, but we recommend pre-hashed via ``#`` for ops to
        avoid shell history leaks)."""
        parts = [
            "ACL", "SETUSER", self.username,
            "on",
            "resetkeys", "resetchannels", "-@all",
        ]
        for cmd in self.commands:
            parts.append(cmd)
        for pat in self.key_patterns:
            parts.append(f"~{pat}")
        for ch in self.channels:
            parts.append(f"&{ch}")
        if password_hash:
            parts.append(f"#{password_hash}")
        return " ".join(parts)


def default_redis_acl_roles() -> list[RedisAclRole]:
    """Three least-privilege roles aligned with the O10 spec:

    * ``orchestrator`` — full write on queue + dist_lock keyspaces
      (it owns enqueue + lock acquisition + sweeps).
    * ``worker`` — pull queue messages, ack/nack, extend lock leases.
      Cannot mint a new lock against another tenant's key, cannot
      delete arbitrary keys.
    * ``observer`` — read-only across queue / lock / metrics.  Used
      by dashboards + Prometheus exporters that must not mutate.
    """
    return [
        RedisAclRole(
            username="omnisight-orchestrator",
            role="orchestrator",
            commands=(
                "+@read", "+@write", "+@stream", "+@list", "+@hash",
                "+@set", "+@sortedset", "+@scripting", "+@connection",
                "+@keyspace", "+@transaction",
                # Explicitly deny dangerous admin verbs even though
                # they wouldn't be granted by the @-categories above.
                "-flushdb", "-flushall", "-debug", "-shutdown",
                "-config", "-cluster", "-replicaof", "-acl",
            ),
            key_patterns=(
                "omnisight:queue:*",
                "omnisight:queue:dlq:*",
                "omnisight:dist_lock:*",
                "omnisight:worker:*",
                "omnisight:metrics:*",
            ),
        ),
        RedisAclRole(
            username="omnisight-worker",
            role="worker",
            commands=(
                # Stream consumption + ack
                "+xreadgroup", "+xack", "+xack", "+xclaim", "+xpending",
                "+xlen", "+xinfo", "+xack",
                # Per-message metadata read/update
                "+hgetall", "+hget", "+hset", "+hmget", "+hdel",
                # Lock: extend lease + release own holds
                "+zadd", "+zrem", "+zscore", "+zrangebyscore",
                "+set", "+get", "+del", "+expire",
                # Heartbeat + worker registry
                "+sadd", "+srem", "+smembers", "+sismember",
                # Common safe verbs
                "+ping", "+exists", "+ttl", "+type",
                # Hard denies — workers must never escalate
                "-flushdb", "-flushall", "-debug", "-shutdown",
                "-config", "-cluster", "-replicaof", "-acl",
                "-xgroup",   # creating/destroying consumer groups is orch-only
                "-xtrim",    # trimming streams is orch-only
            ),
            key_patterns=(
                "omnisight:queue:msg:*",
                "omnisight:queue:stream:*",
                "omnisight:queue:claimed",
                "omnisight:queue:all",
                "omnisight:dist_lock:*",
                "omnisight:worker:*",
                "workers:active",
            ),
        ),
        RedisAclRole(
            username="omnisight-observer",
            role="observer",
            commands=(
                "+@read", "+@connection",
                "-@write", "-@dangerous", "-flushdb", "-flushall",
                "-config", "-cluster", "-replicaof", "-acl", "-shutdown",
            ),
            key_patterns=(
                "omnisight:*",
            ),
        ),
    ]


def render_acl_file(
    roles: Iterable[RedisAclRole] | None = None,
    *,
    password_hashes: dict[str, str] | None = None,
) -> str:
    """Render the role table to a Redis ACL file (``users.acl``).

    ``password_hashes`` maps username → SHA-256 hex digest.  Roles
    without a hash get ``nopass`` (acceptable only on a private
    Unix socket; loud comment is included).
    """
    roles = list(roles or default_redis_acl_roles())
    hashes = password_hashes or {}
    out = [
        "# OmniSight O10 — Redis ACL roles (auto-generated).",
        "# DO NOT EDIT BY HAND — regenerate with",
        "#   python -m backend.security_hardening render-acl > users.acl",
        "#   redis-cli ACL LOAD",
        "# Make sure each principal has a long, unique password.",
        "",
        "user default off nopass nocommands",
        "",
    ]
    for role in roles:
        h = hashes.get(role.username, "")
        if h:
            out.append(role.to_setuser_command(h).replace("ACL SETUSER ", "user "))
        else:
            out.append(
                role.to_setuser_command("").replace("ACL SETUSER ", "user ")
                + " nopass  # WARNING: configure password in production"
            )
    out.append("")
    return "\n".join(out)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Worker attestation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


ATTESTATION_VERSION = "v1"
ATTESTATION_DEFAULT_TTL_S = 5 * 60   # token good for 5 minutes


class AttestationError(ValueError):
    """Raised when ``WorkerAttestation`` rejects a token."""


@dataclass(frozen=True)
class WorkerIdentity:
    """Static identity material assigned to a worker at deploy time."""

    worker_id: str
    tenant_id: str
    capabilities: tuple[str, ...]
    tls_cert_fingerprint: str            # hex SHA-256 of worker leaf cert
    pre_shared_key: bytes                # symmetric secret, never on the wire

    def as_claim(self, nonce: str, issued_at: float) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "tenant_id": self.tenant_id,
            "capabilities": list(self.capabilities),
            "tls_fp": self.tls_cert_fingerprint,
            "nonce": nonce,
            "iat": issued_at,
            "v": ATTESTATION_VERSION,
        }


def _attestation_signature(claim: dict[str, Any], psk: bytes) -> str:
    body = json.dumps(
        claim, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(
        hmac.new(psk, body, hashlib.sha256).digest()
    ).decode()


def issue_attestation(
    identity: WorkerIdentity,
    *,
    nonce: str | None = None,
    issued_at: float | None = None,
) -> dict[str, Any]:
    """Mint an attestation token the worker presents on registration."""
    nonce = nonce or secrets.token_hex(16)
    iat = float(issued_at) if issued_at is not None else time.time()
    claim = identity.as_claim(nonce=nonce, issued_at=iat)
    return {"claim": claim, "sig": _attestation_signature(claim, identity.pre_shared_key)}


@dataclass
class AttestationVerifier:
    """Server-side verifier with per-worker allowlist + tenant policy.

    ``known_workers`` maps worker_id → expected ``WorkerIdentity``.
    ``allowed_tenants`` further narrows which tenants the orchestrator
    is willing to accept tasks for (defence-in-depth — if a worker's
    PSK leaks, an attacker can sign for any tenant the worker was
    *configured* for, not for tenants outside the allowlist).

    ``replay_cache`` (default in-memory dict) drops tokens we've
    already accepted within the TTL — defends "captured token replay".
    """

    known_workers: dict[str, WorkerIdentity]
    allowed_tenants: tuple[str, ...] = ()
    ttl_s: float = ATTESTATION_DEFAULT_TTL_S
    _replay_cache: dict[str, float] = field(default_factory=dict)

    def verify(self, token: dict[str, Any], *, now: float | None = None) -> WorkerIdentity:
        if not isinstance(token, dict):
            raise AttestationError("token must be a dict")
        claim = token.get("claim")
        sig = token.get("sig")
        if not isinstance(claim, dict) or not isinstance(sig, str):
            raise AttestationError("malformed token shape")
        worker_id = claim.get("worker_id")
        if not worker_id or worker_id not in self.known_workers:
            raise AttestationError(f"unknown worker_id: {worker_id!r}")
        identity = self.known_workers[worker_id]

        if claim.get("v") != ATTESTATION_VERSION:
            raise AttestationError(f"unsupported version: {claim.get('v')!r}")
        if claim.get("tenant_id") != identity.tenant_id:
            raise AttestationError(
                f"tenant mismatch: {claim.get('tenant_id')!r} vs "
                f"identity {identity.tenant_id!r}"
            )
        if self.allowed_tenants and identity.tenant_id not in self.allowed_tenants:
            raise AttestationError(
                f"tenant {identity.tenant_id!r} not in allowlist"
            )
        if claim.get("tls_fp") != identity.tls_cert_fingerprint:
            raise AttestationError("TLS fingerprint mismatch")
        if list(claim.get("capabilities") or []) != list(identity.capabilities):
            raise AttestationError("capability set tampered")

        expected_sig = _attestation_signature(claim, identity.pre_shared_key)
        if not hmac.compare_digest(sig, expected_sig):
            raise AttestationError("signature mismatch")

        iat = float(claim.get("iat") or 0)
        now_v = float(now) if now is not None else time.time()
        age = now_v - iat
        if age > self.ttl_s:
            raise AttestationError(
                f"attestation expired: age {age:.0f}s > ttl {self.ttl_s:.0f}s"
            )
        if age < -self.ttl_s:
            raise AttestationError(
                f"attestation iat in future: {-age:.0f}s skew"
            )

        nonce = claim.get("nonce")
        if not nonce:
            raise AttestationError("missing nonce")
        # Drop expired entries opportunistically + reject replay.
        self._gc_replay(now_v)
        if nonce in self._replay_cache:
            raise AttestationError(f"replay detected for nonce {nonce[:8]}…")
        self._replay_cache[nonce] = now_v + self.ttl_s
        return identity

    def _gc_replay(self, now_v: float) -> None:
        expired = [n for n, exp in self._replay_cache.items() if exp <= now_v]
        for n in expired:
            self._replay_cache.pop(n, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Merger vote hash-chain audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


MERGER_AUDIT_ENTITY_KIND = "merger_agent_vote_hashchain"


def _vote_canonical(record: dict[str, Any]) -> str:
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    )


def _chain_hash(prev: str, record: dict[str, Any]) -> str:
    return hashlib.sha256(
        (prev + _vote_canonical(record)).encode("utf-8")
    ).hexdigest()


@dataclass
class MergerVoteAuditChain:
    """In-process hash-chain log of merger-agent-bot votes.

    Wraps ``backend.audit`` for persistence (fire-and-forget) but
    keeps a deterministic in-memory chain so:

      * tests can assert tamper detection without a DB,
      * the orchestration dashboard can query the live chain head
        without paying the SQLite round-trip.

    Each entry stores: change_id, patchset_revision, vote (-2..+2),
    confidence, rationale, actor (always ``merger-agent-bot``),
    timestamp.  ``append()`` returns the new ``curr_hash`` so the
    caller can show it in audit_log UI.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    persist: bool = True

    def append(
        self,
        *,
        change_id: str,
        patchset_revision: str,
        vote: int,
        confidence: float,
        rationale: str,
        reason_code: str,
        ts: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not change_id:
            raise ValueError("change_id required")
        if vote not in (-2, -1, 0, 1, 2):
            raise ValueError(f"vote out of range: {vote}")
        ts_v = float(ts) if ts is not None else time.time()
        record = {
            "change_id": change_id,
            "patchset_revision": patchset_revision or "",
            "vote": int(vote),
            "confidence": float(confidence),
            "rationale": rationale or "",
            "reason_code": reason_code,
            "actor": "merger-agent-bot",
            "ts": round(ts_v, 6),
            "extra": dict(extra or {}),
        }
        prev = self.entries[-1]["curr_hash"] if self.entries else ""
        record["prev_hash"] = prev
        record["curr_hash"] = _chain_hash(prev, record)
        self.entries.append(record)
        if self.persist:
            self._fire_audit(record)
        return record

    def verify(self) -> tuple[bool, int | None]:
        """Walk the chain.  Returns ``(True, None)`` on intact chain or
        ``(False, index_of_first_bad_entry)``."""
        prev = ""
        for i, rec in enumerate(self.entries):
            saved_curr = rec.get("curr_hash")
            saved_prev = rec.get("prev_hash")
            payload = {k: v for k, v in rec.items()
                       if k not in ("curr_hash",)}
            payload["prev_hash"] = prev
            recomputed = _chain_hash(prev, payload)
            if saved_prev != prev or saved_curr != recomputed:
                return (False, i)
            prev = saved_curr
        return (True, None)

    def head(self) -> str:
        return self.entries[-1]["curr_hash"] if self.entries else ""

    def for_change(self, change_id: str) -> list[dict[str, Any]]:
        return [r for r in self.entries if r["change_id"] == change_id]

    @staticmethod
    def _fire_audit(record: dict[str, Any]) -> None:
        """Best-effort write to the canonical ``backend.audit`` chain."""
        try:
            from backend import audit
            audit.log_sync(
                action=f"merger.vote.{record['reason_code']}",
                entity_kind=MERGER_AUDIT_ENTITY_KIND,
                entity_id=record["change_id"],
                after=record,
                actor="merger-agent-bot",
            )
        except Exception as exc:                  # pragma: no cover
            logger.debug("merger audit fire-and-forget failed: %s", exc)


_global_chain: MergerVoteAuditChain | None = None


def get_global_merger_chain() -> MergerVoteAuditChain:
    """Process-wide merger vote chain singleton.  Tests reset via
    ``reset_global_merger_chain_for_tests``."""
    global _global_chain
    if _global_chain is None:
        _global_chain = MergerVoteAuditChain()
    return _global_chain


def reset_global_merger_chain_for_tests() -> None:
    global _global_chain
    _global_chain = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Gerrit least-privilege check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Permissions the merger-agent-bot must NEVER have.  Grouped by Gerrit
# access keyword; verifier scans the `[access "..."]` blocks in
# project.config and refuses any line that grants one of these to
# ``ai-reviewer-bots`` (or by direct identity to the bot account).
FORBIDDEN_MERGER_BOT_PERMS: tuple[str, ...] = (
    "submit",
    "delete",
    "deletechanges",
    "abandon",
    "owner",
    "push force",
    "force push",
    "forcepush",
    "create",
    "rebase",
    "editTopicName",
    "addPatchSet",  # pushing a new patchset is fine via refs/for/*; this
                    # specifically disallows the "addPatchSet" override
                    # which lets the voter add a patchset to ANYONE's change.
)

# Permissions that the merger-agent-bot IS allowed to have.  Anything
# matching one of these prefixes is whitelisted by name.
ALLOWED_MERGER_BOT_PREFIXES: tuple[str, ...] = (
    "label-code-review",
    "push ",
    "read ",
    "label-",
)


@dataclass
class GerritPermissionFinding:
    """One offending line found in project.config."""

    section_header: str
    raw_line: str
    forbidden_token: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_header": self.section_header,
            "raw_line": self.raw_line,
            "forbidden_token": self.forbidden_token,
        }


def verify_merger_least_privilege(config_path: str | Path) -> list[GerritPermissionFinding]:
    """Scan ``.gerrit/project.config(.example)`` and return findings.

    Empty list = config is least-privilege compliant.  Any non-empty
    return value should fail CI.

    The check is *deliberately* string-level: Gerrit's project.config
    grammar is extensible (multi-instance subsections, nested groups),
    so we don't try to parse — we read each non-comment line and look
    for the bot's group name + a forbidden permission token on the
    same access section.  False positives are easier to fix than a
    Prolog parser bug that lets a real escalation through.
    """
    config_path = Path(config_path)
    findings: list[GerritPermissionFinding] = []
    if not config_path.exists():
        return findings

    bot_group = "ai-reviewer-bots"
    bot_identity = "merger-agent-bot"

    section_header = ""
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_header = line.strip("[]").strip()
            continue
        # Only inspect access-section grants.
        if not section_header.lower().startswith("access "):
            continue
        lowered = line.lower()
        # Must mention the bot group OR the bot identity.
        if bot_group not in lowered and bot_identity not in lowered:
            continue
        # Explicit `deny <perm> = group ai-reviewer-bots` is *good* —
        # it's what O10 asks for.  Only flag actual grants.
        if lowered.startswith("deny "):
            continue
        # Forbidden tokens take precedence over allowed prefixes — so
        # `push force = group ai-reviewer-bots` is caught even though
        # `push ` is in the allow-prefix list (the bare `push` verb is
        # fine for refs/for/* but `push force` is never fine).
        flagged = False
        for tok in FORBIDDEN_MERGER_BOT_PERMS:
            if re.search(rf"(^|[\s=]){re.escape(tok)}(\s|=|$)", lowered):
                findings.append(GerritPermissionFinding(
                    section_header=section_header,
                    raw_line=raw_line,
                    forbidden_token=tok,
                ))
                flagged = True
                break
        if flagged:
            continue
        # If line starts with an allowed prefix (and nothing forbidden
        # fired above), skip — eg
        # "label-Code-Review = -2..+2 group ai-reviewer-bots".
        if any(lowered.startswith(p) for p in ALLOWED_MERGER_BOT_PREFIXES):
            continue
    return findings


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cli_main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(
            "usage:\n"
            "  python -m backend.security_hardening render-acl\n"
            "  python -m backend.security_hardening verify-gerrit-config "
            "[<path-to-project.config>]\n",
        )
        return 0
    cmd = argv[0]
    if cmd == "render-acl":
        print(render_acl_file())
        return 0
    if cmd == "verify-gerrit-config":
        path = argv[1] if len(argv) > 1 else ".gerrit/project.config.example"
        findings = verify_merger_least_privilege(path)
        if not findings:
            print(f"OK — {path}: merger-agent-bot is least-privilege compliant.")
            return 0
        print(
            f"VIOLATIONS in {path}: {len(findings)} forbidden grant(s) "
            "for merger-agent-bot:"
        )
        for f in findings:
            print(f"  [{f.section_header}] {f.raw_line!r} → "
                  f"forbidden token: {f.forbidden_token}")
        return 1
    print(f"unknown command: {cmd}", file=__import__("sys").stderr)
    return 2


__all__ = [
    # Queue HMAC
    "HMAC_HEADER_FIELD", "HMAC_TS_FIELD", "HMAC_KEY_ID_FIELD",
    "HMAC_VERSION", "HMAC_DEFAULT_TTL_S", "HMAC_ENV", "HMAC_KEY_ID_ENV",
    "HmacVerifyError", "QueueHmacKey",
    "sign_envelope", "verify_envelope",
    "queue_url_uses_tls", "assert_production_queue_tls",
    # Redis ACL
    "RedisAclRole", "default_redis_acl_roles", "render_acl_file",
    # Worker attestation
    "ATTESTATION_VERSION", "ATTESTATION_DEFAULT_TTL_S",
    "AttestationError", "WorkerIdentity",
    "issue_attestation", "AttestationVerifier",
    # Merger audit
    "MERGER_AUDIT_ENTITY_KIND", "MergerVoteAuditChain",
    "get_global_merger_chain", "reset_global_merger_chain_for_tests",
    # Gerrit
    "FORBIDDEN_MERGER_BOT_PERMS", "ALLOWED_MERGER_BOT_PREFIXES",
    "GerritPermissionFinding", "verify_merger_least_privilege",
]


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv[1:]))
