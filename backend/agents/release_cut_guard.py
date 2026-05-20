"""OP-1540 release-cut Gerrit monitoring guard.

Queries Gerrit for open ``branch:main intopic:release-cut`` changes and
alerts when a real release cut has drifted away from the canonical
promotion tag or when the ``release-cut-promote`` submit requirement is
``NOT_APPLICABLE`` on that cut.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from backend.agents import jira_dispatch
from backend.agents.auto_promote_main import PROMOTE_HASHTAGS, utc_now_iso

DEFAULT_GERRIT_PROJECT = "omnisight/OmniSight-Productizer"
DEFAULT_GERRIT_HOST = "codex-bot@sora.services"
DEFAULT_GERRIT_PORT = 29418
DEFAULT_GERRIT_KEY = Path("~/.config/omnisight/gerrit-codex-bot-ed25519").expanduser()
DEFAULT_QUERY_TOPIC = "release-cut"
CANONICAL_RELEASE_CUT_HASHTAG = "milestone:R3-fastforward"
RELEASE_CUT_PROMOTE_REQUIREMENT = "release-cut-promote"

NotifyFn = Callable[[str, str, str], None]


@dataclass(frozen=True)
class ReleaseCutFinding:
    """One operator-visible release-cut guard failure."""

    kind: str
    change: str
    subject: str
    detail: str


@dataclass(frozen=True)
class GuardResult:
    """Single guard run outcome."""

    status: str
    checked: int
    findings: tuple[ReleaseCutFinding, ...]


class SshGerritClient:
    """Small Gerrit SSH query adapter for the release-cut guard."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        key_path: Path,
        project: str = DEFAULT_GERRIT_PROJECT,
    ) -> None:
        self.host = host
        self.port = port
        self.key_path = key_path
        self.project = project

    @classmethod
    def from_env(cls) -> "SshGerritClient":
        return cls(
            host=os.environ.get("OMNISIGHT_GERRIT_SSH_HOST", DEFAULT_GERRIT_HOST),
            port=int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", str(DEFAULT_GERRIT_PORT))),
            key_path=Path(
                os.environ.get("OMNISIGHT_GIT_SSH_KEY_PATH", str(DEFAULT_GERRIT_KEY))
            ).expanduser(),
            project=os.environ.get("OMNISIGHT_GERRIT_PROJECT", DEFAULT_GERRIT_PROJECT),
        )

    def query(self, query: str, *, timeout: int = 30) -> list[dict[str, Any]]:
        cmd = [
            "ssh",
            "-i",
            str(self.key_path),
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            self.host,
            "gerrit",
            "query",
            "--format=JSON",
            "--current-patch-set",
            "--submit-requirements",
            query,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        rows: list[dict[str, Any]] = []
        for raw in proc.stdout.splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("type") != "stats":
                rows.append(row)
        return rows


def _default_notify(channel: str, severity: str, detail: str) -> None:
    jira_dispatch.notify_operator(channel=channel, severity=severity, detail=detail)


def build_release_cut_query(*, project: str, topic: str = DEFAULT_QUERY_TOPIC) -> str:
    """Return the Gerrit query OP-1540 guards."""

    return f"status:open project:{project} branch:main intopic:{topic}"


def _change_label(change: dict[str, Any]) -> str:
    return str(
        change.get("number")
        or change.get("_number")
        or change.get("id")
        or change.get("change_id")
        or change.get("changeId")
        or "(unknown)"
    )


def _subject(change: dict[str, Any]) -> str:
    return str(change.get("subject") or change.get("commitMessage") or "").splitlines()[0]


def _hashtags(change: dict[str, Any]) -> set[str]:
    raw = change.get("hashtags") or change.get("hashtagsJson") or ()
    if isinstance(raw, str):
        return {tag.strip() for tag in raw.split(",") if tag.strip()}
    if isinstance(raw, dict):
        return {str(tag).strip() for tag in raw if str(tag).strip()}
    if isinstance(raw, Iterable):
        return {str(tag).strip() for tag in raw if str(tag).strip()}
    return set()


def _submit_requirements(change: dict[str, Any]) -> Sequence[dict[str, Any]]:
    raw = change.get("submitRequirements") or change.get("submit_requirements") or ()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(req for req in raw if isinstance(req, dict))
    return ()


def _requirement_status(req: dict[str, Any]) -> str:
    status = req.get("status")
    if not status and isinstance(req.get("applicabilityExpressionResult"), dict):
        status = req["applicabilityExpressionResult"].get("status")
    return str(status or "").strip().upper()


def release_cut_findings(
    changes: Sequence[dict[str, Any]],
    *,
    canonical_hashtag: str = CANONICAL_RELEASE_CUT_HASHTAG,
    promote_requirement: str = RELEASE_CUT_PROMOTE_REQUIREMENT,
) -> tuple[ReleaseCutFinding, ...]:
    """Return all OP-1540 release-cut guard findings for ``changes``."""

    findings: list[ReleaseCutFinding] = []
    for change in changes:
        label = _change_label(change)
        subject = _subject(change)
        tags = _hashtags(change)
        if canonical_hashtag not in tags:
            findings.append(
                ReleaseCutFinding(
                    kind="missing_canonical_hashtag",
                    change=label,
                    subject=subject,
                    detail=(
                        f"branch:main intopic:release-cut change lacks "
                        f"{canonical_hashtag!r}; observed hashtags={sorted(tags)!r}"
                    ),
                )
            )
        for req in _submit_requirements(change):
            name = str(req.get("name") or req.get("requirement") or "").strip()
            if name == promote_requirement and _requirement_status(req) == "NOT_APPLICABLE":
                findings.append(
                    ReleaseCutFinding(
                        kind="promote_requirement_not_applicable",
                        change=label,
                        subject=subject,
                        detail=f"{promote_requirement} resolved NOT_APPLICABLE on a real cut",
                    )
                )
    return tuple(findings)


def _format_alert(findings: Sequence[ReleaseCutFinding]) -> str:
    lines = [
        "OP-1540 release-cut guard fired:",
        f"findings={len(findings)}",
    ]
    for finding in findings:
        lines.append(
            f"- {finding.kind}: change={finding.change} "
            f"subject={finding.subject!r} detail={finding.detail}"
        )
    return "\n".join(lines)


def run_guard(
    *,
    client: SshGerritClient,
    notify: NotifyFn = _default_notify,
    channel: str = "release-cut-guard",
    severity: str = "critical",
    topic: str = DEFAULT_QUERY_TOPIC,
    canonical_hashtag: str = CANONICAL_RELEASE_CUT_HASHTAG,
    promote_requirement: str = RELEASE_CUT_PROMOTE_REQUIREMENT,
    timeout: int = 30,
) -> GuardResult:
    query = build_release_cut_query(project=client.project, topic=topic)
    changes = client.query(query, timeout=timeout)
    findings = release_cut_findings(
        changes,
        canonical_hashtag=canonical_hashtag,
        promote_requirement=promote_requirement,
    )
    if findings:
        notify(channel, severity, _format_alert(findings))
        return GuardResult("alerted", len(changes), findings)
    return GuardResult("quiet", len(changes), ())


def _emit_result(result: GuardResult) -> None:
    print(
        json.dumps(
            {
                "timestamp": utc_now_iso(),
                "event": "release_cut_guard",
                "status": result.status,
                "checked": result.checked,
                "findings": [finding.__dict__ for finding in result.findings],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def _self_test() -> None:
    mis_tagged = {
        "number": "1540",
        "subject": "[release-cut v9.9.9] Merge develop into main",
        "hashtags": ["auto-promote"],
        "submitRequirements": [{"name": RELEASE_CUT_PROMOTE_REQUIREMENT, "status": "SATISFIED"}],
    }
    correct = {
        "number": "1541",
        "subject": "[release-cut v9.9.10] Merge develop into main",
        "hashtags": list(PROMOTE_HASHTAGS),
        "submitRequirements": [{"name": RELEASE_CUT_PROMOTE_REQUIREMENT, "status": "SATISFIED"}],
    }
    c6_negative_synthetic = {
        "number": "1542",
        "subject": "[release-cut v9.9.11] Merge develop into main",
        "hashtags": list(PROMOTE_HASHTAGS),
        "submitRequirements": [
            {"name": RELEASE_CUT_PROMOTE_REQUIREMENT, "status": "NOT_APPLICABLE"}
        ],
    }
    assert len(release_cut_findings([mis_tagged])) == 1
    assert release_cut_findings([correct]) == ()
    assert len(release_cut_findings([c6_negative_synthetic])) == 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("OMNISIGHT_GERRIT_PROJECT", DEFAULT_GERRIT_PROJECT))
    parser.add_argument("--host", default=os.environ.get("OMNISIGHT_GERRIT_SSH_HOST", DEFAULT_GERRIT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", str(DEFAULT_GERRIT_PORT))))
    parser.add_argument("--key-path", type=Path, default=Path(os.environ.get("OMNISIGHT_GIT_SSH_KEY_PATH", str(DEFAULT_GERRIT_KEY))).expanduser())
    parser.add_argument("--topic", default=DEFAULT_QUERY_TOPIC)
    parser.add_argument("--canonical-hashtag", default=CANONICAL_RELEASE_CUT_HASHTAG)
    parser.add_argument("--promote-requirement", default=RELEASE_CUT_PROMOTE_REQUIREMENT)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        print("release_cut_guard self-test passed", flush=True)
        return 0

    result = run_guard(
        client=SshGerritClient(
            host=args.host,
            port=args.port,
            key_path=args.key_path,
            project=args.project,
        ),
        topic=args.topic,
        canonical_hashtag=args.canonical_hashtag,
        promote_requirement=args.promote_requirement,
        timeout=args.timeout,
    )
    _emit_result(result)
    return 2 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
