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


# ── β-1 lane ordering inside the curator (no network, no PG) ────────────────

class _CountConn:
    """Fake conn for count_incidents + the reverted_later EXISTS join."""

    def __init__(self, count: int = 0, reverted: bool = False) -> None:
        self._count = count
        self._reverted = reverted

    async def fetchval(self, sql, *_a, **_kw):
        if "EXISTS" in sql:
            return self._reverted
        return self._count


def _no_fetch(monkeypatch):
    from backend.agents import worker_loop_distiller as wd

    async def _boom(_c):
        raise AssertionError("fetch_artifacts must NOT be called")

    monkeypatch.setattr(wd, "fetch_artifacts", _boom)


@pytest.mark.asyncio
async def test_llm_lane_cheap_arms_skip_before_any_fetch(monkeypatch):
    """A trivial candidate is rejected on ledger arms alone — zero network."""
    _no_fetch(monkeypatch)
    payload, label, labels = await wc._try_llm_draft(
        _CountConn(0),
        {"ticket_key": "OP-1", "gerrit_change": 1, "canonical_subject": "s",
         "revert_state": "none", "patchset_count": 1},
        {"enabled": True, "left": 3, "deadline": float("inf")},
    )
    assert (payload, label, labels) == (None, "gate_skip_trivial", None)


@pytest.mark.asyncio
async def test_llm_lane_deadline_defers_gate_passer(monkeypatch):
    """Past-deadline tick spends nothing AND DEFERS the gate-passer (audit
    MAJOR-4 + MAJOR-5: never minimal-downgrade a hard ticket permanently)."""
    _no_fetch(monkeypatch)
    payload, label, _ = await wc._try_llm_draft(
        _CountConn(9),
        {"ticket_key": "OP-1", "gerrit_change": 1, "canonical_subject": "s",
         "revert_state": "none", "patchset_count": 9},
        {"enabled": True, "left": 3, "deadline": 0.0},
    )
    assert (payload, label) == (None, "llm_deferred")


@pytest.mark.asyncio
async def test_llm_lane_reverted_later_excluded(monkeypatch):
    """Audit MAJOR-1 / leak L5: a SEPARATE revert change for the same ticket
    disqualifies the original fix — caught by the ledger join, no network."""
    _no_fetch(monkeypatch)
    payload, label, _ = await wc._try_llm_draft(
        _CountConn(9, reverted=True),
        {"ticket_key": "OP-1", "gerrit_change": 1, "canonical_subject": "s",
         "revert_state": "none", "patchset_count": 9},
        {"enabled": True, "left": 3, "deadline": float("inf")},
    )
    assert (payload, label) == (None, "gate_skip_reverted_later")


@pytest.mark.asyncio
async def test_deferred_candidate_not_submitted_not_marked(monkeypatch):
    """A deferred gate-passer must NOT submit and NOT mark distilled."""
    async def _defer(_conn, _cand, _state):
        return None, "llm_deferred", None

    monkeypatch.setattr(wc, "_try_llm_draft", _defer)

    class _NoWriteConn:
        async def execute(self, *_a, **_kw):
            raise AssertionError("deferred candidate must not write")

    submit_label, llm_label = await wc._distill_candidate(
        _NoWriteConn(), {"id": "c", "ticket_key": "OP-1", "gerrit_change": 1},
        now="2026-07-21T00:00:00+00:00", llm_state={"enabled": True},
    )
    assert (submit_label, llm_label) == ("deferred", "llm_deferred")


@pytest.mark.asyncio
async def test_llm_lane_labels_unknown_fails_closed(monkeypatch):
    """Ticket exists but labels unavailable ⇒ stoploss arm can't run ⇒ no LLM
    (audit MAJOR-3); the minimal record still ships."""
    from backend.agents import worker_loop_distiller as wd

    async def _artifacts_no_labels(_c):
        return {"summary": "", "description": "", "jira_labels": None,
                "commit_message": "m", "files": [], "files_truncated": 0}

    monkeypatch.setattr(wd, "fetch_artifacts", _artifacts_no_labels)
    payload, label, labels = await wc._try_llm_draft(
        _CountConn(9),
        {"ticket_key": "OP-1", "gerrit_change": 1, "canonical_subject": "s",
         "revert_state": "none", "patchset_count": 9},
        {"enabled": True, "left": 3, "deadline": float("inf")},
    )
    assert (payload, label, labels) == (None, "gate_skip_labels_unknown", None)


