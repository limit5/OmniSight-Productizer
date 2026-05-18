# Release Branch Cut Runbook

> Ticket: OP-880 (D8 - tag + release branch automation)
>
> Audience: release operator cutting a SemVer release branch and tag.

> **2026-05-18 cutover (OP-1474 / META OP-1468):** the second push target
> in §1 and §2 is now **GitLab** (`git@gitlab.com:omnisight/...`, sora
> SSH identity), not GitHub. The legacy GitHub remote is now a read-only
> mirror — see [ADR-0038](../adr/ADR-0038-image-pipeline-on-gitlab.md).

This runbook covers the mechanical release-cut step before the production
approval workflow takes over. It is intentionally separate from
`docs/operations/release-runbook.md`, which covers deploy approval,
rollback, and audit handling after a tag exists.

## 1. Pre-flight

Verify the release is ready:

```bash
git checkout main
git pull --ff-only
scripts/milestone_check.py --version vX.Y.Z
```

Confirm both remotes exist and that the GitLab origin is wired to the
sora SSH identity (post-OP-1474 cutover):

```bash
git remote get-url gerrit
git remote get-url origin       # must be git@gitlab.com:omnisight/<repo>.git
ssh -i ~/.ssh/id_ed25519_sora -T git@gitlab.com   # expect: "Welcome to GitLab, @sora-release"
```

The script pushes the release branch and tag directly to both remotes,
so the sora operator identity must have branch/tag creation permission in
Gerrit and in the GitLab `omnisight` group. The push uses
`GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_sora -F /dev/null'` so the
sora key is selected even when the operator's default `~/.ssh/config`
prefers a personal key.

## 2. Cut the Branch and Tag

Run a dry-run first:

```bash
scripts/cut_release_branch.sh --version vX.Y.Z --dry-run
```

Run the real cut:

```bash
scripts/cut_release_branch.sh --version vX.Y.Z --changelog-jira
```

The script creates `release/vX.Y.Z` from `main`, updates `CHANGELOG.md`,
asks for a manual tag approval, creates annotated tag `vX.Y.Z`, and
pushes branch plus tag to Gerrit and GitLab (over the sora SSH identity;
`origin` is now the GitLab remote — GitHub is a read-only mirror as of
OP-1474).

For automation where a separate approval step already captured the
operator decision, pass:

```bash
scripts/cut_release_branch.sh --version vX.Y.Z --changelog-jira --approve-tag
```

## 3. Changelog Fallback

`scripts/auto_changelog.py` tries to render tickets from the JIRA
fixVersion when `--changelog-jira` is supplied. D16 owns the richer
milestone export; until it ships, lookup failures degrade to a manual
template and the script prints `ChangelogGenFailed` in its JSON output.

When the template path is used, edit the generated section before the
release is promoted.

## 4. Duplicate Recovery

The script refuses existing release branches and tags by default:

| Error | Meaning | Recovery |
|---|---|---|
| `ReleaseBranchExists` | `release/vX.Y.Z` exists locally, in Gerrit, or in GitLab | Inspect the existing ref. If replacing it is intentional, rerun with `--override-existing`. |
| `TagAlreadyExists` | `vX.Y.Z` exists locally, in Gerrit, or in GitLab | Inspect the tag target. If replacing it is intentional, rerun with `--override-existing`. |
| `ChangelogGenFailed` | JIRA milestone export failed | The script inserts the manual template; fill it before promotion. |

Do not force-push over a release branch or tag without a human decision
recorded in the release ticket.

## 5. Rollback

If the script fails before any push, delete local scratch refs if needed:

```bash
git checkout main
git branch -D release/vX.Y.Z
git tag -d vX.Y.Z
```

If either remote push succeeded, do not delete remote refs silently.
Comment on the release ticket with the pushed refs and ask the operator
whether to keep them, replace them with `--override-existing`, or cut a
new patch/release-candidate version.

## 6. Verification

After a successful cut:

```bash
git ls-remote gerrit refs/heads/release/vX.Y.Z refs/tags/vX.Y.Z
git ls-remote origin refs/heads/release/vX.Y.Z refs/tags/vX.Y.Z
git show --stat vX.Y.Z
```

The expected result is one branch ref and one tag ref on each remote,
with the tag pointing at the release branch tip.

## 7. R5 — Staging tip-match audit (continuous-staging model)

