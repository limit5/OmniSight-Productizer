#!/usr/bin/env python3
"""AI-assisted seed ticket refinement helper (OP-781).

Three-stage CLI to unblock the ~500 placeholder Story tickets paused on
2026-05-08 by the bulk-pause + ``runner-needs-refinement`` label sweep.
The 2026-05-08 OP-231 21-cycle infinite-revert loop traced back to a
ticket whose description was still
``_(operator: refine before pickup — TODO.md source line is the seed)_``;
auditing found ~509 sibling Story tickets in the same shape. Manually
refining each by hand is ~125h of operator time; this CLI proposes,
reviews, and applies LLM-drafted refinements at ~$10 / 4h instead.

Subcommands::

    refine_seed_tickets.py --propose [--limit N] [--dry-run] [--post-comment]
    refine_seed_tickets.py --review OP-XXX [--accept | --reject]
    refine_seed_tickets.py --apply  OP-XXX [--dry-run]

``--propose``
    Walks JIRA for tickets with the ``runner-needs-refinement`` label,
    sends each description (plus parent + sibling context) to a Haiku
    Anthropic call, and saves a structured JSON proposal to
    ``data/refine-proposals/OP-XXX.json``. ``--limit N`` caps the batch.

``--review``
    Pretty-prints the saved proposal next to the original placeholder
    description so the operator can eyeball it. ``--accept`` flips
    ``status: accepted`` in the proposal JSON; ``--reject`` flips it to
    ``rejected``.

``--apply``
    Reads the accepted proposal, builds a new description (replacing
    the placeholder Goal / AC / Files / Prerequisites sections), PUTs
    it to JIRA, switches issuetype Task → Story, removes the
    ``runner-needs-refinement`` label, adds ``refined-by:ai-assisted``,
    and posts a ``[ai-refined]`` audit comment. ``--apply`` also writes
    a per-action audit-log line into ``data/refine-proposals/audit.log``.

The default proposer wraps the native Anthropic SDK via
``backend.agents.anthropic_native_client.AnthropicClient`` (Haiku 4.5).
For deterministic / offline tests the proposer is injectable: pass any
callable returning a ``(json_text, token_usage)`` tuple as
``llm_proposer`` in :func:`generate_proposal`.

Costs: at the spec'd Haiku rate (~$0.02/ticket) a 500-ticket batch is
~$10. The cost report in ``--propose`` accumulates per-ticket token
counts so an operator running the batch can confirm spend before
proceeding.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd  # noqa: E402

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────

NEEDS_REFINEMENT_LABEL = "runner-needs-refinement"
REFINED_LABEL = "refined-by:ai-assisted"
PROPOSALS_DIR = REPO_ROOT / "data" / "refine-proposals"
AUDIT_LOG_PATH = PROPOSALS_DIR / "audit.log"
DEFAULT_AGENT_CLASS = "subscription-claude"

# Haiku 4.5 pricing per 1M tokens (input / output) — used for the cost
# report. Pinned 2026-05-08 from backend/events._MODEL_PRICING_PER_MTOK
# so the report stays consistent with the rest of the cost stack. A
# slightly stale rate is fine here — operators care about order of
# magnitude (~$10) not exact pennies.
HAIKU_INPUT_COST_PER_MTOK = 0.80
HAIKU_OUTPUT_COST_PER_MTOK = 4.00

PROPOSAL_SCHEMA_VERSION = 1

# Confidence tier heuristics. Designed to be cheap + explainable —
# overly clever scoring would drift from operator expectations.
HIGH_CONFIDENCE_KEYWORDS = (
    "add aria-label",
    "rename ",
    "delete ",
    "remove ",
    "rewrite ",
    "extract ",
    "wire ",
    "alembic migration",
    "expose endpoint",
    "type-check",
    "lint ",
)
LOW_CONFIDENCE_HINTS = (
    "與 iso",  # JP/CN spec-alignment one-liners
    "與 iec",
    "與 do-",
    "explore ",
    "investigate ",
    "research ",
    "tbd",
    "decide ",
    "evaluate options",
    "design tradeoff",
)


# ── Dataclasses ───────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenUsage:
    """Aggregated token counts for cost reporting."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * HAIKU_INPUT_COST_PER_MTOK
            + self.output_tokens / 1_000_000 * HAIKU_OUTPUT_COST_PER_MTOK
        )


