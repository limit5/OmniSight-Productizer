"""OP-2568 U4-D — human-only memory-promotion approval tests (offline).

Covers the ticket AC (a)-(g):

* (a) ``assert_human_principal`` matrix — api-key / anonymous /
  bot-shaped identities rejected, plain humans accepted.
* (b) ``record_memory_approval`` happy path on the stacked sqlite
  harness (0258+0259) + END-TO-END proof: a ``published`` publication
  event bound to the new approval INSERTs CLEAN through the U4-B gate
  trigger (the D→B integration seam).
* (c) every validation rejection is typed, writes NO row, and the
  pure-shape rejections provably touch no DB (sentinel conn).
* (d) audience gate at the handler-function level with fakes (freeze
  G6: global needs super_admin, tenant needs admin) + the human check
  runs unconditionally FIRST.
* (e) generic decisions approve endpoint 403s ``memory/*`` kinds and
  still resolves ``skill/promote`` (no regression).
* (f) digest builder ranking / cap / tie-break / single informational
  option, and ``build_approval_card`` unknown-key rejection.
* (g) dormant ship — no non-test backend module references the writer.

Router HTTP path is deliberately NOT booted (app lifespan needs a live
PG pool); the pure/handler/module layers are the runner-green surface.
No FK-enforcement assertions on sqlite (PRAGMA foreign_keys is OFF).
"""
from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend import decision_engine as de
from backend.auth import User
from backend.learned_item_approval import (
    ApprovalValidationError,
    assert_human_principal,
    build_approval_card,
    get_version_audience,
    propose_memory_promotion_digest,
    record_memory_approval,
)
from backend.routers import decisions as decisions_router
from backend.routers.memory_promotion import ApproveBody, handle_approve


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0258 = (
    BACKEND_ROOT / "alembic" / "versions" / "0258_u4_learned_item_ledger.py"
)
MIGRATION_0259 = (
    BACKEND_ROOT
    / "alembic"
    / "versions"
    / "0259_u4_publication_gate_trigger.py"
)

GOOD_HASH = "0" * 64


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0258():
    return _load_module(MIGRATION_0258, "_alembic_test_0258_for_approval")


@pytest.fixture(scope="module")
def m0259():
    return _load_module(MIGRATION_0259, "_alembic_test_0259_for_approval")


@pytest.fixture()
def conn(m0258, m0259):
    """Stacked 0258+0259 in-memory sqlite with an Operations proxy."""
    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    connection = engine.connect()
    ctx = MigrationContext.configure(connection=connection)
    with Operations.context(ctx):
        m0258.upgrade()
        m0259.upgrade()
        yield connection
    connection.close()


class _SqliteAdapterConn:
    """asyncpg-shaped facade over a sync sqlalchemy sqlite connection.

    Rewrites ``$N`` → ``?`` — valid because the writer's SQL uses each
    placeholder exactly once in ascending order."""

    def __init__(self, connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, *params):
        return self._connection.exec_driver_sql(
            re.sub(r"\$\d+", "?", sql), tuple(params)
        )

    async def fetchrow(self, sql: str, *params):
        return self._connection.exec_driver_sql(
            re.sub(r"\$\d+", "?", sql), tuple(params)
        ).fetchone()


class _ExplodingConn:
    """Sentinel: any DB touch is a test failure."""

    async def execute(self, sql: str, *params):
        raise AssertionError("DB touched before pure-shape validation")

    async def fetchrow(self, sql: str, *params):
        raise AssertionError("DB touched before pure-shape validation")


class _FakeConn:
    """Handler-level fake: serves the audience + existence reads and
    records the INSERT."""

    def __init__(self, audience: str = "tenant") -> None:
        self.audience = audience
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *params):
        if "audience" in sql:
            return (self.audience,)
        return (1,)

    async def execute(self, sql: str, *params):
        self.executed.append((sql, params))


def _human(role: str = "admin") -> User:
    return User(id="u-777", email="rt3628@gmail.com", name="sora", role=role)


def _mk_version(conn, version_id: str, audience: str = "tenant") -> None:
    conn.exec_driver_sql(
        "INSERT INTO learned_item_versions "
        "(id, canonical_content_hash, kind, audience, tenant_id, payload, "
        " created_by) "
        "VALUES (?, ?, 'lesson', ?, ?, '{}', 'test')",
        (
            version_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            audience,
            "t-default" if audience == "tenant" else None,
        ),
    )


def _mk_eval_run(
    conn, run_id: str, version_id: str, decision: str = "promote"
) -> None:
    conn.exec_driver_sql(
        "INSERT INTO memory_eval_runs (id, version_id, decision) "
        "VALUES (?, ?, ?)",
        (run_id, version_id, decision),
    )


