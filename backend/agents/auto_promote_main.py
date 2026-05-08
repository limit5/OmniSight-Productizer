"""OP-766 develop -> main auto-promotion on milestone-ready events.

Consumes the ``milestone_ready`` JSON records emitted by
``scripts/release_milestone_checker.py`` and promotes ``main`` by a
server-side fast-forward push from ``develop``. A non-fast-forward shape
is treated as an operator alert, not as a merge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
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
EVENT_MAIN_PROMOTED = "main_promoted"
AUDIT_ACTION_MAIN_PROMOTED = "release.main_promoted"
AUDIT_ACTION_MAIN_PROMOTE_BLOCKED = "release.main_promote_blocked"

DEFAULT_REPO = Path("/home/user/sora-bridge")
DEFAULT_EVENT_LOG = Path("/home/user/work/sora/logs/release-milestone/systemd.log")
DEFAULT_CURSOR = Path("/home/user/work/sora/logs/release-milestone/auto-promote.cursor")

NotifyFn = Callable[[str, str, str], None]
EventSink = Callable[[str, dict[str, Any]], None]
AuditSink = Callable[[str, dict[str, Any]], None]


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
) -> PromotionResult:
    """Handle one release event.

    Only ``milestone_ready`` records trigger git writes. The push uses the
    exact Gerrit fast-forward refspec from OP-766: ``develop:main``.
    """
    if event.get("event") != EVENT_MILESTONE_READY:
        return PromotionResult("ignored", "", "", "", (), ())

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

    push = _git(repo, "push", remote, f"{source_branch}:{target_branch}", timeout=120)
    promoted_tip = _git_one(repo, "rev-parse", source_branch)
    payload = {
        **base_payload,
        "promoted_tip": promoted_tip,
        "pushed_ref": f"{source_branch}:{target_branch}",
    }
    event_sink(EVENT_MAIN_PROMOTED, payload)
    audit_sink(AUDIT_ACTION_MAIN_PROMOTED, {
        **payload,
        "before": {"main_tip": check.main_tip},
        "after": {"main_tip": promoted_tip, "push": push.stderr.strip() or push.stdout.strip()},
    })
    return PromotionResult(
        "promoted", version, promoted_tip, check.main_tip,
        check.develop_only, check.main_only,
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