@dataclass
class Proposal:
    """One LLM-drafted refinement, persisted to ``data/refine-proposals/``."""

    key: str
    schema_version: int
    summary: str
    original_description: str
    proposed_acceptance_criteria: list[str]
    proposed_files: list[str]
    proposed_prerequisites: dict[str, list]
    confidence: str            # "high" / "medium" / "low"
    confidence_reason: str
    llm_model: str
    token_usage: TokenUsage
    cost_usd: float
    status: str = "pending"    # "pending" / "accepted" / "rejected" / "applied"
    proposed_at: str = ""
    raw_llm_output: str = ""
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "schema_version": self.schema_version,
            "summary": self.summary,
            "original_description": self.original_description,
            "proposed_acceptance_criteria": list(self.proposed_acceptance_criteria),
            "proposed_files": list(self.proposed_files),
            "proposed_prerequisites": dict(self.proposed_prerequisites),
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "llm_model": self.llm_model,
            "token_usage": {
                "input_tokens": self.token_usage.input_tokens,
                "output_tokens": self.token_usage.output_tokens,
            },
            "cost_usd": round(self.cost_usd, 6),
            "status": self.status,
            "proposed_at": self.proposed_at,
            "raw_llm_output": self.raw_llm_output,
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, data: dict) -> "Proposal":
        usage = data.get("token_usage", {}) or {}
        return cls(
            key=data["key"],
            schema_version=data.get("schema_version", PROPOSAL_SCHEMA_VERSION),
            summary=data.get("summary", ""),
            original_description=data.get("original_description", ""),
            proposed_acceptance_criteria=list(data.get("proposed_acceptance_criteria") or []),
            proposed_files=list(data.get("proposed_files") or []),
            proposed_prerequisites=dict(data.get("proposed_prerequisites") or {}),
            confidence=data.get("confidence", "medium"),
            confidence_reason=data.get("confidence_reason", ""),
            llm_model=data.get("llm_model", "unknown"),
            token_usage=TokenUsage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
            ),
            cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
            status=data.get("status", "pending"),
            proposed_at=data.get("proposed_at", ""),
            raw_llm_output=data.get("raw_llm_output", ""),
            notes=list(data.get("notes") or []),
        )


# ── Confidence classifier ──────────────────────────────────────────


def classify_confidence(summary: str, original_description: str) -> tuple[str, str]:
    """Heuristic-only confidence tier per the OP-781 spec.

    Returns ``(tier, reason)``. The classifier intentionally errs
    toward ``medium`` when no strong signal is present so the operator
    is nudged to glance rather than auto-accept dubious proposals.
    """
    summary_lc = summary.lower()
    desc_lc = original_description.lower()

    # High-confidence keywords win first — a concrete verb like
    # "add aria-label" implies a recognisable AC pattern even if the
    # rest of the description is terse.
    for kw in HIGH_CONFIDENCE_KEYWORDS:
        if kw in summary_lc or kw in desc_lc:
            return "high", f"summary/description contains high-confidence keyword: {kw!r}"

    # Strategic-research one-liners ("L4.7.5 — 與 ISO 26262 對齊") are
    # the canonical low-confidence shape from the OP-781 spec.
    for hint in LOW_CONFIDENCE_HINTS:
        if hint in summary_lc or hint in desc_lc:
            return "low", f"summary/description contains low-confidence hint: {hint!r}"

    # Final fallback: very short summary + description with neither
    # high- nor low-confidence keyword is treated as low (the LLM has
    # nothing concrete to anchor onto).
    summary_words = len(summary_lc.split())
    desc_words = len(desc_lc.split())
    if desc_words < 25 and summary_words < 12:
        return "low", "summary + description are both very short — Goal is one-liner"

    return "medium", "no high/low signal — defaults to medium for operator review"


# ── Prompt builder ─────────────────────────────────────────────────