> **Redefined by OP-975 / AUDIT-22 (2026-05-12).** The pre-AUDIT-19c R5
> contract was "deploy vX.Y.Z to staging (blue-green) before promoting
> main" — a per-release *deploy* action. Continuous staging (AUDIT-19c,
> OP-973) makes R5 an **audit step, not a deploy step**: staging always
> tracks the `develop` tip, so R5 only confirms that the tip the release
> is being cut from is the tip staging has already converged on *and*
> gated green. This section is the R5 implementation reference the
> release-conductor runbook points the R5 child at; R1 already lives in
> this file (§1–§6), so R5 lands here too.

**Applies to**: v0.5.1+ RELEASE chains. v0.5.0-rc1 R5 was operator-handled
via a synthetic-JSONL bypass (the AUDIT-17 staging gate gap) — see the
`RELEASE-v0.5.0-rc1` META retrospective note.

**Pre-condition**: AUDIT-19c (OP-973) complete — the develop→staging sync
orchestrator and its systemd units are installed and running on the
staging host. The `release/vX.Y.Z` branch has been cut (§2) from a known
`develop` tip commit; call it `$DEVELOP_TIP_SHA`.

**Action 1 — `milestone_ready` emitted for the tip:**

```bash
journalctl --user -u release-milestone-checker.service --no-pager \
  | grep -F "$DEVELOP_TIP_SHA" | grep -F milestone_ready | tail -1
# or, if the unit logs to a file: release_milestone_checker.systemd.log
```

Expect ≥1 `milestone_ready` event whose develop tip equals
`$DEVELOP_TIP_SHA`. A `milestone_blocked` line for that tip with no later
`milestone_ready` is a **FAIL** → "On fail".

**Action 2 — `release_audit` has a recent `staging_synced` row for the tip:**

```bash
set -a; . /home/user/.config/omnisight/release-audit.env; set +a
PSQL_URL="${OMNISIGHT_DATABASE_URL/+asyncpg/}"
psql "$PSQL_URL" -tAc \
  "SELECT count(*) FROM release_audit
     WHERE outcome = 'staging_synced'
       AND detail LIKE '%'||'$DEVELOP_TIP_SHA'||'%'
       AND ts > now() - interval '4 hours'"
```

Expect `>= 1`. Zero rows ⇒ staging has not converged on this tip in the
last 4h — **FAIL**.

**Action 3 — gate JSONLs are green for the tip:**

```bash
for f in canary-status.jsonl smoke-status.jsonl; do
  echo "== $f =="; grep -F "$DEVELOP_TIP_SHA" "$f" | tail -1
done
```

Each file must have a record for `$DEVELOP_TIP_SHA` with a green/passing
status. A red record — or no record for the tip — is a **FAIL**.

**On pass**: post the AC verification comment on the R5 child ticket. R5
produces **zero commits**; the `runner:no-commits-expected` label
(`docs/sop/jira-ticket-conventions.md` §14) lets the runner forward-walk
the child after a clean exit. Do **not** fabricate a placeholder commit.

**On fail**:

1. Do **not** advance the RELEASE chain — leave the R5 child blocked.
2. Revert the R5 child to To Do.
3. File an operator-alert ticket linked to the active `RELEASE-vX.Y.Z`
   META naming the failed Action and the offending tip SHA (the Q8 "8b"
   model: red staging ⇒ JIRA blocker on the active META).

## 8. Dual-publish window (ghcr.io + GitLab CR)

> Ticket: OP-1488 (Phase G4 of META OP-1478 — image artifact pipeline
> overhaul). Applies to every `v*` cut between G4 start
> (2026-05-18) and the G5 cutover commit that retires the ghcr.io path.
> Phase 5 of the parallel META OP-1468 (OP-1473) already migrated prod
> `OMNISIGHT_REGISTRY` to `registry.sora.services/omnisight`; the G4
> dual-publish window is the *observation* layer that proves the
> GitLab-CR-only mode is safe to keep before G5 strips the GHCR fallback
> out of compose entirely.

The `v*` tag push in §2 fans out to **both** image pipelines:

| Pipeline | Triggered by | Pushes to |
|---|---|---|
| `.github/workflows/build-images.yml` (GHCR) | tag push to GitHub mirror | `ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/omnisight-{backend,frontend,bridge}` |
| `.gitlab-ci.yml` (GitLab CR) | tag push to `origin` = GitLab | `sora.services:49154/omnisight/OmniSight-Productizer/{backend,frontend,bridge}` |

