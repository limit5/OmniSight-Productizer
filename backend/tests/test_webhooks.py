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
    """Compute the X-Gerrit-Signature header value for a raw body.

    Uses the same HMAC-SHA256 construction as ``backend/routers/webhooks.py:67``
    — keeping the test path and the prod verifier on the same primitive
    is the whole point of "verifier and handler validated in sync"
    (Y-prep.1 #287, fourth sub-bullet). Any drift between this helper
    and the verifier (e.g. someone swaps the prod side to SHA512 or
    base64 hex-encoding) shows up as a 401 here, not a silently-skipped
    verifier. Don't replace with a Mock — the assertion that this is
    the real primitive is what gives ``gerrit_sign`` fixture its
    contract value.
    """
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


@pytest.fixture
def gerrit_sign():
    """pytest fixture wrapping the real HMAC-SHA256 signer.

    Y-prep.1 #287 fourth sub-bullet — promote the signing helper to a
    first-class pytest fixture so test code expresses intent at the
    call site (``signed = gerrit_sign(secret, raw)``) and so the
    "tests sign with REAL HMAC, never with a Mock" contract is visible
    in the fixture surface, not buried in a module helper. The
    underlying ``_sign`` stays in place for the existing test classes
    above so this row is purely additive.
    """
    return _sign


@pytest.fixture
def gerrit_signed_post(client, gerrit_sign):
    """High-level fixture: POST a JSON body to /webhooks/gerrit with a
    correctly-signed ``X-Gerrit-Signature`` header.

    Returns an ``async`` callable ``(secret, body, *, signature=None,
    enable=True)`` that:

      - Serialises ``body`` to JSON, computes the real HMAC-SHA256
        digest with ``secret`` (or accepts an override ``signature``
        for negative-path tests that want a tampered/missing/wrong
        signature),
      - Pins ``settings.gerrit_enabled = True`` and
        ``settings.gerrit_webhook_secret = secret`` for the duration of
        the call (when ``enable`` is True),
      - Returns the response object so the caller can assert status
        + body shape.

    Why a fixture and not just a helper: the verifier and handler must
    be validated in lock-step (Y-prep.1 #287 fourth sub-bullet). Tests
    that go through this fixture cannot accidentally bypass the
    verifier — every POST exercises the
    ``webhooks.py:66-104`` signature path before hitting the dispatcher
    at ``webhooks.py:109-118``. Having a single fixture surface also
    makes it visible at code-review time which tests are signed
    correctly vs. which are deliberately exercising the failure
    modes.
    """
    async def _post(
        secret: str,
        body: dict,
        *,
        signature: str | None = None,
        enable: bool = True,
        omit_signature: bool = False,
    ):
        raw = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if not omit_signature:
            headers["X-Gerrit-Signature"] = (
                signature if signature is not None
                else gerrit_sign(secret, raw)
            )
        if enable:
            with _gerrit_enabled_with_secret(secret):
                return await client.post(
                    "/api/v1/webhooks/gerrit",
                    content=raw,
                    headers=headers,
                )
        return await client.post(
            "/api/v1/webhooks/gerrit",
            content=raw,
            headers=headers,
        )
    return _post


class TestGerritEventRouting:
    """Contract tests: POST /webhooks/gerrit routes each event type to
    the matching handler exactly once.

    Covers Y-prep.1 (#287). See the block comment above for the full
    rationale.

    All three tests carry the ``p0`` marker (Y-prep.1 #287 fifth
    sub-bullet — "CI gate：這三個測試加到 pytest.ini 的 p0 marker，
    不准 skip"). The no-skip enforcement is wired in
    ``backend/tests/conftest.py`` — see the docstring on
    ``pytest_collection_modifyitems`` / ``pytest_runtest_makereport``
    there for the exact contract.
    """

    @pytest.mark.p0
    @pytest.mark.asyncio
    async def test_patchset_created_dispatches_to_on_patchset_created(
        self, client, monkeypatch,
    ):
        """`patchset-created` payload → `_on_patchset_created(conn, body)`
        called exactly once with the parsed event; sibling handlers
        must not fire.

        Y-prep.1 #287 CI gate — this test is part of the ``p0`` marker
        set. The conftest enforcement hook fails collection if a
        ``@pytest.mark.skip`` / ``@pytest.mark.skipif(True, ...)`` lands
        on this test, and converts an inline ``pytest.skip()`` outcome
        into a failure. Do not skip this test locally to silence a
        flake — fix the root cause or talk to the team instead."""
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

    @pytest.mark.p0
    @pytest.mark.asyncio
    async def test_comment_added_dispatches_to_on_comment_added(
        self, client, monkeypatch,
    ):
        """`comment-added` payload → `_on_comment_added(body)` called
        exactly once with the parsed event; sibling handlers must not
        fire.

        Y-prep.1 #287 CI gate — see
        ``test_patchset_created_dispatches_to_on_patchset_created`` for
        the p0-no-skip policy."""
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

    @pytest.mark.p0
    @pytest.mark.asyncio
    async def test_change_merged_dispatches_to_on_change_merged(
        self, client, monkeypatch,
    ):
        """`change-merged` payload → `_on_change_merged(body)` called
        exactly once with the parsed event; sibling handlers must not
        fire.

        Y-prep.1 #287 CI gate — see
        ``test_patchset_created_dispatches_to_on_patchset_created`` for
        the p0-no-skip policy."""
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


# ──────────────────────────────────────────────────────────────────────
# Y-prep.1 (#287) — Handler-internal boundary contracts
# ──────────────────────────────────────────────────────────────────────
#
# The two dispatcher-level test classes above lock the mapping
# (event_type → handler). These classes exercise the REAL handlers and
# lock two business-logic invariants that live one level down:
#
#   1. ``_on_comment_added`` files a "Code-Review fix" task ONLY when
#      the approval carries ``Code-Review: -1``. Any other value
#      (``+1``, ``0``, ``+2``, missing) must NOT create a task —
#      otherwise every neutral/approving comment would spawn a noisy
#      fix-task queue.
#
#   2. ``_on_change_merged`` fans out to ``git push <target>`` ONLY
#      when ``gerrit_replication_targets`` parses to a non-empty list.
#      Empty string, pure whitespace, or a CSV of only-whitespace
#      entries (``", ,  "``) must be skipped BEFORE ``_run`` is called
#      — a stray ``git push ""`` would be a cryptic failure in prod
#      and, depending on git's arg parsing, could push to a default
#      remote by accident.
#
# Design notes:
#   - These tests let the REAL dispatcher + REAL handlers run, but
#     mock the leaf side-effects (``_persist_task``, ``workspace._run``,
#     ``notify``, ``intent_bridge.on_gerrit_change_merged``, and the
#     three ``asyncio.create_task`` worker stubs) so we can observe
#     the boundary cleanly without needing an artifacts dir or a git
#     repo on disk.
#   - HMAC-SHA256 signing stays end-to-end (same invariant as
#     TestGerritEventRouting) so the signature verifier and the
#     handler are still tested in lock-step.


