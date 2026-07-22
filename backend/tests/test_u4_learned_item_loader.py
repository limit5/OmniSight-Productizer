"""OP-2572 U4-C2 — learned-item loader read tests (offline).

Covers the ticket AC (a)-(k) on the stacked sqlite harness (0258+0259)
with the ``$N`` → ``?`` adapter (fetchrow/fetch/execute + advisory-lock
no-op). Fixtures drive the REAL C1 publisher (approval → publish) so
``refresh_scope`` reads REAL snapshots — the C1→C2 integration seam.
The kill-switch env is monkeypatched per test and the loader cache is
reset around every test.

Keyword fixtures are WHOLE ≥3-char tokens present in the context —
token-bag scoring means substring fragments score zero. Metrics are
accessed attribute-style (``metrics.memory_delivery_total``) so the
delivery-metric spy monkeypatch works; ``memory_snapshot_stale_total``
takes NO labels (bare ``.inc()``).
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

import backend.learned_item_loader as loader
import backend.prompt_loader as prompt_loader
from backend import metrics
from backend.learned_item_loader import (
    get_learned_items_block,
    refresh_scope,
)
from backend.learned_item_publication import compute_live_set_hash
from backend.learned_item_publisher import publish_learned_item_version, revoke_learned_item_version


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
_PREAMBLE = "Learned items (retrieved, lower authority):"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0258():
    return _load_module(MIGRATION_0258, "_alembic_test_0258_for_loader")


@pytest.fixture(scope="module")
def m0259():
    return _load_module(MIGRATION_0259, "_alembic_test_0259_for_loader")


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
    # β-3a: reading now ALSO requires the separate read flag.
    monkeypatch.setenv("OMNISIGHT_LEARNED_ITEM_READ", "1")


@pytest.fixture(autouse=True)
def _fresh_cache():
    loader._reset_for_tests()
    yield
    loader._reset_for_tests()


class _SqliteAdapterConn:
    """asyncpg-shaped facade over a sync sqlalchemy sqlite connection
    (the C1 harness idiom): ``$N`` → ``?`` rewrite + advisory-lock
    no-op."""

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


class _CounterSpy:
    """Attribute-style Counter stand-in: records every inc with its
    labels (bare ``.inc()`` records ``{}``)."""

    def __init__(self) -> None:
        self.incs: list[dict] = []

    def labels(self, **labels):
        spy = self

        class _Child:
            def inc(self, amount: float = 1) -> None:
                spy.incs.append(labels)

        return _Child()

    def inc(self, amount: float = 1) -> None:
        self.incs.append({})


# ━━ Fixture seeds (raw SQL + the REAL C1 publisher) ━━━━━━━━━━━━━━━━━━


def _rendered(vid: str) -> str:
    # A2-fence-shaped deterministic payload: the loader must inject
    # these EXACT bytes verbatim (never re-render / re-fence).
    return (
        f"----- BEGIN UNTRUSTED LEARNED-ITEM DATA [lid-fence-{vid}] -----\n"
        f"Scope: lesson body for {vid}\n"
        f"----- END UNTRUSTED LEARNED-ITEM DATA [lid-fence-{vid}] -----"
    )


def _rendered_sha(vid: str) -> str:
    return hashlib.sha256(_rendered(vid).encode("utf-8")).hexdigest()


def _insert_version(
    conn,
    vid: str,
    *,
    audience: str = "global",
    tenant_id: str | None = None,
    delivery_mode: str = "retrieved",
    name: str | None = None,
    description: str | None = None,
    trigger_condition: str | None = None,
    keywords: list[str] | None = None,
) -> None:
    conn.exec_driver_sql(
        "INSERT INTO learned_item_versions "
        "(id, canonical_content_hash, kind, audience, tenant_id, payload, "
        " rendered_payload, renderer_version, rendered_payload_sha256, "
        " delivery_mode, name, description, trigger_condition, keywords, "
        " created_by) "
        "VALUES (?, ?, 'lesson', ?, ?, ?, ?, 'r1', ?, ?, ?, ?, ?, ?, 'test')",
        (
            vid,
            hashlib.sha256(vid.encode()).hexdigest(),
            audience,
            tenant_id,
            json.dumps({"body": vid}),
            _rendered(vid),
            _rendered_sha(vid),
            delivery_mode,
            name,
            description,
            trigger_condition,
            json.dumps(keywords) if keywords is not None else None,
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


def _current_membership(conn, scope_key: str) -> list[dict]:
    row = conn.exec_driver_sql(
        "SELECT membership FROM learned_item_snapshots WHERE scope_key = ? "
        "ORDER BY live_set_head DESC LIMIT 1",
        (scope_key,),
    ).fetchone()
    return [] if row is None else json.loads(row[0])


async def _publish(
    conn,
    adapter,
    vid: str,
    *,
    audience: str = "global",
    tenant_id: str | None = None,
    delivery_mode: str = "retrieved",
    keywords: list[str] | None = None,
    name: str | None = None,
) -> None:
    """Seed a version + matching-live-set approval and drive the REAL
    C1 publisher (the C1→C2 integration seam)."""
    scope_key = (
        f"tenant:{tenant_id}" if audience == "tenant" else "global:-"
    )
    _insert_version(
        conn,
        vid,
        audience=audience,
        tenant_id=tenant_id,
        delivery_mode=delivery_mode,
        keywords=keywords,
        name=name,
    )
    aid = _approve(
        conn, vid, compute_live_set_hash(_current_membership(conn, scope_key))
    )
    await publish_learned_item_version(
        adapter, version_id=vid, approval_id=aid, actor="op"
    )


# ━━ (a) kill-switch OFF ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestKillSwitchOff:
    def test_off_short_circuits_loud(self, monkeypatch) -> None:
        monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)
        spy = _CounterSpy()
        monkeypatch.setattr(metrics, "memory_delivery_total", spy)
        # No cache entry exists at all — the OFF path never reads it.
        block, result = get_learned_items_block(
            tenant_id="t-1", context="kubernetes rollout"
        )
        assert (block, result) == ("", "kill_switch_off")
        assert spy.incs == [{"result": "kill_switch_off"}]

    def test_falsy_value_short_circuits(self, monkeypatch) -> None:
        monkeypatch.setenv(KILL_SWITCH_ENV, "0")
        block, result = get_learned_items_block(
            tenant_id=None, context="anything"
        )
        assert (block, result) == ("", "kill_switch_off")


# ━━ (b) full pipeline: publish → refresh → inject ━━━━━━━━━━━━━━━━━━━


class TestFullPipeline:
    async def test_publish_refresh_inject_non_empty(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(
            conn,
            adapter,
            "v-1",
            keywords=["kubernetes", "ingress"],
            name="k8s ingress lesson",
        )
        head = await refresh_scope(adapter, "global:-")
        assert head == 1
        block, result = get_learned_items_block(
            tenant_id=None,
            context="debug the kubernetes ingress rollout failure",
        )
        assert result == "non_empty"
        # Fixed one-line preamble, then the EXACT A2-fenced bytes.
        assert block.startswith(_PREAMBLE)
        begin_line = _rendered("v-1").splitlines()[0]
        assert begin_line in block  # fence BEGIN line verbatim
        assert _rendered("v-1") in block  # exact stored bundle bytes

    async def test_always_injected_member_not_delivered_v1(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(
            conn,
            adapter,
            "v-ai",
            delivery_mode="always_injected",
            keywords=["kubernetes"],
        )
        await refresh_scope(adapter, "global:-")
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert (block, result) == ("", "empty_expected")


# ━━ (c) legitimately empty ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmptyExpected:
    async def test_no_keyword_match(self, conn, enabled) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(conn, adapter, "v-1", keywords=["kubernetes"])
        await refresh_scope(adapter, "global:-")
        block, result = get_learned_items_block(
            tenant_id=None, context="frontend stylesheet layout polish"
        )
        assert (block, result) == ("", "empty_expected")

    async def test_refresh_scope_with_no_snapshot(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        head = await refresh_scope(adapter, "global:-")
        assert head == 0  # nothing published — valid refreshed state
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert (block, result) == ("", "empty_expected")


# ━━ (d) missing cache entry while enabled = hollow signature ━━━━━━━━━


class TestEmptyDegraded:
    def test_missing_global_entry(self, enabled) -> None:
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert (block, result) == ("", "empty_degraded")

    async def test_missing_tenant_entry(self, conn, enabled) -> None:
        adapter = _SqliteAdapterConn(conn)
        await refresh_scope(adapter, "global:-")  # global cached
        block, result = get_learned_items_block(
            tenant_id="t-1", context="kubernetes rollout"
        )
        assert (block, result) == ("", "empty_degraded")


# ━━ (e) tenant scoping + scope_unresolved ━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTenantScoping:
    async def _seed_both_scopes(self, conn, adapter) -> None:
        await _publish(
            conn, adapter, "v-g", keywords=["kubernetes"], name="global"
        )
        await _publish(
            conn,
            adapter,
            "v-t",
            audience="tenant",
            tenant_id="t-1",
            keywords=["kubernetes"],
            name="tenant",
        )
        await refresh_scope(adapter, "global:-")
        await refresh_scope(adapter, "tenant:t-1")

    async def test_tenant_item_visible_only_with_tenant_id(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await self._seed_both_scopes(conn, adapter)
        block, result = get_learned_items_block(
            tenant_id="t-1", context="kubernetes rollout"
        )
        assert result == "non_empty"
        assert _rendered("v-t") in block
        assert _rendered("v-g") in block

    async def test_none_tenant_reads_global_only_and_measures_miss(
        self, conn, enabled, monkeypatch
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await self._seed_both_scopes(conn, adapter)
        spy = _CounterSpy()
        monkeypatch.setattr(metrics, "memory_failclosed_total", spy)
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        # Result still computed from the global scope (lawful fallback).
        assert result == "non_empty"
        assert _rendered("v-g") in block
        assert _rendered("v-t") not in block
        assert spy.incs == [{"reason": "scope_unresolved"}]
        # Phase-S no-op-safe: the real metric object is callable too.
        metrics.memory_failclosed_total.labels(
            reason="scope_unresolved"
        ).inc()


# ━━ (f) retrieved-invariant: corrupted bundle degrades whole block ━━━


class TestRetrievedInvariant:
    async def test_corrupt_bundle_bytes_degrade(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(conn, adapter, "v-1", keywords=["kubernetes"])
        await refresh_scope(adapter, "global:-")
        # Mutate the cached bundle bytes — sha no longer matches the
        # membership rendered_payload_sha256.
        loader._cache["global:-"].bundle["v-1"] = "TAMPERED-BYTES"
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert (block, result) == ("", "empty_degraded")


# ━━ (g) revoke flow: bytes unretrievable end-to-end ━━━━━━━━━━━━━━━━━━


class TestRevokeFlow:
    async def test_revoked_bytes_gone_from_block(
        self, conn, enabled
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(conn, adapter, "v-1", keywords=["kubernetes"])
        await refresh_scope(adapter, "global:-")
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert result == "non_empty" and _rendered("v-1") in block
        await revoke_learned_item_version(
            adapter, version_id="v-1", revoked_by="op", reason="bad-item"
        )
        head = await refresh_scope(adapter, "global:-")
        assert head == 2
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert (block, result) == ("", "empty_expected")
        assert _rendered("v-1") not in block


# ━━ (h) stale-serve: refresh failure keeps last-good, loudly ━━━━━━━━━


class _RaisingConn:
    async def fetchrow(self, sql: str, *params):
        raise RuntimeError("db down")

    async def fetch(self, sql: str, *params):
        raise RuntimeError("db down")

    async def execute(self, sql: str, *params):
        raise RuntimeError("db down")


class TestStaleServe:
    async def test_refresh_failure_raises_and_keeps_last_good(
        self, conn, enabled, monkeypatch
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(conn, adapter, "v-1", keywords=["kubernetes"])
        await refresh_scope(adapter, "global:-")
        spy = _CounterSpy()
        monkeypatch.setattr(metrics, "memory_snapshot_stale_total", spy)
        with pytest.raises(RuntimeError, match="db down"):
            await refresh_scope(_RaisingConn(), "global:-")
        assert spy.incs == [{}]  # bare .inc() — no labels
        # Previous entry retained (serve-stale) — the read still works.
        assert loader._cache["global:-"].live_set_head == 1
        block, result = get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert result == "non_empty" and _rendered("v-1") in block


# ━━ (i) delivery metric: every path increments exactly once ━━━━━━━━━━


class TestDeliveryMetricEveryPath:
    def _spy(self, monkeypatch) -> _CounterSpy:
        spy = _CounterSpy()
        monkeypatch.setattr(metrics, "memory_delivery_total", spy)
        return spy

    def test_kill_switch_off_once(self, monkeypatch) -> None:
        monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)
        spy = self._spy(monkeypatch)
        get_learned_items_block(tenant_id=None, context="x")
        assert spy.incs == [{"result": "kill_switch_off"}]

    def test_empty_degraded_once(self, enabled, monkeypatch) -> None:
        spy = self._spy(monkeypatch)
        get_learned_items_block(tenant_id=None, context="kubernetes")
        assert spy.incs == [{"result": "empty_degraded"}]

    async def test_empty_expected_once(
        self, conn, enabled, monkeypatch
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await refresh_scope(adapter, "global:-")
        spy = self._spy(monkeypatch)
        get_learned_items_block(tenant_id=None, context="kubernetes")
        assert spy.incs == [{"result": "empty_expected"}]

    async def test_non_empty_once(
        self, conn, enabled, monkeypatch
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(conn, adapter, "v-1", keywords=["kubernetes"])
        await refresh_scope(adapter, "global:-")
        spy = self._spy(monkeypatch)
        get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert spy.incs == [{"result": "non_empty"}]

    async def test_invariant_degraded_once(
        self, conn, enabled, monkeypatch
    ) -> None:
        adapter = _SqliteAdapterConn(conn)
        await _publish(conn, adapter, "v-1", keywords=["kubernetes"])
        await refresh_scope(adapter, "global:-")
        loader._cache["global:-"].bundle["v-1"] = "TAMPERED-BYTES"
        spy = self._spy(monkeypatch)
        get_learned_items_block(
            tenant_id=None, context="kubernetes rollout"
        )
        assert spy.incs == [{"result": "empty_degraded"}]


# ━━ (j) prompt_loader integration + V3.5 leak close ━━━━━━━━━━━━━━━━━━


def _seed_cache_directly() -> None:
    """Monkeypatch-style cache seed (no DB): one retrieved global item
    whose keywords match a 'kubernetes' context token, plus an empty
    tenant scope so tenant reads are cache-complete."""
    vid = "v-cache"
    payload = _rendered(vid)
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    member = {
        "version_id": vid,
        "rendered_payload_sha256": sha,
        "delivery_mode": "retrieved",
        "publication_event_seq": 1,
    }
    loader._cache["global:-"] = loader._ScopeEntry(
        live_set_head=1,
        membership=[member],
        bundle={vid: payload},
        catalog=[
            {
                "version_id": vid,
                "name": "k8s lesson",
                "description": "kubernetes rollout lesson",
                "trigger_condition": "",
                "keywords": ["kubernetes"],
            }
        ],
    )
    loader._cache["tenant:t-1"] = loader._ScopeEntry(
        live_set_head=0, membership=[], bundle={}, catalog=[]
    )


class TestPromptLoaderIntegration:
    def test_non_empty_block_injected_and_capture_skipped(
        self, enabled, monkeypatch
    ) -> None:
        _seed_cache_directly()
        captured: list[tuple] = []
        monkeypatch.setattr(
            prompt_loader,
            "_schedule_prompt_snapshot",
            lambda *a, **k: captured.append(a),
        )
        out = prompt_loader.build_system_prompt(
            agent_type="general",
            domain_context="kubernetes rollout failure",
            tenant_id="t-1",
        )
        assert _PREAMBLE in out
        assert _rendered("v-cache") in out
        assert captured == []  # V3.5: tenant content never captured

    def test_flag_off_output_unchanged_and_capture_fires(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)
        _seed_cache_directly()
        captured: list[tuple] = []
        monkeypatch.setattr(
            prompt_loader,
            "_schedule_prompt_snapshot",
            lambda *a, **k: captured.append(a),
        )
        baseline = prompt_loader.build_system_prompt(
            agent_type="general",
            domain_context="kubernetes rollout failure",
        )
        out = prompt_loader.build_system_prompt(
            agent_type="general",
            domain_context="kubernetes rollout failure",
            tenant_id="t-1",
        )
        assert out == baseline  # zero behaviour change while dormant
        assert _PREAMBLE not in out
        assert len(captured) == 2  # capture fired for BOTH empty builds

    def test_enabled_but_empty_block_still_captures(
        self, enabled, monkeypatch
    ) -> None:
        _seed_cache_directly()
        captured: list[tuple] = []
        monkeypatch.setattr(
            prompt_loader,
            "_schedule_prompt_snapshot",
            lambda *a, **k: captured.append(a),
        )
        out = prompt_loader.build_system_prompt(
            agent_type="general",
            domain_context="frontend stylesheet polish",  # no match
            tenant_id="t-1",
        )
        assert _PREAMBLE not in out
        assert len(captured) == 1  # empty learned block ⇒ capture runs


# ━━ (k) dormant ship: no non-test caller outside the own-set ━━━━━━━━━


class TestDormantShip:
    def test_no_external_loader_reference(self) -> None:
        own = {"learned_item_loader.py", "prompt_loader.py",
               # β-3c: the WIRED per-worker refresh loop + the runner
               # endpoint (dormant premise ended; dual-flag gated).
               "learned_item_refresh.py", "routers/learned_items.py"}
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
            if "learned_item_loader" in py.read_text(errors="ignore"):
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"
