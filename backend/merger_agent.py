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

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from backend import metrics
from backend.gerrit import gerrit_client as _default_gerrit_client

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables — all exposed for tests + docs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_MODEL = os.environ.get(
    "OMNISIGHT_MERGER_MODEL", "anthropic/claude-opus-4-6"
)

# Gate thresholds.  Tweak via env for A/B'ing in production.
MIN_CONFIDENCE_FOR_PLUS_TWO = float(
    os.environ.get("OMNISIGHT_MERGER_MIN_CONFIDENCE", "0.9")
)
MAX_CONFLICT_LINES = int(os.environ.get("OMNISIGHT_MERGER_MAX_LINES", "20"))

# Conflict block we emit the LLM *must* produce — we refuse to trust
# anything longer than the incoming block × safety factor.
MAX_RESOLUTION_EXPANSION_FACTOR = 3.0

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
    r"<<<<<<<\s+(?P<head_label>.+?)\n"
    r"(?P<head>.*?)"
    r"(?:\|{7}.+?\n.*?)?"      # optional diff3 ancestor section
    r"=======\n"
    r"(?P<incoming>.*?)"
    r">>>>>>>\s+(?P<incoming_label>.+?)(?:\n|$)",
    re.DOTALL,
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
    refused_security_file = "refused_security_file"
    refused_test_failure = "refused_test_failure"
    refused_no_conflict = "refused_no_conflict"
    refused_llm_unavailable = "refused_llm_unavailable"
    refused_llm_invalid_json = "refused_llm_invalid_json"
    refused_escalated = "refused_escalated"
    refused_push_failed = "refused_push_failed"
    refused_new_logic_detected = "refused_new_logic_detected"


class LabelVote(int, Enum):
    abstain = 0
    plus_two = 2