class TestGerritCommentAddedReviewBoundary:
    """Boundary contract for ``_on_comment_added``: a fix task must be
    filed iff the approval value is exactly ``-1``.

    Covers Y-prep.1 (#287) third sub-bullet — "``comment-added`` 只在
    ``Code-Review: -1`` 時 file task，``+1`` 不該". See the block
    comment above for rationale.
    """

    @staticmethod
    def _build_payload(cr_value: str | int) -> dict:
        return {
            "type": "comment-added",
            "change": {"id": "Iboundary-cr", "subject": "boundary: Code-Review value"},
            "approvals": [
                {"type": "Code-Review", "value": cr_value, "message": f"CR={cr_value}"},
            ],
            "comment": f"Boundary test for Code-Review={cr_value}.",
        }

    @staticmethod
    def _install_common_mocks(monkeypatch) -> AsyncMock:
        """Mock the leaf side-effects of ``_on_comment_added`` and
        return the ``_persist_task`` mock — the one we assert against.

        ``notify`` is mocked to avoid an incidental DB-insert + SSE
        publish from the underlying notification pipeline; this test
        is strictly about the Code-Review value boundary, not the
        notification fanout."""
        from backend.routers import tasks as _tasks_router
        from backend import notifications as _notifs

        mock_persist = AsyncMock()
        monkeypatch.setattr(_tasks_router, "_persist", mock_persist)
        # notify is called via ``asyncio.create_task(notify(...))`` —
        # its execution is not awaited by _on_comment_added, but the
        # task may resolve during test teardown. Mock it out so we
        # don't depend on notify's DB-insert side effects.
        monkeypatch.setattr(_notifs, "notify", AsyncMock())
        return mock_persist

    @pytest.mark.asyncio
    async def test_code_review_minus_one_files_fix_task(
        self, client, monkeypatch,
    ):
        """``Code-Review: -1`` MUST create exactly one fix task and the
        task's ``external_issue_id`` MUST carry the Gerrit change id so
        downstream pipelines can cross-reference."""
        mock_persist = self._install_common_mocks(monkeypatch)

        secret = "boundary-secret-cr-minus-one"
        body = self._build_payload("-1")
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
        mock_persist.assert_called_once()
        # First positional arg is the Task; verify the change id is
        # threaded through so the task is linkable back to Gerrit.
        filed_task = mock_persist.call_args.args[0]
        assert filed_task.external_issue_id == body["change"]["id"]
        # Label set by the handler — locks the contract so intake
        # queries can filter on ``gerrit-review-fix``.
        assert "gerrit-review-fix" in (filed_task.labels or [])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cr_value",
        ["+1", "1", "0", "+2", "2"],
        ids=["plus_one_str", "plus_one_int_like", "zero", "plus_two_str", "plus_two_int_like"],
    )
    async def test_non_minus_one_does_not_file_fix_task(
        self, client, monkeypatch, cr_value,
    ):
        """Any ``Code-Review`` value other than ``-1`` MUST NOT file a
        fix task. Covers ``+1`` explicitly (the case named in the
        bullet) plus ``0`` / ``+2`` as sanity neighbours so any future
        refactor that, say, accidentally triggers on "non-positive"
        values will break the test."""
        mock_persist = self._install_common_mocks(monkeypatch)

        secret = f"boundary-secret-cr-{cr_value}"
        body = self._build_payload(cr_value)
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
        mock_persist.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_approvals_does_not_file_fix_task(
        self, client, monkeypatch,
    ):
        """A ``comment-added`` payload with no ``approvals`` array
        (pure comment, no review vote) MUST NOT file a fix task. Locks
        the ``for approval in approvals`` loop never entering when the
        list is missing — otherwise a stray ``approval.get("type") is
        None`` path could regress into a false-positive."""
        mock_persist = self._install_common_mocks(monkeypatch)

        secret = "boundary-secret-no-approvals"
        body = {
            "type": "comment-added",
            "change": {"id": "Iboundary-no-appr", "subject": "plain comment"},
            # approvals key intentionally absent
            "comment": "Just a comment, no review vote.",
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
        mock_persist.assert_not_called()


class TestGerritChangeMergedReplicationTargetsBoundary:
    """Boundary contract for ``_on_change_merged``: replication fan-out
    (``git remote get-url`` + ``git push``) must only fire when
    ``gerrit_replication_targets`` parses to at least one non-empty
    target after ``split(",")`` + ``strip()`` filtering.

    Covers Y-prep.1 (#287) third sub-bullet — "``change-merged`` 只在
    ``gerrit_replication_targets`` 非空時 fan-out；空字串 / whitespace
    必須跳過（不能誤觸發 ``git push ""``)". See the block comment above.
    """

    @staticmethod
    def _install_common_mocks(monkeypatch) -> AsyncMock:
        """Stub every leaf side-effect of ``_on_change_merged`` so the
        test observes only the boundary we care about.

        Returns the ``workspace._run`` mock — the one that would
        actually shell out to ``git push`` in prod.
        """
        from backend import workspace as _ws
        from backend import notifications as _notifs
        from backend import intent_bridge as _bridge
        from backend.routers import webhooks as _webhooks

        # ``_run`` returns ``(rc, stdout, stderr)``. Return a success
        # tuple so the code path doesn't take an error branch for the
        # positive-control test; for boundary-skip tests ``_run`` must
        # never be called at all, so the return value is moot.
        mock_run = AsyncMock(return_value=(0, "mirror-url\n", ""))
        monkeypatch.setattr(_ws, "_run", mock_run)

        monkeypatch.setattr(_notifs, "notify", AsyncMock())
        monkeypatch.setattr(
            _bridge, "on_gerrit_change_merged", AsyncMock(return_value=None),
        )
        # Three background tasks spawned after replication; mock them
        # out so asyncio.create_task() scheduling can't race the test
        # assertion window.
        monkeypatch.setattr(
            _webhooks, "_package_merged_artifacts", AsyncMock(),
        )
        monkeypatch.setattr(
            _webhooks, "_save_merged_solution_to_l3", AsyncMock(),
        )
        monkeypatch.setattr(
            _webhooks, "_trigger_ci_pipelines", AsyncMock(),
        )
        return mock_run

    @staticmethod
    def _merged_payload(change_id: str = "Iboundary-merged") -> dict:
        return {
            "type": "change-merged",
            "change": {
                "id": change_id,
                "subject": "boundary: replication target parsing",
                "commitMessage": "boundary\n",
            },
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "targets_value",
        [
            "",              # empty string — most common "not configured"
            "   ",           # pure whitespace — operator typed blank
            "\t\n",          # whitespace with tabs / newlines
            ",",             # a single comma → ["", ""]   → filtered empty
            ", ,  ,",        # comma-only + whitespace → all filtered out
            " ,\t, \n, ",    # mixed whitespace-only entries
        ],
        ids=[
            "empty_string",
            "whitespace_only",
            "tab_newline",
            "single_comma",
            "comma_separated_whitespace",
            "mixed_whitespace_only_entries",
        ],
    )
    async def test_empty_or_whitespace_targets_does_not_push(
        self, client, monkeypatch, targets_value,
    ):
        """Every flavour of "nothing configured" MUST skip the fan-out.
        The critical invariant is that ``git push ""`` NEVER runs —
        empty string at the shell layer would either fail cryptically
        or (depending on git version) fall back to a default remote.
        """
        mock_run = self._install_common_mocks(monkeypatch)

        from backend.config import settings
        original = settings.gerrit_replication_targets
        secret = f"boundary-secret-merged-empty-{abs(hash(targets_value))}"
        body = self._merged_payload()
        raw = json.dumps(body).encode()

        try:
            settings.gerrit_replication_targets = targets_value
            with _gerrit_enabled_with_secret(secret):
                res = await client.post(
                    "/api/v1/webhooks/gerrit",
                    content=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-Gerrit-Signature": _sign(secret, raw),
                    },
                )
        finally:
            settings.gerrit_replication_targets = original

        assert res.status_code == 200
        assert res.json() == {"status": "ok", "event": "change-merged"}
        # The guard under test: no shell call AT ALL. Not "no push" —
        # not even the preceding ``git remote get-url`` lookup should
        # fire, because the target loop is skipped entirely by the
        # ``if not targets: return`` short-circuit at webhooks.py:322.
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_targets_fans_out_push(
        self, client, monkeypatch,
    ):
        """Positive control: with two real-looking targets, we expect
        two pairs of ``_run`` invocations — ``git remote get-url
        <target>`` followed by ``git push <target> develop``.

        RT-23 / ADR-0040 (single-trunk release train): replication mirrors
        the ``develop`` trunk, NOT the retired release branch, and does so
        with a *plain* push — the old ``main --force-with-lease``
        force-align loop is gone. Also asserts that no invocation carries
        an empty ``""`` target argument (the specific failure mode the
        skip-boundary guards against)."""
        mock_run = self._install_common_mocks(monkeypatch)

        from backend.config import settings
        original = settings.gerrit_replication_targets
        secret = "boundary-secret-merged-valid"
        body = self._merged_payload(change_id="Iboundary-valid")
        raw = json.dumps(body).encode()

        try:
            settings.gerrit_replication_targets = "origin-mirror, github-backup"
            with _gerrit_enabled_with_secret(secret):
                res = await client.post(
                    "/api/v1/webhooks/gerrit",
                    content=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-Gerrit-Signature": _sign(secret, raw),
                    },
                )
        finally:
            settings.gerrit_replication_targets = original

        assert res.status_code == 200
        # Two targets × (remote get-url + push) = 4 _run invocations.
        # Whitespace around the comma should be stripped, so 'github-backup'
        # (no leading space) appears in the push command.
        assert mock_run.call_count == 4
        all_cmds = [call.args[0] for call in mock_run.call_args_list]
        # RT-23: push targets the develop trunk (payload carries no branch,
        # so the handler falls back to the single-trunk default).
        assert any('git push "origin-mirror" develop' in c for c in all_cmds)
        assert any('git push "github-backup" develop' in c for c in all_cmds)
        # The critical negative assertions.
        for cmd in all_cmds:
            assert 'git push ""' not in cmd
            assert "git push ''" not in cmd
            # ADR-0040: main is retired and the force-align loop is killed.
            if cmd.startswith("git push"):
                assert " main" not in cmd
                assert "--force-with-lease" not in cmd
                assert "--force" not in cmd


