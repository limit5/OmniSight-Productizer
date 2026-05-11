"""OP-868 release milestone query and acceptance helpers.

The scripts keep external I/O thin and call the pure functions in this
module for milestone readiness decisions. That keeps the JIRA/Gerrit
surface mockable while preserving one shared error catalog.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa


ACCEPTED_STATUSES = frozenset({"公開済み", "Archived"})
FORCE_ACCEPT_LABEL = "milestone:force-accept"

ERR_ALREADY_EXISTS = "MilestoneAlreadyExists"
ERR_VERIFIED_LABEL_MISSING = "MilestoneVerifiedLabelMissing"
ERR_OPEN_BLOCKER_EXISTS = "MilestoneOpenBlockerExists"


@dataclass(frozen=True)
class MilestoneTicket:
    key: str
    status: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifiedRun:
    ticket_key: str
    commit: str
    state: str
    label_present: bool = True


@dataclass(frozen=True)
class MilestoneEvaluation:
    version: str
    ready: bool
    status: str
    exit_code: int
    errors: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]
    force_accept: bool
    tickets: tuple[MilestoneTicket, ...]
    blockers: tuple[MilestoneTicket, ...]
    verified_runs: tuple[VerifiedRun, ...]

    def to_report(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ready": self.ready,
            "status": self.status,
            "exit_code": self.exit_code,
            "force_accept": self.force_accept,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "tickets": [
                {"key": ticket.key, "status": ticket.status, "labels": list(ticket.labels)}
                for ticket in self.tickets
            ],
            "blockers": [
                {"key": ticket.key, "status": ticket.status, "labels": list(ticket.labels)}
                for ticket in self.blockers
            ],
            "verified_runs": [
                {
                    "ticket_key": run.ticket_key,
                    "commit": run.commit,
                    "state": run.state,
                    "label_present": run.label_present,
                }
                for run in self.verified_runs
            ],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


def evaluate_milestone(
    version: str,
    *,
    tickets: Iterable[MilestoneTicket],
    blockers: Iterable[MilestoneTicket],
    verified_runs: Iterable[VerifiedRun],
    verified_required: bool = True,
) -> MilestoneEvaluation:
    ticket_rows = tuple(tickets)
    blocker_rows = tuple(blockers)
    verified_rows = tuple(verified_runs)
    labels = {label for ticket in ticket_rows for label in ticket.labels}
    force_accept = FORCE_ACCEPT_LABEL in labels
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    not_done = [
        {"key": ticket.key, "status": ticket.status}
        for ticket in ticket_rows
        if ticket.status not in ACCEPTED_STATUSES
    ]
    if not_done:
        errors.append(
            {
                "code": "MilestoneTicketsNotPublished",
                "detail": "all fixVersion tickets must be 公開済み or Archived",
                "tickets": not_done,
            }
        )

    if blocker_rows:
        errors.append(
            {
                "code": ERR_OPEN_BLOCKER_EXISTS,
                "detail": "open BLOCKER tickets remain in this milestone",
                "tickets": [ticket.key for ticket in blocker_rows],
            }
        )

    if not verified_required:
        warnings.append(
            {
                "code": ERR_VERIFIED_LABEL_MISSING,
                "detail": "OP-739 Verified label is unavailable; acceptance remains TODO",
                "todo": "Wire required Verified-label CI runs after OP-739 ships.",
            }
        )
    elif not force_accept:
        missing_label = [run for run in verified_rows if not run.label_present]
        red = [run for run in verified_rows if run.label_present and run.state.lower() != "green"]
        if missing_label:
            errors.append(
                {
                    "code": ERR_VERIFIED_LABEL_MISSING,
                    "detail": "Gerrit returned merged commits without a Verified label",
                    "tickets": [run.ticket_key for run in missing_label],
                }
            )
        if red:
            errors.append(
                {
                    "code": "MilestoneVerifiedRunNotGreen",
                    "detail": "required Verified-label CI runs are not green",
                    "runs": [
                        {"ticket_key": run.ticket_key, "commit": run.commit, "state": run.state}
                        for run in red
                    ],
                }
            )

    if force_accept:
        warnings.append(
            {
                "code": "MilestoneForceAcceptOverride",
                "detail": f"{FORCE_ACCEPT_LABEL} present; Verified-label requirement skipped",
            }
        )

    ready = not errors and not any(w["code"] == ERR_VERIFIED_LABEL_MISSING for w in warnings)
    status = "force_accepted" if ready and force_accept else ("ready" if ready else "not_ready")
    return MilestoneEvaluation(
        version=version,
        ready=ready,
        status=status,
        exit_code=0 if ready else 1,
        errors=tuple(errors),
        warnings=tuple(warnings),
        force_accept=force_accept,
        tickets=ticket_rows,
        blockers=blocker_rows,
        verified_runs=verified_rows,
    )


def sqlalchemy_url_from_env(default: str = "sqlite:///release_milestones.db") -> str:
    return os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL") or default


def create_engine(url: str | None = None) -> sa.Engine:
    return sa.create_engine(url or sqlalchemy_url_from_env(), future=True)


def ensure_release_milestones_table_for_dry_run(engine: sa.Engine) -> None:
    """Create the OP-868 table in disposable dry-run SQLite databases.

    Production uses Alembic. This helper exists so operator dry-runs can
    prove the scripts end-to-end without mutating a live database.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS release_milestones ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "version TEXT NOT NULL UNIQUE, "
                "jira_version_id TEXT NOT NULL, "
                "jira_project_key TEXT NOT NULL DEFAULT 'OP', "
                "status TEXT NOT NULL DEFAULT 'defined', "
                "last_report_json TEXT, "
                "last_checked_at TEXT, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )


class ReleaseMilestoneStore:
    def __init__(self, engine: sa.Engine) -> None:
        self.engine = engine

    def create(self, *, version: str, jira_version_id: str, project_key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO release_milestones "
                    "(version, jira_version_id, jira_project_key) "
                    "VALUES (:version, :jira_version_id, :project_key)"
                ),
                {
                    "version": version,
                    "jira_version_id": jira_version_id,
                    "project_key": project_key,
                },
            )

    def exists(self, version: str) -> bool:
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM release_milestones WHERE version = :version"),
                {"version": version},
            ).first()
            return row is not None

    def current_next_versions(self) -> list[str]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT version FROM release_milestones "
                    "WHERE status IN ('defined','not_ready','error') "
                    "ORDER BY created_at ASC, id ASC LIMIT 2"
                )
            ).all()
            return [str(row[0]) for row in rows]

    def update_status(self, version: str, *, status: str, report: dict[str, Any]) -> str | None:
        report_json = json.dumps(report, ensure_ascii=False, sort_keys=True)
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT status FROM release_milestones WHERE version = :version"),
                {"version": version},
            ).first()
            previous = str(row[0]) if row else None
            conn.execute(
                sa.text(
                    "UPDATE release_milestones "
                    "SET status = :status, last_report_json = :report, "
                    "last_checked_at = :checked_at, updated_at = :checked_at "
                    "WHERE version = :version"
                ),
                {
                    "version": version,
                    "status": status,
                    "report": report_json,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return previous


def write_json_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
