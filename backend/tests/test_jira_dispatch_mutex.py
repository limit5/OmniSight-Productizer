"""OP-838: tests for ``claim_ticket_atomic`` + ``release_ticket_claim``.

Pins the mutex behaviour that prevents duplicate Gerrit pushes from
concurrent runner pickups (the OP-836 #356 + OP-837 #358 abandon pattern,
2026-05-11). Each test simulates JIRA via a thread-safe in-memory
``FakeJira`` that mimics the bits we depend on:

- ``assignee`` is a single-valued field (last-writer-wins on PUT).
- ``labels`` is a set; ``update.labels.add`` is set-union.
- ``GET fields=assignee,labels`` returns a consistent snapshot of both.

Cases (AC #5):

1. Solo claim succeeds (empty initial state → win).
2. Two concurrent claims via ``threading`` → exactly one wins, the other
   gets ``ok=False`` with ``lost_to`` pointing at the winner.
3. Claim with stale snapshot (prior claim cleared via
   ``release_ticket_claim``) → claim succeeds + records the new token.
4. Same instance_id pickups twice → idempotent (own claim recognised as
   own; second call returns ``ok=True``).
5. Bonus: claim persists across runner restart → re-pickup with the same
   instance_id sees its own stale label and still returns ``ok=True``.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import pytest

from backend.agents import jira_dispatch as jd


# ── FakeJira: in-memory ticket state with real concurrency semantics ──


class FakeJira:
    """Thread-safe in-memory JIRA stand-in for the ``_request`` surface.

    Models the two ticket fields that ``claim_ticket_atomic`` touches:
    ``assignee`` (single-valued, last-writer-wins on PUT) and ``labels``
    (set-valued, set-union on ``update.labels.add``). A single lock
    serialises every request so a real ``threading.Thread`` race in the
    test sees consistent snapshots and last-writer-wins semantics.
    """

    def __init__(self, *, initial_assignee: str | None = None,
                 initial_labels: tuple[str, ...] = ()) -> None:
        self._lock = threading.Lock()
        self.assignee: str | None = initial_assignee
        self.labels: set[str] = set(initial_labels)
        # Per-call audit log (for tests that need to assert ordering).
        self.calls: list[tuple[str, str, dict | None]] = []
        # Tunable per-call delay to widen the race window in concurrency tests.
        self.put_delay_s: float = 0.0

    def _snapshot(self) -> dict:
        return {
            "fields": {
                "assignee": ({"accountId": self.assignee} if self.assignee else None),
                "labels": sorted(self.labels),
            },
        }

    def request(self, client: jd.DispatchClient, method: str, path: str,
                body: dict | None = None) -> dict:
        """Replacement for ``jira_dispatch._request``."""
        with self._lock:
            self.calls.append((method, path, body))
            if method == "GET" and "/issue/" in path:
                return self._snapshot()
            if method == "PUT" and "/issue/" in path:
                fields = (body or {}).get("fields") or {}
                if "assignee" in fields:
                    assignee = fields["assignee"]
                    self.assignee = (
                        (assignee or {}).get("accountId") if assignee else None
                    )
                update = (body or {}).get("update") or {}
                for op in update.get("labels", []):
                    if "add" in op:
                        self.labels.add(op["add"])
                    if "remove" in op:
                        self.labels.discard(op["remove"])
            # Other endpoints (`/transitions`, `/comment`) — return empty.
        # Replicate observed network latency so the threading test can
        # actually race the two PUTs; held outside the lock so concurrent
        # callers can interleave through the GET → PUT → GET window.
        if method == "PUT" and self.put_delay_s:
            time.sleep(self.put_delay_s)
        return {}


def _fake_dispatch_client(*, bot_account_id: str = "acc-bot-A") -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id=bot_account_id,
        bot_email="bot@example.invalid",
    )


def _install_fake(monkeypatch, fake: FakeJira) -> None:
    monkeypatch.setattr(jd, "_request", fake.request)
    # ``release_ticket_claim`` delegates to ``remove_label`` which itself
    # calls ``_request_idempotent``. Route those through the fake too.
    monkeypatch.setattr(
        jd, "_request_idempotent",
        lambda client, method, path, body, idem_key: fake.request(client, method, path, body),
    )


# ── ClaimResult shape ──────────────────────────────────────────────


def test_claim_result_is_frozen_dataclass() -> None:
    """AC #2: ClaimResult must be a frozen dataclass with ok + lost_to fields."""
    r = jd.ClaimResult(ok=True, lost_to=None, claim_token="default:2026-05-11T00:00:00")
    assert r.ok is True
    assert r.lost_to is None
    assert r.claim_token == "default:2026-05-11T00:00:00"
    with pytest.raises((AttributeError, Exception)):
        r.ok = False  # type: ignore[misc]


