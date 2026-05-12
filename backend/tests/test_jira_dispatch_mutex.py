"""AUDIT-24/OP-977: unit tests for the fencing-token ticket-claim mutex.

Pins the behaviour of ``claim_ticket_atomic`` / ``release_ticket_claim``
that prevents duplicate runner pickups — both the original cross-bot shape
(OP-836 #356 / OP-837 #358, 2026-05-11) and the same-instance shape that
OP-838's bare-label "mutex" could not stop (OP-974, 2026-05-12). The
multi-runner *race* harness lives in ``test_atomic_claim_race.py``; this
file covers the mechanism's pieces in isolation.

``FakeJira`` mimics the bits the claim sequence depends on:

- ``assignee`` is single-valued (last-writer-wins on PUT).
- ``labels`` is a set; ``update.labels.add`` is set-union,
  ``update.labels.remove`` is set-difference.
- ``GET fields=...`` returns a consistent snapshot.
"""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import pytest

from backend.agents import jira_dispatch as jd


# ── FakeJira: in-memory ticket state ──────────────────────────────


class FakeJira:
    """Thread-safe in-memory JIRA stand-in for the ``_request`` surface."""

    def __init__(self, *, initial_assignee: str | None = None,
                 initial_labels: tuple[str, ...] = ()) -> None:
        self._lock = threading.Lock()
        self.assignee: str | None = initial_assignee
        self.labels: set[str] = set(initial_labels)
        self.calls: list[tuple[str, str, dict | None]] = []
        # Number of upcoming GETs that should NOT yet reflect the most
        # recent PUT (Atlassian eventual-consistency simulation).
        self.lag_gets: int = 0
        self._lagged_snapshot: dict | None = None

    def _snapshot(self) -> dict:
        return {
            "fields": {
                "assignee": ({"accountId": self.assignee} if self.assignee else None),
                "labels": sorted(self.labels),
            },
        }

    def request(self, client: jd.DispatchClient, method: str, path: str,
                body: dict | None = None) -> dict:
        with self._lock:
            self.calls.append((method, path, body))
            if method == "GET" and "/issue/" in path:
                if self.lag_gets > 0 and self._lagged_snapshot is not None:
                    self.lag_gets -= 1
                    return self._lagged_snapshot
                return self._snapshot()
            if method == "PUT" and "/issue/" in path:
                # Stash a pre-PUT snapshot so the next ``lag_gets`` GETs can
                # return it (label not yet visible).
                self._lagged_snapshot = self._snapshot()
                fields = (body or {}).get("fields") or {}
                if "assignee" in fields:
                    assignee = fields["assignee"]
                    self.assignee = (assignee or {}).get("accountId") if assignee else None
                for op in ((body or {}).get("update") or {}).get("labels", []):
                    if "add" in op:
                        self.labels.add(op["add"])
                    if "remove" in op:
                        self.labels.discard(op["remove"])
        return {}


def _fake_client(*, bot_account_id: str = "acc-bot-A") -> jd.DispatchClient:
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
    monkeypatch.setattr(
        jd, "_request_idempotent",
        lambda client, method, path, body, idem_key: fake.request(client, method, path, body),
    )
    # Keep the readback retry loop fast in tests.
    monkeypatch.setattr(jd, "_CLAIM_READBACK_DELAY_S", 0.0)


_FENCED_RE = re.compile(r"^claim:[^:]+:\d{16,}-[0-9a-f]{8}$")


# ── Helpers / label format (AC #1) ─────────────────────────────────


def test_mint_claim_token_is_epoch_prefixed_and_unique() -> None:
    t1 = jd._mint_claim_token(1_715_000_000_000_000)
    t2 = jd._mint_claim_token(1_715_000_000_000_000)
    assert t1.startswith("1715000000000000-")
    assert t2.startswith("1715000000000000-")
    assert t1 != t2  # uuid8 suffix breaks the same-microsecond tie
    # Lexicographic order == chronological order.
    earlier = jd._mint_claim_token(1_000)
    later = jd._mint_claim_token(2_000)
    assert earlier < later
    assert jd._claim_token_epoch_us(t1) == 1_715_000_000_000_000