# ──────────────────────────────────────────────────────────────────────
# Y-prep.1 (#287) — verifier ↔ handler synchronisation contract
# ──────────────────────────────────────────────────────────────────────
#
# The four test classes above all exercise the dispatcher / handler
# layer through a real HMAC-SHA256 signature, but none of them PROVE
# that the verifier is doing its job — every payload they post carries
# a CORRECT signature, so a regression that makes the verifier always
# return True (e.g. a stray ``scalar_ok = True`` early-out, a flipped
# polarity at ``webhooks.py:103``, or someone who "speeds up tests" by
# deleting the ``compare_digest`` call) would still leave the existing
# 27 tests green.
#
# That is exactly the "verifier and handler validated in sync"
# (``X-Gerrit-Signature verifier 和 handler 同步驗過``) failure mode
# called out by the fourth Y-prep.1 sub-bullet. To close the gap:
#
#   - We sign every test in this file with REAL HMAC-SHA256 (already
#     covered by ``_sign`` and the new ``gerrit_sign`` /
#     ``gerrit_signed_post`` fixtures); AND
#   - We add an explicit verifier-rejection test class that locks the
#     401-on-bad-signature contract. The two together prove that
#     when a test sees "200 + handler called", it's because the
#     verifier accepted the real HMAC, not because the verifier was
#     bypassed.
#
# Concrete failure-mode coverage:
#   1. ``test_invalid_signature_rejected_no_dispatch`` — posts a
#      well-formed event with a deliberately-wrong hex signature.
#      Verifier MUST reject (401) and dispatcher MUST NOT fire. If
#      this regresses to 200, the verifier code path is broken or
#      bypassed.
#   2. ``test_missing_signature_rejected_when_secret_configured`` —
#      posts with no ``X-Gerrit-Signature`` header at all. The
#      verifier compares against ``""`` and must reject (401).
#      Catches the "header is optional when configured" anti-pattern
#      regression.
#   3. ``test_tampered_body_rejected_no_dispatch`` — signs body A,
#      posts body B (same shape, different change.id). Verifier must
#      detect the body/signature mismatch and reject.
#   4. ``test_correct_signature_accepted_handler_dispatched`` —
#      positive control through the SAME ``gerrit_signed_post``
#      fixture path used by the negative tests. Locks that the
#      "verifier accepted" branch is observable end-to-end so a flat
#      "always-401" regression can't sneak in either.


