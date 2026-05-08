"""OP-741 CI failure recovery state machine.

Handles Gerrit ``Verified -1`` feedback for runner-owned patchsets:
categorise the failure, apply the recovery strategy, enforce a per-JIRA
loop guard, and expose operator escape hooks. The module is deliberately
dependency-injected: production callers wire JIRA/Gerrit/runner actions,
while tests exercise the state machine without network access.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol


FAILURE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"TimeoutError|ConnectionResetError|BrokenPipe", "flaky", "auto-retry"),
    (r"pytest-xdist.*worker.*crashed", "flaky", "auto-retry"),
    (r"OSError.*temporarily unavailable", "flaky", "auto-retry"),
    (r"AssertionError|FAILED.*test_", "real-bug", "bot-patch"),
    (r"ImportError.*develop|ModuleNotFoundError", "stale", "rebase"),
    (r"No such file or directory.*develop branch", "stale", "rebase"),
    (r"(?i)docker\.errors|build.*failed.*image", "infra", "auto-retry-or-escalate"),
    (r"CI worker timeout|killed by signal", "infra", "auto-retry-or-escalate"),
)

CI_ATTEMPT_PREFIX = "ci-attempt:"
CI_DEADLINE_PREFIX = "ci-deadline:"
CI_TIMEOUT_PREFIX = "ci-timeout:"
LOOP_PAUSED_LABEL = "runner-loop-paused-pending-review"
SKIP_CI_LABEL = "runner-skip-ci"

DEFAULT_RETRY_SECONDS = 120
INFRA_RETRY_SECONDS = 300
CI_TIMEOUT_SECONDS = 30 * 60
MEDIUM_NOTIFY_ATTEMPT = 3
HARD_STOP_ATTEMPT = 6
QUARANTINE_MAX_AGE_DAYS = 14


@dataclass(frozen=True)
class CiComment:
    """CI worker comment carrying the failed test output."""

    text: str
    ci_id: str = ""


@dataclass(frozen=True)
class Change:
    """Minimal Gerrit/JIRA shape needed by the recovery state machine."""

    number: int
    jira_key: str
    patchset: int
    commit_sha: str
    subject: str = ""
    project: str = "omnisight/OmniSight-Productizer"
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailureContext:
    """Prompt addendum payload passed back to the runner for bot-patch."""

    change_number: int
    patchset: int
    commit_sha: str
    failed_tests: tuple[str, ...]
    log_snippet: str
    full_log_path: str
    ci_policy: str

    def to_prompt_addendum(self) -> str:
        tests = "\n".join(f"    - {t}" for t in self.failed_tests) or "    - <not parsed>"
        return (
            "ADDENDUM - CI feedback from previous attempt:\n"
            f"  Your previous PS#{self.patchset} (commit {self.commit_sha[:8]}) failed CI.\n"
            "  Test failures:\n"
            f"{tests}\n"
            f"  CI policy: {self.ci_policy}\n"
            f"  Log snippet:\n{self.log_snippet}\n"
            f"  Full log: {self.full_log_path}\n\n"
            "Fix ONLY the failing tests. Preserve the rest of your previous changes "
            "(do not start over). Push as the next patchset on the same Change-Id."
        )


@dataclass(frozen=True)
class RecoveryDecision:
    """Return value for state-machine calls, useful for audit and tests."""

    category: str
    strategy: str
    attempt_count: int
    action: str
    detail: str = ""
    failure_context: FailureContext | None = None


@dataclass
class DeadLetterEntry:
    """In-process dead-letter dashboard row."""

    change: Change
    reason: str
    paused_at: float
    attempt_count: int
    audit_trail: list[dict] = field(default_factory=list)

    def to_payload(self) -> dict:
        payload = asdict(self)
        payload["change"] = asdict(self.change)
        return payload


class RecoveryHooks(Protocol):
    """External effects the state machine needs from JIRA/Gerrit/runner."""

    def increment_ci_attempt_label(self, jira_key: str) -> int: ...
    def hard_stop_with_loop_paused_label(self, jira_key: str, change: Change) -> None: ...
    def notify_operator(self, severity: str, detail: str, change: Change) -> None: ...
    def sleep_then_retrigger_ci(self, change: Change, after_seconds: int) -> None: ...
    def invoke_r3_auto_rebase(self, change: Change) -> None: ...
    def invoke_runner_with_failure_context(
        self, change: Change, comment: CiComment, failure_context: FailureContext
    ) -> None: ...
    def escalate(self, change: Change, reason: str) -> None: ...
    def set_ci_deadline(self, change: Change, deadline_ts: int) -> None: ...
    def clear_ci_deadline(self, change: Change) -> None: ...
    def get_labels(self, jira_key: str) -> tuple[str, ...]: ...
    def add_label(self, jira_key: str, label: str) -> None: ...
    def remove_label(self, jira_key: str, label: str) -> None: ...
    def audit(self, action: str, change: Change | None = None, **payload) -> None: ...


def categorize(comment: CiComment | str) -> tuple[str, str]:
    """Return ``(category, strategy)`` for a Verified -1 comment."""

    log_text = comment.text if isinstance(comment, CiComment) else str(comment)
    for regex, category, strategy in FAILURE_PATTERNS:
        if re.search(regex, log_text):
            return category, strategy
    return "unknown", "auto-retry-once"


def extract_failed_tests(log_text: str) -> tuple[str, ...]:
    """Parse pytest-style failed test nodeids from CI output."""

    found: list[str] = []
    patterns = (
        r"FAILED\s+([A-Za-z0-9_./:-]+::[A-Za-z0-9_:\[\]-]+)",
        r"([A-Za-z0-9_./-]+\.py::[A-Za-z0-9_:\[\]-]+)\s+AssertionError",
    )
    for pattern in patterns:
        for match in re.findall(pattern, log_text):
            if match not in found:
                found.append(match)
    return tuple(found)


def log_snippet(log_text: str, max_lines: int = 40) -> str:
    """Keep the prompt addendum bounded while preserving useful failures."""

    lines = [line.rstrip() for line in log_text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head = lines[: max_lines // 2]
    tail = lines[-(max_lines // 2):]
    return "\n".join(head + ["... <ci log truncated> ..."] + tail)


def build_failure_context(
    change: Change,
    comment: CiComment,
    *,
    ci_policy: str = "Verified -1 recovery: bot may patch only failing tests.",
    log_root: Path | str = "/home/user/work/sora/logs/ci",
) -> FailureContext:
    """Build the bot-patch prompt addendum contract from CI output."""

    ci_id = comment.ci_id or f"change-{change.number}"
    return FailureContext(
        change_number=change.number,
        patchset=change.patchset,
        commit_sha=change.commit_sha,
        failed_tests=extract_failed_tests(comment.text),
        log_snippet=log_snippet(comment.text),
        full_log_path=str(Path(log_root) / f"{ci_id}-PS{change.patchset}.log"),
        ci_policy=ci_policy,
    )


def handle_verified_minus_one(
    change: Change,
    comment: CiComment,
    hooks: RecoveryHooks,
) -> RecoveryDecision:
    """Main 5-step dispatch for a Gerrit ``Verified -1`` comment."""

    category, strategy = categorize(comment)
    attempt_count = hooks.increment_ci_attempt_label(change.jira_key)
    hooks.audit(
        "ci_recovery_verified_minus_one",
        change,
        category=category,
        strategy=strategy,
        attempt_count=attempt_count,
    )

    if attempt_count >= HARD_STOP_ATTEMPT:
        hooks.hard_stop_with_loop_paused_label(change.jira_key, change)
        hooks.notify_operator(
            "high",
            f"PS#{change.number} hard-stopped after {attempt_count} CI failures",
            change,
        )
        hooks.audit("ci_recovery_loop_hard_stop", change, attempt_count=attempt_count)
        return RecoveryDecision(category, strategy, attempt_count, "hard-stop")

    if attempt_count >= MEDIUM_NOTIFY_ATTEMPT:
        hooks.notify_operator(
            "medium",
            f"PS#{change.number} CI failed {attempt_count} times",
            change,
        )

    if strategy == "auto-retry":
        hooks.sleep_then_retrigger_ci(change, after_seconds=DEFAULT_RETRY_SECONDS)
        return RecoveryDecision(category, strategy, attempt_count, "retry")

    if strategy == "rebase":
        hooks.invoke_r3_auto_rebase(change)
        return RecoveryDecision(category, strategy, attempt_count, "rebase")

    if strategy == "bot-patch":
        failure_context = build_failure_context(change, comment)
        hooks.invoke_runner_with_failure_context(change, comment, failure_context)
        return RecoveryDecision(
            category, strategy, attempt_count, "bot-patch",
            failure_context=failure_context,
        )

    if strategy == "auto-retry-or-escalate":
        if attempt_count == 1:
            hooks.sleep_then_retrigger_ci(change, after_seconds=INFRA_RETRY_SECONDS)
            return RecoveryDecision(category, strategy, attempt_count, "retry")
        hooks.notify_operator("high", "CI infra suspected - manual triage", change)
        hooks.escalate(change, "CI infra suspected after retry")
        return RecoveryDecision(category, strategy, attempt_count, "escalate")

    if strategy == "auto-retry-once":
        if attempt_count == 1:
            hooks.sleep_then_retrigger_ci(change, after_seconds=DEFAULT_RETRY_SECONDS)
            return RecoveryDecision(category, strategy, attempt_count, "retry")
        hooks.escalate(change, "unknown failure pattern")
        return RecoveryDecision(category, strategy, attempt_count, "escalate")

    hooks.escalate(change, f"unsupported recovery strategy: {strategy}")
    return RecoveryDecision(category, strategy, attempt_count, "escalate")


def mark_ci_started(
    change: Change,
    hooks: RecoveryHooks,
    *,
    now: int | None = None,
) -> int:
    """Set the 30 minute CI deadline label for a patchset."""

    base = int(time.time()) if now is None else now
    deadline = base + CI_TIMEOUT_SECONDS
    hooks.set_ci_deadline(change, deadline)
    hooks.audit("ci_recovery_deadline_set", change, deadline=deadline)
    return deadline


def handle_ci_timeout(
    change: Change,
    hooks: RecoveryHooks,
    *,
    now: int | None = None,
) -> RecoveryDecision | None:
    """Watcher cron body: expired deadline with no vote becomes infra."""

    current = int(time.time()) if now is None else now
    deadline = _deadline_from_labels(hooks.get_labels(change.jira_key))
    if deadline is None or deadline > current:
        return None

    timeout_count = _timeout_count_from_labels(hooks.get_labels(change.jira_key)) + 1
    _set_prefixed_counter(hooks, change.jira_key, CI_TIMEOUT_PREFIX, timeout_count)
    hooks.clear_ci_deadline(change)
    hooks.audit("ci_recovery_timeout", change, timeout_count=timeout_count)

    if timeout_count == 1:
        hooks.sleep_then_retrigger_ci(change, after_seconds=INFRA_RETRY_SECONDS)
        return RecoveryDecision("infra", "auto-retry-or-escalate", timeout_count, "retry")

    hooks.notify_operator("high", "CI timeout repeated - manual triage", change)
    hooks.escalate(change, "CI timeout repeated")
    return RecoveryDecision("infra", "auto-retry-or-escalate", timeout_count, "escalate")


def should_skip_ci_for_ticket(jira_labels: Iterable[str]) -> bool:
    """Operator escape #2: ticket label makes CI auto-+1."""

    return SKIP_CI_LABEL in set(jira_labels)


