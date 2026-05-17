"""OP-714 / OP-717 / OP-718 — unit tests for proactive merger trigger logic.

Tests the pure decision logic of ``_proactive_merger_check`` and
``_is_merger_uploader`` without needing the FastAPI test client, the
asyncpg pool, or a live Gerrit. Each Gerrit-side dependency is mocked
so we exercise:

  * Loop prevention (uploader == merger-agent-bot under various shapes)
  * Hashtag short-circuit (Merger-Proactive-PS* + Merge-Conflict-Resolved)
  * WIP / private skip
  * Mergeable=true → skip without invoking merger
  * Mergeable=false → set hashtag + delegate to backend via HTTP (OP-718)

OP-718 transport change: the merger invocation is no longer an
in-process ``on_merge_conflict_webhook`` call — the daemon process
lacks the backend's runtime init (asyncpg pool, LLM provider, JIRA
client), so every backend-init-dependent step short-circuited. We now
POST the ``MergeConflictTask`` JSON to the backend merger endpoint
and assert on the httpx call shape (URL, dual-header auth, payload)
instead of the arbiter signature.

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
    _MERGER_HTTP_PATH,
    _PROACTIVE_HASHTAG_PREFIX,
    _RESOLVED_HASHTAG,
    _is_merger_uploader,
    _post_merge_conflict_to_backend,
    _proactive_merger_check,
)


# ──────────────────────────────────────────────────────────────────
# Shared httpx-mock helpers (OP-718)
# ──────────────────────────────────────────────────────────────────


def _make_httpx_response(
    *, status_code: int = 200, json_body: dict | None = None,
    text: str = "",
):
    """Build a stand-in for an ``httpx.Response`` carrying just the
    attributes ``_post_merge_conflict_to_backend`` reads."""
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(
        return_value=json_body if json_body is not None else {},
    )
    response.text = text or (
        "" if json_body is None else "<json body>"
    )
    return response


def _patch_httpx_post(response_or_exc) -> "patch":
    """Patch ``httpx.AsyncClient`` so the ``async with`` block in
    ``_post_merge_conflict_to_backend`` returns a client whose ``post``
    coroutine yields the supplied response (or raises the supplied
    exception)."""
    client_mock = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client_mock.post = AsyncMock(side_effect=response_or_exc)
    else:
        client_mock.post = AsyncMock(return_value=response_or_exc)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client_mock)
    cm.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=cm)
    return patch(
        "backend.routers.webhooks.httpx.AsyncClient", factory,
    ), client_mock


@pytest.fixture
def merger_http_env(monkeypatch):
    """Configure the dual-header auth env vars the OP-718 HTTP delegate
    requires. Tests that need the absence path override individually."""
    monkeypatch.setenv("OMNISIGHT_GERRIT_WEBHOOK_API_KEY", "test-api-key")
    monkeypatch.setenv("OMNISIGHT_JIRA_WEBHOOK_SECRET", "test-jira-secret")
    monkeypatch.delenv("OMNISIGHT_BACKEND_URL", raising=False)
    yield


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

        http_patch, http_client = _patch_httpx_post(_make_httpx_response())

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=hashtag_already_attempted" in r.message
                   for r in caplog.records)
        http_client.post.assert_not_called()
        mock_client.add_hashtag.assert_not_called()

    async def test_skip_when_resolved_hashtag_set(self, caplog):
        """Merger already succeeded — wait for human +2, don't re-fire."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [_RESOLVED_HASHTAG],
            "subject": "[OP-X] x",
        })

        http_patch, http_client = _patch_httpx_post(_make_httpx_response())

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=hashtag_resolved" in r.message
                   for r in caplog.records)
        http_client.post.assert_not_called()

    async def test_skip_when_work_in_progress(self, caplog):
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [],
            "wip": True,
            "subject": "[OP-X] x",
        })

        http_patch, http_client = _patch_httpx_post(_make_httpx_response())

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=work_in_progress" in r.message
                   for r in caplog.records)
        http_client.post.assert_not_called()

    async def test_skip_when_mergeable_true(self, caplog):
        """Clean change — no merger needed (OP-717: enrichment returns
        mergeable=True instead of REST mergeable check)."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-X] x",
        })

        from backend.agents.conflict_enrichment import EnrichmentResult
        clean_result = EnrichmentResult(mergeable=True)

        http_patch, http_client = _patch_httpx_post(_make_httpx_response())

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=clean_result)), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        assert any("skip_reason=mergeable" in r.message
                   for r in caplog.records)
        http_client.post.assert_not_called()


@pytest.mark.asyncio
class TestProactiveMergerInvokes:

    async def test_invokes_merger_when_mergeable_false(
        self, caplog, merger_http_env,
    ):
        """The happy-path: stale base change → enrichment finds the
        conflict block → merger reached via httpx POST (OP-718) with
        real conflict_text + dual-header auth."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] mergeable=false test",
        })
        mock_client.add_hashtag = AsyncMock(return_value={"status": "ok"})

        # OP-717: enrichment returns one ConflictFile with real markers
        from backend.agents.conflict_enrichment import (
            ConflictFile, EnrichmentResult,
        )
        conflict_text = (
            "def f():\n"
            "<<<<<<< HEAD\n    return 1\n=======\n    return 2\n"
            ">>>>>>> branch\n"
        )
        enrichment = EnrichmentResult(
            mergeable=False,
            conflict_files=[ConflictFile(
                path="src/preferences.py",
                conflict_text=conflict_text,
                file_context=conflict_text,
            )],
            head_subject="head subj",
            incoming_subject="incoming subj",
        )

        # Mock backend response so we don't call into LLM.
        http_patch, http_client = _patch_httpx_post(_make_httpx_response(
            json_body={"ok": True, "reason": "plus_two_voted"},
        ))

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=enrichment)), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event(change_number=92, ps_number=1))

        # Verify hashtag was set BEFORE merger invocation
        mock_client.add_hashtag.assert_awaited_once()
        call = mock_client.add_hashtag.call_args
        assert call.kwargs["hashtag"] == f"{_PROACTIVE_HASHTAG_PREFIX}1"
        assert call.kwargs["change_id"] == "92"

        # OP-718: merger reached via httpx POST to backend, NOT in-process.
        http_client.post.assert_awaited_once()
        post_kwargs = http_client.post.await_args.kwargs
        post_args = http_client.post.await_args.args

        # URL points at the backend merger endpoint.
        url = post_args[0] if post_args else post_kwargs.get("url")
        assert url.endswith(_MERGER_HTTP_PATH)

        # Dual-header auth shape.
        headers = post_kwargs["headers"]
        assert headers["Authorization"] == "Bearer test-api-key"
        assert headers["X-Jira-Webhook-Secret"] == "test-jira-secret"

        # JSON payload mirrors the MergeConflictTask the in-process call
        # would have built.
        payload = post_kwargs["json"]
        assert payload["change_id"] == "92"
        assert payload["patchset_revision"] == "d386745be2"
        assert payload["jira_ticket"] == "OP-92"
        assert payload["file_path"] == "src/preferences.py"
        assert "<<<<<<< HEAD" in payload["conflict_text"]
        assert ">>>>>>> branch" in payload["conflict_text"]
        assert payload["head_commit_message"] == "head subj"

        assert any("decision=invoke_merger" in r.message
                   for r in caplog.records)
        assert any("primary_file=src/preferences.py" in r.message
                   for r in caplog.records)
        # OP-718: outcome line now carries the backend-reported reason.
        assert any("merger_outcome reason=plus_two_voted" in r.message
                   for r in caplog.records)

    async def test_invokes_merger_with_additional_files(
        self, caplog, merger_http_env,
    ):
        """Multi-file conflict → primary file is alphabetically-first,
        additional_files lists the rest."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] multi conflict",
        })
        mock_client.add_hashtag = AsyncMock(return_value={"status": "ok"})

        from backend.agents.conflict_enrichment import (
            ConflictFile, EnrichmentResult,
        )
        cfs = [
            ConflictFile(path=p, conflict_text=f"<<<<<<< {p}\nx\n=======\ny\n>>>>>>> b\n", file_context="")
            for p in ["a.py", "b.py", "c.md"]
        ]
        enrichment = EnrichmentResult(mergeable=False, conflict_files=cfs)

        http_patch, http_client = _patch_httpx_post(_make_httpx_response(
            json_body={"ok": True, "reason": "abstained_low_confidence"},
        ))

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=enrichment)), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        http_client.post.assert_awaited_once()
        payload = http_client.post.await_args.kwargs["json"]
        assert payload["file_path"] == "a.py"
        assert payload["additional_files"] == ["b.py", "c.md"]
        assert any("additional_count=2" in r.message
                   for r in caplog.records)
        assert any(
            "merger_outcome reason=abstained_low_confidence" in r.message
            for r in caplog.records
        )

    async def test_logs_arbiter_and_underlying_merger_reason(
        self, caplog, merger_http_env,
    ):
        """OP-1421 — bridge log must preserve the specific MergerReason
        even when the arbiter maps it to a generic refusal bucket."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-1421] refused other",
        })
        mock_client.add_hashtag = AsyncMock(return_value={"status": "ok"})

        from backend.agents.conflict_enrichment import (
            ConflictFile, EnrichmentResult,
        )
        enrichment = EnrichmentResult(
            mergeable=False,
            conflict_files=[ConflictFile(
                path="x.py",
                conflict_text="<<<<<<< x\na\n=======\nb\n>>>>>>> y\n",
                file_context="",
            )],
        )

        http_patch, http_client = _patch_httpx_post(_make_httpx_response(
            json_body={
                "ok": True,
                "reason": "merger_refused_other",
                "merger_outcome": {"reason": "refused_no_conflict"},
            },
        ))

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=enrichment)), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event(change_number=1421, ps_number=3))

        http_client.post.assert_awaited_once()
        assert any(
            "merger_outcome reason=merger_refused_other "
            "merger_reason=refused_no_conflict" in r.message
            for r in caplog.records
        )

    async def test_skips_too_many_conflicts(self, caplog, merger_http_env):
        """If enrichment caps out (>5 files) → set hashtag, no merger call."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] too many",
        })
        mock_client.add_hashtag = AsyncMock(return_value={"status": "ok"})

        from backend.agents.conflict_enrichment import (
            ConflictFile, EnrichmentResult,
        )
        many = [ConflictFile(path=f"f{i}.py", conflict_text="", file_context="")
                for i in range(6)]
        enrichment = EnrichmentResult(
            mergeable=False, too_many=True, conflict_files=many,
        )

        http_patch, http_client = _patch_httpx_post(_make_httpx_response(
            json_body={"ok": True, "reason": "plus_two_voted"},
        ))

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=enrichment)), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        # Hashtag was set (so we don't re-attempt) but merger NOT called.
        mock_client.add_hashtag.assert_awaited_once()
        http_client.post.assert_not_called()
        assert any("skip_reason=too_many_conflicts" in r.message
                   for r in caplog.records)

    async def test_skip_on_enrichment_error(self, caplog, merger_http_env):
        """Enrichment infrastructure failure → skip safely (next event retries)."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] x",
        })
        mock_client.add_hashtag = AsyncMock(return_value={"status": "ok"})

        from backend.agents.conflict_enrichment import EnrichmentResult
        broken = EnrichmentResult(
            mergeable=False, error="fetch develop failed: network down",
        )
        http_patch, http_client = _patch_httpx_post(_make_httpx_response())

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=broken)), \
             caplog.at_level("WARNING", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        http_client.post.assert_not_called()
        mock_client.add_hashtag.assert_not_called()
        assert any("skip_reason=enrichment_error" in r.message
                   for r in caplog.records)

    async def test_swallows_http_exception(self, caplog, merger_http_env):
        """OP-718: backend HTTP error must NOT propagate — the daemon
        keeps running and the next patchset re-runs the pipeline. The
        helper now folds httpx failures into a synthetic reason= line
        instead of raising, so the outcome row is still emitted."""
        import httpx as _httpx
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] x",
        })
        mock_client.add_hashtag = AsyncMock(return_value={"status": "ok"})

        from backend.agents.conflict_enrichment import (
            ConflictFile, EnrichmentResult,
        )
        enrichment = EnrichmentResult(
            mergeable=False,
            conflict_files=[ConflictFile(
                path="x.py",
                conflict_text="<<<<<<< x\na\n=======\nb\n>>>>>>> y\n",
                file_context="",
            )],
        )

        http_patch, http_client = _patch_httpx_post(
            _httpx.ConnectError("backend down"),
        )

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=enrichment)), \
             caplog.at_level("INFO", logger="backend.routers.webhooks"):
            # Must not raise.
            await _proactive_merger_check(_event())

        http_client.post.assert_awaited_once()
        assert any(
            "merger_outcome reason=merger_http_request_error" in r.message
            for r in caplog.records
        )

    async def test_continues_when_set_hashtag_fails(
        self, caplog, merger_http_env,
    ):
        """Throttle marker is best-effort — merger still runs even if it fails."""
        mock_client = MagicMock()
        mock_client.query_change = AsyncMock(return_value={
            "hashtags": [], "subject": "[OP-92] x",
        })
        mock_client.add_hashtag = AsyncMock(
            side_effect=RuntimeError("ssh unreachable"),
        )

        from backend.agents.conflict_enrichment import (
            ConflictFile, EnrichmentResult,
        )
        enrichment = EnrichmentResult(
            mergeable=False,
            conflict_files=[ConflictFile(
                path="x.py",
                conflict_text="<<<<<<< x\na\n=======\nb\n>>>>>>> y\n",
                file_context="",
            )],
        )

        http_patch, http_client = _patch_httpx_post(_make_httpx_response(
            json_body={"ok": True, "reason": "abstained_low_confidence"},
        ))

        with patch("backend.gerrit.gerrit_client", mock_client), \
             http_patch, \
             patch("backend.agents.conflict_enrichment.enrich_via_local_merge",
                   AsyncMock(return_value=enrichment)), \
             caplog.at_level("WARNING", logger="backend.routers.webhooks"):
            await _proactive_merger_check(_event())

        # Merger still invoked despite the hashtag failure
        http_client.post.assert_awaited_once()
        assert any("set_hashtag_failed" in r.message
                   for r in caplog.records)


