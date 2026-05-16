"""AUDIT-24/OP-977 — same-instance atomic-claim race regression suite.

This is the AC #3 / Test-plan harness: it drives ``claim_ticket_atomic``
through the *same-instance* race that OP-838's bare-label "mutex" could not
serialise (the OP-974 incident, 2026-05-12: claude-1 + claude-2 both
"claimed" OP-974; claude-2's failure-recovery wrongly reverted claude-1's
Under-Review work; operator rescue at 16:45).

The fix replaces the idempotent ``claim:{instance_id}`` label-add with a
fencing token ``claim:{instance_id}:{epoch_us:016d}-{uuid8}`` and
"lowest-token-wins" on the post-PUT readback — a *total order over the
readback set*, so every runner that shares an ``instance_id`` agrees on
exactly one winner regardless of how their GET/PUT calls interleave.

``RaceJira`` is a thread-safe in-memory JIRA stand-in that
- models ``labels`` as a set (``update.labels.add`` = set-union, exactly
  the property that broke OP-838) and ``assignee`` as a single-valued
  last-writer-wins field,
- records the order of every PUT (``put_order``) so a test can assert the
  outcome is independent of who wrote first,
- can gate any phase (pre-GET / PUT / post-GET) on a ``threading.Barrier``
  so a test can force the fully-interleaved schedule deterministically,
- can lag the post-PUT GET (``lag_gets``) to exercise the
  ``JIRAPutEventualConsistencyDelay`` retry path.

Test-plan coverage (7 cases):
  1. single-runner happy path
  2. two runners, same instance_id, sequential       → first won, second lost
  3. two runners, same instance_id, interleaved      → exactly one won,
     and the *lowest token* wins for every PUT order
  4. three runners, same instance_id, interleaved    → exactly one won
  5. runner crash mid-claim leaves a stale token     → next claim sweeps it
  6. backward compat with the OP-838 bare label      → recognised + cleaned
  7. eventual-consistency: PUT ok but GET lags 100ms → retry recovers
"""
from __future__ import annotations

import itertools
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import pytest

from backend.agents import jira_dispatch as jd


# ── RaceJira ───────────────────────────────────────────────────────


class RaceJira:
    """Thread-safe JIRA stand-in with real set-union label semantics."""

    def __init__(self, *, initial_assignee: str | None = None,
                 initial_labels: tuple[str, ...] = ()) -> None:
        self._lock = threading.Lock()
        self.assignee: str | None = initial_assignee
        self.labels: set[str] = set(initial_labels)
        self.put_order: list[str] = []          # label added by each PUT, in order
        self.calls: list[tuple[str, str]] = []  # (method, phase-ish)
        # Eventual-consistency knob: this many post-PUT GETs return the
        # pre-PUT snapshot before the label becomes visible.
        self.lag_gets: int = 0
        self._lagged_snapshot: dict | None = None
        # Optional per-phase gating: a {phase: Barrier} dict. Phases:
        # "pre-get" (the first GET of a claim), "put", "post-get".
        self.gates: dict[str, threading.Barrier] = {}
        self._seen_get_threads: set[int] = set()

    def _snapshot(self) -> dict:
        return {
            "fields": {
                "assignee": ({"accountId": self.assignee} if self.assignee else None),
                "labels": sorted(self.labels),
            },
        }

    def _phase_for(self, method: str) -> str:
        if method == "PUT":
            return "put"
        tid = threading.get_ident()
        if tid in self._seen_get_threads:
            return "post-get"
        self._seen_get_threads.add(tid)
        return "pre-get"

    def request(self, client: jd.DispatchClient, method: str, path: str,
                body: dict | None = None) -> dict:
        with self._lock:
            phase = self._phase_for(method)
            self.calls.append((method, phase))
            if method == "GET":
                if phase == "post-get" and self.lag_gets > 0 and self._lagged_snapshot:
                    self.lag_gets -= 1
                    snap = self._lagged_snapshot
                else:
                    snap = self._snapshot()
            else:  # PUT
                self._lagged_snapshot = self._snapshot()
                fields = (body or {}).get("fields") or {}
                if "assignee" in fields:
                    a = fields["assignee"]
                    self.assignee = (a or {}).get("accountId") if a else None
                for op in ((body or {}).get("update") or {}).get("labels", []):
                    if "add" in op:
                        self.labels.add(op["add"])
                        if op["add"].startswith("claim:"):
                            self.put_order.append(op["add"])
                    if "remove" in op:
                        self.labels.discard(op["remove"])
                snap = {}
        # Gate *outside* the lock so the other threads can reach the same
        # barrier; this is what forces the fully-interleaved schedule.
        gate = self.gates.get(phase)
        if gate is not None:
            try:
                gate.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        return snap


