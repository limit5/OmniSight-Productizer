#!/usr/bin/env python3
"""Triage the migrated-from-todo backlog dump (OP-1014).

Three legacy labels — ``runner-needs-refinement``, ``migrated-from-todo-bulk``
and ``migrated-from-todo`` — collectively tag ~1k open OP tickets that were
bulk-imported from ``TODO.md`` without refinement. They are invisible to the
runner JQL (no ``area:`` / ``tier:`` / ``class:`` labels) yet show up in every
project-wide query, drowning capacity planning and sprint review. See the
"Bulk import without refinement creates dead inventory" anti-pattern
(``docs/sop/architecture-anti-patterns.md`` §13, lesson L-OP-1014).

This script has two modes:

``triage`` (read-only, default)
    Fetch every OPEN ticket carrying at least one legacy label, run the
    classification heuristic, and emit (a) a Markdown report and (b) a
    machine-readable JSONL decision file. Nothing is mutated. The JSONL file
    is meant to be eyeballed (and hand-edited) by an operator before ``apply``.

``apply`` (mutating — operator-supervised)
    Read a JSONL decision file and, for each ticket classified ``abandon`` (or
    ``duplicate`` with ``--allow-duplicate-apply``), post a comment recording
    the triage rationale and run the "Won't Do" workflow transition (→ status
    ``Archived``). Defaults to dry-run; pass ``--execute`` to actually mutate.
    Resolution-field note: the OP workflow's transition screens carry no
    fields and ``resolution`` is not in issue editmeta, so this script records
    the "won't do reason" as a structured ticket comment rather than the
    native resolution field. It still *attempts* to set ``resolution`` on the
    transition POST (``--wont-do-resolution-id``, default 10001 = 対応しない /
    Won't Do) and falls back to transition-only + comment if JIRA rejects it.

== Classification heuristic ==

For each open ticket that carries >=1 legacy label, in priority order:

  KEEP / REFINE — there is a live signal the ticket still matters:
    * status is past "To Do" (someone already started it), OR
    * it has a fixVersion, OR
    * it is assigned to a human, OR
    * it has a comment authored by a non-bot account, OR
    * it was updated within --fresh-days (default 14) days, OR
    * it is younger than --abandon-age-days (default 30) days
      (too new to declare dead — fail safe toward keeping).

  ABANDON — old + cold + never started:
    * created more than --abandon-age-days days ago, AND
    * status is still "To Do", AND
    * no fixVersion, AND
    * not assigned, AND
    * no non-bot comments, AND
    * not updated within --fresh-days.

  DUPLICATE (flag only; never auto-applied unless --allow-duplicate-apply) —
    its normalised summary (lowercased, whitespace-collapsed) is shared by an
    older sibling ticket. The *newest* member of a duplicate cluster keeps the
    REFINE/ABANDON verdict from the rules above; the older members are flagged
    DUPLICATE so a human can confirm-and-close.

Tie-break rule: when a ticket would qualify for both KEEP and ABANDON, KEEP
wins. We would rather leave a dead ticket in the backlog than archive a live
one — un-archiving is manual and the survivor set is small.

== Usage ==

    # read-only triage → report + decisions JSONL
    scripts/jira-todo-backlog-triage.py triage \
        --report docs/audit/2026-05-13-todo-backlog-triage.md \
        --decisions /tmp/op-1014-decisions.jsonl

    # dry-run the bulk action (no mutation)
    scripts/jira-todo-backlog-triage.py apply --decisions /tmp/op-1014-decisions.jsonl

    # actually archive the 'abandon' set, 50 at a time
    scripts/jira-todo-backlog-triage.py apply --decisions /tmp/op-1014-decisions.jsonl \
        --execute --limit 50
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

CRED_DIR = Path("~/.config/omnisight").expanduser()
USER_AGENT = "OmniSight-jira-todo-backlog-triage/1.0"

LEGACY_LABELS = ["runner-needs-refinement", "migrated-from-todo-bulk", "migrated-from-todo"]

# Status names (JP locale) that mean "work has started" — never auto-archive these.
STARTED_STATUSES = {"進行中", "In Progress", "Under Review", "承認済み", "Approved", "Reopened"}
TODO_STATUSES = {"To Do", "Open", "未対応"}
# statusCategory.key for "not done" — used as the open-ticket filter in JQL.

# Accounts whose comments do NOT count as a "human refined this" signal.
BOT_COMMENT_AUTHORS = {"claude-bot", "codex-bot", "merger-agent-bot", "merger-bot",
                       "lint-bot", "security-bot", "automation", "Automation for Jira"}

DEFAULT_ABANDON_AGE_DAYS = 30
DEFAULT_FRESH_DAYS = 14
DEFAULT_WONT_DO_RESOLUTION_ID = "10001"  # 対応しない / Won't Do on soraapp.atlassian.net


# ── JIRA plumbing (mirrors scripts/audit_jira_labels.py) ──────────────────────

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
    auth = "Basic " + b64encode(f"{env['OMNISIGHT_JIRA_CLAUDE_EMAIL']}:{token}".encode()).decode()
    return env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/"), env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP"), auth


def _request(method: str, url: str, auth: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"{method} {url} -> {exc.code}: {text}") from exc


def _get(url: str, auth: str) -> dict[str, Any]:
    return _request("GET", url, auth)


def _adf(text: str) -> dict[str, Any]:
    """Minimal ADF wrapper (plain paragraph) for the comment body."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# ── Fetch ─────────────────────────────────────────────────────────────────────

