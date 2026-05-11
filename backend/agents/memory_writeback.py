"""OP-906 (F8) — Memory write-back protocol at task completion.

On runner pickup completion (success or failure), this module fans out
to three backing stores in parallel with per-store timeouts:

* **Memory Tool fleet dir (C1)** — append a lesson entry when the outcome
  is lesson-worthy (success path); the caller posts a
  ``[memory-writeback] lesson=<L-id>`` JIRA comment.
* **runner_incidents (C2)** — insert a row with classified
  ``failure_class`` + summary + ``mutex_label`` on failure; on success
  with ``record_success_outcome=True`` write a positive-feedback row
  (``failure_class=SUCCESS_OUTCOME``).
* **Cognee KG (C3)** — fire-and-forget tickle so the ticket + lesson are
  re-indexed on next ingest pass.

Per AC #3 the writeback is fail-open: a backing store outage logs the
failed-store name and continues; the runner never blocks on write-back.
Per AC #2 the same ``(ticket_key, attempt_n)`` is idempotent — a
re-submission returns the cached result and logs a debug line.

The orchestrator does not call into JIRA directly. The runner glue
hook in ``auto-runner-jira.py`` is responsible for posting the
``[memory-writeback] lesson=<L-id>`` JIRA comment using
:func:`backend.agents.jira_dispatch.add_comment` after the writeback
finishes; that keeps the JIRA-dispatch boundary in one place.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from backend.agents.failure_class import (
    FailureClass,
    classify_from_traceback,
)
from backend.agents.incident_recorder import (
    RunnerIncidentRecord,
    record_runner_incident,
)
from backend.agents.memory_tool_handler import (
    MEMORY_PATH_PREFIX,
    MemoryToolError,
    MemoryToolHandler,
)

log = logging.getLogger(__name__)

__all__ = [
    "IdempotencyCollision",
    "LessonClassificationFailed",
    "MemoryWriteback",
    "MemoryWritebackPartial",
    "STORE_COGNEE",
    "STORE_INCIDENTS",
    "STORE_MEMORY_TOOL",
    "SUCCESS_OUTCOME_FAILURE_CLASS",
    "WritebackRequest",
    "WritebackResult",
    "classify_lesson",
    "get_writebacks_for",
    "reset_for_tests",
]


# ── Constants ───────────────────────────────────────────────────────

STORE_MEMORY_TOOL = "memory_tool"
STORE_INCIDENTS = "runner_incidents"
STORE_COGNEE = "cognee"

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
_VALID_OUTCOMES: frozenset[str] = frozenset({OUTCOME_SUCCESS, OUTCOME_FAILURE})

# The "positive feedback" success marker. The failure_class enum is a
# closed set; for success rows we coerce to FailureClass.OTHER and tag
# the summary with this prefix so dashboards can split success vs real
# failures without an enum migration.
SUCCESS_OUTCOME_FAILURE_CLASS = "SUCCESS_OUTCOME"
_SUCCESS_SUMMARY_PREFIX = f"[{SUCCESS_OUTCOME_FAILURE_CLASS}]"

# Per-store budgets (seconds) per the F8 state-transition spec.
BUDGET_MEMORY_TOOL_S = 2.0
BUDGET_INCIDENTS_S = 1.0
BUDGET_COGNEE_S = 2.0

# Disk-queue location for incident-insert retries (AC recovery section).
# Each line is one JSON-encoded WritebackRequest payload. Hourly retry
# is the operator's cron job; this module is the producer side only.
DEFAULT_INCIDENT_QUEUE_DIR = Path("/var/omnisight/memory/incident_queue")
INCIDENT_QUEUE_DIR_ENV = "OMNISIGHT_INCIDENT_QUEUE_DIR"

# Lesson classifier — minimal heuristic. The runner can override the
# default by passing ``lesson_id``/``lesson_content`` directly on the
# WritebackRequest. The heuristic exists so unattended completions still
# capture an entry when the runner did not pre-classify.
_LESSON_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\blesson(?:s)?[- ]?learned\b", re.I),
    re.compile(r"\bretrospective\b", re.I),
    re.compile(r"\bpost[- ]?mortem\b", re.I),
)


# ── Errors (per AC error catalog) ───────────────────────────────────


class MemoryWritebackPartial(RuntimeError):
    """One or more backing stores failed; runner main loop continues.

    The caller logs + ignores; do NOT re-raise into the runner main
    loop. Use ``.stores_failed`` to surface to the operator dashboard.
    """

    def __init__(self, stores_failed: Iterable[str]) -> None:
        self.stores_failed: tuple[str, ...] = tuple(sorted(set(stores_failed)))
        super().__init__(
            f"memory writeback partial: stores_failed={list(self.stores_failed)}"
        )


class IdempotencyCollision(RuntimeError):
    """Duplicate ``(ticket_key, attempt_n)`` — caller should treat as no-op.

    Surfaced through :class:`WritebackResult.idempotent_skip=True`
    rather than raised by the public entrypoint; reserved for
    explicit ``write(strict=True)`` callers.
    """


class LessonClassificationFailed(RuntimeError):
    """Heuristic classifier could not derive a lesson id from inputs.

    Caller falls back to no-classification (no memory_tool append) and
    logs. Never blocks the rest of the write-back.
    """


# ── Value objects ───────────────────────────────────────────────────


@dataclass(frozen=True)
class WritebackRequest:
    """One write-back invocation describing a runner completion.

    The runner glue in ``auto-runner-jira.py`` builds this from the
    pickup metric + JIRA snapshot + CLI exit context.
    """

    ticket_key: str
    attempt_n: int
    outcome: str  # OUTCOME_SUCCESS | OUTCOME_FAILURE
    summary: str = ""
    failure_class: str | None = None
    raw_traceback: str = ""
    mutex_label: str | None = None
    area: str | None = None
    runner_class: str = "unknown"

    # Optional pre-classified lesson. When omitted on success outcomes,
    # ``classify_lesson`` derives one from ``summary``/``raw_traceback``.
    lesson_id: str | None = None
    lesson_content: str | None = None

    # If True on success outcomes, ALSO emit a positive-feedback row to
    # runner_incidents. Default False — the C2 table is failure-focused
    # so most operators only want negative samples there.
    record_success_outcome: bool = False

    def __post_init__(self) -> None:  # noqa: D401 — validation
        if self.outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"WritebackRequest.outcome must be one of {sorted(_VALID_OUTCOMES)}, "
                f"got {self.outcome!r}"
            )
        if not isinstance(self.attempt_n, int) or self.attempt_n < 0:
            raise ValueError(
                f"WritebackRequest.attempt_n must be a non-negative int, "
                f"got {self.attempt_n!r}"
            )

    @property
    def idempotency_key(self) -> tuple[str, int]:
        return (self.ticket_key, self.attempt_n)


@dataclass(frozen=True)
class WritebackResult:
    """Aggregate outcome of a single write-back fan-out."""

    ticket_key: str
    attempt_n: int
    outcome: str
    memory_tool_lesson_id: str | None
    incident_id: str | None
    cognee_tickled: bool
    stores_failed: tuple[str, ...] = ()
    idempotent_skip: bool = False
    finished_at: float = field(default_factory=time.time)

    @property
    def all_ok(self) -> bool:
        return not self.stores_failed


# Type aliases for injectable collaborators.
CogneeEmitter = Callable[[str], None]
"""``(ticket_key) -> None`` — fire-and-forget Cognee re-index trigger."""


# ── Module state (in-memory dedupe + dashboard buffer) ──────────────


_state_lock = threading.Lock()
_seen_keys: set[tuple[str, int]] = set()
_results_by_ticket: dict[str, list[WritebackResult]] = {}


def reset_for_tests() -> None:
    """Clear in-memory dedupe + dashboard buffer. Test-only seam."""
    with _state_lock:
        _seen_keys.clear()
        _results_by_ticket.clear()


def get_writebacks_for(ticket_key: str) -> list[WritebackResult]:
    """AC #4: per-ticket memory writebacks for the dashboard stub (F15).

    Returns a snapshot of completed writebacks for ``ticket_key`` in
    submission order. The F15 dashboard reads this through the
    backend.api layer once it ships; for now the runner uses it for
    operator-facing log lines.
    """
    with _state_lock:
        return list(_results_by_ticket.get(ticket_key, ()))


# ── Lesson classification ───────────────────────────────────────────


def classify_lesson(request: WritebackRequest) -> tuple[str, str] | None:
    """Derive ``(lesson_id, content)`` from a request, or ``None``.

    Honours ``request.lesson_id``/``request.lesson_content`` when both
    are supplied. Otherwise tries a narrow heuristic on ``summary`` and
    ``raw_traceback`` (lessons-learned keyword); when neither hits, the
    caller treats the writeback as "no-lesson, success-only".

    Raises :class:`LessonClassificationFailed` only when ``lesson_id``
    is supplied without ``lesson_content`` (caller-side bug) — the
    no-match heuristic path returns ``None`` (per error catalog
    fall-back-to-no-classification).
    """
    if request.lesson_id and request.lesson_content:
        return request.lesson_id, request.lesson_content
    if request.lesson_id and not request.lesson_content:
        raise LessonClassificationFailed(
            f"lesson_id={request.lesson_id!r} supplied without lesson_content"
        )
    if request.outcome != OUTCOME_SUCCESS:
        return None
    haystack = f"{request.summary}\n{request.raw_traceback}"
    if not any(pat.search(haystack) for pat in _LESSON_KEY_PATTERNS):
        return None
    lesson_id = f"L-{request.ticket_key}-attempt{request.attempt_n}"
    content = _format_auto_lesson(request, lesson_id)
    return lesson_id, content


def _format_auto_lesson(request: WritebackRequest, lesson_id: str) -> str:
    return (
        f"---\nid: {lesson_id}\nticket: {request.ticket_key}\n"
        f"attempt: {request.attempt_n}\noutcome: {request.outcome}\n"
        f"runner: {request.runner_class}\n---\n\n"
        f"# {lesson_id}\n\n{request.summary.strip()}\n"
    )


# ── Orchestrator ────────────────────────────────────────────────────


class MemoryWriteback:
    """Fan-out write-back to the three backing stores.

    Collaborators are injected so tests can exercise the parallel
    aggregation, per-store budget, and degradation contracts without
    booting Postgres / a Memory Tool storage root / Cognee.
    """

    def __init__(
        self,
        *,
        memory_tool: MemoryToolHandler | None = None,
        cognee_emitter: CogneeEmitter | None = None,
        incident_writer: Callable[..., RunnerIncidentRecord] | None = None,
        incident_queue_dir: Path | None = None,
        memory_tool_budget_s: float = BUDGET_MEMORY_TOOL_S,
        incidents_budget_s: float = BUDGET_INCIDENTS_S,
        cognee_budget_s: float = BUDGET_COGNEE_S,
    ) -> None:
        self._memory_tool = memory_tool
        self._cognee_emitter = cognee_emitter or _default_cognee_emitter
        self._incident_writer = incident_writer or record_runner_incident
        self._memory_tool_budget = memory_tool_budget_s
        self._incidents_budget = incidents_budget_s
        self._cognee_budget = cognee_budget_s
        env_dir = os.environ.get(INCIDENT_QUEUE_DIR_ENV)
        self._incident_queue_dir = (
            incident_queue_dir
            or (Path(env_dir) if env_dir else DEFAULT_INCIDENT_QUEUE_DIR)
        )

    # — Public API —

    def write(self, request: WritebackRequest) -> WritebackResult:
        """Run the three-store fan-out for one runner completion.

        Idempotent on ``(ticket_key, attempt_n)`` (AC #2). Never raises
        on individual store failures (AC #3) — the failed store name
        lands in ``WritebackResult.stores_failed`` and the runner main
        loop continues.
        """
        cached = self._check_idempotent(request)
        if cached is not None:
            log.debug(
                "memory_writeback.idempotent_skip ticket=%s attempt=%s",
                request.ticket_key,
                request.attempt_n,
            )
            return cached

        # Parallel fan-out with per-store budgets. Daemon threads so a
        # slow Cognee tickle (the fire-and-forget store) does not block
        # this method past its budget — the thread keeps running in the
        # background but the runner main loop continues immediately.
        slots: dict[str, Any] = {}

        def run_memory() -> None:
            try:
                slots[STORE_MEMORY_TOOL] = self._do_memory_tool(request)
            except Exception as exc:  # noqa: BLE001 — boundary
                slots[f"{STORE_MEMORY_TOOL}:error"] = exc

        def run_incident() -> None:
            try:
                slots[STORE_INCIDENTS] = self._do_incident(request)
            except Exception as exc:  # noqa: BLE001 — boundary
                slots[f"{STORE_INCIDENTS}:error"] = exc

        def run_cognee() -> None:
            try:
                slots[STORE_COGNEE] = self._do_cognee(request)
            except Exception as exc:  # noqa: BLE001 — boundary
                slots[f"{STORE_COGNEE}:error"] = exc

        t_memory = threading.Thread(
            target=run_memory, name="mwb-memory", daemon=True
        )
        t_incident = threading.Thread(
            target=run_incident, name="mwb-incident", daemon=True
        )
        t_cognee = threading.Thread(
            target=run_cognee, name="mwb-cognee", daemon=True
        )
        t_memory.start()
        t_incident.start()
        t_cognee.start()

        # Per-store budget joins. ``Thread.join`` returns whether the
        # thread is still alive afterwards via ``is_alive()``; we treat
        # both still-alive and recorded-exception as store failures.
        t_memory.join(self._memory_tool_budget)
        t_incident.join(self._incidents_budget)
        t_cognee.join(self._cognee_budget)

        mt_failed = self._slot_failed(slots, STORE_MEMORY_TOOL, t_memory)
        inc_failed = self._slot_failed(slots, STORE_INCIDENTS, t_incident)
        cog_failed = self._slot_failed(slots, STORE_COGNEE, t_cognee)

        lesson_id = slots.get(STORE_MEMORY_TOOL)
        incident_id = slots.get(STORE_INCIDENTS)
        cognee_ok = slots.get(STORE_COGNEE, False)

        stores_failed = tuple(
            name
            for name, failed in (
                (STORE_MEMORY_TOOL, mt_failed),
                (STORE_INCIDENTS, inc_failed),
                (STORE_COGNEE, cog_failed),
            )
            if failed
        )

        # Recovery: when the incident insert critical-path failed, queue
        # the request to disk for the hourly retry job (spec §recovery).
        if STORE_INCIDENTS in stores_failed:
            self._enqueue_incident_for_retry(request)

        result = WritebackResult(
            ticket_key=request.ticket_key,
            attempt_n=request.attempt_n,
            outcome=request.outcome,
            memory_tool_lesson_id=lesson_id,
            incident_id=incident_id,
            cognee_tickled=bool(cognee_ok),
            stores_failed=stores_failed,
        )
        self._remember(request.idempotency_key, result)
        if stores_failed:
            log.warning(
                "memory_writeback.partial ticket=%s attempt=%s stores_failed=%s",
                request.ticket_key,
                request.attempt_n,
                list(stores_failed),
            )
        return result

    # — Per-store workers —

    def _do_memory_tool(self, request: WritebackRequest) -> str | None:
        """Append the lesson entry to the Memory Tool fleet dir.

        Returns the lesson id when an entry was written; ``None`` when
        the request has no lesson (failure outcomes or unclassified
        successes). Raises any underlying Memory Tool errors so the
        budget wrapper can route them to ``stores_failed``.
        """
        try:
            classified = classify_lesson(request)
        except LessonClassificationFailed as exc:
            log.warning(
                "memory_writeback.lesson_classification_failed ticket=%s err=%s",
                request.ticket_key,
                exc,
            )
            return None
        if classified is None:
            return None
        lesson_id, content = classified
        if self._memory_tool is None:
            # No Memory Tool handler injected — the runner skipped
            # bootstrapping it (e.g. tier-X-only fleet or test scope).
            # Log + skip silently — this is not a store failure.
            log.debug(
                "memory_writeback.memory_tool_unset ticket=%s lesson=%s",
                request.ticket_key,
                lesson_id,
            )
            return lesson_id
        path = f"{MEMORY_PATH_PREFIX}/lesson:{lesson_id}.md"
        result = self._memory_tool.handle({
            "command": "create",
            "path": path,
            "file_text": content,
        })
        if isinstance(result, dict) and result.get("error"):
            raise MemoryToolError(
                f"memory_tool create rejected: {result.get('error')!r}"
            )
        return lesson_id

    def _do_incident(self, request: WritebackRequest) -> str | None:
        """Insert one runner_incidents row.

        * Failure outcomes always insert.
        * Success outcomes insert only when ``record_success_outcome``
          is True (positive-feedback flag; default off per AC #1's
          "OR insert" wording).
        """
        if request.outcome == OUTCOME_FAILURE:
            klass = request.failure_class or classify_from_traceback(
                request.raw_traceback
            )
            summary = request.summary or "(no summary supplied)"
        elif request.outcome == OUTCOME_SUCCESS and request.record_success_outcome:
            klass = FailureClass.OTHER  # SUCCESS_OUTCOME tagged in summary
            summary = f"{_SUCCESS_SUMMARY_PREFIX} {request.summary}".strip()
        else:
            return None
        record = self._incident_writer(
            ticket_key=request.ticket_key,
            failure_class=klass,
            summary=summary,
            raw_traceback=request.raw_traceback,
            runner_class=request.runner_class,
            mutex_label=request.mutex_label,
            area=request.area,
        )
        return record.incident_id

    def _do_cognee(self, request: WritebackRequest) -> bool:
        """Fire-and-forget Cognee re-index tickle.

        Returns ``True`` when the emitter ran without raising. Per
        AC #recovery the tickle is best-effort (nightly rebuild
        re-ingests anyway), so this never blocks completion.
        """
        self._cognee_emitter(request.ticket_key)
        return True

    @staticmethod
    def _slot_failed(
        slots: dict[str, Any], store: str, thread: threading.Thread
    ) -> bool:
        """True when the worker exceeded budget or recorded an exception.

        Logs a single line per failed store so operators can grep the
        runner log for ``memory_writeback.store_timeout`` /
        ``memory_writeback.store_failed`` without depending on
        ``WritebackResult.stores_failed`` plumbing.
        """
        err_key = f"{store}:error"
        if err_key in slots:
            log.warning(
                "memory_writeback.store_failed store=%s err=%s",
                store,
                slots[err_key],
            )
            return True
        if thread.is_alive():
            log.warning(
                "memory_writeback.store_timeout store=%s thread_alive=True",
                store,
            )
            return True
        return False

    # — Idempotency ledger —

    def _check_idempotent(self, request: WritebackRequest) -> WritebackResult | None:
        key = request.idempotency_key
        with _state_lock:
            if key not in _seen_keys:
                return None
            # Cached result lookup. Order is preserved in
            # _results_by_ticket so the most recent attempt_n wins on
            # equal-attempt re-submission (the common idempotency case).
            for prior in reversed(_results_by_ticket.get(request.ticket_key, ())):
                if prior.attempt_n == request.attempt_n:
                    return WritebackResult(
                        ticket_key=prior.ticket_key,
                        attempt_n=prior.attempt_n,
                        outcome=prior.outcome,
                        memory_tool_lesson_id=prior.memory_tool_lesson_id,
                        incident_id=prior.incident_id,
                        cognee_tickled=prior.cognee_tickled,
                        stores_failed=prior.stores_failed,
                        idempotent_skip=True,
                        finished_at=prior.finished_at,
                    )
            # Seen marker without a cached row — treat as collision but
            # still emit a result so the caller can log it.
            return WritebackResult(
                ticket_key=request.ticket_key,
                attempt_n=request.attempt_n,
                outcome=request.outcome,
                memory_tool_lesson_id=None,
                incident_id=None,
                cognee_tickled=False,
                stores_failed=(),
                idempotent_skip=True,
            )

    def _remember(
        self, key: tuple[str, int], result: WritebackResult
    ) -> None:
        with _state_lock:
            _seen_keys.add(key)
            _results_by_ticket.setdefault(key[0], []).append(result)

    # — Recovery queue —

    def _enqueue_incident_for_retry(self, request: WritebackRequest) -> None:
        try:
            self._incident_queue_dir.mkdir(parents=True, exist_ok=True)
            path = self._incident_queue_dir / (
                f"{request.ticket_key}-{request.attempt_n}-{uuid.uuid4().hex[:8]}.json"
            )
            payload = asdict(request)
            path.write_text(json.dumps(payload), encoding="utf-8")
            log.info(
                "memory_writeback.incident_queued path=%s ticket=%s",
                path,
                request.ticket_key,
            )
        except OSError as exc:
            log.warning(
                "memory_writeback.incident_queue_failed ticket=%s err=%s",
                request.ticket_key,
                exc,
            )


# ── Helpers ─────────────────────────────────────────────────────────


def _default_cognee_emitter(ticket_key: str) -> None:
    """Default Cognee tickle — append an event to the local audit log.

    The real subscriber lives in the Cognee ECL pipeline (nightly
    rebuild already re-ingests by ticket key); this default keeps the
    contract testable without standing up Neo4j in unit tests.
    """
    log.debug("memory_writeback.cognee_tickle ticket=%s", ticket_key)
