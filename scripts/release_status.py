#!/usr/bin/env python3
"""OP-938 (G2) — Release status query.

Report the current state of a release META: which R-child is in
progress, which are complete, which remain blocked. The script is
strictly read-only — it does no JIRA writes — so it composes safely
with the G1 idempotency guard and with cron-driven health checks.

Usage::

    scripts/release_status.py --version v0.5.1-rc1
    scripts/release_status.py --version v0.5.1-rc1 --json

Exit codes::

    0 — status reported (META exists; children walked)
    1 — no META exists for --version (caller can branch on this)
    2 — JQL fetch failed (transient JIRA outage)
    3 — state ambiguous (multiple METAs share the same version label)

Error catalog
-------------
* ``JQLQueryFailed``   — JIRA REST search blew up; fail closed
* ``StateAmbiguous``   — multiple METAs for the same version label;
  alert P0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch  # noqa: E402

# Reuse the G1 engine's error classes + JQL helper so both scripts
# stay in lockstep with the same idempotency contract.
import importlib.util as _il  # noqa: E402

_ENGINE_PATH = REPO_ROOT / "scripts" / "instantiate_release_meta.py"
_spec = _il.spec_from_file_location("instantiate_release_meta", _ENGINE_PATH)
_engine = _il.module_from_spec(_spec)
sys.modules.setdefault("instantiate_release_meta", _engine)
_spec.loader.exec_module(_engine)  # type: ignore[union-attr]

JQLQueryFailed = _engine.JQLQueryFailed
StateAmbiguous = _engine.StateAmbiguous
EXPECTED_CHILD_COUNT = _engine.EXPECTED_CHILD_COUNT
ARCHIVED_STATES = _engine.ARCHIVED_STATES

DEFAULT_AGENT_CLASS = _engine.DEFAULT_AGENT_CLASS

EXIT_OK = 0
EXIT_NO_SUCH_VERSION = 1
EXIT_JQL_QUERY_FAILED = 2
EXIT_STATE_AMBIGUOUS = 3

# Status sets — keep aligned with backend.agents.jira_dispatch so we
# never drift on the published/in-progress terminology a single
# operator-language change.
PUBLISHED_STATUS_NAMES = {"Published", "公開済み"}
IN_PROGRESS_STATUS_NAMES = {"In Progress", "進行中"}
UNDER_REVIEW_STATUS_NAMES = {"Under Review"}
APPROVED_STATUS_NAMES = {"Approved", "承認済み"}
TODO_STATUS_NAMES = {"To Do"}

CHILD_R_ID_LABEL_RE = re.compile(r"^release-child:(R([1-9]|1[0-3]))$")


# ── Data classes ────────────────────────────────────────────────────


def _classify(status: str) -> str:
    """Bucket a JIRA status into a coarse state for status reports."""
    if status in PUBLISHED_STATUS_NAMES:
        return "published"
    if status in ARCHIVED_STATES:
        return "archived"
    if status in APPROVED_STATUS_NAMES:
        return "approved"
    if status in UNDER_REVIEW_STATUS_NAMES:
        return "under_review"
    if status in IN_PROGRESS_STATUS_NAMES:
        return "in_progress"
    if status in TODO_STATUS_NAMES:
        return "todo"
    return "unknown"


def _is_done(status: str) -> bool:
    return status in PUBLISHED_STATUS_NAMES or status in ARCHIVED_STATES


def _is_active(status: str) -> bool:
    return status in IN_PROGRESS_STATUS_NAMES or status in UNDER_REVIEW_STATUS_NAMES


# ── JIRA fetch ──────────────────────────────────────────────────────


def fetch_children(
    client: jira_dispatch.DispatchClient,
    meta_key: str,
    version: str,
) -> list[dict[str, Any]]:
    """Return all release-child:Rn children for a META (one JQL call).

    Children share the ``RELEASE-{version}`` label with their META
    plus a ``release-child:R<idx>`` discriminator. We scope to
    ``RELEASE-{version}`` so this works equally well for hotfix or rc
    flavours; the META is filtered out via ``meta:release``.
    """
    label = f"RELEASE-{version}"
    jql = (
        f'project = "{client.project_key}" '
        f'AND labels = "{label}" '
        f'AND labels != "meta:release"'
    )
    issues = _engine._run_jql(
        client, jql, fields=["summary", "status", "labels"], max_results=50,
    )
    out: list[dict[str, Any]] = []
    for issue in issues:
        fields = issue.get("fields") or {}
        labels = fields.get("labels") or []
        r_id = None
        for label_value in labels:
            m = CHILD_R_ID_LABEL_RE.match(label_value or "")
            if m:
                r_id = m.group(1)
                break
        if r_id is None:
            # Tagged with RELEASE-{version} but no release-child:Rn
            # marker — skip so we never confuse status reporting.
            continue
        status = (fields.get("status") or {}).get("name") or ""
        out.append({
            "r_id": r_id,
            "key": issue.get("key"),
            "summary": fields.get("summary") or "",
            "status": status,
        })
    # Stable order R1..R13
    out.sort(key=lambda row: int(row["r_id"][1:]))
    return out


# ── Reporting ───────────────────────────────────────────────────────


def build_report(
    version: str,
    meta_key: str,
    meta_status: str,
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute the structured status report.

    ``in_progress`` is the *cursor*: the first child whose status is
    not yet done. We surface every child's status so the operator can
    spot anomalies (e.g. a child stuck in Under Review while later
    children moved ahead).
    """
    in_progress_r_id: str | None = None
    completed: list[str] = []
    pending: list[str] = []
    for child in children:
        if _is_done(child["status"]):
            completed.append(child["r_id"])
        else:
            if in_progress_r_id is None:
                in_progress_r_id = child["r_id"]
            pending.append(child["r_id"])

    return {
        "version": version,
        "meta": {"key": meta_key, "status": meta_status},
        "child_count": len(children),
        "expected_child_count": EXPECTED_CHILD_COUNT,
        "in_progress": in_progress_r_id,
        "completed": completed,
        "pending": pending,
        "children": [
            {
                "r_id": c["r_id"],
                "key": c["key"],
                "status": c["status"],
                "bucket": _classify(c["status"]),
            }
            for c in children
        ],
    }


