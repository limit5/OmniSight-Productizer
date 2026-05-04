#!/usr/bin/env python3
"""
N9 — fallback-branch rebase planner.

Reads `.fallback/manifests/<branch>.toml`, walks `main..origin/main`
(or any user-supplied range), and classifies every commit into one of:

    * pickable      — touches NO path matched by [rebase].skip_globs
    * full-skip     — every changed path matches a skip glob
    * partial-skip  — the commit straddles both safe and skip paths
                      (operator must split manually; the tool refuses
                       to auto-split)

The default mode is `--plan` (dry-run report). `--apply` cherry-picks
the `pickable` set onto the fallback branch in chronological order;
on first conflict it stops and surfaces the offending commit so the
operator can resolve and resume.

Stdlib-only by design — same self-defense argument as N5/N6/N7/N8:
this script is the operator's escape hatch when production is on fire
because of a framework upgrade. It must not depend on the framework
that's broken. `tomllib` is stdlib in Python 3.11+.

Usage::

    python3 scripts/fallback_rebase.py --branch compat/nextjs-15 --plan
    python3 scripts/fallback_rebase.py --branch compat/nextjs-15 --apply
    python3 scripts/fallback_rebase.py --branch compat/nextjs-15 \\
        --range main..origin/main --plan
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = REPO_ROOT / ".fallback" / "manifests"


# ─────────────────────────────────────────────────────────────────────
# Manifest reading
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Manifest:
    branch: str
    framework: str
    framework_track: str
    pin_package: str
    pin_version: str
    skip_globs: list[str] = field(default_factory=list)
    required_check_name: str = ""
    freshness_days: int = 14

    @classmethod
    def load(cls, branch: str) -> "Manifest":
        # branch `compat/nextjs-15` → manifest `nextjs-15.toml`
        leaf = branch.split("/", 1)[-1] if "/" in branch else branch
        path = MANIFESTS_DIR / f"{leaf}.toml"
        if not path.is_file():
            raise FileNotFoundError(
                f"No manifest at {path} — declare it under .fallback/manifests/ first."
            )
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            branch=data["branch"]["name"],
            framework=data["branch"]["framework"],
            framework_track=str(data["branch"]["framework_track"]),
            pin_package=data["pin"]["package"],
            pin_version=data["pin"]["version"],
            skip_globs=list(data.get("rebase", {}).get("skip_globs", [])),
            required_check_name=data.get("gate", {}).get("required_check_name", ""),
            freshness_days=int(data.get("gate", {}).get("freshness_days", 14)),
        )


# ─────────────────────────────────────────────────────────────────────
# Git helpers (subprocess only — no GitPython dep)
# ─────────────────────────────────────────────────────────────────────


def _git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} → exit {proc.returncode}\n{proc.stderr}"
        )
    return proc.stdout


def list_commits(rev_range: str) -> list[str]:
    out = _git("rev-list", "--reverse", rev_range)
    return [line for line in out.splitlines() if line.strip()]


def files_changed(sha: str) -> list[str]:
    out = _git("show", "--name-only", "--pretty=format:", sha)
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_subject(sha: str) -> str:
    return _git("log", "-1", "--pretty=format:%s", sha).strip()


# ─────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────


def matches_any_glob(path: str, globs: list[str]) -> bool:
    """Match `path` against any of `globs` using fnmatch + ** semantics.

    fnmatch alone doesn't grok `**`; we expand it by splitting on `/`
    and checking that every glob segment matches the corresponding path
    segment, with `**` swallowing any number of segments.
    """
    for pattern in globs:
        if _glob_match(path, pattern):
            return True
    return False


def _glob_match(path: str, pattern: str) -> bool:
    if "**" not in pattern:
        return fnmatch.fnmatch(path, pattern)
    # `a/**/b` → recursively walk: match if a prefix matches `a/`,
    # then any number of segments, then suffix matches `b`.
    p_parts = path.split("/")
    g_parts = pattern.split("/")
    return _segment_match(p_parts, g_parts)


def _segment_match(p_parts: list[str], g_parts: list[str]) -> bool:
    if not g_parts:
        return not p_parts
    head, *rest = g_parts
    if head == "**":
        if not rest:
            return True  # trailing `**` matches anything
        for i in range(len(p_parts) + 1):
            if _segment_match(p_parts[i:], rest):
                return True
        return False
    if not p_parts:
        return False
    if not fnmatch.fnmatch(p_parts[0], head):
        return False
    return _segment_match(p_parts[1:], rest)


@dataclass
class Classification:
    sha: str
    subject: str
    files: list[str]
    bucket: str  # "pickable" | "full-skip" | "partial-skip"
    skipped_files: list[str] = field(default_factory=list)
    safe_files: list[str] = field(default_factory=list)


def classify(sha: str, manifest: Manifest) -> Classification:
    files = files_changed(sha)
    if not files:
        # Empty diff (merge commit, etc.) — safest to skip.
        return Classification(sha=sha, subject=commit_subject(sha),
                              files=files, bucket="full-skip")
    skipped = [f for f in files if matches_any_glob(f, manifest.skip_globs)]
    safe = [f for f in files if f not in skipped]
    if not skipped:
        bucket = "pickable"
    elif not safe:
        bucket = "full-skip"
    else:
        bucket = "partial-skip"
    return Classification(
        sha=sha, subject=commit_subject(sha), files=files,
        bucket=bucket, skipped_files=skipped, safe_files=safe,
    )


# ─────────────────────────────────────────────────────────────────────
# Plan / apply orchestration
# ─────────────────────────────────────────────────────────────────────


def plan(manifest: Manifest, rev_range: str) -> dict:
    commits = list_commits(rev_range)
    classifications = [classify(sha, manifest) for sha in commits]
    counts = {"pickable": 0, "full-skip": 0, "partial-skip": 0}
    for c in classifications:
        counts[c.bucket] += 1
    return {
        "branch": manifest.branch,
        "range": rev_range,
        "total": len(commits),
        "counts": counts,
        "commits": [
            {
                "sha": c.sha,
                "subject": c.subject,
                "bucket": c.bucket,
                "skipped_files": c.skipped_files,
                "safe_files": c.safe_files,
            } for c in classifications
        ],
    }


def render_plan_text(report: dict) -> str:
    lines = [
        "# N9 fallback rebase plan",
        "",
        f"* branch: `{report['branch']}`",
        f"* range:  `{report['range']}`",
        f"* total commits in range: {report['total']}",
        "",
        "## Bucket counts",
        "",
        "| bucket | count |",
        "|---|---|",
        f"| pickable     | {report['counts']['pickable']}     |",
        f"| full-skip    | {report['counts']['full-skip']}    |",
        f"| partial-skip | {report['counts']['partial-skip']} |",
        "",
        "## Per-commit detail",
        "",
    ]
    if not report["commits"]:
        lines.append("_No commits in range._")
        return "\n".join(lines) + "\n"
    for c in report["commits"]:
        lines.append(f"### `{c['sha'][:10]}` — {c['bucket']}")
        lines.append("")
        lines.append(f"> {c['subject']}")
        lines.append("")
        if c["bucket"] == "partial-skip":
            lines.append("**Operator action required** — split this commit:")
            lines.append("")
            lines.append(f"* skipped paths ({len(c['skipped_files'])}):")
            for p in c["skipped_files"]:
                lines.append(f"  * `{p}`")
            lines.append(f"* safe paths ({len(c['safe_files'])}):")
            for p in c["safe_files"]:
                lines.append(f"  * `{p}`")
        elif c["bucket"] == "full-skip" and c["skipped_files"]:
            lines.append(f"All {len(c['skipped_files'])} changed paths matched skip globs.")
        lines.append("")
    return "\n".join(lines) + "\n"


def apply(manifest: Manifest, rev_range: str, *, allow_partial: bool) -> int:
    """Cherry-pick the pickable bucket onto the current branch.

    Refuses to run if HEAD is not the fallback branch — guards against
    accidentally applying to main. Stops on first conflict and prints
    the recovery command.
    """
    head = _git("symbolic-ref", "--short", "HEAD").strip()
    if head != manifest.branch:
        print(f"::error::HEAD is `{head}`, expected `{manifest.branch}`. "
              f"Run `git switch {manifest.branch}` first.", file=sys.stderr)
        return 2

    report = plan(manifest, rev_range)
    if report["counts"]["partial-skip"] and not allow_partial:
        print(f"::error::{report['counts']['partial-skip']} partial-skip commits "
              f"found; run with --plan first to inspect, then either split them "
              f"manually or re-run with --allow-partial-skip to drop them.",
              file=sys.stderr)
        return 3

    picked = 0
    for c in report["commits"]:
        if c["bucket"] != "pickable":
            continue
        proc = subprocess.run(
            ["git", "cherry-pick", "-x", c["sha"]],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            print(f"::error::cherry-pick {c['sha'][:10]} ({c['subject']}) "
                  f"hit a conflict.", file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            print(f"\nResolve and resume:\n"
                  f"  git status\n"
                  f"  # ... fix conflicts ...\n"
                  f"  git cherry-pick --continue\n"
                  f"  python3 scripts/fallback_rebase.py "
                  f"--branch {manifest.branch} --range {rev_range} --apply",
                  file=sys.stderr)
            return 1
        picked += 1
    print(f"[N9] cherry-picked {picked} commit(s) onto {manifest.branch}")
    return 0


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="N9 fallback-branch rebase planner / applier.",
    )
    parser.add_argument("--branch", required=True,
                        help="Fallback branch name (e.g. compat/nextjs-15)")
    parser.add_argument(
        "--range", default="HEAD..origin/main",
        help="Git revision range to consider (default: HEAD..origin/main)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true",
                      help="Dry-run report only (default)")
    mode.add_argument("--apply", action="store_true",
                      help="Actually cherry-pick the pickable bucket")
    parser.add_argument("--json", action="store_true",
                        help="Emit plan as JSON instead of markdown")
    parser.add_argument("--allow-partial-skip", action="store_true",
                        help="With --apply, drop partial-skip commits silently "
                             "(default: refuse and ask the operator)")
    args = parser.parse_args(argv)

    manifest = Manifest.load(args.branch)

    if args.apply:
        return apply(manifest, args.range, allow_partial=args.allow_partial_skip)

    report = plan(manifest, args.range)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_plan_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
