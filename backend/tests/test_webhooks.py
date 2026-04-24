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
