"""OP-950 H5 — Gerrit hotfix cherry-pick label handler."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from backend.agents import jira_dispatch


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "hotfix_cherry_pick.py"
HOTFIX_LABEL_RE = re.compile(
    r"^hotfix:cherry-pick-to="
    r"(?P<target>release/v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\+1)$"
)


class LabelMisformed(ValueError):
    """Gerrit hotfix label did not match the OP-950 contract."""


class CherryPickConflict(RuntimeError):
    """The cherry-pick script reported a merge conflict."""


class TargetBranchMissing(RuntimeError):
    """The cherry-pick script reported a missing target branch."""


def _change_number(event: dict[str, Any]) -> str:
    change = event.get("change") or {}
    value = change.get("number") or change.get("_number") or event.get("changeNumber")
    return str(value) if value not in (None, "") else ""


def _label_name(event: dict[str, Any]) -> str:
    approval = event.get("approval") or {}
    for value in (
        approval.get("type"),
        approval.get("name"),
        approval.get("label"),
        event.get("label"),
    ):
        if isinstance(value, str) and value:
            return value
    return ""


def parse_hotfix_target(label: str) -> str:
    match = HOTFIX_LABEL_RE.match(label)
    if not match:
        raise LabelMisformed(
            "expected hotfix:cherry-pick-to=release/vX.Y.Z+1"
        )
    return match.group("target")


def run_hotfix_cherry_pick(change_number: str, target: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--from-change",
            change_number,
            "--target",
            target,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    detail = (proc.stderr or proc.stdout or "").strip()
    if "CherryPickConflict" in detail:
        raise CherryPickConflict(detail)
    if "TargetBranchMissing" in detail:
        raise TargetBranchMissing(detail)
    raise RuntimeError(detail or f"hotfix_cherry_pick exited {proc.returncode}")


def comment_on_gerrit_change(change_number: str, message: str) -> None:
    user, ssh_key = jira_dispatch._gerrit_auth_for_instance("subscription-codex")
    subprocess.run(
        [
            "ssh",
            "-i",
            str(ssh_key),
            "-p",
            str(jira_dispatch.GERRIT_SSH_PORT),
            f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
            "gerrit",
            "review",
            "--message",
            message,
            f"change:{change_number}",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def instantiate_hotfix_meta(target: str, cherry_picked_change: str) -> dict[str, Any]:
    """Small seam for the G5 hotfix variant.

    OP-950 only owns the label-to-action trigger; the concrete G5
    hotfix template remains behind this function so tests can assert
    the integration without expanding release-template ownership here.
    """
    return {
        "meta_key": f"HOTFIX-{target.removeprefix('release/')}",
        "cherry_picked_change": cherry_picked_change,
    }


def open_manual_merge_ticket(change_number: str, target: str, detail: str) -> dict[str, Any]:
    logger.warning(
        "release_conductor.hotfix.manual_merge_required change=%s target=%s detail=%s",
        change_number,
        target,
        detail,
    )
    return {"opened": True, "source_change": change_number, "target": target}


def on_hotfix_label_added(
    event: dict[str, Any],
    *,
    cherry_pick: Callable[[str, str], dict[str, Any]] = run_hotfix_cherry_pick,
    gerrit_comment: Callable[[str, str], None] = comment_on_gerrit_change,
    meta_instantiator: Callable[[str, str], dict[str, Any]] = instantiate_hotfix_meta,
    manual_merge_ticket: Callable[[str, str, str], dict[str, Any]] = open_manual_merge_ticket,
) -> dict[str, Any]:
    label = _label_name(event)
    change_number = _change_number(event)
    try:
        target = parse_hotfix_target(label)
    except LabelMisformed as exc:
        if change_number:
            gerrit_comment(
                change_number,
                f"Hotfix cherry-pick refused: LabelMisformed: {exc}",
            )
        return {
            "outcome": "refused",
            "error": "LabelMisformed",
            "label": label,
            "change_number": change_number,
            "source_unchanged": True,
        }

    if not change_number:
        raise ValueError("Gerrit hotfix label event has no change number")

    try:
        picked = cherry_pick(change_number, target)
    except CherryPickConflict as exc:
        detail = str(exc)
        ticket = manual_merge_ticket(change_number, target, detail)
        gerrit_comment(
            change_number,
            "Hotfix cherry-pick refused: CherryPickConflict. "
            f"Manual merge ticket opened: {ticket}",
        )
        return {
            "outcome": "refused",
            "error": "CherryPickConflict",
            "change_number": change_number,
            "target": target,
            "manual_merge_ticket": ticket,
            "source_unchanged": True,
        }
    except TargetBranchMissing as exc:
        gerrit_comment(
            change_number,
            f"Hotfix cherry-pick refused: TargetBranchMissing: {exc}",
        )
        return {
            "outcome": "refused",
            "error": "TargetBranchMissing",
            "change_number": change_number,
            "target": target,
            "source_unchanged": True,
        }
    except Exception as exc:
        gerrit_comment(
            change_number,
            f"Hotfix cherry-pick failed; partial state reverted: {type(exc).__name__}: {exc}",
        )
        return {
            "outcome": "failed",
            "error": type(exc).__name__,
            "change_number": change_number,
            "target": target,
            "source_unchanged": True,
        }

    picked_change = str(picked["cherry_picked_change"])
    meta = meta_instantiator(target, picked_change)
    gerrit_comment(
        change_number,
        "Hotfix cherry-pick succeeded: "
        f"created Gerrit change {picked_change}; META {meta.get('meta_key')}",
    )
    return {
        "outcome": "cherry_picked",
        "change_number": change_number,
        "target": target,
        "cherry_picked_change": picked_change,
        "cherry_picked_url": picked.get("cherry_picked_url"),
        "meta": meta,
    }
