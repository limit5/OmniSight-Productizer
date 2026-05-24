"""[OP-1572] RT-04e — green-signal integration verifier (= activation evidence).

This is the integration verifier that ties the green-signal pieces landed by
RT-04a..d into a single end-to-end narrative for one develop change. It is
*integration only* — no feature code: every behaviour it asserts is
implemented (or owned) elsewhere; this file proves the seams hold when the
real artifacts are driven together.

The green signal has two halves, and the RT-04e AC names both:

1. **green-by-SHA** — the gate→store→selector seam.
   * RT-04a (CI, ``.gitlab-ci.yml``) runs a per-change fast gate keyed by the
     submitted 40-hex ``CI_COMMIT_SHA``.
   * RT-04b (``backend.agents.green_evidence`` + alembic ``0246``) persists one
     pass/fail row per *full* SHA and answers the fail-closed gate predicate
     ``green_status(sha)``.
   * The candidate-build trigger (RT-09, not yet landed) selects the
     **last-certified** green SHA — the migration documents this as the
     ``(status, recorded_at DESC)`` index hot path. We drive that exact
     selector query against the *real* migration schema to prove the store
     RT-04b built actually serves the selection RT-09 will make.
   * RT-04d's negative case — an unknown/failed full SHA is rejected — is
     re-exercised here against the live ``green_status`` to prove the gate
     predicate is fail-closed in the same timeline the selector reads.

2. **non-blocking develop flow** — the CI seam.
   A develop *push* runs only the per-change ``fast-gate``; the full
   candidate-certification suite (build/sign/sbom/attest/audit-emit) is
   tag-gated and never runs on a develop push. So merging to develop is not
   blocked on the full release suite. The Gerrit develop ``Verified`` channel
   (RT-04c surface) carries the fast gate's signal, distinct from that
   tag-gated certification.

One develop-SHA timeline (``_SHA_OLD_GREEN`` → ``_SHA_MID_FAIL`` →
``_SHA_NEW_GREEN``, plus an uncertified ``_SHA_UNKNOWN``) is threaded through
the store, the selector, and the full-chain test, so every assertion is
demonstrably guarding the *same* candidate timeline, not unrelated fixtures.

Cost: stdlib + pytest + PyYAML. The store helpers run in-process against a
faithful fake conn; the selector runs against an in-memory SQLite built by the
real migration; the CI/Gerrit seams parse the real checked-in files.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from backend.agents.green_evidence import (
    get_evidence,
    green_status,
    record_evidence,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"
GERRIT_CONFIG = REPO_ROOT / ".gerrit" / "project.config"
MIGRATION_0246 = BACKEND_ROOT / "alembic" / "versions" / "0246_green_evidence.py"

# ── one develop-SHA timeline, threaded through every seam below ───────────
# Distinct, real-shaped 40-hex full SHAs at ascending certification times.
_SHA_OLD_GREEN = "1111111111111111111111111111111111111111"
_SHA_MID_FAIL = "2222222222222222222222222222222222222222"
_SHA_NEW_GREEN = "3333333333333333333333333333333333333333"
_SHA_UNKNOWN = "4444444444444444444444444444444444444444"  # never recorded

_T0 = datetime(2026, 5, 22, 1, 0, 0, tzinfo=timezone.utc)
_TIMELINE = (
    (_SHA_OLD_GREEN, "pass", _T0),
    (_SHA_MID_FAIL, "fail", _T0 + timedelta(minutes=10)),
    (_SHA_NEW_GREEN, "pass", _T0 + timedelta(minutes=20)),
)


# ───────────────────────── shared real-artifact loaders ──────────────────


def _load_ci() -> dict:
    return yaml.safe_load(GITLAB_CI.read_text())


def _load_migration():
    spec = importlib.util.spec_from_file_location("_rt04e_m0246", MIGRATION_0246)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["_rt04e_m0246"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ── RT-04b store: a faithful asyncpg-shaped fake keyed on full_sha ────────
# Mirrors backend/tests/test_green_evidence.py::_FakeConn so the *real*
# record_evidence / green_status / get_evidence helpers run unchanged.


class _FakeRow(dict[str, Any]):
    pass


class _FakeConn:
    def __init__(self) -> None:
        self.rows: dict[str, _FakeRow] = {}

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        if "INSERT INTO green_evidence" in sql:
            full_sha, status, pipeline_id, evidence_url, recorded_at = args
            self.rows[full_sha] = _FakeRow(
                full_sha=full_sha,
                status=status,
                pipeline_id=pipeline_id,
                evidence_url=evidence_url,
                recorded_at=recorded_at,
            )
            return self.rows[full_sha]
        if "SELECT status" in sql or "SELECT full_sha" in sql:
            return self.rows.get(args[0])
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")


@asynccontextmanager
async def _factory(conn: _FakeConn) -> AsyncIterator[_FakeConn]:
    yield conn


async def _seed_timeline(conn: _FakeConn) -> None:
    for sha, status, at in _TIMELINE:
        await record_evidence(
            sha,
            status,
            at,
            pipeline_id=f"pipe-{sha[:4]}",
            evidence_url=f"https://ci/run/{sha[:4]}",
            conn_factory=lambda: _factory(conn),
        )


# ════════════════════ part 1 — green-by-SHA (store seam) ═════════════════


async def test_store_records_develop_timeline_by_full_sha() -> None:
    """RT-04a→RT-04b: each develop change's pass/fail is persisted per full SHA
    via the *real* record_evidence (one row per submitted SHA)."""
    conn = _FakeConn()
    await _seed_timeline(conn)

    assert set(conn.rows) == {_SHA_OLD_GREEN, _SHA_MID_FAIL, _SHA_NEW_GREEN}
    row = await get_evidence(_SHA_NEW_GREEN, conn_factory=lambda: _factory(conn))
    assert row is not None
    assert row.status == "pass"
    assert row.pipeline_id == "pipe-3333"  # the gate run that certified it


async def test_green_status_is_fail_closed_across_the_timeline() -> None:
    """The live gate predicate: green only for the certified-pass SHAs; a
    failed SHA and an unrecorded SHA are both rejected (RT-04b + RT-04d)."""
    conn = _FakeConn()
    await _seed_timeline(conn)

    assert await green_status(_SHA_OLD_GREEN, conn_factory=lambda: _factory(conn))
    assert await green_status(_SHA_NEW_GREEN, conn_factory=lambda: _factory(conn))
    # failed submit → not green
    assert not await green_status(_SHA_MID_FAIL, conn_factory=lambda: _factory(conn))
    # RT-04d: an unknown but well-formed full SHA is fail-closed even though
    # other SHAs are green.
    assert not await green_status(_SHA_UNKNOWN, conn_factory=lambda: _factory(conn))


# ══════════ part 2 — last-certified candidate selection (schema seam) ═════
# The RT-09 candidate selector reads the RT-04b store. RT-04b's migration
# documents the selection as the "(status, recorded_at DESC)" index hot path:
# "give me the most recently certified-green SHA." We run that exact query
# against the *real* migration schema to prove the store serves it.

# The selector contract, served by idx_green_evidence_status_recorded.
_SELECTOR_SQL = (
    "SELECT full_sha FROM green_evidence "
    "WHERE status = 'pass' "
    "ORDER BY recorded_at DESC "
    "LIMIT 1"
)


class _StubBind:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

        class _Dialect:
            name = "sqlite"

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


@pytest.fixture()
def green_db(monkeypatch) -> sqlite3.Connection:
    """An in-memory green_evidence built by the *real* alembic 0246 upgrade."""
    from alembic import op as alembic_op

    m0246 = _load_migration()
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(alembic_op, "get_bind", lambda: _StubBind(conn))
    m0246.upgrade()
    return conn


def _insert(conn: sqlite3.Connection, full_sha: str, status: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO green_evidence (full_sha, status, recorded_at) VALUES (?, ?, ?)",
        (full_sha, status, at.isoformat()),
    )


def test_selector_picks_last_certified_green_sha(green_db: sqlite3.Connection) -> None:
    """The candidate selector resolves the *latest* certified-green SHA — not
    an older green one, and never a failed one — over the real schema+index."""
    for sha, status, at in _TIMELINE:
        _insert(green_db, sha, status, at)

    # The index this query rides is the one the migration actually created.
    indexes = {
        r[0]
        for r in green_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_green_evidence_status_recorded" in indexes

    chosen = green_db.execute(_SELECTOR_SQL).fetchone()
    assert chosen is not None
    # newest green wins over the older green; the interleaved fail is skipped.
    assert chosen[0] == _SHA_NEW_GREEN


def test_selector_is_fail_closed_when_no_sha_is_green(
    green_db: sqlite3.Connection,
) -> None:
    """No certified-green SHA → no candidate. An empty store and an all-fail
    store both yield nothing to promote (fail-closed selection)."""
    # empty store
    assert green_db.execute(_SELECTOR_SQL).fetchone() is None

    # only failed evidence present
    _insert(green_db, _SHA_MID_FAIL, "fail", _T0)
    assert green_db.execute(_SELECTOR_SQL).fetchone() is None


# ═════════════ part 3 — non-blocking develop flow (CI seam) ═══════════════


def _extends_closure(ci: dict, name: str) -> set[str]:
    """Resolve a job's full ``extends`` closure (anchors pull in anchors)."""
    seen: set[str] = set()
    pending = [name]
    while pending:
        cur = pending.pop()
        block = ci.get(cur, {})
        ext = block.get("extends") if isinstance(block, dict) else None
        if ext is None:
            continue
        for parent in [ext] if isinstance(ext, str) else ext:
            if parent not in seen:
                seen.add(parent)
                pending.append(parent)
    return seen


