# Release Notes From JIRA Milestone

`scripts/release_notes_from_milestone.py` generates operator handoff notes
from a JIRA `fixVersion` and writes them under `release-notes/`.

## Generate Notes

Run from the repository root:

```bash
scripts/release_notes_from_milestone.py --version vX.Y.Z
```

For the release train, `vX.Y.Z` is reserved before promote. That
planning reservation is enough for release notes and changelog review:
the tools read JIRA `fixVersion=vX.Y.Z` and do not require a `vX.Y.Z`
image tag or git tag to exist.

The script:

- verifies that the JIRA fixVersion exists;
- queries tickets assigned to that fixVersion;
- extracts each ticket's Acceptance criteria section and lesson references;
- groups included tickets by sprint label `A` through `E`, then by priority;
- writes `release-notes/vX.Y.Z.md`;
- checks out or creates `release/vX.Y.Z` and commits the generated file.

Use `--no-commit` for dry generation during operator review:

```bash
scripts/release_notes_from_milestone.py --version vX.Y.Z --no-commit
```

## Ticket Shape

Each ticket description must contain:

```markdown
## Acceptance criteria

- First verified release note item.
- Second verified release note item.
```

Lesson references may be written as `L-OP-745-example.md` or
`docs/sop/lessons/L-OP-745-example.md`.

Sprint grouping is read from labels such as `sprint:A`, `sprint-A`, or `A`.
Unknown sprint labels are grouped under `Sprint Unsorted`.

## Error Catalog

- `JIRAFixVersionNotFound`: the requested fixVersion does not exist. The
  script refuses to generate notes and prints nearby version suggestions.
- `TicketDescriptionMalformed`: the ticket is skipped and listed in the
  generated `Skipped tickets` section.
- `LessonReferenceBroken`: the lesson entry is replaced with
  `<lesson-link-missing>` and the broken reference is listed below the ticket.

## Recovery

The generator is idempotent for the same milestone input. Re-run the same
command after fixing ticket descriptions or lesson files. If the release branch
already exists, the script checks it out and commits only the regenerated notes
when the file content changed.

Rollback is a normal git rollback on the `release/vX.Y.Z` branch:

```bash
git checkout release/vX.Y.Z
git revert HEAD
```

If JIRA is unavailable, rerun once the runner JIRA credentials are restored. Do
not hand-edit generated release notes unless the operator intentionally wants a
manual patch on top of the generated commit.
