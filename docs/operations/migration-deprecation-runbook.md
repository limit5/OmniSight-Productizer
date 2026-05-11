# Migration Deprecation Runbook — OP-865

**Audience**: backend engineers shipping schema changes that drop or
rename columns.

**Gate**: `scripts/check_migration_compat.py` (CI: `migration-check`
workflow). Blocks any patchset that tries to drop a live column in a
single revision.

## TL;DR — "I need to drop a column"

You **cannot** drop a column in a single migration. Old replicas still
running the previous release would crash the moment they SELECT it.
The gate forces a two-revision deprecation window:

1. **Revision N (deprecation-window-1of2)** — rename the column to
   `<old_name>_deprecated` via `op.alter_column(...,
   new_column_name="<old_name>_deprecated")`. Old code can still write
   to / read from the renamed column through a view or a temporary
   compat shim.
2. **Revision N+1 (deprecation-window-2of2)** — `op.drop_column(table,
   "<old_name>_deprecated")`. CI verifies that the `down_revision` of
   this migration is tagged `deprecation-window-1of2` and that the rename
   in the parent points at the exact `<table>.<column>_deprecated`
   target.

Both revisions must declare the `backwards-compat:` tag in their
docstring. The gate hard-fails otherwise.

---

## Step-by-step

### 1. First revision — rename to `_deprecated`

```bash
cd backend
alembic revision -m "deprecate users.legacy_email column"
```

Open the generated file under `backend/alembic/versions/` and replace
the docstring placeholder:

```python
"""deprecate users.legacy_email column

Revision ID: 0207
Revises: 0206
Create Date: 2026-05-12
backwards-compat: deprecation-window-1of2

"""
from alembic import op


revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "legacy_email",
        new_column_name="legacy_email_deprecated",
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "legacy_email_deprecated",
        new_column_name="legacy_email",
    )
```

Land this revision in its own PR. Wait until at least one production
release has rolled past it — all replicas now run code that no longer
references `legacy_email`.

### 2. Second revision — drop the `_deprecated` shadow

After the deprecation window has elapsed (one release minimum, longer
for heavy-traffic tables), create the second revision:

```bash
alembic revision -m "drop users.legacy_email_deprecated"
```

```python
"""drop users.legacy_email_deprecated

Revision ID: 0208
Revises: 0207
Create Date: 2026-05-20
backwards-compat: deprecation-window-2of2

"""
from alembic import op


revision = "0208"
down_revision = "0207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("users", "legacy_email_deprecated")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("legacy_email_deprecated", sa.Text(), nullable=True),
    )
```

The gate verifies:

* docstring tag is `deprecation-window-2of2`;
* `down_revision` points to a migration tagged `deprecation-window-1of2`;
* the dropped column name ends in `_deprecated`;
* the parent migration actually renames a column to that exact target.

If any of those fail, CI rejects the patchset with
`MigrationBreakingChangeRefused` and a pointer back to this runbook.

---

## Renaming a column

**Use `op.alter_column(table, "old", new_column_name="new")`** — the
gate rejects drop-then-add (`op.drop_column` + `op.add_column` against
the same table in one revision) because it leaves the column gone for
a moment of the migration.

Renaming is a breaking change for any deployed code that referenced the
old name, so the migration must be tagged `breaking` and the commit
must carry a `migration:approved-breaking` trailer:

```
Add user display_name rename

Renames users.name -> users.display_name. Aligns with the new profile
schema in OP-XXXX. Old code is gone after release R45 so we can ship
in one revision.

migration:approved-breaking
```

If you'd rather avoid the breaking-trailer route, do the rename through
the same two-revision deprecation window:

* window-1of2: `op.alter_column(..., new_column_name="name_deprecated")`,
* window-2of2: `op.add_column("users", ..., "display_name")` in one
  revision (tag: `safe`) and then a separate window-2of2 drop of
  `name_deprecated`.

---

## NOT NULL columns

Adding a `NOT NULL` column **requires a server-side default**. The gate
rejects any `op.add_column(..., nullable=False)` without
`server_default=...` (or a raw `ALTER TABLE ADD COLUMN ... NOT NULL`
without `DEFAULT`).

If you genuinely cannot supply a default (e.g. the value is per-row and
computed by the application), split the change:

1. Add the column as `NULLable` with a `safe` migration;
2. Backfill via the application or a separate batch job;
3. In a follow-up `breaking` migration with the
   `migration:approved-breaking` trailer, promote to `NOT NULL`.

---

## Enums

Adding an enum value is only safe **at the tail** of the existing list.
The gate rejects any `ALTER TYPE ... ADD VALUE 'X' BEFORE 'Y'` or
`AFTER 'Y'` statement — Postgres cannot wrap positional ADD VALUE in a
transaction with other DDL, and the ordering rewrite races with read
paths during deploy.

If you need a non-tail insertion, treat it as a `breaking` migration
with `migration:approved-breaking`, and schedule it during a downtime
window.

---

## Override path (last resort)

The gate has two override modes:

| Override | Purpose | How |
| --- | --- | --- |
| `migration:approved-breaking` commit trailer | Per-commit; only bypasses the `breaking` tag check (other AST checks still apply). | Add the trailer to the commit message. |
| `migration:break-allowed` label + `--approved-by sora` | OP-765 nuclear option; bypasses every gate. JSONL audit row appended. | Tag the JIRA ticket with `migration:break-allowed`, set `MIGRATION_COMPAT_APPROVED_BY=sora` in CI. |

Both leave a paper trail. Use the trailer route by default; the label
override is reserved for incidents where the gate itself is wrong.

---

## Error catalog (what the gate emits)

* `MigrationMetadataMissing` — no `backwards-compat:` tag in docstring,
  or value is unknown / still the angle-bracket placeholder.
* `MigrationBreakingChangeRefused` — drop_column outside a
  deprecation-window-2of2 chain, drop+add rename, or `breaking` tag
  without the approved-breaking trailer.
* `MigrationEnumNonTail` — `ALTER TYPE ... ADD VALUE` with `BEFORE` or
  `AFTER` positioning.

The CI report (`artifacts/migration-check-report.json`) lists every
failed check with the migration path, the offending pattern, and a
short reason. Use that as the starting point when the gate rejects
your patch.
