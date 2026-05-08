#!/usr/bin/env python3
"""[OP-762] Release milestone readiness checker.

Polls unreleased JIRA fixVersions and emits one structured event per
version:

* ``milestone_ready`` when every promotion gate is green.
* ``milestone_blocked`` with machine-readable reasons when any gate is red.

The integration layer is deliberately thin. Pure gate evaluation is kept
small enough for tests to exercise without live JIRA, Gerrit, or CI access.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol


PUBLISHED_STATUS_NAMES = {"Published", "公開済み"}
GREEN_STATUSES = {"green", "ok", "pass", "passed", "success"}
DEFAULT_PROJECT = "OP"
DEFAULT_AGENT_CLASS = "subscription-codex"
DEFAULT_GERRIT_PROJECT = "omnisight/OmniSight-Productizer"
DEFAULT_CANARY_LOG = Path("/home/user/work/sora/logs/release-milestone/canary-status.jsonl")
DEFAULT_SMOKE_LOG = Path("/home/user/work/sora/logs/release-milestone/smoke-status.jsonl")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def emit_event(event: str, *, version: str, reasons: list[dict[str, Any]] | None = None) -> None:
    record: dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "level": "INFO" if event == "milestone_ready" else "WARN",
        "event": event,
        "fixVersion": version,
    }
    if reasons is not None:
        record["reasons"] = reasons
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


@dataclass(frozen=True)
class FixVersion:
    name: str
    released: bool = False
    archived: bool = False


@dataclass(frozen=True)
class JiraTicket:
    key: str
    status: str


@dataclass(frozen=True)
class GateStatus:
    ok: bool
    detail: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class MilestoneResult:
    version: str
    event: str
    reasons: tuple[dict[str, Any], ...]


class JiraClient(Protocol):
    def open_fix_versions(self) -> list[FixVersion]:
        ...

    def tickets_for_fix_version(self, version: str) -> list[JiraTicket]:
        ...

    def highest_open_affects_tickets(self, version: str) -> list[str]:
        ...


class GerritClient(Protocol):
    def develop_tip(self) -> str:
        ...

    def ticket_merged_on_develop(self, ticket_key: str) -> bool:
        ...


class StatusReader(Protocol):
    def latest(self, suite: str, *, branch: str, revision: str) -> dict[str, Any] | None:
        ...


class AtlassianJiraClient:
    """Minimal JIRA REST client for the milestone gates."""

    def __init__(self, *, base_url: str, email: str, token: str, project: str) -> None:
        self.base_url = base_url.rstrip("/") + "/rest/api/3"
        self.project = project
        raw = f"{email}:{token}".encode()
        self.auth_header = "Basic " + b64encode(raw).decode()

    @classmethod
    def from_env(cls, agent_class: str = DEFAULT_AGENT_CLASS) -> "AtlassianJiraClient":
        suffix = "codex" if agent_class in ("subscription-codex", "api-openai") else "claude"
        cred_dir = Path("~/.config/omnisight").expanduser()
        env_path = cred_dir / f"jira-{suffix}.env"
        token_path = cred_dir / f"jira-{suffix}-token"
        env = _load_env(env_path)
        email_key = f"OMNISIGHT_JIRA_{suffix.upper()}_EMAIL"
        return cls(
            base_url=env["OMNISIGHT_JIRA_SITE_URL"],
            email=env[email_key],
            token=token_path.read_text().strip(),
            project=env.get("OMNISIGHT_JIRA_PROJECT_KEY", DEFAULT_PROJECT),
        )

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace") if exc.fp else ""
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {body_text}") from exc

    def open_fix_versions(self) -> list[FixVersion]:
        payload = self._request("GET", f"/project/{self.project}/versions")
        versions = payload if isinstance(payload, list) else payload.get("values", [])
        out = []
        for row in versions:
            version = FixVersion(
                name=str(row.get("name", "")),
                released=bool(row.get("released", False)),
                archived=bool(row.get("archived", False)),
            )
            if version.name and not version.released and not version.archived:
                out.append(version)
        return out

    def _search(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": fields,
                "maxResults": 500,
            },
        )
        return list(payload.get("issues", []))

    def tickets_for_fix_version(self, version: str) -> list[JiraTicket]:
        jql = f'project = "{self.project}" AND fixVersion = "{version}" ORDER BY key ASC'
        issues = self._search(jql, ["status"])
        return [
            JiraTicket(
                key=str(issue.get("key", "")),
                status=str(((issue.get("fields") or {}).get("status") or {}).get("name", "")),
            )
            for issue in issues
        ]

    def highest_open_affects_tickets(self, version: str) -> list[str]:
        jql = (
            f'project = "{self.project}" '
            f'AND affectedVersion = "{version}" '
            'AND priority >= Highest '
            'AND statusCategory != Done '
            'ORDER BY key ASC'
        )
        return [str(issue.get("key", "")) for issue in self._search(jql, ["key"])]


class SshGerritClient:
    """Gerrit SSH query adapter."""

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
            host=os.environ.get("OMNISIGHT_GERRIT_SSH_HOST", "codex-bot@sora.services"),
            port=int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "29418")),
            key_path=Path(
                os.environ.get(
                    "OMNISIGHT_GIT_SSH_KEY_PATH",
                    "~/.config/omnisight/gerrit-codex-bot-ed25519",
                )
            ).expanduser(),
            project=os.environ.get("OMNISIGHT_GERRIT_PROJECT", DEFAULT_GERRIT_PROJECT),
        )

    def _query(self, query: str, *, timeout: int = 30) -> list[dict[str, Any]]:
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
            query,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        rows: list[dict[str, Any]] = []
        for raw in proc.stdout.splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "stats":
                rows.append(row)
        return rows

    def develop_tip(self) -> str:
        rows = self._query(f'project:{self.project} branch:develop status:merged', timeout=30)
        if not rows:
            raise RuntimeError("Gerrit returned no merged develop changes")
        current = rows[0].get("currentPatchSet") or {}
        revision = str(current.get("revision") or "")
        if not revision:
            raise RuntimeError("Gerrit develop query did not include currentPatchSet.revision")
        return revision

    def ticket_merged_on_develop(self, ticket_key: str) -> bool:
        rows = self._query(
            f'project:{self.project} branch:develop status:merged message:{ticket_key}',
            timeout=30,
        )
        return bool(rows)


class JsonlStatusReader:
    """Reads canary/smoke status JSONL files written by CI jobs."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self.paths = tuple(paths)

    def latest(self, suite: str, *, branch: str, revision: str) -> dict[str, Any] | None:
        latest_record: dict[str, Any] | None = None
        latest_ts: datetime | None = None
        for path in self.paths:
            if not path.exists():
                continue
            for raw in path.read_text().splitlines():
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("suite") or "") != suite:
                    continue
                if str(rec.get("branch") or "") != branch:
                    continue
                rec_revision = str(rec.get("revision") or rec.get("git_sha") or "")
                if rec_revision and rec_revision != revision:
                    continue
                ts = parse_timestamp(rec.get("timestamp")) or datetime.min.replace(
                    tzinfo=timezone.utc
                )
                if latest_ts is None or ts > latest_ts:
                    latest_record = rec
                    latest_ts = ts
        return latest_record


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def evaluate_version(
    version: str,
    *,
    jira: JiraClient,
    gerrit: GerritClient,
    status_reader: StatusReader,
    now: datetime | None = None,
) -> MilestoneResult:
    now = now or utc_now()
    reasons: list[dict[str, Any]] = []

    tickets = jira.tickets_for_fix_version(version)
    if not tickets:
        reasons.append(
            {
                "gate": "jira_fixversion",
                "code": "no_tickets",
                "detail": "fixVersion has no tickets",
            }
        )
    unpublished = [ticket.key for ticket in tickets if ticket.status not in PUBLISHED_STATUS_NAMES]
    if unpublished:
        reasons.append(
            {
                "gate": "jira_fixversion",
                "code": "tickets_not_published",
                "tickets": unpublished,
            }
        )

    develop_tip = gerrit.develop_tip()
    missing_changes = [
        ticket.key for ticket in tickets if not gerrit.ticket_merged_on_develop(ticket.key)
    ]
    if missing_changes:
        reasons.append(
            {
                "gate": "gerrit_develop",
                "code": "missing_merged_change",
                "tickets": missing_changes,
                "develop_tip": develop_tip,
            }
        )

    blockers = jira.highest_open_affects_tickets(version)
    if blockers:
        reasons.append(
            {
                "gate": "jira_blockers",
                "code": "highest_priority_affects_version_open",
                "tickets": blockers,
            }
        )

    canary = check_latest_status(
        status_reader.latest("canary", branch="develop", revision=develop_tip),
        gate="ci_canary",
        revision=develop_tip,
    )
    if not canary.ok:
        reasons.append(canary.evidence)

    smoke = check_latest_status(
        status_reader.latest("smoke", branch="develop", revision=develop_tip),
        gate="smoke_suite",
        revision=develop_tip,
        now=now,
        max_age=timedelta(hours=4),
    )
    if not smoke.ok:
        reasons.append(smoke.evidence)

    event = "milestone_ready" if not reasons else "milestone_blocked"
    return MilestoneResult(version=version, event=event, reasons=tuple(reasons))


