# Scripts

## topo-submit-order.py

Suggests a safe Gerrit submit order for batched JIRA approvals:

```bash
python3 scripts/topo-submit-order.py
python3 scripts/topo-submit-order.py 'status = "Approved" AND assignee in (codex-bot, claude-bot)'
python3 scripts/topo-submit-order.py --fixture synthetic-3
```

The default JQL is:

```text
status = "Under Review" AND assignee in (codex-bot, claude-bot)
```

The script reads matching OP tickets from JIRA, extracts the runner-posted
Gerrit change URL, fetches each current patchset diff, builds a file-overlap
graph, and prints an ordered submit list with rationale. Pairs that touch the
same file and overlapping changed line ranges are flagged as:

```text
MANUAL REBASE REQUIRED
```

By default the script is idempotent and read-only. `--apply` creates a
temporary local branch, cherry-picks the ordered patchset commits, and reports
the first conflict point.

## `jira-todo-backlog-triage.py`

Triages the `migrated-from-todo` backlog dump (OP-1014) — the ~530 open OP
tickets carrying `runner-needs-refinement`, `migrated-from-todo-bulk` or
`migrated-from-todo` that were bulk-imported from `TODO.md` without
refinement and are therefore invisible to the runner JQL but visible in every
project-wide query.

Read-only triage → Markdown report + JSONL decision file:

```bash
python3 scripts/jira-todo-backlog-triage.py triage \
    --report docs/audit/$(date +%F)-todo-backlog-triage.md \
    --decisions /tmp/op-1014-decisions.jsonl
```

Each open legacy-label ticket is classified `keep` (has a live signal:
started / assigned / has fixVersion / non-bot comment / recently updated /
younger than `--abandon-age-days`), `abandon` (old + cold + never started →
Won't Do / Archived) or `duplicate` (shares a normalised summary with an
older sibling — flagged for human confirmation, never auto-closed unless
`--allow-duplicate-apply`). Tie-break: `keep` wins.

The bulk action defaults to dry-run; pass `--execute` to comment + run the
Won't Do transition on an operator-reviewed decision file, optionally batched:

```bash
python3 scripts/jira-todo-backlog-triage.py apply --decisions /tmp/op-1014-decisions.jsonl   # dry-run
python3 scripts/jira-todo-backlog-triage.py apply --decisions /tmp/op-1014-decisions.jsonl --execute --limit 50
```

See anti-pattern #13 in `docs/sop/architecture-anti-patterns.md` and lesson
`L-OP-1014` for why this inventory exists and how to avoid recreating it.

## `ship-pending.py`

Manual Gerrit shipper for operator or interactive-Claude commits made
directly in the main repository, outside the JIRA runner worktree flow.

Typical dry-run from the main repository:

```bash
python3 scripts/ship-pending.py --dry-run
```

Typical real run:

```bash
python3 scripts/ship-pending.py
```

The script scans `git log gerrit/develop..HEAD`, prints each pending
commit with its Change-Id status, skips Change-Ids already found in
Gerrit, and asks before shipping each remaining commit. Confirmed
commits are cherry-picked into a fresh branch in the Codex worktree from
the Gerrit `develop` tip, pushed to `refs/for/develop` with the selected
bot SSH key, and reported back as Gerrit Change URLs.

Useful options:

- `--source-repo PATH`: repository to scan for pending commits.
- `--worktree PATH`: Codex or Claude worktree used for the fresh ship
  branch.
- `--agent-class subscription-codex|subscription-claude|api-openai|api-anthropic`:
  bot identity and SSH key used for the push.
- `--yes`: select all non-idempotent commits without prompting.
- `--auto-cross-review`: after push, ask the other bot to post
  `Code-Review +1` on each new change.

End-of-session use is intentionally manual: when `/loop` ends after
operator or interactive-Claude commits landed directly on main, run the
dry-run first, then the real command if the listed commits are the ones
to ship. The script does not auto-push on its own.
