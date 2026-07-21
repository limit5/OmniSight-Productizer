"""β-0 (leg-2) — worker-loop curator: gate, inert-when-disabled, deterministic
record, evidence rebuild, and a PG-gated end-to-end distill.

The record-shape tests LOCK D3 (design v1's fatal bug): the minimal record must
PASS ``validate_and_render`` — a ``procedure_steps=[]`` record fails
``empty_content`` and would make the whole curator hollow-by-construction.
"""

from __future__ import annotations

import pytest

from backend import db as _db
from backend.agents import worker_loop_curator as wc
from backend.learned_item_provenance import derive_ground_truths
from backend.learned_item_renderer import validate_and_render

_NOW = "2026-07-20T00:00:00+00:00"


# ── gate + inert-when-disabled (no PG) ──────────────────────────────────────

def test_curator_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_WORKER_CURATOR", raising=False)
    assert wc.curator_enabled() is False
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("OMNISIGHT_WORKER_CURATOR", v)
        assert wc.curator_enabled() is True
    monkeypatch.setenv("OMNISIGHT_WORKER_CURATOR", "0")
    assert wc.curator_enabled() is False


@pytest.mark.asyncio
async def test_loop_inert_when_disabled_never_touches_pool(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_WORKER_CURATOR", raising=False)

    def _boom_pool():
        raise AssertionError("get_pool must NOT be called when the curator is disabled")

    ticks = await wc.run_worker_curator_loop(get_pool=_boom_pool)
    assert ticks == 0


# ── deterministic minimal record — LOCKS D3 (must pass validate_and_render) ──

def test_minimal_record_with_ticket_passes_validation():
    payload = wc._build_minimal_record({"ticket_key": "OP-2462", "gerrit_change": 4242})
    record, rendered = validate_and_render(payload)  # raises on reject
    assert rendered.rendered_payload
    assert payload["scope"]
    assert len(payload["procedure_steps"]) == 1
    assert "4242" in payload["procedure_steps"][0]
    assert payload["evidence_references"] == ["OP-2462"]


def test_minimal_record_without_ticket_still_valid():
    payload = wc._build_minimal_record({"ticket_key": None, "gerrit_change": 99})
    validate_and_render(payload)  # raises on reject
    assert "99" in payload["scope"]
    assert "evidence_references" not in payload  # no OP-key -> no card ref


# ── evidence rebuilt from the ledger's VERIFIED snapshot (no Gerrit round-trip) ──

def test_evidence_derives_merged_and_review_plus2():
    item = wc._evidence_for({"gerrit_change": 7, "plus2_reviewer": "sora"})[0]
    assert item["change_ref"] == "7"
    kinds = {t.kind for t in derive_ground_truths(
        change_ref=item["change_ref"], gerrit_change=item["gerrit_change"],
        jira_labels=None, now=_NOW,
    )}
    assert {"merged", "review_plus2"} <= kinds


def test_evidence_bot_or_empty_reviewer_drops_review_plus2():
    # An empty/bot-shaped reviewer must NOT confirm review_plus2 — the evidence
    # degrades to merged-only, never a forged human +2.
    for reviewer in ("", "merger-agent-bot"):
        item = wc._evidence_for({"gerrit_change": 8, "plus2_reviewer": reviewer})[0]
        kinds = {t.kind for t in derive_ground_truths(
            change_ref=item["change_ref"], gerrit_change=item["gerrit_change"],
            jira_labels=None, now=_NOW,
        )}
        assert "merged" in kinds
        assert "review_plus2" not in kinds


# ── PG-gated end-to-end: ledger candidate -> quarantined version + evidence ──

class _PoolWrap:
    """Adapt a single pg_test_conn into the pool.acquire() shape the curator uses."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _CM()


@pytest.mark.asyncio
async def test_curator_once_distills_candidate_end_to_end(pg_test_conn):
    await pg_test_conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ('omnisight-self', 'x', 'free') "
        "ON CONFLICT DO NOTHING"
    )
    await _db.insert_curator_merge_candidate(pg_test_conn, {
        "id": "cmc-e2e", "ticket_key": "OP-4242", "gerrit_change": 4242,
        "change_id": "Iabc", "canonical_subject": "[OP-4242] fix",
        "plus2_reviewer": "sora", "tenant_id": "omnisight-self",
    })

    result = await wc.run_worker_curator_once(_PoolWrap(pg_test_conn))
    assert result["leader"] is True
    assert result["submitted"] == 1
    assert result["error"] == 0

    # candidate marked distilled (won't be re-scanned)
    row = await pg_test_conn.fetchrow(
        "SELECT distilled FROM curator_merge_candidates WHERE gerrit_change = 4242"
    )
    assert row["distilled"] is True

    # a QUARANTINED learned-item version authored by the curator
    v = await pg_test_conn.fetchrow(
        "SELECT kind, audience, tenant_id, created_by FROM learned_item_versions "
        "WHERE created_by = 'ground_truth_curator'"
    )
    assert v["kind"] == "playbook"
    assert v["audience"] == "tenant"
    assert v["tenant_id"] == "omnisight-self"

    # evidence: BOTH ground truths derived from the rebuilt snapshot
    kinds = {r["ground_truth_kind"] for r in await pg_test_conn.fetch(
        "SELECT ground_truth_kind FROM learned_item_evidence"
    )}
    assert {"merged", "review_plus2"} <= kinds


@pytest.mark.asyncio
async def test_curator_second_pass_is_idempotent(pg_test_conn):
    await pg_test_conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ('omnisight-self', 'x', 'free') "
        "ON CONFLICT DO NOTHING"
    )
    await _db.insert_curator_merge_candidate(pg_test_conn, {
        "id": "cmc-idem", "ticket_key": "OP-777", "gerrit_change": 777,
        "change_id": "I777", "canonical_subject": "[OP-777] x",
        "plus2_reviewer": "sora", "tenant_id": "omnisight-self",
    })
    first = await wc.run_worker_curator_once(_PoolWrap(pg_test_conn))
    # distilled=TRUE now, so the second tick scans zero undistilled rows
    second = await wc.run_worker_curator_once(_PoolWrap(pg_test_conn))
    assert first["submitted"] == 1
    assert second["scanned"] == 0
    assert second["submitted"] == 0
