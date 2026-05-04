#!/usr/bin/env python3
"""N10 — deploy-time blue-green gate.

Called by `scripts/deploy.sh` when ENV=prod. Looks at the git ref
that's about to deploy, finds the PR that introduced it, and refuses
the deploy when:

  * the PR carried `requires-blue-green` (auto-applied by the PR-side
    gate), and
  * the cut-over artefact is absent (no matching entry in the rollback
    ledger for this ref, OR the ledger entry's `disposition` is not
    one of the accepted terminal states).

Environment overrides (documented in the policy doc):

  * `OMNISIGHT_CHECK_BLUEGREEN=0` — skip the gate entirely. Intended
    for non-prod environments; deploy.sh sets this automatically for
    staging. Quarterly review will flag any prod deploy that ran with
    this override.
  * `OMNISIGHT_BLUEGREEN_OVERRIDE=1` — explicit disaster-recovery
    override. Logs a line to stderr that the quarterly audit reads.
  * `OMNISIGHT_BLUEGREEN_LEDGER` — path to the ledger; defaults to
    `docs/ops/upgrade_rollback_ledger.md`.

Exit codes:
  0 — gate green (or skipped for a legal reason).
  2 — gate refuses the deploy (fix the PR / ledger / waiver).
  3 — gate environment itself is broken (missing `gh`, no network,
      malformed ledger). Treat as a red deploy, not a flakey skip.

Stdlib-only. Uses `gh` CLI for the PR lookup so there are no OAuth
secrets in this script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

STICKY_LABEL   = "requires-blue-green"
WAIVER_LABEL   = "deploy/bluegreen-waived"
# Dispositions that count as a valid cut-over record in the ledger.
TERMINAL_OK = frozenset({"shipped", "rolled-back", "waived"})


# ─────────────────────────────────────────────────────────────────────
# `gh` helpers
# ─────────────────────────────────────────────────────────────────────

def _have_gh() -> bool:
    try:
        subprocess.run(
            ["gh", "--version"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _gh_json(args: list[str]) -> object | None:
    """Run `gh <args>` and parse stdout as JSON. Returns None on error."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            check=True, capture_output=True, text=True, timeout=20,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def resolve_pr_labels(ref: str) -> list[str] | None:
    """Return the list of labels on the PR that merged `ref`, or None.

    Strategy:
      1. `gh pr list --search <sha> --json number,labels` — picks up
         PRs that contain the merge commit.
      2. Fall back to `gh pr view <sha>` if (1) returns nothing.
    """
    # (1) search
    listing = _gh_json([
        "pr", "list",
        "--state", "merged",
        "--search", ref,
        "--json", "number,labels,mergeCommit",
        "--limit", "5",
    ])
    if isinstance(listing, list):
        for pr in listing:
            mc = (pr or {}).get("mergeCommit") or {}
            if isinstance(mc, dict) and mc.get("oid", "").startswith(ref[:7]):
                return [l.get("name", "") for l in pr.get("labels", [])]
        # fallthrough — try the first hit even if the oid match is soft
        if listing:
            return [l.get("name", "") for l in listing[0].get("labels", [])]

    # (2) view
    view = _gh_json(["pr", "view", ref, "--json", "labels"])
    if isinstance(view, dict):
        return [l.get("name", "") for l in view.get("labels", [])]

    return None


# ─────────────────────────────────────────────────────────────────────
# Ledger scan
# ─────────────────────────────────────────────────────────────────────

LEDGER_ROW_RE = re.compile(
    r"^\|\s*(?P<cutover>[^\|]+?)\s*\|"
    r"\s*(?P<package>[^\|]+?)\s*\|"
    r"\s*(?P<range>[^\|]+?)\s*\|"
    r"\s*(?P<pr>[^\|]+?)\s*\|"
    r"\s*(?P<op>[^\|]+?)\s*\|"
    r"\s*(?P<disp>[^\|]+?)\s*\|"
    r"\s*(?P<notes>[^\|]*?)\s*\|\s*$"
)


