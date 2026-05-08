# Daily drift scanner — `scripts/drift_scanner.py`

OP-726 (META OP-721 / T5). Daily cron that detects state drift across
the deploy surface so silent divergences are surfaced in 24h instead
of weeks-after-the-fact.

## What it checks

Each kind of drift gets a stable code so the alert wrapper (T1) can
route per-code (e.g. page on `image_drift`, ticket on `main_branch_drift`).

| Code | Compares | Drift means |
|---|---|---|
| `image_drift` | `--image-revision` (the commit the deployed backend image was built from) vs `<remote>/develop` | Image rebuild needed. Common after lesson L19 / OP-693 SP-C. |
| `schema_drift` | `--db-version` (current `alembic_version` row) vs leaf revision in `backend/alembic/versions/` | DB needs `alembic upgrade head`, **or** the on-disk head is wrong. |
| `refs_meta_config_drift` | A checkout of live `refs/meta/config` vs `.gerrit/project.config.example` (and `webhooks.config` if present) | Live Gerrit policy diverged from the documented sample (OP-708 / OP-713 / lesson L22). |
| `bridge_drift` | Deployed bridge checkout (default `~/sora-bridge`) HEAD vs `<remote>/develop` | Bridge was hand-patched at runtime; repo copy is stale (2026-05-08 incident). |
| `main_branch_drift` | Local `main` vs `<remote>/main` | Local accumulated merge commits without `git push origin main`. |

## Exit codes

| Code | Meaning | Wrapper action |
|---|---|---|
| 0 | No drift — every check is clean or skipped (input not provided). | No alert. |
| 1 | At least one `DEGRADED` drift detected. | Forward each `DEGRADED` line to T1. |
| 2 | Scanner environment broken (no `git`, repo missing, malformed migrations). | Page the on-call — drift status is *unknown*. |

Exit 1 is independent of severity count: a single drift exits 1, ten
drifts also exit 1. Each individual drift is one stderr line for the
T1 wrapper to scrape.

## Output formats

### Text (default, on stderr)

    [DEGRADED] code=image_drift severity=DEGRADED message='deployed image was built from abcd1234 but origin/develop is ef567890 — image rebuild needed (see lesson L19, OP-693 SP-C)'
    [INFO]     code=schema_drift severity=INFO message='alembic_version row matches head revision \'0202\''

### JSON (`--json`, on stdout)

    {
      "results": [
        {"code": "image_drift", "severity": "DEGRADED", "message": "...",
         "expected": "ef567890...", "actual": "abcd1234...",
         "auto_fixable": true},
        ...
      ]
    }

`auto_fixable` is advisory — flips on for codes where a Gerrit-change
auto-correction would be safe (image rebuild, push, redeploy). Not
flipped for `schema_drift` / `refs_meta_config_drift` because those
corrections are destructive enough to warrant a human review.

## How the daily cron supplies inputs

The scanner is read-only and stdlib-only by design — it never shells
out to docker / psql / ssh itself. The cron wrapper resolves each
input from the deployed surface and passes it as a CLI flag.

A representative wrapper (paste-into-cron template):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /home/user/work/sora/OmniSight-Productizer

# Image revision: read the OCI label set at build time.
IMAGE="ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/omnisight-backend:${OMNISIGHT_IMAGE_TAG:-latest}"
IMAGE_REV=$(docker inspect "$IMAGE" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  2>/dev/null || true)

# DB version: query the live alembic_version row.
DB_VER=$(docker compose -f docker-compose.prod.yml exec -T pg-primary \
  psql -tAU postgres omnisight \
  -c "select version_num from alembic_version" \
  2>/dev/null | tr -d '[:space:]' || true)

# refs/meta/config: fetch into a tmp dir and point the scanner at it.
TMPCFG=$(mktemp -d)
trap 'rm -rf "$TMPCFG"' EXIT
git -C "$TMPCFG" init -q
git -C "$TMPCFG" fetch -q ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer \
  refs/meta/config:refs/heads/meta-config
git -C "$TMPCFG" checkout -q meta-config

git fetch -q origin develop main

python3 scripts/drift_scanner.py \
  ${IMAGE_REV:+--image-revision "$IMAGE_REV"} \
  ${DB_VER:+--db-version "$DB_VER"} \
  --remote-config-path "$TMPCFG" \
  --bridge-path "$HOME/sora-bridge" \
  --json
```

Cron line — daily at 04:30 UTC after the morning fetch settles:

    30 4 * * * /home/user/work/sora/OmniSight-Productizer/scripts/drift_scanner_cron.sh \
      | tee -a /home/user/work/sora/logs/drift-scanner.log \
      | /home/user/work/sora/OmniSight-Productizer/scripts/t1_alert_wrapper.sh

## Auto-fix mode (`--auto-fix`)

Opt-in. Currently logs *which* drifts are safe to auto-correct
(`image_drift`, `bridge_drift`, `main_branch_drift`); does **not**
actually open a Gerrit change in this revision. The intent is for a
follow-up ticket to wire up the Gerrit-change generation path, gated
by a non-source +1 review (per CLAUDE.md L1 — the AI reviewer cap of
+1 still applies).

`schema_drift` and `refs_meta_config_drift` are intentionally
**never** in the auto-fixable set — both can lose data or break submit
rules under partial application.

## Skipping a check

`--skip <code>` (repeatable). Use sparingly; document in
HANDOFF.md / ticket comments. Common case: when one check's input
source is temporarily unavailable, skip it explicitly rather than
letting it INFO-skip silently — the latter looks the same as
"all-clean" in dashboards.

## Tests

`tests/test_drift_scanner.py` covers all three OP-726 acceptance
criteria as named tests:

  * `test_ac1_image_pin_falls_behind_new_develop_commit`
  * `test_ac2_remote_config_diverges_from_sample`
  * `test_ac3_no_drift_exits_zero_with_info_only`

Plus per-check unit-level scenarios for `schema_drift`, `bridge_drift`,
`main_branch_drift`, JSON output shape, `--quiet`, `--auto-fix`, and
the env-failure-vs-drift exit-code distinction.
