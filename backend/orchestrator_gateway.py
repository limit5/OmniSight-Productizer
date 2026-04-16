"""O4 (#267) — Orchestrator Gateway Service.

This is the entry point for the enterprise event-driven pipeline:

    Jira User Story ──webhook──► this module
                                   │
                                   ▼
                           LLM (cheap, e.g. Haiku)
                                   │ DAG JSON
                                   ▼
                        dag_validator (cycle / MECE …)
                                   │
                                   ▼
                      build CATC cards (1 per task)
                                   │
                                   ▼
                 impact_scope pairwise intersect check
                                   │
                                   ▼
                       complexity / token budget gate
                                   │                         no
                                   ├──────► require_human_review ─┐
                                   │                              │
                                   ▼                              ▼
                         queue_backend.push                 stored for PM approve
                                   │
                                   ▼
                          stateless workers (O3)

Design notes
------------

* Stays a **pure service layer** — the FastAPI shim lives in
  ``backend/routers/orchestrator.py`` so this module can be unit-tested
  without TestClient.
* LLM is **pluggable** via ``DagSplitter`` callable so tests supply a
  deterministic stub and production can switch Haiku / Opus / whatever
  for the DAG-split vs Merger stages per §一 of the design doc.
* **In-memory session registry** keyed by ``jira_ticket``: sufficient
  for the single-process v1; a follow-up phase can swap in the
  ``dag_storage`` table when cross-host status queries are needed.
* Token budget is a hard gate: any intake whose total LLM tokens
  exceed the budget rejects with ``token_budget_exceeded`` AND publishes
  an SSE ``token_warning/frozen`` alert so operators see it live.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from backend import queue_backend
from backend.catc import TaskCard, globs_overlap
from backend.dag_planner import OrchestratorResponseError, parse_response
from backend.dag_schema import DAG, Task
from backend.dag_validator import ValidationError as DagValError
from backend.dag_validator import validate as dag_validate
from backend.queue_backend import PriorityLevel

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Complexity score > this ⇒ require_human_review=true.
COMPLEXITY_THRESHOLD = 30

# Default total-token budget for a single intake flow.  Can be overridden
# per call or via env ``OMNISIGHT_ORCH_TOKEN_BUDGET``.
DEFAULT_TOKEN_BUDGET = 60_000

# Default LLM "models" — resolved lazily by ``_default_splitter`` via the
# same ``iq_runner.live_ask_fn`` hook used by the DAG router.  Haiku is
# cheap and fast — appropriate for the deterministic JSON-DAG job.  The
# Merger agent path (Opus) is exposed for symmetry but isn't called from
# this module (it runs inside ``backend/merger_agent.py`` — O6).
DEFAULT_SPLIT_MODEL = os.environ.get(
    "OMNISIGHT_ORCH_SPLIT_MODEL",
    "anthropic/claude-haiku-4-5-20251001",
)
DEFAULT_MERGE_MODEL = os.environ.get(
    "OMNISIGHT_ORCH_MERGE_MODEL",
    "anthropic/claude-opus-4-6",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors + enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class IntakeRejectReason(str, Enum):
    cycle_detected = "cycle_detected"
    impact_scope_conflict = "impact_scope_conflict"
    token_budget_exceeded = "token_budget_exceeded"
    schema_invalid = "schema_invalid"
    semantic_invalid = "semantic_invalid"
    llm_unavailable = "llm_unavailable"
    missing_fields = "missing_fields"
    pending_human_review = "pending_human_review"


class IntakeError(RuntimeError):
    """Raised by ``intake()`` when an intake is rejected hard.

    The reason code is part of the HTTP contract — keep values stable.
    """

    def __init__(self, reason: IntakeRejectReason, detail: str,
                 context: dict | None = None) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail
        self.context = context or {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM backend (pluggable)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# A DagSplitter takes the parsed user story text and returns
# (dag_json_str, tokens_used).  The response is parsed by
# ``dag_planner.parse_response`` to get a ``DAG`` object.
DagSplitter = Callable[[str, str], Awaitable[tuple[str, int]]]


async def _default_splitter(jira_ticket: str, story_text: str
                            ) -> tuple[str, int]:
    """Real-LLM DAG splitter.  Prefers Haiku (cheap) per the design doc.

    Returns ``("", 0)`` when no LLM is configured so the caller can
    surface ``llm_unavailable`` cleanly instead of raising.
    """
    try:
        from backend.iq_runner import live_ask_fn
    except Exception as exc:  # pragma: no cover — env-specific
        logger.warning("orchestrator_gateway: cannot import live_ask_fn: %s",
                       exc)
        return ("", 0)

    prompt = _build_split_prompt(jira_ticket, story_text)
    try:
        return await live_ask_fn(DEFAULT_SPLIT_MODEL, prompt)
    except Exception as exc:
        logger.warning("orchestrator_gateway: live_ask_fn failed: %s", exc)
        return ("", 0)


def _build_split_prompt(jira_ticket: str, story_text: str) -> str:
    """Prompt the splitter LLM — keep deterministic and short."""
    return (
        "You are the Lead Orchestrator.  Break the following Jira user "
        "story into a minimal DAG of tasks.\n\n"
        f"Jira ticket: {jira_ticket}\n"
        f"Story:\n{story_text}\n\n"
        "Output ONE JSON object matching this schema (no prose, no fences):\n"
        '{"schema_version": 1, "dag_id": "<slug>", "tasks": ['
        '{"task_id": "...", "description": "...", '
        '"required_tier": "t1|networked|t3", "toolchain": "...", '
        '"inputs": [], "expected_output": "<file-path or git:sha or issue:id>", '
        '"depends_on": []}]}\n'
        "Rules:\n"
        "  * DAG must be acyclic.\n"
        "  * No two tasks share the same expected_output.\n"
        "  * Each task_id is alphanumeric + dash/underscore only.\n"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class PushedCard:
    """One CATC that landed on the queue."""
    task_id: str                    # DAG Task.task_id (planner-provided)
    message_id: str                 # queue_backend message id
    jira_subtask: str               # ``<jira_ticket>-<n>`` subtask key
    priority: PriorityLevel
    allowed: list[str]
    forbidden: list[str]


@dataclass
class IntakeSession:
    """In-memory record of one Jira → DAG → CATCs intake.

    Persisted only for the lifetime of this process; ``status_snapshot``
    is what ``GET /orchestrator/status`` returns.
    """
    jira_ticket: str
    story_text: str
    created_at: float
    tenant_id: str | None
    dag: DAG | None = None
    cards: list[PushedCard] = field(default_factory=list)
    tokens_used: int = 0
    token_budget: int = DEFAULT_TOKEN_BUDGET
    complexity_score: int = 0
    require_human_review: bool = False
    approved_by: str | None = None
    state: str = "pending"          # pending|queued|approved|rejected|replanned
    reject_reason: str | None = None
    replan_count: int = 0
    last_updated_at: float = 0.0

    def status_snapshot(self) -> dict[str, Any]:
        """Build the GET /status response payload."""
        cards_payload: list[dict[str, Any]] = []
        for c in self.cards:
            msg = queue_backend.get(c.message_id)
            state = msg.state.value if msg else "Done_or_DLQ"
            delivery = msg.delivery_count if msg else 0
            cards_payload.append({
                "task_id": c.task_id,
                "jira_subtask": c.jira_subtask,
                "message_id": c.message_id,
                "priority": c.priority.value,
                "queue_state": state,
                "delivery_count": delivery,
                "allowed": list(c.allowed),
                "forbidden": list(c.forbidden),
                "gerrit": _gerrit_status_stub(c.task_id),
            })
        return {
            "jira_ticket": self.jira_ticket,
            "state": self.state,
            "reject_reason": self.reject_reason,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "last_updated_at": self.last_updated_at or self.created_at,
            "replan_count": self.replan_count,
            "approved_by": self.approved_by,
            "require_human_review": self.require_human_review,
            "complexity_score": self.complexity_score,
            "tokens_used": self.tokens_used,
            "token_budget": self.token_budget,
            "dag": (self.dag.model_dump() if self.dag else None),
            "cards": cards_payload,
        }


# Registry — process-local; tests call ``reset_registry_for_tests()``.
_sessions: dict[str, IntakeSession] = {}


def reset_registry_for_tests() -> None:
    _sessions.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DAG → CATC conversion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_FILE_PATH_HINT = re.compile(r"^[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,12}$")


def _derive_allowed_globs(task: Task) -> list[str]:
    """Derive a conservative ``impact_scope.allowed`` glob set from a
    DAG task.  Prefers the task's own ``expected_output`` when it looks
    like a file path; expands the directory so the worker can touch
    siblings on that component.  Falls back to a single literal path
    when only the exact file should be mutable.

    We purposely do NOT include upstream-task outputs here — those are
    reads, not writes, and the CATC contract says ``allowed`` is the
    write scope.
    """
    out = task.expected_output.strip()
    if _FILE_PATH_HINT.match(out):
        # Widen to the directory — a real task typically touches more
        # than one file within the same module.
        parent = out.rsplit("/", 1)[0] if "/" in out else ""
        if parent:
            return [f"{parent}/**"]
        return [out]
    # Non-file outputs (git:<sha>, issue:<id>) — fall back to task_id-
    # derived scratch directory.  Worker is responsible for respecting it.
    slug = re.sub(r"[^A-Za-z0-9_.\-]", "_", task.task_id)
    return [f"artifacts/{slug}/**"]


def _subtask_key(jira_ticket: str, index: int) -> str:
    """Construct a deterministic JIRA-style subtask key.  Kept inside
    the 64-char CATC limit and matches ``[A-Z][A-Z0-9_]*-\\d+`` so the
    downstream CATC validator accepts it."""
    m = re.match(r"^([A-Z][A-Z0-9_]*)-(\d+)$", jira_ticket)
    if not m:
        # Defensive — caller should have rejected at intake_from_webhook
        return f"{jira_ticket}-{index + 1}"
    prefix, base = m.group(1), m.group(2)
    return f"{prefix}-{int(base) * 1000 + index + 1}"


def build_catcs_from_dag(jira_ticket: str, dag: DAG,
                         *, acceptance_criteria: str = "",
                         domain_context: str = "",
                         forbidden_globs: list[str] | None = None,
                         ) -> list[TaskCard]:
    """Produce one TaskCard per DAG node.

    ``forbidden_globs`` (optional) is applied wholesale to every card
    — useful when the operator wants the entire intake walled off from
    e.g. ``test_assets/**`` (read-only ground truth per CLAUDE.md L1).
    """
    forbidden = list(forbidden_globs or [])
    cards: list[TaskCard] = []
    for i, task in enumerate(dag.tasks):
        allowed = _derive_allowed_globs(task)
        ac = acceptance_criteria.strip() or task.description
        card = TaskCard.from_dict({
            "jira_ticket": _subtask_key(jira_ticket, i),
            "acceptance_criteria": ac,
            "navigation": {
                "entry_point": task.expected_output if _FILE_PATH_HINT.match(
                    task.expected_output) else f"#{task.task_id}",
                "impact_scope": {
                    "allowed": allowed,
                    "forbidden": forbidden,
                },
            },
            "domain_context": domain_context or f"task={task.task_id}",
            "handoff_protocol": [
                f"Run toolchain: {task.toolchain}",
                "Commit changes and push to Gerrit refs/for/main",
            ],
        })
        cards.append(card)
    return cards


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  impact_scope pairwise intersect check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScopeConflict:
    card_a_index: int
    card_b_index: int
    a_task_id: str
    b_task_id: str
    overlap: tuple[str, str]    # the two globs that conflicted

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_a": self.a_task_id,
            "card_b": self.b_task_id,
            "glob_a": self.overlap[0],
            "glob_b": self.overlap[1],
        }


def check_impact_scope_intersect(cards: list[TaskCard], dag: DAG | None = None,
                                 ) -> list[ScopeConflict]:
    """Return all pairwise ``allowed`` glob overlaps.

    Two CATC cards in the same sprint may both legitimately touch the
    same scope ONLY if one depends on the other (serial execution is
    fine — they can't conflict at runtime because the dist-lock
    serialises them).  If ``dag`` is supplied, we skip pairs that are
    transitively connected in the dependency graph; otherwise every
    overlap is reported.
    """
    # Pre-compute dep closure when a DAG is available.
    connected: set[tuple[str, str]] = set()
    if dag is not None:
        # Build adjacency (forward + reverse) and BFS both directions
        # to flatten ancestor+descendant into one "serial"-ness set.
        children: dict[str, list[str]] = {t.task_id: [] for t in dag.tasks}
        parents: dict[str, list[str]] = {t.task_id: list(t.depends_on)
                                         for t in dag.tasks}
        for t in dag.tasks:
            for dep in t.depends_on:
                children.setdefault(dep, []).append(t.task_id)

        def _reachable(start: str, adj: dict[str, list[str]]) -> set[str]:
            seen: set[str] = set()
            stack = list(adj.get(start, []))
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                stack.extend(adj.get(n, []))
            return seen

        for t in dag.tasks:
            for d in _reachable(t.task_id, children):
                connected.add((t.task_id, d))
                connected.add((d, t.task_id))
            for a in _reachable(t.task_id, parents):
                connected.add((t.task_id, a))
                connected.add((a, t.task_id))

    conflicts: list[ScopeConflict] = []
    # We pull the task_id from the DAG by index (cards were built in
    # DAG order by ``build_catcs_from_dag``).
    task_ids = [t.task_id for t in (dag.tasks if dag else [])]
    if not task_ids or len(task_ids) != len(cards):
        task_ids = [c.jira_ticket for c in cards]

    for i in range(len(cards)):
        a = cards[i]
        ai = task_ids[i]
        for j in range(i + 1, len(cards)):
            b = cards[j]
            bi = task_ids[j]
            if (ai, bi) in connected:
                continue
            overlap = _first_glob_overlap(
                a.navigation.impact_scope.allowed,
                b.navigation.impact_scope.allowed,
            )
            if overlap is None:
                continue
            conflicts.append(ScopeConflict(
                card_a_index=i, card_b_index=j,
                a_task_id=ai, b_task_id=bi,
                overlap=overlap,
            ))
    return conflicts


def _first_glob_overlap(left: list[str], right: list[str]
                        ) -> tuple[str, str] | None:
    for g1 in left:
        for g2 in right:
            if globs_overlap(g1, g2):
                return (g1, g2)
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Complexity scoring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def complexity_score(dag: DAG) -> int:
    """Back-of-envelope DAG complexity score.

    Combines node count, edge count, and max fan-in/out so that a
    pathologically deep or branched plan trips ``require_human_review``
    even when it stays under the 10-task "simple" bucket.

    Score breakdown:
      * 2 × len(tasks)
      * 1 × len(edges)
      * 3 × max(fan_in, fan_out)
      * +5 if any task sits on a path of length ≥ 4
    """
    n = len(dag.tasks)
    edges = 0
    fan_in: dict[str, int] = {t.task_id: 0 for t in dag.tasks}
    fan_out: dict[str, int] = {t.task_id: 0 for t in dag.tasks}
    for t in dag.tasks:
        edges += len(t.depends_on)
        fan_out[t.task_id] = len(t.depends_on)  # reversed below? see note
    # We want "fan_in" = how many tasks depend ON t; "fan_out" = how
    # many deps t itself has.  Re-compute fan_in by scanning depends_on.
    fan_in = {t.task_id: 0 for t in dag.tasks}
    for t in dag.tasks:
        for d in t.depends_on:
            if d in fan_in:
                fan_in[d] += 1
    max_fan = max([*fan_in.values(), *fan_out.values(), 0])

    # Depth via simple topological longest-path.
    adj: dict[str, list[str]] = {t.task_id: [] for t in dag.tasks}
    for t in dag.tasks:
        for d in t.depends_on:
            adj.setdefault(d, []).append(t.task_id)

    depth_by_id: dict[str, int] = {}

    def _depth(node: str, stack: set[str]) -> int:
        if node in depth_by_id:
            return depth_by_id[node]
        if node in stack:
            return 0    # cycle — validator will reject later
        stack.add(node)
        best = 0
        for nxt in adj.get(node, []):
            best = max(best, 1 + _depth(nxt, stack))
        stack.discard(node)
        depth_by_id[node] = best
        return best

    deepest = 0
    for t in dag.tasks:
        deepest = max(deepest, _depth(t.task_id, set()))

    score = 2 * n + edges + 3 * max_fan + (5 if deepest >= 4 else 0)
    return int(score)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token budget gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _configured_token_budget(explicit: int | None) -> int:
    if explicit is not None and explicit > 0:
        return int(explicit)
    env = os.environ.get("OMNISIGHT_ORCH_TOKEN_BUDGET", "").strip()
    if env:
        try:
            v = int(env)
            if v > 0:
                return v
        except ValueError:
            logger.warning("orchestrator_gateway: bad token budget env %r", env)
    return DEFAULT_TOKEN_BUDGET


def _emit_token_alert(reason: str, usage: int, budget: int,
                      jira_ticket: str) -> None:
    """Best-effort SSE alert.  Never raises — the intake itself is what
    matters; visibility into it is a nice-to-have."""
    try:
        from backend.events import emit_token_warning
        emit_token_warning(
            level="frozen",
            message=f"orchestrator intake rejected ({reason}): {jira_ticket}",
            usage=float(usage),
            budget=float(budget),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("SSE token_warning emit failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def parse_jira_webhook(body: dict[str, Any]) -> tuple[str, str]:
    """Extract ``(jira_ticket, story_text)`` from a Jira webhook body.

    Jira v3 shape::

        {"issue": {"key": "PROJ-402",
                   "fields": {"summary": "...", "description": "..."}}}

    Falls back to flat keys so test payloads can skip the nesting.
    """
    issue = body.get("issue") or {}
    key = issue.get("key") or body.get("jira_ticket") or body.get("key") or ""
    fields = issue.get("fields") or {}
    summary = fields.get("summary") or body.get("summary") or ""
    description = fields.get("description") or body.get("description") or ""
    text = "\n\n".join(s for s in (summary, _stringify(description)) if s)
    return (str(key).strip(), text.strip())


def _stringify(value: Any) -> str:
    """Jira ADF descriptions arrive as nested dicts — best-effort flatten."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_stringify(v) for v in value if v)
    if isinstance(value, dict):
        t = value.get("text")
        if isinstance(t, str):
            return t
        inner = value.get("content") or value.get("children") or []
        return _stringify(inner)
    return str(value)


@dataclass
class IntakeOutcome:
    """What ``intake()`` returns on success (rejected intakes raise)."""
    jira_ticket: str
    dag: DAG
    cards: list[PushedCard]
    tokens_used: int
    token_budget: int
    complexity_score: int
    require_human_review: bool
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "jira_ticket": self.jira_ticket,
            "dag_id": self.dag.dag_id,
            "n_tasks": len(self.dag.tasks),
            "n_cards_queued": len(self.cards),
            "tokens_used": self.tokens_used,
            "token_budget": self.token_budget,
            "complexity_score": self.complexity_score,
            "require_human_review": self.require_human_review,
            "state": self.state,
            "cards": [
                {
                    "task_id": c.task_id,
                    "jira_subtask": c.jira_subtask,
                    "message_id": c.message_id,
                    "priority": c.priority.value,
                } for c in self.cards
            ],
        }


async def intake(
    webhook_body: dict[str, Any],
    *,
    splitter: DagSplitter | None = None,
    token_budget: int | None = None,
    priority: PriorityLevel = PriorityLevel.P2,
    forbidden_globs: list[str] | None = None,
    tenant_id: str | None = None,
) -> IntakeOutcome:
    """Main entry point — accepts a parsed Jira webhook and drives the
    full pipeline.  Raises ``IntakeError`` on any rejection.

    Idempotency: re-calling ``intake`` for a ticket that already has a
    session replaces it (state → replanned) — this is the
    ``POST /intake`` contract and matches how Jira resends webhooks.
    """
    jira_ticket, story = parse_jira_webhook(webhook_body)
    if not jira_ticket or not story:
        raise IntakeError(
            IntakeRejectReason.missing_fields,
            "webhook must carry both Jira key and summary/description",
            {"had_key": bool(jira_ticket), "had_story": bool(story)},
        )
    if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", jira_ticket):
        raise IntakeError(
            IntakeRejectReason.missing_fields,
            f"jira_ticket {jira_ticket!r} does not match PROJ-123 format",
        )

    budget = _configured_token_budget(token_budget)
    session = IntakeSession(
        jira_ticket=jira_ticket,
        story_text=story,
        created_at=time.time(),
        tenant_id=tenant_id,
        token_budget=budget,
    )
    existing = _sessions.get(jira_ticket)
    if existing is not None:
        session.replan_count = existing.replan_count + 1
    _sessions[jira_ticket] = session

    split = splitter or _default_splitter
    try:
        raw, tokens = await split(jira_ticket, story)
    except Exception as exc:
        session.state = "rejected"
        session.reject_reason = IntakeRejectReason.llm_unavailable.value
        session.last_updated_at = time.time()
        raise IntakeError(
            IntakeRejectReason.llm_unavailable,
            f"DAG splitter raised: {exc}",
        ) from exc

    session.tokens_used = int(tokens or 0)
    session.last_updated_at = time.time()
    if session.tokens_used > budget:
        session.state = "rejected"
        session.reject_reason = IntakeRejectReason.token_budget_exceeded.value
        _emit_token_alert("token_budget_exceeded",
                          session.tokens_used, budget, jira_ticket)
        raise IntakeError(
            IntakeRejectReason.token_budget_exceeded,
            f"intake consumed {session.tokens_used} tokens > budget {budget}",
            {"tokens_used": session.tokens_used, "token_budget": budget},
        )

    if not raw:
        session.state = "rejected"
        session.reject_reason = IntakeRejectReason.llm_unavailable.value
        raise IntakeError(
            IntakeRejectReason.llm_unavailable,
            "DAG splitter returned empty response (LLM unavailable?)",
        )

    # Parse the LLM response → DAG.
    try:
        dag = parse_response(raw)
    except OrchestratorResponseError as exc:
        session.state = "rejected"
        session.reject_reason = IntakeRejectReason.schema_invalid.value
        raise IntakeError(
            IntakeRejectReason.schema_invalid,
            f"could not parse DAG from LLM response: {exc}",
            {"raw_head": raw[:300]},
        ) from exc

    # Semantic validation — cycles, MECE, tier rules, …
    v = dag_validate(dag)
    if not v.ok:
        session.dag = dag
        session.state = "rejected"
        # Distinguish cycle (#1 named failure mode in the design doc)
        # from the generic semantic bucket so the client gets a useful
        # error surface.
        rule_set = v.by_rule
        if rule_set.get("cycle"):
            reason = IntakeRejectReason.cycle_detected
        else:
            reason = IntakeRejectReason.semantic_invalid
        session.reject_reason = reason.value
        raise IntakeError(
            reason,
            f"DAG semantic validation failed: {v.summary()}",
            {"errors": [_err_to_dict(e) for e in v.errors]},
        )

    # Build CATC cards.
    cards = build_catcs_from_dag(
        jira_ticket, dag,
        acceptance_criteria=story,
        forbidden_globs=forbidden_globs,
    )

    # impact_scope pairwise intersect.
    conflicts = check_impact_scope_intersect(cards, dag=dag)
    if conflicts:
        session.dag = dag
        session.state = "rejected"
        session.reject_reason = IntakeRejectReason.impact_scope_conflict.value
        raise IntakeError(
            IntakeRejectReason.impact_scope_conflict,
            f"{len(conflicts)} pairwise impact_scope conflict(s) detected",
            {"conflicts": [c.to_dict() for c in conflicts]},
        )

    # Complexity gate.
    score = complexity_score(dag)
    session.complexity_score = score
    session.require_human_review = score > COMPLEXITY_THRESHOLD
    session.dag = dag

    if session.require_human_review:
        session.state = "pending"
        session.reject_reason = IntakeRejectReason.pending_human_review.value
        session.last_updated_at = time.time()
        _publish_intake_event(session, event="pending_human_review")
        # Not a hard reject — we return with state=pending so the caller
        # knows to go to /replan after PM approves.  The exception path
        # is reserved for *invalid* intakes.
        return IntakeOutcome(
            jira_ticket=jira_ticket,
            dag=dag,
            cards=[],
            tokens_used=session.tokens_used,
            token_budget=budget,
            complexity_score=score,
            require_human_review=True,
            state="pending",
        )

    # Push to queue.
    pushed = _push_cards_to_queue(dag, cards, priority)
    session.cards = pushed
    session.state = "queued"
    session.reject_reason = None
    session.last_updated_at = time.time()
    _publish_intake_event(session, event="queued")

    return IntakeOutcome(
        jira_ticket=jira_ticket,
        dag=dag,
        cards=pushed,
        tokens_used=session.tokens_used,
        token_budget=budget,
        complexity_score=score,
        require_human_review=False,
        state="queued",
    )


async def replan(
    jira_ticket: str,
    *,
    approver: str,
    new_story: str | None = None,
    splitter: DagSplitter | None = None,
    token_budget: int | None = None,
    priority: PriorityLevel = PriorityLevel.P2,
    forbidden_globs: list[str] | None = None,
    override_human_review: bool = False,
) -> IntakeOutcome:
    """Manual replan path — PM approves a complex intake, we rebuild the
    DAG (optionally with a new story) and push CATCs.

    ``override_human_review=True`` accepts the current DAG as-is even
    though complexity > threshold (this is the "PM said yes, ship it"
    path).  Re-splitting is triggered when ``new_story`` is supplied.
    """
    session = _sessions.get(jira_ticket)
    if session is None:
        raise IntakeError(
            IntakeRejectReason.missing_fields,
            f"no intake session for {jira_ticket!r}",
        )
    if not approver or not approver.strip():
        raise IntakeError(
            IntakeRejectReason.missing_fields,
            "replan requires a non-empty approver identity",
        )

    session.replan_count += 1
    session.approved_by = approver.strip()
    session.last_updated_at = time.time()

    # If we already have a DAG and the PM is overriding complexity,
    # short-circuit to the queue push.
    if (
        override_human_review
        and session.dag is not None
        and new_story is None
        and not session.cards
    ):
        cards = build_catcs_from_dag(
            jira_ticket, session.dag,
            acceptance_criteria=session.story_text,
            forbidden_globs=forbidden_globs,
        )
        conflicts = check_impact_scope_intersect(cards, dag=session.dag)
        if conflicts:
            session.state = "rejected"
            session.reject_reason = (
                IntakeRejectReason.impact_scope_conflict.value
            )
            raise IntakeError(
                IntakeRejectReason.impact_scope_conflict,
                "replan detected impact_scope conflict(s); split into "
                "separate Jira stories",
                {"conflicts": [c.to_dict() for c in conflicts]},
            )
        pushed = _push_cards_to_queue(session.dag, cards, priority)
        session.cards = pushed
        session.state = "queued"
        session.reject_reason = None
        session.last_updated_at = time.time()
        _publish_intake_event(session, event="replanned_override")
        return IntakeOutcome(
            jira_ticket=jira_ticket,
            dag=session.dag,
            cards=pushed,
            tokens_used=session.tokens_used,
            token_budget=session.token_budget,
            complexity_score=session.complexity_score,
            require_human_review=False,
            state="queued",
        )

    # Otherwise, re-drive the LLM split (with new story if supplied).
    effective_story = (new_story or session.story_text).strip()
    webhook_body = {
        "issue": {"key": jira_ticket,
                  "fields": {"summary": effective_story}}
    }
    return await intake(
        webhook_body,
        splitter=splitter,
        token_budget=(token_budget or session.token_budget),
        priority=priority,
        forbidden_globs=forbidden_globs,
        tenant_id=session.tenant_id,
    )


def get_status(jira_ticket: str) -> dict[str, Any]:
    """Return the full status snapshot for a Jira intake, or ``{}`` if
    we've never seen the ticket in this process."""
    sess = _sessions.get(jira_ticket)
    if sess is None:
        return {}
    return sess.status_snapshot()


def list_sessions() -> list[dict[str, Any]]:
    """Operator surface — every session in this process.  Status UI
    paginates on top of this; we don't do it here."""
    return [s.status_snapshot() for s in _sessions.values()]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _push_cards_to_queue(dag: DAG, cards: list[TaskCard],
                         priority: PriorityLevel) -> list[PushedCard]:
    pushed: list[PushedCard] = []
    for task, card in zip(dag.tasks, cards):
        try:
            msg_id = queue_backend.push(card, priority)
        except Exception as exc:
            logger.error("queue push failed for %s: %s", task.task_id, exc)
            raise IntakeError(
                IntakeRejectReason.semantic_invalid,
                f"queue push failed for {task.task_id}: {exc}",
            ) from exc
        pushed.append(PushedCard(
            task_id=task.task_id,
            message_id=msg_id,
            jira_subtask=card.jira_ticket,
            priority=priority,
            allowed=list(card.navigation.impact_scope.allowed),
            forbidden=list(card.navigation.impact_scope.forbidden),
        ))
    return pushed


def _err_to_dict(e: DagValError) -> dict[str, Any]:
    return {"rule": e.rule, "task_id": e.task_id, "message": e.message}


def _gerrit_status_stub(task_id: str) -> dict[str, Any]:
    """Placeholder for the Gerrit patchset/review status.  The real
    lookup lands in O6 (Merger Agent) + O7 (submit-rule arbiter); this
    module exposes the shape so the ``GET /status`` response is stable
    across versions.

    Shape matches the design doc:
      * ``patchset`` — commit sha when the worker has pushed
      * ``ai_vote`` / ``human_vote`` — +2 votes from Merger + human
      * ``both_plus_2`` — True when submit-rule is satisfied
    """
    return {
        "patchset": None,
        "ai_vote": 0,
        "human_vote": 0,
        "both_plus_2": False,
    }


def _publish_intake_event(session: IntakeSession, *, event: str) -> None:
    """Best-effort SSE emit.  Uses the generic ``invoke`` event stream
    so existing UI subscribers pick it up without a schema change."""
    try:
        from backend.events import emit_invoke
        emit_invoke(
            f"orchestrator_intake:{event}",
            f"{session.jira_ticket} → {event}",
            jira_ticket=session.jira_ticket,
            state=session.state,
            tokens_used=session.tokens_used,
            token_budget=session.token_budget,
            complexity_score=session.complexity_score,
            require_human_review=session.require_human_review,
            n_cards=len(session.cards),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("intake SSE emit failed: %s", exc)


__all__ = [
    "COMPLEXITY_THRESHOLD",
    "DEFAULT_TOKEN_BUDGET",
    "DEFAULT_SPLIT_MODEL",
    "DEFAULT_MERGE_MODEL",
    "DagSplitter",
    "IntakeError",
    "IntakeOutcome",
    "IntakeRejectReason",
    "IntakeSession",
    "PushedCard",
    "ScopeConflict",
    "build_catcs_from_dag",
    "check_impact_scope_intersect",
    "complexity_score",
    "get_status",
    "intake",
    "list_sessions",
    "parse_jira_webhook",
    "replan",
    "reset_registry_for_tests",
]