def _approvals_count(conn) -> int:
    return conn.exec_driver_sql(
        "SELECT count(*) FROM memory_approvals"
    ).scalar()


# ━━ (a) assert_human_principal matrix ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAssertHumanPrincipal:
    @pytest.mark.parametrize(
        "user",
        [
            User(id="apikey:k1", email="apikey:deploy-key", name="deploy-key",
                 role="admin"),
            User(id="anonymous", email="anonymous@local", name="(anonymous)",
                 role="super_admin"),
            User(id="u-1", email="claude-bot-claude-2@ci.local",
                 name="claude-bot-claude-2", role="admin"),
            User(id="u-2", email="reviews@example.com", name="ai-reviewer",
                 role="admin"),
            User(id="u-3", email="ci-worker@example.com", name="worker",
                 role="admin"),
            User(id="u-4", email="merge@example.com", name="merger-agent-bot",
                 role="super_admin"),
        ],
        ids=["apikey", "anonymous", "claude-bot", "ai-reviewer",
             "ci-worker", "merger-agent-bot"],
    )
    def test_rejects_non_humans(self, user: User) -> None:
        with pytest.raises(HTTPException) as ei:
            assert_human_principal(user)
        assert ei.value.status_code == 403

    @pytest.mark.parametrize(
        "user",
        [
            User(id="u-777", email="rt3628@gmail.com", name="sora",
                 role="admin"),
            User(id="u-778", email="sora@example.com", name="Sora",
                 role="super_admin"),
        ],
        ids=["rt3628", "sora"],
    )
    def test_accepts_plain_humans(self, user: User) -> None:
        assert_human_principal(user)  # no raise


# ━━ (b) happy path + D→B end-to-end seam ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRecordMemoryApproval:
    async def test_happy_path_writes_exactly_one_row(self, conn) -> None:
        _mk_version(conn, "v-1")
        _mk_eval_run(conn, "r-1", "v-1")
        await record_memory_approval(
            _SqliteAdapterConn(conn),
            approval_id="a-1",
            version_id="v-1",
            eval_run_id="r-1",
            live_set_hash=GOOD_HASH,
            approved_by="rt3628@gmail.com",
            digest_id="digest-7",
        )
        assert _approvals_count(conn) == 1
        row = conn.exec_driver_sql(
            "SELECT version_id, eval_run_id, live_set_hash, approved_by, "
            "digest_id, approved_at FROM memory_approvals WHERE id='a-1'"
        ).fetchone()
        assert tuple(row[:5]) == (
            "v-1", "r-1", GOOD_HASH, "rt3628@gmail.com", "digest-7",
        )
        assert row[5] is not None  # DB default, never passed

    async def test_end_to_end_approval_satisfies_publication_gate(
        self, conn
    ) -> None:
        # The D→B integration seam: with the approval recorded, a
        # `published` event now INSERTs CLEAN through the gate trigger.
        _mk_version(conn, "v-2")
        _mk_eval_run(conn, "r-2", "v-2")
        await record_memory_approval(
            _SqliteAdapterConn(conn),
            approval_id="a-2",
            version_id="v-2",
            eval_run_id="r-2",
            live_set_hash=GOOD_HASH,
            approved_by="rt3628@gmail.com",
        )
        conn.exec_driver_sql(
            "INSERT INTO memory_publications "
            "(id, version_id, approval_id, state) "
            "VALUES (?, 'v-2', 'a-2', 'published')",
            (uuid.uuid4().hex,),
        )
        count = conn.exec_driver_sql(
            "SELECT count(*) FROM memory_publications "
            "WHERE approval_id='a-2' AND state='published'"
        ).scalar()
        assert count == 1


