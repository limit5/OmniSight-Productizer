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


def test_five_hour_window_counts_multiple_active_events(quota_dsn: str) -> None:
    provider = _provider("five-hour-active")
    now = datetime.now(timezone.utc)

    tracker.record_usage(provider, 11, ts=now - timedelta(hours=4, minutes=50))
    tracker.record_usage(provider, 13, ts=now - timedelta(hours=2))
    tracker.record_usage(provider, 17, ts=now - timedelta(minutes=5))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 41
    assert state.weekly_tokens == 41


def test_five_hour_window_keeps_events_inside_boundary_margin(
    quota_dsn: str,
) -> None:
    provider = _provider("five-hour-boundary")
    now = datetime.now(timezone.utc)

    tracker.record_usage(provider, 19, ts=now - timedelta(hours=5, minutes=1))
    tracker.record_usage(provider, 23, ts=now - timedelta(hours=4, minutes=59))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 23
    assert state.weekly_tokens == 42


def test_five_hour_window_accepts_naive_utc_timestamps(quota_dsn: str) -> None:
    provider = _provider("five-hour-naive")
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)

    tracker.record_usage(provider, 29, ts=naive_now - timedelta(hours=1))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 29
    assert state.weekly_tokens == 29


def test_weekly_window_excludes_expired_events(quota_dsn: str) -> None:
    provider = _provider("weekly")
    now = datetime.now(timezone.utc)

    tracker.record_usage(provider, 200, ts=now - timedelta(days=8))
    tracker.record_usage(provider, 30, ts=now - timedelta(days=2))
    tracker.record_usage(provider, 40, ts=now - timedelta(hours=2))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 40
    assert state.weekly_tokens == 70


def test_weekly_window_accumulates_usage_outside_five_hour_window(
    quota_dsn: str,
) -> None:
    provider = _provider("weekly-outside-5h")
    now = datetime.now(timezone.utc)

    tracker.record_usage(provider, 31, ts=now - timedelta(days=6))
    tracker.record_usage(provider, 37, ts=now - timedelta(hours=6))
    tracker.record_usage(provider, 41, ts=now - timedelta(hours=1))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 41
    assert state.weekly_tokens == 109


def test_weekly_window_counts_multiple_days_of_usage(quota_dsn: str) -> None:
    provider = _provider("weekly-days")
    now = datetime.now(timezone.utc)

    for days, tokens in ((6, 3), (4, 5), (2, 7), (0, 11)):
        tracker.record_usage(provider, tokens, ts=now - timedelta(days=days))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 11
    assert state.weekly_tokens == 26


def test_weekly_cap_hit_uses_accumulated_weekly_usage(
    quota_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider("weekly-cap")
    now = datetime.now(timezone.utc)
    monkeypatch.setenv(
        f"OMNISIGHT_PROVIDER_CAP_{tracker._env_provider(provider)}_5H",
        "100",
    )
    monkeypatch.setenv(
        f"OMNISIGHT_PROVIDER_CAP_{tracker._env_provider(provider)}_WEEKLY",
        "50",
    )

    tracker.record_usage(provider, 30, ts=now - timedelta(days=2))
    tracker.record_usage(provider, 25, ts=now - timedelta(hours=6))
    state = tracker.get_quota_state(provider)

    assert state.rolling_5h_tokens == 0
    assert state.weekly_tokens == 55
    assert state.circuit_state == "open"
    assert tracker.is_at_cap(provider, "5h") is False
    assert tracker.is_at_cap(provider, "weekly") is True


def test_same_provider_concurrent_record_usage_has_no_lost_updates(
    quota_dsn: str,
) -> None:
    provider = _provider("race")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda n: tracker.record_usage(provider, n), range(1, 11)))

    state = tracker.get_quota_state(provider)
    assert state.rolling_5h_tokens == 55
    assert state.weekly_tokens == 55


def test_same_provider_many_worker_race_preserves_event_rows(
    quota_dsn: str,
) -> None:
    provider = _provider("many-race")

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _: tracker.record_usage(provider, 1), range(20)))

    state = tracker.get_quota_state(provider)
    assert state.rolling_5h_tokens == 20
    assert state.weekly_tokens == 20
    with psycopg2.connect(quota_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM provider_usage_event WHERE provider = %s",
                (provider,),
            )
            assert cur.fetchone()[0] == 20


def test_multi_worker_race_isolates_provider_totals(quota_dsn: str) -> None:
    providers = [_provider(f"race-isolated-{idx}") for idx in range(4)]
    jobs = [(providers[idx % len(providers)], idx + 1) for idx in range(24)]
    expected = {provider: 0 for provider in providers}
    for provider, tokens in jobs:
        expected[provider] += tokens

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda job: tracker.record_usage(*job), jobs))

    for provider, tokens in expected.items():
        state = tracker.get_quota_state(provider)
        assert state.rolling_5h_tokens == tokens
        assert state.weekly_tokens == tokens


def test_concurrent_cap_hit_emits_single_audit_log(
    quota_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider("race-cap")
    monkeypatch.setenv(
        f"OMNISIGHT_PROVIDER_CAP_{tracker._env_provider(provider)}_5H",
        "5",
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: tracker.record_usage(provider, 1), range(8)))

    state = tracker.get_quota_state(provider)
    assert state.rolling_5h_tokens == 8
    assert state.circuit_state == "open"
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
            assert cur.fetchone()[0] == 1


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
