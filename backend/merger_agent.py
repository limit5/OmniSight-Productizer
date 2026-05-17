"""O6 (#269) — Merger Agent.

Specialized LLM wrapper that resolves Git merge conflicts and (when
confident) pushes the resolution to Gerrit as an additional patchset
on the existing change, then casts Code-Review: +2 whose scope is
*strictly* the correctness of the conflict resolution.  It does **not**
auto-submit — the submit-rule (see O7) still requires a human +2.

L1 policy note
--------------
``CLAUDE.md`` Safety Rules previously stated "AI reviewer max score
is +1".  That rule is updated in the same commit to carve out an
exception for this agent — the scope is only the conflict region; the
final merge still requires a human +2.

Design notes
------------

* **Pluggable LLM** — tests inject a deterministic stub; production
  wires ``iq_runner.live_ask_fn`` (same backplane used by
  ``orchestrator_gateway``).
* **Pluggable Gerrit pusher / reviewer** — tests don't need SSH keys.
  ``GitPatchsetPusher`` handles ``git push HEAD:refs/for/main`` and
  ``GerritReviewer`` posts ``Code-Review: +2``.
* **Pluggable test runner** — the "run affected-module unit tests
  before voting" gate shells out via an injected ``TestRunner``; unit
  tests substitute a function that toggles pass/fail.
* **Automation gates are hard-coded** — confidence ≥ 0.9 ∧ conflict
  lines ≤ 20 ∧ single file ∧ non-security path ∧ tests pass.  Violating
  any gate downgrades the vote (Code-Review: 0) or refuses to push.
* **Security-file refusal is absolute** — any path in
  ``auth/`` / ``secrets/`` / ``config/`` / ``.github/workflows/`` (or
  several obvious siblings) makes the agent refuse; it will neither
  push nor vote.
* **3-strike escalation** — per-change failure counter.  On the third
  failure, the agent flips the change into "needs human" and refuses
  to retry for that change until a human marks it resolved.
* **Hash-chain audit** — every vote / abstain / refusal writes into
  ``backend.audit`` (best-effort).

The module stays I/O-light and side-effect-poor: all subprocess /
network / audit writes route through injectable dependencies, so
``resolve_conflict`` can be unit-tested as a pure function.

Public entry points
-------------------

``resolve_conflict(request, *, deps=None) -> ResolutionOutcome``
    The end-to-end: parse conflict → call LLM → gate checks → test
    gate → push patchset → vote.  Never raises on handled error paths
    — every rejection is surfaced as a ``ResolutionOutcome`` with a
    stable ``reason`` enum and ``voted_score`` field.

``parse_conflict_block(text) -> list[ConflictBlock]``
    Split a file containing conflict markers into structured blocks
    so the agent can reason over one conflict at a time.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection, Protocol

from backend import metrics
from backend.gerrit import gerrit_client as _default_gerrit_client

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables — all exposed for tests + docs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_MODEL = os.environ.get(
    "OMNISIGHT_MERGER_MODEL", "anthropic/claude-opus-4-7"
)
REVIEW_MODEL = os.environ.get(
    "OMNISIGHT_MERGER_REVIEW_MODEL", "anthropic/claude-3-5-haiku"
)

# Gate thresholds.  Tweak via env for A/B'ing in production.
MIN_CONFIDENCE_FOR_PLUS_TWO = float(
    os.environ.get("OMNISIGHT_MERGER_MIN_CONFIDENCE", "0.9")
)
MAX_CONFLICT_LINES = int(os.environ.get("OMNISIGHT_MERGER_MAX_LINES", "20"))

# Conflict block we emit the LLM *must* produce — we refuse to trust
# anything longer than the incoming block × safety factor.
MAX_RESOLUTION_EXPANSION_FACTOR = 3.0

# Approximate combined LLM-input cap for the pre-resolution context pack.
# The merger prompt does not depend on a provider tokenizer, so this uses
# the conventional 4 chars/token guardrail and prioritises sections before
# truncating.
CONTEXT_PACK_TOKEN_LIMIT = int(
    os.environ.get("OMNISIGHT_MERGER_CONTEXT_PACK_TOKENS", "32000")
)
_CONTEXT_PACK_CHARS_PER_TOKEN = 4
_CONTEXT_PACK_GIT_LOG_LINES = 50
PROMPT_INPUT_HARD_LIMIT_BYTES = int(
    os.environ.get("OMNISIGHT_MERGER_PROMPT_LIMIT_BYTES", "150000")
)
_CONTEXT_PACK_TRIM_ORDER = ("git_log", "sibling_files", "symbol_table")
_GIT_REGION_LOG_TIMEOUT_SECONDS = 20
_GIT_REGION_LOG_TIMEOUT_MARKER = (
    "[git log timed out after 20s - partial history may exist]"
)
REVIEW_COST_CAP_USD = float(
    os.environ.get("OMNISIGHT_MERGER_REVIEW_COST_CAP_USD", "0.02")
)
_DEFAULT_TOKEN_COST_USD = float(
    os.environ.get("OMNISIGHT_MERGER_TOKEN_COST_USD", "0.000003")
)

# Bumped for OP-1435: split-test guidance for shared setup conflicts.
MERGER_PROMPT_VERSION = "merger-prompt-v3-op1435"

# 3-strike rule (mirrors CLAUDE.md L1 Agent Behavior).
MAX_FAILURES_PER_CHANGE = 3

# Default audit chain event kind.
AUDIT_ENTITY_KIND = "merger_agent_vote"

# File-path substrings that unconditionally refuse a vote.
_SECURITY_PATH_PATTERNS: tuple[str, ...] = (
    "auth/",
    "authz/",
    "authentication/",
    "secrets/",
    "credentials/",
    "config/",
    ".env",
    ".github/workflows/",
    "ci/",
    "cicd/",
    "pipeline.yml",
    "docker-compose",  # shared infra manifest
    "dockerfile",
    "security/",
    "private_key",
    "id_rsa",
)

# Conflict block regex (captures HEAD + incoming halves).
_CONFLICT_RE = re.compile(
    r"^<<<<<<<[ \t]*(?P<head_label>[^\r\n]*)\r?\n"
    r"(?P<head>.*?)"
    r"(?:^\|{7}[^\r\n]*\r?\n.*?)?"      # optional diff3 ancestor section
    r"^=======[^\r\n]*(?:\r?\n|$)"
    r"(?P<incoming>.*?)"
    r"^>>>>>>>[ \t]*(?P<incoming_label>[^\r\n]*)(?:\r?\n|$)",
    re.DOTALL | re.MULTILINE,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MergerReason(str, Enum):
    """Stable reason codes — part of the HTTP + metrics contract."""

    plus_two_voted = "plus_two_voted"
    abstained_low_confidence = "abstained_low_confidence"
    abstained_multi_file = "abstained_multi_file"
    abstained_oversized = "abstained_oversized"
    abstained_prompt_oversized = "abstained_prompt_oversized"
    refused_security_file = "refused_security_file"
    refused_test_failure = "refused_test_failure"
    refused_no_conflict = "refused_no_conflict"
    refused_nested_markers = "refused_nested_markers"
    refused_llm_unavailable = "refused_llm_unavailable"
    refused_llm_invalid_json = "refused_llm_invalid_json"
    refused_escalated = "refused_escalated"
    refused_push_failed = "refused_push_failed"
    refused_new_logic_detected = "refused_new_logic_detected"
    refused_review_objected = "refused_review_objected"
    resolved_deterministic_merge = "resolved_deterministic_merge"
    # OP-1196 phase 3 — daemon-side push wiring (Option C). When the
    # caller (e.g., the gerrit-jira-bridge daemon) sets
    # ``request.push_locally=False``, the merger runs through all the
    # decision gates + LLM call but SKIPS the in-process pusher step
    # and returns this reason. The outcome carries ``resolved_text``
    # so the caller can perform the push + +2 itself using its own
    # workspace + credentials (the daemon has both — backend
    # container has neither). This is the "secrets stay on host"
    # design choice from the OP-1196 α-vs-β-vs-C analysis.
    deferred_push_to_caller = "deferred_push_to_caller"


class LabelVote(int, Enum):
    abstain = 0
    plus_two = 2


class MergerRiskTier(str, Enum):
    """Structural risk tier computed before the merger LLM is invoked."""

    low = "LOW"
    medium = "MEDIUM"
    high = "HIGH"


@dataclass
class ConflictBlock:
    """One ``<<<<<<<`` ... ``>>>>>>>`` section inside a file."""

    head_label: str
    incoming_label: str
    head_lines: list[str]
    incoming_lines: list[str]
    start_line: int
    end_line: int
    has_nested_markers: bool = False

    @property
    def n_conflict_lines(self) -> int:
        return len(self.head_lines) + len(self.incoming_lines)


@dataclass(frozen=True)
class ConflictRisk:
    """Pre-LLM structural risk signal for merge-conflict routing."""

    tier: MergerRiskTier
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceReadFailure:
    """Sentinel for sibling-file workspace reads that could not supply text."""

    reason: str
    detail: str = ""


@dataclass(frozen=True)
class PromptSizeGateResult:
    """Prompt + context pack after applying the oversized-input trim cascade."""

    prompt: str
    context_pack: str
    prompt_size_bytes: int
    limit_bytes: int
    sections_trimmed: tuple[str, ...]
    oversized: bool


@dataclass
class ConflictRequest:
    """Input envelope — what the orchestrator hands the merger."""

    change_id: str                  # Gerrit Change-Id (Ixxxxxxx...)
    project: str                    # Gerrit project
    file_path: str                  # path relative to repo root
    conflict_text: str              # raw file content incl. <<<<<<< markers
    head_commit_message: str = ""
    incoming_commit_message: str = ""
    file_context: str = ""          # 20-line surrounding context (caller-trimmed)
    patchset_revision: str = ""     # Gerrit revision sha (for vote target)
    workspace: str | None = None    # where the pusher runs git commands
    change_number: str = ""         # Gerrit numeric change id, for logs/audit
    jira_ticket: str = ""           # OP-NNNN key for context-pack provenance
    jira_description: str = ""      # caller-supplied ticket description
    sibling_file_contents: dict[str, str] = field(default_factory=dict)
    git_logs: dict[str, str] = field(default_factory=dict)
    symbol_table: dict[str, str] = field(default_factory=dict)
    # Extra files touched by *this* patchset — single-file gate.  Caller
    # normally only sends a single entry (the conflicting file) but the
    # field exists so a multi-file resolution can be explicitly opted
    # into and routed through the abstain-for-human path.
    additional_files: list[str] = field(default_factory=list)
    # OP-1196 phase 3 — daemon-side push wiring (Option C). When True
    # (default — backwards-compatible), the merger does its own push
    # and +2 vote via the in-process GitPatchsetPusher + reviewer.
    # When False, the merger runs everything up to + including the
    # LLM call, populates ResolutionOutcome.resolved_text, but skips
    # the push step entirely and returns MergerReason.
    # deferred_push_to_caller. The caller (typically the
    # gerrit-jira-bridge daemon) inspects the response, applies the
    # resolved_text to its own workspace, amends as merger-agent-bot,
    # pushes, and posts the +2 vote. This is the "secrets stay on
    # host" architecture decided in the OP-1196 α-vs-C analysis —
    # the backend container has the LLM + asyncpg pool but NEITHER
    # the merger-bot SSH key NOR a workspace; the daemon has both.
    push_locally: bool = True


@dataclass
class Resolution:
    """LLM-produced artefact."""

    resolved_text: str              # conflict block replaced with the resolution
    confidence: float               # 0.0 – 1.0
    rationale: str
    diff: str                       # unified diff scoped to conflict region
    changed_blocks: int             # should equal # conflict blocks in input
    changed_identifiers: list[str] = field(default_factory=list)


@dataclass
class ProposalReview:
    """Second-LLM review of a proposed conflict resolution."""

    confirmed: bool
    reason: str
    raw_response: str
    prompt: str
    model: str
    tokens_used: int = 0
    cost_usd: float = 0.0


@dataclass
class ResolutionOutcome:
    """What ``resolve_conflict`` returns.  Never raises."""

    change_id: str
    file_path: str
    reason: MergerReason
    voted_score: LabelVote
    confidence: float
    rationale: str
    diff_preview: str
    push_sha: str = ""
    review_url: str = ""
    failure_count: int = 0
    test_result: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    changed_identifiers: list[str] = field(default_factory=list)
    # OP-1196 phase 3 — full resolved file content (NOT just diff preview),
    # populated whenever the LLM produces a confident-enough resolution.
    # Used by callers that opted into deferred-push mode
    # (``ConflictRequest.push_locally=False``) to apply the resolution
    # to their own workspace before amending + pushing as
    # merger-agent-bot. Empty when the merger short-circuited before
    # the LLM step (abstain gates) or when the LLM refused.
    resolved_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason"] = self.reason.value
        d["voted_score"] = int(self.voted_score)
        return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pluggable collaborators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


MergerLLM = Callable[[str], Awaitable[tuple[str, int]]]
"""LLM callable: (prompt) -> (response_json_text, tokens_used)."""


class PatchsetPusher(Protocol):
    """Pushes a Gerrit patchset.  Tests use a stub; prod uses git CLI."""

    async def push(
        self,
        *,
        change_id: str,
        project: str,
        workspace: str | None,
        file_path: str,
        resolved_text: str,
        commit_message: str,
    ) -> "PatchsetPushResult": ...


@dataclass
class PatchsetPushResult:
    ok: bool
    sha: str = ""
    review_url: str = ""
    reason: str = ""


class GerritReviewer(Protocol):
    """Posts a Code-Review score — default wraps ``gerrit_client``."""

    async def post_review(
        self,
        *,
        commit_sha: str,
        project: str,
        message: str,
        score: int,
    ) -> "ReviewerResult": ...


@dataclass
class ReviewerResult:
    ok: bool
    reason: str = ""


TestRunner = Callable[[ConflictRequest], Awaitable["TestRunResult"]]


@dataclass
class TestRunResult:
    ok: bool
    summary: str = ""
    command: str = ""


CONFLICT_RESOLVED_HASHTAG = "Merge-Conflict-Resolved"
"""Hashtag the merger sets after a successful resolution.  The Gerrit
project.config Merger-Plus-2 submit-requirement is gated on this
hashtag (``applicableIf = hashtag:Merge-Conflict-Resolved``) so non-
conflict changes are not blocked waiting for a merger vote that will
never come.  See OP-694 + docs/ops/gerrit_dual_two_rule.md."""


class HashtagSetter(Protocol):
    """Adds a hashtag to a Gerrit change — default wraps ``gerrit_client``.

    Kept as its own Protocol so the merger's success path can fail
    gracefully if hashtag-set is unavailable, without entangling the
    +2 vote retry logic.
    """

    async def add_hashtag(
        self,
        *,
        change_id: str,
        project: str,
        hashtag: str,
    ) -> "HashtagSetterResult": ...


@dataclass
class HashtagSetterResult:
    ok: bool
    reason: str = ""


AuditSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
"""Audit callable: (action, entity_id, payload) -> None."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Default collaborator implementations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _default_llm(prompt: str) -> tuple[str, int]:
    """Production LLM — dispatches via ``iq_runner.live_ask_fn``.

    Returns ``("", 0)`` when no LLM backplane is configured so callers
    can surface ``refused_llm_unavailable`` cleanly instead of raising.
    """
    try:
        from backend.iq_runner import live_ask_fn
    except Exception as exc:                           # pragma: no cover
        logger.warning("merger_agent: live_ask_fn unavailable: %s", exc)
        return ("", 0)
    try:
        return await live_ask_fn(DEFAULT_MODEL, prompt)
    except Exception as exc:
        logger.warning("merger_agent: live_ask_fn raised: %s", exc)
        return ("", 0)