def render_text_report(report: dict[str, Any]) -> str:
    lines = [
        f"Release status — {report['version']}",
        f"  META:           {report['meta']['key']} (status={report['meta']['status']!r})",
        f"  child count:    {report['child_count']} / {report['expected_child_count']}",
    ]
    cursor = report["in_progress"]
    if cursor is None:
        lines.append("  in progress:    (none — all children done)")
    else:
        lines.append(f"  in progress:    {cursor}")
    lines.append(f"  completed:      {', '.join(report['completed']) or '(none)'}")
    lines.append(f"  pending:        {', '.join(report['pending']) or '(none)'}")
    lines.append("")
    lines.append("  Child detail:")
    for child in report["children"]:
        lines.append(
            f"    {child['r_id']:>3}  {child['key']:<10} "
            f"status={child['status']!r:<20} bucket={child['bucket']}"
        )
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        help="SemVer release version, e.g. v0.5.1 or v0.5.1-rc1",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of human-readable text",
    )
    parser.add_argument(
        "--agent-class",
        default=DEFAULT_AGENT_CLASS,
        help="Runner class to authenticate as for the JIRA REST calls",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _engine.validate_version(args.version)

    client = jira_dispatch.make_client(args.agent_class)

    try:
        matches = _engine.find_existing_meta_matches(client, args.version)
    except JQLQueryFailed as exc:
        print(f"JQLQueryFailed: {exc}", file=sys.stderr)
        return EXIT_JQL_QUERY_FAILED

    if not matches:
        print(
            f"no META exists for {args.version!r} (label RELEASE-{args.version} "
            f"and HOTFIX-{args.version}+* both empty).",
            file=sys.stderr,
        )
        return EXIT_NO_SUCH_VERSION

    # The status query is more permissive than the create-time guard:
    # if one of the matches is Archived and the rest are absent, that
    # is still a meaningful state to report. Only refuse on genuine
    # P0 ambiguity (more than one *active* META share the version).
    active = [m for m in matches if m[1] not in ARCHIVED_STATES]
    if len(active) > 1:
        keys = ", ".join(f"{k}(status={s!r})" for k, s in active)
        print(
            f"StateAmbiguous: multiple active METAs for version {args.version!r}: "
            f"[{keys}]. Reconcile manually.",
            file=sys.stderr,
        )
        return EXIT_STATE_AMBIGUOUS

    # Pick the active META if there is one, otherwise fall back to the
    # newest archived META (matches are returned with RELEASE first,
    # then HOTFIX; index order is deterministic per find call).
    meta_key, meta_status = (active[0] if active else matches[0])

    try:
        children = fetch_children(client, meta_key, args.version)
    except JQLQueryFailed as exc:
        print(f"JQLQueryFailed: {exc}", file=sys.stderr)
        return EXIT_JQL_QUERY_FAILED

    report = build_report(args.version, meta_key, meta_status, children)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text_report(report))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