def check_latest_status(
    record: dict[str, Any] | None,
    *,
    gate: str,
    revision: str,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> GateStatus:
    if record is None:
        evidence = {
            "gate": gate,
            "code": "missing_status",
            "develop_tip": revision,
        }
        return GateStatus(False, "missing status", evidence)
    status = str(record.get("status") or "").lower()
    if status not in GREEN_STATUSES:
        evidence = {
            "gate": gate,
            "code": "status_not_green",
            "status": record.get("status"),
            "run_id": record.get("run_id"),
            "develop_tip": revision,
        }
        return GateStatus(False, "status not green", evidence)
    if max_age is not None:
        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            evidence = {
                "gate": gate,
                "code": "timestamp_missing",
                "run_id": record.get("run_id"),
                "develop_tip": revision,
            }
            return GateStatus(False, "timestamp missing", evidence)
        if now is None:
            now = utc_now()
        age = now - ts
        if age > max_age:
            evidence = {
                "gate": gate,
                "code": "status_too_old",
                "age_seconds": int(age.total_seconds()),
                "max_age_seconds": int(max_age.total_seconds()),
                "run_id": record.get("run_id"),
                "develop_tip": revision,
            }
            return GateStatus(False, "status too old", evidence)
    return GateStatus(True, "green", {"gate": gate, "run_id": record.get("run_id")})


def check_all(
    *,
    jira: JiraClient,
    gerrit: GerritClient,
    status_reader: StatusReader,
    now: datetime | None = None,
) -> list[MilestoneResult]:
    results: list[MilestoneResult] = []
    for version in jira.open_fix_versions():
        result = evaluate_version(
            version.name,
            jira=jira,
            gerrit=gerrit,
            status_reader=status_reader,
            now=now,
        )
        emit_event(result.event, version=result.version, reasons=list(result.reasons))
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-class",
        default=os.environ.get("OMNISIGHT_JIRA_AGENT_CLASS", DEFAULT_AGENT_CLASS),
    )
    parser.add_argument(
        "--canary-log",
        default=os.environ.get("OMNISIGHT_RELEASE_CANARY_LOG", str(DEFAULT_CANARY_LOG)),
    )
    parser.add_argument(
        "--smoke-log",
        default=os.environ.get("OMNISIGHT_RELEASE_SMOKE_LOG", str(DEFAULT_SMOKE_LOG)),
    )
    args = parser.parse_args(argv)

    jira = AtlassianJiraClient.from_env(args.agent_class)
    gerrit = SshGerritClient.from_env()
    status_reader = JsonlStatusReader([Path(args.canary_log), Path(args.smoke_log)])
    check_all(jira=jira, gerrit=gerrit, status_reader=status_reader)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