@pytest.mark.asyncio
async def test_llm_lane_stoploss_labels_thread_to_evidence_not_gate(monkeypatch):
    """Audit MAJOR-3: stoploss labels on a MERGED candidate do NOT gate (they
    mean struggled-then-succeeded); the snapshot threads through for the
    evidence (BLOCKER-1: single snapshot), and the draft proceeds."""
    from backend.agents import worker_loop_distiller as wd

    async def _artifacts_stoploss(_c):
        return {"summary": "", "description": "",
                "jira_labels": ["runner-stoploss:revert-x"],
                "commit_message": "m", "files": [], "files_truncated": 0}

    async def _draft_ok(_cand, _arts, llm=None):
        return {"scope": "s", "procedure_steps": ["do x"]}, "distilled_llm"

    monkeypatch.setattr(wd, "fetch_artifacts", _artifacts_stoploss)
    monkeypatch.setattr(wd, "draft_record", _draft_ok)
    payload, label, labels = await wc._try_llm_draft(
        _CountConn(9),
        {"ticket_key": "OP-1", "gerrit_change": 1, "canonical_subject": "s",
         "revert_state": "none", "patchset_count": 9},
        {"enabled": True, "left": 3, "deadline": float("inf")},
    )
    assert label == "distilled_llm"
    assert payload is not None
    assert labels == ["runner-stoploss:revert-x"]  # snapshot for the evidence row


@pytest.mark.asyncio
async def test_llm_lane_success_path(monkeypatch):
    from backend.agents import worker_loop_distiller as wd

    async def _artifacts_ok(_c):
        return {"summary": "s", "description": "d", "jira_labels": [],
                "commit_message": "m", "files": ["a.c"], "files_truncated": 0}

    async def _draft_ok(_cand, _arts, llm=None):
        return {"scope": "s", "procedure_steps": ["do x"]}, "distilled_llm"

    monkeypatch.setattr(wd, "fetch_artifacts", _artifacts_ok)
    monkeypatch.setattr(wd, "draft_record", _draft_ok)
    state = {"enabled": True, "left": 3, "deadline": float("inf")}
    payload, label, labels = await wc._try_llm_draft(
        _CountConn(9),
        {"ticket_key": "OP-1", "gerrit_change": 1, "canonical_subject": "s",
         "revert_state": "none", "patchset_count": 9},
        state,
    )
    assert label == "distilled_llm"
    assert payload == {"scope": "s", "procedure_steps": ["do x"]}
    assert labels == []
    assert state["left"] == 2  # cap decremented exactly once


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
async def test_curator_llm_lane_end_to_end(pg_test_conn, monkeypatch):
    """β-1 e2e: gate-passing candidate + fake cheap LLM → a QUARANTINED
    version with created_by=ground_truth_curator_llm (provenance tier) and
    the label snapshot threaded into a stoploss evidence row."""
    import json as _json
    from types import SimpleNamespace

    from backend.agents import worker_loop_distiller as wd

    await pg_test_conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ('omnisight-self', 'x', 'free') "
        "ON CONFLICT DO NOTHING"
    )
    await _db.insert_curator_merge_candidate(pg_test_conn, {
        "id": "cmc-llm", "ticket_key": "OP-9100", "gerrit_change": 9100,
        "change_id": "I9100", "canonical_subject": "[OP-9100] hard fix",
        "plus2_reviewer": "sora", "tenant_id": "omnisight-self",
        "patchset_count": 5,
    })

    monkeypatch.setenv("OMNISIGHT_WORKER_CURATOR_LLM", "1")

    async def _artifacts(_c):
        return {"summary": "s", "description": "d",
                "jira_labels": ["runner-stoploss:revert-x"],
                "commit_message": "the fix", "files": ["a.c"],
                "files_truncated": 0}

    class _CheapLLM:
        model_name = "claude-haiku-4-20250506"

        async def ainvoke(self, _msgs):
            return SimpleNamespace(content=_json.dumps({
                "scope": "UVC bind path tickets",
                "procedure_steps": ["Check the buffer size first"],
                "verification": "stream without ENOMEM",
            }))

    monkeypatch.setattr(wd, "fetch_artifacts", _artifacts)
    import backend.agents.llm as _llm_mod
    monkeypatch.setattr(_llm_mod, "get_cheapest_model", lambda **_kw: _CheapLLM())

    result = await wc.run_worker_curator_once(_PoolWrap(pg_test_conn))
    assert result["submitted"] == 1
    assert result["llm"].get("distilled_llm") == 1

    v = await pg_test_conn.fetchrow(
        "SELECT created_by, payload FROM learned_item_versions "
        "WHERE created_by = 'ground_truth_curator_llm'"
    )
    assert v is not None
    payload = _json.loads(v["payload"])
    assert payload["scope"] == "UVC bind path tickets"
    assert payload["evidence_references"] == ["OP-9100"]  # server-set

    kinds = {r["ground_truth_kind"] for r in await pg_test_conn.fetch(
        "SELECT ground_truth_kind FROM learned_item_evidence"
    )}
    # merged + review_plus2 from the ledger snapshot, PLUS the stoploss row
    # from the threaded label snapshot (BLOCKER-1 single-snapshot contract).
    assert {"merged", "review_plus2", "stoploss"} <= kinds


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


