"""OP-1106 / v2-Ⅹ-1bc — Tests for backend.agents.runner_coordination.

Exercises the 4-function API (acquire / release / record_phase /
find_active_holders) plus the race-condition AC requirement (4
concurrent acquires, exactly 1 wins).

Schema bootstrap is done inline via raw SQL (not by running alembic)
so the test isolates the module behaviour from the migration runner.
The schema mirrors ``backend/alembic/versions/0234_runner_claims.py``
exactly — if the migration changes, this fixture must change too.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from backend.agents import runner_coordination as rc


_SCHEMA = """
CREATE TABLE runner_claims (
    lease_id            TEXT PRIMARY KEY,
    ticket_key          TEXT NOT NULL,
    resource_key        TEXT NOT NULL,
    owner_agent_class   TEXT NOT NULL,
    owner_instance_id   TEXT NOT NULL,
    fencing_token       TEXT NOT NULL UNIQUE,
    state               TEXT NOT NULL DEFAULT 'active',
    phase               TEXT NOT NULL DEFAULT 'pickup',
    heartbeat_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acquired_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at         TEXT,
    release_reason      TEXT,
    external_refs       TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT runner_claims_state_check
        CHECK (state IN ('active', 'released'))
);
CREATE UNIQUE INDEX uq_runner_claims_resource_active
    ON runner_claims (resource_key) WHERE state = 'active';
CREATE INDEX idx_runner_claims_ticket
    ON runner_claims (ticket_key, state);