def handle_skip_ci_escape(change: Change, hooks: RecoveryHooks) -> RecoveryDecision | None:
    """Apply the ``runner-skip-ci`` auto-+1 escape when present."""

    if not should_skip_ci_for_ticket(hooks.get_labels(change.jira_key)):
        return None
    hooks.audit(
        "ci_recovery_skip_ci_escape",
        change,
        comment="CI skipped per ticket label",
    )
    return RecoveryDecision("operator-escape", "skip-ci", 0, "skip-ci")


def parse_quarantine_line(line: str) -> tuple[str, int] | None:
    """Parse ``tests/quarantine.txt`` lines: ``nodeid expires=<unix>``."""

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split()
    test_id = parts[0]
    expiry = 0
    for part in parts[1:]:
        if part.startswith("expires="):
            try:
                expiry = int(part.split("=", 1)[1])
            except ValueError:
                expiry = 0
    return test_id, expiry


def load_quarantine(path: Path, *, now: int | None = None) -> tuple[str, ...]:
    """Operator escape #3: tests listed here are excluded until expiry."""

    current = int(time.time()) if now is None else now
    if not path.exists():
        return ()
    quarantined: list[str] = []
    for line in path.read_text().splitlines():
        parsed = parse_quarantine_line(line)
        if parsed is None:
            continue
        test_id, expiry = parsed
        if expiry == 0 or expiry >= current:
            quarantined.append(test_id)
    return tuple(quarantined)