# ━━ (c) validation rejections ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestValidationRejections:
    async def test_version_not_found(self, conn) -> None:
        with pytest.raises(ApprovalValidationError) as ei:
            await record_memory_approval(
                _SqliteAdapterConn(conn),
                approval_id="a-x",
                version_id="v-missing",
                eval_run_id="r-x",
                live_set_hash=GOOD_HASH,
                approved_by="rt3628@gmail.com",
            )
        assert ei.value.reason == "version_not_found"
        assert _approvals_count(conn) == 0

    async def test_eval_run_wrong_decision(self, conn) -> None:
        _mk_version(conn, "v-1")
        _mk_eval_run(conn, "r-1", "v-1", decision="reject")
        with pytest.raises(ApprovalValidationError) as ei:
            await record_memory_approval(
                _SqliteAdapterConn(conn),
                approval_id="a-x",
                version_id="v-1",
                eval_run_id="r-1",
                live_set_hash=GOOD_HASH,
                approved_by="rt3628@gmail.com",
            )
        assert ei.value.reason == "eval_run_not_promote"
        assert _approvals_count(conn) == 0

    async def test_eval_run_wrong_version(self, conn) -> None:
        _mk_version(conn, "v-1")
        _mk_version(conn, "v-2")
        _mk_eval_run(conn, "r-other", "v-2")  # promote, but for v-2
        with pytest.raises(ApprovalValidationError) as ei:
            await record_memory_approval(
                _SqliteAdapterConn(conn),
                approval_id="a-x",
                version_id="v-1",
                eval_run_id="r-other",
                live_set_hash=GOOD_HASH,
                approved_by="rt3628@gmail.com",
            )
        assert ei.value.reason == "eval_run_not_promote"
        assert _approvals_count(conn) == 0

    @pytest.mark.parametrize(
        "bad_hash",
        ["", "0" * 63, "0" * 65, "Z" * 64, "0" * 64 + "\n", "A" * 64],
        ids=["empty", "short", "long", "nonhex", "trailing-nl", "uppercase"],
    )
    async def test_bad_live_set_hash_is_pre_db(self, bad_hash: str) -> None:
        # Sentinel conn: pure-shape checks run BEFORE any DB call.
        with pytest.raises(ApprovalValidationError) as ei:
            await record_memory_approval(
                _ExplodingConn(),
                approval_id="a-x",
                version_id="v-1",
                eval_run_id="r-1",
                live_set_hash=bad_hash,
                approved_by="rt3628@gmail.com",
            )
        assert ei.value.reason == "bad_live_set_hash"

    @pytest.mark.parametrize(
        "bad_by",
        ["", "merger-agent-bot", "ai-reviewer", "ci-worker",
         "svc/ci-runner", "release-bot@example.com"],
        ids=["empty", "-bot", "ai-", "ci-", "boundary-ci-", "-bot-email"],
    )
    async def test_bot_approved_by_is_pre_db(self, bad_by: str) -> None:
        with pytest.raises(ApprovalValidationError) as ei:
            await record_memory_approval(
                _ExplodingConn(),
                approval_id="a-x",
                version_id="v-1",
                eval_run_id="r-1",
                live_set_hash=GOOD_HASH,
                approved_by=bad_by,
            )
        assert ei.value.reason == "bot_approved_by"

    async def test_get_version_audience_missing_version(self, conn) -> None:
        with pytest.raises(ApprovalValidationError) as ei:
            await get_version_audience(_SqliteAdapterConn(conn), "v-missing")
        assert ei.value.reason == "version_not_found"

    async def test_get_version_audience_reads_row(self, conn) -> None:
        _mk_version(conn, "v-g", audience="global")
        assert await get_version_audience(
            _SqliteAdapterConn(conn), "v-g"
        ) == "global"


# ━━ (d) audience gate at the handler level ━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHandlerAudienceGate:
    def _body(self) -> ApproveBody:
        return ApproveBody(eval_run_id="r-1", live_set_hash=GOOD_HASH)

    async def test_global_with_admin_403(self) -> None:
        fake = _FakeConn(audience="global")
        with pytest.raises(HTTPException) as ei:
            await handle_approve(_human("admin"), fake, "v-1", self._body())
        assert ei.value.status_code == 403
        assert fake.executed == []  # nothing recorded

    async def test_global_with_super_admin_records(self) -> None:
        fake = _FakeConn(audience="global")
        out = await handle_approve(
            _human("super_admin"), fake, "v-1", self._body()
        )
        assert out["approval_id"]
        assert len(fake.executed) == 1
        sql, params = fake.executed[0]
        assert "INSERT" in sql
        # approved_by is the REAL authenticated identity, never a literal.
        assert "rt3628@gmail.com" in params

    async def test_tenant_with_admin_records(self) -> None:
        fake = _FakeConn(audience="tenant")
        out = await handle_approve(_human("admin"), fake, "v-1", self._body())
        assert out["approval_id"]
        assert len(fake.executed) == 1

    async def test_human_check_is_unconditional_and_first(self) -> None:
        # The anonymous synthetic principal carries role=super_admin in
        # open auth mode — it passes BOTH role gates; only the human
        # check stops it, BEFORE any DB access (exploding conn).
        anon = User(id="anonymous", email="anonymous@local",
                    name="(anonymous)", role="super_admin")
        with pytest.raises(HTTPException) as ei:
            await handle_approve(anon, _ExplodingConn(), "v-1", self._body())
        assert ei.value.status_code == 403

    def test_body_forbids_extra_fields(self) -> None:
        with pytest.raises(Exception):
            ApproveBody(eval_run_id="r-1", live_set_hash=GOOD_HASH,
                        audience="global")  # body-supplied audience


# ━━ (e) generic decisions endpoint kind-guard ━━━━━━━━━━━━━━━━━━━━━━━━