"""


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "test_runner_claims.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(p))
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return p


# ── Code AC #1+2+3: 4 functions + fencing-token format ────────────────


def test_acquire_returns_lease_with_expected_format(db_path: Path):
    lease = rc.acquire_claim(
        ticket_key="OP-test-1",
        resource_key="mutex:test",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    assert lease.state == "active"
    assert lease.phase == "pickup"
    assert lease.ticket_key == "OP-test-1"
    assert lease.resource_key == "mutex:test"
    # Code AC #3: fencing token format claim:{instance}:{epoch_us}-{uuid}
    assert lease.fencing_token.startswith("claim:claude-1:")
    assert lease.released_at is None
    assert lease.release_reason is None


def test_release_marks_state_released(db_path: Path):
    lease = rc.acquire_claim(
        ticket_key="OP-test-2",
        resource_key="mutex:rel",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    rc.release_claim(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        release_reason="ok",
    )
    holders = rc.find_active_holders(resource_keys=["mutex:rel"])
    assert holders == []


def test_record_phase_updates_phase_and_heartbeat(db_path: Path):
    lease = rc.acquire_claim(
        ticket_key="OP-test-3",
        resource_key="mutex:phase",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    initial_heartbeat = lease.heartbeat_at
    time.sleep(0.01)
    rc.record_phase(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        phase="implementing",
    )

    holders = rc.find_active_holders(resource_keys=["mutex:phase"])
    assert len(holders) == 1
    assert holders[0].phase == "implementing"
    assert holders[0].heartbeat_at != initial_heartbeat


def test_find_active_holders_filters(db_path: Path):
    rc.acquire_claim(
        ticket_key="OP-A",
        resource_key="mutex:a",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    rc.acquire_claim(
        ticket_key="OP-B",
        resource_key="mutex:b",
        owner_agent_class="subscription-codex",
        owner_instance_id="codex-1",
    )

    assert len(rc.find_active_holders()) == 2

    a_only = rc.find_active_holders(resource_keys=["mutex:a"])
    assert len(a_only) == 1
    assert a_only[0].ticket_key == "OP-A"

    excluded = rc.find_active_holders(exclude_ticket="OP-A")
    assert len(excluded) == 1
    assert excluded[0].ticket_key == "OP-B"


# ── Idempotency + error paths ─────────────────────────────────────────


def test_acquire_blocks_when_held_by_other(db_path: Path):
    rc.acquire_claim(
        ticket_key="OP-1",
        resource_key="mutex:shared",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    with pytest.raises(rc.ClaimBlocked) as excinfo:
        rc.acquire_claim(
            ticket_key="OP-2",
            resource_key="mutex:shared",
            owner_agent_class="subscription-codex",
            owner_instance_id="codex-1",
        )
    assert excinfo.value.resource_key == "mutex:shared"
    assert excinfo.value.existing_lease is not None
    assert excinfo.value.existing_lease.owner_agent_class == "subscription-claude"


def test_acquire_idempotent_same_owner_returns_existing(db_path: Path):
    lease1 = rc.acquire_claim(
        ticket_key="OP-dup",
        resource_key="mutex:dup",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    lease2 = rc.acquire_claim(
        ticket_key="OP-dup",
        resource_key="mutex:dup",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    assert lease2.lease_id == lease1.lease_id
    assert lease2.fencing_token == lease1.fencing_token


def test_release_idempotent_on_already_released(db_path: Path):
    lease = rc.acquire_claim(
        ticket_key="OP-r1",
        resource_key="mutex:r1",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    rc.release_claim(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        release_reason="first",
    )
    # No exception — second release is a no-op
    rc.release_claim(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        release_reason="second",
    )


def test_release_unknown_lease_is_noop(db_path: Path):
    rc.release_claim(
        lease_id="never-existed",
        fencing_token="claim:x:0-bogus",
        release_reason="ghost",
    )


def test_release_fencing_mismatch_raises(db_path: Path):
    lease = rc.acquire_claim(
        ticket_key="OP-f1",
        resource_key="mutex:f1",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    with pytest.raises(rc.FencingTokenMismatch):
        rc.release_claim(
            lease_id=lease.lease_id,
            fencing_token="claim:wrong:0-x",
            release_reason="bad",
        )


def test_record_phase_raises_when_no_active_claim(db_path: Path):
    with pytest.raises(rc.ClaimNotFound):
        rc.record_phase(
            lease_id="ghost",
            fencing_token="claim:x:0-y",
            phase="x",
        )


def test_record_phase_fencing_mismatch_raises(db_path: Path):
    lease = rc.acquire_claim(
        ticket_key="OP-p1",
        resource_key="mutex:p1",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    with pytest.raises(rc.FencingTokenMismatch):
        rc.record_phase(
            lease_id=lease.lease_id,
            fencing_token="claim:wrong:0-x",
            phase="x",
        )


# ── Exercised AC: race test ───────────────────────────────────────────


def test_race_4_concurrent_acquires_exactly_one_wins(db_path: Path):
    """Exercised AC: 4 concurrent acquire_claim() calls; exactly 1 wins,
    others get ClaimBlocked."""
    winners: list[rc.ClaimLease] = []
    blocked_count = [0]
    lock = threading.Lock()
    start_barrier = threading.Barrier(4)

    def worker(idx: int) -> None:
        start_barrier.wait()
        try:
            lease = rc.acquire_claim(
                ticket_key=f"OP-race-{idx}",
                resource_key="mutex:race",
                owner_agent_class="subscription-claude",
                owner_instance_id=f"claude-{idx}",
            )
            with lock:
                winners.append(lease)
        except rc.ClaimBlocked:
            with lock:
                blocked_count[0] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}: {winners}"
    assert blocked_count[0] == 3, f"expected 3 blocked, got {blocked_count[0]}"


def test_race_2_concurrent_acquires_one_wins(db_path: Path):
    """Smaller race test — 2 concurrent, 1 wins. Matches Code AC #4
    "race-condition tests for 2 concurrent acquires"."""
    winners: list[rc.ClaimLease] = []
    blocked = [0]
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            lease = rc.acquire_claim(
                ticket_key=f"OP-r2-{idx}",
                resource_key="mutex:r2",
                owner_agent_class="subscription-claude",
                owner_instance_id=f"claude-{idx}",
            )
            with lock:
                winners.append(lease)
        except rc.ClaimBlocked:
            with lock:
                blocked[0] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(winners) == 1
    assert blocked[0] == 1


# ── Integration AC: shadow-mode write path round-trip ─────────────────


def test_integration_shadow_write_round_trip(db_path: Path):
    """Integration AC: shadow-mode write path exercised — runner can
    write claim row + heartbeat + release, end to end."""
    lease = rc.acquire_claim(
        ticket_key="OP-integ",
        resource_key="mutex:integ",
        owner_agent_class="subscription-codex",
        owner_instance_id="codex-1",
        external_refs={"worktree": "/tmp/op-integ-wt", "change_id": "I1234"},
    )

    # heartbeat tick
    rc.record_phase(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        phase="implementing",
    )

    # active visible
    found = rc.find_active_holders(resource_keys=["mutex:integ"])
    assert len(found) == 1
    assert found[0].external_refs == {"worktree": "/tmp/op-integ-wt", "change_id": "I1234"}
    assert found[0].phase == "implementing"

    # release
    rc.release_claim(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        release_reason="success",
    )

    # active gone
    assert rc.find_active_holders(resource_keys=["mutex:integ"]) == []

    # but row preserved in released state — audit trail
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT state, release_reason FROM runner_claims WHERE lease_id = ?",
        (lease.lease_id,),
    ).fetchone()
    conn.close()
    assert row == ("released", "success")


# ── OP-1109: heartbeat TTL expiry (Integration AC) ────────────────────


def _backdate_heartbeat(db_path, lease_id: str, age_seconds: int) -> None:
    """Helper: rewind ``heartbeat_at`` of an active claim so it appears
    older than ``age_seconds``. Lets tests cover the TTL-expiry path
    without sleeping."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    backdated = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE runner_claims SET heartbeat_at = ? WHERE lease_id = ?",
        (backdated, lease_id),
    )
    conn.commit()
    conn.close()


