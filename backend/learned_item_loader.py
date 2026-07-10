"""OP-2572 U4-C2 — learned-item loader READ side (cached snapshot injection).

The ONLY path by which learned items ever reach a prompt (freeze
G0/G1/V3.1/V3.4-leg2/V3.5/V3.6). Anti-hollow by construction: the sync
prompt assembly reads a keyed in-process CACHE — never the DB; an
out-of-band, CALLER-DRIVEN ``refresh_scope`` fills the cache (no timers,
no threads, no task-spawning here — scheduling is operator/U4-J); every
read emits exactly one ``memory_delivery_total{result}`` sample so the
"legitimately empty" vs "broken" distinction is structural (V3.6).

Ships DORMANT: the kill-switch defaults OFF, so every read
short-circuits to ``kill_switch_off`` — loud and measured — and nothing
schedules the refresh.

v1 delivery is RETRIEVED-only (freeze G2): members are keyword-selected
against the caller's context via ``match_skills_for_context`` (token-bag
overlap of whole ≥3-char tokens is the accepted v1 semantic);
``always_injected`` items are never delivered here.

``conn`` is a parameter everywhere (pure seam) — this module never
imports backend.db_pool and never reads a clock. One-way metrics
dependency: imports backend.metrics, never the reverse. The reverse
prompt_loader import is lazy, so this module-level import is cycle-safe.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from backend import metrics
from backend.prompt_loader import (
    _MAX_PHASE2_CHARS,
    _MAX_PHASE2_SKILLS,
    match_skills_for_context,
)


# ━━ Kill-switch (same local truthy parse as the C1 boundary) ━━━━━━━━━

_KILL_SWITCH_ENV = "OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes"})


def _promotion_enabled() -> bool:
    return os.environ.get(_KILL_SWITCH_ENV, "").strip().lower() in _TRUTHY


# ━━ Module cache ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class _ScopeEntry:
    live_set_head: int
    membership: list[dict]
    bundle: dict[str, str]
    catalog: list[dict]


_cache: dict[str, _ScopeEntry] = {}


def _reset_for_tests() -> None:
    _cache.clear()


def _load_json(value: Any) -> Any:
    # Mirrors the C1 helper: sqlite returns jsonb columns as TEXT;
    # asyncpg returns str for jsonb unless a codec is set.
    return json.loads(value) if isinstance(value, str) else value


_PREAMBLE = "Learned items (retrieved, lower authority):"


# ━━ Out-of-band refresh (caller-driven — nothing here schedules it) ━━


async def refresh_scope(conn: Any, scope_key: str) -> int:
    """Read the LATEST snapshot row for *scope_key* plus its members'
    catalog metadata and store the scope's cache entry. head=0 / empty
    lists when no snapshot row exists — that IS a valid refreshed state
    (``empty_expected`` on read, freeze G1/V3.3).

    On ANY exception the previous entry (if one exists) is KEPT,
    ``memory_snapshot_stale_total`` is incremented, and the exception
    re-raises — the caller/timer decides retry; a failure never becomes
    a fake-fresh state (loud serve-stale)."""
    metrics.memory_enabled.set(1 if _promotion_enabled() else 0)
    try:
        row = await conn.fetchrow(
            "SELECT live_set_head, membership, rendered_bundle "
            "FROM learned_item_snapshots WHERE scope_key = $1 "
            "ORDER BY live_set_head DESC LIMIT 1",
            scope_key,
        )
        if row is None:
            head, membership, bundle = 0, [], {}
        else:
            head = int(row[0])
            membership = _load_json(row[1]) or []
            bundle = _load_json(row[2]) or {}
        catalog: list[dict] = []
        # Per-id loop on purpose: the sqlite test adapter rewrites
        # ``$N`` → ``?`` positionally, so an ``= ANY($1)`` array bind
        # would break it; membership is ≤ tens of rows.
        for member in membership:
            vrow = await conn.fetchrow(
                "SELECT name, description, trigger_condition, keywords "
                "FROM learned_item_versions WHERE id = $1",
                member["version_id"],
            )
            if vrow is None:
                raise LookupError(
                    f"catalog row missing for {member['version_id']}"
                )
            keywords = _load_json(vrow[3])
            catalog.append(
                {
                    "version_id": member["version_id"],
                    "name": vrow[0],
                    "description": vrow[1],
                    "trigger_condition": vrow[2],
                    "keywords": keywords if keywords is not None else [],
                }
            )
        _cache[scope_key] = _ScopeEntry(
            live_set_head=head,
            membership=membership,
            bundle=bundle,
            catalog=catalog,
        )
        return head
    except Exception:
        metrics.memory_snapshot_stale_total.inc()
        raise


# ━━ Sync cache-only read (the prompt-assembly seam) ━━━━━━━━━━━━━━━━━


def _finish(block: str, result: str) -> tuple[str, str]:
    metrics.memory_delivery_total.labels(result=result).inc()
    return block, result


def get_learned_items_block(
    *, tenant_id: str | None, context: str
) -> tuple[str, str]:
    """PURE-SYNC, cache-only read. Returns ``(block_text, result)`` with
    ``result ∈ {non_empty, empty_expected, empty_degraded,
    kill_switch_off}`` (the G7 frozen label shape). NO DB access — a
    missing cache entry while the switch is ON is the hollow signature
    (``empty_degraded``, loud)."""
    if not _promotion_enabled():
        return _finish("", "kill_switch_off")
    scope_keys = ["global:-"]
    if tenant_id is not None:
        scope_keys.append(f"tenant:{tenant_id}")
    else:
        # V3.1: tenant_id arrives explicitly; a None here means the
        # caller could not resolve scope — global-only read is the
        # lawful fallback, but the miss is measured.
        metrics.memory_failclosed_total.labels(
            reason="scope_unresolved"
        ).inc()
    entries: list[_ScopeEntry] = []
    for scope_key in scope_keys:
        entry = _cache.get(scope_key)
        if entry is None:
            return _finish("", "empty_degraded")
        entries.append(entry)
    # Pool the retrieved members' catalog across scopes (v1: G2
    # retrieved-only — always_injected never delivers here).
    pooled: list[dict] = []
    by_version: dict[str, tuple[_ScopeEntry, str]] = {}
    for entry in entries:
        for member in entry.membership:
            if member.get("delivery_mode") != "retrieved":
                continue
            vid = member["version_id"]
            by_version[vid] = (entry, member["rendered_payload_sha256"])
        for cat in entry.catalog:
            if cat["version_id"] in by_version:
                pooled.append(cat)
    selected = match_skills_for_context(
        domain_context=context,
        user_prompt="",
        skills=pooled,
        top_k=_MAX_PHASE2_SKILLS,
    )
    # The matcher enforces top_k only — the loader owns the char
    # budget: _MAX_PHASE2_CHARS as BOTH per-item and total cap. Bytes
    # are never truncated (verbatim injection + sha verification), so
    # over-budget items are skipped whole.
    chosen: list[tuple[str, str]] = []  # (version_id, bundle bytes)
    total = 0
    for cat in selected:
        vid = cat["version_id"]
        entry, _sha = by_version[vid]
        payload = entry.bundle.get(vid)
        if payload is None:
            # Member without bundle bytes = corrupted snapshot.
            return _finish("", "empty_degraded")
        if len(payload) > _MAX_PHASE2_CHARS:
            continue
        if total + len(payload) > _MAX_PHASE2_CHARS:
            continue
        chosen.append((vid, payload))
        total += len(payload)
    if not chosen:
        return _finish("", "empty_expected")
    # Retrieved-invariant (V3.4 leg 2): every selected item's bundle
    # bytes must hash to its membership sha — any mismatch degrades the
    # WHOLE block; corrupted snapshots never inject silently-partial
    # content.
    for vid, payload in chosen:
        _entry, sha = by_version[vid]
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != sha:
            return _finish("", "empty_degraded")
    # A2's stored bytes inject VERBATIM — never re-render, never
    # re-fence.
    block = "\n\n".join([_PREAMBLE] + [payload for _, payload in chosen])
    return _finish(block, "non_empty")