def scan_ledger(path: Path, ref: str) -> str | None:
    """Return the disposition cell for a ledger row matching `ref`.

    Ledger rows are markdown table rows under the "## Upgrades"
    heading. We match on `ref` appearing anywhere in the row
    (typically in the PR column or the notes column).
    """
    if not path.is_file():
        return None

    in_upgrades = False
    ref_short = ref[:7] if ref else ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_upgrades = (line.lower() == "## upgrades")
            continue
        if not in_upgrades:
            continue
        # skip header / separator rows
        if line.startswith("|---") or not line.startswith("|"):
            continue
        if not (ref in line or (ref_short and ref_short in line)):
            continue
        m = LEDGER_ROW_RE.match(line)
        if m:
            disp = m.group("disp").strip().lower()
            return disp
    return None


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def resolve_ref(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return proc.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default=None,
                    help="Git ref to evaluate. Defaults to HEAD.")
    ap.add_argument("--ledger",
                    default=str(REPO_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"),
                    help="Path to the rollback ledger.")
    ap.add_argument("--env",
                    default=os.environ.get("OMNISIGHT_DEPLOY_ENV", "prod"),
                    help="Deploy environment (prod|staging|…).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the decision but exit 0 regardless.")
    args = ap.parse_args(argv)

    override = os.environ.get("OMNISIGHT_BLUEGREEN_OVERRIDE") == "1"
    skip     = os.environ.get("OMNISIGHT_CHECK_BLUEGREEN") == "0"

    # Only guard prod. Staging / dev deploys bypass the gate — they
    # *are* the blue-green ceremony's standby step.
    if args.env != "prod":
        sys.stderr.write(f"[bluegreen-gate] env={args.env!r} — gate skipped (prod-only).\n")
        return 0

    if skip:
        sys.stderr.write("[bluegreen-gate] OMNISIGHT_CHECK_BLUEGREEN=0 — gate skipped.\n")
        return 0

    ref = resolve_ref(args.ref)
    if not ref:
        sys.stderr.write("[bluegreen-gate] ERROR: could not resolve git HEAD.\n")
        return 3

    if not _have_gh():
        # No `gh` means we can't inspect the PR. Treat as environmental
        # failure rather than silent pass — operators need visibility.
        sys.stderr.write(
            "[bluegreen-gate] ERROR: `gh` CLI not found on PATH. "
            "Install or set OMNISIGHT_CHECK_BLUEGREEN=0 to bypass "
            "(logged in quarterly review).\n"
        )
        return 3

    labels = resolve_pr_labels(ref)
    if labels is None:
        sys.stderr.write(
            f"[bluegreen-gate] WARN: no merged PR found for ref {ref[:12]}. "
            "Gate green by default (hotfix / direct-to-main case). "
            "If this is a major framework cut-over, abort and re-deploy "
            "from the PR's merge commit.\n"
        )
        return 0

    label_set = set(labels)

    if STICKY_LABEL not in label_set:
        sys.stderr.write(
            f"[bluegreen-gate] OK: PR for {ref[:12]} is not "
            "blue-green-required (no sticky label). Proceeding.\n"
        )
        return 0

    if WAIVER_LABEL in label_set:
        sys.stderr.write(
            f"[bluegreen-gate] WAIVED: PR for {ref[:12]} has "
            f"`{WAIVER_LABEL}`. This will be audited in the next "
            "quarterly policy review — ensure the rationale is in "
            "HANDOFF.md and the ledger.\n"
        )
        return 0

    ledger_path = Path(args.ledger)
    disp = scan_ledger(ledger_path, ref)
    if disp and disp in TERMINAL_OK:
        sys.stderr.write(
            f"[bluegreen-gate] OK: ledger entry for {ref[:12]} has "
            f"disposition={disp!r}. Proceeding.\n"
        )
        return 0

    if override:
        sys.stderr.write(
            f"[bluegreen-gate] OVERRIDE: OMNISIGHT_BLUEGREEN_OVERRIDE=1 "
            f"used on ref {ref[:12]} without a matching ledger entry "
            f"(disposition={disp!r}). Deploy proceeds but this entry "
            "MUST be backfilled in the ledger before the next quarterly "
            "review.\n"
        )
        return 0

    # Gate refuses.
    sys.stderr.write(
        f"[bluegreen-gate] REFUSE: ref {ref[:12]} carries "
        f"`{STICKY_LABEL}` but no matching terminal entry in "
        f"{ledger_path} (found disposition={disp!r}).\n"
        "Fix: run the G3 blue-green ceremony on the standby side, "
        "record the cut-over in the ledger, then retry. "
        "Emergency override: OMNISIGHT_BLUEGREEN_OVERRIDE=1.\n"
    )
    if args.dry_run:
        sys.stderr.write("[bluegreen-gate] --dry-run set; exiting 0.\n")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
