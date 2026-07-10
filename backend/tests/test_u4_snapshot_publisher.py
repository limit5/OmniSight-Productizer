"""OP-2570 U4-C1 — atomic snapshot publisher + revocation tests (offline).

Covers the ticket AC (a)-(l) on the stacked sqlite harness (0258+0259).
The ``$N`` → ``?`` adapter is extended with ``fetchrow`` / ``fetch`` and
a no-op special case for the EXACT advisory-lock SQL (sqlite has no
advisory locks — recognizing that one statement keeps the module
honest). The kill-switch env is monkeypatched ON per test; the OFF path
is tested separately. Fixture rows (version + promote-eval + approval)
are seeded via raw SQL, the D test file's idiom.

Rollback semantics on this harness (empirically proven upstream):
migration DDL commits outside the driver txn; ALL DML — fixtures
included — lives in ONE implicit txn, so ``connection.rollback()``
wipes fixture rows too. The rollback-proof test therefore asserts
GLOBAL emptiness, and the healthy-probe leg runs in a separate test
with a fresh conn.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path

import pytest

import backend.learned_item_publisher as publisher_mod
from backend.learned_item_publication import (
    check_publication_invariant,
    compute_live_set_hash,
    derive_membership_entry,
)
from backend.learned_item_publisher import (
    PublicationDenied,
    PublishValidationError,
    publish_learned_item_version,
    reconcile_publication_invariant,
    revoke_learned_item_version,
)


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

KILL_SWITCH_ENV = "OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED"
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext($1))"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0258():
    return _load_module(MIGRATION_0258, "_alembic_test_0258_for_snapshot")


@pytest.fixture(scope="module")
def m0259():
    return _load_module(MIGRATION_0259, "_alembic_test_0259_for_snapshot")


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


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv(KILL_SWITCH_ENV, "1")


class _SqliteAdapterConn:
    """asyncpg-shaped facade over a sync sqlalchemy sqlite connection.

    Rewrites ``$N`` → ``?`` (each ``$N`` used once in ascending order in
    the module's SQL). The EXACT advisory-lock statement is a no-op —
    sqlite has no advisory locks (single-writer test DB, as documented
    in the B increment)."""

    def __init__(self, connection) -> None:
        self._connection = connection

    def _run(self, sql: str, params: tuple):
        return self._connection.exec_driver_sql(
            re.sub(r"\$\d+", "?", sql), params
        )

    async def execute(self, sql: str, *params):
        if sql == _LOCK_SQL:
            return None
        return self._run(sql, tuple(params))

    async def fetchrow(self, sql: str, *params):
        return self._run(sql, tuple(params)).fetchone()

    async def fetch(self, sql: str, *params):
        return self._run(sql, tuple(params)).fetchall()


# ━━ Fixture seeds (raw SQL — the D test file's idiom) ━━━━━━━━━━━━━━━━


def _rendered(vid: str) -> str:
    return f"RENDERED-BYTES::{vid}"


def _rendered_sha(vid: str) -> str:
    return hashlib.sha256(_rendered(vid).encode()).hexdigest()


def _insert_version(
    conn,
    vid: str,
    *,
    audience: str = "global",
    tenant_id: str | None = None,
) -> None:
    conn.exec_driver_sql(
        "INSERT INTO learned_item_versions "
        "(id, canonical_content_hash, kind, audience, tenant_id, payload, "
        " rendered_payload, renderer_version, rendered_payload_sha256, "
        " delivery_mode, created_by) "
        "VALUES (?, ?, 'lesson', ?, ?, ?, ?, 'r1', ?, 'retrieved', 'test')",
        (
            vid,
            hashlib.sha256(vid.encode()).hexdigest(),
            audience,
            tenant_id,
            json.dumps({"body": vid}),
            _rendered(vid),
            _rendered_sha(vid),
        ),
    )


def _approve(conn, vid: str, live_set_hash: str) -> str:
    rid = uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO memory_eval_runs (id, version_id, decision) "
        "VALUES (?, ?, 'promote')",
        (rid, vid),
    )
    aid = uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO memory_approvals "
        "(id, version_id, eval_run_id, live_set_hash, approved_by) "
        "VALUES (?, ?, ?, ?, 'human-reviewer')",
        (aid, vid, rid, live_set_hash),
    )
    return aid


def _seed_event(
    conn, event_id: str, vid: str, state: str, approval_id: str | None
) -> None:
    # ASCENDING LITERAL ids — within-version NULL-seq events order by
    # ``id``, so random uuids would make the reduced prior
    # nondeterministic.
    conn.exec_driver_sql(
        "INSERT INTO memory_publications (id, version_id, approval_id, "
        "state) VALUES (?, ?, ?, ?)",
        (event_id, vid, approval_id, state),
    )


def _count(conn, table: str) -> int:
    return conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()


def _events(conn, vid: str) -> list[tuple]:
    return conn.exec_driver_sql(
        "SELECT state, approval_id, event_seq FROM memory_publications "
        "WHERE version_id = ? ORDER BY id",
        (vid,),
    ).fetchall()


def _latest_snapshot(conn, scope_key: str):
    row = conn.exec_driver_sql(
        "SELECT live_set_head, membership, rendered_bundle, built_by "
        "FROM learned_item_snapshots WHERE scope_key = ? "
        "ORDER BY live_set_head DESC LIMIT 1",
        (scope_key,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), json.loads(row[1]), json.loads(row[2]), row[3]


async def _publish_first(conn, adapter, vid: str = "v-1"):
    """Seed version + empty-set approval and publish to head 1."""
    _insert_version(conn, vid)
    aid = _approve(conn, vid, compute_live_set_hash([]))
    result = await publish_learned_item_version(
        adapter, version_id=vid, approval_id=aid, actor="operator-1"
    )
    return aid, result


# ━━ (a) kill-switch OFF ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestKillSwitch:
    async def test_off_denies_both_and_writes_nothing(
        self, conn, monkeypatch
    ) -> None:
        monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)
        _insert_version(conn, "v-1")
        aid = _approve(conn, "v-1", compute_live_set_hash([]))
        adapter = _SqliteAdapterConn(conn)
        with pytest.raises(PublicationDenied, match="kill_switch_off"):
            await publish_learned_item_version(
                adapter, version_id="v-1", approval_id=aid, actor="op"
            )
        with pytest.raises(PublicationDenied, match="kill_switch_off"):
            await revoke_learned_item_version(
                adapter, version_id="v-1", revoked_by="op", reason="test"
            )
        assert _count(conn, "memory_publications") == 0
        assert _count(conn, "learned_item_snapshots") == 0

    async def test_falsy_value_denies(self, conn, monkeypatch) -> None:
        monkeypatch.setenv(KILL_SWITCH_ENV, "0")
        with pytest.raises(PublicationDenied, match="kill_switch_off"):
            await publish_learned_item_version(
                _SqliteAdapterConn(conn),
                version_id="v-1",
                approval_id="a-1",
                actor="op",
            )

    def test_truthy_parse_is_case_insensitive(self, monkeypatch) -> None:
        for value in ("1", "true", "TRUE", "yes", "YES", " True "):
            monkeypatch.setenv(KILL_SWITCH_ENV, value)
            assert publisher_mod._promotion_enabled(), value
        for value in ("", "0", "false", "no", "on", "enabled"):
            monkeypatch.setenv(KILL_SWITCH_ENV, value)
            assert not publisher_mod._promotion_enabled(), value


# ━━ (b)/(c) fresh publish + idempotent retry ━━━━━━━━━━━━━━━━━━━━━━━━


class TestFreshPublish:
    async def test_happy_path_global_scope(self, conn, enabled) -> None:
        adapter = _SqliteAdapterConn(conn)
        aid, result = await _publish_first(conn, adapter)
        expected_entry = derive_membership_entry(
            version_id="v-1",
            rendered_payload_sha256=_rendered_sha("v-1"),
            delivery_mode="retrieved",
            publication_event_seq=1,
        )
        expected_hash = compute_live_set_hash([expected_entry])
        assert result.published is True
        assert result.idempotent is False
        assert result.live_set_head == 1
        assert result.live_set_hash == expected_hash
        events = _events(conn, "v-1")
        assert sorted(e[0] for e in events) == sorted(
            ["approved", "publishing", "published"]
        )  # exactly three events
        assert all(e[1] == aid for e in events)  # approval-linked
        seqs = {e[0]: e[2] for e in events}
        assert seqs["published"] == 1
        assert seqs["approved"] is None and seqs["publishing"] is None
        head, membership, bundle, built_by = _latest_snapshot(
            conn, "global:-"
        )
        assert head == 1
        assert membership == [expected_entry]
        assert bundle == {"v-1": _rendered("v-1")}
        assert built_by == "operator-1"

    async def test_happy_path_tenant_scope(self, conn, enabled) -> None:
        adapter = _SqliteAdapterConn(conn)
        _insert_version(conn, "v-t", audience="tenant", tenant_id="t-1")
        aid = _approve(conn, "v-t", compute_live_set_hash([]))
        result = await publish_learned_item_version(
            adapter, version_id="v-t", approval_id=aid, actor="op"
        )
        assert result.live_set_head == 1
        snapshot = _latest_snapshot(conn, "tenant:t-1")
        assert snapshot is not None and snapshot[0] == 1

    async def test_idempotent_retry_same_approval(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        aid, first = await _publish_first(conn, adapter)
        again = await publish_learned_item_version(
            adapter, version_id="v-1", approval_id=aid, actor="operator-1"
        )
        assert again.published is True
        assert again.idempotent is True
        assert again.live_set_head == 1
        assert again.live_set_hash == first.live_set_hash
        assert len(_events(conn, "v-1")) == 3  # unchanged
        assert _count(conn, "learned_item_snapshots") == 1  # no new row

    async def test_version_not_found(self, conn, enabled) -> None:
        with pytest.raises(PublishValidationError) as exc:
            await publish_learned_item_version(
                _SqliteAdapterConn(conn),
                version_id="v-missing",
                approval_id="a-1",
                actor="op",
            )
        assert exc.value.reason == "version_not_found"

    async def test_approval_not_found_and_mismatch(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        _insert_version(conn, "v-1")
        _insert_version(conn, "v-2")
        with pytest.raises(PublishValidationError) as exc:
            await publish_learned_item_version(
                adapter, version_id="v-1", approval_id="a-none", actor="op"
            )
        assert exc.value.reason == "approval_not_found"
        aid_other = _approve(conn, "v-2", compute_live_set_hash([]))
        with pytest.raises(PublishValidationError) as exc:
            await publish_learned_item_version(
                adapter, version_id="v-1", approval_id=aid_other, actor="op"
            )
        assert exc.value.reason == "approval_version_mismatch"


# ━━ (d) F4 revalidation ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLiveSetRevalidation:
    async def test_stale_approval_hash_fails_closed(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish_first(conn, adapter)  # v-1 live, head 1
        _insert_version(conn, "v-2")
        stale_aid = _approve(conn, "v-2", compute_live_set_hash([]))
        with pytest.raises(PublishValidationError) as exc:
            await publish_learned_item_version(
                adapter, version_id="v-2", approval_id=stale_aid, actor="op"
            )
        assert exc.value.reason == "live_set_changed"
        # Fails BEFORE any event insert — head stays 1, no v-2 events.
        assert _events(conn, "v-2") == []
        head, _, _, _ = _latest_snapshot(conn, "global:-")
        assert head == 1


# ━━ (e)/(f) retry chain + illegal prior ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPriorStateChains:
    async def test_retry_after_publish_failed(self, conn, enabled) -> None:
        adapter = _SqliteAdapterConn(conn)
        _insert_version(conn, "v-1")
        aid = _approve(conn, "v-1", compute_live_set_hash([]))
        _seed_event(conn, "e-1", "v-1", "approved", aid)
        _seed_event(conn, "e-2", "v-1", "publishing", aid)
        _seed_event(conn, "e-3", "v-1", "publish_failed", aid)
        result = await publish_learned_item_version(
            adapter, version_id="v-1", approval_id=aid, actor="op"
        )
        assert result.published is True and result.live_set_head == 1
        events = _events(conn, "v-1")
        assert len(events) == 5  # publishing + published only were added
        states = sorted(e[0] for e in events)
        assert states == sorted(
            [
                "approved",
                "publishing",
                "publish_failed",
                "publishing",
                "published",
            ]
        )
        published = [e for e in events if e[0] == "published"]
        assert len(published) == 1 and published[0][2] == 1

    async def test_illegal_prior_fails_closed(self, conn, enabled) -> None:
        adapter = _SqliteAdapterConn(conn)
        _insert_version(conn, "v-1")
        aid = _approve(conn, "v-1", compute_live_set_hash([]))
        _seed_event(conn, "e-1", "v-1", "publishing", aid)
        with pytest.raises(PublishValidationError) as exc:
            await publish_learned_item_version(
                adapter, version_id="v-1", approval_id=aid, actor="op"
            )
        assert exc.value.reason == "illegal_transition:publishing"
        assert len(_events(conn, "v-1")) == 1  # no auto-heal


# ━━ (g)/(h) supersede + revoke ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _publish_then_supersede(conn, adapter):
    """v-1 live at head 1, then v-2 supersedes it at head 2."""
    await _publish_first(conn, adapter, "v-1")
    _, membership, _, _ = _latest_snapshot(conn, "global:-")
    _insert_version(conn, "v-2")
    aid2 = _approve(conn, "v-2", compute_live_set_hash(membership))
    result = await publish_learned_item_version(
        adapter,
        version_id="v-2",
        approval_id=aid2,
        actor="op",
        supersedes_version_id="v-1",
    )
    return result


class TestSupersedeAndRevoke:
    async def test_supersede_retires_predecessor(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        result = await _publish_then_supersede(conn, adapter)
        assert result.live_set_head == 2
        superseded = [
            e for e in _events(conn, "v-1") if e[0] == "superseded"
        ]
        assert len(superseded) == 1
        row = conn.exec_driver_sql(
            "SELECT approval_id, revoke_reason FROM memory_publications "
            "WHERE version_id = 'v-1' AND state = 'superseded'"
        ).fetchone()
        assert row[0] is None  # ungated state
        assert row[1] == "superseded_by:v-2"
        head, membership, bundle, _ = _latest_snapshot(conn, "global:-")
        assert head == 2
        assert [m["version_id"] for m in membership] == ["v-2"]
        assert membership[0]["publication_event_seq"] == 2
        assert set(bundle) == {"v-2"}
        assert _rendered("v-1") not in json.dumps(bundle)  # bytes dropped

    async def test_supersede_requires_live_predecessor(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        _insert_version(conn, "v-1")
        aid = _approve(conn, "v-1", compute_live_set_hash([]))
        with pytest.raises(PublishValidationError) as exc:
            await publish_learned_item_version(
                adapter,
                version_id="v-1",
                approval_id=aid,
                actor="op",
                supersedes_version_id="v-ghost",
            )
        assert exc.value.reason == "not_published"

    async def test_revoke_makes_bytes_unretrievable(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish_then_supersede(conn, adapter)  # v-2 live, head 2
        result = await revoke_learned_item_version(
            adapter, version_id="v-2", revoked_by="op", reason="bad-item"
        )
        assert result.revoked is True
        assert result.live_set_head == 3
        assert result.live_set_hash == compute_live_set_hash([])
        head, membership, bundle, _ = _latest_snapshot(conn, "global:-")
        assert head == 3
        assert membership == []
        # F7.5 unretrievable proof: the revoked bytes are ABSENT from
        # the head-3 snapshot row.
        assert bundle == {}
        assert _rendered("v-2") not in json.dumps(bundle)
        revoked = conn.exec_driver_sql(
            "SELECT revoked_by, revoke_reason, revoked_at "
            "FROM memory_publications "
            "WHERE version_id = 'v-2' AND state = 'revoked'"
        ).fetchone()
        assert revoked[0] == "op"
        assert revoked[1] == "bad-item"
        assert revoked[2] is None  # no clock reads — event row is the record
        with pytest.raises(PublishValidationError) as exc:
            await revoke_learned_item_version(
                adapter, version_id="v-2", revoked_by="op", reason="again"
            )
        assert exc.value.reason == "not_published"


# ━━ (i) invariant divergence + rollback proof + reconcile probe ━━━━━━


def _tampered_derivation():
    async def tampered(conn, *, scope_key):
        return [
            derive_membership_entry(
                version_id="ghost",
                rendered_payload_sha256="f" * 64,
                delivery_mode="retrieved",
                publication_event_seq=99,
            )
        ]

    return tampered


class TestPublicationInvariant:
    async def test_divergence_raises_and_rolls_back(
        self, conn, enabled, monkeypatch
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        _insert_version(conn, "v-1")
        aid = _approve(conn, "v-1", compute_live_set_hash([]))
        monkeypatch.setattr(
            publisher_mod, "_derive_ledger_membership", _tampered_derivation()
        )
        with pytest.raises(PublishValidationError) as exc:
            await publish_learned_item_version(
                adapter, version_id="v-1", approval_id=aid, actor="op"
            )
        assert exc.value.reason == "invariant_divergence"
        # Pre-rollback: the aborted publish DID write inside the open
        # txn (events + snapshot row) — the raise is what forces the
        # caller to roll back.
        assert _count(conn, "memory_publications") == 3
        assert _count(conn, "learned_item_snapshots") == 1
        conn.rollback()
        # GLOBAL emptiness: harness DML (fixtures included) lives in one
        # implicit txn — tables survive, rows gone.
        for table in (
            "memory_publications",
            "learned_item_snapshots",
            "memory_approvals",
            "memory_eval_runs",
            "learned_item_versions",
        ):
            assert _count(conn, table) == 0, table
        # The probe on the tampered derivation reports the divergence.
        reasons = await reconcile_publication_invariant(
            adapter, scope_key="global:-"
        )
        assert reasons == ("missing_from_snapshot:ghost",)

    async def test_reconcile_probe_healthy_scope(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish_first(conn, adapter)
        reasons = await reconcile_publication_invariant(
            adapter, scope_key="global:-"
        )
        assert reasons == ()

    async def test_reconcile_probe_is_ungated(
        self, conn, monkeypatch
    ) -> None:
        monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)
        reasons = await reconcile_publication_invariant(
            _SqliteAdapterConn(conn), scope_key="global:-"
        )
        assert reasons == ()  # empty scope, no snapshot — and no denial


# ━━ (j) pure comparator + entry constructor ━━━━━━━━━━━━━━━━━━━━━━━━━


def _entry(vid: str, seq: int | None = 1) -> dict:
    return derive_membership_entry(
        version_id=vid,
        rendered_payload_sha256="a" * 64,
        delivery_mode="retrieved",
        publication_event_seq=seq,
    )


class TestCheckPublicationInvariantPure:
    def test_clean(self) -> None:
        assert check_publication_invariant(
            [_entry("v-1")], [_entry("v-1")]
        ) == ()
        assert check_publication_invariant([], []) == ()

    def test_missing_from_snapshot(self) -> None:
        assert check_publication_invariant([_entry("v-1")], []) == (
            "missing_from_snapshot:v-1",
        )

    def test_extra_in_snapshot(self) -> None:
        assert check_publication_invariant([], [_entry("v-1")]) == (
            "extra_in_snapshot:v-1",
        )

    def test_hash_mismatch_on_any_component(self) -> None:
        assert check_publication_invariant(
            [_entry("v-1", seq=1)], [_entry("v-1", seq=2)]
        ) == ("hash_mismatch:v-1",)

    def test_deterministic_order_sorted_by_version_id(self) -> None:
        ledger = [_entry("a"), _entry("c", seq=1)]
        snapshot = [_entry("b"), _entry("c", seq=7)]
        assert check_publication_invariant(ledger, snapshot) == (
            "missing_from_snapshot:a",
            "extra_in_snapshot:b",
            "hash_mismatch:c",
        )


class TestDeriveMembershipEntry:
    def test_key_exactness_feeds_hash_clean(self) -> None:
        entry = _entry("v-1")
        assert set(entry) == {
            "version_id",
            "rendered_payload_sha256",
            "delivery_mode",
            "publication_event_seq",
        }
        # compute_live_set_hash validates key-exactness — no raise.
        assert len(compute_live_set_hash([entry])) == 64


# ━━ (k) metrics registrations ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMetricsRegistrations:
    def test_g7_names_registered_and_noop_safe(self) -> None:
        import backend.metrics as m

        for name in (
            "memory_quarantine_depth",
            "memory_promotion_total",
            "memory_eval_delta",
            "memory_neg_control_catch_total",
            "memory_failclosed_total",
            "memory_proposal_outcome_total",
            "memory_delivery_total",
            "memory_reconcile_divergence_total",
            "memory_snapshot_stale_total",
            "memory_enabled",
            "memory_liveness_heartbeat",
        ):
            assert hasattr(m, name), name
        # Phase-S no-op-safe assertion style — callable either way, no
        # scrape-output assertions.
        m.memory_promotion_total.labels(decision="published").inc()
        m.memory_promotion_total.labels(decision="revoked").inc()
        m.memory_reconcile_divergence_total.labels(
            scope="global:-", reason="hash_mismatch"
        ).inc()
        m.memory_neg_control_catch_total.inc()
        m.memory_snapshot_stale_total.inc()
        m.memory_failclosed_total.labels(reason="test").inc()
        m.memory_proposal_outcome_total.labels(decision="test").inc()
        m.memory_delivery_total.labels(result="test").inc()
        m.memory_quarantine_depth.set(0)
        m.memory_eval_delta.set(0)
        m.memory_enabled.set(0)
        m.memory_liveness_heartbeat.set(0)


# ━━ (l) dormant ship ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDormantShip:
    def test_no_new_caller_of_the_publisher_module(self) -> None:
        # The B sweep already enforces the full two-string ban — this
        # narrower check pins THIS ticket's promise: still no caller.
        own = {"learned_item_publisher.py", "learned_item_publication.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if len(parts) == 1 and parts[0] in own:
                continue
            if "learned_item_publisher" in py.read_text(errors="ignore"):
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"