def _real_jobs(ci: dict) -> dict[str, dict]:
    return {n: v for n, v in ci.items() if isinstance(v, dict) and "stage" in v}


def test_develop_push_runs_only_the_fast_gate() -> None:
    """A develop push runs the per-change fast gate and nothing else: every
    other job is tag-gated, so develop merges are NOT blocked on the full
    candidate-certification suite."""
    ci = _load_ci()

    # The develop-push lane exists in the workflow rules.
    assert {
        "if": '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == "develop"'
    } in ci["workflow"]["rules"]

    jobs = _real_jobs(ci)
    develop_jobs, cand_jobs = set(), set()
    for name in jobs:
        closure = _extends_closure(ci, name)
        if ".fast_gate_rules" in closure:
            develop_jobs.add(name)
        if ".candidate_rules" in closure:
            cand_jobs.add(name)

    # exactly the fast gate runs on a develop push …
    assert develop_jobs == {"fast-gate"}
    # … and the entire certification suite is candidate-gated (ADR-0040 RT-20: no v* git tag).
    assert cand_jobs == set(jobs) - {"fast-gate"}
    assert {"candidate-build-image", "candidate-sign-image", "candidate-attest-image"} <= cand_jobs


def test_fast_gate_keyed_by_same_full_sha_and_is_not_certification() -> None:
    """The CI seam meets the store seam on the same key: the fast gate is keyed
    by the 40-hex full SHA the green-evidence store keys on, and it does NOT
    run candidate certification (that's RT-09, tag-gated)."""
    ci = _load_ci()
    job = ci["fast-gate"]
    flat = "\n".join(
        line
        for key in ("before_script", "script")
        for line in (job.get(key) or [])
        if isinstance(line, str)
    )

    # keyed by the full 40-char SHA — the same key green_evidence stores by.
    assert 'test "${#CI_COMMIT_SHA}" = "40"' in flat
    assert job["artifacts"]["name"] == "fast-gate-${CI_COMMIT_SHA}"
    # per-change gate, not candidate certification.
    assert "CANDIDATE_SHA" not in flat
    assert "green_status" not in flat