class TestGenericApproveKindGuard:
    def setup_method(self):
        de._reset_for_tests()
        de.set_mode("manual")

    def _operator(self) -> User:
        return User(id="u-op", email="operator@example.com", name="op",
                    role="operator")

    async def test_memory_kind_403_and_stays_pending(self) -> None:
        d = de.propose(
            "memory/promotion", "t", severity="risky",
            options=[{"id": "open_review", "label": "Open"}],
        )
        with pytest.raises(HTTPException) as ei:
            await decisions_router.approve_decision(
                d.id,
                decisions_router.ResolveRequest(option_id="open_review"),
                None,
                self._operator(),
            )
        assert ei.value.status_code == 403
        assert de.get(d.id).status == de.DecisionStatus.pending

    async def test_non_memory_kind_still_resolves(self) -> None:
        d = de.propose(
            "skill/promote", "t", severity="routine",
            options=[{"id": "promote", "label": "P"},
                     {"id": "discard", "label": "D"}],
        )
        out = await decisions_router.approve_decision(
            d.id,
            decisions_router.ResolveRequest(option_id="promote"),
            None,
            self._operator(),
        )
        assert out["status"] == "approved"
        assert out["chosen_option_id"] == "promote"


# ━━ (f) digest builder + card builder ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _card(version_id: str, p: float, flips: int) -> dict:
    return build_approval_card(
        version_id=version_id,
        kind="lesson",
        audience="tenant",
        scope_key="tenant:t-1",
        eval_summary={"decision": "promote", "mcnemar_p": p,
                      "net_flips": flips, "n": 40},
        live_set_hash=GOOD_HASH,
        provenance_change_id="I" + "a" * 40,
        rendered_card_body="rendered body bytes",
    )


class TestDigestBuilder:
    def setup_method(self):
        de._reset_for_tests()
        de.set_mode("manual")

    def _versions_in_order(self, ids: list[str]) -> list[str]:
        return [de.get(i).source["version_id"] for i in ids]

    def test_ranks_significance_then_effect(self) -> None:
        ids = propose_memory_promotion_digest([
            _card("v-a", 0.04, 3),
            _card("v-b", 0.01, 1),
            _card("v-c", 0.04, -5),
        ])
        assert self._versions_in_order(ids) == ["v-b", "v-c", "v-a"]

    def test_tie_break_is_oldest_first(self) -> None:
        ids = propose_memory_promotion_digest([
            _card("v-old", 0.02, 4),
            _card("v-new", 0.02, -4),  # identical (p, |net_flips|)
        ])
        assert self._versions_in_order(ids) == ["v-old", "v-new"]

    def test_max_cards_cap(self) -> None:
        cards = [_card(f"v-{i}", 0.01 * (i + 1), 1) for i in range(5)]
        ids = propose_memory_promotion_digest(cards, max_cards=2)
        assert len(ids) == 2
        assert self._versions_in_order(ids) == ["v-0", "v-1"]

    def test_single_informational_option_no_approve_anywhere(self) -> None:
        ids = propose_memory_promotion_digest([_card("v-a", 0.01, 2)])
        assert len(ids) == 1
        d = de.get(ids[0])
        assert d.kind == "memory/promotion"
        assert [o["id"] for o in d.options] == ["open_review"]
        forbidden = {"approve", "approve_all"}
        for pending in de.list_pending():
            assert not forbidden & {o["id"] for o in pending.options}

    def test_missing_ranking_keys_fail_loudly(self) -> None:
        card = _card("v-a", 0.01, 2)
        del card["eval_summary"]["mcnemar_p"]
        with pytest.raises(KeyError):
            propose_memory_promotion_digest([card])

    def test_build_approval_card_rejects_unknown_eval_key(self) -> None:
        with pytest.raises(ValueError, match="unknown eval_summary keys"):
            build_approval_card(
                version_id="v-a",
                kind="lesson",
                audience="tenant",
                scope_key="tenant:t-1",
                eval_summary={"decision": "promote", "p_value": 0.01},
                live_set_hash=GOOD_HASH,
                provenance_change_id="I" + "a" * 40,
                rendered_card_body="body",
            )


# ━━ (g) dormant-ship guard ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDormantShip:
    def test_no_nontest_module_references_the_writer(self) -> None:
        # Own-exclusion uses the LEDGER-test membership form (str(rel)
        # in own) — the router is a 2-part path, which the 1-part
        # exclusion shape used by the U4-B sweep cannot express.
        # main.py's mount line says `memory_promotion` (the router
        # module), not the writer module name, so main.py is not an
        # offender.
        own = {"learned_item_approval.py", "routers/memory_promotion.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if str(rel) in own:
                continue
            text = py.read_text(errors="ignore")
            if "learned_item_approval" in text:
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"