Both fire automatically — there is no operator step beyond §2. The
verification steps below confirm both pipelines completed and
produced bit-identical digests.

**Action 1 — Wait for both pipelines to finish.** Each pipeline finishes
the `build`/`sign`/`sbom`/`attest`/`audit-emit` stages in ~25 min for a
clean cache, up to ~60 min on a cold runner. Watch:

```bash
gh run list --workflow=build-images.yml --branch=vX.Y.Z --limit=1
glab ci status --branch vX.Y.Z   # or `glab pipeline status` on older glab
```

Both must reach `success` before continuing. A failure in **either**
pipeline blocks the dual-publish AC — file a P2 INCIDENT JIRA tagged
`image-pipeline,registry` linked to OP-1488 and the active RELEASE META.

**Action 2 — Verify digest parity across the two registries.** The two
pipelines build independently; identical source + Dockerfiles must yield
identical content digests. Drift here is a stop-the-line signal that
the build context differs between runners.

```bash
for image in omnisight-backend omnisight-frontend omnisight-bridge; do
  case "$image" in
    omnisight-backend)  gitlab_repo=backend  ;;
    omnisight-frontend) gitlab_repo=frontend ;;
    omnisight-bridge)   gitlab_repo=bridge   ;;
  esac
  ghcr_digest=$(docker buildx imagetools inspect \
    "ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/${image}:vX.Y.Z" \
    --format '{{.Manifest.Digest}}')
  gitlab_digest=$(docker buildx imagetools inspect \
    "sora.services:49154/omnisight/OmniSight-Productizer/${gitlab_repo}:vX.Y.Z" \
    --format '{{.Manifest.Digest}}')
  if [ "$ghcr_digest" != "$gitlab_digest" ]; then
    echo "DIGEST DRIFT: $image — ghcr=$ghcr_digest gitlab=$gitlab_digest" >&2
    exit 1
  fi
  echo "$image OK $ghcr_digest"
done
```

Expected: each line prints `<image> OK sha256:...` with the same digest
recorded twice per image. Any `DIGEST DRIFT` line is a **FAIL** — abort
the release promotion, file a P1 INCIDENT, and treat the tag as
quarantined (do not promote to staging or prod until divergence is
explained).

**Action 3 — Record both digests in the release ticket.** Paste the
`<image> OK sha256:...` lines into a comment on the `RELEASE-vX.Y.Z`
META so the observation-window tracker in §11 has an audit trail.

## 9. Staging smoke — pull from GitLab CR

The point of dual-publish is to exercise the GitLab CR pull path on a
real Compose deploy before prod relies on it. Staging is the canary:
override `OMNISIGHT_REGISTRY` to the GitLab CR prefix, restart, and
verify the runtime endpoints respond.

This procedure runs on the staging host (e.g. `staging-app-01`); it
does **not** edit the prod `.env`. The G3 abstraction
(`[[L-OP-1487]]`, `tests/test_compose_registry_abstraction.py`)
guarantees compose accepts either registry prefix as a drop-in.

```bash
# 1. Pull the digest lock that pins this release.
scripts/build_image_bundle.py --tag vX.Y.Z --out staging.env.lock.json
source scripts/load_env_lock.sh staging.env.lock.json

# 2. Override OMNISIGHT_REGISTRY for this deploy only (do NOT persist
#    to /etc/omnisight/staging.env yet — the G5 cutover ticket owns
#    the durable flip).
export OMNISIGHT_REGISTRY=sora.services:49154/omnisight/OmniSight-Productizer

# 3. Confirm docker can authenticate to the GitLab CR.
docker login sora.services:49154

# 4. Bring the staging stack up against GitLab CR.
docker compose -f docker-compose.staging.yml up -d --pull=always

# 5. /readyz gate — Rule 5 of prod-deploy-runbook applies.
timeout 300 bash -c "
  until curl -fsS http://staging-app-01:8080/readyz | grep -q '\"ready\":true'; do
    sleep 5
  done
"

# 6. Smoke the integration surface — at least one card-bearing
#    endpoint per area (matches AC #3).
curl -fsS http://staging-app-01:8080/api/v1/agents/cards | jq '.cards | length'
curl -fsS http://staging-app-01:8080/api/v1/version    | jq '.image_source'
curl -fsS http://staging-app-01:8080/livez

# 7. Confirm running containers came from GitLab CR (not ghcr.io).
docker ps --format '{{.Image}}' | grep -E 'omnisight' | sort -u
```