def test_fenced_claim_label_round_trips() -> None:
    label = jd._fenced_claim_label("claude-2", "0001715000000000000-ab12cd34")
    assert label == "claim:claude-2:0001715000000000000-ab12cd34"
    assert jd._parse_claim_label(label) == ("claude-2", "0001715000000000000-ab12cd34")
    # Legacy bare label parses to (instance, None).
    assert jd._parse_claim_label("claim:default") == ("default", None)
    # Non-claim labels and the empty-suffix degenerate case → None.
    assert jd._parse_claim_label("mutex:backend/x.py") is None
    assert jd._parse_claim_label("claim:") is None
    # Back-compat shim still reports the instance id only.
    assert jd._claim_label_instance(label) == "claude-2"
    assert jd._claim_label_instance("claim:default") == "default"


def test_lowest_uuid_claim_winner_picks_smallest_live_token_for_instance() -> None:
    now_us = 1_700_000_000_000_000
    hour_us = 3_600_000_000

    def tok(epoch_us: int, uid: str) -> str:
        return f"{epoch_us:016d}-{uid}"

    live_lo = tok(now_us - 1_000, "aaaaaaaa")   # ours, smallest live token
    live_hi = tok(now_us - 500, "zzzzzzzz")     # ours, larger live token
    ancient = tok(now_us - 10 * hour_us, "cccccccc")   # ours but stale (>2h)
    labels = [
        "tier:M",
        f"claim:default:{live_hi}",
        f"claim:default:{live_lo}",
        f"claim:other:{tok(now_us - 2_000, 'bbbbbbbb')}",   # foreign instance — ignored
        "claim:default",                                     # bare legacy — ignored
        f"claim:default:{ancient}",
    ]
    winner = jd._lowest_uuid_claim_winner(labels, "default", now_us=now_us, max_age_s=2 * 3600)
    assert winner == live_lo
    # No live claim for this instance → None.
    assert jd._lowest_uuid_claim_winner(labels, "nobody", now_us=now_us, max_age_s=2 * 3600) is None


# ── Solo claim (test plan case 1) ─────────────────────────────────


def test_solo_claim_writes_fenced_label_and_wins(monkeypatch) -> None:
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client = _fake_client(bot_account_id="acc-codex")

    result = jd.claim_ticket_atomic(client, "OP-555", "default")

    assert result.ok is True
    assert result.lost_to is None
    assert result.claim_token is not None and result.claim_token.startswith("default:")
    assert fake.assignee == "acc-codex"
    fenced = [l for l in fake.labels if l.startswith("claim:")]
    assert len(fenced) == 1 and _FENCED_RE.match(fenced[0]), fenced
    assert fenced[0].startswith("claim:default:")


def test_solo_claim_emits_get_put_get(monkeypatch) -> None:
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert [c[0] for c in fake.calls] == ["GET", "PUT", "GET"]
    # Single PUT carries assignee + the add op together.
    put = next(c for c in fake.calls if c[0] == "PUT")
    body = put[2]
    assert body["fields"]["assignee"]["accountId"] == "acc-bot-A"
    add_ops = [op for op in body["update"]["labels"] if "add" in op]
    assert len(add_ops) == 1 and add_ops[0]["add"].startswith("claim:default:")


# ── Two same-instance claims, sequential (test plan case 2) ───────


def test_second_same_instance_claim_loses(monkeypatch) -> None:
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client = _fake_client(bot_account_id="acc-codex")

    first = jd.claim_ticket_atomic(client, "OP-555", "default")
    second = jd.claim_ticket_atomic(client, "OP-555", "default")

    assert first.ok is True
    assert second.ok is False
    # Loser points at the (lower-token) winner's label.
    assert second.lost_to is not None and second.lost_to.startswith("claim:default:")
    assert _FENCED_RE.match(second.lost_to)
    # Both labels are still present — the loser leaves its label for GC.
    assert sum(1 for l in fake.labels if l.startswith("claim:default:")) == 2


# ── Foreign-instance / cross-bot fast-fail ────────────────────────


