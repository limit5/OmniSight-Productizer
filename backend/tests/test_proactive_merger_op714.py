"""OP-714 — unit tests for proactive merger trigger logic.

Tests the pure decision logic of ``_proactive_merger_check`` and
``_is_merger_uploader`` without needing the FastAPI test client, the
asyncpg pool, or a live Gerrit. Each Gerrit-side dependency is mocked
so we exercise:

  * Loop prevention (uploader == merger-agent-bot under various shapes)
  * Hashtag short-circuit (Merger-Proactive-PS* + Merge-Conflict-Resolved)
  * WIP / private skip
  * Mergeable=true → skip without invoking merger
  * Mergeable=false → set hashtag + invoke merger

This file deliberately skips the auth-refactor end-to-end test (the
shared ``client`` fixture needs OMNI_TEST_PG_URL to init the pool;
that path runs in CI). The auth refactor is structurally a 2-line
change (add Depends(require_operator) + delegate to
_verify_jira_signature) so its surface area is small enough to
verify by inspection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.routers.webhooks import (
    _PROACTIVE_HASHTAG_PREFIX,
    _RESOLVED_HASHTAG,
    _is_merger_uploader,
    _proactive_merger_check,
)


# ──────────────────────────────────────────────────────────────────
# _is_merger_uploader — pure function, no async, no mocks
# ──────────────────────────────────────────────────────────────────


class TestIsMergerUploader:

    def test_recognises_by_name(self):
        assert _is_merger_uploader({"name": "merger-agent-bot"})
        assert _is_merger_uploader({"name": "Merger-Agent-Bot"})  # case-insensitive

    def test_recognises_by_email(self):
        assert _is_merger_uploader({"email": "merger-agent-bot@svc.x"})

    def test_recognises_by_username(self):
        assert _is_merger_uploader({"username": "merger-agent-bot"})

    def test_rejects_human_uploader(self):
        assert not _is_merger_uploader({
            "name": "sora", "email": "sora@s.com", "username": "sora",
        })

    def test_rejects_other_bot(self):
        assert not _is_merger_uploader({
            "name": "codex-bot", "username": "codex-bot",
        })

    def test_handles_empty(self):
        assert not _is_merger_uploader({})


# ──────────────────────────────────────────────────────────────────
# _proactive_merger_check — full decision flow with mocks
# ──────────────────────────────────────────────────────────────────


def _event(
    *,
    change_number: int = 92,
    project: str = "omnisight/OmniSight-Productizer",
    rev: str = "d386745be2",
    ps_number: int = 1,
    uploader_name: str = "codex-bot",
    uploader_email: str = "codex-bot@x.com",
) -> dict:
    """Build a synthetic patchset-created event payload."""
    return {
        "type": "patchset-created",
        "change": {
            "id": f"I{rev}",
            "number": change_number,
            "project": project,
            "subject": "[OP-75] some change",
        },
        "patchSet": {
            "number": ps_number,
            "revision": rev,
            "uploader": {
                "name": uploader_name,
                "email": uploader_email,
                "username": uploader_name,
            },
        },
    }


@pytest.mark.asyncio
class TestProactiveMergerSkipConditions:

    async def test_skip_when_uploader_is_merger(self, caplog):
        """Loop prevention — merger's own patchsets must not retrigger."""
        # No mocks needed: the function returns BEFORE any Gerrit call.
        with caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(
                _event(uploader_name="merger-agent-bot",
                       uploader_email="merger-agent-bot@svc"),
            )
        assert any("skip_reason=uploader_is_merger" in r.message
                   for r in caplog.records)

    async def test_skip_when_proactive_hashtag_already_set(self, caplog):
        """Same-PS retry from Gerrit: hashtag already there → no double-fire."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [f"{_PROACTIVE_HASHTAG_PREFIX}1"],
            "subject": "[OP-X] x",
        })
        mock_client.add_hashtag = AsyncMock()

        mock_arbiter = AsyncMock()

        with patch("backend.gerrit.gerrit_client", mock_client), \
             patch("backend.merge_arbiter.on_merge_conflict_webhook",
                   mock_arbiter), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=hashtag_already_attempted" in r.message
                   for r in caplog.records)
        mock_arbiter.assert_not_called()
        mock_client.add_hashtag.assert_not_called()

    async def test_skip_when_resolved_hashtag_set(self, caplog):
        """Merger already succeeded — wait for human +2, don't re-fire."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [_RESOLVED_HASHTAG],
            "subject": "[OP-X] x",
        })
        mock_arbiter = AsyncMock()

        with patch("backend.gerrit.gerrit_client", mock_client), \
             patch("backend.merge_arbiter.on_merge_conflict_webhook",
                   mock_arbiter), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=hashtag_resolved" in r.message
                   for r in caplog.records)
        mock_arbiter.assert_not_called()

    async def test_skip_when_work_in_progress(self, caplog):
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [],
            "wip": True,
            "subject": "[OP-X] x",
        })
        mock_arbiter = AsyncMock()

        with patch("backend.gerrit.gerrit_client", mock_client), \
             patch("backend.merge_arbiter.on_merge_conflict_webhook",
                   mock_arbiter), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=work_in_progress" in r.message
                   for r in caplog.records)
        mock_arbiter.assert_not_called()

    async def test_skip_when_mergeable_true(self, caplog):
        """Clean change — no merger needed."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-X] x",
        })
        mock_arbiter = AsyncMock()

        with patch("backend.gerrit.gerrit_client", mock_client), \
             patch("backend.merge_arbiter.on_merge_conflict_webhook",
                   mock_arbiter), \
             patch("backend.routers.webhooks._fetch_mergeable",
                   AsyncMock(return_value={"mergeable": True})), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=mergeable" in r.message
                   for r in caplog.records)
        mock_arbiter.assert_not_called()


@pytest.mark.asyncio
class TestProactiveMergerInvokes:

    async def test_invokes_merger_when_mergeable_false(self, caplog):
        """The happy-path: stale base change → merger called once."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] mergeable=false test",
        })
        mock_client.add_hashtag = AsyncMock(
            return_value={"status": "ok"},
        )

        # Mock the arbiter outcome so we don't call into LLM.
        mock_outcome = MagicMock()
        mock_outcome.reason = MagicMock()
        mock_outcome.reason.value = "merger_abstained_jira_ticket_opened"
        mock_arbiter = AsyncMock(return_value=mock_outcome)

        with patch("backend.gerrit.gerrit_client", mock_client), \
             patch("backend.merge_arbiter.on_merge_conflict_webhook",
                   mock_arbiter), \
             patch("backend.routers.webhooks._fetch_mergeable",
                   AsyncMock(return_value={"mergeable": False})), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event(change_number=92, ps_number=1))

        # Verify hashtag was set BEFORE merger invocation
        mock_client.add_hashtag.assert_awaited_once()
        call = mock_client.add_hashtag.call_args
        assert call.kwargs["hashtag"] == f"{_PROACTIVE_HASHTAG_PREFIX}1"
        assert call.kwargs["change_id"] == "92"

        # Verify merger was called with the right task shape
        mock_arbiter.assert_awaited_once()
        task = mock_arbiter.await_args.args[0]
        assert task.change_id == "92"
        assert task.patchset_revision == "d386745be2"
        # OP-92 ticket extracted from subject
        assert task.jira_ticket == "OP-92"

        assert any("decision=invoke_merger" in r.message
                   for r in caplog.records)
        assert any("merger_outcome reason=merger_abstained_jira_ticket_opened"
                   in r.message for r in caplog.records)

    async def test_swallows_arbiter_exception(self, caplog):
        """Arbiter failure must NOT propagate — webhook already returned."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] x",
        })
        mock_client.add_hashtag = AsyncMock(return_value={"status": "ok"})

        mock_arbiter = AsyncMock(side_effect=RuntimeError("arbiter blew up"))

        with patch("backend.gerrit.gerrit_client", mock_client), \
             patch("backend.merge_arbiter.on_merge_conflict_webhook",
                   mock_arbiter), \
             patch("backend.routers.webhooks._fetch_mergeable",
                   AsyncMock(return_value={"mergeable": False})), \
             caplog.at_level("ERROR", logger="backend.routers.webhooks"):
            # Must not raise
            await _proactive_merger_check(_event())

        assert any("merger_invocation_failed" in r.message
                   for r in caplog.records)

    async def test_continues_when_set_hashtag_fails(self, caplog):
        """Throttle marker is best-effort — merger still runs even if it fails."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] x",
        })
        mock_client.add_hashtag = AsyncMock(
            side_effect=RuntimeError("ssh unreachable"),
        )

        mock_outcome = MagicMock()
        mock_outcome.reason = MagicMock(value="abstained_low_confidence")
        mock_arbiter = AsyncMock(return_value=mock_outcome)

        with patch("backend.gerrit.gerrit_client", mock_client), \
             patch("backend.merge_arbiter.on_merge_conflict_webhook",
                   mock_arbiter), \
             patch("backend.routers.webhooks._fetch_mergeable",
                   AsyncMock(return_value={"mergeable": False})), \
             caplog.at_level("WARNING", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        # Merger still invoked despite the hashtag failure
        mock_arbiter.assert_awaited_once()
        assert any("set_hashtag_failed" in r.message
                   for r in caplog.records)
