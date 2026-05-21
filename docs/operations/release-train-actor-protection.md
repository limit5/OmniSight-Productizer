# Release-train final-tag / promote actor protection (RT-19 / ADR-0040)

**Status**: Accepted 2026-05-22. Operator security policy for the single-trunk Release Train (ADR-0040, image-tag-only per RT-20). Re-scoped from the original "protect git v* tags" framing — under image-tag-only there are **no git release tags**, so the controls below protect the **GitLab Container Registry final image tag + the promote job + the release_train row**.

## Release authority (who may release)

The **release-manager** role = **{ `sora` (operator), `release-train` service account }** ONLY.
- `sora` — manual cut / promote / break-glass.
- `release-train` service account — the dedicated automation identity that runs auto-cut + promote (to be created; until it exists, operator runs promote manually).
- **AI runners (`claude-bot`, `codex-bot`) MUST NOT** create a final `vX.Y.Z` image tag, run the promote job, or write a `release_train` promotion row. They build candidate `sha-<fullsha>` images and push code changes — nothing in the release-authority set.

## Controls

| # | Control | Status | Where enforced |
|---|---|---|---|
| 1 | Gerrit `deny createTag = group ai-reviewer-bots` (no AI bot can create any git tag) | ✅ already live (`.gerrit/project.config:140`) | Gerrit ACL |
| 2 | GitLab protected `v*` **git** tags → create restricted to maintainers (defense-in-depth: even though we don't cut git release tags, block stray ones) | ⬜ TODO (GitLab project 26 protected_tags = none) | GitLab project settings |
| 3 | Final `vX.Y.Z` **image** tag push to GitLab CR → release-manager only | ⬜ TODO | GitLab CR / promote-job permission |
| 4 | Promote job (the digest-retag pipeline) → runnable only by release-manager | ⬜ TODO | GitLab CI/CD protected pipeline + RT-12 promote impl |
| 5 | `release_train` promotion-row write → release-manager actor only; every final tag/promote writes an **immutable audit row** (hard-gate) | ⬜ TODO | RT-10a (table) + RT-12 (promote hard-gate) |

## Enforcement coupling
Controls #3–#5 are implemented WITH **RT-12** (promote job) + **RT-10a** (release_train table audit hard-gate) — they cannot be enforced independently of the promote mechanism. Control #2 is a standalone GitLab project-settings change (low-risk defense-in-depth) the operator can apply any time. Control #1 is already live.

## Break-glass
Emergency override of any control = audited human action only (RT-22): explicit operator command + exact SHA + immutable audit reason. Never an unattended bypass; never moves a final tag without a release_train row.

## Relations
- ADR-0040 §"Decisions LOCKED" (RT-20 image-tag-only) — why git-tag protection is re-scoped to image-tag/promote/release_train.
- RT-10a (release_train table + audit hard-gate), RT-12 (promote job), RT-22 (break-glass) — where controls #3–#5 land.
