#!/usr/bin/env python3
"""
N9 — major-upgrade gate freshness probe.

Reads `.fallback/manifests/<branch>.toml` to find the freshness window
and the required check name, then queries the GitHub Actions API for
the most recent successful workflow run on that branch. If the most
recent green run is older than `freshness_days`, exits non-zero so the
calling workflow's job goes red and the PR is blocked.

Self-contained CLI:

    python3 scripts/check_fallback_freshness.py \\
        --branch compat/nextjs-15 \\
        --repo  owner/repo \\
        --token "$GITHUB_TOKEN"

Stdlib-only (urllib + tomllib). Same self-defense argument as the
other N5/N6/N7/N8 scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = REPO_ROOT / ".fallback" / "manifests"

GITHUB_API = "https://api.github.com"


def load_manifest(branch: str) -> dict:
    leaf = branch.split("/", 1)[-1] if "/" in branch else branch
    path = MANIFESTS_DIR / f"{leaf}.toml"
    if not path.is_file():
        raise FileNotFoundError(
            f"manifest {path} not found — declare the branch first."
        )
    return tomllib.loads(path.read_text(encoding="utf-8"))


def fetch_runs(repo: str, branch: str, token: str | None,
               *, workflow_file: str = "fallback-branches.yml") -> list[dict]:
    """Return the GitHub Actions runs for `workflow_file` on `branch`.

    Pagination is intentionally **not** implemented — we only care about
    the latest 30 runs (page 1 default), and within a 14-day freshness
    window that covers >100x the expected cadence (1/week + push).
    """
    url = (
        f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow_file}/runs"
        f"?branch={urllib.parse.quote(branch, safe='/')}"
        f"&status=success&per_page=30"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent":           "OmniSight-N9-FreshnessProbe/1.0",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GitHub API HTTP {exc.code} for {url}: {exc.read().decode('utf-8', 'replace')[:200]}"
        ) from exc
    return list(payload.get("workflow_runs", []))


def evaluate(runs: list[dict], *, freshness_days: int,
             now: datetime | None = None) -> dict:
    """Decide whether the fallback branch is fresh-green.

    Returns a dict with `verdict` (`green` / `stale` / `never-green`)
    plus the diagnostic fields the workflow surfaces in its summary.
    """
    now = now or datetime.now(timezone.utc)
    if not runs:
        return {
            "verdict":          "never-green",
            "freshness_days":   freshness_days,
            "latest_run":       None,
            "latest_run_age_h": None,
        }
    # Runs are returned newest-first by the API.
    latest = runs[0]
    when = datetime.fromisoformat(latest["updated_at"].replace("Z", "+00:00"))
    age = now - when
    if age <= timedelta(days=freshness_days):
        verdict = "green"
    else:
        verdict = "stale"
    return {
        "verdict":          verdict,
        "freshness_days":   freshness_days,
        "latest_run": {
            "id":         latest.get("id"),
            "html_url":   latest.get("html_url"),
            "head_sha":   latest.get("head_sha"),
            "updated_at": latest.get("updated_at"),
        },
        "latest_run_age_h": round(age.total_seconds() / 3600.0, 2),
    }


def render_summary(branch: str, manifest: dict, evaluation: dict) -> str:
    pin = manifest.get("pin", {})
    lines = [
        f"## N9 fallback freshness probe — `{branch}`",
        "",
        f"* pin: `{pin.get('package', '?')}=={pin.get('version', '?')}` "
        f"({pin.get('ecosystem', '?')})",
        f"* freshness window: {evaluation['freshness_days']} days",
        "",
    ]
    if evaluation["verdict"] == "green":
        lines += [
            "**verdict: GREEN** — fallback is recently certified, gate passes.",
            "",
            f"* latest green run: [{evaluation['latest_run']['head_sha'][:10]}]"
            f"({evaluation['latest_run']['html_url']})",
            f"* age: {evaluation['latest_run_age_h']} hours",
        ]
    elif evaluation["verdict"] == "stale":
        lines += [
            "**verdict: STALE** — gate FAILS; refresh the fallback before merging.",
            "",
            f"* latest green run is {evaluation['latest_run_age_h']} hours old "
            f"(window = {evaluation['freshness_days']} days = "
            f"{evaluation['freshness_days'] * 24} hours)",
            "",
            "Recovery (operator):",
            "",
            "```bash",
            f"git switch {branch}",
            f"python3 scripts/fallback_rebase.py --branch {branch} --plan",
            f"python3 scripts/fallback_rebase.py --branch {branch} --apply",
            f"git push origin {branch}",
            "```",
            "",
            "Then re-run this workflow (or wait for fallback-branches.yml to",
            "re-trigger on the push).",
        ]
    else:  # never-green
        lines += [
            "**verdict: NEVER-GREEN** — gate FAILS; the fallback branch has",
            "no successful run on file. Either it was just created (push it",
            "and wait for the first run) or the bootstrap broke.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True,
                        help="Fallback branch (e.g. compat/nextjs-15)")
    parser.add_argument("--repo", required=True,
                        help="GitHub repo `owner/name`")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub token (default: $GITHUB_TOKEN)")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                        help="Append markdown summary to this path "
                             "(default: $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON evaluation to stdout")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.branch)
    freshness_days = int(manifest.get("gate", {}).get("freshness_days", 14))

    runs = fetch_runs(args.repo, args.branch, args.token)
    evaluation = evaluate(runs, freshness_days=freshness_days)

    if args.json:
        print(json.dumps(evaluation, indent=2))

    summary_md = render_summary(args.branch, manifest, evaluation)
    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        with Path(args.summary).open("a", encoding="utf-8") as fp:
            fp.write(summary_md + "\n")
    # Print to stdout regardless so CI logs carry the verdict.
    print(summary_md)

    if evaluation["verdict"] == "green":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
