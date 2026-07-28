# Deploying staging, by hand

**Ticket:** OP-2737 · **Supersedes:** the retired `staging-sync` lane

## Why this document exists

Staging was supposed to track `develop` continuously via `staging-sync`. That
lane never once succeeded — 10,093 failures, zero successes, since 2026-05-12 —
and it was retired rather than repaired, because its premise did not match how
anything here actually works:

- it triggered on **"develop moved"**, but a deploy needs an **image**, and CI
  builds one only when `CANDIDATE_SHA` is set on an `api`/`web` pipeline. Those
  two events almost never coincide: measured 2026-07-29, **0 of the last 15
  develop tips had an image**.
- staging is not a continuously-tracking environment. It is brought up
  deliberately to test large version bumps.

So the real deployment path has always been by hand. It just was not written
down, which is why a lane that never worked could sit there for two and a half
months looking like the answer.

## The actual procedure

Staging deploys **by image tag**, and the tag format is `sha-<40hex>` — not the
bare commit SHA. That mismatch was one of the retired lane's three defects, and
it is the easiest thing to get wrong by hand too.

```sh
# 1. Confirm an image exists for the SHA you intend to deploy.
#    An image exists only for deliberately-cut candidates, not for every commit.
docker manifest inspect \
  sora.services:49160/omnisight/omnisight-productizer/backend:sha-<40hex>

# 2. Deploy it.
scripts/staging_deploy.sh --image-tag sha-<40hex>
```

Step 1 is not optional ceremony. Without an image the deploy fails at the pull,
which is precisely how the retired lane spent 10,093 runs.

## Checking what staging is on now

```sh
docker ps --filter name=omnisight-staging --format '{{.Names}}\t{{.Status}}'
```

Note the compose project is **`omnisight-staging`**, so containers are
`omnisight-staging-*`. An older name (`staging-postgres-1`) is still baked into
`staging-pg-snapshot`'s unit and is the reason that lane has failed 75/75 times
(OP-2736).

## What was NOT retired

`scripts/staging_deploy.sh` stays — it is the tool this document uses.
`staging-gate-alert.service` stays — it is shared with the canary, smoke,
pg-snapshot and deployment-audit lanes; only `staging-sync`'s reference to it
was dropped.

## If staging ever becomes continuous again

Subscribe to **"a candidate image was published"**, never "develop moved".
Anything else re-creates a lane whose trigger and precondition cannot both be
true, and the cost is not just failure — it is a deploy racing whoever is
setting staging up for a test, at exactly the moment they are doing it.
