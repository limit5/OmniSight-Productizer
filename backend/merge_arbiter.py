"""O7 (#270) — Merge Arbiter.

The Arbiter sits between Gerrit webhook events and the O6 Merger Agent
and glues the dual-+2 submit-rule into a fully automated CI/CD merge
pipeline.

Responsibilities
----------------

1. **Webhook intake** — invoked by ``POST /orchestrator/merge-conflict``
   when Gerrit (or a GitHub Action fallback) reports a merge conflict.
   Builds a :class:`MergeConflictTask` and hands it to the Merger Agent.

2. **Post-merger routing** — once the merger has voted:
     * ``plus_two_voted`` → emit SSE ``orchestration.change.awaiting_human_plus_two``
       so the UI / Slack / email bridge can prompt a human.
     * ``abstained_*`` (O6 gate not met) → file a JIRA ticket and
       assign the original CATC owner; change stays in "work-in-progress".
     * ``refused_*`` (security / test / escalated) → same JIRA route +
       Slack red alert.

3. **Human-vote reconciliation** — public entry point
   :func:`on_human_vote_recorded` reconciles a subsequent human vote:
     * Human +2 → submit-rule is satisfied, Gerrit auto-submits
       (best-effort call into ``gerrit_client.submit_change``).
     * Human -1 / -2 → revert the Merger's +2 with an explanatory
       comment ("human disagrees, merger withdraws"), flip change to
       work-in-progress, clear the failure counter.
     * Human +1 → no-op (below gate, waits for another vote).

4. **Submit-rule pre-check** — exposes :func:`check_change_ready` so
   any caller can ask "are we green?" using the same SSOT
   (``backend.submit_rule.evaluate_submit_rule``) as the Gerrit
   Prolog rule.

Design properties
-----------------

* Every external collaborator is injected — the Gerrit client, the
  JIRA client, the Merger runner, the Slack/webhook notifier.  Tests
  substitute deterministic stubs with zero network.
* No global mutable state — the process is stateless; the Gerrit
  change itself is the source of truth.  The in-memory
  ``_pending_abstains`` cache exists only to de-dupe JIRA ticket
  creation when the same change re-fires.
* All handled failures are surfaced as :class:`ArbiterOutcome` with a
  stable ``reason`` enum; this module never raises on the hot path.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from backend import merger_agent as ma
from backend.submit_rule import (
    ReviewerVote,
    SubmitDecision,
    SubmitReason,
    evaluate_submit_rule,
)

logger = logging.getLogger(__name__)


async def asyncio_wait_for_thread(
    fn: Callable[[], ma.ResolutionOutcome],
    *,
    timeout: int,
) -> ma.ResolutionOutcome:
    return await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)


def _run_async_blocking(awaitable):
    return asyncio.run(awaitable)


def _pytest_identifiers(identifiers: list[str]) -> list[str]:
    safe: list[str] = []
    seen: set[str] = set()
    for ident in identifiers:
        if ident.isidentifier() and ident not in seen:
            seen.add(ident)
            safe.append(ident)
    return safe


def _extract_failed_test(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            return stripped.split()[1]
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("______") and stripped.endswith("______"):
            return stripped.strip("_ ").strip()
    return ""


def _pytest_runtime_seconds(output: str) -> float:
    total = 0.0
    duration_re = re.compile(
        r"^\s*(?P<seconds>\d+(?:\.\d+)?)s\s+(?P<phase>setup|call|teardown)\s+"
    )
    for line in output.splitlines():
        match = duration_re.match(line)
        if match and match.group("phase") == "call":
            total += float(match.group("seconds"))
    return total


def _classify_pytest_timeout(output: str, timeout: float) -> str:
    if _pytest_runtime_seconds(output) > (timeout * 0.5):
        return "slow"
    return "hung"


def _timeout_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts: list[str] = []
    for value in (stdout, stderr):
        if isinstance(value, bytes):
            parts.append(value.decode(errors="replace"))
        elif value:
            parts.append(value)
    return "".join(parts)


def _remaining_seconds(deadline: float) -> float:
    return max(0.1, deadline - time.monotonic())


def _code_fence_for(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def _collapsed_transcript_block(title: str, transcript: str) -> list[str]:
    fence = _code_fence_for(transcript)
    return [
        f"<details><summary>{title}</summary>",
        "",
        f"{fence}text",
        transcript,
        fence,
        "",
        "</details>",
    ]


def _llm_transcript(*, prompt: str, response: str) -> str:
    parts: list[str] = []
    if prompt:
        parts.extend(["Prompt:", prompt])
    if response:
        if parts:
            parts.append("")
        parts.extend(["Response:", response])
    return "\n".join(parts)


def build_abstain_ticket_description(
    *,
    parent: str,
    assignee: str,
    change_id: str,
    merger_reason: str,
    merger_rationale: str,
    file_path: str,
    proposer_transcript: str = "",
    reviewer_transcript: str = "",
) -> str:
    lines = [
        "## Merger Abstain",
        f"- Parent: {parent or '(none)'}",
        f"- Assignee: {assignee}",
        f"- Change-Id: {change_id}",
        f"- File: `{file_path}`",
        f"- Reason: `{merger_reason}`",
        "",
        "## Rationale",
        merger_rationale,
    ]
    if proposer_transcript or reviewer_transcript:
        lines += ["", "## 2-LLM Sandwich Disagreement"]
        if proposer_transcript:
            lines += ["", *_collapsed_transcript_block(
                "LLM-A proposal transcript", proposer_transcript,
            )]
        if reviewer_transcript:
            lines += ["", *_collapsed_transcript_block(
                "LLM-B review transcript", reviewer_transcript,
            )]
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HUMAN_PENDING_WARN_HOURS = int(
    os.environ.get("OMNISIGHT_ARBITER_HUMAN_WARN_HOURS", "24")
)
VERIFY_TIMEOUT_SECONDS = int(
    os.environ.get("OMNISIGHT_MERGER_VERIFY_TIMEOUT_SECONDS", "120")
)
PYTEST_TIMEOUT_SECONDS = int(
    os.environ.get("OMNISIGHT_MERGER_PYTEST_TIMEOUT_SECONDS", "60")
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ArbiterReason(str, Enum):
    """Stable outcome codes — part of the HTTP + audit contract."""

    merger_plus_two_awaiting_human = "merger_plus_two_awaiting_human"
    merger_abstained_jira_ticket_opened = "merger_abstained_jira_ticket_opened"
    merger_refused_security = "merger_refused_security"
    merger_refused_test_failure = "merger_refused_test_failure"
    merger_refused_escalated = "merger_refused_escalated"
    merger_refused_other = "merger_refused_other"
    # OP-1196 phase 3 — the merger resolved the conflict (LLM produced
    # resolved_text at adequate confidence) but the caller asked the
    # backend NOT to push (``MergeConflictTask.push_locally=False``).
    # The caller (daemon) must read ``merger_outcome.resolved_text``,
    # apply it to its own workspace, amend as merger-agent-bot,
    # push, and post the +2 vote. Backend does NOT open a JIRA
    # abstain ticket in this path — the caller is expected to
    # complete the resolution; if the caller-side push later fails,
    # the caller is responsible for surfacing that failure (via SSE,
    # a fresh POST back into backend, or operator notification).
    merger_resolved_pending_caller_push = "merger_resolved_pending_caller_push"

    submitted = "submitted"
    human_disagreed_merger_withdrew = "human_disagreed_merger_withdrew"
    human_vote_recorded_below_gate = "human_vote_recorded_below_gate"

    invalid_payload = "invalid_payload"


@dataclass
class MergeConflictTask:
    """Webhook payload the Arbiter expects."""

    change_id: str
    project: str
    file_path: str
    conflict_text: str
    head_commit_message: str = ""
    incoming_commit_message: str = ""
    file_context: str = ""
    patchset_revision: str = ""
    workspace: str | None = None
    change_number: str = ""
    additional_files: list[str] = field(default_factory=list)
    sibling_file_contents: dict[str, str] = field(default_factory=dict)
    git_logs: dict[str, str] = field(default_factory=dict)
    symbol_table: dict[str, str] = field(default_factory=dict)
    jira_ticket: str = ""                # parent story (for abstain ticket)
    jira_description: str = ""
    catc_owner: str = ""                 # original CATC assignee
    guild_id: str = ""
    size: str = ""
    # OP-1196 phase 3 — when False, the merger pipeline runs through
    # the LLM and populates ``ResolutionOutcome.resolved_text`` but
    # the in-process push step is SKIPPED. The caller (typically the
    # gerrit-jira-bridge daemon) is then responsible for applying
    # the resolution + amending + pushing as merger-agent-bot using
    # its own workspace + SSH key. See OP-1196 α-vs-C analysis for
    # why this split exists.
    push_locally: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MergeConflictTask":
        return cls(
            change_id=str(d.get("change_id") or d.get("changeId") or ""),
            project=str(d.get("project") or ""),
            file_path=str(d.get("file_path") or d.get("filePath") or ""),
            conflict_text=str(d.get("conflict_text") or d.get("conflictText") or ""),
            head_commit_message=str(d.get("head_commit_message") or ""),
            incoming_commit_message=str(d.get("incoming_commit_message") or ""),
            file_context=str(d.get("file_context") or d.get("fileContext") or ""),
            patchset_revision=str(d.get("patchset_revision") or d.get("revision") or ""),
            workspace=d.get("workspace"),
            change_number=str(d.get("change_number") or ""),
            additional_files=list(d.get("additional_files") or []),
            sibling_file_contents=dict(d.get("sibling_file_contents") or {}),
            git_logs=dict(d.get("git_logs") or {}),
            symbol_table=dict(d.get("symbol_table") or {}),
            jira_ticket=str(d.get("jira_ticket") or ""),
            jira_description=str(d.get("jira_description") or ""),
            catc_owner=str(d.get("catc_owner") or ""),
            guild_id=str(d.get("guild_id") or d.get("guild") or ""),
            size=str(
                d.get("size") or d.get("t_shirt_size") or d.get("tshirt_size") or ""
            ),
            # OP-1196 phase 3 — default True for backwards compat. The
            # daemon sets this to False; older clients that don't
            # know about the field stay on the in-process push path.
            push_locally=bool(d.get("push_locally", True)),
        )


@dataclass
class ArbiterOutcome:
    """Result of a webhook / reconciliation call."""

    change_id: str
    reason: ArbiterReason
    detail: str = ""
    merger_outcome: dict[str, Any] | None = None
    submit_decision: dict[str, Any] | None = None
    jira_ticket_created: str | None = None
    awaiting_human_since: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason"] = self.reason.value
        return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pluggable collaborators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


MergerRunner = Callable[[ma.ConflictRequest], Awaitable[ma.ResolutionOutcome]]


class JiraTicketOpener(Protocol):
    """Opens a JIRA ticket when the Merger abstains.  Tests inject a
    stub that records the call; production wires in ``jira_adapter``."""

    async def open_abstain_ticket(
        self,
        *,
        parent: str,
        assignee: str,
        change_id: str,
        merger_reason: str,
        merger_rationale: str,
        file_path: str,
        proposer_transcript: str = "",
        reviewer_transcript: str = "",
    ) -> "JiraTicketResult": ...


@dataclass
class JiraTicketResult:
    ok: bool
    ticket: str = ""
    url: str = ""
    reason: str = ""


class Notifier(Protocol):
    """SSE / Slack / email bridge.  Tests use a stub that records
    events."""

    async def notify(
        self,
        *,
        kind: str,
        change_id: str,
        payload: dict[str, Any],
    ) -> None: ...


class GerritSubmitter(Protocol):
    """Wraps ``gerrit_client.submit_change`` so tests can assert submit
    was called without needing Gerrit SSH."""

    async def submit(self, *, commit: str, project: str) -> dict: ...


class GerritVoteRevoker(Protocol):
    """Posts a ``Code-Review: 0`` to withdraw the Merger's prior +2
    vote when a human disagrees."""

    async def revoke(
        self,
        *,
        commit: str,
        project: str,
        message: str,
    ) -> dict: ...


class ResolutionVerifier(Protocol):
    async def verify_and_push(
        self,
        *,
        task: MergeConflictTask,
        outcome: ma.ResolutionOutcome,
    ) -> ma.ResolutionOutcome: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Default collaborator wrappers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _default_merger_runner(req: ma.ConflictRequest) -> ma.ResolutionOutcome:
    return await ma.resolve_conflict(req)


class _DefaultJiraOpener:
    """Real JIRA ticket opener — lazy-imports ``jira_adapter`` so tests
    that don't touch JIRA don't need the settings wired up."""

    async def open_abstain_ticket(
        self,
        *,
        parent: str,
        assignee: str,
        change_id: str,
        merger_reason: str,
        merger_rationale: str,
        file_path: str,
        proposer_transcript: str = "",
        reviewer_transcript: str = "",
    ) -> JiraTicketResult:
        description = build_abstain_ticket_description(
            parent=parent,
            assignee=assignee,
            change_id=change_id,
            merger_reason=merger_reason,
            merger_rationale=merger_rationale,
            file_path=file_path,
            proposer_transcript=proposer_transcript,
            reviewer_transcript=reviewer_transcript,
        )
        try:
            from backend import jira_adapter as _ja  # noqa: F401
        except Exception as exc:
            logger.debug("arbiter: jira_adapter unavailable: %s", exc)
            return JiraTicketResult(
                ok=False,
                reason=f"jira_adapter unavailable: {exc}",
            )
        # The project-level JIRA client is initialised per-tenant via
        # ``intent_bridge`` — without a live tenant we can't actually
        # open the ticket here.  We surface a recorded-intent outcome so
        # the arbiter still returns a deterministic result; the
        # orchestrator / operator UI picks up the "awaiting" SSE event
        # and a human operator files the ticket.  The stub makes the
        # contract visible in the outcome ``detail``.
        return JiraTicketResult(
            ok=False,
            reason=(
                "no live JIRA client bound; ticket creation deferred to "
                "intent_bridge (see _default_jira_opener note); "
                f"description_len={len(description)}"
            ),
        )


class _DefaultNotifier:
    async def notify(
        self,
        *,
        kind: str,
        change_id: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            from backend.events import emit_invoke
            emit_invoke(
                f"orchestration.{kind}",
                f"{kind}: {change_id}",
                change_id=change_id,
                **payload,
            )
        except Exception as exc:                         # pragma: no cover
            logger.debug("arbiter notify SSE failed: %s", exc)


class _DefaultGerritSubmitter:
    async def submit(self, *, commit: str, project: str) -> dict:
        try:
            from backend.gerrit import gerrit_client
            return await gerrit_client.submit_change(commit=commit, project=project)
        except Exception as exc:                         # pragma: no cover
            return {"error": f"gerrit submit_change failed: {exc}"}


class _DefaultGerritVoteRevoker:
    async def revoke(
        self,
        *,
        commit: str,
        project: str,
        message: str,
    ) -> dict:
        try:
            from backend.gerrit import gerrit_client
            return await gerrit_client.post_review(
                commit=commit,
                message=message,
                labels={"Code-Review": 0},
                project=project,
            )
        except Exception as exc:                         # pragma: no cover
            return {"error": f"gerrit revoke failed: {exc}"}


@dataclass
class VerifyRunResult:
    ok: bool
    stage: str
    summary: str
    verify_outcome: str = ""
    command: str = ""
    stdout: str = ""
    scratch_path: str = ""
    failed_test: str = ""


class _DefaultResolutionVerifier:
    """Build-then-verify gate for LLM conflict resolutions.

    The merger agent is invoked in deferred-push mode so this verifier can
    apply the resolved file to a scratch git worktree, scan changed files
    for conflict markers, run py_compile over changed Python files, run a
    targeted pytest subset plus replay fixtures, then push from the scratch
    worktree only if every check is green.
    """

    def __init__(
        self,
        *,
        pusher: ma.PatchsetPusher | None = None,
        reviewer: ma.GerritReviewer | None = None,
        hashtag_setter: ma.HashtagSetter | None = None,
    ) -> None:
        self._pusher = pusher or ma.GitPatchsetPusher()
        self._reviewer = reviewer or ma.GerritClientReviewer()
        self._hashtag_setter = hashtag_setter or ma.GerritClientHashtagSetter()

    async def verify_and_push(
        self,
        *,
        task: MergeConflictTask,
        outcome: ma.ResolutionOutcome,
    ) -> ma.ResolutionOutcome:
        try:
            return await asyncio_wait_for_thread(
                lambda: self._verify_and_push_sync(task, outcome),
                timeout=VERIFY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return self._failed_outcome(
                task,
                outcome,
                VerifyRunResult(
                    ok=False,
                    stage="timeout",
                    summary=(
                        f"verify step exceeded {VERIFY_TIMEOUT_SECONDS}s "
                        "before push"
                    ),
                    verify_outcome="hung",
                ),
            )
        except Exception as exc:
            return self._failed_outcome(
                task,
                outcome,
                VerifyRunResult(
                    ok=False,
                    stage="scratch",
                    summary=f"verify setup failed: {exc}",
                ),
            )

    def _verify_and_push_sync(
        self,
        task: MergeConflictTask,
        outcome: ma.ResolutionOutcome,
    ) -> ma.ResolutionOutcome:
        if not task.workspace:
            return self._failed_outcome(
                task,
                outcome,
                VerifyRunResult(
                    ok=False,
                    stage="scratch",
                    summary="no workspace provided for scratch verification",
                ),
            )

        deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
        scratch = self._create_scratch_worktree(task.workspace)
        try:
            resolved_files = self._resolved_file_texts(
                task,
                outcome,
            )
            for file_path, resolved_text in resolved_files.items():
                target = Path(scratch) / file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(resolved_text, encoding="utf-8")

            touched_files = self._touched_files(task, resolved_files)
            result = self._run_conflict_marker_scan(
                touched_files,
                cwd=scratch,
                timeout=_remaining_seconds(deadline),
            )
            if not result.ok:
                return self._failed_outcome(task, outcome, result)

            py_files = [p for p in touched_files if p.endswith(".py")]
            for file_path in py_files:
                result = self._run(
                    ["python3", "-m", "py_compile", file_path],
                    cwd=scratch,
                    stage="py_compile",
                    timeout=_remaining_seconds(deadline),
                )
                if not result.ok:
                    return self._failed_outcome(task, outcome, result)

            identifiers = _pytest_identifiers(
                self._changed_identifiers(outcome),
            )
            if identifiers:
                k_expr = " or ".join(identifiers)
                pytest_args = [
                    "python3", "-m", "pytest", "-k", k_expr,
                    f"--timeout={PYTEST_TIMEOUT_SECONDS}", "--durations=0", "-x",
                ]
                result = self._run(
                    pytest_args,
                    cwd=scratch,
                    stage="pytest",
                    timeout=_remaining_seconds(deadline),
                )
                if not result.ok:
                    return self._failed_outcome(task, outcome, result)
            else:
                result = VerifyRunResult(
                    ok=True,
                    stage="pytest",
                    summary="pytest skipped: no changed identifiers extracted",
                    verify_outcome="green",
                    scratch_path=scratch,
                )

            result = self._run_replay_fixture_sweep(
                cwd=scratch,
                timeout=_remaining_seconds(deadline),
            )
            if not result.ok:
                return self._failed_outcome(task, outcome, result)

            if task.push_locally:
                return self._push_verified(task, outcome, scratch, result)
            return self._verified_deferred(task, outcome, result)
        finally:
            self._cleanup_scratch(task.workspace, scratch)

    @staticmethod
    def _touched_files(
        task: MergeConflictTask,
        resolved_files: dict[str, str] | None = None,
    ) -> list[str]:
        touched: list[str] = []
        for file_path in [
            task.file_path,
            *task.additional_files,
            *(resolved_files or {}).keys(),
        ]:
            if file_path and file_path not in touched:
                touched.append(file_path)
        return touched

    @staticmethod
    def _resolved_file_texts(
        task: MergeConflictTask,
        outcome: ma.ResolutionOutcome,
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        if task.file_path:
            resolved[task.file_path] = outcome.resolved_text

        raw = outcome.metadata.get("resolved_files")
        if isinstance(raw, dict):
            for file_path, text in raw.items():
                if isinstance(file_path, str) and isinstance(text, str):
                    resolved[file_path] = text

        raw = outcome.metadata.get("file_resolutions")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                file_path = item.get("file_path") or item.get("path")
                text = item.get("resolved_text") or item.get("text")
                if isinstance(file_path, str) and isinstance(text, str):
                    resolved[file_path] = text
        elif isinstance(raw, dict):
            for file_path, text in raw.items():
                if isinstance(file_path, str) and isinstance(text, str):
                    resolved[file_path] = text

        return resolved

    @staticmethod
    def _changed_identifiers(outcome: ma.ResolutionOutcome) -> list[str]:
        identifiers: list[str] = []
        for ident in outcome.changed_identifiers:
            if ident not in identifiers:
                identifiers.append(ident)

        by_file = outcome.metadata.get("changed_identifiers_by_file")
        if isinstance(by_file, dict):
            for values in by_file.values():
                if not isinstance(values, list):
                    continue
                for ident in values:
                    if isinstance(ident, str) and ident not in identifiers:
                        identifiers.append(ident)

        raw = outcome.metadata.get("file_resolutions")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                values = item.get("changed_identifiers")
                if not isinstance(values, list):
                    continue
                for ident in values:
                    if isinstance(ident, str) and ident not in identifiers:
                        identifiers.append(ident)

        return identifiers

    def _run_conflict_marker_scan(
        self,
        file_paths: list[str],
        *,
        cwd: str,
        timeout: float,
    ) -> VerifyRunResult:
        if not file_paths:
            return VerifyRunResult(
                ok=True,
                stage="conflict_marker",
                summary="conflict_marker skipped: no changed files",
                verify_outcome="green",
                scratch_path=cwd,
            )

        result = self._run(
            ["grep", "-E", r"^(<<<<<<<|=======|>>>>>>>)", "--", *file_paths],
            cwd=cwd,
            stage="conflict_marker",
            timeout=timeout,
        )
        if result.ok:
            result.ok = False
            result.summary = "conflict_marker failed: conflict markers found"
            result.verify_outcome = "red"
            return result
        if "exit 1" in result.summary:
            result.ok = True
            result.summary = "conflict_marker passed"
            result.verify_outcome = "green"
        return result

    def _run_replay_fixture_sweep(
        self,
        *,
        cwd: str,
        timeout: float,
    ) -> VerifyRunResult:
        replay_suite = Path(cwd) / "backend/tests/test_merger_replay.py"
        if not replay_suite.exists():
            return VerifyRunResult(
                ok=True,
                stage="replay",
                summary="replay skipped: backend/tests/test_merger_replay.py missing",
                verify_outcome="green",
                scratch_path=cwd,
            )
        return self._run(
            [
                "python3", "-m", "pytest", "backend/tests/test_merger_replay.py",
                f"--timeout={PYTEST_TIMEOUT_SECONDS}", "--durations=0", "-x",
            ],
            cwd=cwd,
            stage="replay",
            timeout=timeout,
        )

    def _create_scratch_worktree(self, workspace: str) -> str:
        parent = tempfile.mkdtemp(prefix="merger-verify-")
        scratch = str(Path(parent) / "worktree")
        proc = subprocess.run(
            ["git", "-C", workspace, "worktree", "add", "--detach", scratch, "HEAD"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            shutil.rmtree(parent, ignore_errors=True)
            raise RuntimeError(proc.stderr or proc.stdout or "git worktree add failed")
        return scratch

    @staticmethod
    def _cleanup_scratch(workspace: str, scratch: str) -> None:
        subprocess.run(
            ["git", "-C", workspace, "worktree", "remove", "--force", scratch],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(str(Path(scratch).parent), ignore_errors=True)

    @staticmethod
    def _run(
        args: list[str],
        *,
        cwd: str,
        stage: str,
        timeout: float,
    ) -> VerifyRunResult:
        command = " ".join(shlex.quote(a) for a in args)
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = _timeout_output(exc.stdout, exc.stderr)
            verify_outcome = (
                _classify_pytest_timeout(output, timeout)
                if stage == "pytest" else "hung"
            )
            return VerifyRunResult(
                ok=False,
                stage=stage,
                summary=(
                    f"{stage} timed out after {timeout:.1f}s "
                    f"(verify_outcome={verify_outcome})"
                ),
                verify_outcome=verify_outcome,
                command=command,
                stdout=output,
                scratch_path=cwd,
            )
        output = (proc.stdout or "") + (proc.stderr or "")
        return VerifyRunResult(
            ok=proc.returncode == 0,
            stage=stage,
            summary=(
                f"{stage} passed" if proc.returncode == 0
                else f"{stage} failed with exit {proc.returncode}"
            ),
            verify_outcome="green" if proc.returncode == 0 else "red",
            command=command,
            stdout=output,
            failed_test=_extract_failed_test(output),
            scratch_path=cwd,
        )

    def _push_verified(
        self,
        task: MergeConflictTask,
        outcome: ma.ResolutionOutcome,
        scratch: str,
        verify_result: VerifyRunResult,
    ) -> ma.ResolutionOutcome:
        resolution = ma.Resolution(
            resolved_text=outcome.resolved_text,
            confidence=outcome.confidence,
            rationale=outcome.rationale,
            diff=outcome.diff_preview,
            changed_blocks=1,
            changed_identifiers=list(outcome.changed_identifiers),
        )
        push = _run_async_blocking(self._pusher.push(
            change_id=task.change_id,
            project=task.project,
            workspace=scratch,
            file_path=task.file_path,
            resolved_text=outcome.resolved_text,
            commit_message=ma._build_patchset_message(  # type: ignore[attr-defined]
                ma.ConflictRequest(
                    change_id=task.change_id,
                    project=task.project,
                    file_path=task.file_path,
                    conflict_text=task.conflict_text,
                    patchset_revision=task.patchset_revision,
                    workspace=scratch,
                    additional_files=list(task.additional_files),
                ),
                resolution,
            ),
        ))
        if not push.ok:
            failed = VerifyRunResult(
                ok=False,
                stage="push",
                summary=f"Gerrit push failed: {push.reason}",
                scratch_path=scratch,
            )
            return self._failed_outcome(task, outcome, failed)

        review = _run_async_blocking(self._reviewer.post_review(
            commit_sha=push.sha or task.patchset_revision,
            project=task.project,
            message=ma._build_review_message(  # type: ignore[attr-defined]
                ma.ConflictRequest(
                    change_id=task.change_id,
                    project=task.project,
                    file_path=task.file_path,
                    conflict_text=task.conflict_text,
                ),
                resolution,
                push,
            ),
            score=int(ma.LabelVote.plus_two),
        ))
        if not review.ok:
            return self._failed_outcome(
                task,
                outcome,
                VerifyRunResult(
                    ok=False,
                    stage="review",
                    summary=f"Gerrit review failed: {review.reason}",
                    scratch_path=scratch,
                ),
            )

        ht_res = _run_async_blocking(self._hashtag_setter.add_hashtag(
            change_id=task.change_id,
            project=task.project,
            hashtag=ma.CONFLICT_RESOLVED_HASHTAG,
        ))

        passed = ma.ResolutionOutcome(
            change_id=task.change_id,
            file_path=task.file_path,
            reason=ma.MergerReason.plus_two_voted,
            voted_score=ma.LabelVote.plus_two,
            confidence=outcome.confidence,
            rationale=outcome.rationale,
            diff_preview=outcome.diff_preview,
            push_sha=push.sha,
            review_url=push.review_url,
            test_result={
                "ok": True,
                "summary": verify_result.summary,
                "command": verify_result.command,
            },
            metadata={
                **outcome.metadata,
                "verify_result": "green",
                "verify_outcome": "green",
                "verify_stage": verify_result.stage,
                "hashtag_set_ok": ht_res.ok,
                "hashtag_set_reason": ht_res.reason,
            },
            changed_identifiers=list(outcome.changed_identifiers),
        )
        return passed

    @staticmethod
    def _verified_deferred(
        task: MergeConflictTask,
        outcome: ma.ResolutionOutcome,
        verify_result: VerifyRunResult,
    ) -> ma.ResolutionOutcome:
        return ma.ResolutionOutcome(
            change_id=task.change_id,
            file_path=task.file_path,
            reason=ma.MergerReason.deferred_push_to_caller,
            voted_score=ma.LabelVote.abstain,
            confidence=outcome.confidence,
            rationale=outcome.rationale,
            diff_preview=outcome.diff_preview,
            push_sha=outcome.push_sha,
            review_url=outcome.review_url,
            failure_count=outcome.failure_count,
            test_result={
                "ok": True,
                "summary": verify_result.summary,
                "command": verify_result.command,
            },
            metadata={
                **outcome.metadata,
                "verify_result": "green",
                "verify_stage": verify_result.stage,
            },
            changed_identifiers=list(outcome.changed_identifiers),
            resolved_text=outcome.resolved_text,
        )

    @staticmethod
    def _failed_outcome(
        task: MergeConflictTask,
        outcome: ma.ResolutionOutcome,
        result: VerifyRunResult,
    ) -> ma.ResolutionOutcome:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        tail = "\n".join(lines[-30:])
        failed_test = result.failed_test or "(no failed test identified)"
        rationale = (
            f"verify step failed at {result.stage}: {result.summary}; "
            f"failed_test={failed_test}; last_30_lines:\n{tail}"
        )
        failed = ma.ResolutionOutcome(
            change_id=task.change_id,
            file_path=task.file_path,
            reason=ma.MergerReason.refused_test_failure,
            voted_score=ma.LabelVote.abstain,
            confidence=outcome.confidence,
            rationale=rationale,
            diff_preview=outcome.diff_preview,
            test_result={
                "ok": False,
                "summary": result.summary,
                "command": result.command,
                "failed_test": failed_test,
                "last_30_lines": tail,
            },
            metadata={
                **outcome.metadata,
                "verify_result": "red",
                "verify_outcome": result.verify_outcome or "red",
                "verify_stage": result.stage,
            },
            changed_identifiers=list(outcome.changed_identifiers),
        )
        return failed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dependency bundle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ArbiterDeps:
    merger: MergerRunner = _default_merger_runner
    jira: JiraTicketOpener = field(default_factory=_DefaultJiraOpener)
    notifier: Notifier = field(default_factory=_DefaultNotifier)
    submitter: GerritSubmitter = field(default_factory=_DefaultGerritSubmitter)
    revoker: GerritVoteRevoker = field(default_factory=_DefaultGerritVoteRevoker)
    verifier: ResolutionVerifier = field(default_factory=_DefaultResolutionVerifier)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  De-dupe registry — avoid filing twice for the same abstain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_pending_abstains: dict[str, dict[str, Any]] = {}


def reset_arbiter_state_for_tests() -> None:
    _pending_abstains.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-LLM structural risk gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_DOCSTRING_DELIM_RE = re.compile(r"^\s*(?:[rRuUbBfF]{0,2})?('{3}|\"{3})")
_DOC_ENTRY_RE = re.compile(r"^\s*(?::param\s+|@param\s+)?([A-Za-z_][\w-]*)\s*:")


def _classify_risk(conflict_regions: list[ma.ConflictBlock]) -> ma.ConflictRisk:
    """Classify merge-conflict structure before invoking the merger LLM."""
    high: list[str] = []
    medium: list[str] = []

    for region in conflict_regions:
        head = _content_lines(region.head_lines)
        incoming = _content_lines(region.incoming_lines)

        if not head or not incoming:
            continue

        if _is_comment_or_doc_only(head) and _is_comment_or_doc_only(incoming):
            if _docstring_entry_keys(head) & _docstring_entry_keys(incoming):
                medium.append("docstring_entry_overlap")
            continue

        if _has_class_hierarchy_change(head, incoming):
            high.append("class_hierarchy_change")
        if _has_control_flow_overlap(head, incoming):
            high.append("control_flow_overlap")
        if _has_same_function_body_replacement(head, incoming):
            high.append("same_function_body_replacement")

        if _has_signature_overlap(head, incoming):
            medium.append("signature_overlap")
        if _has_overlapping_helper_function(head, incoming):
            medium.append("overlapping_helper_function")

        if high or medium:
            continue

        if not _has_distinct_new_symbols(head, incoming):
            medium.append("edit_overlap")

    if high and ma.is_constructor_behavior_signature_candidate(conflict_regions):
        high = [
            reason for reason in high
            if reason != "same_function_body_replacement"
        ]
        medium.append("signature_body_compat_candidate")

    if high:
        return ma.ConflictRisk(ma.MergerRiskTier.high, tuple(dict.fromkeys(high)))
    if medium:
        return ma.ConflictRisk(ma.MergerRiskTier.medium, tuple(dict.fromkeys(medium)))
    return ma.ConflictRisk(ma.MergerRiskTier.low, ())


def _content_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip()]


def _is_comment_or_doc_only(lines: list[str]) -> bool:
    in_docstring = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if in_docstring:
            if "'''" in stripped or '"""' in stripped:
                in_docstring = False
            continue
        if stripped.startswith("#"):
            continue
        if _DOCSTRING_DELIM_RE.match(stripped):
            if stripped.count("'''") == 1 and stripped.count('"""') == 0:
                in_docstring = True
            elif stripped.count('"""') == 1 and stripped.count("'''") == 0:
                in_docstring = True
            continue
        return False
    return True