async def _default_review_llm(prompt: str) -> tuple[str, int]:
    """Cost-efficient reviewer LLM for the second half of the sandwich."""
    try:
        from backend.iq_runner import live_ask_fn
    except Exception as exc:                           # pragma: no cover
        logger.warning("merger_agent: live_ask_fn unavailable: %s", exc)
        return ("", 0)
    try:
        return await live_ask_fn(REVIEW_MODEL, prompt)
    except Exception as exc:
        logger.warning("merger_agent: review live_ask_fn raised: %s", exc)
        return ("", 0)


class GitPatchsetPusher:
    """Pushes the resolved file as a new patchset on an existing
    Gerrit change via the local ``git`` CLI.

    The orchestrator hands the merger a ``workspace`` that already has
    the change checked out (the conflicting merge in progress).  We
    overwrite ``file_path`` with ``resolved_text``, commit amending the
    existing patchset (``--amend`` retains the Change-Id trailer), then
    ``git push HEAD:refs/for/main%topic=merger-<change_id>`` to produce
    a new patchset on the same change.
    """

    def __init__(
        self,
        *,
        remote: str = "origin",
        ref_prefix: str = "refs/for/main",
        topic_prefix: str = "merger",
        runner: Callable[[str, list[str]], tuple[int, str, str]] | None = None,
    ) -> None:
        self._remote = remote
        self._ref_prefix = ref_prefix
        self._topic_prefix = topic_prefix
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(cwd: str, args: list[str]) -> tuple[int, str, str]:
        import subprocess
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    async def push(
        self,
        *,
        change_id: str,
        project: str,
        workspace: str | None,
        file_path: str,
        resolved_text: str,
        commit_message: str,
    ) -> PatchsetPushResult:
        if not workspace:
            return PatchsetPushResult(
                ok=False, reason="no workspace provided to GitPatchsetPusher"
            )
        ws = Path(workspace)
        if not ws.is_dir():
            return PatchsetPushResult(
                ok=False, reason=f"workspace not a directory: {workspace}"
            )

        target = ws / file_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(resolved_text, encoding="utf-8")
        except OSError as exc:
            return PatchsetPushResult(
                ok=False, reason=f"write failed: {exc}"
            )

        def _git(args: list[str]) -> tuple[int, str, str]:
            return self._runner(str(ws), args)

        rc, out, err = _git(["add", file_path])
        if rc != 0:
            return PatchsetPushResult(
                ok=False, reason=f"git add failed: {err or out}"
            )
        # OP-1196 phase 2 (2026-05-17): --reset-author rewrites the
        # commit's `Author:` header from the original PS uploader to the
        # ambient git identity (which the caller configures as
        # merger-agent-bot before invoking this pusher). Without this
        # flag, `--amend` preserves the original author; Gerrit then
        # rejects with `email <original-author>@... is not registered
        # in your account, and you lack 'forge author' permission`,
        # because merger-agent-bot lacks (and SHOULD NOT have — per
        # O10's forgeAuthor block mirrored in OP-1196 phase 1α)
        # `forgeAuthor` permission. The reset-author path is the safe
        # alternative: declare the merger as the AUTHOR of its
        # resolution PS, which the audit trail should record anyway.
        # Empirical verification (2026-05-17 #689 push test) confirmed
        # this is the failing path absent the flag — Gerrit rejected
        # with `commit 5620b38: email rt3628+codex-bot@gmail.com is
        # not registered in your account, and you lack 'forge author'
        # permission`. Adding --reset-author resolves it.
        rc, out, err = _git([
            "commit", "--amend", "--no-edit",
            "--reset-author",
            "--trailer", f"Merger-Change-Id: {change_id}",
        ])
        if rc != 0:
            # Fallback to a fresh commit when the workspace has no prior commit.
            rc, out, err = _git(["commit", "-m", commit_message])
            if rc != 0:
                return PatchsetPushResult(
                    ok=False, reason=f"git commit failed: {err or out}"
                )
        rc, sha, err = _git(["rev-parse", "HEAD"])
        if rc != 0:
            return PatchsetPushResult(
                ok=False, reason=f"rev-parse failed: {err}"
            )
        sha = sha.strip()
        topic = f"{self._topic_prefix}-{change_id}"
        rc, out, err = _git([
            "push", self._remote, f"HEAD:{self._ref_prefix}%topic={topic}",
        ])
        if rc != 0:
            return PatchsetPushResult(
                ok=False, sha=sha, reason=f"git push failed: {err or out}"
            )
        return PatchsetPushResult(
            ok=True,
            sha=sha,
            review_url=_extract_review_url(out + "\n" + err),
        )


