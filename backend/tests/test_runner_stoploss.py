"""OP-1140: per-ticket §11-revert circuit-breaker contract tests.

Four scenarios required by the ticket's Code AC:

1. Under threshold        — pickup still allowed.
2. Over threshold         — pickup refused, ONE terminal comment posted.
3. Threshold window reset — old revert labels age out of the window.
4. Manual operator reset  — stripping the circuit-tripped label re-arms pickup.

A thin :class:`FakeJira` mirrors the bits ``register_revert`` and
``pre_pickup_stoploss_ok`` actually touch (label add/remove + comment
appends + a labels-only ``GET``) so the suite stays offline.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd
from backend.agents import runner_stoploss as rs


# ── FakeJira: in-memory ticket state ──────────────────────────────


class FakeJira:
    """In-memory JIRA stand-in covering the ``_request`` surface used by
    ``runner_stoploss.register_revert`` (PUT labels, POST comment, GET
    labels). The shape mirrors the FakeJira in ``test_jira_dispatch_mutex``
    so future readers can follow the same convention."""

    def __init__(self, *, initial_labels: tuple[str, ...] = ()) -> None:
        self._lock = threading.Lock()
        self.labels: set[str] = set(initial_labels)
        self.comments: list[str] = []
        self.calls: list[tuple[str, str, dict | None]] = []

    def _snapshot(self) -> dict:
        return {"fields": {"labels": sorted(self.labels)}}

    def request(self, client, method: str, path: str,
                body: dict | None = None) -> dict:
        with self._lock:
            self.calls.append((method, path, body))
            if method == "GET" and "/issue/" in path:
                return self._snapshot()
            if method == "PUT" and "/issue/" in path:
                for op in ((body or {}).get("update") or {}).get("labels", []):
                    if "add" in op:
                        self.labels.add(op["add"])
                    if "remove" in op:
                        self.labels.discard(op["remove"])
            if method == "POST" and "/comment" in path:
                text = _extract_comment_text(body or {})
                self.comments.append(text)
        return {}


def _extract_comment_text(body: dict) -> str:
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                chunks.append(node.get("text", ""))
            for c in node.get("content", []) or []:
                walk(c)
        elif isinstance(node, list):
            for c in node:
                walk(c)

    walk(body.get("body"))
    return "".join(chunks)


def _fake_client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-bot",
        bot_email="bot@example.invalid",
    )


def _install_fake(monkeypatch, fake: FakeJira) -> None:
    monkeypatch.setattr(jd, "_request", fake.request)
    monkeypatch.setattr(
        jd, "_request_idempotent",
        lambda client, method, path, body, idem_key: fake.request(client, method, path, body),
    )


def _now() -> datetime:
    return datetime(2026, 5, 16, 14, 30, 0, tzinfo=timezone.utc)


def _tripped_labels(labels: Iterable[str]) -> list[str]:
    return [l for l in labels if l.startswith(rs.TRIPPED_LABEL_PREFIX)]


def _revert_labels(labels: Iterable[str]) -> list[str]:
    return [l for l in labels if l.startswith(rs.REVERT_LABEL_PREFIX)]


# ── Pure-logic primitives ─────────────────────────────────────────


def test_revert_label_round_trips_compact_iso() -> None:
    now = _now()
    label = rs.revert_label_for(now)
    assert label == "runner-stoploss:revert-20260516T143000Z"
    # The label parses back through the count window check; same minute
    # is inside any positive window.
    assert rs.count_recent_reverts([label], now, window_min=1) == 1


def test_count_recent_reverts_ignores_unparseable_and_foreign_labels() -> None:
    now = _now()
    labels = [
        rs.revert_label_for(now),
        "runner-stoploss:revert-not-a-timestamp",
        "tier:S",
        "area:backend",
        rs.tripped_label_for(now),  # tripped is not a revert label
    ]
    assert rs.count_recent_reverts(labels, now, window_min=15) == 1


def test_current_config_handles_bad_env(monkeypatch) -> None:
    monkeypatch.setenv(rs.WINDOW_MIN_ENV, "not-an-int")
    monkeypatch.setenv(rs.THRESHOLD_ENV, "  ")
    assert rs.current_config() == (rs.DEFAULT_WINDOW_MIN, rs.DEFAULT_THRESHOLD)

    monkeypatch.setenv(rs.WINDOW_MIN_ENV, "0")
    monkeypatch.setenv(rs.THRESHOLD_ENV, "-5")
    # Non-positive values must also fall back — an operator typo cannot
    # silently disable the stoploss.
    assert rs.current_config() == (rs.DEFAULT_WINDOW_MIN, rs.DEFAULT_THRESHOLD)

    monkeypatch.setenv(rs.WINDOW_MIN_ENV, "30")
    monkeypatch.setenv(rs.THRESHOLD_ENV, "5")
    assert rs.current_config() == (30, 5)


def test_pre_pickup_stoploss_ok_passes_clean_labels() -> None:
    ok, reason = rs.pre_pickup_stoploss_ok(["tier:S", "area:backend"])
    assert ok is True
    assert reason == "stoploss-ok"


def test_pre_pickup_stoploss_ok_refuses_when_circuit_tripped() -> None:
    tripped = rs.tripped_label_for(_now())
    ok, reason = rs.pre_pickup_stoploss_ok(["tier:S", tripped, "area:backend"])
    assert ok is False
    assert reason.startswith("runner_stoploss_circuit_tripped:")
    assert tripped in reason


# ── Scenario 1: under threshold (allows pickup) ────────────────────


def test_under_threshold_does_not_trip(monkeypatch) -> None:
    """2 reverts in a 15-minute window with threshold=3 must NOT trip."""
    fake = FakeJira(initial_labels=("tier:S", "area:backend"))
    _install_fake(monkeypatch, fake)
    client = _fake_client()

    now = _now()
    out1 = rs.register_revert(
        client, "OP-1140", labels=sorted(fake.labels), now=now,
        window_min=15, threshold=3,
    )
    assert out1.tripped_now is False
    assert out1.count == 1

    out2 = rs.register_revert(
        client, "OP-1140", labels=sorted(fake.labels),
        now=now + timedelta(minutes=5),
        window_min=15, threshold=3,
    )
    assert out2.tripped_now is False
    assert out2.count == 2

    assert _tripped_labels(fake.labels) == []
    assert len(_revert_labels(fake.labels)) == 2
    # No terminal comment posted while under threshold.
    assert fake.comments == []

    # Pre-pickup gate keeps allowing this ticket.
    ok, _ = rs.pre_pickup_stoploss_ok(sorted(fake.labels))
    assert ok is True


# ── Scenario 2: over threshold (refuses pickup, ONE comment) ───────


def test_over_threshold_trips_circuit_and_blocks_pickup(monkeypatch) -> None:
    """3 reverts inside the window adds the tripped label, posts ONE
    terminal comment, and the pre-pickup gate refuses the next pickup."""
    fake = FakeJira(initial_labels=("tier:S", "area:backend"))
    _install_fake(monkeypatch, fake)
    client = _fake_client()

    now = _now()
    for i in range(3):
        rs.register_revert(
            client, "OP-1140", labels=sorted(fake.labels),
            now=now + timedelta(minutes=i * 2),
            window_min=15, threshold=3,
        )

    tripped = _tripped_labels(fake.labels)
    assert len(tripped) == 1, f"exactly one tripped label expected, got {tripped}"
    assert len(_revert_labels(fake.labels)) == 3

    # Exactly ONE terminal comment, mentioning the threshold + label.
    assert len(fake.comments) == 1
    body = fake.comments[0]
    assert "[runner-stoploss]" in body
    assert "Circuit tripped" in body
    assert "threshold=3" in body
    assert "15 minutes" in body
    assert tripped[0] in body

    # Pre-pickup gate now refuses this ticket.
    ok, reason = rs.pre_pickup_stoploss_ok(sorted(fake.labels))
    assert ok is False
    assert tripped[0] in reason

    # A 4th revert inside the same trip must NOT post a second comment
    # (the existing tripped label suppresses re-tripping).
    rs.register_revert(
        client, "OP-1140", labels=sorted(fake.labels),
        now=now + timedelta(minutes=10),
        window_min=15, threshold=3,
    )
    assert len(fake.comments) == 1, "tripped circuit must not re-comment"
    assert len(_tripped_labels(fake.labels)) == 1


# ── Scenario 3: window reset (old reverts age out) ─────────────────


def test_threshold_resets_after_window_expires(monkeypatch) -> None:
    """Two reverts inside the window plus one well outside it = count of 2,
    no trip. Old reverts must NOT keep the circuit forever-armed."""
    fake = FakeJira(initial_labels=("tier:S", "area:backend"))
    _install_fake(monkeypatch, fake)
    client = _fake_client()

    base = _now()
    # First revert one hour ago — outside any 15-min window from the
    # later reverts.
    rs.register_revert(
        client, "OP-1140", labels=sorted(fake.labels),
        now=base - timedelta(hours=1),
        window_min=15, threshold=3,
    )
    # Two recent reverts inside the window.
    rs.register_revert(
        client, "OP-1140", labels=sorted(fake.labels),
        now=base, window_min=15, threshold=3,
    )
    out = rs.register_revert(
        client, "OP-1140", labels=sorted(fake.labels),
        now=base + timedelta(minutes=5),
        window_min=15, threshold=3,
    )

    # Three total revert labels on the ticket but the trailing 15-min
    # window only contains two — must NOT trip.
    assert len(_revert_labels(fake.labels)) == 3
    assert out.tripped_now is False
    assert out.count == 2
    assert _tripped_labels(fake.labels) == []
    assert fake.comments == []


# ── Scenario 4: manual operator reset ──────────────────────────────


def test_manual_reset_via_stripping_tripped_label(monkeypatch) -> None:
    """Operator strips the ``circuit-tripped-*`` label → next pickup proceeds.
    The revert labels stay (they're audit history); the trip is what gates
    pickup, and the operator alone clears it."""
    fake = FakeJira(initial_labels=("tier:S", "area:backend"))
    _install_fake(monkeypatch, fake)
    client = _fake_client()

    now = _now()
    for i in range(3):
        rs.register_revert(
            client, "OP-1140", labels=sorted(fake.labels),
            now=now + timedelta(minutes=i * 2),
            window_min=15, threshold=3,
        )
    tripped = _tripped_labels(fake.labels)
    assert len(tripped) == 1
    ok, _ = rs.pre_pickup_stoploss_ok(sorted(fake.labels))
    assert ok is False

    # Operator manual reset = strip the tripped label.
    jd.remove_label(client, "OP-1140", tripped[0])
    assert _tripped_labels(fake.labels) == []

    # Pickup gate now allows the ticket again — the revert audit
    # labels remain but they do not gate pickup on their own.
    ok, reason = rs.pre_pickup_stoploss_ok(sorted(fake.labels))
    assert ok is True
    assert reason == "stoploss-ok"
    assert len(_revert_labels(fake.labels)) == 3
