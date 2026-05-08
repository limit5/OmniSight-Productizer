# Generated-File Auto-Resolve Runbook

## Purpose

R3 auto-rebase and the H1 submit queue can automatically resolve conflicts on
registered generated files. The first registered file is
`docs/sop/lessons-learned.md`, regenerated from per-lesson files by
`scripts/build_lessons_index.py`.

## Registry

Generated files are registered in `.gerrit/auto-resolve.yaml`:

```yaml
auto_resolved_files:
  - path: docs/sop/lessons-learned.md
    resolver: scripts/build_lessons_index.py
    description: Index regenerated from docs/sop/lessons/ frontmatter
```

Each entry must use a repo-relative `path` and `resolver`. If a duplicate path
appears, the first entry wins and the daemon emits an
`auto_resolve_duplicate_path` warning.

## Runtime Flow

1. R3 or the submit queue tries Gerrit's REST rebase endpoint.
2. If Gerrit returns HTTP 409, the daemon parses the conflicting file list.
3. If every conflicting file is registered, the daemon fetches the patch set
   into a temporary clone, runs `git rebase <develop-tip>`, checks out the
   target side for each registered file, runs its resolver, stages the file,
   continues the rebase, and pushes the result as the next patch set.
4. The daemon rejects the auto-resolution if the resolver exits non-zero,
   leaves conflict markers behind, or modifies files outside the registered
   conflict set.
5. On success, the daemon posts a JIRA comment starting with `[auto-resolve]`
   and emits an `auto_resolve_audit` log record containing `change_id`, `ps`,
   `file`, `resolver`, and `resolved_at`.

## Operator Response

If auto-resolution fails, treat the change as an ordinary rebase conflict:
resolve the conflict manually, rerun the relevant generator, and re-mark the
change for review or submit. The failure comment includes the resolver error so
the operator can distinguish malformed lesson frontmatter from a real merge
conflict.

Before adding a new registry entry, add a synthetic test that proves the
resolver leaves no conflict markers and does not touch unrelated files.