def test_claim_label_format_matches_ac_default() -> None:
    """AC #4: default label is ``claim:{instance_id}``."""
    assert jd._our_claim_label("default") == "claim:default"
    assert jd._our_claim_label("2") == "claim:2"
    assert jd._claim_label_instance("claim:default") == "default"
    assert jd._claim_label_instance("claim:3") == "3"
    assert jd._claim_label_instance("scope:backend") is None
    assert jd._claim_label_instance("claim:") is None  # empty suffix


# ── AC #5 case 1: solo claim succeeds ─────────────────────────────


def test_solo_claim_on_unassigned_ticket_succeeds(monkeypatch) -> None:
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client(bot_account_id="acc-codex")

    result = jd.claim_ticket_atomic(client, "OP-555", "default")

    assert result.ok is True
    assert result.lost_to is None
    assert result.claim_token is not None and result.claim_token.startswith("default:")
    assert fake.assignee == "acc-codex"
    assert "claim:default" in fake.labels


def test_solo_claim_sets_assignee_and_label_atomically(monkeypatch) -> None:
    """AC #1 step b: assignee + label written in a single PUT."""
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client(bot_account_id="acc-codex")

    jd.claim_ticket_atomic(client, "OP-555", "default")

    puts = [c for c in fake.calls if c[0] == "PUT"]
    assert len(puts) == 1, f"expected 1 PUT, got {len(puts)}"
    method, path, body = puts[0]
    assert path == "/issue/OP-555"
    assert body["fields"]["assignee"]["accountId"] == "acc-codex"
    assert body["update"]["labels"] == [{"add": "claim:default"}]


def test_solo_claim_emits_three_requests_in_correct_order(monkeypatch) -> None:
    """AC #1: pre-GET → PUT → post-GET, exactly."""
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client()

    jd.claim_ticket_atomic(client, "OP-555", "default")

    methods = [c[0] for c in fake.calls]
    assert methods == ["GET", "PUT", "GET"]


# ── AC #5 case 2: concurrent claims via threading ─────────────────