class TestGerritWebhookSignatureVerifier:
    """Lock the ``X-Gerrit-Signature`` HMAC verifier ↔ dispatcher
    handoff: any signature that fails verification MUST short-circuit
    with 401 BEFORE any of the three event handlers is called.

    Covers Y-prep.1 (#287) fourth sub-bullet — "測試 fixture 要用真的
    HMAC-SHA256 簽名而非 mock，確保 X-Gerrit-Signature verifier 和
    handler 同步驗過". See the block comment above for the full
    rationale.
    """

    @staticmethod
    def _install_handler_mocks(monkeypatch):
        """Mock all three event handlers and return the trio so each
        test can assert ``assert_not_called`` (or, for the positive
        control, ``assert_called_once``).

        Mocking even on the negative path is important: if the verifier
        regresses and lets a bad signature through, the dispatcher will
        invoke the real handler, which would do real DB writes /
        background spawns and corrupt the next test. The mocks turn that
        into a clean assertion failure instead.
        """
        from backend.routers import webhooks
        mock_patchset = AsyncMock()
        mock_comment = AsyncMock()
        mock_merged = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_patchset_created", mock_patchset)
        monkeypatch.setattr(webhooks, "_on_comment_added", mock_comment)
        monkeypatch.setattr(webhooks, "_on_change_merged", mock_merged)
        return mock_patchset, mock_comment, mock_merged

    @staticmethod
    def _well_formed_payload(change_id: str = "Iverifier-01") -> dict:
        """A well-formed ``patchset-created`` event used as the carrier
        for the verifier tests. The event TYPE doesn't matter to the
        verifier — it runs before the dispatcher branches — but using
        a real type lets us tell "rejected by verifier" (401) apart
        from "rejected by validator" (400) cleanly."""
        return {
            "type": "patchset-created",
            "change": {"id": change_id, "subject": "verifier sync test"},
            "patchSet": {
                "revision": "0011223344556677",
                "uploader": {"name": "verifier-tester"},
            },
        }

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected_no_dispatch(
        self, monkeypatch, gerrit_signed_post,
    ):
        """A deliberately-wrong hex signature MUST be rejected with 401
        and MUST NOT reach any handler."""
        mock_patchset, mock_comment, mock_merged = self._install_handler_mocks(monkeypatch)

        secret = "verifier-secret-invalid-sig"
        body = self._well_formed_payload(change_id="Iverifier-bad-sig")
        # Same length + character set as a real SHA-256 hex digest, so
        # the verifier reaches ``compare_digest`` rather than failing
        # an upstream length check. 64 hex chars of ``deadbeef``.
        bogus_sig = "deadbeef" * 8
        assert len(bogus_sig) == 64

        res = await gerrit_signed_post(secret, body, signature=bogus_sig)

        # Verifier must short-circuit at webhooks.py:103-104.
        assert res.status_code == 401
        assert res.json() == {"detail": "Invalid signature"}
        # And critically — the dispatcher must NOT have run. If a
        # handler was called here, the verifier was bypassed.
        mock_patchset.assert_not_called()
        mock_comment.assert_not_called()
        mock_merged.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_signature_rejected_when_secret_configured(
        self, monkeypatch, gerrit_signed_post,
    ):
        """If a secret is configured but the request omits the
        ``X-Gerrit-Signature`` header, the verifier compares against
        ``""`` (the default at webhooks.py:65) and MUST reject."""
        mock_patchset, mock_comment, mock_merged = self._install_handler_mocks(monkeypatch)

        secret = "verifier-secret-no-header"
        body = self._well_formed_payload(change_id="Iverifier-no-header")

        res = await gerrit_signed_post(secret, body, omit_signature=True)

        assert res.status_code == 401
        assert res.json() == {"detail": "Invalid signature"}
        mock_patchset.assert_not_called()
        mock_comment.assert_not_called()
        mock_merged.assert_not_called()

    @pytest.mark.asyncio
    async def test_tampered_body_rejected_no_dispatch(
        self, client, monkeypatch, gerrit_sign,
    ):
        """Sign body A, POST body B. The verifier digests the wire
        body, so the signature/body mismatch MUST be caught and
        rejected. This is the exact failure mode the HMAC primitive
        is supposed to catch — locking it in proves the verifier is
        truly digesting the request body, not (e.g.) a cached or
        empty buffer."""
        mock_patchset, mock_comment, mock_merged = self._install_handler_mocks(monkeypatch)

        secret = "verifier-secret-tamper"
        signed_body = self._well_formed_payload(change_id="Iverifier-signed")
        # Same shape, different change id — flipping a single field is
        # enough to invalidate the digest. We sign signed_body but POST
        # tampered_body.
        tampered_body = self._well_formed_payload(change_id="Iverifier-tampered")
        assert signed_body != tampered_body, (
            "test bug: signed_body and tampered_body must differ"
        )

        signed_raw = json.dumps(signed_body).encode()
        tampered_raw = json.dumps(tampered_body).encode()
        sig_for_signed_body = gerrit_sign(secret, signed_raw)

        with _gerrit_enabled_with_secret(secret):
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=tampered_raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Gerrit-Signature": sig_for_signed_body,
                },
            )

        assert res.status_code == 401
        assert res.json() == {"detail": "Invalid signature"}
        mock_patchset.assert_not_called()
        mock_comment.assert_not_called()
        mock_merged.assert_not_called()

    @pytest.mark.asyncio
    async def test_correct_signature_accepted_handler_dispatched(
        self, monkeypatch, gerrit_signed_post,
    ):
        """Positive control through the SAME ``gerrit_signed_post``
        fixture path the negative tests use. Without this, an
        always-401 verifier regression would leave all three negative
        tests green AND silently break production. Pairing the
        positive and negative through one fixture surface guarantees
        verifier acceptance + dispatcher invocation are both
        observable."""
        mock_patchset, mock_comment, mock_merged = self._install_handler_mocks(monkeypatch)

        secret = "verifier-secret-positive-control"
        body = self._well_formed_payload(change_id="Iverifier-positive")

        # No signature override — fixture computes the real HMAC.
        res = await gerrit_signed_post(secret, body)

        assert res.status_code == 200
        assert res.json() == {"status": "ok", "event": "patchset-created"}
        # Exactly the matching handler ran; siblings did not.
        mock_patchset.assert_called_once()
        mock_comment.assert_not_called()
        mock_merged.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# OP-713 — AI Reviewer wiring: _on_patchset_created + _run_ai_review
