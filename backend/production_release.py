"""OP-770 production release approval and deploy orchestration.

This module is deliberately backend-only. The approval surface is a
server-rendered admin page, while the deploy worker shells out through an
injectable runner so tests can pin the production sequence without
touching Docker or the host.
"""
from __future__ import annotations

import asyncio
import argparse
import html
import json
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from backend.config import settings


EVENT_RELEASE_TAGGED = "release_tagged"
DEFAULT_EVENT_LOG = Path("/home/user/work/sora/logs/release-milestone/staging.log")
DEFAULT_CURSOR = Path("/home/user/work/sora/logs/release-milestone/release-approval.cursor")
DEFAULT_STORE = Path(os.environ.get("OMNISIGHT_RELEASE_APPROVAL_DIR", "data/release-approvals"))
DEFAULT_IMAGE_REPOSITORY = os.environ.get(
    "OMNISIGHT_RELEASE_IMAGE_REPOSITORY",
    "ghcr.io/omnisight/productizer",
)
DEFAULT_APPROVAL_BASE_URL = os.environ.get(
    "OMNISIGHT_RELEASE_APPROVAL_BASE_URL",
    settings.frontend_origin.rstrip("/") if settings.frontend_origin else "",
)


@dataclass(frozen=True)
class ReleaseApproval:
    tag: str
    smoke_results: dict[str, Any] = field(default_factory=dict)
    baseline_diff: dict[str, Any] = field(default_factory=dict)
    change_list: list[str] = field(default_factory=list)
    main_sha: str = ""
    release_branch: str = ""
    status: str = "pending"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    approved_by: str = ""
    approved_at: str = ""
    cancelled_by: str = ""
    cancelled_at: str = ""
    deploy_started_at: str = ""
    deploy_finished_at: str = ""
    deploy_elapsed_seconds: float = 0.0
    last_error: str = ""
    approval_url: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReleaseApproval":
        fields = cls.__dataclass_fields__
        return cls(**{k: data[k] for k in fields if k in data})