def expired_quarantines(
    path: Path,
    *,
    now: int | None = None,
    max_age_days: int = QUARANTINE_MAX_AGE_DAYS,
) -> tuple[str, ...]:
    """Reviewer cron helper: page on quarantines older than 14 days."""

    current = int(time.time()) if now is None else now
    cutoff = current - max_age_days * 86400
    expired: list[str] = []
    if not path.exists():
        return ()
    for line in path.read_text().splitlines():
        parsed = parse_quarantine_line(line)
        if parsed is None:
            continue
        test_id, expiry = parsed
        if expiry and expiry < cutoff:
            expired.append(test_id)
    return tuple(expired)


def simulate_soak(
    changes: Iterable[Change],
    comments: Iterable[CiComment],
    hooks_factory: Callable[[], RecoveryHooks],
) -> dict[str, float | int]:
    """Synthetic 50-PS soak helper used by tests and operator drills."""

    total = auto_resolved = hard_stopped = audited = 0
    for change, comment in zip(changes, comments):
        total += 1
        hooks = hooks_factory()
        decision = handle_verified_minus_one(change, comment, hooks)
        if decision.action in {"retry", "rebase", "bot-patch"}:
            auto_resolved += 1
        if decision.action == "hard-stop":
            hard_stopped += 1
        labels = hooks.get_labels(change.jira_key)
        if labels or decision.action:
            audited += 1
    return {
        "total": total,
        "auto_resolve_rate": auto_resolved / total if total else 0.0,
        "hard_stop_rate": hard_stopped / total if total else 0.0,
        "audit_rate": audited / total if total else 0.0,
        "auto_resolved": auto_resolved,
        "hard_stopped": hard_stopped,
    }