# ──────────────────────────────────────────────────────────────────────
#
# These tests pin the ticket's Phase 2/3 wiring contract:
#   * The merger-bot uploader is filtered before the LLM is touched
#     (loop prevention — same rule the OP-714 proactive merger has).
#   * The (change_id, revision) throttle skips a duplicate event for
#     the same SHA inside the 24 h TTL (AC #6).
#   * Oversized patchsets post score=0 with the canonical "too large"
#     message (AC #4).
#   * Happy path posts Code-Review +1 with the chosen model in the
#     comment footer (AC #2 + AC #5).
#
# ``_run_ai_review`` is the unit tested directly — the dispatcher tests
# above already lock that ``_on_patchset_created`` is invoked for the
# event type. Going through ``_run_ai_review`` keeps these tests fast
# (no asyncio.create_task race) while still exercising the real
# routing/posting/recording wiring against a stub Gerrit client.


class _StubGerritClient:
    """In-memory stand-in for ``backend.gerrit.gerrit_client``.

    Captures ``post_review`` invocations and serves a pre-set
    ``query_change`` payload. Anything we don't override returns the
    same ``{"error": "..."}`` shape the real client uses, so
    ``_run_ai_review`` exercises its error branches identically.
    """

    def __init__(self, *, files=None, subject=""):
        self._files = files or []
        self._subject = subject
        self.posted_reviews: list[dict] = []

    async def query_change(self, change_id, project=""):
        return {
            "subject": self._subject,
            "currentPatchSet": {
                "files": [{"file": f} for f in self._files],
            },
        }

    async def post_review(self, *, commit, message, labels, project=""):
        self.posted_reviews.append({
            "commit": commit,
            "message": message,
            "labels": dict(labels or {}),
            "project": project,
        })
        return {"status": "ok", "commit": commit}


