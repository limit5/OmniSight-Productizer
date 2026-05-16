#!/usr/bin/env python3
"""Validate SP-B-X child ticket descriptions against a parent spec."""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from governance_engine.schema.forbidden_combinations import validate_forbidden_combinations
from governance_engine.schema.v0 import TicketContract

DRIFT_FIELDS = (
    "required_paths", "forbidden_paths", "loc_delta_max", "files_touched_max",
    "class", "l1_exclusive_reason", "l2_reason", "tag_type",
    "authority_required", "schema_version", "external_systems",
    "dependency_artifacts", "mutex_with",
)
COUNT_NOUNS = "categor(?:y|ies)|child(?:ren)?|fields?|items?|rules?"
JIRA_ENV_DIR = Path("~/.config/omnisight").expanduser()
USER_AGENT = "OmniSight-ticket-description-drift-validator/1.0"

GA_V0_DEFAULTS: dict[str, Any] = {
    "schema_version": "v0",
    "class": "subscription-codex",
    "loc_delta_max": 1,
    "files_touched_max": 1,
    "required_paths": ["placeholder"],
    "forbidden_paths": [],
    "non_goals": [],
    "interface_contract": None,
    "test_scope": "unit",
    "destructive_op_classes": [],
    "destructive_op_scope": "none",
    "scope_components": [],
    "external_side_effect": "none",
    "external_payload_class": "operational",
    "runtime_capability": "unit-only",
    "dependency_artifacts": [],
    "execution_mode": "unit-testable",
    "mutex_with": [],
    "evidence_class": "unit",
    "on_scope_creep": "abort",
    "scope_summary_max_chars": 240,
    "tag_type": "story",
    "l1_exclusive_reason": None,
    "l2_reason": None,
    "authority_required": "L3",
    "reversibility": "reversible",
    "environment_scope": "local",
    "external_systems": [],
}


@dataclasses.dataclass(frozen=True)
class JiraIssue:
    key: str
    summary: str
    description: str


@dataclasses.dataclass(frozen=True)
class DriftFinding:
    ticket_key: str
    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class TicketValidation:
    issue: JiraIssue
    child_id: str | None
    findings: list[DriftFinding]


def extract_child_id(*texts: str) -> str | None:
    for text in texts:
        if match := re.search(r"\bSP-B-X-\d{3}[A-Za-z0-9-]*\b", text):
            return match.group(0)
    return None


def parent_section_for_child(markdown: str, child_id: str | None) -> str:
    if child_id is None:
        return markdown
    start = re.search(rf"(?m)^###\s+.*\b{re.escape(child_id)}\b.*$", markdown)
    if start is None:
        return markdown
    next_section = re.search(r"(?m)^###\s+", markdown[start.start() + 1 :])
    end = len(markdown) if next_section is None else start.start() + 1 + next_section.start()
    return markdown[start.start() : end]


def extract_boundaries(markdown: str, *, child_id: str | None = None) -> dict[str, Any]:
    section = parent_section_for_child(markdown, child_id)
    for pattern in (
        r"(?ims)^#{3,6}\s+.*boundaries.*?\n\s*```(?:yaml|yml)?\s*\n(.*?)\n\s*```",
        r"(?ims)```(?:yaml|yml)?\s*\n(.*?^\s*(?:loc_delta_max|required_paths|schema_version):.*?)(?:\n\s*```)",
    ):
        if match := re.search(pattern, section):
            payload = yaml.safe_load(match.group(1)) or {}
            if not isinstance(payload, dict):
                raise ValueError("Boundaries YAML must parse to a mapping")
            return payload
    raise ValueError("Boundaries YAML block not found")


def compare_boundary_fields(parent: Mapping[str, Any], child: Mapping[str, Any]) -> list[tuple[str, Any, Any]]:
    return [(field, parent.get(field), child.get(field)) for field in DRIFT_FIELDS if parent.get(field) != child.get(field)]


def forbidden_combination_messages(boundaries: Mapping[str, Any]) -> list[str]:
    payload = dict(GA_V0_DEFAULTS)
    payload.update({key: value for key, value in boundaries.items() if key in payload})
    try:
        contract = TicketContract.model_validate(payload)
    except ValidationError as exc:
        return [f"schema validation failed: {exc.errors()[0]['msg']}"]
    return [f"rule {e.rule_id} {e.rule_name}: {e.detail}" for e in validate_forbidden_combinations(contract)]


def _count_claims(markdown: str) -> dict[str, int]:
    claims: dict[str, int] = {}
    for match in re.finditer(rf"\b(\d+)\s+({COUNT_NOUNS})\b", markdown, flags=re.IGNORECASE):
        noun = match.group(2).lower()
        if noun == "categories":
            noun = "category"
        elif noun == "children":
            noun = "child"
        elif noun.endswith("s"):
            noun = noun[:-1]
        claims.setdefault(noun, int(match.group(1)))
    return claims


