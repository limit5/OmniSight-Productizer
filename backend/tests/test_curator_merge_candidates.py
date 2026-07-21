"""β-F leg-2 — ``curator_merge_candidates`` ledger (alembic 0275).

PG-gated (uses ``pg_test_conn``): the ledger is a serving/PG-only substrate.
Locks the insert contract the worker-loop curator depends on — the (ticket ↔
merged change) row, idempotency on replay (``ON CONFLICT (gerrit_change)``),
and the ``revert_state`` domain CHECK.
"""

from __future__ import annotations

import pytest

from backend import db


@pytest.mark.asyncio
async def test_insert_candidate_round_trips(pg_test_conn) -> None:
    ok = await db.insert_curator_merge_candidate(pg_test_conn, {
        "id": "cmc-rt-1",
        "ticket_key": "OP-2462",
        "gerrit_change": 4242,
        "change_id": "I1234567890abcdef",
        "canonical_subject": "[OP-2462] fix the thing",
        "revert_state": "none",
        "tenant_id": "omnisight-self",
        "patchset_count": 7,
    })
    assert ok is True

    rows = await pg_test_conn.fetch(
        "SELECT * FROM curator_merge_candidates WHERE gerrit_change = 4242"
    )
    assert len(rows) == 1
    assert rows[0]["ticket_key"] == "OP-2462"
    assert rows[0]["change_id"] == "I1234567890abcdef"
    assert rows[0]["revert_state"] == "none"
    assert rows[0]["distilled"] is False
    assert rows[0]["distilled_at"] is None
    assert rows[0]["patchset_count"] == 7  # β-1 struggle signal (NULL ok for legacy)


@pytest.mark.asyncio
async def test_insert_candidate_idempotent_on_replay(pg_test_conn) -> None:
    payload = {
        "id": "cmc-dup-a",
        "ticket_key": "OP-1",
        "gerrit_change": 9001,
        "change_id": "Ixyz",
        "canonical_subject": "s",
        "tenant_id": "omnisight-self",
    }
    first = await db.insert_curator_merge_candidate(pg_test_conn, payload)
    # A replayed change-merged (different row id, SAME change) must not
    # re-insert — else it would re-arm distilled=FALSE on an already-seen row.
    second = await db.insert_curator_merge_candidate(
        pg_test_conn, dict(payload, id="cmc-dup-b")
    )
    assert first is True
    assert second is False

    rows = await pg_test_conn.fetch(
        "SELECT id FROM curator_merge_candidates WHERE gerrit_change = 9001"
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "cmc-dup-a"


@pytest.mark.asyncio
async def test_revert_state_domain_check_rejects_bad_value(pg_test_conn) -> None:
    with pytest.raises(Exception):
        await db.insert_curator_merge_candidate(pg_test_conn, {
            "id": "cmc-bad",
            "gerrit_change": 7,
            "change_id": "I7",
            "revert_state": "bogus",  # not in ('none','reverted')
            "tenant_id": "omnisight-self",
        })


@pytest.mark.asyncio
async def test_reverted_candidate_is_flagged(pg_test_conn) -> None:
    ok = await db.insert_curator_merge_candidate(pg_test_conn, {
        "id": "cmc-rev",
        "ticket_key": None,
        "gerrit_change": 5150,
        "change_id": "Irev",
        "canonical_subject": 'Revert "[OP-9] bad change"',
        "revert_state": "reverted",
        "tenant_id": "omnisight-self",
    })
    assert ok is True
    row = await pg_test_conn.fetchrow(
        "SELECT revert_state, ticket_key FROM curator_merge_candidates "
        "WHERE gerrit_change = 5150"
    )
    assert row["revert_state"] == "reverted"
    assert row["ticket_key"] is None
