# ADR 0002 — GitLab self-hosted primary, GitHub one-way mirror, Gerrit review layer

**Status**: Accepted (2026-05-04)

**Context**

Until 2026-05-04 OmniSight used `github.com/limit5/OmniSight-Productizer` as the single source of truth for code, issues, CI (GitHub Actions), and releases. Three pressures forced re-evaluation:

1. **GitHub Actions free-tier limits** for `limit5` org. Private-repo CI minutes are capped; public repos work but expose code. Build pipeline complexity (release-signer + DLP gate + backup pipeline + multi-track tests) was outgrowing the budget.
2. **Self-host requirement for prod data**. The deployment pipeline embeds backup encryption keys, GPG release signers, and customer KMS test material — none of these belong in a third-party CI runner.
3. **Multi-AI governance** (per [ADR 0001](0001-five-branch-gitflow.md) + [ADR 0003](0003-gerrit-code-review.md)) needs Gerrit-style fine-grained review scoring, which GitHub PR approvals don't natively support.

GitHub itself stays valuable for:
- OSS visibility (recruiting, public artefacts)
- Future open-source library spin-offs
- Backup of code history (insurance against self-host failure)

**Decision**

Three-remote architecture:

```
GitLab self-hosted (sora.services:49154 → Phase 2 https://git.sora.services)
  ↑ source of truth: repo + issues + CI/CD + container registry
  ↓ auto push mirror
GitHub public (limit5/OmniSight-Productizer)
  one-way mirror, full history (all branches + tags)
  not gating; for visibility / OSS spin-off / backup
  
Gerrit self-hosted (Phase 3, separate instance)
  pulls main + feature/* from GitLab; refs/for/develop push for review
```

**Per-component authority**

| Component | Primary | Notes |
|---|---|---|
| Code repo | GitLab | source of truth |
| Issues | JIRA Cloud (Atlassian) | not GitLab Issues — see [ADR 0004](0004-per-agent-jira-identity.md) |
| CI/CD | GitLab CI | not GitHub Actions — single runner pool, self-hosted |
| Container registry | GitLab Container Registry | mirrored to GitHub Container Registry only for public artefacts |
| Code review | Gerrit | not GitLab MR — see [ADR 0003](0003-gerrit-code-review.md) |
| Releases | GitLab releases (canonical) + GitHub releases (mirror) | `gh release` → `glab release` migration in Phase 2 |
| Project board | JIRA | not GitLab Boards |

**CI policy**

GitLab CI is the **only** gating layer. GitHub Actions:
- Continue to run *announce* workflows (post release notes, update README badges)
- **Do not** gate any merge / deploy
- **Do not** run tests / lint / build (avoid double-spend + behavioural drift)

**Mirror direction**

- GitLab → GitHub: **automatic, one-way**. GitLab repo settings → "Mirroring repositories" → push to GitHub via PAT. Schedule: every push.
- GitHub → GitLab: **never**. Any PR opened on GitHub mirror gets auto-closed with link to JIRA + GitLab equivalent (Phase 2 webhook).

**Consequences**

Positive:
- Self-host control over CI minutes, secret material, deploy artefacts
- Fits Gerrit review topology (Gerrit pulls from GitLab, not GitHub)
- Consolidates billing (one infra pool vs GitHub Actions + Atlassian + Cloudflare)
- GitHub stays free for OSS visibility / lib spin-off

Negative:
- Self-host operational burden (GitLab Omnibus image ~4GB RAM; PostgreSQL + Redis + nginx co-located)
- Mirror push can drift (one-way is simpler than bi-directional, but still requires monitoring)
- New contributor onboarding: must explain "GitHub is mirror, real work happens in GitLab"
- Phase 2 `external_url` BLOCKER (see Track B Phase 0 baseline notes — currently API returns docker container hostname, must fix before primary mirror flip)

Neutral:
- Existing GitHub PRs / issues / wikis migrate or archive (Phase 2 task; some legacy refs stay as historical)
- `git remote -v` patterns change for all contributors

**Phase-by-phase rollout**

- **Phase 1**: Branch rename only. CI still on GitHub Actions. GitLab not yet primary.
- **Phase 2**: GitLab becomes primary. CI fully on GitLab. GitHub Actions stripped to announce-only. Mirror push set up.
- **Phase 3**: Gerrit added. `refs/for/develop` becomes the review entry point.

**Related**

- [ADR 0001 — Five-branch Git Flow](0001-five-branch-gitflow.md) — defines the branches that live on these remotes
- [ADR 0003 — Gerrit code review](0003-gerrit-code-review.md) — defines how Gerrit pulls from GitLab
- [Phase 1 migration SOP](../sop/migration-plan-2026-05.md) — Phase 1 / 2 / 3 operational steps
- `reference_gitlab_self_hosted.md` (memory) — Track B baseline including `external_url` BLOCKER