PROMPT_SYSTEM = """You are refining a JIRA ticket draft. Your output must be VALID JSON \
matching this schema (no prose, no markdown fences):

{
  "acceptance_criteria": ["...", "...", ...],   // 3-5 items, each with concrete evidence pattern
  "files": ["path/one.py", "path/two.md", ...], // best-guess paths the work will touch
  "prerequisites": {
    "blocks_on": [],
    "soft_prereqs": [],
    "mutex_with": [],
    "schema_locks": [],
    "live_state_requires": [],
    "external_blockers": []
  },
  "notes": "free-text caveats / uncertainty / ambiguous goal"
}

Rules:
- Each AC must be a concrete checklist item with an evidence pattern (test name,
  file:line range, or visible behaviour).
- Files paths must be repo-relative and plausible; do NOT invent fictional paths
  outside the obvious area for the ticket.
- Prerequisites must be a strict subset of the schema above; empty arrays are OK.
- If the goal is genuinely ambiguous or under-specified, populate "notes" with a
  short sentence explaining what context is missing — do NOT guess wildly.
- Output JSON only. No backticks. No commentary.
"""


def build_prompt(
    *,
    summary: str,
    description: str,
    parent_summary: str | None,
    parent_description: str | None,
    sibling_keys: list[str],
) -> str:
    """Assemble the user-message for the proposer LLM call."""
    parts: list[str] = []
    parts.append(f"Ticket summary: {summary}\n")
    parts.append("Original description (placeholder):\n" + (description or "(empty)"))
    if parent_summary:
        parts.append("\nParent (Wave) summary: " + parent_summary)
    if parent_description:
        parts.append("\nParent (Wave) description excerpt:\n" + parent_description[:1500])
    if sibling_keys:
        parts.append("\nSibling tickets (same area / priority): " + ", ".join(sibling_keys[:8]))
    parts.append(
        "\nPropose 3-5 concrete Acceptance Criteria, the files/paths the work will "
        "touch, and a Prerequisites YAML block. Output JSON only."
    )
    return "\n".join(parts)


# ── LLM proposer plumbing ──────────────────────────────────────────


LlmProposer = Callable[[str, str], tuple[str, TokenUsage]]
"""Callable contract: ``(system_prompt, user_prompt) -> (json_text, token_usage)``.

Injected into :func:`generate_proposal` so tests can swap in a deterministic
fake instead of round-tripping a real Anthropic call.
"""


def _default_llm_proposer(
    *,
    model: str = "claude-haiku-4-5-20251001",
) -> LlmProposer:
    """Default proposer using ``backend.agents.anthropic_native_client``.

    Lazy-imports the SDK so the script (and its tests) can be loaded
    without ``anthropic`` installed; the import cost is paid only on
    actual ``--propose`` invocations.
    """
    def _proposer(system: str, user: str) -> tuple[str, TokenUsage]:
        from backend.agents.anthropic_native_client import AnthropicClient

        client = AnthropicClient(default_model=model, max_tokens_default=4096)
        text, usage = client.simple(
            prompt=user,
            system=system,
            model=model,
            max_tokens=4096,
            temperature=0.2,
        )
        return text, TokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    return _proposer


