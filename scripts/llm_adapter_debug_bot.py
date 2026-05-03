#!/usr/bin/env python3
"""Z.7.8 — llm-adapter-debug-bot: auto-diagnose consecutive LLM live-test
failures and file a GitHub issue.

Called by the GitHub Actions ``escalate`` job when 2 consecutive nightly
LLM live-test runs fail. The bot:

  1. Downloads the pytest JSON report artifact from each failing run.
  2. Analyzes per-provider failure patterns across both runs.
  3. Files a structured GitHub issue via ``gh issue create``.
  4. Rate-limits to one open issue per failure streak (skips when an open
     ``llm-live-test-failure`` issue already exists — prevents spam).

Usage::

    python scripts/llm_adapter_debug_bot.py \\
        --current-run-id  <int> \\
        --previous-run-id <int>

Environment variables:
    GITHUB_TOKEN          Injected by GitHub Actions (needs ``issues: write``).
    GITHUB_REPOSITORY     Injected by GitHub Actions (e.g. "org/repo").
    OMNISIGHT_BACKEND_URL Optional; used to surface SharedKV status in the
                          issue body if available.

Note on BP.B Guild dependency
──────────────────────────────
The spec calls for "BP.B Guild 派 ``llm-adapter-debug-bot``". BP.B Guild
infrastructure (Guild registry, GUILD_TOOLS remap, agent dispatch) is not
yet implemented (all BP.B rows are ``[ ]``). This script acts as the
standalone ``llm-adapter-debug-bot`` agent. Once BP.B lands, it can be
wrapped as a Guild agent callable via ``_guild_node_factory``. The
diagnostic and issue-creation logic here is intentionally self-contained
so the BP.B migration is a thin wrapper, not a rewrite.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


# ─── helpers ─────────────────────────────────────────────────────────────────


def _gh(*args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["gh", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if check and result.returncode != 0:
        print(
            f"[debug-bot] gh {' '.join(args[:3])} ... failed (exit {result.returncode}):\n"
            f"  stdout: {result.stdout.strip()[:500]}\n"
            f"  stderr: {result.stderr.strip()[:500]}",
            file=sys.stderr,
        )
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result


def _get_run_details(run_id: int) -> dict:
    try:
        result = _gh(
            "run", "view", str(run_id),
            "--json", "conclusion,createdAt,url,displayTitle",
        )
        return json.loads(result.stdout)
    except Exception as exc:
        print(f"[debug-bot] could not fetch run {run_id}: {exc}", file=sys.stderr)
        return {}


def _download_test_report(run_id: int, dest_dir: str) -> dict | None:
    """Download pytest JSON report artifact; return parsed dict or None."""
    artifact_name = f"llm-live-test-report-{run_id}"
    try:
        _gh(
            "run", "download", str(run_id),
            "--name", artifact_name,
            "--dir", dest_dir,
        )
        # gh places downloaded files directly in dest_dir
        downloaded = Path(dest_dir) / "live-test-report.json"
        if downloaded.exists():
            with open(downloaded) as f:
                return json.load(f)
    except Exception as exc:
        print(
            f"[debug-bot] could not download artifact '{artifact_name}': {exc}",
            file=sys.stderr,
        )
    return None


def _parse_provider_status(report: dict | None) -> dict[str, str]:
    """Return {provider: 'pass'|'fail'|'skip'} from a pytest JSON report."""
    if not report:
        return {}
    tallies: dict[str, dict] = {}
    for test in report.get("tests", []):
        node_id: str = test.get("nodeid", "")
        outcome: str = test.get("outcome", "unknown")
        if "Anthropic" in node_id:
            key = "Anthropic"
        elif "OpenAI" in node_id:
            key = "OpenAI"
        elif "Gemini" in node_id:
            key = "Gemini"
        else:
            continue
        if key not in tallies:
            tallies[key] = {"failed": False, "run": 0, "skipped": 0}
        if outcome == "failed":
            tallies[key]["failed"] = True
            tallies[key]["run"] += 1
        elif outcome == "skipped":
            tallies[key]["skipped"] += 1
        else:
            tallies[key]["run"] += 1

    result: dict[str, str] = {}
    for k, v in tallies.items():
        if v["failed"]:
            result[k] = "fail"
        elif v["run"] == 0 and v["skipped"] > 0:
            result[k] = "skip"
        else:
            result[k] = "pass"
    return result


# ─── rate-limit guard ────────────────────────────────────────────────────────


def _existing_open_escalation_issue(repo: str) -> int | None:
    """Return the issue number of the most recent open escalation issue, or None."""
    try:
        result = _gh(
            "issue", "list",
            "--repo", repo,
            "--label", "llm-live-test-failure",
            "--state", "open",
            "--limit", "1",
            "--json", "number",
        )
        issues = json.loads(result.stdout)
        if issues:
            return int(issues[0]["number"])
    except Exception as exc:
        print(f"[debug-bot] issue list check failed: {exc}", file=sys.stderr)
    return None


def _ensure_label(repo: str) -> None:
    """Create the triage label (idempotent via --force)."""
    try:
        _gh(
            "label", "create",
            "llm-live-test-failure",
            "--repo", repo,
            "--color", "D93F0B",
            "--description",
            "Z.7.8 automatic escalation: consecutive LLM live-test failures",
            "--force",
        )
    except Exception as exc:
        print(f"[debug-bot] warning: label ensure failed: {exc}", file=sys.stderr)


# ─── issue body builder ───────────────────────────────────────────────────────

_STATUS_ICON = {"pass": "✅ pass", "fail": "❌ fail", "skip": "⏭️ skip"}


def _icon(status: str) -> str:
    return _STATUS_ICON.get(status, "❓ unknown")


def _run_link(run_id: int, details: dict, label: str) -> str:
    url = details.get(
        "url",
        f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{run_id}",
    )
    created = (details.get("createdAt") or "")[:10] or "unknown"
    return f"[{label} #{run_id}]({url}) ({created})"


def _pattern_analysis(
    cur_providers: dict[str, str],
    prev_providers: dict[str, str],
) -> str:
    failing_now = {k for k, v in cur_providers.items() if v == "fail"}
    failing_both = {
        k for k in failing_now if prev_providers.get(k) == "fail"
    }
    if not failing_now:
        return (
            "No provider-level failures detected — the overall run failed for "
            "a reason not captured in the per-provider breakdown (e.g., import "
            "error, fixture failure, or missing CI secrets)."
        )
    if len(failing_now) == 3:
        return (
            "**All three providers failing** in both runs. "
            "Likely cause: shared dependency change (LangChain version bump, "
            "network connectivity issue, or malformed test fixture)."
        )
    if failing_both:
        names = ", ".join(f"**{k}**" for k in sorted(failing_both))
        if len(failing_both) == 1:
            return (
                f"Isolated to {names} across both runs. "
                "Likely cause: provider-specific API schema change, "
                "expired or rate-limited CI API key, or breaking SDK update "
                "for this provider."
            )
        return (
            f"Consistent failures in {names} across both runs. "
            "Check each provider's API status page and SDK changelog."
        )
    # failing now but not in previous run (or no data for previous)
    names = ", ".join(f"**{k}**" for k in sorted(failing_now))
    return (
        f"New failures in {names} (not seen in the previous run). "
        "May be transient (provider incident, rate-limit spike) or "
        "caused by a dependency change landed between the two runs."
    )


def _rca_checklist(
    cur_providers: dict[str, str],
    prev_providers: dict[str, str],
    current_run_id: int,
    previous_run_id: int,
) -> str:
    steps: list[str] = [
        f"Review workflow logs for run [{current_run_id}](https://github.com/"
        f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{current_run_id})",
        f"Review workflow logs for run [{previous_run_id}](https://github.com/"
        f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{previous_run_id})",
        "Check `backend/requirements.txt` for recent LangChain / provider-SDK version bumps",
    ]
    failing = {k for k, v in cur_providers.items() if v == "fail"}
    if "Anthropic" in failing:
        steps += [
            "Check [Anthropic API status](https://status.anthropic.com)",
            "Verify `ANTHROPIC_API_KEY_CI` has not expired (Anthropic console → API keys)",
        ]
    if "OpenAI" in failing:
        steps += [
            "Check [OpenAI API status](https://status.openai.com)",
            "Verify `OPENAI_API_KEY_CI` has not expired (OpenAI platform → API keys)",
        ]
    if "Gemini" in failing:
        steps += [
            "Check [Google AI status](https://status.cloud.google.com)",
            "Verify `GOOGLE_API_KEY_CI` has not expired (Google AI Studio → API keys)",
        ]
    steps.append(
        "Reproduce locally: `pytest -m live -v backend/tests/test_llm_adapter_live.py`"
    )
    return "\n".join(f"- [ ] {s}" for s in steps)


def _build_body(
    current_run_id: int,
    previous_run_id: int,
    current_details: dict,
    previous_details: dict,
    cur_providers: dict[str, str],
    prev_providers: dict[str, str],
) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    prev_link = _run_link(previous_run_id, previous_details, "Previous run")
    curr_link = _run_link(current_run_id, current_details, "Current run")

    providers_all = sorted(
        set(cur_providers) | set(prev_providers) | {"Anthropic", "OpenAI", "Gemini"}
    )
    header_row = "| Run | " + " | ".join(providers_all) + " |"
    sep_row = "|-----|" + "|".join("---" for _ in providers_all) + "|"

    def _row(link: str, pmap: dict[str, str]) -> str:
        cells = " | ".join(_icon(pmap.get(p, "")) for p in providers_all)
        return f"| {link} | {cells} |"

    table = "\n".join([
        header_row,
        sep_row,
        _row(prev_link, prev_providers),
        _row(curr_link, cur_providers),
    ])

    pattern = _pattern_analysis(cur_providers, prev_providers)
    rca = _rca_checklist(cur_providers, prev_providers, current_run_id, previous_run_id)

    return f"""## Z.7.8 Automatic Failure Escalation

