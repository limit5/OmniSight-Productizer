"""Live-state check engine — generic dispatcher for Prerequisites
``live_state_requires`` checks.

Per ``docs/sop/jira-ticket-conventions.md`` §13. Replaces the
hardcoded ``_current_alembic_head()`` probe in ``auto-runner-codex.py``
with a pluggable check registry.

Each check kind is a callable ``(argument) -> CheckResult``. Runner
calls :func:`evaluate` with the parsed YAML list before transitioning
a ticket from TODO to In Progress.

Adding a new check kind:

1. Implement handler ``def _check_<kind>(arg): ...`` returning CheckResult.
2. Register in ``CHECK_KINDS`` dict.
3. Add unit test in ``backend/tests/test_live_state_check.py``.
4. Document the kind in jira-ticket-conventions.md §13 table.

The handler argument shape comes straight from the YAML — runner
passes whatever value the operator wrote. Handlers must validate
shape and return CheckResult(False, "<reason>") on malformed input.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single live-state check.

    Attributes:
        passed: True iff the check matched expected state.
        kind: The check kind name (e.g. "alembic_head") for reporting.
        detail: Human-readable explanation. On fail, must include both
            expected and actual values so the runner-comment template
            in §13 produces actionable diagnostics.
    """

    passed: bool
    kind: str
    detail: str


# ── Built-in check kinds (handlers below) ─────────────────────────