# ── Proposal generation ────────────────────────────────────────────


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_llm_json(text: str) -> dict:
    """Best-effort JSON extraction.

    The system prompt forbids markdown fences but a Haiku response
    occasionally still ships them; we strip the outer envelope before
    parsing. Raises ``ValueError`` with a snippet on parse failure so
    the caller can record a useful note in the proposal file.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json\n{...}\n```  →  {...}
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(text)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"could not parse JSON from LLM response: {text[:200]!r}")


def generate_proposal(
    issue: dict,
    *,
    parent_summary: str | None = None,
    parent_description: str | None = None,
    sibling_keys: list[str] | None = None,
    llm_proposer: LlmProposer | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> Proposal:
    """Run the LLM and return a :class:`Proposal` ready to persist.

    Pure function modulo the injected ``llm_proposer`` — does not touch
    JIRA. ``issue`` is a JIRA REST issue dict (the shape returned by
    ``GET /rest/api/3/issue/<key>``).
    """
    proposer = llm_proposer or _default_llm_proposer(model=model)
    fields = issue.get("fields") or {}
    summary = fields.get("summary", "")
    description = _adf_to_text(fields.get("description"))

    user_prompt = build_prompt(
        summary=summary,
        description=description,
        parent_summary=parent_summary,
        parent_description=parent_description,
        sibling_keys=sibling_keys or [],
    )
    raw_text, usage = proposer(PROMPT_SYSTEM, user_prompt)

    notes: list[str] = []
    try:
        parsed = parse_llm_json(raw_text)
    except (ValueError, json.JSONDecodeError) as exc:
        parsed = {}
        notes.append(f"LLM JSON parse failed: {exc}")

    ac = [str(x).strip() for x in (parsed.get("acceptance_criteria") or []) if str(x).strip()]
    files = [str(x).strip() for x in (parsed.get("files") or []) if str(x).strip()]
    prereqs_raw = parsed.get("prerequisites") or {}
    prereqs = {
        "blocks_on": list(prereqs_raw.get("blocks_on") or []),
        "soft_prereqs": list(prereqs_raw.get("soft_prereqs") or []),
        "mutex_with": list(prereqs_raw.get("mutex_with") or []),
        "schema_locks": list(prereqs_raw.get("schema_locks") or []),
        "live_state_requires": list(prereqs_raw.get("live_state_requires") or []),
        "external_blockers": list(prereqs_raw.get("external_blockers") or []),
    }
    extra_note = str(parsed.get("notes") or "").strip()
    if extra_note:
        notes.append(extra_note)

    confidence, reason = classify_confidence(summary, description)
    if not ac:
        # No AC parsed → downgrade confidence so operator must look.
        confidence = "low"
        reason = "no acceptance criteria parsed from LLM output"

    cost = usage.cost_usd()
    return Proposal(
        key=issue["key"],
        schema_version=PROPOSAL_SCHEMA_VERSION,
        summary=summary,
        original_description=description,
        proposed_acceptance_criteria=ac,
        proposed_files=files,
        proposed_prerequisites=prereqs,
        confidence=confidence,
        confidence_reason=reason,
        llm_model=model,
        token_usage=usage,
        cost_usd=cost,
        status="pending",
        proposed_at=datetime.now(timezone.utc).isoformat(),
        raw_llm_output=raw_text,
        notes=notes,
    )


# ── Proposal persistence ───────────────────────────────────────────


def proposal_path(key: str, base_dir: Path = PROPOSALS_DIR) -> Path:
    return base_dir / f"{key}.json"


def save_proposal(proposal: Proposal, base_dir: Path = PROPOSALS_DIR) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    path = proposal_path(proposal.key, base_dir)
    path.write_text(json.dumps(proposal.to_json(), indent=2, ensure_ascii=False))
    return path


def load_proposal(key: str, base_dir: Path = PROPOSALS_DIR) -> Proposal:
    path = proposal_path(key, base_dir)
    if not path.exists():
        raise FileNotFoundError(f"no proposal at {path}; run --propose first")
    return Proposal.from_json(json.loads(path.read_text()))


# ── ADF helpers + new-description renderer ────────────────────────


def _adf_to_text(adf: Any) -> str:
    """Walk an ADF doc tree and concat text + paragraph breaks."""
    if not adf:
        return ""
    chunks: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t == "text":
                chunks.append(node.get("text", ""))
            elif t == "hardBreak":
                chunks.append("\n")
            elif t == "paragraph":
                for c in node.get("content", []):
                    _walk(c)
                chunks.append("\n\n")
            elif t == "codeBlock":
                for c in node.get("content", []):
                    _walk(c)
                chunks.append("\n")
            else:
                for c in node.get("content", []):
                    _walk(c)
        elif isinstance(node, list):
            for c in node:
                _walk(c)

    _walk(adf)
    return "".join(chunks).strip()


def _adf_codeblock(markdown: str) -> dict:
    """Wrap the rendered new-description markdown in an ADF codeBlock.

    Mirrors the ``_adf_codeblock`` shape used by
    ``scripts/jira_seed_example_tickets.py`` so the runner sees the
    same description rendering as legacy seed Stories — markdown source
    visible verbatim, no markdown→ADF lossy conversion.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "attrs": {"language": "markdown"},
                "content": [{"type": "text", "text": markdown}],
            }
        ],
    }


def _yaml_safe_value(v: Any) -> str:
    """Cheap inline-YAML encoder for the prereqs block. PyYAML would do
    the same but bringing it in just for empty arrays + simple strings
    is overkill — the schema is restricted enough that this is safe.
    """
    if isinstance(v, str):
        if not v or any(ch in v for ch in ":#\n\""):
            return json.dumps(v, ensure_ascii=False)
        return v
    return json.dumps(v, ensure_ascii=False)


