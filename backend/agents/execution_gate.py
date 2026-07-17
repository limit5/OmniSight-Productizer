"""Concrete, default-safe pluggables for the U6-0 execution service (dormant).

Default-DENY: real execution requires BOTH OMNISIGHT_U6_EXECUTE truthy AND the
(adapter, tool) explicitly allowlisted. Even when authorized, the executor here
is a NO-OP that performs no side effect (the real per-tool executor is GAP-5d).
Mirrors backend.agents.action_executor's P5 flag+allowlist gate.
"""

import json
import os
import uuid
from collections.abc import Mapping

from backend.agents.execution_contract import Applied, DefinitelyNotApplied, StoredAction


def execution_enabled() -> bool:
    # OFF unless the environment explicitly opts in (identical predicate to
    # P5's is_execute_enabled).
    return os.environ.get("OMNISIGHT_U6_EXECUTE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def execute_allowlist() -> frozenset[str]:
    # Comma-separated "adapter_namespace:tool_name" keys; DEFAULT EMPTY (more
    # conservative than P5). Keep only well-formed entries: non-empty, no
    # dash-lead (P5 EXEC-03 guard), and a ':' present. A colon-less entry can
    # never match a real f"{adapter}:{tool}" key.
    raw = os.environ.get("OMNISIGHT_U6_EXECUTE_ALLOWLIST", "").strip()
    if not raw:
        return frozenset()
    return frozenset(
        entry
        for item in raw.split(",")
        if (entry := item.strip())
        and not entry.startswith("-")
        and ":" in entry
    )


_REQUIRED_IDENTITY = (
    "tenant_id",
    "principal_type",
    "actor_id",
    "adapter_namespace",
    "tool_name",
)


async def authorizer(identity: Mapping) -> bool:
    """Apply the final policy gate on the claim path, denying on any error.

    Authorization is a global (adapter, tool) capability gate. Per-tenant and
    principal policy is a future refinement. All identity fields are still
    validated so a malformed identity denies.
    """
    try:
        if not execution_enabled():
            return False
        # All five caller fields must be present, strings, and non-empty.
        for key in _REQUIRED_IDENTITY:
            value = identity.get(key)
            if not isinstance(value, str) or not value:
                return False
        adapter = identity["adapter_namespace"]
        tool = identity["tool_name"]
        # Tool names may contain ':'. Reject it in the adapter so the compound
        # key remains unambiguous: the adapter is the prefix before the first
        # colon.
        if ":" in adapter:
            return False
        return f"{adapter}:{tool}" in execute_allowlist()
    except Exception:  # noqa: BLE001 — never raise into the claim transaction
        return False


async def noop_executor(stored: StoredAction) -> DefinitelyNotApplied:
    """Perform no side effect and report DefinitelyNotApplied honestly.

    This executor remains wired until GAP-5d. The terminal resolver fails the
    grant and records a definitely_not_applied attempt. It never reports
    Applied, so it cannot falsely mark a grant consumed.
    """
    return DefinitelyNotApplied(
        error="",
        evidence="noop: real execution not implemented (GAP-5d)",
    )


def mint_attempt_id() -> str:
    return "exec-" + uuid.uuid4().hex


_MAX_RECEIPT_BYTES = 65536


def result_of(outcome: Applied) -> str:
    # Called only for Applied. This runs after the effect, inside the finalize
    # transaction, and is cast to JSONB. It must be total over arbitrary result
    # values and never throw, or finalization could roll back after the effect.
    try:
        receipt = json.dumps(
            {"result": dict(outcome.result), "evidence": outcome.evidence},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        )
    except Exception:  # noqa: BLE001 — receipt failure must not roll back an effect
        receipt = json.dumps(
            {
                "result_serialization": "unavailable",
                "evidence": str(outcome.evidence)[:1024],
            },
            separators=(",", ":"),
        )
    if len(receipt.encode("utf-8")) > _MAX_RECEIPT_BYTES:
        receipt = json.dumps(
            {
                "result_serialization": "oversize",
                "evidence": str(outcome.evidence)[:1024],
            },
            separators=(",", ":"),
        )
    return receipt
