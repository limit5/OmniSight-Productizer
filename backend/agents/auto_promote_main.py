"""OP-766 / OP-960 / OP-983 develop -> main auto-promotion on
milestone-ready events.

Consumes the ``milestone_ready`` JSON records emitted by
``scripts/release_milestone_checker.py`` and advances ``main`` to the
``develop`` tip.

History
-------
OP-766 originally did a *direct* fast-forward push to ``refs/heads/main``
(``git push gerrit develop:main``). OP-960 (AUDIT-13) showed that Gerrit's
ACL on ``refs/heads/main`` correctly refuses bot direct push — ``main``
must advance through Gerrit Code Review, never a side-door push. OP-983
(AUDIT-26d) replaced the OP-960 bulk-chain review push with one local
``--no-ff`` merge commit, then pushes that commit to the Gerrit magic ref
``refs/for/main``. The change is tagged with the hashtags in
:data:`PROMOTE_HASHTAGS` and a per-release topic from
:func:`promote_topic_for_version`.

What submits the change is, by design, *not* this bot's job:
* short term — an operator submits via the Gerrit UI (the established
  humans-in-the-loop pattern; Sprint H H4 / OP-949 adds a one-click
  "advance main now" affordance);
* longer term — a conditional submit-requirement keyed on the
  ``R3-fastforward`` hashtag lets the merger-bot cast a scoped
  ``Code-Review: +2`` + auto-submit (an ``area:devops`` Gerrit-config
  follow-up, tracked in the AUDIT-13 ADR).

A non-fast-forward shape (``main`` has commits absent from ``develop``)
is still treated as an operator alert, not as a merge. Long develop chains
are intentionally accepted because the review push now contains one merge
commit instead of one Gerrit change per intervening commit.

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
from backend.release_cut_metadata import RELEASE_CUT_HASHTAGS

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
AUDIT_ACTION_MAIN_PROMOTE_CHANGE_PUSHED = "release.main_promote_change_pushed"
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
# ``auto-promote`` marks the source; ``R3-fastforward`` is the
# hook the future conditional submit-requirement keys on (AUDIT-13 ADR /
# ``area:devops`` follow-up) so the merger-bot may cast a scoped +2 +
# auto-submit. Until that rule lands an operator submits via the Gerrit UI.
PROMOTE_HASHTAGS: tuple[str, ...] = RELEASE_CUT_HASHTAGS
PROMOTE_TOPIC_PREFIX = "release-"
PROMOTE_TOPIC = "release-vX.Y.Z"

# Historical OP-960 guard retained for CLI/API compatibility. OP-983 no
# longer uses it because one merge commit creates one Gerrit review change.
DEFAULT_MAX_PROMOTE_BATCH = 10

DEFAULT_REPO = Path("/home/user/sora-bridge")
DEFAULT_EVENT_LOG = Path("/home/user/work/sora/logs/release-milestone/systemd.log")
DEFAULT_CURSOR = Path("/home/user/work/sora/logs/release-milestone/auto-promote.cursor")

NotifyFn = Callable[[str, str, str], None]
EventSink = Callable[[str, dict[str, Any]], None]
AuditSink = Callable[[str, dict[str, Any]], None]
# (repo, remote, commit_sha, target_ref, hashtags, topic) -> PushOutcome
PushForReviewFn = Callable[..., "PushOutcome"]


class MergeCommitConflict(RuntimeError):
    """Raised when the local no-ff merge cannot be created cleanly."""


class MetaTicketNotFound(RuntimeError):
    """Raised when no release META ticket can be derived for a version."""


class PushToReviewFailedFromMergeCommit(RuntimeError):
    """Raised when Gerrit refuses the single merge-commit review push."""


@dataclass(frozen=True)
class PushOutcome:
    """Result of pushing the promotion merge commit to review."""

    ok: bool
    change_urls: tuple[str, ...] = ()
    raw: str = ""
    change_number: str = ""


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
_GERRIT_CHANGE_NUMBER_RE = re.compile(r"/\+/(\d+)")


def _push_for_review(
    *,
    repo: Path,
    remote: str,
    commit_sha: str,
    target_ref: str = "refs/for/main",
    hashtags: tuple[str, ...] = PROMOTE_HASHTAGS,
    topic: str = PROMOTE_TOPIC,
    change_description: str = "",
    timeout: int = 120,
) -> PushOutcome:
    """Push ``commit_sha`` to ``target_ref`` as one review change.

    Hashtags + topic go via ``git push -o`` push options (robust for
    values containing punctuation, which the
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
    cmd += [remote, f"{commit_sha}:{target_ref}"]
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
    change_number = ""
    if urls:
        match = _GERRIT_CHANGE_NUMBER_RE.search(urls[0])
        if match:
            change_number = match.group(1)
    return PushOutcome(
        ok=proc.returncode == 0,
        change_urls=urls,
        raw=blob[:2000],
        change_number=change_number,
    )


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


def promote_topic_for_version(release_version: str) -> str:
    """Return the ADR-0020 per-release Gerrit topic."""
    return f"{PROMOTE_TOPIC_PREFIX}{release_version}"


def _resolve_meta_ticket_for(release_version: str) -> str:
    """Resolve the release META identifier carried in merge/audit text."""
    if not release_version:
        raise MetaTicketNotFound("missing release version")
    return f"RELEASE-{release_version}"


def _build_promote_merge_commit(
    repo: Path,
    develop_sha: str,
    main_sha: str,
    release_version: str,
    meta_ticket: str,
) -> str:
    """Create a merge commit with parents ``[main_sha, develop_sha]``.

    Subject: ``[release-cut {release_version}] Merge develop into main for {meta_ticket}``
    Body: ``Develop tip: {develop_sha} • RELEASE META: {meta_ticket} • Reviewed at R8``
    Returns merge commit SHA. Caller pushes to ``refs/for/main``.
    """
    subject = f"[release-cut {release_version}] Merge develop into main for {meta_ticket}"
    body = f"Develop tip: {develop_sha} • RELEASE META: {meta_ticket} • Reviewed at R8"
    try:
        _git(repo, "checkout", "--detach", main_sha)
        _git(
            repo,
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            subject,
            "-m",
            body,
            develop_sha,
        )
        return _git_one(repo, "rev-parse", "HEAD")
    except subprocess.CalledProcessError as exc:
        try:
            _git(repo, "merge", "--abort")
        except Exception:  # noqa: BLE001 - best-effort cleanup after conflict
            pass
        detail = ((exc.stderr or "") + (exc.stdout or "")).strip()
        raise MergeCommitConflict(detail or "merge commit creation failed") from exc


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
    topic: str | None = None,
    meta_ticket_resolver: Callable[[str], str] = _resolve_meta_ticket_for,
) -> PromotionResult:
    """Handle one release event.

    Only authorized records (``milestone_ready`` or the ADR-0019 operator
    override ``milestone_force_promoted``) trigger git writes, and the
    write is never a direct push to ``refs/heads/<target>`` (Gerrit ACL
    refuses bot direct push to ``main`` — by design). When the develop tip
    is a clean fast-forward over ``main`` we build one merge commit and
    push that commit to ``refs/for/<target>`` so ``main`` advances through
    Gerrit Code Review; submitting the resulting change stays an operator / future
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
    meta_ticket = str(event.get("metaTicket") or event.get("releaseMeta") or "")
    if not meta_ticket:
        try:
            meta_ticket = meta_ticket_resolver(version)
        except MetaTicketNotFound as exc:
            detail = (
                f"develop -> main promotion blocked for {version or 'unknown version'}: "
                f"release META ticket not found ({exc})"
            )
            notify("release-auto-promote", "critical", detail)
            audit_sink(AUDIT_ACTION_MAIN_PROMOTE_BLOCKED, {
                "fixVersion": version,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "before": None,
                "after": {"status": "meta_ticket_not_found"},
            })
            return PromotionResult("blocked", version, "", "", (), (), detail)
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

    # Kept to avoid churn in wrappers/tests that still pass the historical
    # OP-960 batch guard. OP-983 always pushes one merge commit.
    _ = max_promote_batch

    try:
        merge_sha = _build_promote_merge_commit(
            repo, check.develop_tip, check.main_tip, version, meta_ticket
        )
    except MergeCommitConflict as exc:
        detail = (
            f"develop -> main promotion blocked for {version or 'unknown version'}: "
            f"merge commit could not be created: {exc}"
        )
        notify("release-auto-promote", "critical", detail)
        audit_sink(AUDIT_ACTION_MAIN_PROMOTE_BLOCKED, {
            **base_payload,
            "before": {"main_tip": check.main_tip},
            "after": {"status": "merge_conflict", "err": str(exc)[:400]},
        })
        return PromotionResult(
            "blocked", version, check.develop_tip, check.main_tip,
            check.develop_only, check.main_only, detail,
        )

    target_ref = f"refs/for/{target_branch}"
    review_ref = f"{merge_sha}:{target_ref}"
    promote_topic = topic or promote_topic_for_version(version)
    push = push_for_review(
        repo=repo,
        remote=remote,
        commit_sha=merge_sha,
        target_ref=target_ref,
        hashtags=hashtags,
        topic=promote_topic,
        change_description=warning_block,
    )
    if not push.ok:
        detail = (
            f"develop -> main merge-commit push to {review_ref} refused by Gerrit "
            f"for {version or 'unknown version'}: {push.raw[:400] or '(no output)'}"
        )
        err = PushToReviewFailedFromMergeCommit(detail)
        notify("release-auto-promote", "critical", detail)
        audit_sink(AUDIT_ACTION_MAIN_PROMOTE_BLOCKED, {
            **base_payload,
            "before": {"main_tip": check.main_tip},
            "after": {
                "status": "push_rejected",
                "review_ref": review_ref,
                "merge_sha": merge_sha,
                "err": str(err)[:400],
            },
        })
        return PromotionResult(
            "push_rejected", version, check.develop_tip, check.main_tip,
            check.develop_only, check.main_only, detail,
        )

    created = list(push.change_urls)
    detail = (
        (f"{warning_block}\n" if warning_block else "")
        + f"develop -> main promotion merge change created for {version or 'unknown version'} "
        f"on {review_ref}"
        + (f": {', '.join(created)}" if created else " (see Gerrit)")
        + f"; hashtags={list(hashtags)}, topic={promote_topic!r}. main advances through Gerrit "
        f"Code Review — operator submits via the Gerrit UI (or merger-bot once the "
        f"R3-fastforward submit-requirement lands)."
    )
    payload = {
        **base_payload,
        "review_ref": review_ref,
        "hashtags": list(hashtags),
        "topic": promote_topic,
        "release_version": version,
        "meta_ticket": meta_ticket,
        "merge_sha": merge_sha,
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
        "develop_sha": check.develop_tip,
        "release_version": version,
        "merge_sha": merge_sha,
        "gerrit_change_number": push.change_number,
    }
    if force_promoted:
        after["force_promote_reasons"] = force_reasons
        after["change_description"] = warning_block
    notify("release-auto-promote", "warning", detail)
    event_sink(EVENT_MAIN_PROMOTE_CHANGE_CREATED, payload)
    audit_sink(AUDIT_ACTION_MAIN_PROMOTE_CHANGE_PUSHED, {
        **payload,
        "before": {"main_sha": check.main_tip},
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