def render_prerequisites_yaml(prereqs: dict) -> str:
    keys = (
        "blocks_on", "soft_prereqs", "mutex_with",
        "schema_locks", "live_state_requires", "external_blockers",
    )
    lines: list[str] = []
    for k in keys:
        items = prereqs.get(k) or []
        if not items:
            lines.append(f"{k}: []")
            continue
        lines.append(f"{k}:")
        for it in items:
            lines.append(f"  - {_yaml_safe_value(it)}")
    return "\n".join(lines)


def render_refined_description(proposal: Proposal) -> str:
    """Render the new ticket description body from an accepted proposal.

    Layout matches the canonical Story format documented in
    ``scripts/jira_seed_example_tickets.py``: Goal / Acceptance Criteria
    / Files / Prerequisites / footer audit-trail block. The Goal is
    intentionally taken verbatim from the placeholder body so the LLM
    cannot rewrite operator intent — only AC + Files + Prereqs are
    proposal-driven.
    """
    goal_block = proposal.original_description.strip() or "(no goal supplied — refine before pickup)"
    ac_lines = "\n".join(f"- [ ] {ac}" for ac in proposal.proposed_acceptance_criteria) \
        or "- [ ] (no AC proposed — operator must add manually)"
    files_lines = "\n".join(f"- {p}" for p in proposal.proposed_files) \
        or "- (no files proposed — operator must add manually)"
    prereqs_yaml = render_prerequisites_yaml(proposal.proposed_prerequisites)

    notes_block = ""
    if proposal.notes:
        notes_block = "\n\n## LLM proposer notes\n\n" + "\n".join(f"- {n}" for n in proposal.notes)

    return (
        f"## Goal\n{goal_block}\n\n"
        f"## Acceptance Criteria\n{ac_lines}\n\n"
        f"## Files / Paths\n{files_lines}\n\n"
        f"## Prerequisites\n\n```yaml\n{prereqs_yaml}\n```\n"
        f"{notes_block}\n\n"
        f"---\n"
        f"**Refined by ai-assisted proposal** ({proposal.llm_model}, "
        f"confidence: {proposal.confidence}).\n"
        f"Original placeholder preserved in proposal file under "
        f"`data/refine-proposals/{proposal.key}.json`.\n"
    )


# ── JIRA wrappers ─────────────────────────────────────────────────


def fetch_needs_refinement(client: jd.DispatchClient, max_results: int = 100) -> list[dict]:
    """Page-walk JIRA for every Task carrying the refinement label.

    Returns a list of full issue payloads (with ``description``,
    ``parent``, ``labels``, ``issuetype``). The label search is
    intentionally broad — it does NOT filter on status or issuetype
    because the bulk-pause action moved tickets across both axes; the
    label is the only stable handle on "this needs refinement".
    """
    issues: list[dict] = []
    start_at = 0
    while True:
        resp = jd._request(
            client, "POST", "/search/jql",
            {
                "jql": (
                    f'project = "{client.project_key}" '
                    f'AND labels = "{NEEDS_REFINEMENT_LABEL}" '
                    f'AND labels != "{REFINED_LABEL}" '
                    f'ORDER BY key ASC'
                ),
                "fields": [
                    "summary", "description", "labels", "status",
                    "issuetype", "parent",
                ],
                "maxResults": min(50, max_results - len(issues)),
                "startAt": start_at,
            },
        )
        page = resp.get("issues", []) or []
        if not page:
            break
        issues.extend(page)
        if len(issues) >= max_results:
            break
        if len(page) < 50:
            break
        start_at += len(page)
    return issues[:max_results]


def fetch_full_issue(client: jd.DispatchClient, key: str) -> dict:
    return jd._request(
        client, "GET",
        f"/issue/{key}?fields=summary,description,labels,status,issuetype,parent",
    )


def fetch_parent_context(client: jd.DispatchClient, parent_key: str) -> tuple[str | None, str | None]:
    try:
        issue = jd._request(
            client, "GET",
            f"/issue/{parent_key}?fields=summary,description",
        )
    except RuntimeError as exc:
        logger.debug("parent fetch failed for %s: %s", parent_key, exc)
        return None, None
    fields = issue.get("fields") or {}
    return fields.get("summary"), _adf_to_text(fields.get("description"))


