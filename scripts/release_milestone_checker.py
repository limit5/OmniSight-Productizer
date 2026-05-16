#!/usr/bin/env python3
"""[OP-762] Release milestone readiness checker.

Polls unreleased JIRA fixVersions and emits one structured event per
version:

* ``milestone_ready`` when every promotion gate is green.
* ``milestone_blocked`` with machine-readable reasons when any gate is red.
* ``milestone_force_promoted`` (ADR-0019 / OP-967 AUDIT-18b) when gates
  are red **but** the fixVersion carries the operator emergency-override
  label ``release:force-promote``. The original blockers ride along in
  ``reasons`` so the audit trail records exactly what was bypassed; when
  gates are actually green the override is a no-op (plain ``milestone_ready``).
* ``LabelInvalid`` when the fixVersion carries a near-miss of the override
  label (e.g. ``release:force_promote``) — rejected, not silently honored.

The integration layer is deliberately thin. Pure gate evaluation is kept
small enough for tests to exercise without live JIRA, Gerrit, or CI access.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

# ── ADR-0019 operator force-promote override ───────────────────────────
# The one valid spelling of the fixVersion label (frozen wire contract).
FORCE_PROMOTE_LABEL = "release:force-promote"
# Subtle misspellings an operator might type instead — ``release:force_promote``
# and friends. Anything that *looks* like the override but isn't the frozen
# form is rejected (``LabelInvalid``), not silently honored — mirrors the
# AUDIT-13a ``release:force_create`` rejection in release_conductor_cron.sh.
_FORCE_PROMOTE_TYPO_RE = re.compile(r"^release:force[-_]?promote$")
# Same label surface release_conductor_cron.sh::query_fix_version_labels
# reads: the literal ``labels`` array plus ``release:*`` tokens embedded in
# the version description.
_RELEASE_LABEL_RE = re.compile(r"release:[A-Za-z0-9_-]+")

DEFAULT_PROJECT = "OP"
DEFAULT_AGENT_CLASS = "subscription-codex"
DEFAULT_GERRIT_PROJECT = "omnisight/OmniSight-Productizer"
DEFAULT_CANARY_LOG = Path("/home/user/work/sora/logs/release-milestone/canary-status.jsonl")
DEFAULT_SMOKE_LOG = Path("/home/user/work/sora/logs/release-milestone/smoke-status.jsonl")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _warn(message: str) -> None:
    """Emit an operator-facing warning to stderr (the systemd journal)."""
    print(f"{utc_now_iso()} WARN {message}", file=sys.stderr, flush=True)


def parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def emit_event(
    event: str,
    *,
    version: str,
    reasons: list[dict[str, Any]] | None = None,
    operator_override: bool = False,
) -> None:
    record: dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "level": "INFO" if event == "milestone_ready" else "WARN",
        "event": event,
        "fixVersion": version,
    }
    if reasons is not None:
        record["reasons"] = reasons
    if operator_override:
        # ADR-0019: flag the abnormal/forced outcome so consumers treat it
        # as green for control flow but as a bypassed gate for audit/display.
        record["operator_override"] = True
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
    operator_override: bool = False


class JiraClient(Protocol):
    def open_fix_versions(self) -> list[FixVersion]:
        ...

    def tickets_for_fix_version(self, version: str) -> list[JiraTicket]:
        ...

    def highest_open_affects_tickets(self, version: str) -> list[str]:
        ...

    def fix_version_labels(self, version: str) -> list[str]:
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

    def fix_version_labels(self, version: str) -> list[str]:
        payload = self._request("GET", f"/project/{self.project}/versions")
        rows = payload if isinstance(payload, list) else payload.get("values", [])
        return sorted(parse_fix_version_labels(rows, version))


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
        # ``--current-patch-set`` is REQUIRED: Gerrit 3.13's ``gerrit query``
        # no longer emits the ``currentPatchSet`` block by default (pre-3.13 it
        # did), and ``develop_tip()`` needs ``currentPatchSet.revision``.
        # See OP-959 / GerritAPIShapeChange313.
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
            raise RuntimeError(
                "Gerrit develop query did not include currentPatchSet.revision "
                "(expected `gerrit query --current-patch-set`; see OP-959)"
            )
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


def parse_fix_version_labels(version_rows: Iterable[dict[str, Any]], version: str) -> set[str]:
    """Extract the ``release:*`` label set for *version* from a JIRA versions payload.

    Shared "small util" (OP-967 AC #1) extracted from
    ``release_conductor_cron.sh::query_fix_version_labels`` so the conductor
    cron and the milestone checker read the *same* label surface: a
    version's labels are the union of its ``labels`` array and any
    ``release:*`` tokens embedded in its free-text ``description``.
    """
    labels: set[str] = set()
    for row in version_rows:
        if str(row.get("name") or "") != version:
            continue
        raw_labels = row.get("labels") or []
        if isinstance(raw_labels, list):
            labels.update(str(label) for label in raw_labels if label)
        description = str(row.get("description") or "")
        labels.update(_RELEASE_LABEL_RE.findall(description))
        break
    return labels


def resolve_force_promote(labels: Iterable[str], version: str) -> MilestoneResult | bool:
    """Interpret a fixVersion label set against the ADR-0019 override contract.

    Returns ``True`` when ``release:force-promote`` is present, ``False``
    when no override applies, and a ``LabelInvalid`` :class:`MilestoneResult`
    when a near-miss spelling (``release:force_promote`` …) is present —
    the caller should emit that result verbatim and act on nothing else.
    """
    label_set = set(labels)
    collisions = sorted(
        label
        for label in label_set
        if label != FORCE_PROMOTE_LABEL and _FORCE_PROMOTE_TYPO_RE.match(label)
    )
    if collisions:
        return MilestoneResult(
            version=version,
            event="LabelInvalid",
            reasons=(
                {
                    "gate": "operator_label",
                    "code": "label_format_collision",
                    "labels": collisions,
                    "detail": (
                        f"fixVersion label(s) {collisions} resemble {FORCE_PROMOTE_LABEL!r} "
                        "but are not the frozen wire form; fix the label on the JIRA version"
                    ),
                },
            ),
        )
    return FORCE_PROMOTE_LABEL in label_set


def evaluate_version(
    version: str,
    *,
    jira: JiraClient,
    gerrit: GerritClient,
    status_reader: StatusReader,
    now: datetime | None = None,
) -> MilestoneResult:
    now = now or utc_now()

    # ── ADR-0019 operator force-promote override ───────────────────────
    # The fixVersion label set is read *now*, at gate-evaluation time —
    # never a value cached at META-creation time: operators add
    # ``release:force-promote`` mid-chain, after a gate has gone red.
    force_promote = False
    try:
        labels = list(jira.fix_version_labels(version))
    except Exception as exc:  # LabelLookupFailedDuringForce — fail closed
        _warn(
            f"LabelLookupFailedDuringForce: {version}: "
            f"{type(exc).__name__}: {exc} — treating as no override, normal gating applies"
        )
    else:
        decision = resolve_force_promote(labels, version)
        if isinstance(decision, MilestoneResult):
            # LabelFormatCollision -> LabelInvalid; do not act on anything else.
            return decision
        force_promote = decision

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

    if not reasons:
        # Gates green — any override is a no-op; emit the normal ready event
        # with no warning block so the warning stays meaningful (it appears
        # only when something was actually bypassed). ADR-0019 §"Emit".
        return MilestoneResult(version=version, event="milestone_ready", reasons=())
    if force_promote:
        # Gates red but the operator override is set: emit the
        # green-equivalent ``milestone_force_promoted`` carrying the
        # original blockers, instead of ``milestone_blocked`` (ADR-0019).
        return MilestoneResult(
            version=version,
            event="milestone_force_promoted",
            reasons=tuple(reasons),
            operator_override=True,
        )
    return MilestoneResult(version=version, event="milestone_blocked", reasons=tuple(reasons))


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
        emit_event(
            result.event,
            version=result.version,
            reasons=list(result.reasons),
            operator_override=result.operator_override,
        )
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