FETCH_FIELDS = "summary,status,created,updated,fixVersions,assignee,labels,issuetype,comment,parent"


def fetch_legacy_tickets(site: str, project: str, auth: str, labels: list[str]) -> list[dict[str, Any]]:
    """All open (statusCategory != Done) tickets carrying >=1 of ``labels``."""
    label_clause = ", ".join(f'"{lab}"' for lab in labels)
    jql = f"project = {project} AND labels in ({label_clause}) AND statusCategory != Done ORDER BY created ASC"
    issues: list[dict[str, Any]] = []
    next_token: str | None = None
    page = 0
    while True:
        page += 1
        params = {"jql": jql, "fields": FETCH_FIELDS, "maxResults": "100"}
        if next_token:
            params["nextPageToken"] = next_token
        resp = _get(site + "/rest/api/3/search/jql?" + urllib.parse.urlencode(params), auth)
        batch = resp.get("issues", [])
        issues.extend(batch)
        print(f"  page {page}: +{len(batch)} (cumulative {len(issues)})", file=sys.stderr)
        if resp.get("isLast") or not resp.get("nextPageToken"):
            break
        next_token = resp["nextPageToken"]
        if page > 100:
            print("  WARN: stopping at 100 pages safety cap", file=sys.stderr)
            break
    return issues


