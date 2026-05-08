#!/usr/bin/env python3
"""Thin launcher for the submit-queue worker daemon (OP-751).

Wires the SubmitQueueWorker class to real Gerrit SSH/REST credentials
and the JIRA dispatch helpers, then runs the polling loop forever.
The launcher itself is intentionally small so the daemon's logic stays
fully unit-testable inside ``backend.agents.submit_queue_worker``.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("OMNISIGHT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from backend.agents import auto_rebase, jira_dispatch
from backend.agents.gerrit_jira_bridge import structured_log
from backend.agents.submit_queue_worker import (
    DEFAULT_LOCK_DIR,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RATE_LIMIT_SECONDS,
    GERRIT_REST_BASE_URL,
    SubmitQueueWorker,
)


def _ssh_cmd_builder(host: str, port: int, key_path: Path):
    def build(*remote_args: str) -> list[str]:
        return [
            "ssh",
            "-i", str(key_path),
            "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new",
            host,
            *remote_args,
        ]
    return build


def _ssh_env_builder() -> dict[str, str]:
    return os.environ.copy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-class", default="subscription-claude",
        help="JIRA dispatch credential class.",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get(
            "OMNISIGHT_GERRIT_PROJECT",
            "omnisight/OmniSight-Productizer",
        ),
    )
    parser.add_argument(
        "--gerrit-host",
        default=os.environ.get(
            "OMNISIGHT_GERRIT_SSH_HOST", "claude-bot@sora.services",
        ),
        help="SSH host string including bot username.",
    )
    parser.add_argument(
        "--gerrit-port",
        type=int,
        default=int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "29418")),
    )
    parser.add_argument(
        "--gerrit-rest-url",
        default=os.environ.get(
            "OMNISIGHT_GERRIT_URL", GERRIT_REST_BASE_URL,
        ),
    )
    parser.add_argument(
        "--gerrit-key-path",
        default=os.environ.get(
            "OMNISIGHT_GIT_SSH_KEY_PATH",
            "/home/user/.config/omnisight/gerrit-claude-bot-ed25519",
        ),
    )
    parser.add_argument(
        "--worker-username", default="claude-bot",
        help="Gerrit account that votes Submit-Ready=-1 on rejection.",
    )
    parser.add_argument(
        "--rate-limit-seconds", type=float,
        default=float(os.environ.get(
            "OMNISIGHT_SUBMIT_QUEUE_RATE_LIMIT_S",
            DEFAULT_RATE_LIMIT_SECONDS,
        )),
    )
    parser.add_argument(
        "--poll-interval-seconds", type=float,
        default=float(os.environ.get(
            "OMNISIGHT_SUBMIT_QUEUE_POLL_INTERVAL_S",
            DEFAULT_POLL_INTERVAL_SECONDS,
        )),
    )
    parser.add_argument(
        "--lock-dir",
        default=os.environ.get(
            "OMNISIGHT_SUBMIT_QUEUE_LOCK_DIR", str(DEFAULT_LOCK_DIR),
        ),
    )
    args = parser.parse_args(argv)

    client = jira_dispatch.make_client(args.agent_class)

    def _notify(ticket_key: str, message: str) -> None:
        jira_dispatch.add_comment(client, ticket_key, message)

    worker = SubmitQueueWorker(
        project=args.project,
        ssh_cmd_builder=_ssh_cmd_builder(
            args.gerrit_host, args.gerrit_port,
            Path(args.gerrit_key_path).expanduser(),
        ),
        ssh_env_builder=_ssh_env_builder,
        worker_username=args.worker_username,
        worker_password_loader=(
            lambda: auto_rebase.load_owner_http_password(args.worker_username)
        ),
        rest_base_url=args.gerrit_rest_url,
        notify_jira=_notify,
        rate_limit_seconds=args.rate_limit_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        lock_dir=Path(args.lock_dir).expanduser(),
        log=structured_log,
    )
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
