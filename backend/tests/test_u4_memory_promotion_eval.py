"""OP-2575 U4-F-exec — plan-triage eval executor tests (offline).

Covers the ticket AC (a)-(j) on the stacked sqlite harness (0258+0259):

* (a) promote path — scripted ask_fn where the candidate arm beats the
  baseline on 9 of 10 cases; eval-run row + 10 case rows written, and
  ``stat_summary`` carries the F-math 4-key shape + ``detail`` +
  ``prompt_assembly_fingerprint`` + ``samples_per_case``.
* (b) reject path (candidate regresses).
* (c) insufficient_evidence (few discordant).
* (d) infra_invalid matrix — every terminal writes decision +
  reason code + NO case rows + bumps the failclosed counter.
* (e) neg-control enforcement — candidate-side pass on a flagged
  question forces reject and bumps the catch counter; a flagged
  question the candidate fails leaves the stats path alone.
* (f) baseline cache — two evals same (suite, client) → second run's
  ask_fn sees ONLY candidate-arm calls (call-count halves).
* (g) fingerprints — deterministic, 64-hex, template-version sensitive.
* (h) arm-prompt purity — baseline prompt contains NO candidate bytes;
  candidate prompt embeds the EXACT ``rendered_payload`` verbatim (fence
  line present).
* (i) dormant rglob (membership form) + module-source string-ban test.
* (j) sample math — ``samples_per_case=3`` with a flip-flopping scripted
  fn → majority collapse feeds the F-math stats (integration).

All tests are offline: scripted ask_fns only; the REAL ``build_ask_fn``
is exercised solely through a monkeypatched-None ``get_llm`` smoke test.
The tmp_path shards + manifest go through the REAL H module — every
verified-load code path is exercised end-to-end.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from backend import memory_promotion_eval as mpe
from backend.eval_suite_manifest import SuiteManifestError
from backend.memory_promotion_eval import (
    EvalClient,
    EvalOutcome,
    TEMPLATE_VERSION,
    _baseline_prompt,
    _candidate_prompt,
    _reset_for_tests,
    build_ask_fn,
    model_fingerprint,
    prompt_assembly_fingerprint,
    run_plan_triage_eval,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = BACKEND_ROOT / "memory_promotion_eval.py"
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
CAND_RENDERED_BYTES = "CANDIDATE-ITEM-RENDERED-BYTES-v1"
CAND_RENDERED_SHA = hashlib.sha256(CAND_RENDERED_BYTES.encode()).hexdigest()


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0258():
    return _load_module(MIGRATION_0258, "_alembic_test_0258_for_eval")


@pytest.fixture(scope="module")
def m0259():
    return _load_module(MIGRATION_0259, "_alembic_test_0259_for_eval")


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

    Rewrites ``$N`` → ``?`` (each ``$N`` used once in ascending order in
    this module's SQL). Adds a no-op path for the advisory-lock SQL —
    sqlite has no advisory locks; this module never issues it but the
    adapter matches the C1 test file's shape for future-proofing.
    """

    _LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext($1))"

    def __init__(self, connection) -> None:
        self._connection = connection

    def _run(self, sql: str, params: tuple):
        return self._connection.exec_driver_sql(
            re.sub(r"\$\d+", "?", sql), params
        )

    async def execute(self, sql: str, *params):
        if sql == self._LOCK_SQL:
            return None
        return self._run(sql, tuple(params))

    async def fetchrow(self, sql: str, *params):
        return self._run(sql, tuple(params)).fetchone()

    async def fetch(self, sql: str, *params):
        return self._run(sql, tuple(params)).fetchall()


# ━━ helpers ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _insert_version(conn, vid: str) -> None:
    """Seed one row on the A1 versions table so the eval-run row has a
    valid FK target under the 0258 constraints."""
    conn.exec_driver_sql(
        "INSERT INTO learned_item_versions "
        "(id, canonical_content_hash, kind, audience, payload, created_by) "
        "VALUES (?, ?, 'lesson', 'global', '{}', 'test')",
        (vid, hashlib.sha256(vid.encode()).hexdigest()),
    )


