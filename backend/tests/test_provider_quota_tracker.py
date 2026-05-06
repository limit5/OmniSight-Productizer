"""MP.W1.2 -- provider quota tracker PG contract."""

from __future__ import annotations

import concurrent.futures
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from backend.agents import provider_quota_tracker as tracker


@pytest.fixture
def quota_dsn(pg_test_alembic_upgraded: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_alembic_upgraded)
    with psycopg2.connect(pg_test_alembic_upgraded) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE provider_usage_event, provider_quota_state")
            cur.execute(
                "DELETE FROM audit_log WHERE action = 'provider_quota_cap_hit'"
            )
    yield pg_test_alembic_upgraded
    with psycopg2.connect(pg_test_alembic_upgraded) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE provider_usage_event, provider_quota_state")
            cur.execute(
                "DELETE FROM audit_log WHERE action = 'provider_quota_cap_hit'"
            )


def _provider(name: str) -> str:
    return f"op15-{name}-{uuid.uuid4().hex[:10]}"


def test_record_usage_round_trip(quota_dsn: str) -> None:
    provider = _provider("roundtrip")

    tracker.record_usage(provider, 123)
    state = tracker.get_quota_state(provider)

    assert state.provider == provider
    assert state.rolling_5h_tokens == 123
    assert state.weekly_tokens == 123
    assert state.last_reset_at is None
    assert state.last_cap_hit_at is None
    assert state.circuit_state == "closed"


def test_get_quota_state_creates_empty_provider_row(quota_dsn: str) -> None:
    state = tracker.get_quota_state(_provider("empty"))

    assert state.rolling_5h_tokens == 0
    assert state.weekly_tokens == 0
    assert state.circuit_state == "closed"


def test_five_hour_window_excludes_expired_events(quota_dsn: str) -> None:
    provider = _provider("five-hour")
    now = datetime.now(timezone.utc)

    tracker.record_usage(provider, 80, ts=now - timedelta(hours=6))
    tracker.record_usage(provider, 20, ts=now - timedelta(hours=1))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 20
    assert state.weekly_tokens == 100


def test_weekly_window_excludes_expired_events(quota_dsn: str) -> None:
    provider = _provider("weekly")
    now = datetime.now(timezone.utc)

    tracker.record_usage(provider, 200, ts=now - timedelta(days=8))
    tracker.record_usage(provider, 30, ts=now - timedelta(days=2))
    tracker.record_usage(provider, 40, ts=now - timedelta(hours=2))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 40
    assert state.weekly_tokens == 70


def test_same_provider_concurrent_record_usage_has_no_lost_updates(
    quota_dsn: str,
) -> None:
    provider = _provider("race")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda n: tracker.record_usage(provider, n), range(1, 11)))

    state = tracker.get_quota_state(provider)
    assert state.rolling_5h_tokens == 55
    assert state.weekly_tokens == 55


def test_different_provider_writes_do_not_wait_on_locked_provider(
    quota_dsn: str,
) -> None:
    locked_provider = _provider("locked")
    free_provider = _provider("free")
    lock_ready = threading.Event()
    release_lock = threading.Event()

    def _hold_provider_lock() -> None:
        with psycopg2.connect(quota_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (locked_provider,),
                )
                lock_ready.set()
                release_lock.wait(timeout=5)

    holder = threading.Thread(target=_hold_provider_lock)
    holder.start()
    assert lock_ready.wait(timeout=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        blocked = pool.submit(tracker.record_usage, locked_provider, 1)
        free = pool.submit(tracker.record_usage, free_provider, 7)
        assert free.result(timeout=1) is None
        assert blocked.done() is False
        release_lock.set()
        assert blocked.result(timeout=3) is None

    holder.join(timeout=2)
    assert tracker.get_quota_state(free_provider).weekly_tokens == 7
    assert tracker.get_quota_state(locked_provider).weekly_tokens == 1


def test_cap_hit_opens_circuit_and_emits_audit_log(
    quota_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider("cap")
    monkeypatch.setenv(
        f"OMNISIGHT_PROVIDER_CAP_{tracker._env_provider(provider)}_5H",
        "10",
    )
    monkeypatch.setenv(
        f"OMNISIGHT_PROVIDER_CAP_{tracker._env_provider(provider)}_WEEKLY",
        "100",
    )

    tracker.record_usage(provider, 9)
    assert tracker.get_quota_state(provider).circuit_state == "closed"

    tracker.record_usage(provider, 2)
    state = tracker.get_quota_state(provider)

    assert state.circuit_state == "open"
    assert state.last_cap_hit_at is not None
    assert tracker.is_at_cap(provider, "5h") is True
    assert tracker.is_at_cap(provider, "weekly") is False

    with psycopg2.connect(quota_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT after_json
                FROM audit_log
                WHERE action = 'provider_quota_cap_hit'
                  AND entity_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (provider,),
            )
            row = cur.fetchone()

    assert row is not None
    assert '"provider": "%s"' % provider in row[0]
    assert '"scopes": ["5h"]' in row[0]


def test_cap_hit_audit_emits_only_on_closed_to_open_transition(
    quota_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider("cap-once")
    monkeypatch.setenv(
        f"OMNISIGHT_PROVIDER_CAP_{tracker._env_provider(provider)}_5H",
        "3",
    )

    tracker.record_usage(provider, 4)
    tracker.record_usage(provider, 4)

    with psycopg2.connect(quota_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM audit_log
                WHERE action = 'provider_quota_cap_hit'
                  AND entity_id = %s
                """,
                (provider,),
            )
            count = cur.fetchone()[0]

    assert count == 1


def test_reset_five_hour_window_keeps_older_weekly_usage(
    quota_dsn: str,
) -> None:
    provider = _provider("reset-5h")
    now = datetime.now(timezone.utc)

    tracker.record_usage(provider, 10, ts=now - timedelta(hours=6))
    tracker.record_usage(provider, 5, ts=now - timedelta(hours=1))
    tracker.reset_window(provider, "5h")
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 0
    assert state.weekly_tokens == 10
    assert state.last_reset_at is not None
    assert state.circuit_state == "closed"


def test_reset_weekly_window_clears_active_usage(quota_dsn: str) -> None:
    provider = _provider("reset-weekly")

    tracker.record_usage(provider, 10)
    tracker.reset_window(provider, "weekly")
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 0
    assert state.weekly_tokens == 0
    assert state.last_reset_at is not None


def test_invalid_provider_and_tokens_rejected(quota_dsn: str) -> None:
    with pytest.raises(ValueError, match="provider"):
        tracker.record_usage(" ", 1)
    with pytest.raises(ValueError, match="tokens"):
        tracker.record_usage("anthropic-subscription", -1)


def test_same_provider_lock_serializes_writer(quota_dsn: str) -> None:
    provider = _provider("same-lock")
    lock_ready = threading.Event()
    release_lock = threading.Event()

    def _hold_provider_lock() -> None:
        with psycopg2.connect(quota_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (provider,),
                )
                lock_ready.set()
                release_lock.wait(timeout=5)

    holder = threading.Thread(target=_hold_provider_lock)
    holder.start()
    assert lock_ready.wait(timeout=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(tracker.record_usage, provider, 3)
        time.sleep(0.2)
        assert fut.done() is False
        release_lock.set()
        assert fut.result(timeout=3) is None

    holder.join(timeout=2)
    assert tracker.get_quota_state(provider).weekly_tokens == 3
