# Scripts

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