def _shard_yaml(
    *,
    n: int = 10,
    name: str = "tmp-shard",
    neg_ids: tuple[str, ...] = (),
) -> str:
    """A REAL IQQuestion-valid shard (firmware-debug.yaml shape)."""
    lines = ["schema_version: 1", f'name: "{name}"', "questions:"]
    for i in range(1, n + 1):
        qid = f"q{i:02d}"
        lines += [
            f"  - id: {qid}",
            f'    prompt: "Which keyword answers case {i}?"',
            f'    expected_keywords: ["kw{i:02d}"]',
        ]
        if qid in neg_ids:
            lines.append("    neg_control: true")
    return "\n".join(lines) + "\n"


def _build_suite(
    tmp_path: Path,
    *,
    n: int = 10,
    name: str = "tmp-shard",
    min_neg_controls: int = 0,
    neg_ids: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """Write one shard + a manifest with REAL computed sha256s."""
    fname = "shard.yaml"
    shard_path = tmp_path / fname
    shard_path.write_text(
        _shard_yaml(n=n, name=name, neg_ids=neg_ids), encoding="utf-8"
    )
    entries = [
        {
            "path": fname,
            "sha256": hashlib.sha256(shard_path.read_bytes()).hexdigest(),
            "min_cases": n,
            "min_neg_controls": min_neg_controls,
        }
    ]
    manifest_path = tmp_path / "manifest.yml"
    manifest_path.write_text(
        yaml.safe_dump({"schema_version": 1, "suites": entries}),
        encoding="utf-8",
    )
    return manifest_path, tmp_path


def _client(temperature: float = 0.0) -> EvalClient:
    return EvalClient(
        provider="anthropic", model="claude-opus-4-7", temperature=temperature
    )


def _scripted(
    *,
    baseline_passes: dict[str, bool] | None = None,
    candidate_passes: dict[str, bool] | None = None,
    tokens_per_call: int = 10,
):
    """Return an ask_fn + a call counter. The ask_fn recognizes the
    baseline vs candidate arm by whether the caller's ``rendered_payload``
    bytes appear in the prompt; per-question routing is by
    ``expected_keywords`` (each shard question has EXACTLY one keyword —
    the qid-derived ``kwNN``).

    Answering with the right ``kwNN`` → ``IQQuestion.matches`` = True.
    Answering with "no" → False.
    """
    baseline_passes = baseline_passes or {}
    candidate_passes = candidate_passes or {}
    calls: dict[str, int] = {"baseline": 0, "candidate": 0, "total": 0}

    async def _f(model: str, prompt: str) -> tuple[str, int]:
        calls["total"] += 1
        is_candidate = CAND_RENDERED_BYTES in prompt
        arm = "candidate" if is_candidate else "baseline"
        calls[arm] += 1
        m = re.search(r"case (\d+)", prompt)
        qid = f"q{int(m.group(1)):02d}" if m else "q00"
        table = candidate_passes if is_candidate else baseline_passes
        passes = table.get(qid, False)
        return ("kw" + qid[1:] if passes else "no", tokens_per_call)

    return _f, calls


@pytest.fixture(autouse=True)
def _reset_baseline_cache_between_tests():
    """(f) deliberately runs two evals inside ONE test to prove the hit;
    every OTHER test needs a fresh cache to isolate the arm-call math."""
    _reset_for_tests()
    yield
    _reset_for_tests()


# ━━ (a) promote path ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPromotePath:
    async def test_candidate_wins_nine_of_ten_writes_promote(
        self, conn, tmp_path
    ) -> None:
        _insert_version(conn, "v-promote")
        manifest, base = _build_suite(tmp_path)
        # Candidate passes q01-q09; baseline passes none.
        cand = {f"q{i:02d}": True for i in range(1, 10)}
        ask_fn, calls = _scripted(candidate_passes=cand)

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-promote",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-a",
            now="2026-07-11T00:00:00Z",
            samples_per_case=3,
        )

        assert outcome.decision == "promote"
        assert outcome.n == 10
        assert outcome.net_flips == 9
        assert outcome.mcnemar_p is not None and outcome.mcnemar_p < 0.05
        assert outcome.suite_sha256 and len(outcome.suite_sha256) == 64
        assert outcome.infra_reason is None

        row = conn.exec_driver_sql(
            "SELECT eval_kind, decision, suite_sha256, live_set_hash, "
            "model_fingerprint, stat_summary "
            "FROM memory_eval_runs WHERE id = ?",
            (outcome.eval_run_id,),
        ).fetchone()
        assert row is not None
        eval_kind, decision, suite_hash, live_set_hash, model_fp, ss_json = row
        assert eval_kind == "plan_triage"
        assert decision == "promote"
        assert suite_hash == outcome.suite_sha256
        assert live_set_hash == "lsh-a"
        assert model_fp == model_fingerprint(_client())

        import json as _json

        ss = _json.loads(ss_json)
        assert ss["decision"] == "promote"
        # F-math 4 keys are present:
        for k in ("decision", "mcnemar_p", "net_flips", "n"):
            assert k in ss
        # detail sub-dict from build_stat_detail:
        assert ss["detail"] == {"improvements": 9, "regressions": 0}
        # prompt-assembly fingerprint + samples_per_case:
        assert ss["samples_per_case"] == 3
        assert ss["prompt_assembly_fingerprint"] == prompt_assembly_fingerprint(
            suite_sha256=outcome.suite_sha256,
            candidate_rendered_sha256=CAND_RENDERED_SHA,
            template_version=TEMPLATE_VERSION,
        )
        # model_fingerprint is a COLUMN, not in the JSON (freeze F4).
        assert "model_fingerprint" not in ss

        case_count = conn.exec_driver_sql(
            "SELECT count(*) FROM memory_eval_cases WHERE eval_run_id = ?",
            (outcome.eval_run_id,),
        ).scalar()
        assert case_count == 10


