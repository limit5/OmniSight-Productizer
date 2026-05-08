# `image_drift`

| field | value |
|-------|-------|
| Severity | `DEGRADED` |
| Source | T5 drift scanner — `scripts/drift_scanner.py` |
| Tier owner | release-eng |
| Parent META | OP-721 |

## What triggers it

Once daily the drift scanner reads the **pinned** container image
SHA from `deploy/` (whichever compose / k8s manifest is
authoritative for the deploy host) and compares it against what
the host **actually ran** for the relevant service. If they
differ, it emits a `DriftResult` with `code="image_drift"`,
exit-code 1, and a `[DEGRADED] code=image_drift severity=DEGRADED`
text line on stderr.

Common drift sources:

* `docker compose pull && up -d` ran out-of-band, picking up
  `:latest` instead of the pinned digest;
* a manual `docker run` test left a different image as the
  active container;
* the deploy was rolled back via the registry but the manifest
  was not updated.

## Severity rationale

`DEGRADED` rather than `CRITICAL` because:

* the deployed image may still be a *valid*, functional image —
  just not the one the manifest claims;
* drift is detected by a daily timer, so it can sit for up to 24h
  without operator harm in the worst case;
* if the drift causes a real failure, the relevant per-feature
  alerts (`refused_llm_unavailable`, `gerrit_client_ssh_failed`,
  etc.) page at higher severity from inside the running daemon.

But: image drift is the **leading indicator** of a broken release
process. Don't sit on it.

## Immediate action

1. **Confirm the drift is real.** Run the scanner manually and
   read the JSON output:

       python scripts/drift_scanner.py --json | jq '.[] | select(.code == "image_drift")'

   The payload includes `expected_sha`, `observed_sha`, and the
   `service` name. Sanity-check both ends:

       grep -n "$(jq -r .expected_sha <<< "$payload")" deploy/
       docker inspect "$service" --format '{{.Image}}'

2. **Decide direction**:

   * **Roll forward** to what the manifest claims (most common):

         docker pull "$expected_sha"
         docker compose up -d "$service"

   * **Update the manifest** to what is actually running (only if
     the running image is intentional and the manifest is stale):

         # edit deploy/docker-compose.prod.yml — pin the new digest
         git commit -m "deploy: pin <service> to <digest> ([T5 image_drift] OP-728)"

   The `--auto-fix` mode of the scanner is **advisory only** in the
   current revision — it never auto-rolls images. The decision is
   manual.

3. **Re-run the scanner** to confirm clean:

       python scripts/drift_scanner.py --json | jq -e 'all(.[]; .code != "image_drift")'

## Root-cause investigation

* `docs/ops/upgrade_rollback_ledger.md` — the entry preceding the
  drift event identifies whether a rollback skipped the manifest
  update.
* Recent `docker compose ...` invocations in shell history /
  `docs/ops/production_deploy.md` — out-of-band `pull` is the
  classic culprit.
* CI deploy logs — was a deploy step skipped? Did
  `bluegreen_label_decider.py` emit anything?

## Escalation

* Resolved within 24h: low-priority follow-up; record the cause.
* Recurring (≥2 in a week): the deploy process itself is leaking;
  open a META ticket against release-eng.
* Drift correlated with a customer-reported regression: treat as
  `CRITICAL` manually and trigger the rollback runbook
  (`docs/ops/blue_green_runbook.md`).