class InMemoryRecoveryBackend:
    """Small production-shaped backend for tests and local drills."""

    def __init__(self) -> None:
        self.labels: dict[str, set[str]] = {}
        self.calls: list[dict] = []
        self.dead_letters: dict[str, DeadLetterEntry] = {}

    def increment_ci_attempt_label(self, jira_key: str) -> int:
        current = _attempt_from_labels(self.get_labels(jira_key)) + 1
        _set_prefixed_counter(self, jira_key, CI_ATTEMPT_PREFIX, current)
        return current

    def hard_stop_with_loop_paused_label(self, jira_key: str, change: Change) -> None:
        self.add_label(jira_key, LOOP_PAUSED_LABEL)
        self.dead_letters[jira_key] = DeadLetterEntry(
            change=change,
            reason="loop guard hard-stop",
            paused_at=time.time(),
            attempt_count=_attempt_from_labels(self.get_labels(jira_key)),
            audit_trail=list(self.calls),
        )
        self.audit("ci_recovery_dead_lettered", change, label=LOOP_PAUSED_LABEL)

    def notify_operator(self, severity: str, detail: str, change: Change) -> None:
        self.calls.append({"action": "notify", "severity": severity, "detail": detail, "change": change.number})

    def sleep_then_retrigger_ci(self, change: Change, after_seconds: int) -> None:
        self.calls.append({"action": "retry", "after_seconds": after_seconds, "change": change.number})
        self.audit("ci_recovery_retry", change, after_seconds=after_seconds)

    def invoke_r3_auto_rebase(self, change: Change) -> None:
        self.calls.append({"action": "rebase", "change": change.number})
        self.audit("ci_recovery_rebase", change)

    def invoke_runner_with_failure_context(
        self, change: Change, comment: CiComment, failure_context: FailureContext
    ) -> None:
        self.calls.append({
            "action": "bot-patch",
            "change": change.number,
            "prompt_addendum": failure_context.to_prompt_addendum(),
        })
        self.audit(
            "ci_recovery_bot_patch",
            change,
            failed_tests=list(failure_context.failed_tests),
            full_log_path=failure_context.full_log_path,
        )

    def escalate(self, change: Change, reason: str) -> None:
        self.calls.append({"action": "escalate", "reason": reason, "change": change.number})
        self.audit("ci_recovery_escalate", change, reason=reason)

    def set_ci_deadline(self, change: Change, deadline_ts: int) -> None:
        _set_prefixed_counter(self, change.jira_key, CI_DEADLINE_PREFIX, deadline_ts)

    def clear_ci_deadline(self, change: Change) -> None:
        for label in list(self.labels.setdefault(change.jira_key, set())):
            if label.startswith(CI_DEADLINE_PREFIX):
                self.remove_label(change.jira_key, label)

    def get_labels(self, jira_key: str) -> tuple[str, ...]:
        return tuple(sorted(self.labels.get(jira_key, set())))

    def add_label(self, jira_key: str, label: str) -> None:
        self.labels.setdefault(jira_key, set()).add(label)

    def remove_label(self, jira_key: str, label: str) -> None:
        self.labels.setdefault(jira_key, set()).discard(label)

    def audit(self, action: str, change: Change | None = None, **payload) -> None:
        self.calls.append({
            "action": "audit",
            "audit_action": action,
            "change": change.number if change else None,
            **payload,
        })

    def list_dead_letters(self) -> list[DeadLetterEntry]:
        return sorted(self.dead_letters.values(), key=lambda row: row.paused_at, reverse=True)

    def dead_letter_action(self, jira_key: str, action: str, reason: str = "") -> bool:
        entry = self.dead_letters.get(jira_key)
        if entry is None:
            return False
        self.calls.append({"action": f"dead-letter:{action}", "jira_key": jira_key, "reason": reason})
        if action == "retrigger-ci":
            self.sleep_then_retrigger_ci(entry.change, DEFAULT_RETRY_SECONDS)
        elif action == "abandon-ps":
            self.escalate(entry.change, reason or "operator abandoned PS")
        elif action == "mark-quarantine":
            self.audit("ci_recovery_quarantine_escape", entry.change, reason=reason)
        elif action == "manual-review":
            self.audit("ci_recovery_manual_review_escape", entry.change, reason=reason)
        else:
            return False
        return True


def _attempt_from_labels(labels: Iterable[str]) -> int:
    return _counter_from_labels(labels, CI_ATTEMPT_PREFIX)


def _timeout_count_from_labels(labels: Iterable[str]) -> int:
    return _counter_from_labels(labels, CI_TIMEOUT_PREFIX)


def _deadline_from_labels(labels: Iterable[str]) -> int | None:
    value = _counter_from_labels(labels, CI_DEADLINE_PREFIX)
    return value or None


def _counter_from_labels(labels: Iterable[str], prefix: str) -> int:
    values: list[int] = []
    for label in labels:
        if not label.startswith(prefix):
            continue
        try:
            values.append(int(label.split(":", 1)[1]))
        except ValueError:
            continue
    return max(values) if values else 0


def _set_prefixed_counter(
    hooks: RecoveryHooks,
    jira_key: str,
    prefix: str,
    value: int,
) -> None:
    for label in hooks.get_labels(jira_key):
        if label.startswith(prefix):
            hooks.remove_label(jira_key, label)
    hooks.add_label(jira_key, f"{prefix}{value}")