class TestAIReviewerWiring:
    """OP-713 — patchset-created → AI Reviewer plumbing."""

    @pytest.fixture(autouse=True)
    def _reset_throttle(self):
        from backend.agents import ai_reviewer
        ai_reviewer.reset_throttle()
        yield
        ai_reviewer.reset_throttle()

    @pytest.mark.asyncio
    async def test_merger_uploader_short_circuits(self, monkeypatch):
        """Loop prevention: a patchset uploaded by ``merger-agent-bot``
        must never spawn an AI review (would feedback-loop with the
        OP-714 conflict resolution patchsets)."""
        from backend.routers import webhooks
        from backend.agents import ai_reviewer

        # Make sure we'd notice if the body fired
        run_calls = []
        async def fake_run_ai_review(**kw):
            run_calls.append(kw)
        monkeypatch.setattr(webhooks, "_run_ai_review", fake_run_ai_review)
        # Silence the L2 notification.
        from backend import notifications
        async def _noop_notify(*a, **kw):
            pass
        monkeypatch.setattr(notifications, "notify", _noop_notify)

        event = {
            "type": "patchset-created",
            "change": {"id": "Imerger01", "subject": "merger resolve"},
            "patchSet": {
                "revision": "deadbeef" * 5,
                "uploader": {
                    "name": "Merger Agent Bot",
                    "username": "merger-agent-bot",
                    "email": "merger-agent-bot@example.com",
                },
            },
        }
        await webhooks._on_patchset_created(conn=None, event=event)

        assert run_calls == []
        # Throttle entry must NOT be set — we want the next non-merger
        # event for the same change to still trigger a review.
        assert ai_reviewer.should_skip_recent(
            "Imerger01", "deadbeef" * 5,
        ) is False

    @pytest.mark.asyncio
    async def test_throttle_blocks_duplicate_revision_within_24h(
        self, monkeypatch,
    ):
        """AC #6: same change re-uploaded with the same revision SHA
        twice within 24 h → only one AI review fires."""
        from backend.routers import webhooks
        from backend.agents import ai_reviewer

        run_calls = []
        async def fake_run_ai_review(**kw):
            run_calls.append(kw)
        monkeypatch.setattr(webhooks, "_run_ai_review", fake_run_ai_review)
        from backend import notifications
        async def _noop_notify(*a, **kw):
            pass
        monkeypatch.setattr(notifications, "notify", _noop_notify)

        event = {
            "type": "patchset-created",
            "change": {"id": "Idup01", "subject": "first push"},
            "patchSet": {
                "revision": "abc12345" * 5,
                "uploader": {"name": "alice"},
                "sizeInsertions": 10,
                "sizeDeletions": 2,
            },
        }
        # First delivery — fires.
        await webhooks._on_patchset_created(conn=None, event=event)
        # Yield once so the asyncio.create_task'd background coroutine
        # actually runs against the stubbed _run_ai_review and appends
        # to ``run_calls``.
        import asyncio as _asyncio
        await _asyncio.sleep(0)

        # Second delivery (Gerrit retried, same SHA) — throttled.
        await webhooks._on_patchset_created(conn=None, event=event)
        await _asyncio.sleep(0)

        assert len(run_calls) == 1
        assert ai_reviewer.should_skip_recent(
            "Idup01", "abc12345" * 5,
        ) is True

    @pytest.mark.asyncio
    async def test_run_ai_review_oversize_posts_too_large_with_score_zero(
        self, monkeypatch,
    ):
        """AC #4: a >1500 LOC patchset gets score=0 and the canonical
        ``too large for AI review`` body, and the LLM is never invoked."""
        from backend.routers import webhooks
        from backend.gerrit import gerrit_client as _real

        stub = _StubGerritClient(files=["backend/big.py"], subject="huge refactor")
        monkeypatch.setattr("backend.gerrit.gerrit_client", stub)

        # Diff fetch must not be needed for the over-cap path; stub it
        # to assert that.
        async def _fake_fetch_diff(rev, project):
            raise AssertionError("diff must not be fetched for over-cap path")
        monkeypatch.setattr(webhooks, "_fetch_patchset_diff", _fake_fetch_diff)

        # Pin LLM stub: should never be called either.
        from backend.agents import ai_reviewer

        def _no_invoke(prompt, *, model):
            raise AssertionError("LLM must not be invoked for over-cap diffs")

        # Re-route through the public API so the size-cap fast path runs
        # with our stub Gerrit client. _run_ai_review reads
        # ``ai_reviewer.is_too_large`` etc., but invokes the LLM through
        # ``review_patchset`` only when below the cap — we exercise the
        # over-cap branch end-to-end here.
        await webhooks._run_ai_review(
            change_id="Ibig01",
            change_number="123",
            revision="cafef00d" * 5,
            project="omnisight",
            subject="huge refactor",
            insertions=1200,
            deletions=400,  # 1600 total
        )

        assert len(stub.posted_reviews) == 1
        post = stub.posted_reviews[0]
        assert post["labels"] == {"Code-Review": 0}
        assert "too large for AI review" in post["message"]
        assert "human deep-review" in post["message"]

    @pytest.mark.asyncio
    async def test_run_ai_review_happy_path_posts_plus_one_with_model_footer(
        self, monkeypatch,
    ):
        """AC #2 + AC #5: low-risk docs PSet → footer shows haiku model
        and Code-Review +1 lands on the change."""
        from backend.routers import webhooks
        from backend.agents import ai_reviewer

        stub = _StubGerritClient(
            files=["docs/howto.md"],
            subject="docs typo",
        )
        monkeypatch.setattr("backend.gerrit.gerrit_client", stub)

        # No real workspace in tests — short-circuit the diff fetch.
        async def _empty_diff(rev, project):
            return ""
        monkeypatch.setattr(webhooks, "_fetch_patchset_diff", _empty_diff)

        # Stub the LLM call inside review_patchset by patching the
        # `invoke_chat` import target; the simpler path is to monkey-
        # patch ``review_patchset`` itself.
        from backend.agents.ai_reviewer import (
            MODEL_HAIKU, ReviewResult, _with_footer,
        )
        captured: dict = {}

        def fake_review_patchset(diff, model="", *, files=(), subject="",
                                  insertions=None, deletions=None, **kw):
            captured["diff"] = diff
            captured["files"] = tuple(files)
            captured["subject"] = subject
            chosen = ai_reviewer.route_model(diff=diff, files=files)
            return ReviewResult(
                score=1,
                message=_with_footer(
                    "LGTM — docs only.",
                    model_id=chosen, cost_usd=0.0001,
                ),
                model_id=chosen,
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.0001,
            )
        monkeypatch.setattr(
            ai_reviewer, "review_patchset", fake_review_patchset,
        )

        # Avoid the optional billing record reaching the real PG pool.
        async def _noop_record(**kw):
            return None
        monkeypatch.setattr(
            "backend.billing_usage.record_llm_call", _noop_record,
        )

        await webhooks._run_ai_review(
            change_id="Idoc01",
            change_number="456",
            revision="fee1c0de" * 5,
            project="omnisight",
            subject="docs typo",
            insertions=5,
            deletions=1,
        )

        assert len(stub.posted_reviews) == 1
        post = stub.posted_reviews[0]
        assert post["labels"] == {"Code-Review": 1}
        # Footer pinned: ``reviewed-by: <model_id> · cost: $X.XX``
        assert "reviewed-by:" in post["message"]
        assert MODEL_HAIKU in post["message"]
        # Routing input arrived intact — the AI Reviewer saw the file
        # list pulled from query_change.
        assert captured["files"] == ("docs/howto.md",)
        assert captured["subject"] == "docs typo"


# ──────────────────────────────────────────────────────────────────────
# OP-801 — bridge-daemon-callable AI Reviewer pipeline tests
# ──────────────────────────────────────────────────────────────────────


