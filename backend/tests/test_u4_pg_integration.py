"""U4 substrate — REAL-PostgreSQL integration test (the full governed chain).

The per-module U4 tests all run on the stacked in-memory SQLite harness,
which structurally cannot catch asyncpg-vs-SQLite divergences (a bare `%`
in a plpgsql body, an ISO string into a ``timestamptz`` param, a
``uuid.UUID`` fetched from a uuid column compared to a ``str``). Every one
of those shipped green offline and only failed on the first real staging
apply (the U4-J pilot).

This test closes that gap: it drives the ENTIRE chain — producer submit
(+ evidence) → F-exec plan-triage eval (scripted arms) → D human approval
→ C1 atomic publish → C2 loader delivery → C1 revoke → gone — against a
REAL Postgres via ``pg_test_pool``. It is skipped when ``OMNI_TEST_PG_URL``
is unset (normal CI), and MUST be run against a scratch PG before any U4
release (``OMNI_TEST_PG_URL=postgresql://.../<name>_staging pytest -k
u4_pg_integration``).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "configs" / "iq_benchmark"
MANIFEST = SUITE / "manifest.yml"
NOW = "2026-07-11T12:00:00Z"


def _qmap() -> dict[str, str]:
    m: dict[str, str] = {}
    for shard in ("firmware-debug.yaml", "holdout-finetune.yaml"):
        data = yaml.safe_load((SUITE / shard).read_text())
        for q in data.get("questions") or []:
            m[q["prompt"].strip()[:60]] = " ".join(q.get("expected_keywords") or [])
    return m


_QMAP = _qmap()


async def _good_ask(model: str, prompt: str) -> tuple[str, int]:
    """Baseline arm empty (fails), candidate arm answers correctly — a
    clean improvement so the scripted eval promotes."""
    if "BEGIN UNTRUSTED LEARNED-ITEM DATA" not in prompt:
        return "", 20
    for qp, kw in _QMAP.items():
        if qp in prompt:
            return kw, 20
    return "", 20


_GOOD = {
    "scope": "cross-compiling worker backends gated on vendor sysroots",
    "procedure_steps": ["cross-compile against the staging sysroot before approving"],
    "known_failures": ["host stub linked silently"],
    "prohibited_actions": ["approving from host-only green"],
    "evidence_references": ["OP-2472"],
}
_GOOD_EVIDENCE = (
    {
        "change_ref": "1844",
        "gerrit_change": {
            "status": "MERGED",
            "currentPatchSet": {
                "approvals": [
                    {"type": "Code-Review", "value": "2", "by": {"username": "sora"}}
                ]
            },
        },
        "jira_labels": [],
    },
)


async def test_u4_full_chain_on_real_pg(pg_test_pool, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED", "1")

    from backend.learned_item_producer import submit_quarantined_version
    from backend.learned_item_approval import record_memory_approval
    from backend.learned_item_publisher import (
        publish_learned_item_version,
        revoke_learned_item_version,
    )
    from backend.learned_item_publication import (
        compute_live_set_hash,
        publication_scope_key,
    )
    from backend.learned_item_loader import (
        refresh_scope,
        get_learned_items_block,
        _reset_for_tests as loader_reset,
    )
    from backend.memory_promotion_eval import EvalClient, run_plan_triage_eval

    async with pg_test_pool.acquire() as conn:
        # producer submit + server-derived evidence (verified_at timestamptz)
        sub = await submit_quarantined_version(
            conn, payload=_GOOD, kind="skill", audience="global", tenant_id=None,
            created_by="pilot", name="sysroot", keywords=["sysroot", "backend"],
            evidence=_GOOD_EVIDENCE, now=NOW,
        )
        vid = sub.version_id
        assert sub.created is True
        ev_kinds = {
            r[0]
            for r in await conn.fetch(
                "SELECT ground_truth_kind FROM learned_item_evidence WHERE version_id=$1",
                vid,
            )
        }
        assert {"merged", "review_plus2"} <= ev_kinds

        # idempotent resubmit — asyncpg returns a UUID; must equal the str id
        sub2 = await submit_quarantined_version(
            conn, payload=_GOOD, kind="skill", audience="global", tenant_id=None,
            created_by="pilot", name="sysroot", keywords=["sysroot"], evidence=(), now=NOW,
        )
        assert sub2.created is False
        assert str(sub2.version_id) == str(vid)

        # F-exec eval (scripted arms) — ran_at timestamptz + eval-case rows
        row = await conn.fetchrow(
            "SELECT rendered_payload, rendered_payload_sha256 "
            "FROM learned_item_versions WHERE id=$1",
            vid,
        )
        live_hash = compute_live_set_hash([])
        outcome = await run_plan_triage_eval(
            conn, version_id=vid, rendered_payload=row[0],
            rendered_payload_sha256=row[1], ask_fn=_good_ask,
            client=EvalClient(provider="scripted", model="pilot", temperature=0.0),
            manifest_path=MANIFEST, base_dir=SUITE, live_set_hash=live_hash,
            now=NOW, samples_per_case=1,
        )
        assert outcome.decision == "promote"
        assert await conn.fetchval(
            "SELECT count(*) FROM memory_eval_cases WHERE eval_run_id=$1",
            outcome.eval_run_id,
        ) > 0

        # D human approval → C1 atomic publish (UUID approval-version compare)
        aid = str(uuid.uuid4())
        await record_memory_approval(
            conn, approval_id=aid, version_id=vid, eval_run_id=outcome.eval_run_id,
            live_set_hash=live_hash, approved_by="sora@human",
        )
        async with conn.transaction():
            pub = await publish_learned_item_version(
                conn, version_id=vid, approval_id=aid, actor="pilot"
            )
        assert pub.published is True and pub.live_set_head == 1

        # C2 loader delivery (materialized snapshot → cached read)
        loader_reset()
        scope = publication_scope_key("global", None)
        await refresh_scope(conn, scope)
        block, result = get_learned_items_block(
            tenant_id=None, context="cross-compile sysroot backend build"
        )
        assert result == "non_empty" and "sysroot" in block.lower()

        # C1 revoke → the bytes become unretrievable from the new head
        async with conn.transaction():
            rev = await revoke_learned_item_version(
                conn, version_id=vid, revoked_by="sora@human", reason="pilot"
            )
        assert rev.revoked is True and rev.live_set_head == 2
        loader_reset()
        await refresh_scope(conn, scope)
        block2, _ = get_learned_items_block(
            tenant_id=None, context="cross-compile sysroot backend"
        )
        assert "sysroot" not in block2.lower()