def update_description(
    client: jd.DispatchClient,
    key: str,
    new_markdown: str,
    *,
    idem_key: str | None = None,
) -> None:
    idem = idem_key or f"refine-desc-{key}"
    jd._request_idempotent(
        client, "PUT", f"/issue/{key}",
        {"fields": {"description": _adf_codeblock(new_markdown)}},
        idem,
    )


def change_issuetype(
    client: jd.DispatchClient,
    key: str,
    issuetype_name: str,
    *,
    idem_key: str | None = None,
) -> None:
    """PUT issuetype on an issue (Task → Story for un-pause).

    Atlassian Cloud accepts the English issuetype name on the OP project
    even when the UI shows the Japanese localised name (verified via
    ``scripts/jira_seed_example_tickets.py`` which already round-trips
    "ストーリー" / "Story" interchangeably).
    """
    idem = idem_key or f"refine-issuetype-{key}-{issuetype_name}"
    jd._request_idempotent(
        client, "PUT", f"/issue/{key}",
        {"fields": {"issuetype": {"name": issuetype_name}}},
        idem,
    )


# ── Audit log ─────────────────────────────────────────────────────


def append_audit_log(
    *,
    operator: str,
    action: str,
    key: str,
    token_usage: TokenUsage | None = None,
    extra: dict | None = None,
    log_path: Path = AUDIT_LOG_PATH,
) -> None:
    """Append one JSONL line to ``data/refine-proposals/audit.log``.

    Keeps the footprint minimal (operator + ts + action + token count
    only) so the file remains greppable. Errors here are non-fatal —
    failing to log must not block the apply.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "operator": operator,
        "action": action,
        "key": key,
    }
    if token_usage is not None:
        entry["token_usage"] = {
            "input_tokens": token_usage.input_tokens,
            "output_tokens": token_usage.output_tokens,
        }
        entry["cost_usd"] = round(token_usage.cost_usd(), 6)
    if extra:
        entry.update(extra)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Subcommand: --propose ─────────────────────────────────────────


def cmd_propose(
    args: argparse.Namespace,
    *,
    client: jd.DispatchClient | None = None,
    llm_proposer: LlmProposer | None = None,
    base_dir: Path = PROPOSALS_DIR,
) -> int:
    client = client or jd.make_client(args.agent_class)
    issues = fetch_needs_refinement(client, max_results=args.limit or 500)
    if not issues:
        print("No tickets carry the runner-needs-refinement label. Nothing to do.")
        return 0

    print(f"Found {len(issues)} ticket(s) with label {NEEDS_REFINEMENT_LABEL!r}.")
    if args.dry_run:
        for issue in issues:
            print(f"  [dry-run] would propose: {issue['key']} — {issue['fields']['summary']}")
        return 0

    total_usage = TokenUsage()
    per_ticket_breakdown: list[tuple[str, TokenUsage, float]] = []
    skipped_existing: list[str] = []

    for idx, issue in enumerate(issues, start=1):
        key = issue["key"]
        path = proposal_path(key, base_dir)
        if path.exists() and not args.overwrite:
            skipped_existing.append(key)
            continue

        parent_summary: str | None = None
        parent_description: str | None = None
        parent = (issue.get("fields") or {}).get("parent")
        if parent and parent.get("key"):
            parent_summary, parent_description = fetch_parent_context(client, parent["key"])

        try:
            proposal = generate_proposal(
                issue,
                parent_summary=parent_summary,
                parent_description=parent_description,
                sibling_keys=[],
                llm_proposer=llm_proposer,
                model=args.model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("propose failed for %s: %s", key, exc)
            print(f"  [{idx}/{len(issues)}] {key}: ERROR {type(exc).__name__}: {exc}")
            continue

        save_proposal(proposal, base_dir)
        total_usage = total_usage + proposal.token_usage
        per_ticket_breakdown.append((key, proposal.token_usage, proposal.cost_usd))
        print(
            f"  [{idx}/{len(issues)}] {key} → {path.name} "
            f"(confidence: {proposal.confidence}, "
            f"in: {proposal.token_usage.input_tokens} tok, "
            f"out: {proposal.token_usage.output_tokens} tok, "
            f"cost: ${proposal.cost_usd:.4f})"
        )

        if args.post_comment:
            try:
                jd.add_comment(
                    client, key,
                    (
                        "[ai-proposal] Refinement proposal saved to "
                        f"data/refine-proposals/{key}.json. "
                        f"Confidence: {proposal.confidence}. "
                        "Operator: review + --apply once accepted."
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("comment post failed for %s: %s", key, exc)

        append_audit_log(
            operator=client.bot_email,
            action="propose",
            key=key,
            token_usage=proposal.token_usage,
            extra={"confidence": proposal.confidence},
            log_path=base_dir / "audit.log",
        )

    print()
    print("=" * 60)
    print("Cost report")
    print("=" * 60)
    print(f"  Tickets proposed: {len(per_ticket_breakdown)}")
    if skipped_existing:
        print(f"  Skipped (proposal already exists, use --overwrite): {len(skipped_existing)}")
    print(f"  Total input tokens : {total_usage.input_tokens:,}")
    print(f"  Total output tokens: {total_usage.output_tokens:,}")
    print(f"  Total cost (Haiku) : ${total_usage.cost_usd():.4f}")
    if per_ticket_breakdown:
        avg_cost = total_usage.cost_usd() / len(per_ticket_breakdown)
        print(f"  Avg cost / ticket  : ${avg_cost:.4f}")
    return 0


# ── Subcommand: --review ──────────────────────────────────────────


def cmd_review(
    args: argparse.Namespace,
    *,
    base_dir: Path = PROPOSALS_DIR,
) -> int:
    proposal = load_proposal(args.key, base_dir)

    if args.accept:
        proposal.status = "accepted"
        save_proposal(proposal, base_dir)
        print(f"{proposal.key}: marked accepted.")
        return 0
    if args.reject:
        proposal.status = "rejected"
        save_proposal(proposal, base_dir)
        print(f"{proposal.key}: marked rejected.")
        return 0

    # Default: human-readable side-by-side dump.
    print(f"=== Proposal for {proposal.key} ===")
    print(f"Summary       : {proposal.summary}")
    print(f"Status        : {proposal.status}")
    print(f"Confidence    : {proposal.confidence} ({proposal.confidence_reason})")
    print(f"Model         : {proposal.llm_model}")
    print(f"Tokens (in/out): {proposal.token_usage.input_tokens} / {proposal.token_usage.output_tokens}")
    print(f"Cost          : ${proposal.cost_usd:.4f}")
    print()
    print("--- Original placeholder description ---")
    print(proposal.original_description or "(empty)")
    print()
    print("--- Proposed Acceptance Criteria ---")
    for ac in proposal.proposed_acceptance_criteria or ["(none)"]:
        print(f"  - [ ] {ac}")
    print()
    print("--- Proposed Files / Paths ---")
    for f in proposal.proposed_files or ["(none)"]:
        print(f"  - {f}")
    print()
    print("--- Proposed Prerequisites ---")
    print(render_prerequisites_yaml(proposal.proposed_prerequisites))
    if proposal.notes:
        print()
        print("--- Notes ---")
        for n in proposal.notes:
            print(f"  - {n}")
    print()
    print("Re-run with --accept or --reject to record a decision.")
    return 0


# ── Subcommand: --apply ───────────────────────────────────────────


def cmd_apply(
    args: argparse.Namespace,
    *,
    client: jd.DispatchClient | None = None,
    base_dir: Path = PROPOSALS_DIR,
) -> int:
    proposal = load_proposal(args.key, base_dir)
    if proposal.status not in ("accepted", "applied"):
        print(
            f"Refusing to apply {proposal.key}: status is {proposal.status!r}. "
            "Run --review --accept first."
        )
        return 2
    if proposal.status == "applied" and not args.force:
        print(f"{proposal.key}: already applied (status=applied). Pass --force to redo.")
        return 0

    new_markdown = render_refined_description(proposal)
    if args.dry_run:
        print(f"=== Dry-run: would PUT new description for {proposal.key} ===")
        print(new_markdown)
        print()
        print("Issuetype: Task → Story")
        print(f"Labels  : -{NEEDS_REFINEMENT_LABEL} +{REFINED_LABEL}")
        return 0

    client = client or jd.make_client(args.agent_class)

    update_description(client, proposal.key, new_markdown)
    change_issuetype(client, proposal.key, "Story")
    jd.remove_label(client, proposal.key, NEEDS_REFINEMENT_LABEL)
    jd.add_label(client, proposal.key, REFINED_LABEL)
    operator_id = args.operator or client.bot_email
    jd.add_comment(
        client, proposal.key,
        (
            f"[ai-refined] Description refined by LLM proposal accepted by "
            f"{operator_id} at {datetime.now(timezone.utc).isoformat()}. "
            f"Model: {proposal.llm_model}. "
            f"Token usage: {proposal.token_usage.input_tokens} in / "
            f"{proposal.token_usage.output_tokens} out (~${proposal.cost_usd:.4f})."
        ),
    )

    proposal.status = "applied"
    save_proposal(proposal, base_dir)
    append_audit_log(
        operator=operator_id,
        action="apply",
        key=proposal.key,
        token_usage=proposal.token_usage,
        extra={"confidence": proposal.confidence, "model": proposal.llm_model},
        log_path=base_dir / "audit.log",
    )

    print(
        f"{proposal.key}: applied. issuetype=Story, labels updated, "
        f"audit-log entry written."
    )
    return 0


# ── CLI entry point ───────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Top-level parser. Mode is selected by exactly one of
    ``--propose`` / ``--review`` / ``--apply`` per the OP-781 spec.

    argparse subparsers can't carry names that start with ``--`` (the
    parser treats them as optionals and refuses to bind a positional
    after them), so the spec's ``--<verb>`` form is implemented as a
    mutually-exclusive flag group rather than three subparsers.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agent-class", default=DEFAULT_AGENT_CLASS,
        help="JIRA bot credentials to use (default: subscription-claude).",
    )
    parser.add_argument(
        "--operator", default=None,
        help="Operator id recorded in audit-log + apply comment "
             "(default: bot email from credentials).",
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--propose", action="store_true",
                      help="Generate LLM proposals for every ticket carrying "
                           f"the {NEEDS_REFINEMENT_LABEL!r} label.")
    mode.add_argument("--review", metavar="OP-XXX", default=None,
                      help="Inspect / accept / reject one proposal by key.")
    mode.add_argument("--apply", metavar="OP-XXX", dest="apply_key", default=None,
                      help="Push an accepted proposal to JIRA.")

    # ── --propose flags ──
    parser.add_argument("--limit", type=int, default=None,
                        help="(--propose) cap number of tickets to process.")
    parser.add_argument("--overwrite", action="store_true",
                        help="(--propose) re-propose even if proposal file exists.")
    parser.add_argument("--post-comment", action="store_true",
                        help="(--propose) post a [ai-proposal] comment on each ticket.")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001",
                        help="(--propose) Anthropic model id for proposer call.")

    # ── --review flags ──
    parser.add_argument("--accept", action="store_true",
                        help="(--review) flip proposal status to 'accepted'.")
    parser.add_argument("--reject", action="store_true",
                        help="(--review) flip proposal status to 'rejected'.")

    # ── --apply flags ──
    parser.add_argument("--force", action="store_true",
                        help="(--apply) re-apply an already-applied proposal.")

    # ── shared flags ──
    parser.add_argument("--dry-run", action="store_true",
                        help="--propose: list candidates without calling LLM. "
                             "--apply: print rendered description without PUTting.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Resolve the proposals directory at call-time (not at function-def
    # time) so tests that monkeypatch ``rst.PROPOSALS_DIR`` are honoured.
    base_dir = PROPOSALS_DIR

    if args.propose:
        return cmd_propose(args, base_dir=base_dir)
    if args.review:
        if args.accept and args.reject:
            print("Pick one: --accept or --reject (not both).")
            return 2
        args.key = args.review
        return cmd_review(args, base_dir=base_dir)
    if args.apply_key:
        args.key = args.apply_key
        return cmd_apply(args, base_dir=base_dir)
    parser.error("must pick exactly one of --propose / --review / --apply")
    return 2


if __name__ == "__main__":
    sys.exit(main())