def test_pre_get_foreign_fenced_claim_short_circuits(monkeypatch) -> None:
    fake = FakeJira(initial_labels=("claim:other:9999999999999999-deadbeef",))
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert result.ok is False
    assert result.lost_to == "claim:other:9999999999999999-deadbeef"
    assert [c[0] for c in fake.calls] == ["GET"]   # no PUT — fast-fail
    assert not any(l.startswith("claim:default:") for l in fake.labels)


def test_pre_get_foreign_assignee_short_circuits(monkeypatch) -> None:
    fake = FakeJira(initial_assignee="acc-claude-bot")
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(bot_account_id="acc-codex-bot"), "OP-555", "d")
    assert result.ok is False
    assert result.lost_to == "assignee:acc-claude-bot"


# ── Backward compat with the OP-838 bare label (test plan case 6) ─


def test_bare_legacy_label_is_treated_as_expired_and_gced(monkeypatch) -> None:
    fake = FakeJira(initial_labels=("claim:default", "tier:M"))
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert result.ok is True
    # The bare label was removed; our fenced label took its place.
    assert "claim:default" not in fake.labels
    assert any(l.startswith("claim:default:") for l in fake.labels)
    assert "tier:M" in fake.labels
    # The GC happened in the same PUT as the add.
    put = next(c for c in fake.calls if c[0] == "PUT")
    ops = put[2]["update"]["labels"]
    assert {"remove": "claim:default"} in ops


def test_foreign_bare_legacy_label_does_not_block_claim(monkeypatch) -> None:
    # A *different* instance's pre-AUDIT-24 bare label is also "expired".
    fake = FakeJira(initial_labels=("claim:legacy-other",))
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert result.ok is True
    assert "claim:legacy-other" not in fake.labels


# ── Orphan / stale fenced token sweep (test plan case 5) ──────────


def test_stale_fenced_token_is_swept_and_claim_succeeds(monkeypatch) -> None:
    # A crashed runner left a UUID-tagged label with an ancient epoch.
    ancient = "claim:default:0000000000000000001-cafef00d"
    fake = FakeJira(initial_labels=(ancient,))
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert result.ok is True
    assert ancient not in fake.labels
    assert any(l.startswith("claim:default:") and l != ancient for l in fake.labels)


def test_recent_foreign_fenced_token_is_respected(monkeypatch) -> None:
    # Same shape as above but a *recent* epoch + foreign instance → blocks.
    recent_us = jd.time.time_ns() // 1000
    fresh = f"claim:other:{recent_us:016d}-feedface"
    fake = FakeJira(initial_labels=(fresh,))
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert result.ok is False
    assert result.lost_to == fresh


# ── Eventual-consistency readback retry (test plan case 7) ────────


def test_post_get_lag_recovered_by_retry(monkeypatch) -> None:
    fake = FakeJira()
    fake.lag_gets = 2   # first two post-PUT GETs miss our label
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert result.ok is True
    # 1 pre-GET + (1 immediate + 2 retried) post-GETs == 4 GETs total.
    assert [c[0] for c in fake.calls].count("GET") == 4


def test_post_get_lag_exceeds_bound_returns_loss(monkeypatch) -> None:
    fake = FakeJira()
    fake.lag_gets = 99   # never converges within the retry budget
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert result.ok is False
    assert result.lost_to == "claim-label-missing-from-readback"


# ── Idempotent re-pickup / runner restart ─────────────────────────


def test_runner_restart_with_own_stale_token_reclaims(monkeypatch) -> None:
    # Prior runner crashed long ago leaving our instance's own ancient
    # fenced label — treated as expired, swept, re-claimed cleanly.
    ours_old = "claim:default:0000000000000000005-0badf00d"
    fake = FakeJira(initial_assignee="acc-codex", initial_labels=(ours_old, "tier:M"))
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(bot_account_id="acc-codex"), "OP-555", "default")
    assert result.ok is True
    assert ours_old not in fake.labels


# ── release_ticket_claim GC (AC #4) ──────────────────────────────


