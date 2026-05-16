#!/usr/bin/env python3
"""OP-1013 — JIRA OP-project label cleanup: migrate legacy formats + retire stale.

Background
----------
Auditing 191 labels across 982 OP tickets (see
``docs/audit/2026-05-12-audit-29-phase-0-state-audit.md`` and
``scripts/audit_jira_labels.py``) surfaced two classes of cruft:

* **Legacy-format labels** that predate the current ``prefix:Value``
  convention — e.g. ``tier-m`` instead of ``tier:M``, ``agent-class:foo``
  instead of ``class:foo``, ``complexity:high`` (now folded into the
  ``tier:`` scale), plus a handful of labels that should really be
  first-class JIRA fields (Priority, fixVersion, issuelinks).
* **Stale labels** with zero open tickets — old ``priority:*-track``
  buckets, the rc1 ``runner-blocked:waiting-OP-92*`` chain,
  ``refined-by:claude-direct``, and per-ticket one-offs.

This script does both, idempotently, with a mandatory dry-run.

What it does
------------
For every OP issue it computes a *plan* of changes:

1. **Re-label** — add the canonical label, drop the legacy one
   (``tier-m`` → ``tier:M`` etc.; mappings in ``LABEL_RENAMES`` and
   ``COMPLEXITY_TO_TIER``).
2. **Promote to field** — ``priority:high`` → JIRA Priority field;
   ``release:v0.5.0-rc1`` → JIRA fixVersion; the legacy label is then
   dropped (mappings in ``PRIORITY_LABEL_TO_FIELD`` and
   ``RELEASE_LABEL_TO_FIXVERSION``).
3. **Promote to issuelink** — ``parent:OP-N`` / ``blocked-by:OP-N`` /
   ``blocks:OP-N`` → a ``Blocks`` issuelink in the correct direction
   (Atlassian semantics: ``inwardIssue`` = blocker, ``outwardIssue`` =
   blocked — see ``scripts/audit_blockedby_directions.py`` and the
   OP-874 incident), the legacy label dropped afterwards.
4. **Retire** — drop labels matched by ``RETIRE_EXACT`` /
   ``RETIRE_PATTERNS`` (the ``priority:*-track`` buckets,
   ``refined-by:claude-direct``, the ``runner-blocked:waiting-OP-92*``
   rc1 chain) and — only when ``--retire-oneoffs`` is passed — labels
   used by ``<= --oneoff-threshold`` tickets that aren't on the
   canonical allowlist.

``--dry-run`` is the default: it prints the plan (and, with
``--report-file``, dumps it to a file). This script intentionally does
*not* write under ``docs/`` — the human-facing conventions doc
(``docs/sop/jira-label-conventions.md``, AC §1 last bullet) is out of
this ticket's area and is left to a docs-area follow-up. ``--execute``
actually performs the JIRA writes.

Canonical label registry
-------------------------
``CANONICAL_PREFIXES`` + ``CANONICAL_BARE`` define what a *valid* label
looks like post-migration; ``is_canonical_label()`` is importable so a
lint hook / ``scripts/file_jira_ticket.py`` can reject new tickets that
use a retired or legacy label. ``--validate LABEL...`` exercises it from
the CLI (exit 1 if any label is non-canonical).

Usage
-----
::

  # dry-run plan (default), human-readable:
  scripts/jira-label-migrate.py

  # dry-run, also dump the plan as JSON:
  scripts/jira-label-migrate.py --report-file /tmp/label-plan.json --json

  # actually apply (operator-supervised — writes to production JIRA):
  scripts/jira-label-migrate.py --execute

  # also retire one-off labels (<=5 tickets, not on allowlist):
  scripts/jira-label-migrate.py --execute --retire-oneoffs

  # lint a candidate label list (CI / pre-commit / file_jira_ticket.py):
  scripts/jira-label-migrate.py --validate area:backend tier:S class:foo

Exit codes
----------
* 0 — dry-run printed, or ``--execute`` completed with no write errors,
  or ``--validate`` saw only canonical labels.
* 1 — ``--validate`` saw a non-canonical label, or ``--execute`` hit
  one or more write errors (details on stderr).
* 2 — JIRA connectivity / config failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CRED_DIR = Path("~/.config/omnisight").expanduser()
USER_AGENT = "OmniSight-jira-label-migrate/1.0 (OP-1013)"

OPEN_STATUSES = {
    "To Do", "In Progress", "進行中", "Under Review", "承認済み",
    "Open", "Reopened",
}

# --------------------------------------------------------------------------
# Mapping tables — the codified migration rules (AC §1).
# --------------------------------------------------------------------------

# 1. Plain legacy → canonical label renames.
LABEL_RENAMES: dict[str, str] = {
    # hyphen-form tier → colon-form tier (Tier S/M/L/X scale, ADR-0011).
    "tier-s": "tier:S",
    "tier-m": "tier:M",
    "tier-l": "tier:L",
    "tier-x": "tier:X",
    # also accept the all-caps hyphen variants seen in a few old tickets.
    "tier-S": "tier:S",
    "tier-M": "tier:M",
    "tier-L": "tier:L",
    "tier-X": "tier:X",
}

# Prefix renames applied to any label of the form ``<old>:<rest>``.
PREFIX_RENAMES: dict[str, str] = {
    # OP-855 capability matrix consolidated agent-class hints under ``class:``.
    "agent-class": "class",
}

# 2. ``complexity:*`` → ``tier:*`` (folded into the Tier scale).
COMPLEXITY_TO_TIER: dict[str, str] = {
    "complexity:trivial": "tier:S",
    "complexity:low": "tier:S",
    "complexity:small": "tier:S",
    "complexity:medium": "tier:M",
    "complexity:moderate": "tier:M",
    "complexity:high": "tier:L",
    "complexity:large": "tier:L",
    "complexity:very-high": "tier:X",
    "complexity:xl": "tier:X",
    "complexity:extreme": "tier:X",
}

# 3. ``priority:<level>`` label → JIRA Priority field name.
#    (These are the *severity* priority labels, NOT the ``priority:*-track``
#    portfolio-bucket labels, which are retired wholesale below.)
PRIORITY_LABEL_TO_FIELD: dict[str, str] = {
    "priority:critical": "Highest",
    "priority:high": "High",
    "priority:medium": "Medium",
    "priority:normal": "Medium",
    "priority:low": "Low",
    "priority:lowest": "Lowest",
    "priority:trivial": "Lowest",
}

# 4. ``release:<ver>`` label → JIRA fixVersion name.
RELEASE_LABEL_TO_FIXVERSION: dict[str, str] = {
    "release:v0.5.0-rc1": "v0.5.0-rc1",
    "release:v0.5.0-rc2": "v0.5.0-rc2",
    "release:v0.5.0": "v0.5.0",
}

# 5. Label → issuelink. Each entry is keyed by the *prefix*; the value is
#    a function describing how to turn ``<prefix>:OP-N`` on issue K into a
#    Blocks link. Returns (inward_key, outward_key) — inward = blocker.
def _parent_link(this_key: str, other_key: str) -> tuple[str, str]:
    # ``parent:OP-N`` on K  ⇒  OP-N is the parent ⇒ parent blocks child.
    return (other_key, this_key)


def _blocked_by_link(this_key: str, other_key: str) -> tuple[str, str]:
    # ``blocked-by:OP-N`` on K  ⇒  OP-N blocks K.
    return (other_key, this_key)


def _blocks_link(this_key: str, other_key: str) -> tuple[str, str]:
    # ``blocks:OP-N`` on K  ⇒  K blocks OP-N.
    return (this_key, other_key)


LINK_LABEL_PREFIXES: dict[str, Any] = {
    "parent": _parent_link,
    "blocked-by": _blocked_by_link,
    "blockedby": _blocked_by_link,
    "blocks": _blocks_link,
}
_OP_KEY_RE = re.compile(r"^(OP-\d+)$")

# --------------------------------------------------------------------------
# Retirement rules (AC §1 "Retire: ...").
# --------------------------------------------------------------------------

# The 11 portfolio ``priority:*-track`` buckets (hd/mp/rpg/cl/l4/l5/bp/he/
# fx2/wp + the original fx) + the two named one-offs.
RETIRE_EXACT: set[str] = {
    "priority:hd-track",
    "priority:mp-track",
    "priority:rpg-track",
    "priority:cl-track",
    "priority:l4-track",
    "priority:l5-track",
    "priority:bp-track",
    "priority:he-track",
    "priority:fx-track",
    "priority:fx2-track",
    "priority:wp-track",
    "refined-by:claude-direct",
}

RETIRE_PATTERNS: list[re.Pattern[str]] = [
    # any future ``priority:<x>-track`` bucket label.
    re.compile(r"^priority:[a-z0-9]+-track$"),
    # the rc1 ``runner-blocked:waiting-OP-92*`` chain (OP-920..929).
    re.compile(r"^runner-blocked:waiting-OP-92\d$"),
]

# --------------------------------------------------------------------------
# Canonical-label registry — what a *valid* post-migration label looks like.
# Used by ``is_canonical_label`` (lint hook / file_jira_ticket.py / CI).
# --------------------------------------------------------------------------

# prefixes that take a free-ish value (``prefix:value``).
CANONICAL_PREFIXES: set[str] = {
    "area",        # area:backend, area:devops, ...
    "class",       # class:subscription-codex, class:api-anthropic, ...
    "tier",        # tier:S / tier:M / tier:L / tier:X
    "scope",       # scope:adr, scope:audit, ...
    "meta",        # meta:retrospective, ...
    "runner",      # runner:no-commits-expected, runner:atomic-claim, ...
    "release",     # release:v0.5.0-rc2 (transitional — prefer fixVersion)
    "epic",        # epic:<slug>
    "sprint",      # sprint:G, sprint:H, ...
    "agent",       # agent:auto (set by scripts/file_jira_ticket.py)
    "type",        # type:story / type:bug / ... (set by file_jira_ticket.py)
    "label",       # label:cleanup follow-ups (LABEL-CLEANUP family)
}

# allowed values for the enumerated prefixes (None ⇒ free value).
CANONICAL_PREFIX_VALUES: dict[str, set[str] | None] = {
    "tier": {"S", "M", "L", "X"},
    "area": None,
    "class": None,
    "scope": None,
    "meta": None,
    "runner": None,
    "release": None,
    "epic": None,
    "sprint": None,
}

# bare (prefix-less) labels — and a couple of legitimate marker labels that
# happen to contain a colon but are *not* severity/portfolio labels — that
# remain legitimate post-migration.
CANONICAL_BARE: set[str] = {
    "migrated-from-todo",
    "migrated-from-todo-bulk",
    "rc-blocker",
    "good-first-ticket",
    "needs-operator-input",
    "priority:meta",  # marker for portfolio META tickets — not a severity
}


def is_canonical_label(label: str) -> bool:
    """True iff ``label`` conforms to the post-OP-1013 convention.

    A label is canonical when it is either an allow-listed bare label, or
    of the form ``prefix:value`` where ``prefix`` is in
    ``CANONICAL_PREFIXES`` and (if the prefix is enumerated) ``value`` is
    one of its allowed values. Retired / legacy labels are *not*
    canonical even though they may still be syntactically ``prefix:value``.
    """
    if label in CANONICAL_BARE:
        return True
    if label in RETIRE_EXACT or any(p.match(label) for p in RETIRE_PATTERNS):
        return False
    if label in LABEL_RENAMES or label in COMPLEXITY_TO_TIER:
        return False
    if label in PRIORITY_LABEL_TO_FIELD or label in RELEASE_LABEL_TO_FIXVERSION:
        return False
    if ":" not in label:
        return False
    prefix, value = label.split(":", 1)
    if prefix in PREFIX_RENAMES:
        return False
    if prefix in LINK_LABEL_PREFIXES:
        return False
    if prefix not in CANONICAL_PREFIXES:
        return False
    allowed = CANONICAL_PREFIX_VALUES.get(prefix)
    if allowed is not None and value not in allowed:
        return False
    return bool(value)


# --------------------------------------------------------------------------
# JIRA REST plumbing (same credential layout as scripts/audit_jira_labels.py).
# --------------------------------------------------------------------------

def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _jira_config() -> tuple[str, str, str]:
    env = _load_env(CRED_DIR / "jira-claude.env")
    token = (CRED_DIR / "jira-claude-token").read_text().strip()
    auth = "Basic " + b64encode(
        f"{env['OMNISIGHT_JIRA_CLAUDE_EMAIL']}:{token}".encode()
    ).decode()
    return (
        env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/"),
        env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP"),
        auth,
    )


def _request(method: str, url: str, auth: str, body: dict | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"{method} {url} -> {exc.code}: {text}") from exc


def fetch_all_issues(site: str, project: str, auth: str) -> list[dict[str, Any]]:
    """Page through every OP issue, keeping labels / priority / fixVersions / links."""
    issues: list[dict[str, Any]] = []
    next_token: str | None = None
    fields = "labels,status,priority,fixVersions,issuelinks,summary"
    page = 0
    while True:
        page += 1
        params = {
            "jql": f"project = {project} ORDER BY created ASC",
            "fields": fields,
            "maxResults": "100",
        }
        if next_token:
            params["nextPageToken"] = next_token
        url = site + "/rest/api/3/search/jql?" + urllib.parse.urlencode(params)
        try:
            resp = _request("GET", url, auth)
        except RuntimeError as exc:
            if "410" in str(exc) or "404" in str(exc):
                url = site + "/rest/api/3/search?" + urllib.parse.urlencode({
                    "jql": f"project = {project} ORDER BY created ASC",
                    "fields": fields,
                    "maxResults": "100",
                    "startAt": str(len(issues)),
                })
                resp = _request("GET", url, auth)
            else:
                raise
        batch = resp.get("issues", [])
        issues.extend(batch)
        print(f"  page {page}: +{len(batch)} (cum {len(issues)})", file=sys.stderr)
        if resp.get("nextPageToken"):
            next_token = resp["nextPageToken"]
            continue
        if resp.get("isLast") or len(batch) < 100:
            break
        if "startAt" in resp and resp.get("startAt", 0) + len(batch) >= resp.get("total", 0):
            break
        if page > 200:
            print("  WARN: 200-page safety cap hit", file=sys.stderr)
            break
    return issues


# --------------------------------------------------------------------------
# Plan computation.
# --------------------------------------------------------------------------

@dataclass
class IssuePlan:
    key: str
    summary: str
    labels_add: list[str] = field(default_factory=list)
    labels_remove: list[str] = field(default_factory=list)
    set_priority: str | None = None
    add_fixversions: list[str] = field(default_factory=list)
    add_links: list[tuple[str, str, str]] = field(default_factory=list)  # (inward, outward, type)

    def is_noop(self) -> bool:
        return not (
            self.labels_add or self.labels_remove or self.set_priority
            or self.add_fixversions or self.add_links
        )


def _existing_blocks_links(fields: dict[str, Any]) -> set[tuple[str, str]]:
    """Existing ``Blocks`` links, normalised relative to *this* issue.

    Atlassian populates exactly one side of each link object: if
    ``inwardIssue`` is set then *this* issue is the outward (blocked)
    side, so we record ``("<self>", other)``; if ``outwardIssue`` is set
    then *this* issue is the inward (blocker), recorded as
    ``(other, "<self>")``.
    """
    out: set[tuple[str, str]] = set()
    for link in fields.get("issuelinks") or []:
        if ((link.get("type") or {}).get("name")) != "Blocks":
            continue
        if link.get("inwardIssue"):
            # inward = "is blocked by": this issue is blocked by inwardIssue.
            out.add((link["inwardIssue"]["key"], "<self>"))
        if link.get("outwardIssue"):
            # outward = "blocks": this issue blocks outwardIssue.
            out.add(("<self>", link["outwardIssue"]["key"]))
    return out


def compute_plan(
    issues: list[dict[str, Any]],
    *,
    retire_oneoffs: bool,
    oneoff_threshold: int,
    skip_links: bool,
) -> list[IssuePlan]:
    # First pass: per-label total usage (for the one-off heuristic).
    usage: dict[str, int] = defaultdict(int)
    for issue in issues:
        for lab in (issue.get("fields", {}).get("labels") or []):
            usage[lab] += 1

    plans: list[IssuePlan] = []
    for issue in issues:
        key = issue["key"]
        fields = issue.get("fields", {}) or {}
        summary = (fields.get("summary") or "")[:80]
        labels = list(fields.get("labels") or [])
        cur_priority = ((fields.get("priority") or {}).get("name"))
        cur_fixv = {v["name"] for v in (fields.get("fixVersions") or [])}
        existing_links = _existing_blocks_links(fields)

        plan = IssuePlan(key=key, summary=summary)
        seen_after: set[str] = set(labels)
        # Is there an explicit ``tier-*`` legacy label on this issue? If so,
        # it (not any ``complexity:*`` label) decides the canonical tier.
        explicit_tier_present = any(lab in LABEL_RENAMES for lab in labels)

        def has_canonical_tier() -> bool:
            return any(s.startswith("tier:") for s in seen_after)

        for lab in labels:
            # --- retire? ---
            if lab in RETIRE_EXACT or any(p.match(lab) for p in RETIRE_PATTERNS):
                plan.labels_remove.append(lab)
                continue
            if retire_oneoffs and usage[lab] <= oneoff_threshold and not is_canonical_label(lab):
                # don't touch labels that are themselves migration *targets*
                if lab not in LABEL_RENAMES and lab not in COMPLEXITY_TO_TIER \
                        and lab not in PRIORITY_LABEL_TO_FIELD \
                        and lab not in RELEASE_LABEL_TO_FIXVERSION \
                        and ":" in lab and lab.split(":", 1)[0] not in (
                            set(PREFIX_RENAMES) | set(LINK_LABEL_PREFIXES)):
                    plan.labels_remove.append(lab)
                    continue

            # --- plain rename (incl. tier-* -> tier:*) ---
            if lab in LABEL_RENAMES:
                plan.labels_remove.append(lab)
                tgt = LABEL_RENAMES[lab]
                # tier-* renames: skip the add if the issue already carries a
                # canonical ``tier:`` label (don't create a conflicting pair).
                if tgt.startswith("tier:") and has_canonical_tier():
                    continue
                if tgt not in seen_after:
                    plan.labels_add.append(tgt)
                    seen_after.add(tgt)
                continue

            # --- prefix rename (agent-class: -> class:) ---
            if ":" in lab and lab.split(":", 1)[0] in PREFIX_RENAMES:
                old_pref, rest = lab.split(":", 1)
                tgt = f"{PREFIX_RENAMES[old_pref]}:{rest}"
                plan.labels_remove.append(lab)
                if tgt not in seen_after:
                    plan.labels_add.append(tgt)
                    seen_after.add(tgt)
                continue

            # --- complexity: -> tier: (only "where mappable" — i.e. when
            #     the issue has no other tier signal; an explicit tier-* /
            #     tier: label always wins over the complexity inference) ---
            if lab in COMPLEXITY_TO_TIER:
                plan.labels_remove.append(lab)
                tgt = COMPLEXITY_TO_TIER[lab]
                if not explicit_tier_present and not has_canonical_tier():
                    plan.labels_add.append(tgt)
                    seen_after.add(tgt)
                continue

            # --- priority:<level> -> Priority field ---
            if lab in PRIORITY_LABEL_TO_FIELD:
                plan.labels_remove.append(lab)
                want = PRIORITY_LABEL_TO_FIELD[lab]
                if cur_priority != want and plan.set_priority is None:
                    plan.set_priority = want
                continue

            # --- release:<ver> -> fixVersion ---
            if lab in RELEASE_LABEL_TO_FIXVERSION:
                plan.labels_remove.append(lab)
                ver = RELEASE_LABEL_TO_FIXVERSION[lab]
                if ver not in cur_fixv and ver not in plan.add_fixversions:
                    plan.add_fixversions.append(ver)
                continue

            # --- parent:/blocked-by:/blocks: -> Blocks issuelink ---
            if ":" in lab and lab.split(":", 1)[0] in LINK_LABEL_PREFIXES and not skip_links:
                pref, rest = lab.split(":", 1)
                rest = rest.strip()
                m = _OP_KEY_RE.match(rest)
                if not m or m.group(1) == key:
                    # malformed / self-reference — leave the label, flag it.
                    continue
                other = m.group(1)
                inward, outward = LINK_LABEL_PREFIXES[pref](key, other)
                # has an equivalent Blocks link already? (existing_links is
                # normalised with "<self>" standing in for this issue)
                already = False
                if inward == key and ("<self>", other) in existing_links:
                    already = True
                if outward == key and (other, "<self>") in existing_links:
                    already = True
                plan.labels_remove.append(lab)
                if not already and (inward, outward, "Blocks") not in plan.add_links:
                    plan.add_links.append((inward, outward, "Blocks"))
                continue

        if not plan.is_noop():
            plans.append(plan)
    return plans


# --------------------------------------------------------------------------
# Plan execution.
# --------------------------------------------------------------------------

def execute_plan(site: str, auth: str, plans: list[IssuePlan], *, sleep: float) -> int:
    errors = 0
    for i, plan in enumerate(plans, 1):
        # 1. labels (single PUT with add/remove ops)
        ops = [{"add": lab} for lab in plan.labels_add] + \
              [{"remove": lab} for lab in plan.labels_remove]
        try:
            if ops:
                _request("PUT", f"{site}/rest/api/3/issue/{plan.key}", auth,
                         {"update": {"labels": ops}})
        except RuntimeError as exc:
            print(f"  ✗ {plan.key} labels: {exc}", file=sys.stderr)
            errors += 1

        # 2. priority + fixVersion (one PUT)
        field_update: dict[str, Any] = {}
        if plan.set_priority:
            field_update["priority"] = {"name": plan.set_priority}
        if plan.add_fixversions:
            field_update["fixVersions"] = [{"add": {"name": v}} for v in plan.add_fixversions]
        try:
            if "priority" in field_update and "fixVersions" not in field_update:
                _request("PUT", f"{site}/rest/api/3/issue/{plan.key}", auth,
                         {"fields": {"priority": field_update["priority"]}})
            elif "fixVersions" in field_update and "priority" not in field_update:
                _request("PUT", f"{site}/rest/api/3/issue/{plan.key}", auth,
                         {"update": {"fixVersions": field_update["fixVersions"]}})
            elif field_update:
                _request("PUT", f"{site}/rest/api/3/issue/{plan.key}", auth, {
                    "fields": {"priority": field_update["priority"]},
                    "update": {"fixVersions": field_update["fixVersions"]},
                })
        except RuntimeError as exc:
            print(f"  ✗ {plan.key} fields: {exc}", file=sys.stderr)
            errors += 1

        # 3. issuelinks
        for inward, outward, ltype in plan.add_links:
            try:
                _request("POST", f"{site}/rest/api/3/issueLink", auth, {
                    "type": {"name": ltype},
                    "inwardIssue": {"key": inward},
                    "outwardIssue": {"key": outward},
                })
            except RuntimeError as exc:
                print(f"  ✗ {plan.key} link {inward}->{outward}: {exc}", file=sys.stderr)
                errors += 1

        if i % 25 == 0:
            print(f"  ... {i}/{len(plans)} issues processed", file=sys.stderr)
        if sleep:
            time.sleep(sleep)
    return errors


# --------------------------------------------------------------------------
# Reporting.
# --------------------------------------------------------------------------

def render_report(plans: list[IssuePlan], *, total_issues: int) -> str:
    lines: list[str] = []
    lines.append("# JIRA OP label-migration plan (OP-1013)")
    lines.append("")
    lines.append(f"- issues scanned: {total_issues}")
    lines.append(f"- issues with changes: {len(plans)}")
    n_add = sum(len(p.labels_add) for p in plans)
    n_rm = sum(len(p.labels_remove) for p in plans)
    n_prio = sum(1 for p in plans if p.set_priority)
    n_fixv = sum(len(p.add_fixversions) for p in plans)
    n_link = sum(len(p.add_links) for p in plans)
    lines.append(f"- label adds: {n_add} · label removes: {n_rm}")
    lines.append(f"- priority sets: {n_prio} · fixVersion adds: {n_fixv} · links created: {n_link}")
    removed_labels: dict[str, int] = defaultdict(int)
    for p in plans:
        for lab in p.labels_remove:
            removed_labels[lab] += 1
    lines.append("")
    lines.append("## Labels being removed (count)")
    lines.append("")
    for lab, n in sorted(removed_labels.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- `{lab}` × {n}")
    lines.append("")
    lines.append("## Per-issue plan")
    lines.append("")
    for p in plans:
        bits: list[str] = []
        if p.labels_add:
            bits.append("add[" + ", ".join(p.labels_add) + "]")
        if p.labels_remove:
            bits.append("rm[" + ", ".join(p.labels_remove) + "]")
        if p.set_priority:
            bits.append(f"priority={p.set_priority}")
        if p.add_fixversions:
            bits.append("fixVersion[" + ", ".join(p.add_fixversions) + "]")
        if p.add_links:
            bits.append("link[" + ", ".join(f"{a}→{b}" for a, b, _ in p.add_links) + "]")
        lines.append(f"- {p.key} ({p.summary}): " + "; ".join(bits))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="OP JIRA label cleanup migration (OP-1013)")
    parser.add_argument("--execute", action="store_true",
                        help="actually perform JIRA writes (default: dry-run)")
    parser.add_argument("--retire-oneoffs", action="store_true",
                        help="also retire labels used by <= --oneoff-threshold non-canonical tickets")
    parser.add_argument("--oneoff-threshold", type=int, default=5,
                        help="usage count at/below which a non-canonical label is a 'one-off' (default 5)")
    parser.add_argument("--skip-links", action="store_true",
                        help="don't convert parent:/blocked-by:/blocks: labels into issuelinks")
    parser.add_argument("--report-file", type=Path, default=None,
                        help="also write the Markdown plan here")
    parser.add_argument("--json", action="store_true",
                        help="emit the plan as JSON to stdout instead of Markdown")
    parser.add_argument("--sleep", type=float, default=0.1,
                        help="seconds to sleep between issues in --execute (default 0.1)")
    parser.add_argument("--validate", nargs="+", metavar="LABEL", default=None,
                        help="lint mode: check the given labels against the canonical registry and exit")
    args = parser.parse_args(argv)

    # --- lint mode (no JIRA call) ---
    if args.validate is not None:
        bad = [lab for lab in args.validate if not is_canonical_label(lab)]
        for lab in args.validate:
            mark = "ok " if lab not in bad else "BAD"
            print(f"  [{mark}] {lab}")
        if bad:
            print(f"\n{len(bad)} non-canonical label(s): {', '.join(bad)}", file=sys.stderr)
            return 1
        return 0

    try:
        site, project, auth = _jira_config()
    except (KeyError, OSError) as exc:
        print(f"JIRA config error: {exc}", file=sys.stderr)
        return 2

    print(f"Fetching {project} issues from {site} ...", file=sys.stderr)
    try:
        issues = fetch_all_issues(site, project, auth)
    except RuntimeError as exc:
        print(f"JIRA fetch failed: {exc}", file=sys.stderr)
        return 2
    print(f"  total issues: {len(issues)}", file=sys.stderr)

    plans = compute_plan(
        issues,
        retire_oneoffs=args.retire_oneoffs,
        oneoff_threshold=args.oneoff_threshold,
        skip_links=args.skip_links,
    )

    if args.json:
        print(json.dumps({
            "total_issues": len(issues),
            "changed_issues": len(plans),
            "plans": [asdict(p) for p in plans],
        }, indent=2))
    else:
        report = render_report(plans, total_issues=len(issues))
        print(report)
        if args.report_file:
            args.report_file.write_text(report, encoding="utf-8")
            print(f"\n[report written to {args.report_file}]", file=sys.stderr)

    if not args.execute:
        print("\n(dry-run — pass --execute to apply; this writes to production JIRA)",
              file=sys.stderr)
        return 0

    print(f"\nExecuting {len(plans)} issue updates ...", file=sys.stderr)
    errors = execute_plan(site, auth, plans, sleep=args.sleep)
    if errors:
        print(f"\nDONE with {errors} write error(s) — see above.", file=sys.stderr)
        return 1
    print("\nDONE — all updates applied.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
