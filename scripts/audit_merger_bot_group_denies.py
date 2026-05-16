#!/usr/bin/env python3
"""OP-1196 Phase 1α protection #1 — audit deny-parity between
``ai-reviewer-bots`` and ``merger-agent-bot`` Gerrit groups on
``refs/meta/config:project.config``.

Background — why this audit exists
----------------------------------
Phase 1α of OP-1196 split ``merger-agent-bot`` out of the
``ai-reviewer-bots`` group so the merger could ``addPatchSet`` on a
human's open Gerrit change (per O6 design + CLAUDE.md L1 exception).
The remaining O10 restrictions — every other destructive verb the
ai-reviewer-bots group is blocked from — are PRESERVED for
merger-agent-bot by mirroring each ``block group ai-reviewer-bots``
rule to ALSO emit ``block group merger-agent-bot`` on the same scope.

The mirror is fragile by design: a future engineer extending O10 with
a new deny verb on ai-reviewer-bots (e.g., a new ``forgeServerIdent``
permission) might forget to add the matching block on merger-agent-bot,
silently opening a permission gap. This script fails CI when that
drift appears.

Mechanics
---------
1. Parse ``project.config`` (the tracked authoritative copy in
   ``.gerrit/`` mirrors what should be live on ``refs/meta/config``;
   audit BOTH if both are present — see ``FILES_TO_CHECK``).
2. For each access block, collect:

      ai_denies      = { permission : scope }  for ``= block group ai-reviewer-bots``
      merger_denies  = { permission : scope }  for ``= block group merger-agent-bot``

3. The set difference ``ai_denies - merger_denies`` MUST equal the
   documented ``EXCEPTIONS`` set (currently: ``{"addPatchSet"}``).
   Any extra entry on either side fails the audit.

4. The opposite direction (``merger_denies - ai_denies``) is allowed
   to be non-empty — merger may have MORE restrictions if a future
   ticket adds merger-specific O10 hardening — but logged as INFO.

Usage
-----
    $ python scripts/audit_merger_bot_group_denies.py
    # exits 0 on parity match, non-zero on drift

CI hook: wired into ``.pre-commit-config.yaml`` plus the main CI
workflow (.github/workflows/ci.yml or .gerrit-ci equivalent).

The audit is read-only — it never modifies project.config. To resolve
a drift failure, edit project.config so that the two groups' deny
sets differ only by ``EXCEPTIONS``.

Exception list
--------------
``EXCEPTIONS`` enumerates the verbs where ai-reviewer-bots and
merger-agent-bot intentionally diverge. To add an entry, file a
ticket explaining the carve-out + update the comment alongside the
exception.

Tracking ticket: OP-1196 (Phase 1α post-deploy follow-up).
Related: CLAUDE.md L1 O6 + O10. Live config audit: refs/meta/config
on the OmniSight-Productizer project at sora.services.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

# Permissions where the two groups are INTENTIONALLY allowed to diverge.
# Currently only addPatchSet (the merger's resolution-push pathway).
# Adding to this set requires a corresponding ticket + comment.
EXCEPTIONS: frozenset[str] = frozenset({
    "addPatchSet",   # OP-1196 phase 1α — merger needs this for O6 resolution-push
})

# Files to audit at the default (no-args) entry point.
#
# The tracked ``.gerrit/project.config`` is documentation-of-intent and
# uses the LEGACY ``deny X = group Y`` syntax that JGit rejects on push
# (separate issue tracked in OP-1192). This script's parser intentionally
# does NOT consume that syntax — auditing the tracked file would conflate
# two distinct kinds of drift (intent-vs-live vs ai-vs-merger). For now
# the audit's authoritative target is refs/meta/config, which uses the
# valid ``X = block group Y`` syntax. Wire via stdin:
#
#   $ git show refs/remotes/gerrit/meta-config:project.config \
#       | scripts/audit_merger_bot_group_denies.py --from-stdin
#
# Once OP-1192 fixes the tracked file syntax, this list will gain
# ``.gerrit/project.config`` for the regular CI hook.
FILES_TO_CHECK: tuple[str, ...] = ()

# Regex captures "<permission> = block group <group-name>" lines.
# The group name has no whitespace; permission is a single word.
_BLOCK_RE = re.compile(
    r"^\s*(?P<perm>[A-Za-z][A-Za-z]*)\s*=\s*block\s+group\s+(?P<group>\S+)\s*$",
)
_SECTION_RE = re.compile(r"^\s*\[access\s+\"(?P<scope>[^\"]+)\"\]\s*$")
# `block` rules MAY be a plain assignment (modern Gerrit) — the parser
# also accepts the explicit-prefix form ``block <permission> = group X``
# which appears in some legacy configs.
_LEGACY_BLOCK_RE = re.compile(
    r"^\s*block\s+(?P<perm>[A-Za-z][A-Za-z]*)\s*=\s*group\s+(?P<group>\S+)\s*$",
)


def parse_block_lines(text: str) -> dict[str, dict[str, set[str]]]:
    """Return ``{group_name: {scope: {permission, ...}, ...}, ...}``.

    Only ``block`` rules are extracted. ``allow`` and label rules are
    not part of the parity audit.
    """
    by_group: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set),
    )
    scope = "<no-access-section>"
    for raw_line in text.splitlines():
        # Strip inline comments (after `#`) — keeps the line parseable
        # while preserving comment-only lines that won't match anyway.
        line = raw_line.split("#", 1)[0]
        section_match = _SECTION_RE.match(line)
        if section_match:
            scope = section_match.group("scope")
            continue
        match = _BLOCK_RE.match(line) or _LEGACY_BLOCK_RE.match(line)
        if not match:
            continue
        by_group[match.group("group")][scope].add(match.group("perm"))
    return {g: {s: perms for s, perms in scopes.items()}
            for g, scopes in by_group.items()}


def audit_text(
    text: str,
    *,
    source: str,
) -> int:
    """Compare deny sets between the two groups. Returns exit code.

    0 → parity holds (modulo ``EXCEPTIONS``).
    1 → drift found.
    """
    by_group = parse_block_lines(text)
    ai = by_group.get("ai-reviewer-bots", {})
    merger = by_group.get("merger-agent-bot", {})

    if not ai:
        print(
            f"[{source}] WARN: no `block group ai-reviewer-bots` rules "
            f"found — the audit has nothing to compare against; this "
            f"is unexpected and probably means the file is not a valid "
            f"project.config.",
            file=sys.stderr,
        )
        return 1
    if not merger:
        print(
            f"[{source}] FAIL: `merger-agent-bot` has NO block rules "
            f"at all. After OP-1196 phase 1α the merger group must "
            f"mirror every O10 block on ai-reviewer-bots EXCEPT "
            f"{sorted(EXCEPTIONS)}.",
            file=sys.stderr,
        )
        return 1

    drift = 0

    all_scopes = set(ai) | set(merger)
    for scope in sorted(all_scopes):
        ai_perms = ai.get(scope, set())
        merger_perms = merger.get(scope, set())
        # Permissions on ai-reviewer-bots that should also be on merger,
        # minus the documented carve-outs.
        missing_on_merger = (ai_perms - merger_perms) - EXCEPTIONS
        # Permissions exclusive to merger (allowed but worth noting).
        extra_on_merger = merger_perms - ai_perms

        if missing_on_merger:
            drift += len(missing_on_merger)
            for perm in sorted(missing_on_merger):
                print(
                    f"[{source}] FAIL drift at [access \"{scope}\"]: "
                    f"`{perm} = block group ai-reviewer-bots` has no "
                    f"matching `block group merger-agent-bot` rule. "
                    f"Either add the mirror block, or — if this is an "
                    f"intentional new carve-out — extend the "
                    f"EXCEPTIONS set in this script with a comment "
                    f"citing the authorising ticket.",
                    file=sys.stderr,
                )

        if extra_on_merger:
            for perm in sorted(extra_on_merger):
                print(
                    f"[{source}] INFO at [access \"{scope}\"]: "
                    f"`{perm}` blocked on merger-agent-bot but not on "
                    f"ai-reviewer-bots. Allowed (merger may be more "
                    f"restricted) — flagged for awareness.",
                    file=sys.stderr,
                )

    # Cross-cut check: every permission in EXCEPTIONS must actually
    # appear as `block group ai-reviewer-bots` somewhere — otherwise
    # the exception is stale (lists a verb that isn't being blocked
    # anyway, suggesting confusion).
    all_ai_perms = {perm for perms in ai.values() for perm in perms}
    stale_exceptions = EXCEPTIONS - all_ai_perms
    if stale_exceptions:
        drift += len(stale_exceptions)
        for perm in sorted(stale_exceptions):
            print(
                f"[{source}] FAIL stale EXCEPTION: `{perm}` is in this "
                f"script's EXCEPTIONS set but NOT blocked on "
                f"ai-reviewer-bots anywhere — remove from EXCEPTIONS "
                f"or restore the ai-reviewer-bots block this exception "
                f"was carving out.",
                file=sys.stderr,
            )

    if drift == 0:
        print(
            f"[{source}] OK — ai-reviewer-bots ↔ merger-agent-bot deny "
            f"parity holds (EXCEPTIONS={sorted(EXCEPTIONS)})."
        )
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-stdin", action="store_true",
        help="Read project.config text from stdin (for auditing the live "
             "refs/meta/config: `git show refs/remotes/gerrit/meta-config:"
             "project.config | scripts/audit_merger_bot_group_denies.py "
             "--from-stdin`). When unset, audit the tracked file(s) listed "
             "in FILES_TO_CHECK.",
    )
    args = parser.parse_args()

    if args.from_stdin:
        return audit_text(sys.stdin.read(), source="stdin")

    exit_code = 0
    repo_root = Path(__file__).resolve().parent.parent
    for relpath in FILES_TO_CHECK:
        full = repo_root / relpath
        if not full.exists():
            print(
                f"[{relpath}] SKIP — file not found; nothing to audit.",
                file=sys.stderr,
            )
            continue
        rc = audit_text(full.read_text(encoding="utf-8"), source=relpath)
        if rc != 0:
            exit_code = rc
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