The **LLM live integration test suite** (Z.7 nightly CI) has failed in \\
**2 consecutive runs**. This issue was automatically filed by \\
`llm-adapter-debug-bot` (Z.7.8).

> **A human should diagnose and resolve the underlying cause, then close this issue.**

---

## Failure Summary

{table}

## Pattern Analysis

{pattern}

## Suggested RCA Steps

{rca}

---

_Triggered by: Z.7.8 failure escalation — `llm-adapter-debug-bot`_
_Consecutive failing runs: {previous_run_id}, {current_run_id}_
_Repository: {repo}_

> Note: Once BP.B Guild infrastructure lands, this bot will be re-dispatched
> as a formal Guild agent via `_guild_node_factory`. For now it runs as a
> standalone CI script (see `scripts/llm_adapter_debug_bot.py`).
"""


# ─── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Z.7.8 llm-adapter-debug-bot: file escalation issue on consecutive failures"
    )
    parser.add_argument("--current-run-id", type=int, required=True,
                        help="GitHub Actions run ID of the current (just-failed) run")
    parser.add_argument("--previous-run-id", type=int, required=True,
                        help="GitHub Actions run ID of the previous (also-failed) run")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("[debug-bot] GITHUB_REPOSITORY not set — cannot create issue", file=sys.stderr)
        return 1

    # Rate-limit: skip if an open escalation issue already exists.
    existing = _existing_open_escalation_issue(repo)
    if existing is not None:
        print(
            f"[debug-bot] open escalation issue #{existing} already exists "
            f"— skipping duplicate (close it once resolved to re-arm escalation)"
        )
        return 0

    print(f"[debug-bot] analysing runs {args.previous_run_id} + {args.current_run_id}")

    current_details = _get_run_details(args.current_run_id)
    previous_details = _get_run_details(args.previous_run_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        curr_dir = os.path.join(tmpdir, "current")
        prev_dir = os.path.join(tmpdir, "previous")
        os.makedirs(curr_dir)
        os.makedirs(prev_dir)

        current_report = _download_test_report(args.current_run_id, curr_dir)
        previous_report = _download_test_report(args.previous_run_id, prev_dir)

    cur_providers = _parse_provider_status(current_report)
    prev_providers = _parse_provider_status(previous_report)

    print(f"[debug-bot] current run  providers: {cur_providers or '(no data)'}")
    print(f"[debug-bot] previous run providers: {prev_providers or '(no data)'}")

    body = _build_body(
        current_run_id=args.current_run_id,
        previous_run_id=args.previous_run_id,
        current_details=current_details,
        previous_details=previous_details,
        cur_providers=cur_providers,
        prev_providers=prev_providers,
    )

    _ensure_label(repo)

    title = (
        f"[Z.7.8] LLM Live Tests: 2 consecutive nightly failures "
        f"(runs {args.previous_run_id}, {args.current_run_id})"
    )
    try:
        result = _gh(
            "issue", "create",
            "--repo", repo,
            "--title", title,
            "--body", body,
            "--label", "llm-live-test-failure",
        )
        print(f"[debug-bot] issue created: {result.stdout.strip()}")
    except subprocess.CalledProcessError as exc:
        print(f"[debug-bot] failed to create issue: {exc.stderr}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
