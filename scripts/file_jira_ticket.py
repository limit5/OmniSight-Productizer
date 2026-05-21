#!/usr/bin/env python3
"""File a runner-pickable JIRA ticket.

Enforces two invariants:
1. ``issuetype = Story`` because runner JQL excludes Task.
2. ``area:`` labels cover domains referenced by AC + Files / Paths text.

Usage:
  scripts/file_jira_ticket.py --summary 'X' --description-file desc.md \
      --priority High --tier S --class subscription-codex \
      --areas backend,docs,tests

Use ``--check`` to validate locally without a JIRA POST.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path
from typing import Any

try:
    from jira_label_validator import format_issues, validate
except ModuleNotFoundError:  # pragma: no cover - import path used by tests
    from scripts.jira_label_validator import format_issues, validate

try:
    from jira_mutex_auto_inject import auto_inject_for_filing
except ModuleNotFoundError:  # pragma: no cover - import path used by tests
    from scripts.jira_mutex_auto_inject import auto_inject_for_filing

VALID_AREAS = {
    "backend",
    "frontend",
    "docs",
    "tests",
    "devops",
    "security",
    "embedded",
    "tooling",
    "ci",
    "gerrit",
    "db",
}
VALID_TIERS = {"S", "M", "L", "X"}
VALID_CLASSES = {
    "subscription-codex",
    "subscription-claude",
    "api-anthropic",
    "api-openai",
}
# Subscription classes push to Gerrit; api-* classes call the model API and
# do not push. The capability:enable=gerrit_push label gates runner pickup
# and is auto-added for subscription-* tickets unless --no-push-capability
# is supplied (see feedback_capability_safe_default_leak memory).
PUSH_CAPABLE_CLASSES = {"subscription-codex", "subscription-claude"}
DEFAULT_PUSH_CAPABILITY = "gerrit_push"

# Areas accepted by the runner (auto-runner-jira.RECOGNISED_AREAS) but
# intentionally NOT accepted by this script. Keep this empty unless the
# filing CLI deliberately lags the runner whitelist for an operator-facing
# reason.
RUNNER_ONLY_AREAS: set[str] = set()
RUNNER_RECOGNIZED_AREAS_MEMORY = "feedback_runner_recognized_areas"

REPO_ROOT = Path(__file__).resolve().parents[1]
CRED_DIR = Path("~/.config/omnisight").expanduser()
USER_AGENT = "OmniSight-file-jira-ticket/1.0"


def validate_areas_match_description(areas: set[str], description_text: str) -> list[str]:
    """Warn if description references files in domains not covered by areas."""
    referenced: set[str] = set()
    if re.search(r"\bdocs/[A-Za-z0-9_/.-]+\.md\b", description_text):
        referenced.add("docs")
    if re.search(r"\b(?:backend|scripts)/[A-Za-z0-9_/.-]+\.py\b", description_text):
        referenced.add("backend")
    if re.search(r"\bbackend/tests/test_[A-Za-z0-9_/.-]+\.py\b", description_text):
        referenced.add("tests")
    if re.search(r"\b(?:components|hooks|app|test)/[A-Za-z0-9_/.-]+\.tsx?\b", description_text):
        referenced.add("frontend")
    if re.search(r"\bdeploy/systemd/|\.(?:service|timer)\b", description_text):
        referenced.add("devops")

    missing = referenced - areas
    if not missing:
        return []
    return [
        f"Description references files in {sorted(missing)} but --areas "
        f"only includes {sorted(areas)}. Runner CLI will halt at AC time."
    ]


def parse_description_to_adf(description_text: str) -> dict[str, Any]:
    """Render markdown as a single ADF codeBlock, matching migration scripts."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "attrs": {"language": "markdown"},
                "content": [{"type": "text", "text": description_text}],
            }
        ],
    }


def runner_pickup_jql(project: str, agent_class: str) -> str:
    return (
        f'project = "{project}" '
        "AND issuetype = Story "
        'AND status = "To Do" '
        "AND assignee is EMPTY "
        f'AND labels = "class:{agent_class}" '
        'AND status != "Waiting for External" '
        'AND labels not in ("tier:X") '
        "ORDER BY priority DESC, created ASC"
    )


def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _jira_config(agent_class: str) -> tuple[str, str, str]:
    if agent_class in ("subscription-codex", "api-openai"):
        env_file = CRED_DIR / "jira-codex.env"
        token_file = CRED_DIR / "jira-codex-token"
        email_key = "OMNISIGHT_JIRA_CODEX_EMAIL"
    else:
        env_file = CRED_DIR / "jira-claude.env"
        token_file = CRED_DIR / "jira-claude-token"
        email_key = "OMNISIGHT_JIRA_CLAUDE_EMAIL"

    env = _load_env(env_file)
    raw = f"{env[email_key]}:{token_file.read_text().strip()}".encode()
    site = env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/")
    project = env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP")
    return site, project, "Basic " + b64encode(raw).decode()


def _request(method: str, url: str, auth_header: str, body: dict[str, Any] | None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"{method} {url} -> {exc.code}: {text}") from exc


