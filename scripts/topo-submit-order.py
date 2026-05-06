#!/usr/bin/env python3
"""Suggest a safe Gerrit submit order for batched JIRA approvals.

Default mode is read-only:

    python3 scripts/topo-submit-order.py
    python3 scripts/topo-submit-order.py 'status = "Approved" AND project = OP'

Candidate tickets are fetched from JIRA, their runner-posted Gerrit change
URLs are extracted from issue comments, and current patchset diffs are fetched
from Gerrit. The resulting file/range overlap graph is ordered
deterministically so later same-file changes rebase on earlier ones. Exact
range overlaps are flagged for manual rebase.

Use ``--fixture synthetic-3`` for the OP-688 synthetic validation scenario.
Use ``--apply`` only when you want the script to create a temporary local
branch and cherry-pick the ordered patchset commits until the first conflict.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
CRED_DIR = Path("~/.config/omnisight").expanduser()

DEFAULT_JQL = 'status = "Under Review" AND assignee in (codex-bot, claude-bot)'
JIRA_AGENT_CLASS = "subscription-codex"
GERRIT_HOST = "sora.services"
GERRIT_PORT = 29418
GERRIT_PROJECT = "omnisight/OmniSight-Productizer"
GERRIT_URL_RE = re.compile(r"https?://\S+/c/[^/\s]+/[^/\s]+/\+/(\d+)")
CHANGE_ID_RE = re.compile(r"\bI[0-9a-fA-F]{8,40}\b")


@dataclass(frozen=True)
class FileRange:
    path: str
    start: int
    end: int

    def overlaps(self, other: "FileRange") -> bool:
        return self.path == other.path and self.start <= other.end and other.start <= self.end

    def label(self) -> str:
        if self.start == self.end:
            return f"{self.path}:{self.start}"
        return f"{self.path}:{self.start}-{self.end}"


@dataclass(frozen=True)
class CandidateChange:
    ticket: str
    summary: str
    change_number: str
    change_id: str
    patchset_ref: str
    revision: str
    files: tuple[str, ...]
    ranges: tuple[FileRange, ...]


@dataclass(frozen=True)
class RiskPair:
    left: str
    right: str
    ranges: tuple[str, ...]


def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _jira_auth() -> tuple[str, str, str]:
    env_file = CRED_DIR / "jira-codex.env"
    token_file = CRED_DIR / "jira-codex-token"
    env = _load_env(env_file)
    token = token_file.read_text().strip()
    email = env["OMNISIGHT_JIRA_CODEX_EMAIL"]
    site = env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/")
    project = env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP")
    raw = f"{email}:{token}".encode()
    return site + "/rest/api/3", "Basic " + b64encode(raw).decode(), project


def _jira_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url, auth_header, _project = _jira_auth()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"{method} {path} -> {exc.code}: {text}") from exc


def _adf_text(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(_adf_text(child) for child in node.get("content", []))
    if isinstance(node, list):
        return "".join(_adf_text(child) for child in node)
    return ""


def fetch_candidate_tickets(jql: str) -> list[dict[str, str]]:
    """Fetch candidate ticket keys and Gerrit change numbers from JIRA."""
    _base_url, _auth, project = _jira_auth()
    tickets: list[dict[str, str]] = []
    next_page_token: str | None = None
    while True:
        params = {
            "jql": f'project = "{project}" AND {jql}',
            "fields": "summary,comment,description",
            "maxResults": "100",
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        query = urllib.parse.urlencode(params)
        payload = _jira_request("GET", f"/search/jql?{query}")
        for issue in payload.get("issues", []):
            fields = issue.get("fields", {})
            haystack = _adf_text(fields.get("description"))
            for comment in fields.get("comment", {}).get("comments", []):
                haystack += "\n" + _adf_text(comment.get("body"))
            match = GERRIT_URL_RE.search(haystack)
            if not match:
                continue
            tickets.append(
                {
                    "key": issue["key"],
                    "summary": fields.get("summary") or "",
                    "change_number": match.group(1),
                }
            )
        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            break
    return tickets


def _gerrit_auth(agent_class: str) -> tuple[str, Path]:
    if agent_class in ("subscription-codex", "api-openai"):
        return "codex-bot", CRED_DIR / "gerrit-codex-bot-ed25519"
    return "claude-bot", CRED_DIR / "gerrit-claude-bot-ed25519"


def _ssh_cmd(agent_class: str, *args: str) -> list[str]:
    user, key_path = _gerrit_auth(agent_class)
    return [
        "ssh",
        "-i",
        str(key_path),
        "-p",
        str(GERRIT_PORT),
        f"{user}@{GERRIT_HOST}",
        *args,
    ]


def _gerrit_ssh_url(agent_class: str) -> str:
    user, _key_path = _gerrit_auth(agent_class)
    return f"ssh://{user}@{GERRIT_HOST}:{GERRIT_PORT}/{GERRIT_PROJECT}"


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def gerrit_query_change(change_number: str, agent_class: str) -> dict[str, Any]:
    result = _run(
        _ssh_cmd(
            agent_class,
            "gerrit",
            "query",
            "--format=JSON",
            "--current-patch-set",
            f"change:{change_number}",
        )
    )
    for line in result.stdout.splitlines():
        payload = json.loads(line)
        if "project" in payload:
            return payload
    raise RuntimeError(f"Gerrit change {change_number} not found")


def fetch_patchset_ref(ref: str, agent_class: str) -> str:
    _user, key_path = _gerrit_auth(agent_class)
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {key_path}"
    result = _run(["git", "fetch", _gerrit_ssh_url(agent_class), ref], check=True, env=env)
    blob = result.stdout + result.stderr
    match = re.search(r"FETCH_HEAD", blob)
    if not match:
        # ``git fetch`` still updates FETCH_HEAD even when the progress format
        # differs. Verify it explicitly instead of depending on stderr text.
        _run(["git", "rev-parse", "--verify", "FETCH_HEAD"], check=True)
    return "FETCH_HEAD"


def parse_unified_zero_diff(diff_text: str) -> tuple[FileRange, ...]:
    ranges: list[FileRange] = []
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if not line.startswith("@@ ") or not current_file:
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        end = start if count == 0 else start + count - 1
        ranges.append(FileRange(current_file, start, end))
    return tuple(ranges)


def fetch_change(ticket: dict[str, str], agent_class: str) -> CandidateChange:
    payload = gerrit_query_change(ticket["change_number"], agent_class)
    patchset = payload.get("currentPatchSet") or {}
    ref = patchset.get("ref")
    revision = patchset.get("revision")
    if not ref or not revision:
        raise RuntimeError(f"Gerrit change {ticket['change_number']} has no current patchset ref")
    fetch_patchset_ref(ref, agent_class)
    diff = _run(["git", "diff", "--unified=0", f"{revision}^", revision]).stdout
    ranges = parse_unified_zero_diff(diff)
    files = tuple(sorted({r.path for r in ranges}))
    change_id = payload.get("id") or ""
    if not CHANGE_ID_RE.search(change_id):
        change_id = payload.get("changeId") or change_id
    return CandidateChange(
        ticket=ticket["key"],
        summary=ticket["summary"],
        change_number=ticket["change_number"],
        change_id=change_id,
        patchset_ref=ref,
        revision=revision,
        files=files,
        ranges=ranges,
    )


def synthetic_changes() -> list[CandidateChange]:
    return [
        CandidateChange(
            ticket="OP-A",
            summary="touch X only",
            change_number="1001",
            change_id="Iaaaaaaaa",
            patchset_ref="refs/changes/01/1001/1",
            revision="synthetic-a",
            files=("X.py",),
            ranges=(FileRange("X.py", 10, 12),),
        ),
        CandidateChange(
            ticket="OP-B",
            summary="touch X and Y",
            change_number="1002",
            change_id="Ibbbbbbbb",
            patchset_ref="refs/changes/02/1002/1",
            revision="synthetic-b",
            files=("X.py", "Y.py"),
            ranges=(FileRange("X.py", 30, 32), FileRange("Y.py", 20, 20)),
        ),
        CandidateChange(
            ticket="OP-C",
            summary="touch Y only",
            change_number="1003",
            change_id="Icccccccc",
            patchset_ref="refs/changes/03/1003/1",
            revision="synthetic-c",
            files=("Y.py",),
            ranges=(FileRange("Y.py", 80, 81),),
        ),
    ]


def _overlap_files(left: CandidateChange, right: CandidateChange) -> set[str]:
    return set(left.files) & set(right.files)


def _range_overlaps(left: CandidateChange, right: CandidateChange) -> tuple[str, ...]:
    labels: list[str] = []
    for l_range in left.ranges:
        for r_range in right.ranges:
            if l_range.overlaps(r_range):
                labels.append(f"{l_range.label()} <-> {r_range.label()}")
    return tuple(labels)


def build_graph(changes: Iterable[CandidateChange]) -> tuple[dict[str, set[str]], list[RiskPair]]:
    ordered = sorted(changes, key=lambda c: c.ticket)
    graph = {change.ticket: set() for change in ordered}
    risks: list[RiskPair] = []

    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            shared = _overlap_files(left, right)
            if not shared:
                continue
            risky_ranges = _range_overlaps(left, right)
            if risky_ranges:
                risks.append(RiskPair(left.ticket, right.ticket, risky_ranges))

            # Keep same-file batches deterministic: earlier ticket keys land
            # first, later overlapping tickets rebase on them.
            graph[left.ticket].add(right.ticket)
    return graph, risks


def topo_order(changes: list[CandidateChange], graph: dict[str, set[str]]) -> list[CandidateChange]:
    by_ticket = {change.ticket: change for change in changes}
    indegree = {ticket: 0 for ticket in graph}
    for dependents in graph.values():
        for dependent in dependents:
            indegree[dependent] += 1

    ready = sorted(
        (ticket for ticket, degree in indegree.items() if degree == 0),
        key=lambda ticket: ticket,
    )
    result: list[CandidateChange] = []
    while ready:
        ticket = ready.pop(0)
        result.append(by_ticket[ticket])
        for dependent in sorted(graph[ticket]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort(key=lambda item: item)

    if len(result) != len(changes):
        raise RuntimeError("overlap graph contains a cycle")
    return result


def _format_files(files: tuple[str, ...]) -> str:
    if len(files) == 1:
        return f"touches {files[0]} only"
    return "touches " + " + ".join(files)


def render_order(order: list[CandidateChange], graph: dict[str, set[str]], risks: list[RiskPair]) -> str:
    prerequisites: dict[str, list[str]] = {change.ticket: [] for change in order}
    for parent, children in graph.items():
        for child in children:
            prerequisites[child].append(parent)
    risky_tickets = {risk.left for risk in risks} | {risk.right for risk in risks}

    lines = ["Submit order:"]
    for index, change in enumerate(order, start=1):
        if prerequisites[change.ticket]:
            risk_note = (
                "line-range risk flagged below"
                if change.ticket in risky_tickets
                else "no line-range conflict"
            )
            reason = (
                f"{_format_files(change.files)}; rebases on "
                f"{', '.join(sorted(prerequisites[change.ticket]))}; {risk_note}"
            )
        else:
            risk_note = (
                "; line-range risk flagged below"
                if change.ticket in risky_tickets
                else ""
            )
            reason = f"{_format_files(change.files)}; no prior overlap dependency{risk_note}"
        lines.append(
            f"{index}. {change.ticket} first" if index == 1 else f"{index}. then {change.ticket}"
        )
        lines[-1] += f" ({reason}) [change {change.change_number}]"

    if risks:
        lines.append("")
        lines.append("Risky pairs:")
        for risk in risks:
            joined = "; ".join(risk.ranges)
            lines.append(f"- {risk.left} / {risk.right}: same file, same line range ({joined}) -> MANUAL REBASE REQUIRED")
    else:
        lines.append("")
        lines.append("Risky pairs: none")
    return "\n".join(lines)


def apply_order(order: list[CandidateChange]) -> int:
    branch = "topo-submit-" + next(tempfile._get_candidate_names())
    _run(["git", "checkout", "-B", branch])
    print(f"Created temp branch {branch}")
    for change in order:
        result = _run(["git", "cherry-pick", change.revision], check=False)
        if result.returncode != 0:
            print(
                f"CONFLICT at {change.ticket} (change {change.change_number})\n"
                f"{result.stdout}{result.stderr}",
                file=sys.stderr,
            )
            return 1
        print(f"cherry-picked {change.ticket} ({change.revision})")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jql", nargs="?", default=DEFAULT_JQL, help=f"JQL filter; default: {DEFAULT_JQL}")
    parser.add_argument("--agent-class", default=JIRA_AGENT_CLASS, help="Bot credential class for Gerrit SSH")
    parser.add_argument("--apply", action="store_true", help="Cherry-pick ordered changes onto a temp branch")
    parser.add_argument("--fixture", choices=["synthetic-3"], help="Use a built-in fixture instead of JIRA/Gerrit")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.fixture == "synthetic-3":
        changes = synthetic_changes()
    else:
        tickets = fetch_candidate_tickets(args.jql)
        if not tickets:
            print("No candidate Gerrit changes found.")
            return 0
        changes = [fetch_change(ticket, args.agent_class) for ticket in tickets]

    graph, risks = build_graph(changes)
    order = topo_order(changes, graph)
    print(render_order(order, graph, risks))

    if args.apply:
        return apply_order(order)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