def test_two_concurrent_claims_exactly_one_wins(monkeypatch) -> None:
    """AC #5 case 2: threading race → exactly one ok=True, the other ok=False
    with ``lost_to`` set to ``assignee:<winner-bot>``.

    Mirrors the OP-836/837 incident: ``codex-bot`` and ``claude-bot`` both
    pass the JQL ``assignee is EMPTY`` filter and reach
    ``transition_to_in_progress`` simultaneously. The fix discriminates via
    the post-PUT assignee readback — single-valued, last-writer-wins, so
    exactly one bot accountId appears for both threads' readback.
    """
    fake = FakeJira()
    fake.put_delay_s = 0.05  # 50ms widens the GET→PUT→GET interleaving
    _install_fake(monkeypatch, fake)
    client_codex = _fake_dispatch_client(bot_account_id="acc-codex-bot")
    client_claude = _fake_dispatch_client(bot_account_id="acc-claude-bot")

    results: dict[str, jd.ClaimResult] = {}
    barrier = threading.Barrier(2)

    def claim_as(name: str, client: jd.DispatchClient, instance_id: str) -> None:
        barrier.wait()
        results[name] = jd.claim_ticket_atomic(client, "OP-555", instance_id)

    t1 = threading.Thread(target=claim_as, args=("codex", client_codex, "default"))
    t2 = threading.Thread(target=claim_as, args=("claude", client_claude, "default-claude"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    winners = [k for k, r in results.items() if r.ok]
    losers = [k for k, r in results.items() if not r.ok]
    assert len(winners) == 1, f"expected exactly 1 winner, got {winners}"
    assert len(losers) == 1, f"expected exactly 1 loser, got {losers}"

    winner = winners[0]
    loser = losers[0]
    winner_account = {"codex": "acc-codex-bot", "claude": "acc-claude-bot"}[winner]
    winner_label = {"codex": "claim:default", "claude": "claim:default-claude"}[winner]
    # Loser's lost_to points at the winner — either via the pre-GET fast-fail
    # (foreign claim label spotted) or via the post-GET assignee mismatch,
    # depending on how the two threads' GET / PUT / GET interleaved. Both
    # are correct mutex outcomes.
    assert results[loser].lost_to in (winner_label, f"assignee:{winner_account}"), (
        f"unexpected lost_to: {results[loser].lost_to!r}"
    )
    assert results[loser].claim_token is not None


def test_post_get_assignee_discriminates_when_both_pass_pre_get(monkeypatch) -> None:
    """Deterministic AC #1 step c+d: both threads pass pre-GET (empty state),
    both PUT, then post-GET's assignee field discriminates last-writer-wins.

    Uses a controlled barrier sequence so both pre-GETs land before either
    PUT — guaranteeing the post-GET path is exercised rather than the
    pre-GET fast-fail.
    """
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client_a = _fake_dispatch_client(bot_account_id="acc-bot-A")
    client_b = _fake_dispatch_client(bot_account_id="acc-bot-B")

    # Sequence each request through gates so both pre-GETs run, then both
    # PUTs run, then both post-GETs run.
    pre_get_done = threading.Barrier(2)
    put_done = threading.Barrier(2)
    real_request = fake.request

    state = {"pre_count": 0, "put_count": 0}
    state_lock = threading.Lock()

    def gated_request(client, method, path, body=None):
        result = real_request(client, method, path, body)
        if method == "GET" and "/issue/" in path:
            with state_lock:
                state["pre_count"] += 1
                # First GET per thread is the pre-GET; second is post-GET.
                is_pre = state["pre_count"] <= 2
            if is_pre:
                pre_get_done.wait(timeout=5)
            else:
                # post-GET happens after both PUTs done
                pass
        elif method == "PUT":
            with state_lock:
                state["put_count"] += 1
            put_done.wait(timeout=5)
        return result

    monkeypatch.setattr(jd, "_request", gated_request)

    results: dict[str, jd.ClaimResult] = {}

    def claim_as(name: str, client: jd.DispatchClient, instance_id: str) -> None:
        results[name] = jd.claim_ticket_atomic(client, "OP-555", instance_id)

    t1 = threading.Thread(target=claim_as, args=("A", client_a, "a"))
    t2 = threading.Thread(target=claim_as, args=("B", client_b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    winners = [k for k, r in results.items() if r.ok]
    losers = [k for k, r in results.items() if not r.ok]
    assert len(winners) == 1, f"expected exactly 1 winner, got {winners}"
    assert len(losers) == 1
    # Loser's lost_to must be the assignee-form (post-GET path) since both
    # passed pre-GET.
    loser_lost_to = results[losers[0]].lost_to
    assert loser_lost_to is not None
    assert loser_lost_to.startswith("assignee:"), (
        f"expected assignee-form lost_to (post-GET path), got {loser_lost_to!r}"
    )


# ── AC #5 case 3: claim with stale snapshot ───────────────────────


def test_release_ticket_claim_removes_only_own_label(monkeypatch) -> None:
    """``release_ticket_claim`` removes the caller's label, leaves others."""
    fake = FakeJira(initial_labels=("claim:default", "claim:other", "tier:M"))
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client()

    jd.release_ticket_claim(client, "OP-555", "default")

    assert "claim:default" not in fake.labels
    assert "claim:other" in fake.labels
    assert "tier:M" in fake.labels


def test_claim_with_stale_snapshot_succeeds_after_release(monkeypatch) -> None:
    """AC #5 case 3: prior claim cleared → new claim records the new token."""
    fake = FakeJira(initial_labels=("claim:default",))
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client()

    # Operator (or revert path) clears the prior claim.
    jd.release_ticket_claim(client, "OP-555", "default")
    assert "claim:default" not in fake.labels

    # A different instance now picks up the cleared ticket. Use bot-B so
    # the cross-bot fast-fail (pre-GET assignee mismatch) doesn't fire.
    fake.assignee = None  # release_ticket_claim only touches the label
    client2 = _fake_dispatch_client(bot_account_id="acc-bot-B")
    result = jd.claim_ticket_atomic(client2, "OP-555", "2")

    assert result.ok is True
    assert result.lost_to is None
    assert "claim:2" in fake.labels
    # The recorded claim_token reflects the fresh attempt's instance_id.
    assert result.claim_token is not None and result.claim_token.startswith("2:")


# ── AC #5 case 4 + bonus case 5: idempotent re-claim ──────────────


def test_same_instance_id_pickups_twice_are_idempotent(monkeypatch) -> None:
    """AC #5 case 4: re-claim by the same instance returns ok=True both times."""
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client(bot_account_id="acc-codex")

    first = jd.claim_ticket_atomic(client, "OP-555", "default")
    second = jd.claim_ticket_atomic(client, "OP-555", "default")

    assert first.ok is True
    assert second.ok is True
    assert second.lost_to is None
    # set-add semantics: only one claim:default label even after two PUTs.
    assert sum(1 for l in fake.labels if l == "claim:default") == 1


def test_runner_restart_reuses_persisted_claim_label(monkeypatch) -> None:
    """Bonus case (test plan): instance_id reused after crash → own old
    claim recognised as own, second claim returns ok=True.
    """
    # Simulate state from a prior runner: our label + assignee already set.
    fake = FakeJira(
        initial_assignee="acc-codex",
        initial_labels=("claim:default", "tier:M"),
    )
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client(bot_account_id="acc-codex")

    result = jd.claim_ticket_atomic(client, "OP-555", "default")

    assert result.ok is True
    assert result.lost_to is None


# ── Fast-fail paths (pre-GET detection) ───────────────────────────


def test_pre_get_detects_foreign_claim_label_short_circuits(monkeypatch) -> None:
    """Pre-GET sees foreign claim label → return ok=False without PUT or post-GET.

    Saves a wasted JIRA write when another runner already claimed.
    """
    fake = FakeJira(initial_labels=("claim:other-instance",))
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client()

    result = jd.claim_ticket_atomic(client, "OP-555", "default")

    assert result.ok is False
    assert result.lost_to == "claim:other-instance"
    # No PUT was issued — fast-fail.
    methods = [c[0] for c in fake.calls]
    assert methods == ["GET"]
    # Our label was NOT written.
    assert "claim:default" not in fake.labels


def test_pre_get_detects_foreign_assignee_short_circuits(monkeypatch) -> None:
    """Pre-GET sees foreign assignee (different bot already claimed) →
    return ok=False with ``assignee:<id>``.
    """
    fake = FakeJira(initial_assignee="acc-claude-bot")
    _install_fake(monkeypatch, fake)
    client = _fake_dispatch_client(bot_account_id="acc-codex-bot")

    result = jd.claim_ticket_atomic(client, "OP-555", "default")

    assert result.ok is False
    assert result.lost_to == "assignee:acc-claude-bot"


# ── Transport error wrapping ──────────────────────────────────────


def test_pre_get_failure_raises_runner_mutex_api_error(monkeypatch) -> None:
    """Transport failure on pre-GET → typed RunnerMutexAPIError."""

    def boom(client, method, path, body=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(jd, "_request", boom)
    client = _fake_dispatch_client()

    with pytest.raises(jd.RunnerMutexAPIError) as ei:
        jd.claim_ticket_atomic(client, "OP-555", "default")
    assert ei.value.key == "OP-555"
    assert ei.value.step == "pre-GET"


def test_put_failure_raises_runner_mutex_api_error(monkeypatch) -> None:
    """Transport failure on PUT → typed RunnerMutexAPIError with step='PUT'."""

    calls: list[str] = []

    def maybe_boom(client, method, path, body=None):
        calls.append(method)
        if method == "PUT":
            raise RuntimeError("503 Service Unavailable")
        return {"fields": {"assignee": None, "labels": []}}

    monkeypatch.setattr(jd, "_request", maybe_boom)
    client = _fake_dispatch_client()

    with pytest.raises(jd.RunnerMutexAPIError) as ei:
        jd.claim_ticket_atomic(client, "OP-555", "default")
    assert ei.value.step == "PUT"
    # post-GET must NOT have been attempted — fail fast on PUT failure.
    assert calls.count("GET") == 1


# ── RunnerMutexLost exception class export ─────────────────────────


def test_runner_mutex_lost_exception_carries_diagnostic_fields() -> None:
    """Error catalog: ``RunnerMutexLost(key, claim_token, observed_token)`` is
    a soft signal — exported as a class for programmatic callers."""
    err = jd.RunnerMutexLost(
        key="OP-838",
        claim_token="default:2026-05-11T12:00:00",
        observed_token="claim:other",
    )
    assert err.key == "OP-838"
    assert err.claim_token == "default:2026-05-11T12:00:00"
    assert err.observed_token == "claim:other"
    msg = str(err)
    assert "OP-838" in msg
    assert "default:2026-05-11T12:00:00" in msg
    assert "claim:other" in msg