# ════════ part 4 — develop Verified submit signal (Gerrit surface) ════════


def _develop_access_block(text: str) -> str:
    m = re.search(
        r'\[access "refs/heads/develop"\](.*?)(?=\n\[|\Z)', text, re.DOTALL
    )
    assert m, "no [access \"refs/heads/develop\"] block found"
    return m.group(1)


def _verified_sr_block(text: str) -> str:
    m = re.search(
        r'\[submit-requirement "Verified"\](.*?)(?=\n\[|\Z)', text, re.DOTALL
    )
    assert m, "no [submit-requirement \"Verified\"] block found"
    return m.group(1)


def test_develop_verified_channel_carries_the_fast_gate_signal() -> None:
    """RT-04c surface: develop's Verified channel is wired for the per-change
    CI signal (ci-bot reports Verified), and the Verified submit-requirement
    keys submittability on that signal — distinct from tag-gated certification.

    NOTE: flipping the SR from ``applicableIf = is:false`` to enforced is the
    RT-04c operator/Gerrit action (area:gerrit, out of scope for this
    tests-area verifier). This asserts the wiring is in place and points the
    right way; it does not assert the enforcement flip.
    """
    text = GERRIT_CONFIG.read_text()

    # ci-bot may report green on develop via Verified (the fast gate's channel).
    assert "label-Verified = -1..+1 group ci-bot" in _develop_access_block(text)

    # When active, develop submit requires the Verified+1 signal.
    sr = _verified_sr_block(text)
    assert "submittableIf = label:Verified=+1" in sr
    # documented as still-OFF pending RT-04c — recorded, not asserted-active.
    assert "applicableIf = is:false" in sr


# ═══════════════ part 5 — full chain: one develop change e2e ══════════════


async def test_green_signal_chain_for_one_develop_change(
    green_db: sqlite3.Connection,
) -> None:
    """Activation evidence: a develop change at _SHA_NEW_GREEN passes the
    per-change fast gate (keyed by its full SHA), is recorded green in the
    store, is selectable as the last-certified candidate — all while the full
    release suite never ran on that develop push. The seams compose end to end.
    """
    # 1. green-by-SHA — record the timeline through the real store, gate green.
    conn = _FakeConn()
    await _seed_timeline(conn)
    assert await green_status(_SHA_NEW_GREEN, conn_factory=lambda: _factory(conn))

    # 2. last-certified selection over the real schema picks that same SHA.
    for sha, status, at in _TIMELINE:
        _insert(green_db, sha, status, at)
    chosen = green_db.execute(_SELECTOR_SQL).fetchone()
    assert chosen is not None and chosen[0] == _SHA_NEW_GREEN

    # 3. non-blocking develop flow — the develop push that produced this SHA
    #    ran only the fast gate; the full certification suite is tag-gated.
    ci = _load_ci()
    jobs = _real_jobs(ci)
    develop_jobs = {
        n for n in jobs if ".fast_gate_rules" in _extends_closure(ci, n)
    }
    assert develop_jobs == {"fast-gate"}
