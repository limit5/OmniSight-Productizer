"""OP-1587 [RT-10c] -- standalone concurrent-promote race test (tests only).

RT-10a (``backend.agents.release_train``) shipped the CAS single-winner
primitive and a 2-way smoke test. This module is the dedicated race test
the RT-10c story calls for: it asserts the **CAS single-winner**
invariant under genuine cooperative interleaving and at a range of
concurrency degrees, and -- crucially -- that the *losers* perform no
side effects (no audit insert, no tag write), which the RT-10a smoke
test does not check.

No CAS implementation changes here (RT-10c is tests only); the asserted
contract is RT-10a's :func:`backend.agents.release_train.promote`.

Modelling the Postgres CAS faithfully
-------------------------------------
The single-winner guarantee in production rests on Postgres serializing
the conditional ``UPDATE ... WHERE promotion_state = 'pending'`` via
row-level locking: of two concurrent updates, the second observes the
first's committed state and matches zero rows. The fake connection here
reproduces exactly that: every ``fetchrow``/``execute`` *yields the event
loop on entry* (``await asyncio.sleep(0)``) to force the racing promote
coroutines to interleave, then performs its read-modify-write
**run-to-completion with no further await** -- so each conditional UPDATE
is atomic with respect to the other cooperatively-scheduled coroutines,
just as a row lock makes it atomic with respect to other transactions.

If :func:`promote` failed to treat a zero-row CAS UPDATE as a lost race,
these tests would see more than one :class:`PromoteResult`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from backend.agents.release_train import (
    AuditWriteError,
    PromoteRaceLost,
    PromoteResult,
    UnknownTrain,
    create_train,
    get_train,
    promote,
)


SHA = "ab" * 20  # 40-hex
DIGEST_BE = "sha256:" + "1" * 64
DIGEST_FE = "sha256:" + "2" * 64


class _Row(dict[str, Any]):
    pass


class _InterleavingConn:
    """asyncpg-shaped fake whose conditional UPDATE is atomic-under-interleave.

    Each call yields the event loop on entry (``await asyncio.sleep(0)``)
    so concurrently-gathered promote coroutines genuinely interleave, then
    runs to completion without a further await -- modelling Postgres
    row-lock serialization of the CAS UPDATE. Counters record every audit
    insert and tag write the *store* performs so the test can prove the
    losers had no side effects.
    """

    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}
        # Observability: how many times the acquiring CAS UPDATE matched a
        # pending row (i.e. how many coroutines won the flip).
        self.cas_acquired = 0

    async def fetchrow(self, sql: str, *args: Any) -> _Row | None:
        await asyncio.sleep(0)  # force interleave; the body below is atomic
        if "INSERT INTO release_train" in sql:
            candidate_sha, be, fe, reserved, actor = args
            row = _Row(
                candidate_sha=candidate_sha,
                source_digest_backend=be,
                source_digest_frontend=fe,
                reserved_version=reserved,
                promotion_state="pending",
                actor=actor,
                row_version=0,
                final_digest_equality=None,
                promotion_audit_id=None,
            )
            self.rows[candidate_sha] = row
            return _Row(row)
        if "SELECT 1 FROM release_train" in sql:
            return _Row(one=1) if args[0] in self.rows else None
        if sql.strip().startswith("SELECT"):
            row = self.rows.get(args[0])
            return _Row(row) if row is not None else None
        if "SET promotion_state = 'promoting'" in sql:
            sha, actor = args
            row = self.rows.get(sha)
            if row is None or row["promotion_state"] != "pending":
                return None
            row["promotion_state"] = "promoting"
            row["actor"] = actor
            row["row_version"] += 1
            self.cas_acquired += 1
            return _Row(row)
        if "SET promotion_state = 'promoted'" in sql:
            sha, equality, audit_id = args
            row = self.rows.get(sha)
            if row is None or row["promotion_state"] != "promoting":
                return None
            row["promotion_state"] = "promoted"
            row["final_digest_equality"] = equality
            row["promotion_audit_id"] = audit_id
            row["row_version"] += 1
            return _Row(row)
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> None:
        await asyncio.sleep(0)
        if "SET promotion_state = 'failed'" in sql:
            row = self.rows.get(args[0])
            if row is not None and row["promotion_state"] == "promoting":
                row["promotion_state"] = "failed"
                row["row_version"] += 1
            return None
        raise AssertionError(f"unexpected execute SQL: {sql}")


def _factory(conn: _InterleavingConn):
    @asynccontextmanager
    async def _cm() -> AsyncIterator[_InterleavingConn]:
        yield conn

    return _cm


class _SideEffects:
    """Counts audit inserts and tag writes, tagging each by actor.

    Lets a test assert that *exactly one* promote (the winner) reached the
    audit gate and the tag write, and that the losers reached neither.
    """

    def __init__(self) -> None:
        self.audit_actors: list[str] = []
        self.tag_actors: list[str] = []
        self._next_audit_id = 5000

    def audit_log(self):
        async def _log(*_a: Any, actor: str = "", **_k: Any) -> int:
            await asyncio.sleep(0)
            self.audit_actors.append(actor)
            self._next_audit_id += 1
            return self._next_audit_id

        return _log

    def tag_writer(self):
        async def _write(train) -> bool:
            await asyncio.sleep(0)
            self.tag_actors.append(train.actor)
            return True

        return _write


async def _seed(conn: _InterleavingConn) -> None:
    await create_train(
        SHA, DIGEST_BE, DIGEST_FE, actor="creator", conn_factory=_factory(conn)
    )


# -- AC: race test asserts CAS single-winner --------------------------------


@pytest.mark.parametrize("degree", [2, 3, 8, 32])
async def test_n_concurrent_promotes_exactly_one_winner(degree: int) -> None:
    """N promotes of one candidate -> 1 PromoteResult, N-1 PromoteRaceLost.

    The losers are *only* PromoteRaceLost -- never UnknownTrain (the row
    exists) and never AuditWriteError (they never reach the audit gate).
    """

    conn = _InterleavingConn()
    await _seed(conn)
    fx = _SideEffects()

    async def attempt(i: int):
        return await promote(
            SHA,
            f"rm-{i}",
            tag_writer=fx.tag_writer(),
            audit_log=fx.audit_log(),
            conn_factory=_factory(conn),
        )

    results = await asyncio.gather(
        *(attempt(i) for i in range(degree)), return_exceptions=True
    )

    wins = [r for r in results if isinstance(r, PromoteResult)]
    losses = [r for r in results if isinstance(r, PromoteRaceLost)]
    assert len(wins) == 1, results
    assert len(losses) == degree - 1, results
    # No loser misclassified as "absent train" or as an audit failure.
    assert not any(isinstance(r, (UnknownTrain, AuditWriteError)) for r in results)

    # The CAS flip fired exactly once across all racers.
    assert conn.cas_acquired == 1

    # Only the single winner performed side effects: one audit insert, one
    # tag write, both by the same actor; the losers did neither.
    assert len(fx.audit_actors) == 1
    assert len(fx.tag_actors) == 1
    winner = wins[0]
    assert fx.audit_actors == fx.tag_actors

    # Durable end state: promoted, carrying the winner's audit id, and the
    # row advanced pending(0) -> promoting(1) -> promoted(2) exactly once.
    row = await get_train(SHA, conn_factory=_factory(conn))
    assert row is not None
    assert row.promotion_state == "promoted"
    assert row.promotion_audit_id == winner.promotion_audit_id
    assert row.row_version == 2
    assert row.actor == fx.tag_actors[0]


async def test_losing_racers_perform_no_audit_or_tag_side_effect() -> None:
    """Sharper restatement of the loser contract at high contention."""

    conn = _InterleavingConn()
    await _seed(conn)
    fx = _SideEffects()

    async def attempt(i: int):
        return await promote(
            SHA,
            f"rm-{i}",
            tag_writer=fx.tag_writer(),
            audit_log=fx.audit_log(),
            conn_factory=_factory(conn),
        )

    results = await asyncio.gather(
        *(attempt(i) for i in range(16)), return_exceptions=True
    )
    losers = [r for r in results if isinstance(r, PromoteRaceLost)]
    assert len(losers) == 15
    # 16 racers, but the audit gate and tag write each ran exactly once.
    assert len(fx.audit_actors) == 1
    assert len(fx.tag_actors) == 1


async def test_single_winner_holds_even_when_winner_aborts_at_audit_gate() -> None:
    """If the winner's audit hard-gate fails, no loser is promoted in its place.

    The CAS flip still happens exactly once; the winner aborts to
    ``failed`` (AuditWriteError) before any tag write, and every other
    racer loses with :class:`PromoteRaceLost`. The single-winner property
    is about who flips the row, not about the promote succeeding.
    """

    conn = _InterleavingConn()
    await _seed(conn)
    tag_calls: list[str] = []

    async def tag_writer(train) -> bool:
        tag_calls.append(train.candidate_sha)
        return True

    async def audit_fails(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0)
        return None  # hard-gate failure for whichever racer wins the CAS

    async def attempt(i: int):
        return await promote(
            SHA,
            f"rm-{i}",
            tag_writer=tag_writer,
            audit_log=audit_fails,
            conn_factory=_factory(conn),
        )

    results = await asyncio.gather(
        *(attempt(i) for i in range(8)), return_exceptions=True
    )

    audit_errors = [r for r in results if isinstance(r, AuditWriteError)]
    losses = [r for r in results if isinstance(r, PromoteRaceLost)]
    assert len(audit_errors) == 1, results  # exactly one racer reached the gate
    assert len(losses) == 7, results
    assert conn.cas_acquired == 1
    # Audit hard gate means the tag write never ran for anyone.
    assert tag_calls == []
    # The train is durably failed -- never promoted by a losing racer.
    row = await get_train(SHA, conn_factory=_factory(conn))
    assert row is not None
    assert row.promotion_state == "failed"
    assert row.promotion_audit_id is None


async def test_distinct_candidates_do_not_contend() -> None:
    """Concurrent promotes of *different* candidates each win independently.

    Single-winner is per-candidate (the CAS keys on candidate_sha), so two
    distinct trains promoted concurrently both succeed -- the invariant is
    not a global mutex.
    """

    conn = _InterleavingConn()
    sha2 = "cd" * 20
    await _seed(conn)
    await create_train(
        sha2, DIGEST_BE, DIGEST_FE, actor="creator", conn_factory=_factory(conn)
    )

    async def audit_ok(*_a: Any, **_k: Any) -> int:
        await asyncio.sleep(0)
        return 1

    async def attempt(sha: str):
        return await promote(
            sha, "rm", audit_log=audit_ok, conn_factory=_factory(conn)
        )

    results = await asyncio.gather(attempt(SHA), attempt(sha2))
    assert all(isinstance(r, PromoteResult) for r in results)
    assert {r.candidate_sha for r in results} == {SHA, sha2}