def _extract_review_url(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("remote:") and "http" in line:
            for tok in line.split():
                if tok.startswith("http"):
                    return tok
    return ""


class GerritClientReviewer:
    """Wraps ``backend.gerrit.gerrit_client.post_review`` so the Merger
    agent never imports Gerrit internals directly from the resolve
    pathway.  Tests substitute a stub that records the call."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client or _default_gerrit_client

    async def post_review(
        self,
        *,
        commit_sha: str,
        project: str,
        message: str,
        score: int,
    ) -> ReviewerResult:
        res = await self._client.post_review(
            commit=commit_sha,
            message=message,
            labels={"Code-Review": score},
            project=project,
        )
        if "error" in res:
            return ReviewerResult(ok=False, reason=str(res.get("error")))
        return ReviewerResult(ok=True)


class GerritClientHashtagSetter:
    """Wraps ``backend.gerrit.gerrit_client.add_hashtag`` so the merger
    success path can mark a change as conflict-resolved without importing
    Gerrit internals directly."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client or _default_gerrit_client

    async def add_hashtag(
        self,
        *,
        change_id: str,
        project: str,
        hashtag: str,
    ) -> HashtagSetterResult:
        # The default gerrit_client may or may not expose add_hashtag
        # (it landed alongside this OP-694 work). Probe and degrade
        # cleanly so an old client doesn't blow up the merger path.
        fn = getattr(self._client, "add_hashtag", None)
        if fn is None:
            return HashtagSetterResult(
                ok=False,
                reason="gerrit_client lacks add_hashtag (pre-OP-694)",
            )
        res = await fn(change_id=change_id, project=project, hashtag=hashtag)
        if isinstance(res, dict) and "error" in res:
            return HashtagSetterResult(ok=False, reason=str(res.get("error")))
        return HashtagSetterResult(ok=True)


async def _default_test_runner(_req: ConflictRequest) -> TestRunResult:
    """No-op test runner.  Production deployments inject a real runner
    keyed off the touched file (e.g. pytest for backend/, vitest for
    app/).  When nothing is wired in, we return ``ok=True`` so the
    merger doesn't block every resolution on missing infra — the real
    gate is enforced by operators injecting the right runner."""
    return TestRunResult(ok=True, summary="no test runner configured")


async def _default_audit(
    action: str, entity_id: str, payload: dict[str, Any]
) -> None:
    """Dual-sink audit: (1) standard tenant hash-chain via ``backend.audit``
    — durable, per-tenant, verifiable via ``audit verify`` CLI, and
    (2) the O10 process-local merger vote chain which stores the
    specific (vote, confidence, rationale, patchset_revision) tuple
    the O10 spec asks for, tamper-detectable without hitting the DB.
    Both are best-effort so a hot-path regression in either sink
    doesn't block the merger decision."""
    try:
        from backend import audit
        await audit.log(
            action=action,
            entity_kind=AUDIT_ENTITY_KIND,
            entity_id=entity_id,
            after=payload,
            actor="merger-agent-bot",
        )
    except Exception as exc:                           # pragma: no cover
        logger.debug("merger_agent: audit failed: %s", exc)
    try:
        from backend import security_hardening
        chain = security_hardening.get_global_merger_chain()
        chain.append(
            change_id=entity_id,
            patchset_revision=str(payload.get("push_sha", "")),
            vote=int(payload.get("voted_score", 0)),
            confidence=float(payload.get("confidence", 0.0)),
            rationale=str(payload.get("rationale", "")),
            reason_code=str(payload.get("reason", action)),
            extra={
                "file_path": payload.get("file_path", ""),
                "review_url": payload.get("review_url", ""),
            },
        )
    except Exception as exc:                           # pragma: no cover
        logger.debug("merger_agent: O10 chain append failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dependency bundle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class MergerDeps:
    """Inject-at-call-time bundle so tests don't need to monkey-patch."""

    llm: MergerLLM = _default_llm
    review_llm: MergerLLM = _default_review_llm
    pusher: PatchsetPusher = field(default_factory=GitPatchsetPusher)
    reviewer: GerritReviewer = field(default_factory=GerritClientReviewer)
    hashtag_setter: HashtagSetter = field(default_factory=GerritClientHashtagSetter)
    test_runner: TestRunner = _default_test_runner
    audit: AuditSink = _default_audit


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-change failure counter (3-strike rule)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_failure_counts: dict[str, int] = {}
_failure_lock = threading.Lock()


def get_failure_count(change_id: str) -> int:
    with _failure_lock:
        return _failure_counts.get(change_id, 0)


def _bump_failure(change_id: str) -> int:
    with _failure_lock:
        v = _failure_counts.get(change_id, 0) + 1
        _failure_counts[change_id] = v
        return v


def _reset_failure(change_id: str) -> None:
    with _failure_lock:
        _failure_counts.pop(change_id, None)


def reset_failure_counts_for_tests() -> None:
    with _failure_lock:
        _failure_counts.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public: parse + system prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


TAKE_BOTH_FEATURE_PRESERVATION_RUBRIC = """\
Take-both feature-preservation rubric:

1. ADD-vs-ADD distinct symbols:
   - When both sides add distinct symbols, prefer a take-both resolution.
   - Symbols include functions, classes, parameters, exports, constants,
     routing branches, metrics fields, validation guards, and side-effect
     calls.
   - Keep both additions unless there is concrete rename evidence or the
     two behaviors are mutually exclusive.

2. RENAME DETECTION:
   - Treat two new symbols as a possible rename only when they differ
     mainly by name and share the same shape, similar docstring, or
     identical body.
   - Before unifying, check the conflict context, sibling files, symbol
     table, and call sites supplied in the context pack.
   - If unifying, prefer the older or more conventional name and preserve
     all call-site-observable behavior from both sides.

3. DOCSTRING COLLISION:
   - When both sides add docstring entries for the same parameter, merge
     by concatenating the descriptions.
   - Keep both descriptions unless one is a strict duplicate of the other.
   - Do not let docstring collisions justify dropping either parameter or
     its implementation.

4. SIGNATURE OVERLAP:
   - When both sides add new parameters to the same function signature,
     keep both parameters in the resolved signature.
   - In the function body, propagate both values to downstream callers
     using names already present in the conflict or context.
   - Preserve defaults, keyword-only markers, type annotations, and call
     ordering unless the context proves a rename.

5. SPLIT TESTS WITH SHARED SETUP:
   - When a conflict spans two test methods that share the same setup
     pattern, do not collapse them into one merged test method.
   - Shared setup includes the same mock names, fixture imports, helper
     calls, patch decorators, or context-manager patches.
   - If the methods diverge in assert lines, patched return payloads, or
     `_event(...)` arguments, emit two separate test methods.
   - Duplicate the complete setup in each method so each branch keeps its
     own assertion intent.

6. ABSTAIN BIAS:
   - If you cannot identify whether ADD-vs-ADD, rename detection,
     docstring collision, signature overlap, or split-test preservation
     applies, abstain.
   - Emit a low confidence score and name the unresolved reason class in
     the rationale instead of interleaving lines or picking one side.
"""


SYSTEM_PROMPT = (
    f"Prompt version: {MERGER_PROMPT_VERSION}\n\n"
    "You are a merge conflict resolution expert.  You receive one Git "
    "conflict block (HEAD side + incoming side) and must produce a "
    "single unified resolution that PRESERVES THE LOGICAL INTENT OF BOTH "
    "commits.  You MUST NOT introduce any logic, function, call, "
    "variable, or statement that does not appear in either half of the "
    "conflict or in the provided file context.  If you cannot preserve "
    "both intents without introducing new logic, output a low confidence "
    "score and explain the ambiguity in the rationale — do NOT fabricate "
    "a compromise.\n\n"
    + TAKE_BOTH_FEATURE_PRESERVATION_RUBRIC
    + "\nOutput STRICTLY a JSON object matching this schema:\n"
    '{"resolved_block": "<text that replaces the conflict block>",'
    ' "confidence": <float 0..1>,'
    ' "rationale": "<one-paragraph explanation>",'
    ' "new_logic_detected": <bool; true if you had to invent anything>}'
)


def parse_conflict_block(text: str) -> list[ConflictBlock]:
    """Parse a file containing ``<<<<<<<`` / ``=======`` / ``>>>>>>>``
    markers into structured blocks."""
    blocks: list[ConflictBlock] = []
    for m in _CONFLICT_RE.finditer(text):
        head = m.group("head") or ""
        incoming = m.group("incoming") or ""
        has_nested_markers = "<<<<<<<" in head or "<<<<<<<" in incoming
        head_lines = head.splitlines()
        incoming_lines = incoming.splitlines()
        start = text[: m.start()].count("\n") + 1
        end = start + text[m.start(): m.end()].count("\n")
        blocks.append(ConflictBlock(
            head_label=m.group("head_label").strip(),
            incoming_label=m.group("incoming_label").strip(),
            head_lines=head_lines,
            incoming_lines=incoming_lines,
            start_line=start,
            end_line=end,
            has_nested_markers=has_nested_markers,
        ))
    return blocks


def _context_pack_limit_chars() -> int:
    return max(0, CONTEXT_PACK_TOKEN_LIMIT * _CONTEXT_PACK_CHARS_PER_TOKEN)


def _safe_workspace_file(workspace: str | None, rel_path: str) -> Path | None:
    if not workspace or not rel_path:
        return None
    root = Path(workspace).expanduser().resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def _read_workspace_text(
    workspace: str | None,
    rel_path: str,
) -> str | WorkspaceReadFailure:
    target = _safe_workspace_file(workspace, rel_path)
    if target is None:
        return WorkspaceReadFailure(
            reason="unsafe_path",
            detail="path is outside workspace or workspace is unavailable",
        )
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return WorkspaceReadFailure(reason="missing_file", detail="file not found")
    except OSError:
        return WorkspaceReadFailure(reason="read_error", detail="OSError")
    if b"\x00" in raw[:8192]:
        return "<binary file>"
    return raw.decode("utf-8", errors="replace")


def _render_workspace_read_failure(
    path: str,
    failure: WorkspaceReadFailure,
) -> str:
    detail = f": {failure.detail}" if failure.detail else ""
    return f"[workspace read failed: {failure.reason} for {path}{detail}]"


def _git_region_log(
    workspace: str | None,
    file_path: str,
    blocks: list[ConflictBlock],
) -> str:
    root_path = Path(workspace).expanduser().resolve() if workspace else None
    if root_path is None or not (root_path / ".git").exists() or not blocks:
        return ""
    first = blocks[0]
    start = max(1, first.start_line - _CONTEXT_PACK_GIT_LOG_LINES)
    end = first.end_line + _CONTEXT_PACK_GIT_LOG_LINES
    commands = [
        ["git", "log", "--follow", "-n", "5", f"-L{start},{end}:{file_path}"],
        ["git", "log", "-p", "--follow", "-n", "5", "--", file_path],
    ]
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                cwd=root_path,
                capture_output=True,
                text=True,
                timeout=_GIT_REGION_LOG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "merger_agent: git region log timed out after %ss for %s",
                _GIT_REGION_LOG_TIMEOUT_SECONDS,
                file_path,
            )
            return _GIT_REGION_LOG_TIMEOUT_MARKER
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return ""


def _collect_file_contents(req: ConflictRequest) -> dict[str, str]:
    paths = [req.file_path, *req.additional_files]
    out: dict[str, str] = {}
    for path in paths:
        if not path or path in out:
            continue
        if path == req.file_path:
            out[path] = req.conflict_text
            continue
        if path in req.sibling_file_contents:
            out[path] = req.sibling_file_contents[path]
            continue
        content = _read_workspace_text(req.workspace, path)
        if isinstance(content, WorkspaceReadFailure):
            logger.warning(
                "merger_agent: sibling workspace read failed "
                "jira=%s change=%s path=%s reason=%s detail=%s",
                req.jira_ticket or "(none)",
                req.change_number or req.change_id or "(unknown)",
                path,
                content.reason,
                content.detail,
            )
            out[path] = _render_workspace_read_failure(path, content)
            continue
        out[path] = content
    for path, content in req.sibling_file_contents.items():
        if path and path not in out:
            out[path] = content
    return out


def _truncate_section(
    title: str,
    content: str,
    remaining: int,
    *,
    empty_label: str = "(none supplied)",
) -> str:
    if remaining <= 0:
        return ""
    body = content.strip() or empty_label
    section = f"## {title}\n{body}\n"
    if len(section) <= remaining:
        return section
    suffix = "\n[context-pack truncated]\n"
    keep = max(0, remaining - len(suffix))
    if keep <= 0:
        return ""
    return section[:keep].rstrip() + suffix


def build_context_pack(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
    *,
    omit_sections: Collection[str] | None = None,
) -> str:
    """Build the pre-resolution context pack with deterministic priority.

    Priority order follows OP-1403: conflict file, recent git log, JIRA
    description, sibling files, then symbol table. All collectors are
    best-effort so a missing daemon workspace never blocks the merger.
    """
    limit = _context_pack_limit_chars()
    if limit <= 0:
        return ""

    omitted = set(omit_sections or ())
    file_contents = _collect_file_contents(req)
    conflict_file = file_contents.pop(req.file_path, req.conflict_text)
    git_log = ""
    if "git_log" not in omitted:
        git_log = req.git_logs.get(req.file_path) or _git_region_log(
            req.workspace, req.file_path, blocks,
        )

    sections: list[tuple[str, str, str]] = [
        (f"Conflict file: {req.file_path}", conflict_file, "(none supplied)"),
        (
            f"JIRA ticket {req.jira_ticket or '(unknown)'}",
            req.jira_description,
            "(none supplied)",
        ),
    ]
    if "git_log" not in omitted:
        sections.insert(1, (
            f"Recent git log for {req.file_path}",
            git_log,
            "(none supplied)",
        ))
    if "sibling_files" not in omitted:
        for path in sorted(file_contents):
            sections.append((
                f"Sibling file: {path}",
                file_contents[path],
                "(file is empty)",
            ))
    if "symbol_table" not in omitted:
        for path in sorted(req.symbol_table):
            sections.append((
                f"Develop symbol table: {path}",
                req.symbol_table[path],
                "(none supplied)",
            ))

    out = ""
    for title, content, empty_label in sections:
        sep = "\n\n" if out else ""
        chunk = _truncate_section(
            title,
            content,
            limit - len(out) - len(sep),
            empty_label=empty_label,
        )
        if not chunk:
            break
        out = f"{out}{sep}{chunk}"
        if len(out) >= limit:
            break
    return out[:limit].strip()


def _prompt_size_bytes(prompt: str) -> int:
    return len(prompt.encode("utf-8"))


def _format_sections_trimmed(sections: Collection[str]) -> str:
    return "[" + ",".join(sections) + "]"


def build_prompt_with_size_gate(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
    risk: ConflictRisk,
) -> PromptSizeGateResult:
    """Build a prompt, trimming lower-priority context before LLM input.

    The context-pack budget is token-ish and local to the context block.
    This gate measures the final UTF-8 prompt bytes after the system
    prompt, rubric, commit messages, and conflict blocks are present.
    """
    limit = max(0, PROMPT_INPUT_HARD_LIMIT_BYTES)
    sections_trimmed: tuple[str, ...] = ()

    while True:
        context_pack = build_context_pack(
            req, blocks, omit_sections=sections_trimmed,
        )
        prompt = build_prompt(req, blocks, risk, context_pack=context_pack)
        prompt_size = _prompt_size_bytes(prompt)
        oversized = prompt_size > limit
        if not oversized or len(sections_trimmed) == len(_CONTEXT_PACK_TRIM_ORDER):
            return PromptSizeGateResult(
                prompt=prompt,
                context_pack=context_pack,
                prompt_size_bytes=prompt_size,
                limit_bytes=limit,
                sections_trimmed=sections_trimmed,
                oversized=oversized,
            )
        sections_trimmed = _CONTEXT_PACK_TRIM_ORDER[:len(sections_trimmed) + 1]


def build_prompt(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
    risk: ConflictRisk | None = None,
    *,
    context_pack: str | None = None,
) -> str:
    """Deterministic prompt — inlines the conflict + commit messages +
    the provided file context, and repeats the no-new-logic guardrail."""
    risk = risk or classify_conflict_risk(req, blocks)
    if context_pack is None:
        context_pack = build_context_pack(req, blocks)
    parts: list[str] = [
        "SYSTEM: " + SYSTEM_PROMPT,
        "",
        f"FILE: {req.file_path}",
        f"Structural risk tier: {risk.tier.value}",
        "Structural risk signals: "
        + (", ".join(risk.reasons) if risk.reasons else "(none)"),
        f"Gerrit change number: {req.change_number or '(unknown)'}",
        f"JIRA ticket: {req.jira_ticket or '(none supplied)'}",
        f"HEAD commit message:\n{req.head_commit_message.strip()}",
        f"Incoming commit message:\n{req.incoming_commit_message.strip()}",
        "",
        "Context pack (priority-capped):",
        context_pack or "(none supplied)",
        "",
        "File context (20 lines surrounding the conflict):",
        req.file_context.strip() or "(none supplied)",
        "",
        "Conflict blocks:",
    ]
    for i, blk in enumerate(blocks, start=1):
        parts.extend([
            f"  Block {i} (lines {blk.start_line}-{blk.end_line}):",
            f"    HEAD [{blk.head_label}]:",
            *[f"      {line}" for line in blk.head_lines],
            f"    INCOMING [{blk.incoming_label}]:",
            *[f"      {line}" for line in blk.incoming_lines],
        ])
    parts.append("")
    parts.append(
        "Return ONE JSON object (no prose, no fences) with the schema "
        "described above."
    )
    return "\n".join(parts)


def build_review_prompt(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
    resolution: Resolution,
) -> str:
    """Prompt for LLM-B: independently review LLM-A's resolution."""
    parts: list[str] = [
        "SYSTEM: You are an independent merge-resolution reviewer. "
        "You do not propose edits. You only decide whether the proposed "
        "resolution preserves both sides' intent.",
        "",
        f"FILE: {req.file_path}",
        f"Gerrit change number: {req.change_number or '(unknown)'}",
        f"JIRA ticket: {req.jira_ticket or '(none supplied)'}",
        "",
        "Original conflict:",
        req.conflict_text,
        "",
        "LLM-A proposed resolved file:",
        resolution.resolved_text,
        "",
        "LLM-A diff explanation:",
        resolution.diff,
        "",
        f"LLM-A rationale: {resolution.rationale}",
        f"LLM-A confidence: {resolution.confidence:.2f}",
        "",
        "Review rubric:",
        TAKE_BOTH_FEATURE_PRESERVATION_RUBRIC,
        "",
        "Does this resolution preserve both sides' intent?",
        "Does it handle all five rubric cases above?",
        "Reply on the first line with exactly CONFIRM or OBJECT, then "
        "give one short reason.",
    ]
    for i, blk in enumerate(blocks, start=1):
        parts.extend([
            "",
            f"Conflict block {i} HEAD:",
            "\n".join(blk.head_lines),
            f"Conflict block {i} INCOMING:",
            "\n".join(blk.incoming_lines),
        ])
    return "\n".join(parts)


def _estimate_llm_cost(tokens_used: int) -> float:
    return max(0.0, float(tokens_used) * _DEFAULT_TOKEN_COST_USD)


def _observe_llm_cost(cost_usd: float) -> None:
    try:
        metrics.merger_llm_cost_usd_total.inc(cost_usd)
    except Exception:
        pass


async def _review_proposal(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
    resolution: Resolution,
    *,
    deps: MergerDeps,
) -> ProposalReview:
    """Ask LLM-B to confirm or object to LLM-A's resolution."""
    prompt = build_review_prompt(req, blocks, resolution)
    try:
        raw, tokens = await deps.review_llm(prompt)
    except Exception as exc:
        logger.warning("merger_agent: review llm raised: %s", exc)
        raw, tokens = "", 0

    cost_usd = _estimate_llm_cost(tokens)
    _observe_llm_cost(cost_usd)
    first = (raw.strip().splitlines() or [""])[0].strip().upper()
    confirmed = first == "CONFIRM" and cost_usd <= REVIEW_COST_CAP_USD
    reason = raw.strip() or "review LLM returned empty response"
    if cost_usd > REVIEW_COST_CAP_USD:
        reason = (
            f"review cost ${cost_usd:.6f} exceeded cap "
            f"${REVIEW_COST_CAP_USD:.6f}; raw={reason[:500]}"
        )
    return ProposalReview(
        confirmed=confirmed,
        reason=reason,
        raw_response=raw,
        prompt=prompt,
        model=REVIEW_MODEL,
        tokens_used=tokens,
        cost_usd=cost_usd,
    )


def is_security_sensitive(file_path: str) -> bool:
    """True when the file lives under an auth/secrets/config/CI path.

    Match is case-insensitive on substring so ``.github/workflows/ci.yml``,
    ``backend/auth/session.py``, and ``configs/prod/app.yaml`` all trip."""
    norm = file_path.strip().replace("\\", "/").lower()
    return any(pat in norm for pat in _SECURITY_PATH_PATTERNS)


def classify_conflict_risk(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
    coupling_components: list[set[str]] | None = None,
) -> ConflictRisk:
    """Classify structural merge risk before spending an LLM call.

    LOW means the conflict is a narrow line edit. MEDIUM is the intended
    LLM-resolution lane for edit/edit overlaps that still fit the hard
    gates. HIGH is reserved for shapes the current one-block JSON
    contract cannot safely verify before push.
    """
    reasons: list[str] = []
    touched_files = [
        path for path in dict.fromkeys(
            [req.file_path, *req.additional_files]
        )
        if path
    ]
    multi_file = len(touched_files) > 1

    if len(blocks) > 1:
        reasons.append("multiple_conflict_blocks")
    if multi_file:
        reasons.append("additional_files_present")

    total_lines = sum(block.n_conflict_lines for block in blocks)
    if total_lines > max(1, int(MAX_CONFLICT_LINES * 0.75)):
        reasons.append("near_line_limit")

    for block in blocks:
        if _has_signature_overlap(block):
            reasons.append("signature_overlap")
        if _has_param_name_collision(block):
            reasons.append("param_name_collision")
        if _has_take_both_feature_shape(block):
            reasons.append("take_both_feature_shape")

    if multi_file:
        if len(touched_files) >= 5:
            reasons.append("multi_file_count_exceeds_batch")
        coupled = any(
            len(set(component) & set(touched_files)) > 1
            for component in (coupling_components or [])
        )
        reasons.append(
            "multi_file_high_coupling" if coupled else "multi_file_low_coupling"
        )
        if coupled and total_lines > MAX_CONFLICT_LINES:
            reasons.append("multi_file_coupled_oversize")

    deduped = tuple(dict.fromkeys(reasons))
    if multi_file:
        high_reasons = {
            "multi_file_count_exceeds_batch",
            "multi_file_coupled_oversize",
        }
        if any(reason in high_reasons for reason in deduped):
            return ConflictRisk(MergerRiskTier.high, deduped)
        return ConflictRisk(MergerRiskTier.medium, deduped)

    high_reasons = {
        "multiple_conflict_blocks",
        "additional_files_present",
        "near_line_limit",
    }
    if any(reason in high_reasons for reason in deduped):
        return ConflictRisk(MergerRiskTier.high, deduped)
    if deduped:
        return ConflictRisk(MergerRiskTier.medium, deduped)
    return ConflictRisk(MergerRiskTier.low, ())


def _classify_coupling(
    file_paths: list[str],
    workspace: str,
    conflict_text: str = "",
) -> list[set[str]]:
    """Return connected components for files that should be resolved together."""
    deadline = time.monotonic() + 20.0
    root = Path(workspace).expanduser().resolve()
    targets = [_normalise_rel_path(path) for path in file_paths]
    targets = [path for path in dict.fromkeys(targets) if path]
    if not targets:
        return []

    parent = {path: path for path in targets}

    def find(path: str) -> str:
        while parent[path] != path:
            parent[path] = parent[parent[path]]
            path = parent[path]
        return path

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    module_to_path: dict[str, str] = {}
    definitions: dict[str, set[str]] = {}
    references: dict[str, set[str]] = {}
    imports: dict[str, set[str]] = {}
    field_references: dict[str, set[str]] = {}

    if root.is_dir():
        try:
            for path in root.rglob("*.py"):
                if time.monotonic() > deadline:
                    break
                rel_path = _normalise_rel_path(path.relative_to(root).as_posix())
                module_to_path[_module_name_for_path(rel_path)] = rel_path
                if rel_path.endswith("/__init__.py"):
                    module_to_path[_module_name_for_path(rel_path[:-12])] = rel_path
        except OSError:
            module_to_path = {}

    for path in targets:
        if time.monotonic() > deadline:
            continue
        tree = _parse_workspace_python(root, path)
        if tree is None:
            definitions[path] = set()
            references[path] = set()
            imports[path] = set()
            field_references[path] = set()
            continue
        definitions[path] = _top_level_symbols(tree)
        references[path] = _referenced_symbols(tree)
        imports[path] = _imported_modules(tree)
        field_references[path] = _field_like_symbols(tree)

    target_set = set(targets)
    for path in targets:
        for imported in imports.get(path, set()):
            imported_path = _resolve_imported_path(imported, module_to_path)
            if imported_path in target_set and imported_path != path:
                union(path, imported_path)

    for left in targets:
        for right in targets:
            if left == right:
                continue
            if definitions.get(left, set()) & references.get(right, set()):
                union(left, right)

    for left in targets:
        for right in targets:
            if left != right and _is_test_source_pair(left, right):
                union(left, right)

    data_flow_fields = _data_flow_fields_for_targets(
        root,
        targets,
        field_references,
        conflict_text,
        deadline,
    )
    for files in data_flow_fields.values():
        ordered = [path for path in targets if path in files]
        for path in ordered[1:]:
            union(ordered[0], path)

    components: dict[str, set[str]] = {}
    for path in targets:
        components.setdefault(find(path), set()).add(path)
    return list(components.values())


def _normalise_rel_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


def _module_name_for_path(path: str) -> str:
    path = _normalise_rel_path(path)
    if path.endswith(".py"):
        path = path[:-3]
    if path.endswith("/__init__"):
        path = path[:-9]
    return path.replace("/", ".")


def _parse_workspace_python(root: Path, rel_path: str) -> ast.AST | None:
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    try:
        source = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return ast.parse(source, filename=rel_path)
    except SyntaxError:
        return None


def _top_level_symbols(tree: ast.AST) -> set[str]:
    symbols: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
    return symbols


def _referenced_symbols(tree: ast.AST) -> set[str]:
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
    return refs


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(
                f"{node.module}.{alias.name}" for alias in node.names
                if alias.name != "*"
            )
    return modules


def _field_like_symbols(tree: ast.AST) -> set[str]:
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            fields.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            fields.add(node.arg)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            fields.add(node.slice.value)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    fields.add(key.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            fields.add(node.target.id)
    return {field for field in fields if _is_data_flow_field_name(field)}


def _data_flow_fields_for_targets(
    root: Path,
    targets: list[str],
    field_references: dict[str, set[str]],
    conflict_text: str,
    deadline: float,
) -> dict[str, set[str]]:
    target_set = set(targets)
    candidates = set().union(*(field_references.get(path, set()) for path in targets))
    conflict_fields = _field_names_from_text(conflict_text)
    if conflict_fields:
        candidates &= conflict_fields
    if not candidates:
        return {}

    codebase_references: dict[str, set[str]] = {field: set() for field in candidates}
    if root.is_dir():
        try:
            paths = root.rglob("*.py")
            for path in paths:
                if time.monotonic() > deadline:
                    break
                rel_path = _normalise_rel_path(path.relative_to(root).as_posix())
                tree = _parse_workspace_python(root, rel_path)
                if tree is None:
                    continue
                fields = _field_like_symbols(tree) & candidates
                for field in fields:
                    codebase_references[field].add(rel_path)
        except OSError:
            return {}

    return {
        field: files
        for field, files in codebase_references.items()
        if len(files) >= 3 and files <= target_set
    }


def _field_names_from_text(text: str) -> set[str]:
    if not text:
        return set()
    fields: set[str] = set()
    for block in parse_conflict_block(text):
        fields.update(_field_names_from_lines(block.head_lines))
        fields.update(_field_names_from_lines(block.incoming_lines))
    if not fields:
        fields.update(_field_names_from_lines(text.splitlines()))
    return fields


def _field_names_from_lines(lines: list[str]) -> set[str]:
    fields: set[str] = set()
    for line in lines:
        fields.update(
            match.group("field")
            for match in re.finditer(r"[.\[]['\"]?(?P<field>[A-Za-z_][A-Za-z0-9_]*)", line)
        )
        fields.update(
            match.group("field")
            for match in re.finditer(r"\b(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        )
        fields.update(
            match.group("field")
            for match in re.finditer(r"['\"](?P<field>[A-Za-z_][A-Za-z0-9_]*)['\"]\s*:", line)
        )
    return {field for field in fields if _is_data_flow_field_name(field)}


def _is_data_flow_field_name(name: str) -> bool:
    return (
        len(name) >= 3
        and not name.startswith("_")
        and name not in {"self", "cls", "args", "kwargs", "return"}
    )


def _resolve_imported_path(
    imported: str,
    module_to_path: dict[str, str],
) -> str | None:
    module = imported
    while module:
        if module in module_to_path:
            return module_to_path[module]
        module = module.rpartition(".")[0]
    return None


def _is_test_source_pair(left: str, right: str) -> bool:
    return (
        _source_for_test_path(left) == right
        or _source_for_test_path(right) == left
        or _test_targets_package(left, right)
        or _test_targets_package(right, left)
    )


def _source_for_test_path(path: str) -> str | None:
    if not path.startswith("backend/tests/test_") or not path.endswith(".py"):
        return None
    name = path.removeprefix("backend/tests/test_")
    return f"backend/{name}"


def _test_targets_package(test_path: str, source_path: str) -> bool:
    source = _source_for_test_path(test_path)
    if source is None or not source_path.endswith(".py"):
        return False
    package = source.removesuffix(".py")
    return source_path.startswith(f"{package}/")


_PY_SIGNATURE_RE = re.compile(r"^\s*(?:async\s+def|def)\s+\w+\s*\((?P<params>[^)]*)\)")
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def _has_signature_overlap(block: ConflictBlock) -> bool:
    return bool(_signature_params(block.head_lines)) and bool(
        _signature_params(block.incoming_lines)
    )


def _signature_params(lines: list[str]) -> set[str]:
    params: set[str] = set()
    for line in lines:
        match = _PY_SIGNATURE_RE.match(line)
        if not match:
            continue
        for raw in match.group("params").split(","):
            name = raw.strip().split("=", 1)[0].split(":", 1)[0].strip()
            if name and name not in {"self", "cls", "*", "/"}:
                params.add(name.lstrip("*"))
    return params


def _has_param_name_collision(block: ConflictBlock) -> bool:
    head_params = _signature_params(block.head_lines)
    incoming_params = _signature_params(block.incoming_lines)
    if not head_params or not incoming_params:
        return False
    if head_params & incoming_params:
        return True
    for left in head_params:
        for right in incoming_params:
            if left.startswith(f"{right}_") or right.startswith(f"{left}_"):
                return True
    return False


def _has_take_both_feature_shape(block: ConflictBlock) -> bool:
    head_names = set(_IDENT_RE.findall("\n".join(block.head_lines)))
    incoming_names = set(_IDENT_RE.findall("\n".join(block.incoming_lines)))
    head_unique = head_names - incoming_names
    incoming_unique = incoming_names - head_names
    return bool(head_unique and incoming_unique)


def _risk_metadata(risk: ConflictRisk) -> dict[str, Any]:
    return {
        "risk_tier": risk.tier.value,
        "risk_reasons": list(risk.reasons),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM response parsing + resolution assembly
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _LLMParseError(RuntimeError):
    pass


def _parse_llm_response(raw: str) -> dict[str, Any]:
    """Tolerant JSON parse — strips optional ```json fences + leading
    prose the LLM sometimes emits despite the instructions."""
    if not raw or not raw.strip():
        raise _LLMParseError("empty response")
    txt = raw.strip()
    if txt.startswith("```"):
        # ```json\n...\n```  → strip fence
        first_nl = txt.find("\n")
        if first_nl > 0:
            txt = txt[first_nl + 1 :]
        if txt.endswith("```"):
            txt = txt[:-3]
    # Sometimes the model trails with "Here's the JSON:" before the
    # braces — pull the first balanced object.
    start = txt.find("{")
    end = txt.rfind("}")
    if start < 0 or end <= start:
        raise _LLMParseError(f"no JSON object in response: {txt[:120]!r}")
    try:
        return json.loads(txt[start : end + 1])
    except json.JSONDecodeError as exc:
        raise _LLMParseError(f"json decode failed: {exc}") from exc


def _assemble_resolution(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
    llm_payload: dict[str, Any],
) -> Resolution:
    """Splice the LLM's ``resolved_block`` back into the original file,
    preserving every line outside the conflict region."""
    all_empty_hunk = all(b.n_conflict_lines == 0 for b in blocks)
    resolved_block = str(llm_payload.get("resolved_block", ""))
    if "resolved_block" not in llm_payload:
        raise _LLMParseError("resolved_block missing")
    if not resolved_block and not all_empty_hunk:
        raise _LLMParseError("resolved_block missing or empty")

    # We only support the single-block path for auto-vote; a multi-
    # block file is allowed through but the gate will refuse the +2.
    text = req.conflict_text
    # Replace every conflict block in source order with the same resolved
    # chunk when the LLM gave us one.  For multi-block conflicts the
    # LLM must return an array — we detect the shape and fail fast.
    if "resolved_blocks" in llm_payload and isinstance(
        llm_payload["resolved_blocks"], list
    ):
        blocks_out = [str(b) for b in llm_payload["resolved_blocks"]]
        if len(blocks_out) != len(blocks):
            raise _LLMParseError(
                f"resolved_blocks length {len(blocks_out)} != "
                f"conflict blocks {len(blocks)}"
            )
    else:
        blocks_out = [resolved_block] * len(blocks)

    # Do the splices right-to-left so earlier offsets stay valid.
    matches = list(_CONFLICT_RE.finditer(text))
    for idx in range(len(matches) - 1, -1, -1):
        m = matches[idx]
        text = text[: m.start()] + blocks_out[idx] + text[m.end() :]

    confidence = float(llm_payload.get("confidence", 0.0))
    if confidence < 0:
        confidence = 0.0
    if confidence > 1:
        confidence = 1.0
    rationale = str(llm_payload.get("rationale", ""))
    new_logic = bool(llm_payload.get("new_logic_detected", False))

    if new_logic:
        # Clamp confidence hard — the prompt asked for exactly this.
        confidence = min(confidence, 0.3)

    diff = _make_block_diff(req.file_path, blocks, blocks_out)
    return Resolution(
        resolved_text=text,
        confidence=confidence,
        rationale=rationale or "(no rationale supplied)",
        diff=diff,
        changed_blocks=len(blocks),
        changed_identifiers=_extract_changed_identifiers(req.file_path, text),
    )


def _extract_changed_identifiers(file_path: str, resolved_text: str) -> list[str]:
    """Best-effort pytest ``-k`` terms from resolved Python content."""
    if not file_path.endswith(".py"):
        return []
    try:
        tree = ast.parse(resolved_text)
    except SyntaxError:
        return []

    names: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            names.append(value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]:
                _add(arg.arg)
            if node.args.vararg:
                _add(node.args.vararg.arg)
            if node.args.kwarg:
                _add(node.args.kwarg.arg)
    return names


def _make_block_diff(
    file_path: str, blocks: list[ConflictBlock], blocks_out: list[str]
) -> str:
    """Human-readable diff restricted to the conflict regions.  We
    purposely don't render a full unified diff — the gate requires that
    no other line was touched, and a block-scoped diff surfaces that
    invariant."""
    out: list[str] = [f"--- a/{file_path} (conflict)", f"+++ b/{file_path} (resolved)"]
    for blk, resolved in zip(blocks, blocks_out):
        out.append(f"@@ -{blk.start_line},{blk.n_conflict_lines} "
                   f"+{blk.start_line},{len(resolved.splitlines())} @@")
        for line in blk.head_lines:
            out.append(f"-{line}  # HEAD")
        for line in blk.incoming_lines:
            out.append(f"-{line}  # INCOMING")
        for line in resolved.splitlines():
            out.append(f"+{line}")
    return "\n".join(out)


def try_deterministic_merge(
    req: ConflictRequest,
    blocks: list[ConflictBlock],
) -> ResolutionOutcome | None:
    """Resolve narrow AST-safe take-both conflicts without calling an LLM."""
    if not blocks:
        return None

    resolved_blocks: list[str] = []
    patterns: list[str] = []
    matches = list(_CONFLICT_RE.finditer(req.conflict_text))
    if len(matches) != len(blocks):
        return None
    for block, match in zip(blocks, matches):
        prefix = req.conflict_text[:match.start()]
        resolved = _deterministic_block_resolution(block, prefix)
        if resolved is None:
            return None
        resolved_block, pattern = resolved
        resolved_blocks.append(resolved_block)
        patterns.append(pattern)

    text = req.conflict_text
    for idx in range(len(matches) - 1, -1, -1):
        match = matches[idx]
        text = text[: match.start()] + resolved_blocks[idx] + text[match.end():]

    diff = _make_block_diff(req.file_path, blocks, resolved_blocks)
    outcome = _build_abstain(
        req,
        MergerReason.resolved_deterministic_merge,
        confidence=1.0,
        rationale=(
            "deterministic AST trivial merge: "
            f"{', '.join(dict.fromkeys(patterns))}"
        ),
        diff_preview=diff,
        metadata={
            "merger_path": "deterministic",
            "deterministic_patterns": list(dict.fromkeys(patterns)),
            "conflict_lines": sum(block.n_conflict_lines for block in blocks),
            "blocks": len(blocks),
        },
    )
    outcome.resolved_text = text
    outcome.changed_identifiers = _extract_changed_identifiers(req.file_path, text)
    return outcome


def _deterministic_block_resolution(
    block: ConflictBlock,
    prefix: str,
) -> tuple[str, str] | None:
    resolvers = (
        lambda head, incoming: _resolve_dunder_all_union(head, incoming, prefix),
        _resolve_import_union,
        _resolve_distinct_symbol_adds,
        _resolve_dict_literal_adds,
        _resolve_comment_docstring_adds,
    )
    for resolver in resolvers:
        resolved = resolver(block.head_lines, block.incoming_lines)
        if resolved is not None:
            return resolved
    return None


def _join_block_lines(lines: list[str]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


def _literal_sort_key(value: str) -> tuple[str, str]:
    return (value.lower(), value)


def _parse_single_assign(lines: list[str]) -> ast.Assign | None:
    try:
        tree = ast.parse(_join_block_lines(lines))
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        return None
    return tree.body[0]


def _string_list_assignment(
    lines: list[str],
    target_name: str,
) -> tuple[str, list[str]] | None:
    assign = _parse_single_assign(lines)
    if assign is None or len(assign.targets) != 1:
        return None
    target = assign.targets[0]
    if not isinstance(target, ast.Name) or target.id != target_name:
        return None
    if not isinstance(assign.value, (ast.List, ast.Tuple)):
        return None
    values: list[str] = []
    for elt in assign.value.elts:
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
            return None
        values.append(elt.value)
    return target.id, values


def _resolve_dunder_all_union(
    head_lines: list[str],
    incoming_lines: list[str],
    prefix: str = "",
) -> tuple[str, str] | None:
    head = _string_list_assignment(head_lines, "__all__")
    incoming = _string_list_assignment(incoming_lines, "__all__")
    if head is not None and incoming is not None:
        values = sorted(set(head[1]) | set(incoming[1]), key=_literal_sort_key)
        lines = ["__all__ = ["]
        lines.extend(f'    "{value}",' for value in values)
        lines.append("]")
        return _join_block_lines(lines), "__all__ list union"

    if "__all__" not in prefix[-500:]:
        return None
    head_entries = _string_list_entries(head_lines)
    incoming_entries = _string_list_entries(incoming_lines)
    if head_entries is None or incoming_entries is None:
        return None
    indent = _first_indent([*head_lines, *incoming_lines])
    values = sorted(
        set(head_entries) | set(incoming_entries),
        key=_literal_sort_key,
    )
    lines = [f'{indent}"{value}",' for value in values]
    return _join_block_lines(lines), "__all__ list union"


def _string_list_entries(lines: list[str]) -> list[str] | None:
    meaningful = [line.strip() for line in lines if line.strip()]
    if not meaningful:
        return None
    try:
        expr = ast.parse("[" + "\n".join(meaningful) + "\n]", mode="eval")
    except SyntaxError:
        return None
    if not isinstance(expr.body, ast.List):
        return None
    values: list[str] = []
    for elt in expr.body.elts:
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
            return None
        values.append(elt.value)
    return values


def _resolve_import_union(
    head_lines: list[str],
    incoming_lines: list[str],
) -> tuple[str, str] | None:
    head = _import_statements(head_lines)
    incoming = _import_statements(incoming_lines)
    if head is None or incoming is None:
        return None
    imports = sorted(set(head) | set(incoming), key=str.lower)
    return _join_block_lines(imports), "import statement union"


def _import_statements(lines: list[str]) -> list[str] | None:
    if not lines or any(line[:1].isspace() for line in lines if line.strip()):
        return None
    try:
        tree = ast.parse(_join_block_lines(lines))
    except SyntaxError:
        return None
    if not tree.body:
        return None
    imports: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            return None
        imports.append(ast.unparse(node))
    return imports


def _resolve_distinct_symbol_adds(
    head_lines: list[str],
    incoming_lines: list[str],
) -> tuple[str, str] | None:
    head = _top_level_symbol_blocks(head_lines)
    incoming = _top_level_symbol_blocks(incoming_lines)
    if head is None or incoming is None:
        return None
    head_names = {name for name, _text in head}
    incoming_names = {name for name, _text in incoming}
    if not head_names or not incoming_names or head_names & incoming_names:
        return None
    ordered = sorted([*head, *incoming], key=lambda item: _literal_sort_key(item[0]))
    return "\n\n".join(text.rstrip("\n") for _name, text in ordered) + "\n", (
        "add/add distinct symbol"
    )


def _top_level_symbol_blocks(lines: list[str]) -> list[tuple[str, str]] | None:
    source = _join_block_lines(lines)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body:
        return None
    symbols: list[tuple[str, str]] = []
    source_lines = source.splitlines()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return None
        end_lineno = getattr(node, "end_lineno", None)
        if end_lineno is None:
            return None
        block_text = "\n".join(source_lines[node.lineno - 1:end_lineno]) + "\n"
        symbols.append((node.name, block_text))
    return symbols


def _resolve_dict_literal_adds(
    head_lines: list[str],
    incoming_lines: list[str],
) -> tuple[str, str] | None:
    head = _dict_entries(head_lines)
    incoming = _dict_entries(incoming_lines)
    if head is None or incoming is None:
        return None
    head_keys = {key for key, _line in head}
    incoming_keys = {key for key, _line in incoming}
    if not head_keys or not incoming_keys or head_keys & incoming_keys:
        return None
    entries = sorted([*head, *incoming], key=lambda item: _literal_sort_key(item[0]))
    return _join_block_lines([line for _key, line in entries]), (
        "dict literal disjoint additions"
    )


def _dict_entries(lines: list[str]) -> list[tuple[str, str]] | None:
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return None
    source = "{\n" + "\n".join(meaningful) + "\n}"
    try:
        expr = ast.parse(source, mode="eval")
    except SyntaxError:
        return None
    if not isinstance(expr.body, ast.Dict):
        return None
    if len(expr.body.keys) != len(meaningful):
        return None
    entries: list[tuple[str, str]] = []
    for key_node, line in zip(expr.body.keys, meaningful):
        if not isinstance(key_node, ast.Constant):
            return None
        key = key_node.value
        if not isinstance(key, (str, int, float, bool)):
            return None
        entries.append((str(key), line))
    return entries


def _resolve_comment_docstring_adds(
    head_lines: list[str],
    incoming_lines: list[str],
) -> tuple[str, str] | None:
    if not _comment_or_docstring_lines(head_lines):
        return None
    if not _comment_or_docstring_lines(incoming_lines):
        return None
    indent = _first_indent([*head_lines, *incoming_lines])
    separator = f"{indent}# ---"
    return _join_block_lines([*head_lines, separator, *incoming_lines]), (
        "add-only docstring/comment"
    )


def _comment_or_docstring_lines(lines: list[str]) -> bool:
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return False
    if all(line.lstrip().startswith("#") for line in meaningful):
        return True
    try:
        tree = ast.parse(_join_block_lines(meaningful))
    except SyntaxError:
        return False
    return all(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for node in tree.body
    )


def _first_indent(lines: list[str]) -> str:
    for line in lines:
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return ""


def _is_oversized(blocks: list[ConflictBlock], resolution: Resolution) -> bool:
    total = sum(b.n_conflict_lines for b in blocks)
    if total > MAX_CONFLICT_LINES:
        return True
    resolved_lines = sum(len(s.splitlines()) for s in resolution.diff.split("@@")[1:])
    if resolved_lines > total * MAX_RESOLUTION_EXPANSION_FACTOR:
        return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core orchestration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def resolve_conflict(
    request: ConflictRequest,
    *,
    deps: MergerDeps | None = None,
) -> ResolutionOutcome:
    """Full resolve-and-vote flow.  Never raises — every failure path
    produces a ``ResolutionOutcome`` the caller can log / surface.

    Order of operations (from the spec):

      1. 3-strike gate — if this change has failed >= MAX_FAILURES_PER_CHANGE
         times, refuse immediately.
      2. Security-file gate — refuse unconditionally; no push, no vote.
      3. Multi-file gate — abstain if request touches > 1 file.
      4. Parse conflicts — empty list is "no conflict found".
      5. Oversized gate — abstain if combined conflict > MAX_CONFLICT_LINES.
      6. LLM call — abstain on unavailable / invalid JSON.
      7. New-logic gate — refuse if LLM admits new logic.
      8. Confidence gate — abstain if < MIN_CONFIDENCE_FOR_PLUS_TWO.
      9. Test gate — run affected-module tests; refuse-push on failure.
     10. Push patchset.
     11. Post +2 vote.
     12. Audit.

    Metrics emitted:
      * ``merger_agent_plus_two_total`` on successful +2.
      * ``merger_agent_abstain_total`` on any abstain branch.
      * ``merger_agent_security_refusal_total`` on security refusal.
      * ``merger_agent_confidence`` histogram (observed on any LLM run).
    """
    deps = deps or MergerDeps()

    change_id = request.change_id or "<unknown-change>"
    current_failures = get_failure_count(change_id)

    # ── 1. 3-strike gate ─────────────────────────────────────────
    if current_failures >= MAX_FAILURES_PER_CHANGE:
        outcome = _build_abstain(
            request, MergerReason.refused_escalated,
            confidence=0.0,
            rationale=(f"change has failed {current_failures} times; "
                       f"merger refuses to retry (CLAUDE.md 3-strike)"),
        )
        outcome.failure_count = current_failures
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 2. Security-file gate ────────────────────────────────────
    if is_security_sensitive(request.file_path):
        outcome = _build_refusal(
            request, MergerReason.refused_security_file,
            rationale=(f"{request.file_path!r} matches a security-sensitive "
                       f"pattern; merger refuses by policy"),
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 3. Multi-file gate ───────────────────────────────────────
    extra = [p for p in request.additional_files if p and p != request.file_path]
    if extra:
        coupling_components = _classify_coupling(
            [request.file_path, *extra],
            request.workspace or os.getcwd(),
            request.conflict_text,
        )
        coupling_summary = [sorted(component) for component in coupling_components]
        logger.info(
            "merger_agent: multi-file coupling summary "
            "jira=%s change=%s components=%s",
            request.jira_ticket or "(none)",
            request.change_number or request.change_id or "(unknown)",
            coupling_summary,
        )
        outcome = _build_abstain(
            request, MergerReason.abstained_multi_file,
            confidence=0.0,
            rationale=(f"patchset touches {len(extra) + 1} files; "
                       f"merger only auto-votes on single-file conflicts"),
            metadata={
                "additional_files": extra,
                "coupling_components": coupling_summary,
            },
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 4. Parse conflicts ───────────────────────────────────────
    blocks = parse_conflict_block(request.conflict_text)
    if not blocks:
        outcome = _build_refusal(
            request, MergerReason.refused_no_conflict,
            rationale="no <<<<<<< / ======= / >>>>>>> markers found",
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    nested_blocks = [b for b in blocks if b.has_nested_markers]
    if nested_blocks:
        logger.warning(
            "nested_marker_warning change_id=%s file=%s blocks=%s",
            request.change_id,
            request.file_path,
            [b.start_line for b in nested_blocks],
        )
        outcome = _build_refusal(
            request, MergerReason.refused_nested_markers,
            rationale="nested conflict markers found inside parsed conflict block",
            metadata={"nested_marker_start_lines": [
                b.start_line for b in nested_blocks
            ]},
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    total_lines = sum(b.n_conflict_lines for b in blocks)
    if total_lines > MAX_CONFLICT_LINES:
        outcome = _build_abstain(
            request, MergerReason.abstained_oversized,
            confidence=0.0,
            rationale=(f"combined conflict {total_lines} lines exceeds "
                       f"gate {MAX_CONFLICT_LINES}"),
            metadata={"conflict_lines": total_lines},
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    deterministic = try_deterministic_merge(request, blocks)
    if deterministic is not None:
        logger.info(
            "merger_path=deterministic change=%s file=%s patterns=%s",
            request.change_number or change_id,
            request.file_path,
            deterministic.metadata.get("deterministic_patterns", []),
        )
        test_result = await deps.test_runner(request)
        if not test_result.ok:
            _bump_failure(change_id)
            outcome = _build_refusal(
                request,
                MergerReason.refused_test_failure,
                rationale=(
                    "deterministic merge verifier failed: "
                    f"{test_result.summary or test_result.command}"
                ),
                confidence=deterministic.confidence,
                diff_preview=deterministic.diff_preview,
                metadata=deterministic.metadata,
            )
            outcome.changed_identifiers = list(deterministic.changed_identifiers)
            outcome.test_result = {
                "ok": False,
                "summary": test_result.summary,
                "command": test_result.command,
            }
            outcome.failure_count = get_failure_count(change_id)
            _observe_metric(outcome)
            await _safe_audit(deps.audit, outcome)
            return outcome

        deterministic.test_result = {
            "ok": True,
            "summary": test_result.summary,
            "command": test_result.command,
        }
        if not request.push_locally:
            _observe_metric(deterministic)
            await _safe_audit(deps.audit, deterministic)
            return deterministic

        resolution = Resolution(
            resolved_text=deterministic.resolved_text,
            confidence=deterministic.confidence,
            rationale=deterministic.rationale,
            diff=deterministic.diff_preview,
            changed_blocks=len(blocks),
            changed_identifiers=list(deterministic.changed_identifiers),
        )
        commit_message = _build_patchset_message(request, resolution)
        push = await deps.pusher.push(
            change_id=change_id,
            project=request.project,
            workspace=request.workspace,
            file_path=request.file_path,
            resolved_text=resolution.resolved_text,
            commit_message=commit_message,
        )
        if not push.ok:
            _bump_failure(change_id)
            outcome = _build_refusal(
                request,
                MergerReason.refused_push_failed,
                rationale=f"Gerrit push failed: {push.reason}",
                confidence=resolution.confidence,
                diff_preview=resolution.diff,
                metadata=deterministic.metadata,
            )
            outcome.changed_identifiers = list(resolution.changed_identifiers)
            outcome.failure_count = get_failure_count(change_id)
            _observe_metric(outcome)
            await _safe_audit(deps.audit, outcome)
            return outcome

        review_sha = push.sha or request.patchset_revision
        review = await deps.reviewer.post_review(
            commit_sha=review_sha,
            project=request.project,
            message=_build_review_message(request, resolution, push),
            score=int(LabelVote.plus_two),
        )
        if not review.ok:
            outcome = _build_abstain(
                request,
                MergerReason.abstained_low_confidence,
                confidence=resolution.confidence,
                rationale=(
                    f"deterministic patchset pushed but +2 vote call failed: "
                    f"{review.reason}; human to take over"
                ),
                diff_preview=resolution.diff,
                metadata={
                    **deterministic.metadata,
                    "push_sha": push.sha,
                    "review_url": push.review_url,
                },
            )
            outcome.push_sha = push.sha
            outcome.review_url = push.review_url
            outcome.changed_identifiers = list(resolution.changed_identifiers)
            outcome.test_result = deterministic.test_result
            _observe_metric(outcome)
            await _safe_audit(deps.audit, outcome)
            return outcome

        hashtag_set_ok = True
        hashtag_set_reason = ""
        try:
            ht_res = await deps.hashtag_setter.add_hashtag(
                change_id=change_id,
                project=request.project,
                hashtag=CONFLICT_RESOLVED_HASHTAG,
            )
            hashtag_set_ok = ht_res.ok
            hashtag_set_reason = ht_res.reason
        except Exception as exc:                           # pragma: no cover
            hashtag_set_ok = False
            hashtag_set_reason = f"hashtag_setter raised: {exc!r}"

        _reset_failure(change_id)
        deterministic.voted_score = LabelVote.plus_two
        deterministic.push_sha = push.sha
        deterministic.review_url = push.review_url
        deterministic.failure_count = 0
        deterministic.metadata = {
            **deterministic.metadata,
            "push_sha": push.sha,
            "review_url": push.review_url,
            "hashtag_set_ok": hashtag_set_ok,
            "hashtag_set_reason": hashtag_set_reason,
        }
        _observe_metric(deterministic)
        await _safe_audit(deps.audit, deterministic)
        _emit_sse_voted(deterministic)
        return deterministic

    # ── 5. Structural risk gate ─────────────────────────────────
    risk = classify_conflict_risk(request, blocks)
    risk_meta = _risk_metadata(risk)
    if risk.tier is MergerRiskTier.high:
        outcome = _build_abstain(
            request,
            MergerReason.abstained_low_confidence,
            confidence=0.0,
            rationale=(
                "structural risk HIGH before LLM invocation; "
                f"signals={','.join(risk.reasons) or 'none'}"
            ),
            metadata={**risk_meta, "conflict_lines": total_lines},
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 6. LLM call ──────────────────────────────────────────────
    prompt_gate = build_prompt_with_size_gate(request, blocks, risk)
    logger.info(
        "merger_agent: context_pack_bytes=%d merger_prompt_version=%s "
        "merger_prompt_size_bytes=%d sections_trimmed=%s "
        "prompt_limit_bytes=%d change=%s file=%s",
        len(prompt_gate.context_pack.encode("utf-8")),
        MERGER_PROMPT_VERSION,
        prompt_gate.prompt_size_bytes,
        _format_sections_trimmed(prompt_gate.sections_trimmed),
        prompt_gate.limit_bytes,
        request.change_number or change_id,
        request.file_path,
    )
    if prompt_gate.oversized:
        outcome = _build_abstain(
            request,
            MergerReason.abstained_prompt_oversized,
            confidence=0.0,
            rationale=(
                "LLM prompt remained over the hard input-size gate after "
                "trimming lower-priority context-pack sections"
            ),
            metadata={
                **risk_meta,
                "prompt_size_bytes": prompt_gate.prompt_size_bytes,
                "prompt_limit_bytes": prompt_gate.limit_bytes,
                "sections_trimmed": list(prompt_gate.sections_trimmed),
            },
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    prompt = prompt_gate.prompt
    try:
        raw, _tokens = await deps.llm(prompt)
        _observe_llm_cost(_estimate_llm_cost(_tokens))
    except Exception as exc:
        logger.warning("merger_agent: llm raised: %s", exc)
        raw = ""

    if not raw:
        _bump_failure(change_id)
        outcome = _build_abstain(
            request, MergerReason.refused_llm_unavailable,
            confidence=0.0,
            rationale="LLM returned empty response",
            metadata=risk_meta,
        )
        outcome.failure_count = get_failure_count(change_id)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    try:
        payload = _parse_llm_response(raw)
        resolution = _assemble_resolution(request, blocks, payload)
    except _LLMParseError as exc:
        _bump_failure(change_id)
        outcome = _build_abstain(
            request, MergerReason.refused_llm_invalid_json,
            confidence=0.0,
            rationale=f"LLM returned invalid payload: {exc}",
            metadata={**risk_meta, "raw_head": raw[:200]},
        )
        outcome.failure_count = get_failure_count(change_id)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    try:
        metrics.merger_confidence.observe(resolution.confidence)
    except Exception:
        pass

    # ── 6. New-logic gate ────────────────────────────────────────
    if bool(payload.get("new_logic_detected", False)):
        outcome = _build_abstain(
            request, MergerReason.refused_new_logic_detected,
            confidence=resolution.confidence,
            rationale=(f"LLM self-reported new logic invention; "
                       f"{resolution.rationale}"),
            diff_preview=resolution.diff,
            metadata=risk_meta,
        )
        outcome.changed_identifiers = list(resolution.changed_identifiers)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 7. Confidence gate ───────────────────────────────────────
    if resolution.confidence < MIN_CONFIDENCE_FOR_PLUS_TWO:
        outcome = _build_abstain(
            request, MergerReason.abstained_low_confidence,
            confidence=resolution.confidence,
            rationale=(f"confidence {resolution.confidence:.2f} < "
                       f"{MIN_CONFIDENCE_FOR_PLUS_TWO}; {resolution.rationale}"),
            diff_preview=resolution.diff,
            metadata=risk_meta,
        )
        outcome.changed_identifiers = list(resolution.changed_identifiers)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 8. Independent LLM-B review gate ────────────────────────
    review = await _review_proposal(request, blocks, resolution, deps=deps)
    sandwich_decision = "confirm" if review.confirmed else "object"
    logger.info(
        "merger_sandwich_decision=%s change=%s file=%s model=%s cost_usd=%.6f",
        sandwich_decision,
        request.change_number or change_id,
        request.file_path,
        review.model,
        review.cost_usd,
    )
    review_meta = {
        **risk_meta,
        "merger_sandwich_decision": sandwich_decision,
        "review_model": review.model,
        "review_cost_usd": review.cost_usd,
        "review_tokens_used": review.tokens_used,
        "review_prompt": review.prompt,
        "review_response": review.raw_response,
        "proposal_prompt": prompt,
        "proposal_response": raw,
    }
    if not review.confirmed:
        outcome = _build_abstain(
            request,
            MergerReason.refused_review_objected,
            confidence=resolution.confidence,
            rationale=(
                "LLM-B objected to LLM-A's proposed resolution; "
                f"{review.reason}"
            ),
            diff_preview=resolution.diff,
            metadata=review_meta,
        )
        outcome.changed_identifiers = list(resolution.changed_identifiers)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 9. Test gate (before push — spec: "fail => don't push") ──
    test_result = await deps.test_runner(request)
    if not test_result.ok:
        _bump_failure(change_id)
        outcome = _build_refusal(
            request, MergerReason.refused_test_failure,
            rationale=(f"affected-module tests failed: "
                       f"{test_result.summary or test_result.command}"),
            confidence=resolution.confidence,
            diff_preview=resolution.diff,
            metadata=review_meta,
        )
        outcome.changed_identifiers = list(resolution.changed_identifiers)
        outcome.test_result = {
            "ok": False,
            "summary": test_result.summary,
            "command": test_result.command,
        }
        outcome.failure_count = get_failure_count(change_id)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 10. Push patchset ───────────────────────────────────────
    commit_message = _build_patchset_message(request, resolution)

    # OP-1196 phase 3 — caller-side push handoff (Option C). The
    # gerrit-jira-bridge daemon sets ``push_locally=False`` because the
    # backend container has neither a workspace nor the merger-bot SSH
    # key, and the daemon (running on host) has both. Return the
    # fully-LLM-resolved file content so the caller can apply +
    # amend + push using its own credentials.
    if not request.push_locally:
        outcome = _build_abstain(
            request,
            reason=MergerReason.deferred_push_to_caller,
            confidence=resolution.confidence,
            rationale=(
                f"LLM produced a resolution at confidence "
                f"{resolution.confidence:.2f}; backend deferring push "
                f"to caller per push_locally=False (caller is "
                f"responsible for applying, amending as merger-agent-bot, "
                f"pushing, and posting the +2 vote)."
            ),
            diff_preview=resolution.diff,
            metadata=review_meta,
        )
        outcome.resolved_text = resolution.resolved_text
        outcome.changed_identifiers = list(resolution.changed_identifiers)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    push = await deps.pusher.push(
        change_id=change_id,
        project=request.project,
        workspace=request.workspace,
        file_path=request.file_path,
        resolved_text=resolution.resolved_text,
        commit_message=commit_message,
    )
    if not push.ok:
        _bump_failure(change_id)
        outcome = _build_refusal(
            request, MergerReason.refused_push_failed,
            rationale=f"Gerrit push failed: {push.reason}",
            confidence=resolution.confidence,
            diff_preview=resolution.diff,
            metadata=risk_meta,
        )
        outcome.changed_identifiers = list(resolution.changed_identifiers)
        outcome.failure_count = get_failure_count(change_id)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 11. Post +2 vote ────────────────────────────────────────
    review_sha = push.sha or request.patchset_revision
    review_message = _build_review_message(request, resolution, push)
    review = await deps.reviewer.post_review(
        commit_sha=review_sha,
        project=request.project,
        message=review_message,
        score=int(LabelVote.plus_two),
    )
    if not review.ok:
        # Push succeeded but vote failed — record abstain + reset
        # because we DID push a valid patchset.  Human will still see
        # the patchset and can +2 manually.
        outcome = _build_abstain(
            request, MergerReason.abstained_low_confidence,
            confidence=resolution.confidence,
            rationale=(f"patchset pushed but +2 vote call failed: "
                       f"{review.reason}; human to take over"),
            diff_preview=resolution.diff,
            metadata={
                **review_meta,
                "push_sha": push.sha,
                "review_url": push.review_url,
            },
        )
        outcome.push_sha = push.sha
        outcome.review_url = push.review_url
        outcome.changed_identifiers = list(resolution.changed_identifiers)
        outcome.test_result = {"ok": True, "summary": test_result.summary,
                               "command": test_result.command}
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 11b. Mark change with Merge-Conflict-Resolved hashtag ────
    # OP-694: the Gerrit Merger-Plus-2 submit-requirement is gated on
    # this hashtag (`applicableIf = hashtag:Merge-Conflict-Resolved`).
    # Setting it here is what makes the +2 we just cast actually count
    # toward the dual-sign gate. Best-effort: a hashtag failure does
    # NOT undo the +2 vote — fall-through is "non-conflict semantics"
    # (Merger-Plus-2 NOT_APPLICABLE), which is conservative for merge
    # decisions and surfaces via the audit log + outcome.metadata for
    # ops to spot.
    hashtag_set_ok = True
    hashtag_set_reason = ""
    try:
        ht_res = await deps.hashtag_setter.add_hashtag(
            change_id=change_id,
            project=request.project,
            hashtag=CONFLICT_RESOLVED_HASHTAG,
        )
        hashtag_set_ok = ht_res.ok
        hashtag_set_reason = ht_res.reason
    except Exception as exc:                               # pragma: no cover
        hashtag_set_ok = False
        hashtag_set_reason = f"hashtag_setter raised: {exc!r}"
    if not hashtag_set_ok:
        logger.warning(
            "merger_agent: +2 cast on %s but Merge-Conflict-Resolved "
            "hashtag set failed (%s); change will be treated as non-"
            "conflict by submit-rule (Merger-Plus-2 NOT_APPLICABLE).",
            change_id, hashtag_set_reason,
        )

    # ── 12. Success — +2 voted ───────────────────────────────────
    _reset_failure(change_id)
    outcome = ResolutionOutcome(
        change_id=change_id,
        file_path=request.file_path,
        reason=MergerReason.plus_two_voted,
        voted_score=LabelVote.plus_two,
        confidence=resolution.confidence,
        rationale=resolution.rationale,
        diff_preview=resolution.diff,
        push_sha=push.sha,
        review_url=push.review_url,
        failure_count=0,
        test_result={"ok": True, "summary": test_result.summary,
                     "command": test_result.command},
        metadata={
            **review_meta,
            "conflict_lines": total_lines,
            "blocks": len(blocks),
            "hashtag_set_ok": hashtag_set_ok,
            "hashtag_set_reason": hashtag_set_reason,
        },
        changed_identifiers=list(resolution.changed_identifiers),
    )
    _observe_metric(outcome)
    await _safe_audit(deps.audit, outcome)
    _emit_sse_voted(outcome)
    return outcome


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_abstain(
    request: ConflictRequest,
    reason: MergerReason,
    *,
    confidence: float,
    rationale: str,
    diff_preview: str = "",
    metadata: dict[str, Any] | None = None,
) -> ResolutionOutcome:
    return ResolutionOutcome(
        change_id=request.change_id or "<unknown>",
        file_path=request.file_path,
        reason=reason,
        voted_score=LabelVote.abstain,
        confidence=confidence,
        rationale=rationale,
        diff_preview=diff_preview,
        metadata=metadata or {},
    )


def _build_refusal(
    request: ConflictRequest,
    reason: MergerReason,
    *,
    rationale: str,
    confidence: float = 0.0,
    diff_preview: str = "",
    metadata: dict[str, Any] | None = None,
) -> ResolutionOutcome:
    # "Refusal" in our taxonomy is distinct from "abstain" only on
    # reason code + SSE routing — the vote is still 0.  The
    # distinction exists so metrics + audit can tell "policy said no"
    # apart from "merger wanted to but gate fired".
    return _build_abstain(
        request, reason,
        confidence=confidence,
        rationale=rationale,
        diff_preview=diff_preview,
        metadata=metadata,
    )


def _build_patchset_message(req: ConflictRequest, res: Resolution) -> str:
    """Commit trailer body for the new patchset."""
    return (
        f"Resolve merge conflict in {req.file_path}\n\n"
        f"Merger Agent confidence: {res.confidence:.2f}\n"
        f"Rationale: {res.rationale}\n\n"
        f"Change-Id: {req.change_id}\n"
        f"Resolved-By: merger-agent-bot\n"
    )


def _build_review_message(
    req: ConflictRequest, res: Resolution, push: PatchsetPushResult
) -> str:
    return (
        "Merger Agent: Code-Review +2 (scope: conflict-block correctness only).\n"
        f"File: {req.file_path}\n"
        f"Confidence: {res.confidence:.2f}\n"
        f"Rationale: {res.rationale}\n\n"
        f"Diff (conflict region only):\n{res.diff}\n\n"
        f"Patchset sha: {push.sha}\n"
        "NOTE: Submission still requires a human Code-Review +2 per "
        "the CLAUDE.md L1 Safety Rules (see O7 submit-rule)."
    )


async def _safe_audit(audit_fn: AuditSink, outcome: ResolutionOutcome) -> None:
    try:
        await audit_fn(
            f"merger.{outcome.reason.value}",
            outcome.change_id,
            outcome.to_dict(),
        )
    except Exception as exc:                           # pragma: no cover
        logger.debug("merger_agent: audit sink raised: %s", exc)


def _observe_metric(outcome: ResolutionOutcome) -> None:
    try:
        if outcome.reason is MergerReason.plus_two_voted:
            metrics.merger_plus_two_total.inc()
        elif outcome.reason is MergerReason.refused_security_file:
            metrics.merger_security_refusal_total.inc()
        else:
            metrics.merger_abstain_total.labels(
                reason=outcome.reason.value,
            ).inc()
    except Exception:
        pass


def _emit_sse_voted(outcome: ResolutionOutcome) -> None:
    """Best-effort SSE emit so the orchestration UI updates in real time.

    Two events fire:
      * legacy ``merger.<reason>`` (kept for backward compat with the
        invoke-channel subscribers).
      * O9 ``orchestration.merger.voted`` — structured event the
        orchestration dashboard subscribes to with a stable schema.
    """
    try:
        from backend.events import emit_invoke
        emit_invoke(
            f"merger.{outcome.reason.value}",
            f"Merger +2 on {outcome.change_id} ({outcome.file_path})",
            change_id=outcome.change_id,
            file_path=outcome.file_path,
            confidence=outcome.confidence,
            voted_score=int(outcome.voted_score),
            push_sha=outcome.push_sha,
        )
    except Exception as exc:                           # pragma: no cover
        logger.debug("merger_agent: legacy SSE emit failed: %s", exc)

    try:
        from backend.orchestration_observability import emit_merger_voted
        emit_merger_voted(
            change_id=outcome.change_id,
            file_path=outcome.file_path,
            reason=outcome.reason.value,
            voted_score=int(outcome.voted_score),
            confidence=outcome.confidence,
            push_sha=outcome.push_sha,
            review_url=outcome.review_url,
        )
    except Exception as exc:                           # pragma: no cover
        logger.debug("merger_agent: orchestration SSE emit failed: %s", exc)


__all__ = [
    "AUDIT_ENTITY_KIND",
    "ConflictBlock",
    "ConflictRequest",
    "ConflictRisk",
    "DEFAULT_MODEL",
    "GerritClientReviewer",
    "GerritReviewer",
    "GitPatchsetPusher",
    "LabelVote",
    "MAX_CONFLICT_LINES",
    "MAX_FAILURES_PER_CHANGE",
    "MIN_CONFIDENCE_FOR_PLUS_TWO",
    "MergerDeps",
    "MergerLLM",
    "MERGER_PROMPT_VERSION",
    "MergerReason",
    "MergerRiskTier",
    "PatchsetPushResult",
    "PatchsetPusher",
    "ProposalReview",
    "REVIEW_MODEL",
    "Resolution",
    "ResolutionOutcome",
    "ReviewerResult",
    "SYSTEM_PROMPT",
    "TestRunResult",
    "TestRunner",
    "_classify_coupling",
    "build_context_pack",
    "build_prompt",
    "build_review_prompt",
    "classify_conflict_risk",
    "get_failure_count",
    "is_security_sensitive",
    "parse_conflict_block",
    "reset_failure_counts_for_tests",
    "resolve_conflict",
    "try_deterministic_merge",
]