class TestAiReviewerCheckBridgePath:
    """OP-801 — :func:`webhooks._ai_reviewer_check` is the in-process
    coroutine the bridge daemon (``backend/agents/gerrit_jira_bridge.py``)
    invokes via ``asyncio.run`` in a per-event thread. The tests below
    pin the same skip behaviour the webhook ``_on_patchset_created``
    path enforces, but exercised through the new shared entry point.
    """

    @pytest.fixture(autouse=True)
    def _reset_throttle(self):
        from backend.agents import ai_reviewer
        ai_reviewer.reset_throttle()
        yield
        ai_reviewer.reset_throttle()

    @pytest.mark.asyncio
    async def test_merger_uploader_short_circuits(self, monkeypatch, caplog):
        """Loop prevention — merger-agent-bot's own resolution patchsets
        must NOT trigger an AI review (would feedback-loop with OP-714)."""
        from backend.routers import webhooks
        from backend.agents import ai_reviewer

        run_calls = []

        async def fake_run_ai_review(**kw):
            run_calls.append(kw)
        monkeypatch.setattr(webhooks, "_run_ai_review", fake_run_ai_review)

        from backend import notifications

        async def _noop_notify(*a, **kw):
            pass
        monkeypatch.setattr(notifications, "notify", _noop_notify)

        event = {
            "type": "patchset-created",
            "change": {"id": "Imerger01", "subject": "merger resolve"},
            "patchSet": {
                "revision": "deadbeef" * 5,
                "uploader": {
                    "name": "Merger Agent Bot",
                    "username": "merger-agent-bot",
                    "email": "merger-agent-bot@example.com",
                },
            },
        }
        with caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await webhooks._ai_reviewer_check(event)

        assert run_calls == []
        # Throttle entry must NOT be set — a follow-up non-merger PS for
        # the same change should still fire a review.
        assert ai_reviewer.should_skip_recent(
            "Imerger01", "deadbeef" * 5,
        ) is False
        # AC#4 wording — the bridge log surfaces the skip reason.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "ai_reviewer_skip" in joined
        assert "uploader_is_merger" in joined

    @pytest.mark.asyncio
    async def test_throttle_blocks_duplicate_revision_within_24h(
        self, monkeypatch,
    ):
        """AC#2 — same (change_id, revision) re-delivered (e.g. on a
        bridge restart that replays cursor events) → only one review."""
        from backend.routers import webhooks
        from backend.agents import ai_reviewer

        run_calls = []

        async def fake_run_ai_review(**kw):
            run_calls.append(kw)
        monkeypatch.setattr(webhooks, "_run_ai_review", fake_run_ai_review)

        from backend import notifications

        async def _noop_notify(*a, **kw):
            pass
        monkeypatch.setattr(notifications, "notify", _noop_notify)

        event = {
            "type": "patchset-created",
            "change": {"id": "Idup01", "subject": "first push"},
            "patchSet": {
                "revision": "abc12345" * 5,
                "uploader": {"name": "alice"},
                "sizeInsertions": 10,
                "sizeDeletions": 2,
            },
        }
        # First delivery — fires.
        await webhooks._ai_reviewer_check(event)
        # Second delivery (replayed by the bridge) — throttled.
        await webhooks._ai_reviewer_check(event)

        assert len(run_calls) == 1
        assert ai_reviewer.should_skip_recent(
            "Idup01", "abc12345" * 5,
        ) is True

    @pytest.mark.asyncio
    async def test_happy_path_invokes_run_ai_review_with_event_fields(
        self, monkeypatch,
    ):
        """The shared entry point must pass the change/PS metadata on
        to ``_run_ai_review`` unchanged so risk-tier routing fires
        correctly downstream (AC#3 needs this end-to-end)."""
        from backend.routers import webhooks

        captured = {}

        async def fake_run_ai_review(**kw):
            captured.update(kw)
        monkeypatch.setattr(webhooks, "_run_ai_review", fake_run_ai_review)

        from backend import notifications

        async def _noop_notify(*a, **kw):
            pass
        monkeypatch.setattr(notifications, "notify", _noop_notify)

        event = {
            "type": "patchset-created",
            "change": {
                "id": "Ialembic01",
                "number": 555,
                "subject": "[OP-801] add column",
                "project": "omnisight/OmniSight-Productizer",
            },
            "patchSet": {
                "revision": "abcd1234" * 5,
                "uploader": {"name": "alice"},
                "sizeInsertions": 30,
                "sizeDeletions": 4,
            },
        }
        await webhooks._ai_reviewer_check(event)

        assert captured["change_id"] == "Ialembic01"
        assert captured["change_number"] == "555"
        assert captured["revision"] == "abcd1234" * 5
        assert captured["project"] == "omnisight/OmniSight-Productizer"
        assert captured["subject"] == "[OP-801] add column"
        assert captured["insertions"] == 30
        assert captured["deletions"] == 4

    @pytest.mark.asyncio
    async def test_run_ai_review_logs_invoked_with_model(
        self, monkeypatch, caplog,
    ):
        """AC#3 — risk-tier routing observable in the bridge log line
        ``ai_reviewer_invoked change=N model=opus`` (for an
        alembic/versions/ touch). The same log line covers AC#1 for
        haiku on a docs-only change."""
        from backend.routers import webhooks
        from backend.agents import ai_reviewer
        from backend.agents.ai_reviewer import (
            MODEL_OPUS, ReviewResult, _with_footer,
        )

        # Stub Gerrit + diff fetch. The change touches alembic/versions
        # which the router pins to opus regardless of LOC.
        stub = _StubGerritClient(
            files=["backend/alembic/versions/0200_add_col.py"],
            subject="db migration",
        )
        monkeypatch.setattr("backend.gerrit.gerrit_client", stub)

        async def _empty_diff(rev, project):
            return ""
        monkeypatch.setattr(webhooks, "_fetch_patchset_diff", _empty_diff)

        def fake_review_patchset(diff, model="", *, files=(), subject="",
                                  insertions=None, deletions=None, **kw):
            chosen = ai_reviewer.route_model(diff=diff, files=files)
            return ReviewResult(
                score=1,
                message=_with_footer(
                    "Migration looks safe.",
                    model_id=chosen, cost_usd=0.01,
                ),
                model_id=chosen,
                input_tokens=200,
                output_tokens=40,
                cost_usd=0.01,
            )
        monkeypatch.setattr(
            ai_reviewer, "review_patchset", fake_review_patchset,
        )

        async def _noop_record(**kw):
            return None
        monkeypatch.setattr(
            "backend.billing_usage.record_llm_call", _noop_record,
        )

        with caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await webhooks._run_ai_review(
                change_id="Imig01",
                change_number="800",
                revision="ba110011" * 5,
                project="omnisight",
                subject="db migration",
                insertions=30,
                deletions=4,
            )

        joined = "\n".join(r.getMessage() for r in caplog.records)
        # AC#1 + AC#3 — single greppable line carries the model tier.
        assert "ai_reviewer_invoked" in joined
        assert "change=Imig01" in joined
        assert f"model={MODEL_OPUS}" in joined

        # Sanity: the +1 actually landed on the change.
        assert len(stub.posted_reviews) == 1
        assert stub.posted_reviews[0]["labels"] == {"Code-Review": 1}


# ──────────────────────────────────────────────────────────────────────
# OP-1565 / RT-23 — merge webhook decoupled from release CI (ADR-0040)
# ──────────────────────────────────────────────────────────────────────
#
# ADR-0040 (single-trunk release train) retires `main` and makes releases
# tag-driven and branch-agnostic — the GitLab image pipeline builds on
# `^v` tags only (ADR-0038). Two `main`-hardcoded landmines lived in the
# post-merge path of webhooks.py:
#
#   1. `_on_change_merged` force-pushed the retired `main` branch to the
#      replication mirrors (`git push <t> main --force-with-lease`).
#   2. `_trigger_ci_pipelines` kicked *release* CI on `main` via a GitHub
#      Actions workflow dispatch (`gh workflow run ci.yml -r main`) and a
#      GitLab branch-ref pipeline (`-d ref=main`).
#
# RT-23 removed both. These tests lock the contract so a future refactor
# can't silently re-introduce a merge→release-CI-on-main coupling.