# ──────────────────────────────────────────────────────────────────
# OP-718 — _post_merge_conflict_to_backend (HTTP delegate unit tests)
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPostMergeConflictToBackend:
    """Direct tests on the HTTP-delegate helper so the auth + URL +
    error-branch logic is covered without going through the full
    proactive-merger decision flow."""

    def _task(self):
        from backend.merge_arbiter import MergeConflictTask
        return MergeConflictTask(
            change_id="92",
            project="omnisight/Productizer",
            file_path="src/x.py",
            conflict_text="<<<<<<< a\n=======\n>>>>>>> b\n",
            patchset_revision="abc123",
            jira_ticket="OP-92",
        )

    async def test_missing_api_key_short_circuits(self, monkeypatch):
        """No OMNISIGHT_GERRIT_WEBHOOK_API_KEY → return synthetic error
        without making any network call. Reproduces the pre-OP-718
        symptom where daemon env was missing the credential."""
        monkeypatch.delenv(
            "OMNISIGHT_GERRIT_WEBHOOK_API_KEY", raising=False,
        )
        monkeypatch.setenv("OMNISIGHT_JIRA_WEBHOOK_SECRET", "s")

        factory = MagicMock()
        with patch(
            "backend.routers.webhooks.httpx.AsyncClient", factory,
        ):
            outcome = await _post_merge_conflict_to_backend(self._task())

        assert outcome["reason"] == "merger_http_missing_credentials"
        assert outcome["ok"] is False
        factory.assert_not_called()

    async def test_missing_jira_secret_short_circuits(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_GERRIT_WEBHOOK_API_KEY", "k")
        monkeypatch.delenv(
            "OMNISIGHT_JIRA_WEBHOOK_SECRET", raising=False,
        )

        factory = MagicMock()
        with patch(
            "backend.routers.webhooks.httpx.AsyncClient", factory,
        ):
            outcome = await _post_merge_conflict_to_backend(self._task())

        assert outcome["reason"] == "merger_http_missing_credentials"
        factory.assert_not_called()

    async def test_uses_backend_url_override(self, merger_http_env, monkeypatch):
        """OMNISIGHT_BACKEND_URL overrides the default localhost target."""
        monkeypatch.setenv("OMNISIGHT_BACKEND_URL", "http://omni:9000/")
        http_patch, http_client = _patch_httpx_post(_make_httpx_response(
            json_body={"ok": True, "reason": "plus_two_voted"},
        ))
        with http_patch:
            outcome = await _post_merge_conflict_to_backend(self._task())

        url = http_client.post.await_args.args[0]
        assert url == "http://omni:9000/api/v1/orchestrator/merge-conflict"
        assert outcome["reason"] == "plus_two_voted"

    async def test_non_200_response_folds_to_status_reason(
        self, merger_http_env,
    ):
        """A 401 (bad bearer) becomes ``reason=merger_http_status_401``
        so operators see the auth failure in the daemon log instead of a
        silent miss."""
        http_patch, _ = _patch_httpx_post(_make_httpx_response(
            status_code=401, text="Invalid Jira webhook secret",
        ))
        with http_patch:
            outcome = await _post_merge_conflict_to_backend(self._task())

        assert outcome["reason"] == "merger_http_status_401"
        assert outcome["ok"] is False

    async def test_invalid_json_response_folds_to_reason(
        self, merger_http_env,
    ):
        """A 200 with a non-JSON body still produces a structured
        reason= so the caller's logger line stays uniform."""
        response = _make_httpx_response(status_code=200, text="<html>")
        response.json.side_effect = ValueError("not json")
        http_patch, _ = _patch_httpx_post(response)
        with http_patch:
            outcome = await _post_merge_conflict_to_backend(self._task())

        assert outcome["reason"] == "merger_http_invalid_json"

    async def test_request_error_folds_to_reason(self, merger_http_env):
        """Network failure (ConnectError / TimeoutException) folds to
        the synthetic reason instead of raising."""
        import httpx as _httpx
        http_patch, _ = _patch_httpx_post(
            _httpx.ConnectError("connection refused"),
        )
        with http_patch:
            outcome = await _post_merge_conflict_to_backend(self._task())

        assert outcome["reason"] == "merger_http_request_error"
        assert "ConnectError" in outcome["detail"]
