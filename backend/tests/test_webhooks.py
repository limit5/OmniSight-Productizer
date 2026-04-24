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
        <target>`` followed by ``git push <target> main
        --force-with-lease``. Also asserts that no invocation carries
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
        assert any('git push "origin-mirror"' in c for c in all_cmds)
        assert any('git push "github-backup"' in c for c in all_cmds)
        # The critical negative assertion — the boundary's raison d'être.
        # An empty-quoted target would look like ``git push ""`` in the
        # shell arg; it must never appear.
        for cmd in all_cmds:
            assert 'git push ""' not in cmd
            assert "git push ''" not in cmd