# ── Classification ────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> datetime:
    # JIRA timestamps look like 2026-05-07T12:34:56.789+0000
    s = s.strip()
    if not s:
        return datetime.now(timezone.utc)
    s2 = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        return datetime.fromisoformat(s2)
    except ValueError:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _norm_summary(summary: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", summary.lower())).strip()


def _author_name(comment: dict[str, Any]) -> str:
    a = comment.get("author") or {}
    return a.get("displayName") or a.get("name") or a.get("emailAddress") or ""


def classify(
    issue: dict[str, Any],
    now: datetime,
    abandon_age_days: int,
    fresh_days: int,
) -> tuple[str, list[str]]:
    """Return ``(verdict, signals)`` where verdict is keep|abandon."""
    f = issue.get("fields", {})
    status = (f.get("status") or {}).get("name", "")
    created = _parse_dt(f.get("created", ""))
    updated = _parse_dt(f.get("updated", ""))
    fix_versions = f.get("fixVersions") or []
    assignee = f.get("assignee")
    comments = ((f.get("comment") or {}).get("comments")) or []
    human_comments = [c for c in comments if _author_name(c) not in BOT_COMMENT_AUTHORS]

    age_days = (now - created).days
    idle_days = (now - updated).days

    signals: list[str] = []
    if status not in TODO_STATUSES:
        signals.append(f"started(status={status})")
    if fix_versions:
        signals.append("has-fixVersion(" + ",".join(v.get("name", "?") for v in fix_versions) + ")")
    if assignee:
        signals.append("assigned(" + (assignee.get("displayName") or "?") + ")")
    if human_comments:
        signals.append(f"human-comments({len(human_comments)})")
    if idle_days < fresh_days:
        signals.append(f"fresh(updated {idle_days}d ago)")
    if age_days < abandon_age_days:
        signals.append(f"young(created {age_days}d ago)")

    verdict = "keep" if signals else "abandon"
    return verdict, signals


def find_duplicate_clusters(issues: list[dict[str, Any]]) -> dict[str, str]:
    """Map ticket key → key of the *newest* sibling sharing its normalised summary.

    Only keys that have at least one older sibling are present in the result;
    the value is the cluster's survivor (newest by ``created``)."""
    by_norm: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for it in issues:
        f = it.get("fields", {})
        norm = _norm_summary(f.get("summary", ""))
        if not norm:
            continue
        by_norm[norm].append((_parse_dt(f.get("created", "")), it["key"]))
    dup: dict[str, str] = {}
    for norm, members in by_norm.items():
        if len(members) < 2:
            continue
        members.sort()  # oldest first
        survivor = members[-1][1]  # newest
        for _, key in members[:-1]:
            dup[key] = survivor
    return dup


# ── Triage mode ───────────────────────────────────────────────────────────────

def cmd_triage(args: argparse.Namespace) -> int:
    site, project, auth = _jira_config()
    labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    print(f"Fetching open {project} tickets with labels in {labels} ...", file=sys.stderr)
    issues = fetch_legacy_tickets(site, project, auth, labels)
    now = datetime.now(timezone.utc)
    print(f"Fetched {len(issues)} open legacy-label tickets.\n", file=sys.stderr)

    dup_map = find_duplicate_clusters(issues)

    rows: list[dict[str, Any]] = []
    counts = defaultdict(int)
    by_label_counts: dict[str, dict[str, int]] = {lab: defaultdict(int) for lab in labels}
    for it in issues:
        f = it.get("fields", {})
        verdict, signals = classify(it, now, args.abandon_age_days, args.fresh_days)
        cls = verdict  # keep | abandon
        if it["key"] in dup_map:
            cls = "duplicate"
        counts[cls] += 1
        ticket_labels = [l for l in (f.get("labels") or []) if l in labels]
        for lab in ticket_labels:
            by_label_counts[lab][cls] += 1
        rows.append({
            "key": it["key"],
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "created": f.get("created", "")[:10],
            "updated": f.get("updated", "")[:10],
            "issuetype": (f.get("issuetype") or {}).get("name", ""),
            "fix_versions": [v.get("name") for v in (f.get("fixVersions") or [])],
            "assignee": ((f.get("assignee") or {}) or {}).get("displayName"),
            "parent": ((f.get("parent") or {}) or {}).get("key"),
            "legacy_labels": ticket_labels,
            "classification": cls,
            "verdict": verdict,
            "duplicate_of": dup_map.get(it["key"]),
            "signals": signals,
            "comment": _wont_do_reason(cls, verdict, signals, dup_map.get(it["key"]),
                                       args.abandon_age_days, args.fresh_days),
        })

    rows.sort(key=lambda r: (r["classification"], r["created"], r["key"]))

    # ── decisions JSONL ──
    if args.decisions:
        Path(args.decisions).parent.mkdir(parents=True, exist_ok=True)
        with open(args.decisions, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"Wrote {len(rows)} decisions → {args.decisions}", file=sys.stderr)

    # ── Markdown report ──
    report = _render_report(project, labels, issues, rows, counts, by_label_counts, now, args)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report)
        print(f"Wrote report → {args.report}", file=sys.stderr)
    else:
        print(report)
    return 0