# ━━ (b) reject path ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRejectPath:
    async def test_candidate_regresses_writes_reject(
        self, conn, tmp_path
    ) -> None:
        _insert_version(conn, "v-reject")
        manifest, base = _build_suite(tmp_path)
        base_passes = {f"q{i:02d}": True for i in range(1, 10)}  # baseline 9/10
        ask_fn, _ = _scripted(baseline_passes=base_passes, candidate_passes={})

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-reject",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-b",
            now="2026-07-11T00:00:00Z",
        )

        assert outcome.decision == "reject"
        assert outcome.n == 10
        assert outcome.net_flips == -9
        row = conn.exec_driver_sql(
            "SELECT decision FROM memory_eval_runs WHERE id = ?",
            (outcome.eval_run_id,),
        ).fetchone()
        assert row[0] == "reject"


# ━━ (c) insufficient_evidence ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInsufficientEvidence:
    async def test_few_discordant_pairs_is_insufficient(
        self, conn, tmp_path
    ) -> None:
        _insert_version(conn, "v-insuf")
        manifest, base = _build_suite(tmp_path)
        # Both sides pass everything → 0 discordant → insufficient_evidence.
        all_pass = {f"q{i:02d}": True for i in range(1, 11)}
        ask_fn, _ = _scripted(baseline_passes=all_pass, candidate_passes=all_pass)

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-insuf",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-c",
            now="2026-07-11T00:00:00Z",
        )

        assert outcome.decision == "insufficient_evidence"
        assert outcome.n == 10
        assert outcome.net_flips == 0