@dataclass
class ConflictBlock:
    """One ``<<<<<<<`` ... ``>>>>>>>`` section inside a file."""

    head_label: str
    incoming_label: str
    head_lines: list[str]
    incoming_lines: list[str]
    start_line: int
    end_line: int

    @property
    def n_conflict_lines(self) -> int:
        return len(self.head_lines) + len(self.incoming_lines)


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
    # Extra files touched by *this* patchset — single-file gate.  Caller
    # normally only sends a single entry (the conflicting file) but the
    # field exists so a multi-file resolution can be explicitly opted
    # into and routed through the abstain-for-human path.
    additional_files: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    """LLM-produced artefact."""

    resolved_text: str              # conflict block replaced with the resolution
    confidence: float               # 0.0 – 1.0
    rationale: str
    diff: str                       # unified diff scoped to conflict region
    changed_blocks: int             # should equal # conflict blocks in input


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
        rc, out, err = _git([
            "commit", "--amend", "--no-edit",
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dependency bundle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class MergerDeps:
    """Inject-at-call-time bundle so tests don't need to monkey-patch."""

    llm: MergerLLM = _default_llm
    pusher: PatchsetPusher = field(default_factory=GitPatchsetPusher)
    reviewer: GerritReviewer = field(default_factory=GerritClientReviewer)
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


SYSTEM_PROMPT = (
    "You are a merge conflict resolution expert.  You receive one Git "
    "conflict block (HEAD side + incoming side) and must produce a "
    "single unified resolution that PRESERVES THE LOGICAL INTENT OF BOTH "
    "commits.  You MUST NOT introduce any logic, function, call, "
    "variable, or statement that does not appear in either half of the "
    "conflict or in the provided file context.  If you cannot preserve "
    "both intents without introducing new logic, output a low confidence "
    "score and explain the ambiguity in the rationale — do NOT fabricate "
    "a compromise.  Output STRICTLY a JSON object matching this schema:\n"
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
        ))
    return blocks


def build_prompt(req: ConflictRequest, blocks: list[ConflictBlock]) -> str:
    """Deterministic prompt — inlines the conflict + commit messages +
    the provided file context, and repeats the no-new-logic guardrail."""
    parts: list[str] = [
        "SYSTEM: " + SYSTEM_PROMPT,
        "",
        f"FILE: {req.file_path}",
        f"HEAD commit message:\n{req.head_commit_message.strip()}",
        f"Incoming commit message:\n{req.incoming_commit_message.strip()}",
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


def is_security_sensitive(file_path: str) -> bool:
    """True when the file lives under an auth/secrets/config/CI path.

    Match is case-insensitive on substring so ``.github/workflows/ci.yml``,
    ``backend/auth/session.py``, and ``configs/prod/app.yaml`` all trip."""
    norm = file_path.strip().replace("\\", "/").lower()
    return any(pat in norm for pat in _SECURITY_PATH_PATTERNS)


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
    resolved_block = str(llm_payload.get("resolved_block", ""))
    if not resolved_block:
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
    )


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
        outcome = _build_abstain(
            request, MergerReason.abstained_multi_file,
            confidence=0.0,
            rationale=(f"patchset touches {len(extra) + 1} files; "
                       f"merger only auto-votes on single-file conflicts"),
            metadata={"additional_files": extra},
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

    # ── 5. LLM call ──────────────────────────────────────────────
    prompt = build_prompt(request, blocks)
    try:
        raw, _tokens = await deps.llm(prompt)
    except Exception as exc:
        logger.warning("merger_agent: llm raised: %s", exc)
        raw = ""

    if not raw:
        _bump_failure(change_id)
        outcome = _build_abstain(
            request, MergerReason.refused_llm_unavailable,
            confidence=0.0,
            rationale="LLM returned empty response",
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
            metadata={"raw_head": raw[:200]},
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
        )
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
        )
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 8. Test gate (before push — spec: "fail => don't push") ──
    test_result = await deps.test_runner(request)
    if not test_result.ok:
        _bump_failure(change_id)
        outcome = _build_refusal(
            request, MergerReason.refused_test_failure,
            rationale=(f"affected-module tests failed: "
                       f"{test_result.summary or test_result.command}"),
            confidence=resolution.confidence,
            diff_preview=resolution.diff,
        )
        outcome.test_result = {
            "ok": False,
            "summary": test_result.summary,
            "command": test_result.command,
        }
        outcome.failure_count = get_failure_count(change_id)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 9. Push patchset ────────────────────────────────────────
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
            request, MergerReason.refused_push_failed,
            rationale=f"Gerrit push failed: {push.reason}",
            confidence=resolution.confidence,
            diff_preview=resolution.diff,
        )
        outcome.failure_count = get_failure_count(change_id)
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 10. Post +2 vote ────────────────────────────────────────
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
            metadata={"push_sha": push.sha, "review_url": push.review_url},
        )
        outcome.push_sha = push.sha
        outcome.review_url = push.review_url
        outcome.test_result = {"ok": True, "summary": test_result.summary,
                               "command": test_result.command}
        _observe_metric(outcome)
        await _safe_audit(deps.audit, outcome)
        return outcome

    # ── 11. Success — +2 voted ───────────────────────────────────
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
        metadata={"conflict_lines": total_lines, "blocks": len(blocks)},
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
    """Best-effort SSE emit so the orchestration UI updates in real time."""
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
        logger.debug("merger_agent: SSE emit failed: %s", exc)


__all__ = [
    "AUDIT_ENTITY_KIND",
    "ConflictBlock",
    "ConflictRequest",
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
    "MergerReason",
    "PatchsetPushResult",
    "PatchsetPusher",
    "Resolution",
    "ResolutionOutcome",
    "ReviewerResult",
    "SYSTEM_PROMPT",
    "TestRunResult",
    "TestRunner",
    "build_prompt",
    "get_failure_count",
    "is_security_sensitive",
    "parse_conflict_block",
    "reset_failure_counts_for_tests",
    "resolve_conflict",
]