def _wont_do_reason(cls: str, verdict: str, signals: list[str], dup_of: str | None,
                    abandon_age_days: int, fresh_days: int) -> str:
    if cls == "duplicate":
        return (f"[OP-1014 backlog-triage] Flagged as a likely duplicate of {dup_of} "
                f"(identical normalised summary). Confirm and close as 重複/Duplicate, "
                f"or remove the legacy label and refine if genuinely distinct.")
    if cls == "abandon":
        return ("[OP-1014 backlog-triage] Auto-classified ABANDONED: bulk-imported from "
                f"TODO.md, older than {abandon_age_days}d, still in To Do, never assigned, "
                f"no fixVersion, no non-bot comment, untouched for >{fresh_days}d. "
                "Archiving via Won't Do. Reopen if this work is still wanted — but then it "
                "needs proper area:/tier:/class: labels before a runner can pick it up.")
    return ("[OP-1014 backlog-triage] Kept for refinement — live signals: "
            + "; ".join(signals) + ".")


def _render_report(project, labels, issues, rows, counts, by_label_counts, now, args) -> str:
    keep_n = counts.get("keep", 0)
    aband_n = counts.get("abandon", 0)
    dup_n = counts.get("duplicate", 0)
    total = len(rows)
    out: list[str] = []
    a = out.append
    now_cst = now.astimezone(timezone(timedelta(hours=8)))  # audit docs are dated in CST
    a(f"# {now_cst:%Y-%m-%d} — migrated-from-todo backlog triage (OP-1014)")
    a("")
    a(f"**Date**: {now_cst:%Y-%m-%d %H:%M CST} · **Generated by**: `scripts/jira-todo-backlog-triage.py triage` "
      f"· **Project**: {project}")
    a("")
    a("**Heuristic params**: "
      f"`--abandon-age-days {args.abandon_age_days}` · `--fresh-days {args.fresh_days}` · "
      f"`--labels {','.join(labels)}`")
    a("")
    a("## Summary")
    a("")
    a(f"- Open tickets carrying >=1 legacy label (de-duplicated): **{total}**")
    a("  - the raw per-label sums double-count tickets that carry several legacy labels; "
      "this report works on the de-duplicated union.")
    a("- Classification:")
    a(f"  - `keep` (refine) — **{keep_n}** ({keep_n*100//max(total,1)}%) — has a live signal, do not archive")
    a(f"  - `abandon` (Won't Do → Archived) — **{aband_n}** ({aband_n*100//max(total,1)}%) — old + cold + never started")
    a(f"  - `duplicate` (human confirm, then close) — **{dup_n}** ({dup_n*100//max(total,1)}%) — shares a summary with an older sibling")
    a("")
    a(f"Projected backlog after sweep (abandon + confirmed duplicates closed): "
      f"~**{total - aband_n - dup_n}** tickets need refinement; the rest are archived.")
    a("")

    # ── Findings & recommendation (adapts to the data) ──
    created_dates = sorted(r["created"] for r in rows if r["created"])
    oldest = created_dates[0] if created_dates else "?"
    newest = created_dates[-1] if created_dates else "?"
    oldest_age = (now.date() - datetime.fromisoformat(oldest).date()).days if created_dates else 0
    n_todo = sum(1 for r in rows if r["status"] in TODO_STATUSES)
    n_unassigned = sum(1 for r in rows if not r["assignee"])
    n_no_fv = sum(1 for r in rows if not r["fix_versions"])
    a("## Findings & recommendation")
    a("")
    a(f"- Every ticket in this dump was created between **{oldest}** and **{newest}** "
      f"— the oldest is only **{oldest_age} days** old. The whole inventory was bulk-imported "
      f"from `TODO.md` during the 2026-05 governance migration; it is *new* dead-weight, not "
      f"*aged* dead-weight.")
    a(f"- {n_todo}/{total} are still in `To Do`, {n_unassigned}/{total} are unassigned, "
      f"{n_no_fv}/{total} have no fixVersion. So the dump is uniformly un-refined — but by the "
      f"agreed `older-than-{args.abandon_age_days}d` heuristic **{aband_n}** of them are "
      f"archive-eligible *today*.")
    if aband_n == 0 and oldest_age < args.abandon_age_days:
        cutoff = (datetime.fromisoformat(oldest).date() + timedelta(days=args.abandon_age_days))
        a(f"- **Do not bulk-archive yet.** Closing week-old tickets en masse would be the same "
          f"mistake in reverse. The abandon window opens around **{cutoff.isoformat()}** "
          f"(oldest-import + {args.abandon_age_days}d); re-run `triage` then. Until then this "
          f"report's 'keep' list *is* the real backlog — sprint planning should treat it as "
          f"~{total} un-refined items, not assume they are noise that will self-clear.")
        a("- Alternative policy call (above this ticket's pay-grade): if the operator decides the "
          "bulk import itself was a mistake, the `apply` mode can mass-archive on an "
          "operator-edited decisions file — but that is a deliberate revert, not a heuristic "
          "sweep, and should be recorded as such.")
    else:
        a(f"- The `apply` mode (dry-run by default) will comment + run the Won't Do transition "
          f"on the {aband_n}-ticket abandon set. Review the decisions JSONL first.")
    a("")
    a("## Per-label breakdown")
    a("")
    a("| Legacy label | open (this label) | keep | abandon | duplicate |")
    a("|---|--:|--:|--:|--:|")
    for lab in labels:
        c = by_label_counts[lab]
        tot_lab = sum(c.values())
        a(f"| `{lab}` | {tot_lab} | {c.get('keep',0)} | {c.get('abandon',0)} | {c.get('duplicate',0)} |")
    a("")
    a("## Classification heuristic (as run)")
    a("")
    a("A ticket is **kept for refinement** if ANY of these holds (tie-break: keep wins):")
    a("")
    a("1. status is past *To Do* (work already started),")
    a("2. it has a fixVersion,")
    a("3. it is assigned,")
    a("4. it has a comment by a non-bot account,")
    a(f"5. it was updated within {args.fresh_days} days,")
    a(f"6. it is younger than {args.abandon_age_days} days (too new to call dead).")
    a("")
    a("Otherwise it is **abandoned** → Won't Do (status `Archived`). Separately, any ticket whose "
      "normalised summary (lowercased, whitespace-collapsed) is shared by an older sibling is flagged "
      "**duplicate** for human confirmation — the script never auto-closes duplicates unless run with "
      "`--allow-duplicate-apply`.")
    a("")
    a("> Provenance: the bulk import that created this dump is the *\"Bulk import without refinement "
      "creates dead inventory\"* anti-pattern — see `docs/sop/architecture-anti-patterns.md` §13 and "
      "lesson `L-OP-1014`. The cure is enforced going forward by the 4-AC discipline (a ticket with "
      "only a Code AC is shipped-but-not-deployed by design) and by `scripts/file_jira_ticket.py`'s "
      "file-time label checks.")
    a("")
    a("## Abandon set (Won't Do candidates)")
    a("")
    a(f"{aband_n} tickets. Apply with: `scripts/jira-todo-backlog-triage.py apply --decisions <file> --execute`.")
    a("")
    a("<details><summary>full list</summary>")
    a("")
    a("| key | created | updated | type | summary |")
    a("|---|---|---|---|---|")
    for r in rows:
        if r["classification"] != "abandon":
            continue
        a(f"| {r['key']} | {r['created']} | {r['updated']} | {r['issuetype']} | {_md_cell(r['summary'])} |")
    a("")
    a("</details>")
    a("")
    a("## Duplicate clusters (human confirm before closing)")
    a("")
    a("| key | duplicate_of | created | summary |")
    a("|---|---|---|---|")
    for r in rows:
        if r["classification"] != "duplicate":
            continue
        a(f"| {r['key']} | {r['duplicate_of']} | {r['created']} | {_md_cell(r['summary'])} |")
    a("")
    a("## Keep / refine set")
    a("")
    a(f"{keep_n} tickets retained. Each still needs proper `area:` / `tier:` / `class:` labels before a "
      "runner can pick it up — that is the *refinement* work this triage hands back to sprint planning.")
    a("")
    a("<details><summary>full list (key · signals)</summary>")
    a("")
    for r in rows:
        if r["classification"] != "keep":
            continue
        a(f"- {r['key']} — {_md_cell(r['summary'])} — _signals_: {'; '.join(r['signals'])}")
    a("")
    a("</details>")
    a("")
    return "\n".join(out) + "\n"


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()