@pytest.mark.asyncio
@pytest.mark.parametrize("created,want_submit", [(True, "submitted"), (False, "dup")])
async def test_distill_candidate_success_path_returns_two_tuple(
    monkeypatch, created, want_submit,
):
    """Arity regression (staging 2026-07-23): the SUCCESS return was a bare
    string, so the caller's ``submit_label, llm_label = …`` unpacked its
    CHARACTERS ("dup" → 3 values) AFTER submit+mark committed — data landed,
    counters all fell into ``error``. Lock the (submit_label, llm_label)
    contract on the path no non-PG test previously walked."""

    class _Result:
        pass

    _Result.created = created

    async def _fake_submit(_conn, **_kw):
        return _Result()

    class _MarkConn:
        def __init__(self):
            self.updates = 0

        async def execute(self, sql, *_a):
            assert "SET distilled = TRUE" in sql
            self.updates += 1

    monkeypatch.setattr(wc, "submit_quarantined_version", _fake_submit)
    conn = _MarkConn()
    out = await wc._distill_candidate(
        conn, {"id": "c1", "ticket_key": "OP-1", "gerrit_change": 1,
               "canonical_subject": "s", "revert_state": "none"},
        now="2026-07-23T00:00:00+00:00", llm_state=None,
    )
    assert out == (want_submit, None)
    assert conn.updates == 1


@pytest.mark.asyncio
async def test_distill_candidate_llm_success_returns_two_tuple(monkeypatch):
    """Same arity lock for the LLM-draft success path: label rides slot 2."""

    class _Result:
        created = True

    async def _fake_submit(_conn, **kw):
        assert kw["created_by"] == "ground_truth_curator_llm"
        return _Result()

    async def _draft(_conn, _cand, _state):
        return {"scope": "s", "procedure_steps": ["x"]}, "distilled_llm", None

    class _MarkConn:
        async def execute(self, sql, *_a):
            assert "SET distilled = TRUE" in sql

    monkeypatch.setattr(wc, "submit_quarantined_version", _fake_submit)
    monkeypatch.setattr(wc, "_try_llm_draft", _draft)
    out = await wc._distill_candidate(
        _MarkConn(), {"id": "c2", "ticket_key": "OP-2", "gerrit_change": 2,
                      "canonical_subject": "s", "revert_state": "none"},
        now="2026-07-23T00:00:00+00:00", llm_state={"enabled": True},
    )
    assert out == ("submitted", "distilled_llm")
