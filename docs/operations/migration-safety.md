# Migration safety

OP-765 adds a patchset gate for Alembic migrations. The existing DB
engine matrix still validates the whole committed chain; this gate
focuses on the migrations introduced by a single pull request.

## CI gate

The `migration_compat_check` job in
`.github/workflows/migration-compat.yml` runs when a PR changes
`backend/alembic/versions/*.py`, the Alembic template, or the checker
itself.

For each changed migration, `scripts/check_migration_compat.py`:

1. reads `revision` and `down_revision` from the migration file,
2. upgrades a fresh database to the parent revision,
3. records a table/column fingerprint,
4. upgrades the changed revision,
5. runs `alembic downgrade -1`, and
6. fails if the fingerprint differs from the parent schema.

The same script rejects static old-code/new-schema hazards such as
column renames, column drops, table drops, type changes, and
`NOT NULL` column additions without a default. When
`MIGRATION_COMPAT_OLD_IMAGE` is configured, CI also runs the previous
release image's smoke command against the newly migrated database.

## Verdicts

Failures emit a GitHub Actions error and a JSON report with verdict
`Verified -1`. The old-code/new-schema path uses the reason
`old code + new schema regression` so merge automation can surface the
schema-break verdict directly.

A clean run emits verdict `Verified +1`.

## Override

The emergency override requires all of the following:

- ticket label `migration:break-allowed`
- `MIGRATION_COMPAT_APPROVED_BY=sora`
- a writable `MIGRATION_COMPAT_AUDIT_LOG` path

When those are present, the checker appends a JSONL audit row containing
the ticket, label set, approver, and changed migrations, then exits
successfully. Missing approval with the label present is a hard failure.

## Migration template

New Alembic migrations generated from `backend/alembic/script.py.mako`
include:

```text
Backwards-compat: <safe|requires-coordinated-deploy>
```

Authors must replace the placeholder before review. `safe` is for
additive migrations that old binaries can tolerate. Use
`requires-coordinated-deploy` when the migration needs an ordered
deploy, a feature flag, or the override process above.