def test_expire_stale_active_claims_marks_stale_as_released(db_path):
    lease = rc.acquire_claim(
        ticket_key="OP-stale-1",
        resource_key="mutex:stale1",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    # Backdate so heartbeat is 10 min old (older than default 5-min TTL)
    _backdate_heartbeat(db_path, lease.lease_id, age_seconds=600)

    expired = rc.expire_stale_active_claims(max_age_seconds=300)
    assert expired == 1

    # The lease is now released with reason 'ttl-expired'
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT state, release_reason FROM runner_claims WHERE lease_id = ?",
        (lease.lease_id,),
    ).fetchone()
    conn.close()
    assert row == ("released", "ttl-expired")
    # And the resource is now claimable again
    assert rc.find_active_holders(resource_keys=["mutex:stale1"]) == []


def test_expire_stale_active_claims_leaves_fresh_claims_alone(db_path):
    """A claim whose heartbeat is recent must not be expired."""
    lease = rc.acquire_claim(
        ticket_key="OP-fresh-1",
        resource_key="mutex:fresh1",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    # heartbeat_at is now-ish; 5-min TTL must NOT expire it
    expired = rc.expire_stale_active_claims(max_age_seconds=300)
    assert expired == 0
    holders = rc.find_active_holders(resource_keys=["mutex:fresh1"])
    assert len(holders) == 1
    assert holders[0].lease_id == lease.lease_id


def test_expire_stale_active_claims_record_phase_refreshes_heartbeat(db_path):
    """Calling record_phase() must reset the heartbeat clock so a
    long-running but still-alive claim is not collected as stale."""
    lease = rc.acquire_claim(
        ticket_key="OP-long-1",
        resource_key="mutex:long1",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    # Pretend 10 min have passed
    _backdate_heartbeat(db_path, lease.lease_id, age_seconds=600)
    # Worker checks in — should reset the clock
    rc.record_phase(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        phase="implementing",
    )
    expired = rc.expire_stale_active_claims(max_age_seconds=300)
    assert expired == 0, "record_phase should have refreshed the heartbeat"


def test_expire_stale_active_claims_idempotent(db_path):
    """A second sweep with the same threshold must be a no-op once the
    stale rows are already in 'released' state."""
    lease = rc.acquire_claim(
        ticket_key="OP-idem-1",
        resource_key="mutex:idem1",
        owner_agent_class="subscription-claude",
        owner_instance_id="claude-1",
    )
    _backdate_heartbeat(db_path, lease.lease_id, age_seconds=900)
    first = rc.expire_stale_active_claims(max_age_seconds=300)
    second = rc.expire_stale_active_claims(max_age_seconds=300)
    assert first == 1
    assert second == 0


def test_expire_stale_active_claims_partial_age_buckets(db_path):
    """Of 4 active claims with varied heartbeat ages, only those older
    than the threshold get expired."""
    fresh = rc.acquire_claim(
        ticket_key="OP-bk-1", resource_key="mutex:bk1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    just_under = rc.acquire_claim(
        ticket_key="OP-bk-2", resource_key="mutex:bk2",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    just_over = rc.acquire_claim(
        ticket_key="OP-bk-3", resource_key="mutex:bk3",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    very_stale = rc.acquire_claim(
        ticket_key="OP-bk-4", resource_key="mutex:bk4",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    _backdate_heartbeat(db_path, just_under.lease_id, age_seconds=250)
    _backdate_heartbeat(db_path, just_over.lease_id, age_seconds=320)
    _backdate_heartbeat(db_path, very_stale.lease_id, age_seconds=3600)

    expired = rc.expire_stale_active_claims(max_age_seconds=300)
    assert expired == 2, "just_over + very_stale should be expired"

    active = {h.lease_id for h in rc.find_active_holders()}
    assert fresh.lease_id in active
    assert just_under.lease_id in active
    assert just_over.lease_id not in active
    assert very_stale.lease_id not in active
