"""OP-2568 U4-D — memory-promotion approvals WRITER.

The freeze's REAL gate (G3) is a human-only ``memory_approvals`` row
bound to the exact ``live_set_hash`` and referencing a promote-decision
eval run. This module is the ONLY sanctioned writer of that table; the
dedicated router delegates all DB work here. Approval does NOT publish
— U4-C's publisher revalidates and publishes later. Ships DORMANT: the
endpoint is live but the ledger stays empty until U4-I produces
candidates, and nothing schedules the digest until U4-J.

``conn`` is a parameter everywhere (pure seam) — this module never
imports backend.db_pool nor initialises a pool; reads/writes speak
asyncpg (``fetchrow``/``execute`` with ``$N`` placeholders).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException


class ApprovalValidationError(ValueError):
    """Fail-closed approval validation failure. ``reason`` is a stable
    machine-readable code the router surfaces as the 422 detail."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_LIVE_SET_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Bot-shaped identity: "ai-"/"ci-" at a token boundary, or "-bot"
# anywhere. Kept NARROW deliberately — "apikey:"-shaped strings are the
# upstream principal gate's job, not this pattern's.
_BOT_PREFIX_RE = re.compile(r"(^|[^a-z0-9])(ai-|ci-)")


def _is_bot_shaped(value: str) -> bool:
    v = value.lower()
    return "-bot" in v or bool(_BOT_PREFIX_RE.search(v))


def assert_human_principal(user: Any) -> None:
    """Raise HTTPException(403) unless the principal is a real human.

    Role checks can NEVER prove a human: api-key principals are minted
    with role="admin" by construction, and the legacy bearer / open
    auth mode yields the anonymous synthetic super_admin. This is the
    U4-D principal gate, reused by later increments.
    """
    if user.id.startswith("apikey:"):
        raise HTTPException(
            status_code=403,
            detail="memory-promotion approval requires a human principal "
                   "(api-key principals are not humans)",
        )
    if user.id == "anonymous":
        raise HTTPException(
            status_code=403,
            detail="memory-promotion approval requires a human principal "
                   "(anonymous principal cannot prove a human)",
        )
    for v in (user.id.lower(), user.email.lower(), (user.name or "").lower()):
        if _is_bot_shaped(v):
            raise HTTPException(
                status_code=403,
                detail="memory-promotion approval requires a human "
                       "principal (bot-shaped identity rejected)",
            )


async def get_version_audience(conn: Any, version_id: str) -> str:
    """Server-derived audience of a version row — NEVER trust a
    body-supplied audience (freeze G6 gates key off this value)."""
    row = await conn.fetchrow(
        "SELECT audience FROM learned_item_versions WHERE id = $1",
        version_id,
    )
    if row is None:
        raise ApprovalValidationError("version_not_found")
    return row[0]


async def record_memory_approval(
    conn: Any,
    *,
    approval_id: str,
    version_id: str,
    eval_run_id: str,
    live_set_hash: str,
    approved_by: str,
    digest_id: str | None = None,
) -> None:
    """Fail-closed validation THEN insert of one approvals row.

    Pure-shape checks run FIRST, before ANY DB call; ``approved_at``
    has a DB default and is never passed.
    """
    # 1. pure, pre-DB: hash shape.
    # fullmatch: `$` alone would tolerate a trailing newline.
    if not _LIVE_SET_HASH_RE.fullmatch(live_set_hash):
        raise ApprovalValidationError("bad_live_set_hash")
    # 2. pure, pre-DB: approved_by non-empty + bot-pattern rejection.
    if not approved_by or _is_bot_shaped(approved_by):
        raise ApprovalValidationError("bot_approved_by")
    # 3. DB: version row exists — and (β-3a, kernel-audit F2) its STORED
    #    renderer_version matches the CURRENT renderer. Nothing else
    #    compares them, so without this gate pre-u4r2 rows (imperative
    #    headings) would remain approvable forever; stale rows are
    #    unapprovable BY DESIGN (re-submit re-renders under the current
    #    renderer).
    version_row = await conn.fetchrow(
        "SELECT renderer_version FROM learned_item_versions WHERE id = $1",
        version_id,
    )
    if version_row is None:
        raise ApprovalValidationError("version_not_found")
    from backend.learned_item_renderer import RENDERER_VERSION

    if (version_row["renderer_version"] or "") != RENDERER_VERSION:
        raise ApprovalValidationError("stale_renderer")
    # 4. DB: an approval may only ever bind a promote-decision eval run
    #    of the SAME version (freeze F3).
    eval_row = await conn.fetchrow(
        "SELECT ran_at, live_set_hash FROM memory_eval_runs "
        "WHERE id = $1 AND version_id = $2 AND decision = 'promote'",
        eval_run_id,
        version_id,
    )
    if eval_row is None:
        raise ApprovalValidationError("eval_run_not_promote")
    # 5. β-3a bearer-token semantics (gate-audit F5): a promote run is NOT
    #    a forever-token. Code-gate checks (append-only table, autocommit;
    #    the DB-invariant hardening lands with β-3b's migration).
    #    (a) the bound run must be the version's LATEST run (ties fail
    #        closed — uuids carry no order);
    bound_ran_at = eval_row["ran_at"]
    later = await conn.fetchrow(
        "SELECT 1 FROM memory_eval_runs "
        "WHERE version_id = $1 AND (ran_at > $2 OR (ran_at = $2 AND id <> $3)) "
        "LIMIT 1",
        version_id, bound_ran_at, eval_run_id,
    )
    if later is not None:
        raise ApprovalValidationError("eval_run_not_latest")
    #    (b) freshness bound — Python compare, not SQL interval math (the
    #        sqlite adapter path has no make_interval; ran_at may arrive
    #        as a naive string there).
    max_age_days = _approval_max_run_age_days()
    ran_at_dt = _coerce_ran_at(bound_ran_at)
    if ran_at_dt is not None and (
        datetime.now(timezone.utc) - ran_at_dt > timedelta(days=max_age_days)
    ):
        raise ApprovalValidationError("eval_run_stale")
    #    (c) any later reject blocks (independent invariant; implied by (a)
    #        under conservative ties but kept explicit).
    later_reject = await conn.fetchrow(
        "SELECT 1 FROM memory_eval_runs "
        "WHERE version_id = $1 AND decision = 'reject' "
        "AND ran_at >= $2 AND id <> $3 LIMIT 1",
        version_id, bound_ran_at, eval_run_id,
    )
    if later_reject is not None:
        raise ApprovalValidationError("later_reject_exists")
    #    (d) β-3a wiring F3: the approval's hash must equal the live set the
    #        eval evidence was actually measured against.
    if (eval_row["live_set_hash"] or "") != live_set_hash:
        raise ApprovalValidationError("live_set_hash_mismatch")
    await conn.execute(
        "INSERT INTO memory_approvals "
        "(id, version_id, eval_run_id, live_set_hash, approved_by, "
        " digest_id) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        approval_id,
        version_id,
        eval_run_id,
        live_set_hash,
        approved_by,
        digest_id,
    )