class CommandRunner(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> None: ...


def run_command(
    args: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(
        args,
        check=True,
        timeout=timeout,
        env=env,
        capture_output=True,
        text=True,
    )


def _safe_tag(tag: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in tag.strip())
    if not safe:
        raise ValueError("release tag is required")
    return safe


def _store_path(tag: str, store_dir: Path = DEFAULT_STORE) -> Path:
    return store_dir / f"{_safe_tag(tag)}.json"


def _approval_url(tag: str, base_url: str = DEFAULT_APPROVAL_BASE_URL) -> str:
    path = f"/admin/release-approval?tag={_safe_tag(tag)}"
    return f"{base_url}{path}" if base_url else path


def save_approval(record: ReleaseApproval, store_dir: Path = DEFAULT_STORE) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    path = _store_path(record.tag, store_dir)
    path.write_text(
        json.dumps(asdict(record), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_approval(tag: str, store_dir: Path = DEFAULT_STORE) -> ReleaseApproval:
    path = _store_path(tag, store_dir)
    if not path.exists():
        raise FileNotFoundError(f"release approval not found for tag {tag}")
    return ReleaseApproval.from_dict(json.loads(path.read_text(encoding="utf-8")))


def latest_approval(store_dir: Path = DEFAULT_STORE) -> ReleaseApproval | None:
    if not store_dir.exists():
        return None
    files = sorted(store_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    return ReleaseApproval.from_dict(json.loads(files[0].read_text(encoding="utf-8")))


def create_approval_from_release_tagged(
    event: dict[str, Any],
    *,
    store_dir: Path = DEFAULT_STORE,
    notify_fn: Callable[..., Any] | None = None,
    ticket: str = "OP-770",
) -> ReleaseApproval:
    """Persist a pending approval and notify the operator with a link."""
    tag = str(event.get("tag") or event.get("fixVersion") or "").strip()
    if not tag:
        raise ValueError("release_tagged event missing tag/fixVersion")
    record = ReleaseApproval(
        tag=tag,
        smoke_results=dict(event.get("smoke_results") or event.get("smokeResults") or {}),
        baseline_diff=dict(event.get("baseline_diff") or event.get("baselineDiff") or {}),
        change_list=list(event.get("change_list") or event.get("changeList") or []),
        main_sha=str(event.get("main_sha") or ""),
        release_branch=str(event.get("release_branch") or ""),
        approval_url=_approval_url(tag),
    )
    save_approval(record, store_dir)

    if notify_fn is None:
        from backend.agents import operator_notifier
        notify_fn = operator_notifier.notify
    notify_fn(
        "CRITICAL",
        "release.ship_approval_required",
        f"Release {tag} is ready for production approval: {record.approval_url}",
        context={"tag": tag, "approval_url": record.approval_url},
        ticket=ticket,
        scope="release",
        root_cause_key=f"release:{tag}:approval",
    )
    return record


def run_once(
    *,
    event_log: Path = DEFAULT_EVENT_LOG,
    cursor: Path = DEFAULT_CURSOR,
    store_dir: Path = DEFAULT_STORE,
    notify_fn: Callable[..., Any] | None = None,
) -> list[ReleaseApproval]:
    """Consume new ``release_tagged`` records from the milestone log."""
    from backend.agents.auto_promote_main import iter_new_records

    records: list[ReleaseApproval] = []
    for event in iter_new_records(event_log, cursor):
        if event.get("event") != EVENT_RELEASE_TAGGED:
            continue
        records.append(
            create_approval_from_release_tagged(
                event,
                store_dir=store_dir,
                notify_fn=notify_fn,
            )
        )
    return records


def render_approval_html(record: ReleaseApproval) -> str:
    changes = "\n".join(f"<li>{html.escape(item)}</li>" for item in record.change_list)
    smoke = html.escape(json.dumps(record.smoke_results, indent=2, sort_keys=True))
    baseline = html.escape(json.dumps(record.baseline_diff, indent=2, sort_keys=True))
    tag = html.escape(record.tag)
    status = html.escape(record.status)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Release Approval {tag}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 32px; max-width: 1040px; }}
    h1 {{ font-size: 28px; margin-bottom: 4px; }}
    section {{ border-top: 1px solid #ddd; padding: 18px 0; }}
    pre {{ background: #f6f8fa; padding: 12px; overflow: auto; }}
    button {{ font-size: 18px; font-weight: 700; padding: 12px 18px; margin-right: 8px; }}
    .ship {{ background: #0a7f35; color: white; border: 0; }}
    .cancel {{ background: #eee; border: 1px solid #bbb; }}
  </style>
</head>
<body>
  <h1>Production Release {tag}</h1>
  <p>Status: <strong>{status}</strong></p>
  <section><h2>Smoke Results</h2><pre>{smoke}</pre></section>
  <section><h2>Baseline Diff</h2><pre>{baseline}</pre></section>
  <section><h2>Change List</h2><ul>{changes}</ul></section>
  <section>
    <h2>Operator reason (required)</h2>
    <p>Per OP-779, every prod ship/cancel needs a free-form reason that lands in <code>deploy_audit</code>.</p>
    <form method="post" action="/admin/release-approval/ship?tag={tag}" style="display:inline">
      <input type="text" name="reason" required minlength="1" placeholder="reason for shipping" size="60">
      <button class="ship" type="submit">SHIP IT</button>
    </form>
    <form method="post" action="/admin/release-approval/cancel?tag={tag}" style="display:inline">
      <input type="text" name="reason" required minlength="1" placeholder="reason for cancelling" size="60">
      <button class="cancel" type="submit">Cancel</button>
    </form>
  </section>
</body>
</html>"""


class ProductionDeployOrchestrator:
    def __init__(
        self,
        *,
        runner: CommandRunner = run_command,
        compose_file: str = "docker-compose.prod.yml",
        image_repository: str = DEFAULT_IMAGE_REPOSITORY,
        staging_rollback_command: list[str] | None = None,
        health_timeout_seconds: float = 120.0,
        deploy_timeout_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runner = runner
        self.compose_file = compose_file
        self.image_repository = image_repository
        raw_cancel = os.environ.get("OMNISIGHT_RELEASE_STAGING_ROLLBACK_CMD", "").strip()
        self.staging_rollback_command = (
            list(staging_rollback_command)
            if staging_rollback_command is not None
            else (shlex.split(raw_cancel) if raw_cancel else ["true"])
        )
        self.health_timeout_seconds = health_timeout_seconds
        self.deploy_timeout_seconds = deploy_timeout_seconds
        self.clock = clock

    def _run(self, args: list[str], *, timeout: float, tag: str) -> None:
        env = os.environ.copy()
        env["OMNISIGHT_RELEASE_IMAGE_TAG"] = tag
        self.runner(args, timeout=timeout, env=env)

    def _rollback(self, tag: str) -> None:
        self._run(["scripts/deploy.sh", "--rollback"], timeout=120.0, tag=tag)

    def ship(self, tag: str) -> float:
        started = self.clock()
        try:
            self._run(["docker", "pull", f"{self.image_repository}:{tag}"], timeout=300.0, tag=tag)
            self._run(
                [
                    "docker", "compose", "-f", self.compose_file, "run", "--rm",
                    "--no-deps", "-e", "PYTHONSAFEPATH=1", "-w", "/app/backend",
                    "backend-a", "python", "-m", "alembic", "upgrade", "head",
                ],
                timeout=300.0,
                tag=tag,
            )
            for service, port in (("backend-a", "8000"), ("backend-b", "8001")):
                self._run(
                    ["docker", "compose", "-f", self.compose_file, "up", "-d", "--no-deps", service],
                    timeout=120.0,
                    tag=tag,
                )
                self._run(
                    ["curl", "-sf", f"http://localhost:{port}/readyz"],
                    timeout=self.health_timeout_seconds,
                    tag=tag,
                )
            self._run(
                ["docker", "compose", "-f", self.compose_file, "up", "-d", "--no-deps", "frontend"],
                timeout=120.0,
                tag=tag,
            )
        except Exception:
            self._rollback(tag)
            raise
        elapsed = self.clock() - started
        if elapsed >= self.deploy_timeout_seconds:
            self._rollback(tag)
            raise TimeoutError(f"production deploy exceeded {self.deploy_timeout_seconds:.0f}s")
        return elapsed

    def cancel(self, tag: str) -> None:
        self._run(self.staging_rollback_command, timeout=120.0, tag=tag)


def _require_reason(reason: str | None, action: str) -> str:
    """OP-779 D18 -- prod approval workflow must capture an operator reason."""
    if reason is None or not reason.strip():
        raise ValueError(
            f"{action} requires a non-empty reason (OP-779 audit requirement)"
        )
    return reason.strip()


async def approve_and_ship(
    tag: str,
    *,
    actor: str,
    reason: str | None = None,
    store_dir: Path = DEFAULT_STORE,
    orchestrator: ProductionDeployOrchestrator | None = None,
    audit_log: Callable[..., Any] | None = None,
    deploy_audit_record: Callable[..., int] | None = None,
) -> ReleaseApproval:
    reason = _require_reason(reason, "release.ship_approved")
    record = load_approval(tag, store_dir)
    now = datetime.now(timezone.utc).isoformat()
    record = ReleaseApproval.from_dict({
        **asdict(record),
        "status": "deploying",
        "approved_by": actor,
        "approved_at": now,
        "deploy_started_at": now,
    })
    save_approval(record, store_dir)
    if audit_log is None:
        from backend import audit
        audit_log = audit.log
    await audit_log(
        action="release.ship_approved",
        entity_kind="release",
        entity_id=tag,
        before={"status": "pending"},
        after={"status": "deploying", "tag": tag, "reason": reason},
        actor=actor,
    )

    if deploy_audit_record is None:
        from backend import deploy_audit as _deploy_audit
        deploy_audit_record = _deploy_audit.record
    await asyncio.to_thread(
        deploy_audit_record,
        kind="deploy",
        status="started",
        tag=tag,
        actor=actor,
        reason=reason,
    )

    orch = orchestrator or ProductionDeployOrchestrator()
    try:
        elapsed = await asyncio.to_thread(orch.ship, tag)
    except Exception as exc:
        failed = ReleaseApproval.from_dict({
            **asdict(record),
            "status": "rolled_back",
            "deploy_finished_at": datetime.now(timezone.utc).isoformat(),
            "last_error": str(exc),
        })
        save_approval(failed, store_dir)
        await asyncio.to_thread(
            deploy_audit_record,
            kind="rollback",
            status="succeeded",
            tag=tag,
            actor=actor,
            reason=f"automatic rollback after deploy failure: {exc}",
            context={"trigger": "ship_failure", "error": str(exc)},
        )
        raise

    shipped = ReleaseApproval.from_dict({
        **asdict(record),
        "status": "shipped",
        "deploy_finished_at": datetime.now(timezone.utc).isoformat(),
        "deploy_elapsed_seconds": elapsed,
    })
    save_approval(shipped, store_dir)
    await asyncio.to_thread(
        deploy_audit_record,
        kind="deploy",
        status="succeeded",
        tag=tag,
        actor=actor,
        reason=reason,
        elapsed_seconds=elapsed,
    )
    return shipped


async def cancel_release(
    tag: str,
    *,
    actor: str,
    reason: str | None = None,
    store_dir: Path = DEFAULT_STORE,
    orchestrator: ProductionDeployOrchestrator | None = None,
    deploy_audit_record: Callable[..., int] | None = None,
) -> ReleaseApproval:
    reason = _require_reason(reason, "release.cancelled")
    record = load_approval(tag, store_dir)
    orch = orchestrator or ProductionDeployOrchestrator()
    await asyncio.to_thread(orch.cancel, tag)
    cancelled = ReleaseApproval.from_dict({
        **asdict(record),
        "status": "cancelled",
        "cancelled_by": actor,
        "cancelled_at": datetime.now(timezone.utc).isoformat(),
    })
    save_approval(cancelled, store_dir)
    if deploy_audit_record is None:
        from backend import deploy_audit as _deploy_audit
        deploy_audit_record = _deploy_audit.record
    await asyncio.to_thread(
        deploy_audit_record,
        kind="operator_action",
        status="succeeded",
        tag=tag,
        actor=actor,
        reason=reason,
        context={"action": "release_cancelled"},
    )
    return cancelled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OP-770 release approval consumer")
    parser.add_argument("--event-log", type=Path, default=DEFAULT_EVENT_LOG)
    parser.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR)
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE)
    args = parser.parse_args(argv)
    records = run_once(
        event_log=args.event_log,
        cursor=args.cursor,
        store_dir=args.store_dir,
    )
    print(json.dumps({"approvals_created": [r.tag for r in records]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