class TestMergeDoesNotTriggerReleaseCI:
    """RT-23 / ADR-0040 landmine #4 — a Gerrit merge MUST NOT trigger
    release CI on the retired ``main`` ref.

    The legacy merge-time GitHub Actions workflow dispatch and GitLab
    branch-ref pipeline triggers were removed from
    ``_trigger_ci_pipelines``. These tests prove the merge path no longer
    spawns either, even with both CI integrations explicitly enabled.
    """

    @staticmethod
    @contextmanager
    def _ci_flags(*, github: bool, gitlab: bool, jenkins: bool):
        """Pin the three CI integration switches for the test body and
        restore them on exit (Settings is a module-global Pydantic model)."""
        from backend.config import settings
        saved = {
            "ci_github_actions_enabled": settings.ci_github_actions_enabled,
            "ci_gitlab_enabled": settings.ci_gitlab_enabled,
            "ci_jenkins_enabled": settings.ci_jenkins_enabled,
        }
        try:
            settings.ci_github_actions_enabled = github
            settings.ci_gitlab_enabled = gitlab
            settings.ci_jenkins_enabled = jenkins
            yield
        finally:
            for key, value in saved.items():
                setattr(settings, key, value)

    @pytest.mark.asyncio
    async def test_trigger_ci_pipelines_spawns_no_github_or_gitlab_subprocess(
        self, monkeypatch,
    ):
        """With BOTH GitHub Actions and GitLab CI enabled (and Jenkins
        off), ``_trigger_ci_pipelines`` spawns NO subprocess at all — the
        ``gh workflow run`` dispatch and the GitLab ``/pipeline`` POST are
        gone. Any spawn here is a regression that re-couples merge to
        release CI."""
        import asyncio as _asyncio
        from backend.routers import webhooks

        spawned: list[tuple] = []

        async def _record_exec(*args, **kwargs):
            spawned.append(args)
            return AsyncMock()  # never reached on the github/gitlab paths

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _record_exec)

        with self._ci_flags(github=True, gitlab=True, jenkins=False):
            await webhooks._trigger_ci_pipelines()

        # No `gh`, no GitLab `/pipeline` POST — in fact nothing spawned.
        assert spawned == [], (
            f"merge path must not spawn release CI subprocesses, got: {spawned!r}"
        )

    @pytest.mark.asyncio
    async def test_change_merged_does_not_trigger_release_ci(
        self, monkeypatch,
    ):
        """Drive the REAL ``_on_change_merged`` → ``_trigger_ci_pipelines``
        path (no replication targets) and assert it spawns no release-CI
        subprocess, even with GitHub + GitLab CI enabled. Calls the
        handler directly (the dispatcher routing is locked by
        ``TestGerritEventRouting``) so the contract is observable without
        the HTTP/DB layer."""
        import asyncio as _asyncio
        from backend.routers import webhooks
        from backend import notifications as _notifs
        from backend import intent_bridge as _bridge

        spawned: list[tuple] = []

        async def _record_exec(*args, **kwargs):
            spawned.append(args)
            return AsyncMock()

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _record_exec)
        monkeypatch.setattr(_notifs, "notify", AsyncMock())
        monkeypatch.setattr(
            _bridge, "on_gerrit_change_merged", AsyncMock(return_value=None),
        )
        # Keep the OTHER post-merge background fan-out inert, but leave the
        # real ``_trigger_ci_pipelines`` in place — it is the unit under
        # test here.
        monkeypatch.setattr(webhooks, "_package_merged_artifacts", AsyncMock())
        monkeypatch.setattr(webhooks, "_save_merged_solution_to_l3", AsyncMock())

        from backend.config import settings
        orig_targets = settings.gerrit_replication_targets
        body = {
            "type": "change-merged",
            "change": {
                "id": "Irt23-merged",
                "branch": "develop",
                "subject": "Release 9.9.9",
                "commitMessage": "Release 9.9.9\n",
            },
        }

        try:
            settings.gerrit_replication_targets = ""  # skip mirror fan-out
            with self._ci_flags(github=True, gitlab=True, jenkins=False):
                await webhooks._on_change_merged(body)
                # ``_trigger_ci_pipelines`` runs in an asyncio.create_task'd
                # coroutine; yield a couple of times so it executes fully.
                await _asyncio.sleep(0)
                await _asyncio.sleep(0)
        finally:
            settings.gerrit_replication_targets = orig_targets

        assert spawned == [], (
            f"change-merged must not trigger release CI, spawned: {spawned!r}"
        )


class TestMergeReplicationBranchContract:
    """RT-23 / ADR-0040 — replication mirrors the merged trunk branch
    (``develop``), never the retired ``main``, with a plain (non-force)
    push."""

    @pytest.mark.asyncio
    async def test_push_honours_event_branch_and_never_force_pushes_main(
        self, monkeypatch,
    ):
        """The push command targets the branch carried on the merge event
        (``change.branch``) and never emits ``main``, ``--force`` or
        ``--force-with-lease``. Calls ``_on_change_merged`` directly to
        observe the shell-out without the HTTP/DB layer."""
        from backend import workspace as _ws
        from backend import notifications as _notifs
        from backend import intent_bridge as _bridge
        from backend.routers import webhooks as _webhooks

        mock_run = AsyncMock(return_value=(0, "mirror-url\n", ""))
        monkeypatch.setattr(_ws, "_run", mock_run)
        monkeypatch.setattr(_notifs, "notify", AsyncMock())
        monkeypatch.setattr(
            _bridge, "on_gerrit_change_merged", AsyncMock(return_value=None),
        )
        monkeypatch.setattr(_webhooks, "_package_merged_artifacts", AsyncMock())
        monkeypatch.setattr(_webhooks, "_save_merged_solution_to_l3", AsyncMock())
        monkeypatch.setattr(_webhooks, "_trigger_ci_pipelines", AsyncMock())

        from backend.config import settings
        original = settings.gerrit_replication_targets
        body = {
            "type": "change-merged",
            "change": {
                "id": "Irt23-branch",
                "branch": "develop",
                "subject": "feat: trunk merge",
                "commitMessage": "feat\n",
            },
        }

        try:
            settings.gerrit_replication_targets = "origin-mirror"
            await _webhooks._on_change_merged(body)
        finally:
            settings.gerrit_replication_targets = original

        push_cmds = [
            c.args[0] for c in mock_run.call_args_list
            if c.args and c.args[0].startswith("git push")
        ]
        assert push_cmds, "expected at least one git push invocation"
        for cmd in push_cmds:
            assert cmd == 'git push "origin-mirror" develop'
            assert " main" not in cmd
            assert "--force" not in cmd