# ━━ (d) infra_invalid matrix ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeCounter:
    """Minimal Prometheus-shaped stand-in — .labels().inc() + .inc()."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def labels(self, **labels):
        parent = self

        class _Bound:
            def inc(self_inner, amount: int = 1) -> None:
                parent.calls.append({"labels": labels, "amount": amount})

        return _Bound()

    def inc(self, amount: int = 1) -> None:
        self.calls.append({"labels": None, "amount": amount})


class TestInfraInvalidMatrix:
    async def test_ask_fn_raises_writes_infra_invalid(
        self, conn, tmp_path, monkeypatch
    ) -> None:
        _insert_version(conn, "v-raise")
        manifest, base = _build_suite(tmp_path)

        async def _boom(model: str, prompt: str) -> tuple[str, int]:
            raise RuntimeError("provider unreachable")

        failclosed = _FakeCounter()
        monkeypatch.setattr(mpe.metrics, "memory_failclosed_total", failclosed)

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-raise",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=_boom,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-x",
            now="2026-07-11T00:00:00Z",
        )
        assert outcome.decision == "infra_invalid"
        assert outcome.infra_reason == "ask_exception"
        row = conn.exec_driver_sql(
            "SELECT decision, stat_summary FROM memory_eval_runs WHERE id = ?",
            (outcome.eval_run_id,),
        ).fetchone()
        assert row[0] == "infra_invalid"
        import json as _json

        assert _json.loads(row[1])["reason"] == "ask_exception"
        assert (
            conn.exec_driver_sql(
                "SELECT count(*) FROM memory_eval_cases WHERE eval_run_id = ?",
                (outcome.eval_run_id,),
            ).scalar()
            == 0
        )
        assert any(
            c["labels"] == {"reason": "eval_infra"} for c in failclosed.calls
        )

    async def test_ask_fn_times_out_writes_infra_invalid(
        self, conn, tmp_path, monkeypatch
    ) -> None:
        import asyncio

        _insert_version(conn, "v-timeout")
        manifest, base = _build_suite(tmp_path)

        async def _slow(model: str, prompt: str) -> tuple[str, int]:
            await asyncio.sleep(0.5)  # much longer than the per-Q timeout
            return ("kw01", 1)

        failclosed = _FakeCounter()
        monkeypatch.setattr(mpe.metrics, "memory_failclosed_total", failclosed)

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-timeout",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=_slow,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-t",
            now="2026-07-11T00:00:00Z",
            per_question_timeout_s=0.02,
        )
        assert outcome.decision == "infra_invalid"
        assert outcome.infra_reason == "timeout"
        assert any(
            c["labels"] == {"reason": "eval_infra"} for c in failclosed.calls
        )

    async def test_token_budget_crossing_writes_infra_invalid(
        self, conn, tmp_path, monkeypatch
    ) -> None:
        _insert_version(conn, "v-tokens")
        manifest, base = _build_suite(tmp_path)

        ask_fn, _ = _scripted(tokens_per_call=1_000)
        failclosed = _FakeCounter()
        monkeypatch.setattr(mpe.metrics, "memory_failclosed_total", failclosed)

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-tokens",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-tb",
            now="2026-07-11T00:00:00Z",
            samples_per_case=3,
            token_budget=500,  # crossed on the first sample
        )
        assert outcome.decision == "infra_invalid"
        assert outcome.infra_reason == "token_budget"
        assert (
            conn.exec_driver_sql(
                "SELECT count(*) FROM memory_eval_cases WHERE eval_run_id = ?",
                (outcome.eval_run_id,),
            ).scalar()
            == 0
        )
        assert any(
            c["labels"] == {"reason": "eval_infra"} for c in failclosed.calls
        )

    async def test_manifest_hash_mismatch_writes_infra_invalid(
        self, conn, tmp_path, monkeypatch
    ) -> None:
        _insert_version(conn, "v-corrupt")
        manifest, base = _build_suite(tmp_path)
        # Corrupt the shard on disk AFTER manifest was written with the
        # legit sha → the H module raises ``hash_mismatch``.
        (base / "shard.yaml").write_bytes(
            (base / "shard.yaml").read_bytes() + b"\n# corrupted\n"
        )

        ask_fn, calls = _scripted()
        failclosed = _FakeCounter()
        monkeypatch.setattr(mpe.metrics, "memory_failclosed_total", failclosed)

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-corrupt",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-hm",
            now="2026-07-11T00:00:00Z",
        )
        assert outcome.decision == "infra_invalid"
        assert outcome.infra_reason.startswith("hash_mismatch:")
        # No ask_fn calls happened because the load raised.
        assert calls["total"] == 0
        # suite_sha256 unknown on this path → EvalOutcome empty; row NULL.
        assert outcome.suite_sha256 == ""
        row = conn.exec_driver_sql(
            "SELECT suite_sha256 FROM memory_eval_runs WHERE id = ?",
            (outcome.eval_run_id,),
        ).fetchone()
        assert row[0] is None
        assert any(
            c["labels"] == {"reason": "eval_infra"} for c in failclosed.calls
        )

    async def test_none_client_via_real_build_ask_fn(
        self, conn, tmp_path, monkeypatch
    ) -> None:
        """The one path that exercises the REAL ``build_ask_fn`` — with
        ``get_llm`` monkeypatched to return None (its documented no-client
        outcome). The executor maps ``ask_fn is None`` → ``no_client``."""
        _insert_version(conn, "v-noclient")
        manifest, base = _build_suite(tmp_path)

        from backend.agents import llm as llm_mod

        def _no_client(**kwargs):
            # allow_failover=False path — the executor never walks the chain.
            assert kwargs.get("allow_failover") is False
            return None

        monkeypatch.setattr(llm_mod, "get_llm", _no_client)
        ask_fn = build_ask_fn(_client())
        assert ask_fn is None

        failclosed = _FakeCounter()
        monkeypatch.setattr(mpe.metrics, "memory_failclosed_total", failclosed)

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-noclient",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-nc",
            now="2026-07-11T00:00:00Z",
        )
        assert outcome.decision == "infra_invalid"
        assert outcome.infra_reason == "no_client"
        # No case rows, and suite_sha256 empty (loader never even reached).
        assert outcome.suite_sha256 == ""
        assert (
            conn.exec_driver_sql(
                "SELECT count(*) FROM memory_eval_cases WHERE eval_run_id = ?",
                (outcome.eval_run_id,),
            ).scalar()
            == 0
        )
        assert any(
            c["labels"] == {"reason": "eval_infra"} for c in failclosed.calls
        )


# ━━ (e) neg-control enforcement ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNegControlEnforcement:
    async def test_candidate_pass_on_flagged_forces_reject_and_counts(
        self, conn, tmp_path, monkeypatch
    ) -> None:
        _insert_version(conn, "v-neg-catch")
        manifest, base = _build_suite(
            tmp_path, n=10, min_neg_controls=1, neg_ids=("q05",)
        )
        # Otherwise-promoting scores; candidate ALSO passes the neg-control q05.
        cand = {f"q{i:02d}": True for i in range(1, 10)}
        cand["q05"] = True
        ask_fn, _ = _scripted(candidate_passes=cand)

        catch = _FakeCounter()
        monkeypatch.setattr(
            mpe.metrics, "memory_neg_control_catch_total", catch
        )

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-neg-catch",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-nc1",
            now="2026-07-11T00:00:00Z",
        )
        assert outcome.decision == "reject"
        # One catch on q05.
        assert catch.calls == [{"labels": None, "amount": 1}]

    async def test_candidate_fails_flagged_leaves_normal_stats(
        self, conn, tmp_path, monkeypatch
    ) -> None:
        _insert_version(conn, "v-neg-clean")
        manifest, base = _build_suite(
            tmp_path, n=10, min_neg_controls=1, neg_ids=("q05",)
        )
        # Candidate passes q01-q09 but FAILS the neg-control q05.
        cand = {f"q{i:02d}": True for i in range(1, 10)}
        cand["q05"] = False
        ask_fn, _ = _scripted(candidate_passes=cand)

        catch = _FakeCounter()
        monkeypatch.setattr(
            mpe.metrics, "memory_neg_control_catch_total", catch
        )

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-neg-clean",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-nc2",
            now="2026-07-11T00:00:00Z",
        )
        # Candidate wins 8-of-10 discordant; stats flow decides.
        assert outcome.decision == "promote"
        assert catch.calls == []


# ━━ (f) baseline cache ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBaselineCache:
    async def test_second_eval_skips_baseline_arm_entirely(
        self, conn, tmp_path
    ) -> None:
        _insert_version(conn, "v-cache-1")
        _insert_version(conn, "v-cache-2")
        manifest, base = _build_suite(tmp_path)
        cand = {f"q{i:02d}": True for i in range(1, 10)}
        ask_fn, calls = _scripted(candidate_passes=cand)

        # First eval — populates the cache.
        _reset_for_tests()  # explicit: prove we start empty
        adapter = _SqliteAdapterConn(conn)
        outcome1 = await run_plan_triage_eval(
            adapter,
            version_id="v-cache-1",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-1",
            now="2026-07-11T00:00:00Z",
            samples_per_case=3,
        )
        assert outcome1.decision == "promote"
        # 10 cases * 3 samples * 2 arms = 60 calls
        first_total = calls["total"]
        assert first_total == 60
        assert calls["baseline"] == 30
        assert calls["candidate"] == 30

        # Second eval — SAME (suite, client, samples_per_case) → cache hit.
        outcome2 = await run_plan_triage_eval(
            adapter,
            version_id="v-cache-2",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-2",
            now="2026-07-11T00:00:00Z",
            samples_per_case=3,
        )
        assert outcome2.decision == "promote"
        # Only 30 additional candidate-arm calls; baseline was skipped.
        assert calls["candidate"] == 60
        assert calls["baseline"] == 30  # unchanged
        assert calls["total"] == first_total + 30

    async def test_different_model_fingerprint_misses_cache(
        self, conn, tmp_path
    ) -> None:
        _insert_version(conn, "v-cache-m1")
        _insert_version(conn, "v-cache-m2")
        manifest, base = _build_suite(tmp_path)
        cand = {f"q{i:02d}": True for i in range(1, 10)}
        ask_fn, calls = _scripted(candidate_passes=cand)

        adapter = _SqliteAdapterConn(conn)
        await run_plan_triage_eval(
            adapter,
            version_id="v-cache-m1",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(temperature=0.0),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-m1",
            now="2026-07-11T00:00:00Z",
            samples_per_case=3,
        )
        first_baseline = calls["baseline"]
        # Different fingerprint (temp changes) → different model_fp → miss.
        await run_plan_triage_eval(
            adapter,
            version_id="v-cache-m2",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=ask_fn,
            client=_client(temperature=0.7),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-m2",
            now="2026-07-11T00:00:00Z",
            samples_per_case=3,
        )
        # Baseline arm was called AGAIN — cache missed.
        assert calls["baseline"] == first_baseline + 30


# ━━ (g) fingerprints ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFingerprints:
    def test_model_fingerprint_deterministic_64_hex(self) -> None:
        c = _client(temperature=0.3)
        v1 = model_fingerprint(c)
        v2 = model_fingerprint(c)
        assert v1 == v2
        assert len(v1) == 64
        assert all(ch in "0123456789abcdef" for ch in v1)

    def test_model_fingerprint_sensitive_to_temperature(self) -> None:
        a = model_fingerprint(_client(temperature=0.0))
        b = model_fingerprint(_client(temperature=0.7))
        assert a != b

    def test_prompt_assembly_fingerprint_template_version_sensitive(
        self,
    ) -> None:
        base = prompt_assembly_fingerprint(
            suite_sha256="a" * 64,
            candidate_rendered_sha256="b" * 64,
            template_version=TEMPLATE_VERSION,
        )
        other = prompt_assembly_fingerprint(
            suite_sha256="a" * 64,
            candidate_rendered_sha256="b" * 64,
            template_version="u4fe2",
        )
        assert base != other
        assert len(base) == 64
        assert all(ch in "0123456789abcdef" for ch in base)

    def test_prompt_assembly_fingerprint_deterministic(self) -> None:
        one = prompt_assembly_fingerprint(
            suite_sha256="a" * 64,
            candidate_rendered_sha256="b" * 64,
            template_version=TEMPLATE_VERSION,
        )
        two = prompt_assembly_fingerprint(
            suite_sha256="a" * 64,
            candidate_rendered_sha256="b" * 64,
            template_version=TEMPLATE_VERSION,
        )
        assert one == two


# ━━ (h) arm-prompt purity ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestArmPromptPurity:
    def test_baseline_prompt_has_no_candidate_bytes(self) -> None:
        p = _baseline_prompt("what is the answer?")
        assert CAND_RENDERED_BYTES not in p

    def test_candidate_prompt_embeds_rendered_payload_verbatim(self) -> None:
        p = _candidate_prompt("what is the answer?", CAND_RENDERED_BYTES)
        # Exact byte substring (never a re-render).
        assert CAND_RENDERED_BYTES in p
        # Fence line present (audit B2 shape check).
        assert "===== item start =====" in p
        assert "===== item end =====" in p

    def test_baseline_and_candidate_differ_only_in_item_slot(self) -> None:
        b = _baseline_prompt("q?")
        c = _candidate_prompt("q?", CAND_RENDERED_BYTES)
        assert b != c
        # Diff is exactly the item bytes inserted between the fences.
        assert b.replace("{item}", "") == b
        # Removing the item bytes from the candidate yields the baseline.
        assert c.replace(CAND_RENDERED_BYTES, "") == b


# ━━ (i) dormant + module-source string bans ━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDormantShip:
    def test_no_nontest_module_references_the_new_module(self) -> None:
        # Membership form (F-math precedent): the only backend module
        # allowed to contain the sentinel string is this file itself.
        # β-2/β-3a: the dormant premise ended — the scheduler is the WIRED
        # eval driver and metrics carries its label docs.
        own = {"memory_promotion_eval.py",
               "metrics.py", "agents/memory_promotion_scheduler.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if str(rel) in own or (len(parts) == 1 and parts[0] in own):
                continue
            if "memory_promotion_eval" in py.read_text(errors="ignore"):
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"


class TestModuleSourceBans:
    def test_no_sibling_module_names_except_memory_eval_stats(self) -> None:
        # The forward-design string sweep bans A2/D/C1/loader module
        # references — "learned_item" as a substring catches them all.
        # ``memory_eval_stats`` (F-math) is the ONE permitted sibling.
        src = MODULE_PATH.read_text()
        assert "learned_item" not in src
        assert "_SKILLS_LIVE" not in src
        assert "memory_eval_stats" in src  # positive: F-math IS imported

    def test_no_clock_reads(self) -> None:
        # ``now`` is caller-supplied for determinism — the module must not
        # READ a clock. It MAY import datetime to COERCE that caller string
        # into a tz-aware value for the ``ran_at`` timestamptz param
        # (asyncpg rejects a bare string; parsing is not a clock read).
        src = MODULE_PATH.read_text()
        assert "datetime.now" not in src
        assert "datetime.utcnow" not in src
        assert "time.time" not in src
        assert "time.monotonic" not in src
        # ``import time`` is banned; ``asyncio.wait_for`` supplies the
        # per-Q timeout without a raw clock read.
        for line in src.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("import time")
            assert not stripped.startswith("from time ")

    def test_no_pool_import(self) -> None:
        src = MODULE_PATH.read_text()
        assert "backend.db_pool" not in src
        assert "from backend import db_pool" not in src


# ━━ (j) sample math — samples_per_case=3 flip-flop collapse ━━━━━━━━━━


class TestSampleMath:
    async def test_flip_flopping_scripted_fn_collapses_by_majority(
        self, conn, tmp_path
    ) -> None:
        """samples_per_case=3 with a flip-flopping candidate arm.

        The scripted fn alternates pass/fail per call (independently of
        the question) so each case sees a (T,F,T) or similar sequence.
        Per-side majority collapse means the collapsed candidate side is
        the majority of the 3 samples — which the F-math stats then
        pair with the collapsed baseline.
        """
        _insert_version(conn, "v-samples")
        manifest, base = _build_suite(tmp_path)

        cand_counts: dict[str, int] = {}

        async def _flip(model: str, prompt: str) -> tuple[str, int]:
            m = re.search(r"case (\d+)", prompt)
            qid = f"q{int(m.group(1)):02d}" if m else "q00"
            if CAND_RENDERED_BYTES in prompt:
                idx = cand_counts.get(qid, 0)
                cand_counts[qid] = idx + 1
                # Pattern per Q: True, False, True → majority True.
                passes = (idx % 3) != 1
                return ("kw" + qid[1:] if passes else "no", 10)
            return ("no", 10)  # baseline fails everywhere

        outcome = await run_plan_triage_eval(
            _SqliteAdapterConn(conn),
            version_id="v-samples",
            rendered_payload=CAND_RENDERED_BYTES,
            rendered_payload_sha256=CAND_RENDERED_SHA,
            ask_fn=_flip,
            client=_client(),
            manifest_path=manifest,
            base_dir=base,
            live_set_hash="lsh-j",
            now="2026-07-11T00:00:00Z",
            samples_per_case=3,
        )
        # 10 cases * majority(True) candidate, majority(False) baseline
        # → 10 improvements, 0 regressions → promote.
        assert outcome.decision == "promote"
        assert outcome.n == 10
        assert outcome.net_flips == 10

        # And 10 collapsed case rows landed on the case table (one per Q).
        rows = conn.exec_driver_sql(
            "SELECT baseline_pass, candidate_pass FROM memory_eval_cases "
            "WHERE eval_run_id = ?",
            (outcome.eval_run_id,),
        ).fetchall()
        assert len(rows) == 10
        # Every row should be (baseline=False, candidate=True) — the
        # per-side majority of (F,F,F) and (T,F,T).
        for base_pass, cand_pass in rows:
            assert bool(base_pass) is False
            assert bool(cand_pass) is True