def _docstring_entry_keys(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    for line in lines:
        match = _DOC_ENTRY_RE.match(line.strip().lstrip("#").strip())
        if match:
            keys.add(match.group(1))
    return keys


def _parse_snippet(lines: list[str]) -> ast.AST | None:
    source = textwrap.dedent("\n".join(lines)).strip("\n")
    if not source.strip():
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        wrapped = "def __conflict__():\n" + textwrap.indent(source, "    ")
        try:
            return ast.parse(wrapped)
        except SyntaxError:
            return None


def _function_signatures(lines: list[str]) -> dict[str, str]:
    tree = _parse_snippet(lines)
    if tree is None:
        return {}
    signatures: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "__conflict__":
                continue
            signatures[node.name] = ast.dump(node.args, include_attributes=False)
    return signatures


def _function_body_shapes(lines: list[str]) -> dict[str, str]:
    tree = _parse_snippet(lines)
    if tree is None:
        return {}
    bodies: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "__conflict__":
                continue
            body = ast.Module(body=node.body, type_ignores=[])
            bodies[node.name] = ast.dump(body, include_attributes=False)
    return bodies


def _class_bases(lines: list[str]) -> dict[str, str]:
    tree = _parse_snippet(lines)
    if tree is None:
        return {}
    bases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases[node.name] = ast.dump(node.bases, include_attributes=False)
    return bases


def _top_level_symbols(lines: list[str]) -> set[str]:
    tree = _parse_snippet(lines)
    if not isinstance(tree, ast.Module):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _control_flow_shapes(lines: list[str]) -> dict[str, set[str]]:
    tree = _parse_snippet(lines)
    if tree is None:
        return {}
    shapes: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            key = f"if:{ast.dump(node.test, include_attributes=False)}"
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            key = f"for:{ast.dump(node.target, include_attributes=False)}"
        elif isinstance(node, ast.While):
            key = f"while:{ast.dump(node.test, include_attributes=False)}"
        else:
            continue
        shapes.setdefault(key, set()).add(ast.dump(node, include_attributes=False))
    return shapes


def _has_signature_overlap(head: list[str], incoming: list[str]) -> bool:
    left = _function_signatures(head)
    right = _function_signatures(incoming)
    return any(name in right and right[name] != sig for name, sig in left.items())


def _has_overlapping_helper_function(head: list[str], incoming: list[str]) -> bool:
    left = _top_level_symbols(head)
    right = _top_level_symbols(incoming)
    return bool(left and right and left & right)


def _has_distinct_new_symbols(head: list[str], incoming: list[str]) -> bool:
    left = _top_level_symbols(head)
    right = _top_level_symbols(incoming)
    return bool(left and right and not (left & right))


def _has_class_hierarchy_change(head: list[str], incoming: list[str]) -> bool:
    left = _class_bases(head)
    right = _class_bases(incoming)
    return any(name in right and right[name] != bases for name, bases in left.items())


def _has_control_flow_overlap(head: list[str], incoming: list[str]) -> bool:
    left = _control_flow_shapes(head)
    right = _control_flow_shapes(incoming)
    for key, shapes in left.items():
        if key in right and shapes != right[key]:
            return True
    return False


def _has_same_function_body_replacement(head: list[str], incoming: list[str]) -> bool:
    left_signatures = _function_signatures(head)
    right_signatures = _function_signatures(incoming)
    left_bodies = _function_body_shapes(head)
    right_bodies = _function_body_shapes(incoming)
    for name, body in left_bodies.items():
        if (
            name in right_bodies
            and left_signatures.get(name) == right_signatures.get(name)
            and body != right_bodies[name]
        ):
            return True
    return False


def _risk_metadata(risk: ma.ConflictRisk) -> dict[str, Any]:
    return {
        "risk_tier": risk.tier.value,
        "risk_reasons": list(risk.reasons),
    }


def _take_both_resolution(conflict_text: str, blocks: list[ma.ConflictBlock]) -> str:
    resolved_blocks = [
        "\n".join([*block.head_lines, *block.incoming_lines])
        for block in blocks
    ]
    text = conflict_text
    matches = list(ma._CONFLICT_RE.finditer(text))  # type: ignore[attr-defined]
    for idx in range(len(matches) - 1, -1, -1):
        text = (
            text[: matches[idx].start()]
            + resolved_blocks[idx]
            + text[matches[idx].end() :]
        )
    return text


def _build_low_risk_outcome(
    task: MergeConflictTask,
    blocks: list[ma.ConflictBlock],
    risk: ma.ConflictRisk,
) -> ma.ResolutionOutcome:
    resolved_text = _take_both_resolution(task.conflict_text, blocks)
    return ma.ResolutionOutcome(
        change_id=task.change_id,
        file_path=task.file_path,
        reason=ma.MergerReason.deferred_push_to_caller,
        voted_score=ma.LabelVote.abstain,
        confidence=1.0,
        rationale="risk_tier=LOW; deterministic take-both resolution before LLM",
        diff_preview=ma._make_block_diff(  # type: ignore[attr-defined]
            task.file_path,
            blocks,
            [
                "\n".join([*block.head_lines, *block.incoming_lines])
                for block in blocks
            ],
        ),
        metadata={**_risk_metadata(risk), "deterministic_take_both": True},
        resolved_text=resolved_text,
    )


def _build_high_risk_outcome(
    task: MergeConflictTask,
    risk: ma.ConflictRisk,
) -> ma.ResolutionOutcome:
    reasons = ",".join(risk.reasons) or "structural_high_risk"
    return ma.ResolutionOutcome(
        change_id=task.change_id,
        file_path=task.file_path,
        reason=ma.MergerReason.refused_escalated,
        voted_score=ma.LabelVote.abstain,
        confidence=0.0,
        rationale=(
            "risk_tier=HIGH before LLM invocation; "
            f"reason={reasons}; human escalation required"
        ),
        diff_preview="",
        metadata=_risk_metadata(risk),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public entry points
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def on_merge_conflict_webhook(
    task: MergeConflictTask,
    *,
    deps: ArbiterDeps | None = None,
) -> ArbiterOutcome:
    """Called by ``POST /orchestrator/merge-conflict`` (Gerrit webhook
    or GitHub Actions fallback).

    Pipeline:
      1. Validate payload (required fields).
      2. Build a :class:`ma.ConflictRequest` and hand it to the Merger.
      3. Route on the merger's reason code.
    """
    deps = deps or ArbiterDeps()

    if not task.change_id or not task.project or not task.file_path:
        return ArbiterOutcome(
            change_id=task.change_id or "<unknown>",
            reason=ArbiterReason.invalid_payload,
            detail="change_id, project, and file_path are required",
        )

    blocks = ma.parse_conflict_block(task.conflict_text)
    if blocks:
        risk = _classify_risk(blocks)
        logger.info(
            "merge_arbiter.risk_tier=%s change_id=%s file=%s reasons=%s",
            risk.tier.value,
            task.change_id,
            task.file_path,
            ",".join(risk.reasons) or "none",
        )
        if risk.tier is ma.MergerRiskTier.low:
            return await _route_merger_outcome(
                task,
                _build_low_risk_outcome(task, blocks, risk),
                deps,
            )
        if risk.tier is ma.MergerRiskTier.high:
            return await _route_merger_outcome(
                task,
                _build_high_risk_outcome(task, risk),
                deps,
            )

    req = ma.ConflictRequest(
        change_id=task.change_id,
        project=task.project,
        file_path=task.file_path,
        conflict_text=task.conflict_text,
        head_commit_message=task.head_commit_message,
        incoming_commit_message=task.incoming_commit_message,
        file_context=task.file_context,
        patchset_revision=task.patchset_revision,
        workspace=task.workspace,
        change_number=task.change_number,
        jira_ticket=task.jira_ticket,
        jira_description=task.jira_description,
        additional_files=list(task.additional_files),
        sibling_file_contents=dict(task.sibling_file_contents),
        git_logs=dict(task.git_logs),
        symbol_table=dict(task.symbol_table),
        # OP-1196 phase 3 + OP-1405 — pass through the deferred-push
        # flag. OP-1405 routes local-push requests through the
        # arbiter's scratch-worktree verifier before the actual push:
        # the merger returns the resolved file only, the arbiter
        # py_compile + targeted-pytest verifies, and only green checks
        # cause the real push.
        push_locally=False if task.push_locally else task.push_locally,

    )
    merger_outcome = await deps.merger(req)
    sandwich_decision = merger_outcome.metadata.get("merger_sandwich_decision")
    if sandwich_decision:
        logger.info(
            "merger_sandwich_decision=%s change_id=%s file=%s",
            sandwich_decision,
            task.change_id,
            task.file_path,
        )
    return await _route_merger_outcome(task, merger_outcome, deps)


async def _route_merger_outcome(
    task: MergeConflictTask,
    outcome: ma.ResolutionOutcome,
    deps: ArbiterDeps,
) -> ArbiterOutcome:
    """Translate a merger outcome into an arbiter decision."""
    if outcome.reason is ma.MergerReason.plus_two_voted:
        now = time.time()
        _pending_abstains.pop(task.change_id, None)       # clear stale
        # O9 (#272) — register in the awaiting-human-+2 dashboard
        # registry so the orchestration panel + Prometheus gauge can
        # surface the dual-sign-pending count.
        try:
            from backend.orchestration_observability import (
                register_awaiting_human,
            )
            register_awaiting_human(
                change_id=task.change_id,
                project=task.project,
                file_path=task.file_path,
                merger_confidence=outcome.confidence,
                merger_rationale=outcome.rationale,
                review_url=outcome.review_url,
                push_sha=outcome.push_sha,
                awaiting_since=now,
                jira_ticket=task.jira_ticket,
                guild_id=task.guild_id,
                size=task.size,
            )
        except Exception as exc:                          # pragma: no cover
            logger.debug("arbiter: awaiting-human registry update failed: %s", exc)
        await deps.notifier.notify(
            kind="change.awaiting_human_plus_two",
            change_id=task.change_id,
            payload={
                "project": task.project,
                "file_path": task.file_path,
                "merger_confidence": outcome.confidence,
                "merger_rationale": outcome.rationale,
                "review_url": outcome.review_url,
                "push_sha": outcome.push_sha,
                "awaiting_since": now,
                "jira_ticket": task.jira_ticket,
                "guild_id": task.guild_id,
                "size": task.size,
            },
        )
        return ArbiterOutcome(
            change_id=task.change_id,
            reason=ArbiterReason.merger_plus_two_awaiting_human,
            detail=(
                f"Merger Agent cast +2 with confidence "
                f"{outcome.confidence:.2f}; waiting on human +2 from the "
                f"`non-ai-reviewer` group (hard gate)."
            ),
            merger_outcome=outcome.to_dict(),
            awaiting_human_since=now,
        )

    # OP-1196 phase 3 — deferred-push: the LLM produced a resolution
    # (confidence already passed the gate) but the caller asked the
    # backend NOT to push. Return the resolution back so the caller
    # can apply + amend + push as merger-agent-bot using its own
    # workspace + SSH key. Backend does NOT open a JIRA abstain
    # ticket here — the caller will complete the resolution.
    if outcome.reason is ma.MergerReason.deferred_push_to_caller:
        verified = await deps.verifier.verify_and_push(
            task=task,
            outcome=outcome,
        )
        logger.info(
            "merge_arbiter.verify_result=%s verify_outcome=%s "
            "change_id=%s stage=%s",
            verified.metadata.get("verify_result", "unknown"),
            verified.metadata.get("verify_outcome", "unknown"),
            task.change_id,
            verified.metadata.get("verify_stage", ""),
        )
        if verified.reason is not ma.MergerReason.deferred_push_to_caller:
            return await _route_merger_outcome(task, verified, deps)

        return ArbiterOutcome(
            change_id=task.change_id,
            reason=ArbiterReason.merger_resolved_pending_caller_push,
            detail=(
                f"Merger LLM produced a resolution at confidence "
                f"{outcome.confidence:.2f}; backend deferred the push "
                f"to the caller (push_locally=False). Caller is "
                f"responsible for applying merger_outcome.resolved_text "
                f"to its workspace, amending as merger-agent-bot, "
                f"pushing to refs/for/<branch>, and posting the +2 vote."
            ),
            merger_outcome=verified.to_dict(),
        )

    # Any non-+2 outcome — branch by reason.
    return await _handle_non_plus_two(task, outcome, deps)


async def _handle_non_plus_two(
    task: MergeConflictTask,
    outcome: ma.ResolutionOutcome,
    deps: ArbiterDeps,
) -> ArbiterOutcome:
    reason_map = {
        ma.MergerReason.refused_security_file:
            ArbiterReason.merger_refused_security,
        ma.MergerReason.refused_test_failure:
            ArbiterReason.merger_refused_test_failure,
        ma.MergerReason.refused_escalated:
            ArbiterReason.merger_refused_escalated,
    }
    abstains = {
        ma.MergerReason.abstained_low_confidence,
        ma.MergerReason.abstained_multi_file,
        ma.MergerReason.abstained_oversized,
        ma.MergerReason.refused_llm_unavailable,
        ma.MergerReason.refused_llm_invalid_json,
        ma.MergerReason.refused_new_logic_detected,
        ma.MergerReason.refused_review_objected,
    }

    if outcome.reason in reason_map:
        arb_reason = reason_map[outcome.reason]
    elif outcome.reason in abstains:
        arb_reason = ArbiterReason.merger_abstained_jira_ticket_opened
    else:
        arb_reason = ArbiterReason.merger_refused_other

    # Open JIRA ticket (de-duped by change-id).
    prior = _pending_abstains.get(task.change_id)
    if prior and prior.get("merger_reason") == outcome.reason.value:
        jira_ticket = prior.get("jira_ticket")
        jira_ok = bool(jira_ticket)
    else:
        assignee = task.catc_owner or "orchestrator-oncall"
        parent = task.jira_ticket or ""
        proposer_transcript = _llm_transcript(
            prompt=str(outcome.metadata.get("proposal_prompt", "") or ""),
            response=str(outcome.metadata.get("proposal_response", "") or ""),
        )
        reviewer_transcript = _llm_transcript(
            prompt=str(outcome.metadata.get("review_prompt", "") or ""),
            response=str(outcome.metadata.get("review_response", "") or ""),
        )
        res = await deps.jira.open_abstain_ticket(
            parent=parent,
            assignee=assignee,
            change_id=task.change_id,
            merger_reason=outcome.reason.value,
            merger_rationale=outcome.rationale,
            file_path=task.file_path,
            proposer_transcript=proposer_transcript,
            reviewer_transcript=reviewer_transcript,
        )
        jira_ticket = res.ticket if res.ok else ""
        jira_ok = res.ok
        _pending_abstains[task.change_id] = {
            "merger_reason": outcome.reason.value,
            "jira_ticket": jira_ticket,
            "opened_at": time.time(),
        }

    # Emit SSE so the UI / Slack bridge shows the abstain + ticket link.
    await deps.notifier.notify(
        kind="change.merger_abstain",
        change_id=task.change_id,
        payload={
            "project": task.project,
            "file_path": task.file_path,
            "merger_reason": outcome.reason.value,
            "merger_rationale": outcome.rationale,
            "jira_ticket": jira_ticket,
            "jira_opened_ok": jira_ok,
            "parent_jira": task.jira_ticket,
            "assignee": task.catc_owner,
            "merger_sandwich_decision": outcome.metadata.get(
                "merger_sandwich_decision", ""
            ),
            "proposal_transcript": outcome.metadata.get("proposal_response", ""),
            "review_transcript": outcome.metadata.get("review_response", ""),
        },
    )

    detail = (
        f"Merger outcome: {outcome.reason.value} — {outcome.rationale}. "
        f"{'JIRA ticket ' + jira_ticket + ' opened for human follow-up.' if jira_ok else 'JIRA ticket creation deferred (see SSE event).'}"
    )
    return ArbiterOutcome(
        change_id=task.change_id,
        reason=arb_reason,
        detail=detail,
        merger_outcome=outcome.to_dict(),
        jira_ticket_created=jira_ticket or None,
        metadata={"jira_opened_ok": jira_ok},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Human-vote reconciliation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def on_human_vote_recorded(
    *,
    change_id: str,
    project: str,
    commit: str,
    votes: list[ReviewerVote | dict[str, Any]],
    deps: ArbiterDeps | None = None,
) -> ArbiterOutcome:
    """Called when a human casts a Code-Review on a change the Merger
    has already +2'd.

    ``votes`` is the *full* vote list (merger + humans + other AI bots)
    so the evaluator can answer the submit question authoritatively.
    """
    deps = deps or ArbiterDeps()
    decision = evaluate_submit_rule(votes)

    # Negative from a human → Merger withdraws and change goes WIP.
    if decision.reason is SubmitReason.reject_negative_vote:
        revoke_res = await deps.revoker.revoke(
            commit=commit,
            project=project,
            message=(
                "Merger Agent withdraws +2: human reviewer cast a "
                f"negative score (voters: {', '.join(decision.negative_voters)}). "
                "Change returned to work-in-progress."
            ),
        )
        await deps.notifier.notify(
            kind="change.work_in_progress",
            change_id=change_id,
            payload={
                "project": project,
                "commit": commit,
                "negative_voters": decision.negative_voters,
                "revoke_ok": "error" not in revoke_res,
            },
        )
        # Re-arm the merger for the next patchset on this change.  We
        # clear only this change's strike counter, not the whole
        # registry, so other in-flight merges are unaffected.
        ma._reset_failure(change_id)  # type: ignore[attr-defined]
        # O9 (#272) — drop from awaiting-human dashboard registry.
        try:
            from backend.orchestration_observability import clear_awaiting_human
            clear_awaiting_human(change_id)
        except Exception as exc:                              # pragma: no cover
            logger.debug("arbiter: awaiting-human clear (withdrew) failed: %s", exc)
        return ArbiterOutcome(
            change_id=change_id,
            reason=ArbiterReason.human_disagreed_merger_withdrew,
            detail=(
                f"Human reviewer(s) {', '.join(decision.negative_voters)} "
                f"cast a negative score; merger withdrew its +2 and the "
                f"change is back in work-in-progress."
            ),
            submit_decision=decision.to_dict(),
            metadata={"revoke_response": revoke_res},
        )

    # All gates satisfied → submit.
    if decision.allow:
        submit_res = await deps.submitter.submit(commit=commit, project=project)
        submit_ok = "error" not in submit_res
        # O9 (#272) — change is shipping; drop from awaiting-human registry
        # regardless of submit_ok (a failed submit is operator-visible
        # via the "change.submitted" SSE event with submit_ok=false).
        try:
            from backend.orchestration_observability import clear_awaiting_human
            clear_awaiting_human(change_id)
        except Exception as exc:                              # pragma: no cover
            logger.debug("arbiter: awaiting-human clear (submitted) failed: %s", exc)
        await deps.notifier.notify(
            kind="change.submitted",
            change_id=change_id,
            payload={
                "project": project,
                "commit": commit,
                "submit_ok": submit_ok,
                "decision": decision.to_dict(),
            },
        )
        return ArbiterOutcome(
            change_id=change_id,
            reason=ArbiterReason.submitted,
            detail=(
                f"Dual-+2 satisfied (human +2 × {decision.human_plus_twos}, "
                f"merger +2 × {decision.merger_plus_twos}); submit call "
                f"{'succeeded' if submit_ok else 'failed'}."
            ),
            submit_decision=decision.to_dict(),
            metadata={"submit_response": submit_res},
        )

    # Otherwise still below the gate — emit an awaiting SSE so the UI
    # keeps showing the pending state.
    await deps.notifier.notify(
        kind="change.awaiting_more_votes",
        change_id=change_id,
        payload={
            "project": project,
            "missing": list(decision.missing),
            "decision": decision.to_dict(),
        },
    )
    return ArbiterOutcome(
        change_id=change_id,
        reason=ArbiterReason.human_vote_recorded_below_gate,
        detail=decision.detail,
        submit_decision=decision.to_dict(),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-check utility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def check_change_ready(
    votes: list[ReviewerVote | dict[str, Any]],
) -> SubmitDecision:
    """Return the current submit-rule verdict without side-effects.

    Callers: orchestrator status panel, observability dashboard, CLI.
    """
    return evaluate_submit_rule(votes)


__all__ = [
    "ArbiterDeps",
    "ArbiterOutcome",
    "ArbiterReason",
    "MergeConflictTask",
    "JiraTicketOpener",
    "JiraTicketResult",
    "Notifier",
    "GerritSubmitter",
    "GerritVoteRevoker",
    "_classify_risk",
    "build_abstain_ticket_description",
    "check_change_ready",
    "on_human_vote_recorded",
    "on_merge_conflict_webhook",
    "reset_arbiter_state_for_tests",
]
