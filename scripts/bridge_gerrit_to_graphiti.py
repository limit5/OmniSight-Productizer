#!/usr/bin/env python3
"""OP-901 — Gerrit stream-events bridge to Graphiti."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable

FORWARDED_EVENT_TYPES = frozenset({
    "patchset-created",
    "change-merged",
    "comment-added",
})


class GraphitiBridgeConfigError(RuntimeError):
    """Required Graphiti/Gerrit bridge configuration is missing."""


class GraphitiAuthRejected(RuntimeError):
    """Graphiti rejected the configured bearer token."""


@dataclass(frozen=True)
class GraphitiBridgeConfig:
    graphiti_base_url: str
    graphiti_token: str
    gerrit_host: str
    gerrit_port: int = 29418
    gerrit_user: str = "codex-bot"
    gerrit_key_path: str = ""
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "GraphitiBridgeConfig":
        src = env if env is not None else os.environ
        token = src.get("OMNISIGHT_MCP_GRAPHITI_TOKEN", "").strip()
        host = src.get("OMNISIGHT_GERRIT_SSH_HOST", "").strip()
        if not token:
            raise GraphitiBridgeConfigError(
                "OMNISIGHT_MCP_GRAPHITI_TOKEN is required"
            )
        if not host:
            raise GraphitiBridgeConfigError("OMNISIGHT_GERRIT_SSH_HOST is required")
        return cls(
            graphiti_base_url=src.get(
                "OMNISIGHT_MCP_GRAPHITI_URL",
                "https://mcp-graphiti.sora.services",
            ).rstrip("/"),
            graphiti_token=token,
            gerrit_host=host,
            gerrit_port=int(src.get("OMNISIGHT_GERRIT_SSH_PORT", "29418")),
            gerrit_user=src.get("OMNISIGHT_GERRIT_USER", "codex-bot"),
            gerrit_key_path=src.get("OMNISIGHT_GERRIT_KEY_PATH", ""),
        )


def parse_stream_line(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def is_code_review_plus_two(event: dict[str, Any]) -> bool:
    if event.get("type") != "comment-added":
        return False
    for approval in event.get("approvals") or []:
        if approval.get("type") != "Code-Review":
            continue
        try:
            if int(approval.get("value", 0)) >= 2:
                return True
        except (TypeError, ValueError):
            continue
    return False


def should_forward_event(event: dict[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type in {"patchset-created", "change-merged"}:
        return True
    if event_type == "comment-added":
        return is_code_review_plus_two(event)
    return False


def normalize_gerrit_event(event: dict[str, Any]) -> dict[str, Any]:
    change = event.get("change") or {}
    patch_set = event.get("patchSet") or {}
    author = event.get("author") or event.get("uploader") or {}
    return {
        "source": "gerrit",
        "event": event.get("type"),
        "change_id": change.get("id") or patch_set.get("changeId"),
        "change_number": change.get("number") or event.get("changeNumber"),
        "project": change.get("project"),
        "branch": change.get("branch"),
        "subject": change.get("subject"),
        "patchset": patch_set.get("number"),
        "revision": patch_set.get("revision"),
        "actor": {
            "name": author.get("name"),
            "email": author.get("email"),
            "username": author.get("username"),
        },
        "approvals": event.get("approvals") or [],
        "event_created_on": event.get("eventCreatedOn"),
        "raw": event,
    }


def post_gerrit_event_to_graphiti(
    event: dict[str, Any],
    config: GraphitiBridgeConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    body = json.dumps(normalize_gerrit_event(event)).encode()
    request = urllib.request.Request(
        f"{config.graphiti_base_url}/ingest/gerrit",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.graphiti_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with opener(request, timeout=config.timeout_seconds) as response:
            data = response.read().decode()
            return json.loads(data) if data else {"status": "ok"}
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise GraphitiAuthRejected(f"Graphiti HTTP {exc.code}") from exc
        raise


def ssh_stream_events_cmd(config: GraphitiBridgeConfig) -> list[str]:
    cmd = [
        "ssh",
        "-p",
        str(config.gerrit_port),
        "-o",
        "BatchMode=yes",
    ]
    if config.gerrit_key_path:
        cmd.extend(["-i", config.gerrit_key_path])
    cmd.extend([f"{config.gerrit_user}@{config.gerrit_host}", "gerrit", "stream-events"])
    return cmd


def forward_stream(
    lines: Iterable[str],
    config: GraphitiBridgeConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> int:
    forwarded = 0
    for line in lines:
        event = parse_stream_line(line)
        if event is None or not should_forward_event(event):
            continue
        post_gerrit_event_to_graphiti(event, config, opener=opener)
        forwarded += 1
    return forwarded


def run(config: GraphitiBridgeConfig) -> int:
    process = subprocess.Popen(
        ssh_stream_events_cmd(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        forward_stream(process.stdout, config)
    finally:
        process.terminate()
    return process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once-file", help="read stream-event JSON lines from file")
    args = parser.parse_args(argv)
    config = GraphitiBridgeConfig.from_env()
    if args.once_file:
        with open(args.once_file, encoding="utf-8") as fh:
            forward_stream(fh, config)
        return 0
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
