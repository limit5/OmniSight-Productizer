#!/usr/bin/env python3
"""OP-767 staging auto-deploy worker.

Consumes ``main_promoted`` JSON records, pulls the promoted image tag,
applies Alembic with the new backend image, starts the staging compose
stack, then runs the D7 smoke command against staging.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

EVENT_MAIN_PROMOTED = "main_promoted"
EVENT_STAGING_DEPLOYED = "staging_deployed"

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENT_LOG = Path("/home/user/work/sora/logs/release-milestone/systemd.log")
DEFAULT_CURSOR = Path("/home/user/work/sora/logs/release-milestone/auto-deploy-staging.cursor")
DEFAULT_COMPOSE_FILE = REPO_ROOT / "deploy" / "staging" / "docker-compose.yml"
DEFAULT_ENV_FILE = REPO_ROOT / "deploy" / "staging" / ".env"
DEFAULT_STAGING_URL = "https://staging.sora.services"
DEFAULT_DEADLINE_SECONDS = 300

Runner = Callable[..., subprocess.CompletedProcess[str]]
EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class DeployResult:
    """Single staging deploy attempt outcome."""

    status: str
    image_tag: str
    elapsed_seconds: float
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
    offset = 0
    if cursor_path.exists():
        try:
            offset = int(cursor_path.read_text(encoding="utf-8").strip() or "0")
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


def image_tag_from_event(event: dict[str, Any]) -> str:
    for key in ("image_tag", "imageTag", "promoted_tip", "main_sha", "sha"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    raise ValueError("main_promoted event missing image tag/promoted SHA")


def _compose_cmd(compose_file: Path, env_file: Path) -> list[str]:
    cmd = ["docker", "compose", "-f", str(compose_file)]
    if env_file.exists():
        cmd += ["--env-file", str(env_file)]
    return cmd


def _run(
    runner: Runner,
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return runner(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def deploy_on_main_promoted(
    event: dict[str, Any],
    *,
    compose_file: Path = DEFAULT_COMPOSE_FILE,
    env_file: Path = DEFAULT_ENV_FILE,
    staging_url: str = DEFAULT_STAGING_URL,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    runner: Runner = subprocess.run,
    event_sink: EventSink = emit_event,
) -> DeployResult:
    """Handle one ``main_promoted`` event."""
    if event.get("event") != EVENT_MAIN_PROMOTED:
        return DeployResult("ignored", "", 0.0)

    start = time.monotonic()
    image_tag = image_tag_from_event(event)
    env = os.environ.copy()
    env["OMNISIGHT_IMAGE_TAG"] = image_tag

    compose = _compose_cmd(compose_file, env_file)
    smoke_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "prod_smoke_test.py"),
        staging_url.rstrip("/"),
        "--subset",
        "dag1",
    ]
    steps = [
        compose + ["pull"],
        compose + ["up", "-d", "postgres"],
        compose + [
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "PYTHONSAFEPATH=1",
            "-w",
            "/app/backend",
            "backend-a",
            "python",
            "-m",
            "alembic",
            "upgrade",
            "heads",
        ],
        compose + ["up", "-d"],
        smoke_cmd,
    ]

    for cmd in steps:
        remaining = deadline_seconds - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError(f"staging deploy exceeded {deadline_seconds}s before {cmd!r}")
        _run(runner, cmd, env=env, timeout=max(1, int(remaining)))

    elapsed = time.monotonic() - start
    if elapsed > deadline_seconds:
        raise TimeoutError(f"staging deploy exceeded {deadline_seconds}s after smoke")

    payload = {
        "image_tag": image_tag,
        "staging_url": staging_url.rstrip("/"),
        "elapsed_seconds": round(elapsed, 3),
        "deadline_seconds": deadline_seconds,
    }
    event_sink(EVENT_STAGING_DEPLOYED, payload)
    return DeployResult("deployed", image_tag, elapsed)


def run_once(
    *,
    event_log: Path,
    cursor: Path,
    compose_file: Path,
    env_file: Path,
    staging_url: str,
    deadline_seconds: int,
    runner: Runner = subprocess.run,
    event_sink: EventSink = emit_event,
) -> list[DeployResult]:
    results: list[DeployResult] = []
    for record in iter_new_records(event_log, cursor):
        result = deploy_on_main_promoted(
            record,
            compose_file=compose_file,
            env_file=env_file,
            staging_url=staging_url,
            deadline_seconds=deadline_seconds,
            runner=runner,
            event_sink=event_sink,
        )
        if result.status != "ignored":
            results.append(result)
    return results


def follow(
    *,
    event_log: Path,
    cursor: Path,
    compose_file: Path,
    env_file: Path,
    staging_url: str,
    deadline_seconds: int,
    poll_seconds: float,
) -> None:
    while True:
        run_once(
            event_log=event_log,
            cursor=cursor,
            compose_file=compose_file,
            env_file=env_file,
            staging_url=staging_url,
            deadline_seconds=deadline_seconds,
        )
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, default=DEFAULT_EVENT_LOG)
    parser.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--staging-url", default=DEFAULT_STAGING_URL)
    parser.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if args.once:
        run_once(
            event_log=args.event_log,
            cursor=args.cursor,
            compose_file=args.compose_file,
            env_file=args.env_file,
            staging_url=args.staging_url,
            deadline_seconds=args.deadline_seconds,
        )
        return 0

    follow(
        event_log=args.event_log,
        cursor=args.cursor,
        compose_file=args.compose_file,
        env_file=args.env_file,
        staging_url=args.staging_url,
        deadline_seconds=args.deadline_seconds,
        poll_seconds=args.poll_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
