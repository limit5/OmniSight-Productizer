"""OP-2567 U4-B — PURE publication state machine + canonical hashes.

No SQL, no I/O, no imports from the writer layer (one-way dependency:
the publisher imports this module, never the reverse). Operates on the
publication ledger (migration 0258) event model without naming any of
its tables — the sanctioned writer boundary is the ONLY non-test module
allowed to do that.

Frozen per the U4-A0 contract freeze §G3/G4 + forward design v2 R3:

* ``LEGAL_TRANSITIONS`` — the FROZEN transition table. Transition-order
  enforcement happens in U4-C's advisory-locked procedure, which calls
  ``is_legal_transition`` under the lock; the DB trigger (0259) only
  backstops approval linkage.
* ``reduce_publication_state`` folds an ORDERED event iterable
  (oldest → newest — the CALLER supplies order; U4-C orders by the
  ledger's event sequence) down to the latest state.
* ``publication_scope_key`` — freeze V3.3 scope-key encoding.
* ``compute_live_set_hash`` — freeze G4 (v2 supersedes v1-F4's "sorted
  set of version ids"): membership-component hash, order-independent.
* ``advisory_lock_key`` — documents the repo locking idiom
  ``pg_advisory_xact_lock(hashtext($1))`` (see backend/audit.py): key
  derivation is server-side ``hashtext``, so the helper returns the
  scope_key unchanged.

Ships DORMANT — no caller until U4-C.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


PUBLICATION_STATES = (
    "approved",
    "publishing",
    "published",
    "publish_failed",
    "revoked",
    "superseded",
)

# FROZEN transition table (None = no prior event for the version).
# `superseded` records its successor as
# revoke_reason = 'superseded_by:<new_version_id>' (informational);
# the reduction treats `superseded` as out of the live set.
LEGAL_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"approved"}),
    "approved": frozenset({"publishing"}),
    "publishing": frozenset({"published", "publish_failed"}),
    "publish_failed": frozenset({"publishing"}),  # retry
    "published": frozenset({"revoked", "superseded"}),
    "revoked": frozenset(),  # terminal
    "superseded": frozenset(),  # terminal
}

_LIVE_SET_MEMBER_KEYS = frozenset(
    {
        "version_id",
        "rendered_payload_sha256",
        "delivery_mode",
        "publication_event_seq",
    }
)

_FROZEN_AUDIENCES = ("tenant", "global")  # v1 freeze — no 'project' yet


def is_legal_transition(prior: str | None, new: str) -> bool:
    return new in LEGAL_TRANSITIONS.get(prior, frozenset())


def _event_state(event: Any) -> str:
    if isinstance(event, dict):
        return event["state"]
    return event.state


def reduce_publication_state(events: Iterable[Any]) -> str | None:
    """Fold an ORDERED (oldest → newest) event iterable to the latest
    state, or None when there are no events. This module does NOT sort
    — ordering is the caller's contract."""
    state: str | None = None
    for event in events:
        state = _event_state(event)
    return state


def is_live(events: Iterable[Any]) -> bool:
    """True iff the reduction ends in ``published``."""
    return reduce_publication_state(events) == "published"


def publication_scope_key(audience: str, tenant_id: str | None) -> str:
    """Freeze V3.3: ``audience + ":" + (tenant_id or "-")``."""
    if audience not in _FROZEN_AUDIENCES:
        raise ValueError(
            f"publication_scope_key: audience {audience!r} is not in the "
            f"v1 freeze {_FROZEN_AUDIENCES}"
        )
    if audience == "tenant" and tenant_id is None:
        raise ValueError(
            "publication_scope_key: audience 'tenant' requires tenant_id"
        )
    if audience == "global" and tenant_id is not None:
        raise ValueError(
            "publication_scope_key: audience 'global' forbids tenant_id"
        )
    return audience + ":" + (tenant_id if tenant_id is not None else "-")


def compute_live_set_hash(membership: Iterable[dict[str, Any]]) -> str:
    """Freeze G4 canonical live-set hash.

    ``membership`` = iterable of dicts with EXACTLY the keys
    ``{version_id, rendered_payload_sha256, delivery_mode,
    publication_event_seq}``. Canonicalize = sort by ``version_id``
    ascending, then sha256-hex of the compact JSON. Deterministic; any
    component change yields a different hash. Unknown or missing keys
    raise ValueError (loud, not silent).
    """
    members = list(membership)
    for member in members:
        keys = set(member.keys())
        if keys != _LIVE_SET_MEMBER_KEYS:
            missing = _LIVE_SET_MEMBER_KEYS - keys
            unknown = keys - _LIVE_SET_MEMBER_KEYS
            raise ValueError(
                "compute_live_set_hash: membership entry key mismatch "
                f"(missing={sorted(missing)}, unknown={sorted(unknown)})"
            )
    canonical = sorted(members, key=lambda m: m["version_id"])
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def derive_membership_entry(
    *,
    version_id: str,
    rendered_payload_sha256: str,
    delivery_mode: str,
    publication_event_seq: int | None,
) -> dict:
    """OP-2570 U4-C1 — the ONE constructor of live-set membership
    entries (exactly the four G4 keys; ``compute_live_set_hash``
    validates key-exactness downstream)."""
    return {
        "version_id": version_id,
        "rendered_payload_sha256": rendered_payload_sha256,
        "delivery_mode": delivery_mode,
        "publication_event_seq": publication_event_seq,
    }


def check_publication_invariant(
    ledger_membership: Iterable[dict[str, Any]],
    snapshot_membership: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    """OP-2570 U4-C1 — pure comparator for the freeze-V3.4 publication
    invariant (ledger membership == snapshot membership).

    Both arguments are iterables of membership dicts (the G4 shape).
    Returns a tuple of divergence reason strings — empty means the
    invariant holds:

    * ``missing_from_snapshot:<version_id>`` — live per the ledger but
      absent from the snapshot side;
    * ``extra_in_snapshot:<version_id>`` — in the snapshot but not live
      per the ledger;
    * ``hash_mismatch:<version_id>`` — same version on both sides with
      a differing component (rendered sha / delivery mode / seq).

    Deterministic ordering: reasons sort by version_id ascending.
    """
    ledger = {m["version_id"]: m for m in ledger_membership}
    snapshot = {m["version_id"]: m for m in snapshot_membership}
    reasons: list[str] = []
    for version_id in sorted(set(ledger) | set(snapshot)):
        if version_id not in snapshot:
            reasons.append(f"missing_from_snapshot:{version_id}")
        elif version_id not in ledger:
            reasons.append(f"extra_in_snapshot:{version_id}")
        elif ledger[version_id] != snapshot[version_id]:
            reasons.append(f"hash_mismatch:{version_id}")
    return tuple(reasons)


def advisory_lock_key(scope_key: str) -> str:
    """Returns the scope_key unchanged — locking uses the repo idiom
    ``pg_advisory_xact_lock(hashtext($1))``, so the key derivation is
    server-side ``hashtext``. No strip/lower: scope_key is already
    canonical (``publication_scope_key``)."""
    return scope_key