def _client(*, bot: str = "acc-bot") -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id=bot,
        bot_email="bot@example.invalid",
    )


def _install(monkeypatch, fake: RaceJira) -> None:
    monkeypatch.setattr(jd, "_request", fake.request)
    monkeypatch.setattr(
        jd, "_request_idempotent",
        lambda client, method, path, body, idem_key: fake.request(client, method, path, body),
    )
    monkeypatch.setattr(jd, "_CLAIM_READBACK_DELAY_S", 0.0)


def _mk_token_factory(uids):
    """Deterministic ``_mint_claim_token`` replacement.

    Each call returns ``{epoch_us:016d}-{uid}`` for the next ``uid`` in
    ``uids``, using a *fresh* real microsecond epoch so the token is never
    seen as stale by the staleness sweep.
    """
    it = iter(uids)
    used: list[str] = []

    def _mint(now_us: int | None = None) -> str:
        uid = next(it)
        epoch = (now_us if now_us is not None else time.time_ns() // 1000)
        tok = f"{epoch:016d}-{uid}"
        used.append(tok)
        return tok

    _mint.used = used  # type: ignore[attr-defined]
    return _mint


def _run_threads(targets) -> None:
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    for t in threads:
        assert not t.is_alive(), "claim thread deadlocked"


# ── Case 1: single-runner happy path ──────────────────────────────


def test_case1_single_runner_happy_path(monkeypatch) -> None:
    fake = RaceJira()
    _install(monkeypatch, fake)
    r = jd.claim_ticket_atomic(_client(bot="acc-codex"), "OP-555", "default")
    assert r.ok is True and r.lost_to is None
    fenced = [l for l in fake.labels if l.startswith("claim:default:")]
    assert len(fenced) == 1
    assert fake.assignee == "acc-codex"


# ── Case 2: two runners, same instance_id, sequential ─────────────


def test_case2_two_runners_same_instance_sequential(monkeypatch) -> None:
    fake = RaceJira()
    _install(monkeypatch, fake)
    mint = _mk_token_factory(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(jd, "_mint_claim_token", mint)
    c = _client(bot="acc-codex")

    first = jd.claim_ticket_atomic(c, "OP-555", "default")
    second = jd.claim_ticket_atomic(c, "OP-555", "default")

    assert first.ok is True
    assert second.ok is False
    assert second.lost_to is not None and second.lost_to.endswith("-aaaaaaaa")
    # The second attempt leaves its loser label behind for GC.
    assert sum(1 for l in fake.labels if l.startswith("claim:default:")) == 2


# ── Case 3: interleaved — exactly one wins, the lowest token wins ─


@pytest.mark.parametrize("uid_a,uid_b", [
    ("00000001", "ffffffff"),   # A has the lower token → A must win
    ("ffffffff", "00000001"),   # B has the lower token → B must win
    ("7f3a9c10", "7f3a9c0f"),   # adjacent values — B lower by one
])
def test_case3_interleaved_lowest_token_wins(monkeypatch, uid_a, uid_b) -> None:
    fake = RaceJira()
    # Force the fully-interleaved schedule: both pre-GETs complete, then
    # both PUTs complete, then both post-GETs run — so each post-GET
    # readback observes *both* claim labels and the lowest-token rule (not
    # who happened to write first) decides the winner. The PUT *order*
    # itself is left racy on purpose — that is exactly the property under
    # test, and ``put_order`` records whichever order actually occurred.
    fake.gates = {
        "pre-get": threading.Barrier(2),
        "put": threading.Barrier(2),
        "post-get": threading.Barrier(2),
    }
    _install(monkeypatch, fake)
    # Same instance_id, same bot account — the OP-974 shape exactly.
    bot, inst = "acc-claude", "default"
    # Identical epoch so only the uid suffix decides the order.
    epoch = time.time_ns() // 1000
    tokens = {"A": f"{epoch:016d}-{uid_a}", "B": f"{epoch:016d}-{uid_b}"}
    name_by_tid: dict[int, str] = {}
    monkeypatch.setattr(jd, "_mint_claim_token",
                        lambda now_us=None: tokens[name_by_tid[threading.get_ident()]])

    results: dict[str, jd.ClaimResult] = {}
    go = threading.Barrier(2)

    def claim(name):
        name_by_tid[threading.get_ident()] = name
        go.wait()
        results[name] = jd.claim_ticket_atomic(_client(bot=bot), "OP-555", inst)

    _run_threads([lambda: claim("A"), lambda: claim("B")])

    winners = [k for k, r in results.items() if r.ok]
    losers = [k for k, r in results.items() if not r.ok]
    assert len(winners) == 1, f"expected exactly one winner, got {winners} ({results})"
    assert len(losers) == 1
    expected_winner = "A" if tokens["A"] < tokens["B"] else "B"
    assert winners[0] == expected_winner, (
        f"lowest token {min(tokens.values())!r} should win; got winner={winners[0]}"
    )
    assert results[losers[0]].lost_to == f"claim:{inst}:{tokens[expected_winner]}"
    assert len(fake.put_order) == 2   # both PUTs really happened


# ── Case 4: three runners, same instance_id ───────────────────────


def test_case4_three_runners_same_instance_exactly_one_wins(monkeypatch) -> None:
    fake = RaceJira()
    fake.gates = {
        "pre-get": threading.Barrier(3),
        "put": threading.Barrier(3),
        "post-get": threading.Barrier(3),
    }
    _install(monkeypatch, fake)
    bot, inst = "acc-claude", "default"
    epoch = time.time_ns() // 1000
    tokens = {"A": f"{epoch:016d}-00000010",
              "B": f"{epoch:016d}-00000002",   # lowest
              "C": f"{epoch:016d}-00000030"}
    name_by_tid: dict[int, str] = {}
    monkeypatch.setattr(jd, "_mint_claim_token",
                        lambda now_us=None: tokens[name_by_tid[threading.get_ident()]])
    results: dict[str, jd.ClaimResult] = {}
    go = threading.Barrier(3)

    def claim(name):
        name_by_tid[threading.get_ident()] = name
        go.wait()
        results[name] = jd.claim_ticket_atomic(_client(bot=bot), "OP-777", inst)

    _run_threads([lambda: claim("A"), lambda: claim("B"), lambda: claim("C")])

    winners = [k for k, r in results.items() if r.ok]
    assert winners == ["B"], f"expected only B (lowest token) to win, got {winners} ({results})"
    for loser in ("A", "C"):
        assert results[loser].lost_to == f"claim:{inst}:{tokens['B']}"


# ── Case 5: runner crash mid-claim leaves a stale fenced token ────


def test_case5_stale_token_from_crashed_runner_is_swept(monkeypatch) -> None:
    # Crashed runner left a fenced label with an ancient epoch (well past
    # the 2× CLI-timeout stale window).
    orphan = "claim:default:0000000000000123-deadbeef"
    fake = RaceJira(initial_labels=(orphan, "tier:M"))
    _install(monkeypatch, fake)
    r = jd.claim_ticket_atomic(_client(), "OP-555", "default")
    assert r.ok is True
    assert orphan not in fake.labels
    assert any(l.startswith("claim:default:") and l != orphan for l in fake.labels)
    assert "tier:M" in fake.labels


# ── Case 6: backward compat with the OP-838 bare label ────────────


def test_case6_legacy_bare_label_recognised_and_cleaned(monkeypatch) -> None:
    fake = RaceJira(initial_labels=("claim:default", "tier:M"))
    _install(monkeypatch, fake)
    r = jd.claim_ticket_atomic(_client(), "OP-555", "default")
    assert r.ok is True
    assert "claim:default" not in fake.labels                    # legacy label cleaned
    assert any(l.startswith("claim:default:") for l in fake.labels)  # fenced replacement
    assert "tier:M" in fake.labels


def test_case6b_legacy_label_under_rollback_flag_round_trips(monkeypatch) -> None:
    # With the rollback flag set, the legacy idempotent-add path is used and
    # the bare label is what gets written (forward-compatible: AUDIT-24 code
    # would later see it as "expired" and clean it).
    monkeypatch.setenv("OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY", "1")
    fake = RaceJira()
    _install(monkeypatch, fake)
    r = jd.claim_ticket_atomic(_client(bot="acc-codex"), "OP-555", "default")
    assert r.ok is True
    assert "claim:default" in fake.labels
    assert not any(l.startswith("claim:default:") for l in fake.labels)


# ── Case 7: eventual-consistency — PUT ok, GET lags, retry recovers


def test_case7_put_then_get_lag_recovered_by_retry(monkeypatch) -> None:
    fake = RaceJira()
    fake.lag_gets = 1   # the first post-PUT GET still shows the pre-PUT state
    _install(monkeypatch, fake)
    r = jd.claim_ticket_atomic(_client(), "OP-555", "default")
    assert r.ok is True
    # pre-GET + (immediate post-GET that missed) + (retried post-GET) = 3.
    assert sum(1 for m, _ in fake.calls if m == "GET") == 3


def test_case7b_get_lag_beyond_retry_budget_is_a_clean_loss(monkeypatch) -> None:
    fake = RaceJira()
    fake.lag_gets = 50
    _install(monkeypatch, fake)
    r = jd.claim_ticket_atomic(_client(), "OP-555", "default")
    assert r.ok is False
    assert r.lost_to == "claim-label-missing-from-readback"


# ── Stress: many randomised interleavings still pick exactly one winner ──


def test_stress_repeated_two_runner_race_always_single_winner(monkeypatch) -> None:
    """Re-runs the fully-interleaved two-runner schedule (both pre-GETs,
    then both PUTs, then both post-GETs) many times. Which thread mints the
    lower token varies run to run (real ``time.time_ns()`` + a monotonic
    suffix counter, raced for by the two threads), so both token orderings
    get exercised; the lowest-token rule must still pick exactly one winner
    every time. This is the schedule the design guarantees — the residual
    >600ms-wedge race is out of scope per docs/sop/runner-pickup-mutex.md §3.2.
    """
    counter = itertools.count()

    def mint(now_us=None):
        n = next(counter)
        epoch = time.time_ns() // 1000
        return f"{epoch:016d}-{n:08x}"

    monkeypatch.setattr(jd, "_mint_claim_token", mint)
    for _ in range(25):
        fake = RaceJira()
        fake.gates = {
            "pre-get": threading.Barrier(2),
            "put": threading.Barrier(2),
            "post-get": threading.Barrier(2),
        }
        # Re-install per iteration (fresh fake, fresh barriers).
        monkeypatch.setattr(jd, "_request", fake.request)
        monkeypatch.setattr(jd, "_CLAIM_READBACK_DELAY_S", 0.0)
        results: dict[str, jd.ClaimResult] = {}
        go = threading.Barrier(2)

        def claim(name):
            go.wait()
            results[name] = jd.claim_ticket_atomic(_client(bot="acc-claude"), "OP-999", "default")

        _run_threads([lambda: claim("A"), lambda: claim("B")])
        ok = [k for k, r in results.items() if r.ok]
        assert len(ok) == 1, f"iteration produced {ok} winners: {results}"
        # The winner's token is the lexicographically smallest fenced label.
        fenced = sorted(l for l in fake.labels if l.startswith("claim:default:"))
        winner_label = f"claim:default:{results[ok[0]].claim_token.split(':', 1)[1]}"
        assert winner_label == fenced[0]