def _labels(args: argparse.Namespace) -> list[str]:
    labels = [
        "agent:auto",
        f"class:{args.cls}",
        f"tier:{args.tier}",
        "priority:meta",
        f"type:{args.type}",
    ]
    for area in args.areas:
        if area not in VALID_AREAS:
            if area in RUNNER_ONLY_AREAS:
                raise SystemExit(
                    f"invalid area: {area} — '{area}' is in the runner's "
                    "RECOGNISED_AREAS whitelist (see auto-runner-jira.py) "
                    "but intentionally not in file_jira_ticket VALID_AREAS. "
                    f"See memory '{RUNNER_RECOGNIZED_AREAS_MEMORY}' for "
                    f"context on this drift. Valid areas here: "
                    f"{sorted(VALID_AREAS)}."
                )
            raise SystemExit(f"invalid area: {area} (valid: {sorted(VALID_AREAS)})")
        labels.append(f"area:{area}")
    no_push = bool(getattr(args, "no_push_capability", False))
    if args.cls in PUSH_CAPABLE_CLASSES and not no_push:
        labels.append(f"capability:enable={DEFAULT_PUSH_CAPABILITY}")
    for cap in getattr(args, "capability", None) or []:
        label = f"capability:enable={cap}"
        if label not in labels:
            labels.append(label)
    if args.scope:
        labels.append(f"scope:{args.scope}")
    return labels


def _validate_or_exit(args: argparse.Namespace, description_text: str) -> None:
    labels = _labels(args)
    label_issues = validate(labels, description_text)
    for line in format_issues(label_issues):
        print(line, file=sys.stderr)
    if any(issue.is_error for issue in label_issues) and not args.force:
        raise SystemExit("aborting; pass --force to skip label validation errors")

    warnings = validate_areas_match_description(set(args.areas), description_text)
    if warnings and not args.force:
        for warning in warnings:
            print(f"WARN: {warning}", file=sys.stderr)
        raise SystemExit("aborting; pass --force to skip area validation")
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)


def maybe_inject_hot_file_mutexes(description_text: str) -> str:
    """Run the OP-1124 hot-file mutex auto-inject before filing.

    Pure pass-through if the description references no hot files. When
    injection fires, the rewritten description is what we POST to JIRA
    so the runner's pre-pickup gate sees the new ``mutex_with`` entries.
    """
    result = auto_inject_for_filing(description_text)
    if result.added_mutexes:
        print(
            "INFO: OP-1124 auto-injected mutex_with for hot-file edits: "
            f"{', '.join(result.added_mutexes)}",
            file=sys.stderr,
        )
    return result.description


def file_ticket(args: argparse.Namespace, description_text: str) -> str:
    """POST a Story issue to JIRA and return the created issue key."""
    _validate_or_exit(args, description_text)
    description_text = maybe_inject_hot_file_mutexes(description_text)
    site, project, auth_header = _jira_config(args.cls)
    body = {
        "fields": {
            "project": {"key": project},
            "summary": args.summary,
            "description": parse_description_to_adf(description_text),
            "issuetype": {"name": "Story"},
            "priority": {"name": args.priority},
            "labels": _labels(args),
        }
    }
    resp = _request("POST", site + "/rest/api/3/issue", auth_header, body)
    return resp["key"]


def _print_check(args: argparse.Namespace, description_text: str) -> int:
    _validate_or_exit(args, description_text)
    project = "OP"
    visible = args.tier != "X"
    print("Runner pickup JQL:")
    print(runner_pickup_jql(project, args.cls))
    if visible:
        print("OK - would file runner-visible Story ticket")
        return 0
    print("OK - would file Story ticket, but tier:X is intentionally excluded by runner JQL")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True)
    parser.add_argument("--description-file", required=True)
    parser.add_argument(
        "--priority",
        required=True,
        choices=["Highest", "High", "Medium", "Low", "Lowest"],
    )
    parser.add_argument("--tier", required=True, choices=sorted(VALID_TIERS))
    parser.add_argument("--class", dest="cls", required=True, choices=sorted(VALID_CLASSES))
    parser.add_argument("--type", default="bug", choices=["bug", "feature", "docs", "meta"])
    parser.add_argument("--areas", required=True, type=lambda s: [p.strip() for p in s.split(",") if p.strip()])
    parser.add_argument("--scope", default=None)
    parser.add_argument("--check", action="store_true", help="dry-run, validate, no POST")
    parser.add_argument("--force", action="store_true", help="bypass area-mismatch warning")
    parser.add_argument(
        "--no-push-capability",
        dest="no_push_capability",
        action="store_true",
        help=(
            "opt out of the default capability:enable=gerrit_push label "
            "(applies only to subscription-* classes; api-* classes never "
            "auto-add it)."
        ),
    )
    parser.add_argument(
        "--capability",
        dest="capability",
        default=[],
        type=lambda s: [p.strip() for p in s.split(",") if p.strip()],
        help=(
            "comma-separated extra capability names to enable on the ticket; "
            "each is rendered as capability:enable=<name> on top of the "
            "default set (e.g. --capability run_tests,jira_update)."
        ),
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    description_text = Path(args.description_file).read_text()
    if args.check:
        return _print_check(args, description_text)
    key = file_ticket(args, description_text)
    print(f"created: https://soraapp.atlassian.net/browse/{key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
