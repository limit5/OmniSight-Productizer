"""OP-766 / OP-960 (AUDIT-13) develop -> main auto-promotion on
milestone-ready events.

Consumes the ``milestone_ready`` JSON records emitted by
``scripts/release_milestone_checker.py`` and advances ``main`` to the
``develop`` tip.

History
-------
OP-766 originally did a *direct* fast-forward push to ``refs/heads/main``
(``git push gerrit develop:main``). OP-960 (AUDIT-13) showed that Gerrit's
ACL on ``refs/heads/main`` correctly refuses bot direct push — ``main``
must advance through Gerrit Code Review, never a side-door push. So the
mechanism now pushes ``develop`` to the Gerrit magic ref
``refs/for/main`` (creating a review change per intervening commit),
tagged with the hashtags in :data:`PROMOTE_HASHTAGS` and topic
:data:`PROMOTE_TOPIC`.

What submits the change(s) is, by design, *not* this bot's job:
* short term — an operator submits via the Gerrit UI (the established
  humans-in-the-loop pattern; Sprint H H4 / OP-949 adds a one-click
  "advance main now" affordance);
* longer term — a conditional submit-requirement keyed on the
  ``milestone:R3-fastforward`` hashtag lets the merger-bot cast a scoped
  ``Code-Review: +2`` + auto-submit (an ``area:devops`` Gerrit-config
  follow-up, tracked in the AUDIT-13 ADR).

A non-fast-forward shape (``main`` has commits absent from ``develop``)
is still treated as an operator alert, not as a merge. A develop tip more
than :data:`DEFAULT_MAX_PROMOTE_BATCH` commits ahead of ``main`` is
refused (Gerrit ``receive.maxBatchChanges``) so an operator does a manual
catch-up merge first.

OP-968 (AUDIT-18c) wires the ``release:force-promote`` operator override
(ADR-0019): the milestone checker may emit ``milestone_force_promoted``
in place of ``milestone_blocked`` when the operator has set the override
label. This module now treats that event as a green-equivalent
authorization (:func:`is_promotion_authorized_event`), prepends an
``OPERATOR FORCE-PROMOTE WARNING:`` block (naming the bypassed gates) to
the Gerrit auto-promote change description, and records the activation in
the audit row with ``outcome=force_promoted`` so it is queryable distinct
from a genuine ``milestone_ready`` promotion.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from backend.agents import jira_dispatch

log = logging.getLogger(__name__)

EVENT_MILESTONE_READY = "milestone_ready"
# ADR-0019 / OP-968: the operator ``release:force-promote`` override event,
# emitted by ``release_milestone_checker.py`` in place of
# ``milestone_blocked`` when the override fixVersion label is present. It
# authorizes a promotion exactly like ``milestone_ready`` but rides a
# permanent warning block + a distinct audit outcome.
EVENT_MILESTONE_FORCE_PROMOTED = "milestone_force_promoted"
EVENT_MAIN_PROMOTED = "main_promoted"
EVENT_MAIN_PROMOTE_CHANGE_CREATED = "main_promote_change_created"
AUDIT_ACTION_MAIN_PROMOTED = "release.main_promoted"
AUDIT_ACTION_MAIN_PROMOTE_CHANGE_CREATED = "release.main_promote_change_created"
AUDIT_ACTION_MAIN_PROMOTE_BLOCKED = "release.main_promote_blocked"

# ``release_audit.outcome`` values. A clean ``milestone_ready`` promotion
# is ``success``; one driven by the ``release:force-promote`` override is
# ``force_promoted`` so dashboards / queries can tell them apart (ADR-0019
# frozen wire contract).
PROMOTE_OUTCOME_SUCCESS = "success"
PROMOTE_OUTCOME_FORCE_PROMOTED = "force_promoted"

# Frozen artifact warning prefix (ADR-0019). Prepended to the Gerrit
# auto-promote change description when a force-promote override is active.
FORCE_PROMOTE_WARNING_PREFIX = "OPERATOR FORCE-PROMOTE WARNING:"

# Hashtags + topic attached to the develop -> main review change(s).
# ``auto-promote`` marks the source; ``milestone:R3-fastforward`` is the
# hook the future conditional submit-requirement keys on (AUDIT-13 ADR /
# ``area:devops`` follow-up) so the merger-bot may cast a scoped +2 +
# auto-submit. Until that rule lands an operator submits via the Gerrit UI.
PROMOTE_HASHTAGS: tuple[str, ...] = ("auto-promote", "milestone:R3-fastforward")
PROMOTE_TOPIC = "develop-to-main"

# Gerrit refuses a single push that would create more than
# ``receive.maxBatchChanges`` changes at once (default 10). If ``develop``
# is that far ahead of ``main`` the promotion needs a manual catch-up
# merge first — we refuse and alert rather than fire a doomed push.
DEFAULT_MAX_PROMOTE_BATCH = 10

DEFAULT_REPO = Path("/home/user/sora-bridge")
DEFAULT_EVENT_LOG = Path("/home/user/work/sora/logs/release-milestone/systemd.log")
DEFAULT_CURSOR = Path("/home/user/work/sora/logs/release-milestone/auto-promote.cursor")

NotifyFn = Callable[[str, str, str], None]
EventSink = Callable[[str, dict[str, Any]], None]
AuditSink = Callable[[str, dict[str, Any]], None]
# (repo, remote, source_branch, target_branch, hashtags, topic) -> PushOutcome
PushForReviewFn = Callable[..., "PushOutcome"]


@dataclass(frozen=True)
class PushOutcome:
    """Result of pushing ``develop`` to ``refs/for/<target>``."""

    ok: bool
    change_urls: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class PromotionResult:
    """Single promotion attempt outcome."""

    status: str
    version: str
    develop_tip: str
    main_tip: str
    develop_only: tuple[str, ...]
    main_only: tuple[str, ...]
    detail: str = ""
    created_changes: tuple[str, ...] = ()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_event(event: str, payload: dict[str, Any]) -> None:
    record = {
        "timestamp": utc_now_iso(),
        "level": "INFO",
        "event": event,
        **payload,
    }
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def _git_lines(repo: Path, *args: str) -> tuple[str, ...]:
    out = _git(repo, *args).stdout.strip()
    if not out:
        return ()
    return tuple(line.strip() for line in out.splitlines() if line.strip())


def _git_one(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


# Gerrit echoes one ``remote:`` line per created/updated change, e.g.
#   remote:   https://gerrit.example.com/c/sora-bridge/+/12345 subject [NEW]
_GERRIT_CHANGE_URL_RE = re.compile(r"https?://\S+?/\+/\d+")


def _push_for_review(
    *,
    repo: Path,
    remote: str,
    source_branch: str,
    target_branch: str,
    hashtags: tuple[str, ...] = PROMOTE_HASHTAGS,
    topic: str = PROMOTE_TOPIC,
    change_description: str = "",
    timeout: int = 120,
) -> PushOutcome:
    """Push ``source_branch`` to ``refs/for/<target_branch>`` as review change(s).

    Hashtags + topic go via ``git push -o`` push options (robust for
    values containing ``:`` like ``milestone:R3-fastforward``, which the
    ``%``-refspec form can't carry). ``change_description``, when set, is
    forwarded as the Gerrit ``message`` push option so it lands on the
    created change(s); the force-promote override (ADR-0019) uses it to
    carry the ``OPERATOR FORCE-PROMOTE WARNING:`` block. Returns a
    :class:`PushOutcome` instead of raising so a Gerrit ACL refusal is
    handled gracefully (the OP-766 direct-push form let a
    ``CalledProcessError`` escape).
    """
    cmd = ["git", "push"]
    for tag in hashtags:
        cmd += ["-o", f"hashtag={tag}"]
    if topic:
        cmd += ["-o", f"topic={topic}"]
    if change_description:
        cmd += ["-o", f"message={change_description}"]
    cmd += [remote, f"{source_branch}:refs/for/{target_branch}"]
    proc = subprocess.run(
        cmd,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    blob = f"{proc.stdout}\n{proc.stderr}".strip()
    urls = tuple(dict.fromkeys(_GERRIT_CHANGE_URL_RE.findall(blob)))
    return PushOutcome(ok=proc.returncode == 0, change_urls=urls, raw=blob[:2000])


def _write_audit(action: str, payload: dict[str, Any]) -> None:
    async def _run() -> None:
        from backend import audit

        await audit.log(
            action=action,
            entity_kind="release_branch",
            entity_id=payload.get("target_branch") or "main",
            before=payload.get("before"),
            after=payload.get("after"),
            actor="auto_promote_main",
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        log.debug("audit skipped: event loop already running")
        return
    try:
        asyncio.run(_run())
    except Exception:
        log.warning("audit write failed for %s", action, exc_info=True)


def _default_notify(channel: str, severity: str, detail: str) -> None:
    jira_dispatch.notify_operator(channel=channel, severity=severity, detail=detail)


def is_promotion_authorized_event(rec: dict[str, Any]) -> bool:
    """True if ``rec`` is an event that authorizes a develop -> main promotion.

    Both the normal ``milestone_ready`` and the ADR-0019 operator override
    ``milestone_force_promoted`` qualify; every other record is ignored.
    """
    return rec.get("event") in (EVENT_MILESTONE_READY, EVENT_MILESTONE_FORCE_PROMOTED)


def _force_promote_reasons(event: dict[str, Any]) -> str:
    """Render the bypassed-gate reasons carried by a force-promote event.

    Consumes the ``reasons`` field the milestone checker copies from the
    ``milestone_blocked`` shape it would otherwise have emitted; tolerates
    a list, a scalar, or nothing.
    """
    reasons = event.get("reasons")
    if isinstance(reasons, (list, tuple)):
        joined = ", ".join(str(r).strip() for r in reasons if str(r).strip())
        return joined or "(unspecified)"
    text = str(reasons).strip() if reasons not in (None, "") else ""
    return text or "(unspecified)"


def force_promote_warning_block(reasons: str) -> str:
    """The ADR-0019 ``OPERATOR FORCE-PROMOTE WARNING:`` block for ``reasons``."""
    return f"{FORCE_PROMOTE_WARNING_PREFIX} gates {reasons}"


def evaluate_fast_forward(
    *,
    repo: Path,
    source_branch: str,
    target_branch: str,
) -> PromotionResult:
    """Return the FF pre-check shape for ``target <- source``."""
    develop_only = _git_lines(repo, "log", "--oneline", f"{target_branch}..{source_branch}")
    main_only = _git_lines(repo, "log", "--oneline", f"{source_branch}..{target_branch}")
    develop_tip = _git_one(repo, "rev-parse", source_branch)
    main_tip = _git_one(repo, "rev-parse", target_branch)
    status = "ff_possible" if develop_only and not main_only else "blocked"
    if not develop_only:
        status = "noop"
    return PromotionResult(
        status=status,
        version="",
        develop_tip=develop_tip,
        main_tip=main_tip,
        develop_only=develop_only,
        main_only=main_only,
    )


def promote_on_milestone_ready(
    event: dict[str, Any],
    *,
    repo: Path = DEFAULT_REPO,
    remote: str = "gerrit",
    source_branch: str = "develop",
    target_branch: str = "main",
    notify: NotifyFn = _default_notify,
    event_sink: EventSink = emit_event,
    audit_sink: AuditSink = _write_audit,
    push_for_review: PushForReviewFn = _push_for_review,
    max_promote_batch: int = DEFAULT_MAX_PROMOTE_BATCH,
    hashtags: tuple[str, ...] = PROMOTE_HASHTAGS,
    topic: str = PROMOTE_TOPIC,
) -> PromotionResult:
    """Handle one release event.

    Only authorized records (``milestone_ready`` or the ADR-0019 operator
    override ``milestone_force_promoted``) trigger git writes, and the
    write is never a direct push to ``refs/heads/<target>`` (Gerrit ACL
    refuses bot direct push to ``main`` — by design). When the develop tip
    is a clean fast-forward over ``main`` we push ``develop`` to
    ``refs/for/<target>`` so ``main`` advances through Gerrit Code
    Review; submitting the resulting change(s) stays an operator / future
    merger-bot action (see module docstring). A force-promoted event
    additionally carries the ``OPERATOR FORCE-PROMOTE WARNING:`` block
    into the change description and an ``outcome=force_promoted`` audit
    row.
    """
    if not is_promotion_authorized_event(event):
        return PromotionResult("ignored", "", "", "", (), ())

    force_promoted = event.get("event") == EVENT_MILESTONE_FORCE_PROMOTED
    force_reasons = _force_promote_reasons(event) if force_promoted else ""
    warning_block = force_promote_warning_block(force_reasons) if force_promoted else ""
    outcome = (
        PROMOTE_OUTCOME_FORCE_PROMOTED if force_promoted else PROMOTE_OUTCOME_SUCCESS
    )
    version = str(event.get("fixVersion") or "")
    check = evaluate_fast_forward(
        repo=repo,
        source_branch=source_branch,
        target_branch=target_branch,
    )
    base_payload = {
        "fixVersion": version,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "develop_tip": check.develop_tip,
        "main_tip": check.main_tip,
        "develop_only_count": len(check.develop_only),
        "main_only_count": len(check.main_only),
    }

    if check.status == "noop":
        detail = f"{target_branch} already contains {source_branch}; no promotion needed"
        audit_sink(AUDIT_ACTION_MAIN_PROMOTE_BLOCKED, {
            **base_payload,
            "before": {"main_tip": check.main_tip},
            "after": {"status": "noop"},
        })
        return PromotionResult(
            "noop", version, check.develop_tip, check.main_tip,
            check.develop_only, check.main_only, detail,
        )

    if check.status != "ff_possible":
        diff = "\n".join(check.main_only[:50])
        detail = (
            f"develop -> main promotion blocked for {version or 'unknown version'}: "
            f"{target_branch} has {len(check.main_only)} commit(s) absent from {source_branch}.\n"
            f"{target_branch}-only commits:\n{diff}"
        )
        notify("release-auto-promote", "critical", detail)
        audit_sink(AUDIT_ACTION_MAIN_PROMOTE_BLOCKED, {
            **base_payload,
            "before": {"main_tip": check.main_tip},
            "after": {"status": "non_ff", "main_only": list(check.main_only[:50])},
        })
        return PromotionResult(
            "blocked", version, check.develop_tip, check.main_tip,
            check.develop_only, check.main_only, detail,
        )

    # Clean fast-forward shape — but ``main`` advances through Gerrit Code
    # Review, not a direct push. Refuse first if the develop tip is so far
    # ahead that the single push would breach Gerrit ``receive.maxBatchChanges``.
    if len(check.develop_only) > max_promote_batch:
        detail = (
            f"develop -> main promotion deferred for {version or 'unknown version'}: "
            f"{source_branch} is {len(check.develop_only)} commit(s) ahead of {target_branch} "
            f"(> receive.maxBatchChanges={max_promote_batch}); needs a manual catch-up merge first."
        )
        notify("release-auto-promote", "critical", detail)
        audit_sink(AUDIT_ACTION_MAIN_PROMOTE_BLOCKED, {
            **base_payload,
            "before": {"main_tip": check.main_tip},
            "after": {"status": "batch_too_large", "develop_only": list(check.develop_only[:50])},
        })
        return PromotionResult(
            "blocked", version, check.develop_tip, check.main_tip,
            check.develop_only, check.main_only, detail,
        )

    push = push_for_review(
        repo=repo,
        remote=remote,
        source_branch=source_branch,
        target_branch=target_branch,
        hashtags=hashtags,
        topic=topic,
        change_description=warning_block,
    )
    review_ref = f"{source_branch}:refs/for/{target_branch}"
    if not push.ok:
        detail = (
            f"develop -> main review-change push to {review_ref} refused by Gerrit "
            f"for {version or 'unknown version'}: {push.raw[:400] or '(no output)'}"
        )
        notify("release-auto-promote", "critical", detail)
        audit_sink(AUDIT_ACTION_MAIN_PROMOTE_BLOCKED, {
            **base_payload,
            "before": {"main_tip": check.main_tip},
            "after": {"status": "push_rejected", "review_ref": review_ref, "err": push.raw[:400]},
        })
        return PromotionResult(
            "push_rejected", version, check.develop_tip, check.main_tip,
            check.develop_only, check.main_only, detail,
        )

    created = list(push.change_urls)
    detail = (
        (f"{warning_block}\n" if warning_block else "")
        + f"develop -> main promotion change(s) created for {version or 'unknown version'} "
        f"on {review_ref}"
        + (f": {', '.join(created)}" if created else " (see Gerrit)")
        + f"; hashtags={list(hashtags)}, topic={topic!r}. main advances through Gerrit "
        f"Code Review — operator submits via the Gerrit UI (or merger-bot once the "
        f"milestone:R3-fastforward submit-requirement lands)."
    )
    payload = {
        **base_payload,
        "review_ref": review_ref,
        "hashtags": list(hashtags),
        "topic": topic,
        "created_changes": created,
    }
    if force_promoted:
        payload["force_promoted"] = True
        payload["force_promote_reasons"] = force_reasons
        payload["change_description"] = warning_block
    after = {
        "status": "change_created",
        "outcome": outcome,
        "created_changes": created,
        "hashtags": list(hashtags),
    }
    if force_promoted:
        after["force_promote_reasons"] = force_reasons
        after["change_description"] = warning_block
    notify("release-auto-promote", "warning", detail)
    event_sink(EVENT_MAIN_PROMOTE_CHANGE_CREATED, payload)
    audit_sink(AUDIT_ACTION_MAIN_PROMOTE_CHANGE_CREATED, {
        **payload,
        "before": {"main_tip": check.main_tip},
        "after": after,
    })
    return PromotionResult(
        "change_created", version, check.develop_tip, check.main_tip,
        check.develop_only, check.main_only, detail, created_changes=tuple(created),
    )


def _parse_json_line(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(raw[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def iter_new_records(path: Path, cursor_path: Path) -> Iterable[dict[str, Any]]:
    """Yield JSON records appended since the last stored byte offset."""
    offset = 0
    if cursor_path.exists():
        try:
            offset = int(cursor_path.read_text().strip() or "0")
        except ValueError:
            offset = 0
    if not path.exists():
        return
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        for raw in fh:
            record = _parse_json_line(raw)
            if record is not None:
                yield record
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(str(fh.tell()), encoding="utf-8")


def run_once(
    *,
    event_log: Path,
    cursor: Path,
    repo: Path,
    remote: str,
    source_branch: str,
    target_branch: str,
    notify: NotifyFn = _default_notify,
    event_sink: EventSink = emit_event,
    audit_sink: AuditSink = _write_audit,
    push_for_review: PushForReviewFn = _push_for_review,
    max_promote_batch: int = DEFAULT_MAX_PROMOTE_BATCH,
) -> list[PromotionResult]:
    results: list[PromotionResult] = []
    for record in iter_new_records(event_log, cursor):
        result = promote_on_milestone_ready(
            record,
            repo=repo,
            remote=remote,
            source_branch=source_branch,
            target_branch=target_branch,
            notify=notify,
            event_sink=event_sink,
            audit_sink=audit_sink,
            push_for_review=push_for_review,
            max_promote_batch=max_promote_batch,
        )
        if result.status != "ignored":
            results.append(result)
    return results


def follow(
    *,
    event_log: Path,
    cursor: Path,
    repo: Path,
    remote: str,
    source_branch: str,
    target_branch: str,
    poll_seconds: float,
) -> None:
    while True:
        run_once(
            event_log=event_log,
            cursor=cursor,
            repo=repo,
            remote=remote,
            source_branch=source_branch,
            target_branch=target_branch,
            notify=_default_notify,
            event_sink=emit_event,
            audit_sink=_write_audit,
        )
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, default=DEFAULT_EVENT_LOG)
    parser.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--remote", default="gerrit")
    parser.add_argument("--source-branch", default="develop")
    parser.add_argument("--target-branch", default="main")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument(
        "--max-promote-batch", type=int, default=DEFAULT_MAX_PROMOTE_BATCH,
        help="refuse the promotion if develop is more than this many commits ahead of main",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if args.once:
        run_once(
            event_log=args.event_log,
            cursor=args.cursor,
            repo=args.repo,
            remote=args.remote,
            source_branch=args.source_branch,
            target_branch=args.target_branch,
            max_promote_batch=args.max_promote_batch,
        )
        return 0
    follow(
        event_log=args.event_log,
        cursor=args.cursor,
        repo=args.repo,
        remote=args.remote,
        source_branch=args.source_branch,
        target_branch=args.target_branch,
        poll_seconds=args.poll_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