def _check_alembic_head(expected: Any, cwd: Path = None) -> CheckResult:
    """Pass iff ``alembic heads`` returns exactly the expected revision.

    Per L17 (2026-05-06): runs ``alembic`` in ``<cwd>/backend`` so the
    check sees the worktree's actual state, not the runner-host main repo.
    """
    if not isinstance(expected, str):
        return CheckResult(False, "alembic_head", f"argument must be str, got {type(expected).__name__}")
    base = cwd if cwd is not None else REPO_ROOT
    try:
        result = subprocess.run(
            ["alembic", "heads"],
            cwd=base / "backend",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return CheckResult(False, "alembic_head", f"alembic invocation failed: {e}")
    head_match = re.search(r"^([0-9a-f]{4,})\s+\(head\)", result.stdout, re.MULTILINE)
    actual = head_match.group(1) if head_match else result.stdout.strip().split()[0] if result.stdout.strip() else "<empty>"
    return CheckResult(
        actual == expected,
        "alembic_head",
        f"expected {expected!r}, got {actual!r}" if actual != expected else f"head={actual}",
    )


def _check_feature_flag(expected: Any, cwd: Path = None) -> CheckResult:
    """Pass iff env feature flag matches.

    Argument shape: ``"OMNISIGHT_FOO_ENABLED=true"`` (env-only for now;
    DB feature_flags fallback can be added later).
    """
    if not isinstance(expected, str) or "=" not in expected:
        return CheckResult(False, "feature_flag", f"argument must be 'KEY=VALUE', got {expected!r}")
    name, _, want = expected.partition("=")
    actual = os.environ.get(name.strip(), "<unset>")
    return CheckResult(
        actual == want.strip(),
        "feature_flag",
        f"{name}: expected {want!r}, got {actual!r}" if actual != want.strip() else f"{name}={actual}",
    )


def _check_file_exists(expected: Any, cwd: Path = None) -> CheckResult:
    """Pass iff path exists relative to ``cwd`` (default REPO_ROOT)."""
    if not isinstance(expected, str):
        return CheckResult(False, "file_exists", f"argument must be str path, got {type(expected).__name__}")
    base = cwd if cwd is not None else REPO_ROOT
    target = base / expected
    return CheckResult(
        target.exists(),
        "file_exists",
        f"{expected}: " + ("present" if target.exists() else "MISSING"),
    )


def _check_command_succeeds(expected: Any, cwd: Path = None) -> CheckResult:
    """Pass iff shell command returns exit code 0. 30s timeout, runs in ``cwd``."""
    if not isinstance(expected, str):
        return CheckResult(False, "command_succeeds", f"argument must be str command, got {type(expected).__name__}")
    base = cwd if cwd is not None else REPO_ROOT
    try:
        result = subprocess.run(
            expected,
            shell=True,
            cwd=base,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(False, "command_succeeds", f"command timed out: {expected[:60]}")
    if result.returncode == 0:
        return CheckResult(True, "command_succeeds", f"OK: {expected[:60]}")
    stderr_tail = (result.stderr or "").strip().splitlines()[-1] if result.stderr else "<no stderr>"
    return CheckResult(
        False,
        "command_succeeds",
        f"exit {result.returncode}: {expected[:60]} — {stderr_tail[:80]}",
    )


def _check_db_row_count(expected: Any, cwd: Path = None) -> CheckResult:
    """Pass iff ``SELECT COUNT(*) FROM <table>`` satisfies range.

    Argument shape: ``{"table": "users", "min": 1, "max": 100}`` (max optional).
    Requires DATABASE_URL env. Skipped (returns pass) if DB unreachable
    — hard DB-dependent runner pickup should fail-soft, not block.
    """
    if not isinstance(expected, dict) or "table" not in expected:
        return CheckResult(False, "db_row_count", f"argument must be dict with 'table' key, got {expected!r}")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return CheckResult(True, "db_row_count", f"DATABASE_URL unset, skipping {expected['table']}")
    try:
        # Lazy import to avoid hard dependency
        from sqlalchemy import create_engine, text
        engine = create_engine(db_url)
        with engine.connect() as conn:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {expected['table']}")).scalar()
    except Exception as e:
        return CheckResult(False, "db_row_count", f"DB query failed: {e}")
    minv = int(expected.get("min", 0))
    maxv = expected.get("max")
    if count < minv:
        return CheckResult(False, "db_row_count", f"{expected['table']}: count {count} < min {minv}")
    if maxv is not None and count > int(maxv):
        return CheckResult(False, "db_row_count", f"{expected['table']}: count {count} > max {maxv}")
    return CheckResult(True, "db_row_count", f"{expected['table']}: count={count}")


def _check_deployed_version(expected: Any, cwd: Path = None) -> CheckResult:
    """Pass iff localhost:8000/healthz reports matching version."""
    if not isinstance(expected, str):
        return CheckResult(False, "deployed_version", f"argument must be version str, got {type(expected).__name__}")
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:8000/healthz", timeout=5) as resp:
            import json as _json
            data = _json.loads(resp.read().decode())
    except Exception as e:
        return CheckResult(False, "deployed_version", f"health endpoint unreachable: {e}")
    actual = data.get("version") or data.get("release_tag") or "<unknown>"
    return CheckResult(
        actual == expected,
        "deployed_version",
        f"expected {expected!r}, got {actual!r}" if actual != expected else f"version={actual}",
    )


CHECK_KINDS: dict[str, Callable[[Any, Path], CheckResult]] = {
    "alembic_head": _check_alembic_head,
    "feature_flag": _check_feature_flag,
    "file_exists": _check_file_exists,
    "command_succeeds": _check_command_succeeds,
    "db_row_count": _check_db_row_count,
    "deployed_version": _check_deployed_version,
}


# ── Public API ─────────────────────────────────────────────────────


def evaluate(
    requirements: list[dict[str, Any]],
    cwd: Path = None,
) -> list[CheckResult]:
    """Dispatch each requirement to its handler. Order-independent.

    Per L17 (2026-05-06): ``cwd`` controls where path-relative checks
    resolve (file_exists / command_succeeds / alembic_head). When the
    runner calls this for pre-pickup of a worktree-bound ticket, pass
    the worktree path so checks reflect the agent's actual environment,
    not the runner host's main repo state.
    """
    results: list[CheckResult] = []
    for req in requirements:
        if not isinstance(req, dict):
            results.append(CheckResult(False, "<malformed>", f"requirement must be dict, got {type(req).__name__}"))
            continue
        if len(req) != 1:
            results.append(CheckResult(False, "<malformed>", f"requirement must have exactly 1 key, got {list(req.keys())}"))
            continue
        kind, arg = next(iter(req.items()))
        handler = CHECK_KINDS.get(kind)
        if handler is None:
            results.append(CheckResult(False, kind, f"unknown check kind: {kind}"))
            continue
        try:
            results.append(handler(arg, cwd))
        except Exception as e:
            results.append(CheckResult(False, kind, f"handler raised {type(e).__name__}: {e}"))
    return results


def all_passed(results: list[CheckResult]) -> bool:
    return all(r.passed for r in results)


def format_failures(results: list[CheckResult]) -> str:
    """One line per fail: ``- {kind}: {detail}``"""
    return "\n".join(f"  - {r.kind}: {r.detail}" for r in results if not r.passed)


# ── SP-B-X-004 / OP-1062 — transition-boundary TOCTOU re-read ─────
# Complement to the prerequisite checks above: re-read mutable JIRA
# fields at every runner phase boundary, diff vs the pickup snapshot,
# and abort on mid-flight mutations (status revert, assignee swap,
# operator-window label added per ADR-0033, new unresolved Blocks dep).
# Rate-limited to one JIRA fetch per ticket per 60s.

PUBLISHED_STATUS_NAMES: frozenset[str] = frozenset({"Published", "公開済み"})
# "Revert" = ticket pushed back to To Do mid-flight; "advanced" = ticket
# walked forward to a downstream state. Different recovery paths.
_REVERT_STATUS_NAMES: frozenset[str] = frozenset({"To Do"})
_FORWARD_STATUS_NAMES: frozenset[str] = frozenset({
    "Under Review", "Approved", "Approval Pending", "Rejected",
    "Published", "Done",
    "承認待ち", "差し戻し", "承認済み", "完了", "公開済み",
})

# Per ADR-0033: `class:operator-window-*` / `class:operator-rehearsal*`
# labels added mid-flight signal that the ticket is now under operator
# supervision and the autonomous runner must yield.
_OPERATOR_WINDOW_PREFIXES: tuple[str, ...] = (
    "class:operator-window",
    "class:operator-rehearsal",
)

_TOCTOU_REREAD_ENV = "OMNISIGHT_RUNNER_TOCTOU_REREAD_ENABLED"
_TOCTOU_TTL_SEC = 60.0

# Module-level TTL cache. Single-process runner; keyed per ticket so
# concurrent pickups (different keys) don't share entries. `lru_cache`
# is the more familiar Python idiom but it lacks first-class TTL; the
# manual dict-with-timestamp below is the equivalent semantics — one
# JIRA fetch per ticket per TTL window. Tests use `_toctou_reset_cache`.
_LIVE_CACHE: dict[str, tuple[float, dict]] = {}


@dataclass(frozen=True)
class TransitionBoundarySnapshot:
    """Pickup-time view of the mutable ticket fields TOCTOU watches.

    Built once by :func:`capture_transition_boundary_snapshot` right
    after the runner's ``transition_to_in_progress`` POST returns. The
    runner threads it through phase boundaries and passes it to
    :func:`recheck_transition_boundary_state`, which diffs it against
    a (cached, rate-limited) re-read of JIRA.
    """

    key: str
    assignee_account_id: str | None
    status_name: str
    labels: frozenset[str]
    blocker_keys: frozenset[str]


@dataclass(frozen=True)
class BoundaryRecheckResult:
    """Outcome of one transition-boundary re-read. ``action`` is the
    dispatch hint for the runner's recovery path — values:
    ``ok``/``disabled`` (proceed), ``abort_assignee_changed``/
    ``abort_operator_window``/``abort_newly_blocked`` (revert + audit),
    ``abort_reverted``/``abort_already_advanced`` (audit only, leave
    status alone — either someone already reverted, or the ticket has
    walked forward and a duplicate push would land twice)."""

    ok: bool
    action: str
    reason: str


def _toctou_enabled() -> bool:
    raw = os.environ.get(_TOCTOU_REREAD_ENV, "true").strip().lower()
    return raw not in ("0", "false", "off", "no", "")


def _toctou_reset_cache() -> None:
    """Clear the TTL cache. Tests use this between cases."""
    _LIVE_CACHE.clear()


def _toctou_fetch_live(client: Any, key: str) -> dict:
    """Lazy import of jira_dispatch to avoid a top-level circular import."""
    from backend.agents import jira_dispatch as jd

    fields = "assignee,status,labels,issuelinks"
    issue = jd._request(client, "GET", f"/issue/{key}?fields={fields}")
    return (issue.get("fields") or {}) if isinstance(issue, dict) else {}


def _toctou_cached_live(
    client: Any, key: str, *, now: float | None = None,
) -> dict:
    """Return cached live fields, refetching at most once per TTL."""
    stamp = now if now is not None else time.monotonic()
    cached = _LIVE_CACHE.get(key)
    if cached is not None and (stamp - cached[0]) < _TOCTOU_TTL_SEC:
        return cached[1]
    fields = _toctou_fetch_live(client, key)
    _LIVE_CACHE[key] = (stamp, fields)
    return fields


def _snapshot_from_fields(key: str, fields: dict) -> TransitionBoundarySnapshot:
    assignee = fields.get("assignee") or {}
    acc = assignee.get("accountId") if isinstance(assignee, dict) else None
    status = (fields.get("status") or {}).get("name") or ""
    labels = frozenset(fields.get("labels") or ())
    blockers: set[str] = set()
    for link in fields.get("issuelinks") or ():
        if (link.get("type") or {}).get("name") != "Blocks":
            continue
        inward = link.get("inwardIssue") or {}
        bk = inward.get("key")
        if bk:
            blockers.add(bk)
    return TransitionBoundarySnapshot(
        key=key,
        assignee_account_id=acc,
        status_name=status,
        labels=labels,
        blocker_keys=frozenset(blockers),
    )


def capture_transition_boundary_snapshot(
    client: Any, key: str, *, now: float | None = None,
) -> TransitionBoundarySnapshot:
    """Snapshot mutable fields at pickup so later rechecks can diff.

    Populates the TTL cache as a side effect — the first in-flight
    :func:`recheck_transition_boundary_state` after capture reuses the
    same payload instead of re-hitting JIRA.
    """
    fields = _toctou_cached_live(client, key, now=now)
    return _snapshot_from_fields(key, fields)


def _evaluate_recheck(
    snap: TransitionBoundarySnapshot, fields: dict,
) -> BoundaryRecheckResult:
    assignee = fields.get("assignee") or {}
    live_acc = assignee.get("accountId") if isinstance(assignee, dict) else None
    if live_acc != snap.assignee_account_id:
        return BoundaryRecheckResult(
            False, "abort_assignee_changed",
            f"assignee diverged from pickup: snapshot={snap.assignee_account_id!r} "
            f"live={live_acc!r}",
        )
    live_status = (fields.get("status") or {}).get("name") or ""
    if live_status and live_status != snap.status_name:
        if live_status in _REVERT_STATUS_NAMES:
            return BoundaryRecheckResult(
                False, "abort_reverted",
                f"status {snap.status_name!r} → {live_status!r}; someone reverted us",
            )
        if live_status in _FORWARD_STATUS_NAMES:
            return BoundaryRecheckResult(
                False, "abort_already_advanced",
                f"status advanced to {live_status!r} mid-flight; do not push duplicate",
            )
    live_labels = frozenset(fields.get("labels") or ())
    newly_added = live_labels - snap.labels
    op_window = sorted(
        l for l in newly_added
        if any(l.startswith(p) for p in _OPERATOR_WINDOW_PREFIXES)
    )
    if op_window:
        return BoundaryRecheckResult(
            False, "abort_operator_window",
            f"operator-window/rehearsal labels added mid-flight: {op_window} (ADR-0033)",
        )
    newly_blocked: list[str] = []
    for link in fields.get("issuelinks") or ():
        if (link.get("type") or {}).get("name") != "Blocks":
            continue
        inward = link.get("inwardIssue") or {}
        bk = inward.get("key")
        if not bk or bk in snap.blocker_keys:
            continue
        inward_status = ((inward.get("fields") or {}).get("status") or {}).get("name") or ""
        if inward_status not in PUBLISHED_STATUS_NAMES:
            newly_blocked.append(f"{bk}({inward_status or '<unknown>'})")
    if newly_blocked:
        return BoundaryRecheckResult(
            False, "abort_newly_blocked",
            f"new Blocks dependencies not yet published: {newly_blocked}",
        )
    return BoundaryRecheckResult(True, "ok", "no transition-boundary mutation")


def recheck_transition_boundary_state(
    client: Any,
    ticket_key: str,
    pickup_snapshot: TransitionBoundarySnapshot,
    *,
    now: float | None = None,
) -> BoundaryRecheckResult:
    """TOCTOU re-read at a phase boundary; diff vs pickup snapshot.

    Runner calls this between phases — after `working` before `submitting`,
    and after `committing` before push. Returns a non-ok result whenever
    one of the four C3a mutation classes (assignee swap, status drift,
    operator-window label added mid-flight, new unresolved Blocks
    dependency) fires.

    Rate-limited: at most one live JIRA fetch per ticket per 60s; calls
    within the window reuse the cached fields. The env flag
    ``OMNISIGHT_RUNNER_TOCTOU_REREAD_ENABLED=false`` short-circuits to
    a noop (returns ok, action="disabled") for emergency operator bypass.
    """
    if not _toctou_enabled():
        return BoundaryRecheckResult(True, "disabled", "TOCTOU reread disabled via env")
    fields = _toctou_cached_live(client, ticket_key, now=now)
    return _evaluate_recheck(pickup_snapshot, fields)
