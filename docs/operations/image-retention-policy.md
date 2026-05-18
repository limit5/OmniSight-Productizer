# Image Retention Policy

OP-1480 replaces the old "tagged forever, untagged after 30 days" GHCR
cleanup rule with per-tag-class retention. The weekly retention job in
`.github/workflows/build-images.yml` runs `scripts/enforce_image_retention.py`
for each package and uploads a per-package decision table artifact.

## Packages

The scheduled workflow applies the same policy to:

- `omnisight-backend`
- `omnisight-frontend`
- `omnisight-bridge`

## Tag Classes

| Tag class | Example | Retention |
|---|---:|---|
| Immutable release | `v1.2.3` | Kept forever |
| Hotfix release line | `v1.2.3-hotfix-1` | Kept forever |
| Release candidate | `v1.2.3-rc1` | Kept until 90 days after `v1.2.3` ships; if the parent has not shipped, kept 30 days |
| Develop build | `develop-abcdef123456` | Kept for 14 days or newest 30 develop tags, whichever keeps more |
| Feature build | `feature-abcdef123456` | Kept 7 days |
| Untagged orphan | no tags | Kept 7 days |
| Mutable alias | `canary`, `staging`, `prod`, `latest`, `develop-latest` | Never selected for deletion as a standalone retention target |
| Unknown tagged version | any unmatched tag | Kept for manual review |

Unknown tagged versions are intentionally conservative. GHCR deletion is
irreversible, and the current registry still contains legacy tags that do not
fit the V2 `develop-<12sha>` convention.

## Operator Commands

Dry-run the backend package and print the decision table:

```bash
GH_TOKEN=... python3 scripts/enforce_image_retention.py \
  --dry-run \
  --package omnisight-backend
```

Write the same table to a file:

```bash
GH_TOKEN=... python3 scripts/enforce_image_retention.py \
  --dry-run \
  --package omnisight-backend \
  --summary-file artifacts/retention/omnisight-backend.txt
```

Run live cleanup by omitting `--dry-run`. The script prints the same table
before deleting candidate versions.

## Decision Table

The script emits tab-separated rows:

```text
version_id  updated_at  tag_class  action  tags  reason
```

`action=delete` means the version is selected for deletion in live mode.
`action=keep` means the version remains in GHCR and the reason column states
which retention rule protected it.