def test_release_removes_all_own_instance_claim_labels(monkeypatch) -> None:
    fake = FakeJira(initial_labels=(
        "claim:default:0000000000000000010-aaaaaaaa",   # winner token
        "claim:default:0000000000000000011-bbbbbbbb",   # same-instance loser leftover
        "claim:default",                                  # legacy bare — also ours
        "claim:other:0000000000000000012-cccccccc",     # foreign — keep
        "tier:M",
    ))
    _install_fake(monkeypatch, fake)
    jd.release_ticket_claim(_fake_client(), "OP-555", "default",
                            token="0000000000000000010-aaaaaaaa")
    assert not any(l.startswith("claim:default") for l in fake.labels)
    assert "claim:other:0000000000000000012-cccccccc" in fake.labels
    assert "tier:M" in fake.labels


def test_release_no_op_when_no_matching_claim(monkeypatch) -> None:
    fake = FakeJira(initial_labels=("tier:M", "claim:other:0000000000000000001-aaaaaaaa"))
    _install_fake(monkeypatch, fake)
    jd.release_ticket_claim(_fake_client(), "OP-555", "default")
    # Only the GET happened — nothing to remove → no PUT.
    assert [c[0] for c in fake.calls] == ["GET"]
    assert "claim:other:0000000000000000001-aaaaaaaa" in fake.labels


def test_release_falls_back_to_single_label_when_get_fails(monkeypatch) -> None:
    calls: list[str] = []

    def flaky(client, method, path, body=None):
        calls.append(method)
        if method == "GET":
            raise RuntimeError("connection reset")
        return {}

    monkeypatch.setattr(jd, "_request", flaky)
    jd.release_ticket_claim(_fake_client(), "OP-555", "default", token="0000000000000000010-aaaaaaaa")
    # GET failed → fell back to a single targeted remove PUT.
    assert calls == ["GET", "PUT"]


# ── Transport-error wrapping ──────────────────────────────────────


def test_pre_get_failure_raises_runner_mutex_api_error(monkeypatch) -> None:
    monkeypatch.setattr(jd, "_request", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("refused")))
    with pytest.raises(jd.RunnerMutexAPIError) as ei:
        jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert ei.value.key == "OP-555" and ei.value.step == "pre-GET"


def test_put_failure_raises_runner_mutex_api_error(monkeypatch) -> None:
    calls: list[str] = []

    def maybe_boom(client, method, path, body=None):
        calls.append(method)
        if method == "PUT":
            raise RuntimeError("503")
        return {"fields": {"assignee": None, "labels": []}}

    monkeypatch.setattr(jd, "_request", maybe_boom)
    with pytest.raises(jd.RunnerMutexAPIError) as ei:
        jd.claim_ticket_atomic(_fake_client(), "OP-555", "default")
    assert ei.value.step == "PUT"
    assert calls.count("GET") == 1   # no post-GET after a PUT failure


# ── ClaimResult / exception export ───────────────────────────────


def test_claim_result_is_frozen_dataclass() -> None:
    r = jd.ClaimResult(ok=True, lost_to=None, claim_token="default:0001715-ab")
    assert (r.ok, r.lost_to, r.claim_token) == (True, None, "default:0001715-ab")
    with pytest.raises(Exception):
        r.ok = False  # type: ignore[misc]


def test_runner_mutex_lost_carries_diagnostics() -> None:
    err = jd.RunnerMutexLost(key="OP-977", claim_token="d:0001-ab", observed_token="claim:d:0000-cd")
    assert err.key == "OP-977"
    assert "OP-977" in str(err) and "0001-ab" in str(err) and "0000-cd" in str(err)


# ── Rollback flag → legacy OP-838 path ───────────────────────────


def test_legacy_flag_routes_to_bare_label_path(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY", "1")
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    result = jd.claim_ticket_atomic(_fake_client(bot_account_id="acc-codex"), "OP-555", "default")
    assert result.ok is True
    # Legacy path writes the *bare* label, not a fenced token.
    assert "claim:default" in fake.labels
    assert not any(_FENCED_RE.match(l) for l in fake.labels)


@pytest.mark.parametrize("val,expect_legacy", [
    ("", False), ("0", False), ("false", False), ("no", False),
    ("1", True), ("true", True), ("yes", True), ("anything", True),
])
def test_legacy_claim_mode_env_parsing(monkeypatch, val, expect_legacy) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY", val)
    assert jd._legacy_claim_mode() is expect_legacy
