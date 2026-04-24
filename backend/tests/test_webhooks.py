"""Tests for backend/routers/webhooks.py — Gerrit webhook event handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest


class TestWebhookEndpoint:

    @pytest.mark.asyncio
    async def test_gerrit_disabled_returns_503(self, client):
        """Webhook returns 503 when Gerrit is disabled."""
        from backend.config import settings
        original = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = False
            res = await client.post("/api/v1/webhooks/gerrit", json={"type": "test"})
            assert res.status_code == 503
        finally:
            settings.gerrit_enabled = original

    @pytest.mark.asyncio
    async def test_gerrit_enabled_accepts_event(self, client):
        """Webhook returns 200 when Gerrit is enabled."""
        from backend.config import settings
        original = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = True
            res = await client.post("/api/v1/webhooks/gerrit", json={"type": "unknown-event"})
            assert res.status_code == 200
            data = res.json()
            assert data["event"] == "unknown-event"
        finally:
            settings.gerrit_enabled = original

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, client):
        from backend.config import settings
        original = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = True
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert res.status_code == 400
        finally:
            settings.gerrit_enabled = original


# ──────────────────────────────────────────────────────────────────────
# Y-prep.1 (#287) — Gerrit event dispatcher routing contract
# ──────────────────────────────────────────────────────────────────────
#
# TestWebhookEndpoint above only covers the HMAC + gerrit_enabled gate.
# The three event types (patchset-created / comment-added /
# change-merged) at webhooks.py:109-116 route to three distinct
# handlers, and production has been relying on debug-log observation
# as a temporary regression signal. These tests lock the dispatcher
# mapping so any future refactor (e.g. registry-based dispatch) can
# verify the contract.
#
# Design:
#   - Each test mocks all three handlers, posts a single event type,
#     and asserts exactly-one handler received the call. This doubles
#     as a negative case per test: posting `patchset-created` must not
#     fire `_on_change_merged`, etc.
#   - HMAC-SHA256 signing uses a real secret + real digest so the
#     signature-verifier code path at webhooks.py:66-72 is exercised
#     end-to-end alongside the handler dispatch. A fully-mocked
#     signature would leave the verifier/dispatcher desynchronised if
#     one side's contract shifts silently.
#   - The dispatcher calls ``_on_patchset_created(conn, body)`` with an
#     asyncpg conn as first positional arg, while ``_on_comment_added``
#     and ``_on_change_merged`` take only the body dict. The assertions
#     account for this asymmetry.


@contextmanager
def _gerrit_enabled_with_secret(secret: str):
    """Temporarily enable gerrit + pin a scalar webhook secret.

    Settings is a module-global Pydantic model — use this context
    manager to guarantee teardown restores the pre-test values even
    if the test body raises.
    """
    from backend.config import settings
    orig_enabled = settings.gerrit_enabled
    orig_secret = settings.gerrit_webhook_secret
    try:
        settings.gerrit_enabled = True
        settings.gerrit_webhook_secret = secret
        yield
    finally:
        settings.gerrit_enabled = orig_enabled
        settings.gerrit_webhook_secret = orig_secret


def _sign(secret: str, raw: bytes) -> str:
    """Compute the X-Gerrit-Signature header value for a raw body."""
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class TestGerritEventRouting:
    """Contract tests: POST /webhooks/gerrit routes each event type to
    the matching handler exactly once.

    Covers Y-prep.1 (#287). See the block comment above for the full
    rationale.
    """

    @pytest.mark.asyncio
    async def test_patchset_created_dispatches_to_on_patchset_created(
        self, client, monkeypatch,
    ):
        """`patchset-created` payload → `_on_patchset_created(conn, body)`
        called exactly once with the parsed event; sibling handlers
        must not fire."""
        from backend.routers import webhooks

        mock_patchset = AsyncMock()
        mock_comment = AsyncMock()
        mock_merged = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_patchset_created", mock_patchset)
        monkeypatch.setattr(webhooks, "_on_comment_added", mock_comment)
        monkeypatch.setattr(webhooks, "_on_change_merged", mock_merged)

        secret = "test-gerrit-secret-patchset"
        body = {
            "type": "patchset-created",
            "change": {"id": "I0123abc", "subject": "Add feature X"},
            "patchSet": {
                "revision": "abcdef0123456789",
                "uploader": {"name": "alice"},
            },
        }
        raw = json.dumps(body).encode()

        with _gerrit_enabled_with_secret(secret):
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Gerrit-Signature": _sign(secret, raw),
                },
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok", "event": "patchset-created"}

        mock_patchset.assert_called_once()
        # Signature: (conn, event_dict). conn comes from the pool
        # dependency — we don't assert its identity, only that the
        # parsed body is passed through as the second positional arg.
        call = mock_patchset.call_args
        assert len(call.args) == 2
        assert call.args[1] == body

        mock_comment.assert_not_called()
        mock_merged.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_added_dispatches_to_on_comment_added(
        self, client, monkeypatch,
    ):
        """`comment-added` payload → `_on_comment_added(body)` called
        exactly once with the parsed event; sibling handlers must not
        fire."""
        from backend.routers import webhooks

        mock_patchset = AsyncMock()
        mock_comment = AsyncMock()
        mock_merged = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_patchset_created", mock_patchset)
        monkeypatch.setattr(webhooks, "_on_comment_added", mock_comment)
        monkeypatch.setattr(webhooks, "_on_change_merged", mock_merged)

        secret = "test-gerrit-secret-comment"
        body = {
            "type": "comment-added",
            "change": {"id": "I0456def", "subject": "Fix bug Y"},
            "approvals": [
                {"type": "Code-Review", "value": "-1", "message": "nit"},
            ],
            "comment": "Please revisit.",
        }
        raw = json.dumps(body).encode()

        with _gerrit_enabled_with_secret(secret):
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Gerrit-Signature": _sign(secret, raw),
                },
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok", "event": "comment-added"}

        mock_comment.assert_called_once()
        call = mock_comment.call_args
        assert len(call.args) == 1
        assert call.args[0] == body

        mock_patchset.assert_not_called()
        mock_merged.assert_not_called()

    @pytest.mark.asyncio
    async def test_change_merged_dispatches_to_on_change_merged(
        self, client, monkeypatch,
    ):
        """`change-merged` payload → `_on_change_merged(body)` called
        exactly once with the parsed event; sibling handlers must not
        fire."""
        from backend.routers import webhooks

        mock_patchset = AsyncMock()
        mock_comment = AsyncMock()
        mock_merged = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_patchset_created", mock_patchset)
        monkeypatch.setattr(webhooks, "_on_comment_added", mock_comment)
        monkeypatch.setattr(webhooks, "_on_change_merged", mock_merged)

        secret = "test-gerrit-secret-merged"
        body = {
            "type": "change-merged",
            "change": {
                "id": "I0789abc",
                "subject": "Release 1.2.3",
                "commitMessage": "Release 1.2.3\n\nBug: 42",
            },
        }
        raw = json.dumps(body).encode()

        with _gerrit_enabled_with_secret(secret):
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Gerrit-Signature": _sign(secret, raw),
                },
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok", "event": "change-merged"}

        mock_merged.assert_called_once()
        call = mock_merged.call_args
        assert len(call.args) == 1
        assert call.args[0] == body

        mock_patchset.assert_not_called()
        mock_comment.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Y-prep.1 (#287) — Explicit negative-case coverage for the dispatcher
# ──────────────────────────────────────────────────────────────────────
#
# The positive tests in TestGerritEventRouting above already assert
# that sibling handlers are NOT called when a given event type fires
# ("each test doubles as a negative case for the other two events").
# That embedded coverage is fragile: a future refactor that, say,
# extracts the dispatcher into a registry and drops the ``assert_not_called``
# lines while updating the positive assertions would silently lose the
# mismatch contract.
#
# These tests promote the negative contract to first-class, parametrised
# tests so the intent is self-evident and survives refactors:
#
#   - For every (posted_event_type, forbidden_handler) pair where
#     ``posted_event_type`` is NOT the one that should route to
#     ``forbidden_handler``, assert the handler is not called. 6 pairs
#     total: 3 event types × 2 non-matching handlers each.
#   - Additionally, unknown event types (e.g. ``ref-updated``) must
#     accept the request (200) but fire NO handler — the else-branch
#     at webhooks.py:115-116 is logged-and-dropped, and we lock that
#     contract here.


_EVENT_PAYLOADS = {
    "patchset-created": {
        "type": "patchset-created",
        "change": {"id": "Ineg01", "subject": "neg patchset"},
        "patchSet": {"revision": "deadbeefcafef00d",
                     "uploader": {"name": "bob"}},
    },
    "comment-added": {
        "type": "comment-added",
        "change": {"id": "Ineg02", "subject": "neg comment"},
        "approvals": [{"type": "Code-Review", "value": "-1"}],
        "comment": "negative-case body",
    },
    "change-merged": {
        "type": "change-merged",
        "change": {"id": "Ineg03", "subject": "neg merged"},
    },
}

# (posted_event_type, forbidden_handler_attr) — every pair where the
# posted event type must NOT route to the named handler.
_MISMATCH_PAIRS = [
    ("patchset-created", "_on_comment_added"),
    ("patchset-created", "_on_change_merged"),
    ("comment-added",    "_on_patchset_created"),
    ("comment-added",    "_on_change_merged"),
    ("change-merged",    "_on_patchset_created"),
    ("change-merged",    "_on_comment_added"),
]


class TestGerritEventRoutingNegativeCases:
    """Explicit negative-case contract: a payload of event type X must
    NOT trigger a handler registered for a different event type Y.

    Covers Y-prep.1 (#287). See the block comment above for rationale.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "posted_event,forbidden_handler",
        _MISMATCH_PAIRS,
        ids=[f"{p}->not_{h}" for p, h in _MISMATCH_PAIRS],
    )
    async def test_mismatched_event_does_not_trigger_handler(
        self, client, monkeypatch, posted_event, forbidden_handler,
    ):
        """Post ``posted_event``; assert ``forbidden_handler`` is NOT
        called. All three handlers are mocked so the forbidden one is
        observable even if production code were to fan out a matching
        event to multiple handlers."""
        from backend.routers import webhooks

        mock_patchset = AsyncMock()
        mock_comment = AsyncMock()
        mock_merged = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_patchset_created", mock_patchset)
        monkeypatch.setattr(webhooks, "_on_comment_added", mock_comment)
        monkeypatch.setattr(webhooks, "_on_change_merged", mock_merged)

        handler_mocks = {
            "_on_patchset_created": mock_patchset,
            "_on_comment_added": mock_comment,
            "_on_change_merged": mock_merged,
        }

        secret = f"test-secret-neg-{posted_event}-{forbidden_handler}"
        body = _EVENT_PAYLOADS[posted_event]
        raw = json.dumps(body).encode()

        with _gerrit_enabled_with_secret(secret):
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Gerrit-Signature": _sign(secret, raw),
                },
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok", "event": posted_event}
        # The negative assertion — the whole point of this test.
        handler_mocks[forbidden_handler].assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_event_triggers_no_handler(
        self, client, monkeypatch,
    ):
        """An unknown event type (e.g. ``ref-updated``) must accept the
        request but fire none of the three handlers — locks the
        else-branch at webhooks.py:115-116."""
        from backend.routers import webhooks

        mock_patchset = AsyncMock()
        mock_comment = AsyncMock()
        mock_merged = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_patchset_created", mock_patchset)
        monkeypatch.setattr(webhooks, "_on_comment_added", mock_comment)
        monkeypatch.setattr(webhooks, "_on_change_merged", mock_merged)

        secret = "test-secret-neg-unknown-event"
        body = {
            "type": "ref-updated",
            "refUpdate": {
                "oldRev": "0" * 40,
                "newRev": "a" * 40,
                "refName": "refs/heads/main",
                "project": "omnisight",
            },
        }
        raw = json.dumps(body).encode()

        with _gerrit_enabled_with_secret(secret):
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Gerrit-Signature": _sign(secret, raw),
                },
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok", "event": "ref-updated"}
        mock_patchset.assert_not_called()
        mock_comment.assert_not_called()
        mock_merged.assert_not_called()