def compare_count_claims(parent_section: str, child_description: str) -> list[tuple[str, int, int | None]]:
    child_claims = _count_claims(child_description)
    return [
        (noun, expected, child_claims.get(noun))
        for noun, expected in _count_claims(parent_section).items()
        if child_claims.get(noun) != expected
    ]


def adf_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(adf_to_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    suffix = "\n" if value.get("type") in {"paragraph", "heading", "codeBlock", "listItem"} else ""
    return str(value.get("text", "")) + adf_to_text(value.get("content", [])) + suffix


def _load_env(path: Path) -> dict[str, str]:
    pairs = (line.partition("=") for line in path.read_text(encoding="utf-8").splitlines())
    return {key.strip(): value.strip() for key, sep, value in pairs if sep and not key.strip().startswith("#")}


def _jira_auth(agent_class: str) -> tuple[str, str]:
    if agent_class in ("subscription-codex", "api-openai"):
        env_file, token_file, email_key = JIRA_ENV_DIR / "jira-codex.env", JIRA_ENV_DIR / "jira-codex-token", "OMNISIGHT_JIRA_CODEX_EMAIL"
    else:
        env_file, token_file, email_key = JIRA_ENV_DIR / "jira-claude.env", JIRA_ENV_DIR / "jira-claude-token", "OMNISIGHT_JIRA_CLAUDE_EMAIL"
    env = _load_env(env_file)
    raw = f"{env[email_key]}:{token_file.read_text(encoding='utf-8').strip()}".encode()
    return env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/"), "Basic " + b64encode(raw).decode()


def fetch_jira_issue(key: str, *, agent_class: str) -> JiraIssue:
    site, auth_header = _jira_auth(agent_class)
    url = f"{site}/rest/api/3/issue/{key}?fields=summary,description"
    request = urllib.request.Request(url, method="GET", headers={"Authorization": auth_header, "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            fields = (json.loads(response.read().decode("utf-8")).get("fields") or {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        raise RuntimeError(f"GET {url} -> {exc.code}: {body}") from exc
    return JiraIssue(key=key, summary=str(fields.get("summary") or ""), description=adf_to_text(fields.get("description") or ""))


def validate_issue(parent_markdown: str, issue: JiraIssue) -> TicketValidation:
    findings: list[DriftFinding] = []
    child_id = extract_child_id(issue.summary, issue.description)
    if child_id is None:
        findings.append(DriftFinding(issue.key, "child-id-missing", "no SP-B-X child identifier found"))
    try:
        parent_boundaries = extract_boundaries(parent_markdown, child_id=child_id)
        child_boundaries = extract_boundaries(issue.description)
    except ValueError as exc:
        code = "parent-boundaries-missing" if "parent_boundaries" not in locals() else "child-boundaries-missing"
        return TicketValidation(issue, child_id, [DriftFinding(issue.key, code, str(exc))])

    for field, expected, actual in compare_boundary_fields(parent_boundaries, child_boundaries):
        findings.append(DriftFinding(issue.key, "boundary-field-drift", f"{field}: parent={expected!r} child={actual!r}"))
    for source, boundaries in (("parent", parent_boundaries), ("child", child_boundaries)):
        findings.extend(DriftFinding(issue.key, "forbidden-combination", f"{source}: {msg}") for msg in forbidden_combination_messages(boundaries))
    parent_section = parent_section_for_child(parent_markdown, child_id)
    for noun, expected, actual in compare_count_claims(parent_section, issue.description):
        findings.append(DriftFinding(issue.key, "count-claim-drift", f"{noun}: parent={expected} child={actual if actual is not None else 'missing'}"))
    return TicketValidation(issue, child_id, findings)


def validate_tickets(parent_markdown: str, ticket_keys: list[str], fetcher: Callable[[str], JiraIssue]) -> list[TicketValidation]:
    return [validate_issue(parent_markdown, fetcher(key)) for key in ticket_keys]


def render_report(validations: list[TicketValidation]) -> str:
    lines = ["# SP-B-X Ticket Description Drift Report", "", f"Generated: `{datetime.now(timezone.utc).isoformat()}`", ""]
    lines += [f"Tickets checked: {len(validations)}", f"Findings: {sum(len(v.findings) for v in validations)}", ""]
    for validation in validations:
        lines += [f"## {validation.issue.key} — {validation.child_id or 'unknown child'}", ""]
        lines += [f"- `{f.code}`: {f.message}" for f in validation.findings] or ["No drift detected."]
        lines.append("")
    return "\n".join(lines)


def write_report(report: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"sprint-sp-b-x-drift-validator-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    path.write_text(report, encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parent_spec", type=Path)
    parser.add_argument("ticket_keys", nargs="+")
    parser.add_argument("--agent-class", default="subscription-codex")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "docs" / "audit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validations = validate_tickets(
        args.parent_spec.read_text(encoding="utf-8"),
        args.ticket_keys,
        lambda key: fetch_jira_issue(key, agent_class=args.agent_class),
    )
    path = write_report(render_report(validations), args.out_dir)
    findings = sum(len(validation.findings) for validation in validations)
    print(f"wrote drift report: {path}")
    if findings:
        print(f"drift detected: {findings} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