# ── Apply mode ────────────────────────────────────────────────────────────────

def _find_transition(site: str, auth: str, key: str, names: Iterable[str]) -> dict[str, Any] | None:
    want = {n.lower() for n in names}
    resp = _get(site + f"/rest/api/3/issue/{key}/transitions", auth)
    for t in resp.get("transitions", []):
        if t.get("name", "").lower() in want or t.get("to", {}).get("name", "").lower() in want:
            return t
    return None


def cmd_apply(args: argparse.Namespace) -> int:
    site, project, auth = _jira_config()
    rows = [json.loads(l) for l in Path(args.decisions).read_text().splitlines() if l.strip()]
    wanted = {"abandon"} | ({"duplicate"} if args.allow_duplicate_apply else set())
    targets = [r for r in rows if r.get("classification") in wanted]
    if args.only:
        targets = [r for r in targets if r["classification"] == args.only]
    if args.limit:
        targets = targets[: args.limit]

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[{mode}] {len(targets)} ticket(s) to action "
          f"(classifications: {sorted(wanted)}; limit={args.limit or 'none'})", file=sys.stderr)

    done = skipped = failed = 0
    for r in targets:
        key = r["key"]
        cls = r["classification"]
        reason = r.get("comment") or _wont_do_reason(cls, r.get("verdict", cls), r.get("signals", []),
                                                     r.get("duplicate_of"), args.abandon_age_days_for_msg,
                                                     args.fresh_days_for_msg)
        # Skip if already terminal.
        try:
            cur = _get(site + f"/rest/api/3/issue/{key}?fields=status", auth)
            cur_status = (cur.get("fields", {}).get("status") or {}).get("name", "")
        except RuntimeError as exc:
            print(f"  {key}: SKIP (cannot read status: {exc})", file=sys.stderr)
            skipped += 1
            continue
        if cur_status not in TODO_STATUSES and cur_status not in STARTED_STATUSES:
            print(f"  {key}: SKIP (already in terminal status {cur_status!r})", file=sys.stderr)
            skipped += 1
            continue

        trans = None
        if not args.execute:
            print(f"  {key}: would comment + Won't Do transition [{cls}] — {reason[:80]}...")
            done += 1
            continue

        # 1. comment
        try:
            _request("POST", site + f"/rest/api/3/issue/{key}/comment", auth, {"body": _adf(reason)})
        except RuntimeError as exc:
            print(f"  {key}: FAIL posting comment: {exc}", file=sys.stderr)
            failed += 1
            continue
        # 2. transition
        trans = _find_transition(site, auth, key, ["Won't Do", "Force Close", "Archived"])
        if not trans:
            print(f"  {key}: FAIL (no Won't Do / Archived transition available)", file=sys.stderr)
            failed += 1
            continue
        body: dict[str, Any] = {"transition": {"id": trans["id"]}}
        if args.wont_do_resolution_id:
            body_with_res = dict(body)
            body_with_res["fields"] = {"resolution": {"id": args.wont_do_resolution_id}}
            try:
                _request("POST", site + f"/rest/api/3/issue/{key}/transitions", auth, body_with_res)
            except RuntimeError as exc:
                if "resolution" in str(exc).lower() or "400" in str(exc):
                    # transition screen has no resolution field — retry without it
                    try:
                        _request("POST", site + f"/rest/api/3/issue/{key}/transitions", auth, body)
                    except RuntimeError as exc2:
                        print(f"  {key}: FAIL transition: {exc2}", file=sys.stderr)
                        failed += 1
                        continue
                else:
                    print(f"  {key}: FAIL transition: {exc}", file=sys.stderr)
                    failed += 1
                    continue
        else:
            try:
                _request("POST", site + f"/rest/api/3/issue/{key}/transitions", auth, body)
            except RuntimeError as exc:
                print(f"  {key}: FAIL transition: {exc}", file=sys.stderr)
                failed += 1
                continue
        print(f"  {key}: DONE [{cls}] → {trans['to']['name']}")
        done += 1
        if args.sleep:
            time.sleep(args.sleep)

    print(f"\n[{mode}] done={done} skipped={skipped} failed={failed}", file=sys.stderr)
    return 1 if failed else 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    t = sub.add_parser("triage", help="read-only: classify + write report and decisions JSONL")
    t.add_argument("--labels", default=",".join(LEGACY_LABELS),
                   help=f"comma-separated legacy labels (default: {','.join(LEGACY_LABELS)})")
    t.add_argument("--abandon-age-days", type=int, default=DEFAULT_ABANDON_AGE_DAYS,
                   help=f"older than this many days is eligible for abandon (default {DEFAULT_ABANDON_AGE_DAYS})")
    t.add_argument("--fresh-days", type=int, default=DEFAULT_FRESH_DAYS,
                   help=f"updated within this many days counts as a keep signal (default {DEFAULT_FRESH_DAYS})")
    t.add_argument("--report", help="write Markdown report to this path (default: stdout)")
    t.add_argument("--decisions", help="write machine-readable JSONL decisions to this path")
    t.set_defaults(func=cmd_triage)

    ap = sub.add_parser("apply", help="mutating: comment + Won't Do transition for the abandon set")
    ap.add_argument("--decisions", required=True, help="JSONL decisions file from `triage`")
    ap.add_argument("--execute", action="store_true", help="actually mutate JIRA (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="only action the first N targets (batching; 0 = all)")
    ap.add_argument("--only", choices=["abandon", "duplicate"], help="restrict to one classification")
    ap.add_argument("--allow-duplicate-apply", action="store_true",
                    help="also action 'duplicate' rows (default: abandon only — duplicates need human confirm)")
    ap.add_argument("--wont-do-resolution-id", default=DEFAULT_WONT_DO_RESOLUTION_ID,
                    help=f"resolution id to attempt on the transition (default {DEFAULT_WONT_DO_RESOLUTION_ID} "
                         "= 対応しない; pass empty string to skip)")
    ap.add_argument("--sleep", type=float, default=0.2, help="seconds to sleep between mutations (rate-limit)")
    # only used as a fallback when a decisions row predates the embedded comment field
    ap.add_argument("--abandon-age-days-for-msg", type=int, default=DEFAULT_ABANDON_AGE_DAYS,
                    help=argparse.SUPPRESS)
    ap.add_argument("--fresh-days-for-msg", type=int, default=DEFAULT_FRESH_DAYS, help=argparse.SUPPRESS)
    ap.set_defaults(func=cmd_apply)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