Expected: `/readyz` returns `"ready": true` within the 300 s window;
`/api/v1/agents/cards` returns a non-empty `cards` array; the
`/api/v1/version` `image_source` field reports `gitlab-cr`; every
`docker ps` line starts with `sora.services:49154/...`.

Any of the four expectations failing is a **FAIL** → §10 rollback,
and the release is not eligible to promote to prod over the GitLab CR
path on this cut.

## 10. Rollback — GitLab CR pull failure on staging

If §9 fails — `/readyz` does not reach `ready:true`, the smoke
endpoints return 5xx, or `docker compose pull` fails to authenticate
against GitLab CR — restore the staging stack to the ghcr.io path
before investigating. The flip is `.env`-only; no image rebuild, no DB
change.

```bash
# 1. Unset the override and fall back to the compose default
#    (ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}). See
#    docker-compose.staging.yml header for the precedence rule.
unset OMNISIGHT_REGISTRY

# 2. If the override was persisted into /etc/omnisight/staging.env
#    (it should not have been — §9 step 2 keeps it shell-local — but
#    check for a stray edit), comment it out:
sudo sed -i 's|^OMNISIGHT_REGISTRY=.*|# OMNISIGHT_REGISTRY=  # rolled back per OP-1488 §10|' \
    /etc/omnisight/staging.env

# 3. Rolling restart — one service at a time, /readyz gated.
for service in backend-a backend-b frontend; do
  docker compose -f docker-compose.staging.yml up -d --pull=always "$service"
  timeout 300 bash -c '
    until curl -fsS http://staging-app-01:8080/readyz | grep -q "\"ready\":true"; do
      sleep 5
    done
  '
done

# 4. Confirm rollback: every running container should now use
#    ghcr.io/... again.
docker ps --format '{{.Image}}' | grep -E 'omnisight' | sort -u
```

After rollback, file a P2 INCIDENT JIRA tagged
`image-pipeline,registry,gitlab-cr` against META OP-1478, with:
- The failing curl/log output from §9.
- The digest pair from §8 Action 2 (so the investigator knows whether
  the GitLab CR side built but failed to serve, or never built).
- The exact `vX.Y.Z` tag, so retention does not garbage-collect the
  evidence under
  `docs/operations/image-retention-policy.md`.

Production rollback follows the same shape but lives in
`docs/operations/gitlab-cr-cutover-checklist.md` §5 — that checklist
already owns the prod-host `.env` flip back to ghcr.io and the
LB-drained rolling restart. Do **not** duplicate the prod path here.

## 11. Observation window (Phase G4)

AC #4 of OP-1488 requires **≥ 3 successful release cuts via
dual-publish over a ~2-week window with zero divergence incidents**
before G5 may strip the ghcr.io fallback. The window opens with the
first `v*` tag cut after OP-1488 lands and closes once the threshold is
met or a stop-the-line incident resets it.

Each cut in the window must record, on the `RELEASE-vX.Y.Z` META JIRA
ticket, a comment with the following structure:

```
OP-1488 G4 dual-publish record — RELEASE-vX.Y.Z
- Cut at: 2026-05-DDTHH:MMZ
- §8 Action 1 (both pipelines green): ghcr-run=<url> gitlab-run=<url>
- §8 Action 2 (digest parity): backend=<sha256:..> frontend=<sha256:..> bridge=<sha256:..>
- §9 (staging smoke from GitLab CR): pass | fail (<reason>)
- §10 rollback invoked: no | yes (<INCIDENT ticket>)
```

The G4 close-out ticket (a META child filed by the release operator
once the AC #4 threshold is met) aggregates these records and posts a
single roll-up comment on OP-1488 with the cut-by-cut table. The
roll-up is the artifact G5 cites when proposing to remove the
`OMNISIGHT_REGISTRY` default fallback to ghcr.io from
`docker-compose.{prod,staging}.yml`.

**Incident reset rule.** Any of these resets the count back to zero:
- A digest-drift FAIL in §8 Action 2.
- A staging-smoke FAIL in §9 that required §10 rollback.
- An INCIDENT JIRA tagged `image-pipeline` filed during the window
  with priority P0 or P1.

A reset is not punitive — it just means the dual-publish path has not
yet demonstrated stability. Document the reset on OP-1488 with the
INCIDENT link and restart the count from the next clean cut.