def _approval_max_run_age_days() -> int:
    raw = os.environ.get(
        "OMNISIGHT_MEMORY_APPROVAL_MAX_RUN_AGE_DAYS", "",
    ).strip()
    try:
        return max(int(raw), 1) if raw else 30
    except ValueError:
        return 30


def _coerce_ran_at(value: Any) -> "datetime | None":
    """Tolerant ran_at coercion (PG returns aware datetimes; the sqlite
    adapter path returns naive ISO strings). None ⇒ skip the age check."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


_EVAL_SUMMARY_KEYS = frozenset({"decision", "mcnemar_p", "net_flips", "n"})

_CARD_KEYS = (
    "version_id",
    "kind",
    "audience",
    "scope_key",
    "eval_summary",
    "live_set_hash",
    "provenance_change_id",
    "rendered_card_body",
)


def build_approval_card(
    *,
    version_id: str,
    kind: str,
    audience: str,
    scope_key: str,
    eval_summary: dict,
    live_set_hash: str,
    provenance_change_id: str,
    rendered_card_body: str,
) -> dict:
    """PURE builder of the freeze-G7 digest card. The rendered body is
    PASSED IN (it is A1's stored ``rendered_payload`` bytes — the U4-A2
    renderer is never imported here). ``eval_summary`` rejects unknown
    keys loudly; missing keys are allowed at build time (the digest
    ranking indexes ``mcnemar_p``/``net_flips`` directly and fails
    loudly if a caller omits them)."""
    unknown = set(eval_summary) - _EVAL_SUMMARY_KEYS
    if unknown:
        raise ValueError(
            f"build_approval_card: unknown eval_summary keys "
            f"{sorted(unknown)!r} — allowed: {sorted(_EVAL_SUMMARY_KEYS)}"
        )
    return {
        "version_id": version_id,
        "kind": kind,
        "audience": audience,
        "scope_key": scope_key,
        "eval_summary": dict(eval_summary),
        "live_set_hash": live_set_hash,
        "provenance_change_id": provenance_change_id,
        "rendered_card_body": rendered_card_body,
    }


def propose_memory_promotion_digest(
    candidates: list[dict], *, max_cards: int = 10
) -> list[str]:
    """DORMANT digest builder — no caller in this change (U4-J wires
    scheduling). Ranks by significance-then-effect (``mcnemar_p``
    ascending, then ``abs(net_flips)`` descending; ties keep the
    caller-supplied order = oldest first), caps at ``max_cards``, and
    opens ONE informational decision card per candidate. Deliberately
    NO approve option and NO approve-all: the dedicated human-only
    endpoint is the ONLY approval path."""
    from backend import decision_engine as de

    ranked = sorted(
        candidates,
        key=lambda card: (
            card["eval_summary"]["mcnemar_p"],
            -abs(card["eval_summary"]["net_flips"]),
        ),
    )
    decision_ids: list[str] = []
    for card in ranked[:max_cards]:
        dec = de.propose(
            kind="memory/promotion",
            title=f"Memory promotion candidate: {card['version_id']} "
                  f"({card['scope_key']})",
            detail=json.dumps(card),
            options=[
                {
                    "id": "open_review",
                    "label": "Open in review UI",
                    "description": "Informational only — approval happens "
                                   "exclusively via the human-only "
                                   "memory-promotions endpoint.",
                },
            ],
            default_option_id="open_review",
            severity="risky",
            timeout_s=86400.0,
            source={
                "builder": "memory_promotion_digest",
                "version_id": card["version_id"],
            },
        )
        decision_ids.append(dec.id)
    return decision_ids


__all__ = [
    "ApprovalValidationError",
    "assert_human_principal",
    "get_version_audience",
    "record_memory_approval",
    "build_approval_card",
    "propose_memory_promotion_digest",
]
