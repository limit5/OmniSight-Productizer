# gitlab-runner install + register (OP-1478 activation O1)

> **Ticket**: OP-1512 (META OP-1478 activation step O1)
> **Status**: authoritative operator runbook
> **Owner**: prod-on-call operator
> **Companion docs**:
> - [`gitlab-ci-image-build.md`](gitlab-ci-image-build.md) — pipeline definition this runner executes
> - [`gitlab-cr-cutover-checklist.md`](gitlab-cr-cutover-checklist.md) — the cutover that this runner unblocks
> - [`image-retention-policy.md`](image-retention-policy.md) — retention policy applied to the images this runner publishes

This runbook walks the operator through installing and registering the
self-managed `gitlab-runner` daemon on the prod+dev co-existing host so
that `.gitlab-ci.yml` pipelines (currently `failed` with all jobs
`skipped` because zero runners are registered) start producing signed
images in the GitLab Container Registry.

---

## Why this exists

Verified 2026-05-19 11:00 CST:

- `.gitlab-ci.yml` is on develop tip and fires on `v*` tags
  (pipeline #28 triggered by `v0.5.0-rc5` at 01:00).
- Pipeline #28 and all earlier `v*` pipelines came back `failed` with
  15 jobs `skipped`. Root cause: `GET /runners` for the project returns
  `[]`. No runner is registered, so no job has a worker to land on.
- GitLab Container Registry is empty as a consequence.
- `docker-compose.prod.yml` still references `ghcr.io/...` paths; the
  prod host runs from its local docker cache, which works by accident
  rather than by design.

The activation chain this O1 step unblocks:

```
register runner  →  pipeline produces images  →  bundle.json populated
                 →  CR populated  →  compose cutover  →  ghcr.io decom
```

Per ADR-0001, CI moves entirely to GitLab; ghcr.io is decommissioned at
the end of the chain. This runbook covers only the first arrow.

---

## Risk acknowledgement

The prod host is the **same machine** that serves production traffic
and development workloads. Registering a runner with the docker executor
in `--docker-privileged` mode (required by the dind pattern in
`.gitlab-ci.yml`) is a real attack-surface increase: a privileged
build container shares the host kernel and can in principle escape to
the docker daemon socket.

This trade-off is **accepted as known debt** until a dedicated CI host
is provisioned. Mitigations in scope here:

- Runner is registered with a **project token** (not an instance token),
  so it only picks up jobs from `omnisight/OmniSight-Productizer`.
- Tag-list pinning (`docker,dind`) prevents accidental enrollment by
  unrelated pipelines if the token is ever widened to group scope.
- Rollback (§ Rollback) is a single `systemctl stop` away.

---

## Prereqs

- `sudo` access on the prod+dev co-existing host (`sora.services`).
- Docker daemon healthy: `sudo systemctl is-active docker` returns
  `active` and `docker info` succeeds without errors.
- GitLab project token. To mint one:
  1. Open <http://sora.services:49156/omnisight/omnisight-productizer/-/settings/ci_cd>.
  2. Expand **Runners** → **Project runners** → **New project runner**.
  3. Tags: `docker,dind`. Run untagged jobs: **off**. Lock to current
     project: **on**.
  4. Copy the registration token. **Treat as secret** — anyone holding
     it can register a worker that picks up this project's CI jobs.
- Read access to `~/.config/omnisight/gitlab-claude-token` (an admin
  API token, used only for the verification curl in §4 — not for
  registration).

---

## Steps

### 1. Install the `gitlab-runner` binary

Use the upstream apt repository so the version tracks GitLab server
releases and so `apt upgrade` keeps the binary patched.

```bash
curl -L https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh \
  | sudo bash
sudo apt install gitlab-runner
```

Verify:

```bash
gitlab-runner --version
# expect: Version: 17.x.y  (≥ 17.0 — matches the 17.x server)
```

If the version is `< 17.x`, stop and check that the upstream apt repo
is configured (`/etc/apt/sources.list.d/runner_gitlab-runner.list`
should exist and point at `packages.gitlab.com`).

### 2. Register the runner

Non-interactive form (preferred — reproducible, scriptable, no TTY
prompts that can hang in automation):

```bash
sudo gitlab-runner register --non-interactive \
  --url "http://sora.services:49154" \
  --registration-token "<TOKEN_FROM_UI>" \
  --executor docker \
  --description "omnisight-prod-host-runner" \
  --tag-list "docker,dind" \
  --docker-image "docker:27.3.1" \
  --docker-privileged \
  --docker-volumes /certs/client
```

Flag rationale (read before changing any of these):

| Flag | Why this value |
|------|----------------|
| `--url` | Internal GitLab base URL the runner long-polls. Must match the `CI_SERVER_URL` the GitLab instance advertises. |
| `--executor docker` | `.gitlab-ci.yml`'s `.docker_job` template uses dind; only the docker executor supports the `services:` block as written. |
| `--description` | Surfaces in the GitLab UI runner list — keep stable so dashboards and alerts can pin to it. |
| `--tag-list "docker,dind"` | Project jobs all carry these tags. Untagged jobs are explicitly rejected (see §Risk). |
| `--docker-image "docker:27.3.1"` | Matches the `image:` pinned in `.gitlab-ci.yml`'s `.docker_job` extends-base. Drift here breaks reproducibility of the cosign/syft installer downloads. |
| `--docker-privileged` | Required by the dind service. **Real attack-surface cost** — see §Risk acknowledgement. |
| `--docker-volumes /certs/client` | dind TLS cert path; even with `DOCKER_TLS_CERTDIR=""` in the pipeline, the executor templates expect this mount to exist. |

**Security note**: `--docker-privileged` is required by the dind
pattern in `.gitlab-ci.yml`. This is a real attack-surface increase on
a host that also runs prod. The trade-off is accepted until a dedicated
CI host is provisioned; this risk is documented at the top of this
runbook and referenced from CLAUDE.md.

After this command completes, the runner config is written to
`/etc/gitlab-runner/config.toml`. Inspect it once to confirm the
`[[runners]]` block matches what you passed and that `token = "..."`
(the **runner** token, distinct from the **registration** token) is
populated.

### 3. Replace the deb-installed systemd unit with the project-managed template

The upstream `.deb` ships a generic systemd unit that runs the daemon
as the `gitlab-runner` user with no hardening flags and no journal
identifier we can pin Loki alerts against. The project-managed unit at
`deploy/systemd/gitlab-runner.service` (companion to this runbook)
adds:

- `SyslogIdentifier=gitlab-runner` so the journal forwarder
  (`omnisight-journal-error-forwarder.service`) can match it.
- `Restart=on-failure` with a bounded `RestartSec` so a crash loop
  doesn't burn the host.
- `MemoryMax` / `TasksMax` ceilings sized for the prod+dev host so a
  runaway build can't starve omnisight-backend.

To install:

```bash
sudo cp /home/user/work/sora/OmniSight-Productizer/deploy/systemd/gitlab-runner.service \
        /etc/systemd/system/gitlab-runner.service
sudo systemctl daemon-reload
sudo systemctl enable --now gitlab-runner.service
```

**If `deploy/systemd/gitlab-runner.service` is not yet present in the
checkout**: the deb-installed default unit at
`/lib/systemd/system/gitlab-runner.service` is acceptable for the
initial activation. File a follow-up ticket against META OP-1478 to
land the project-managed unit before the next prod restart, and skip
the `cp` step above. Run only:

```bash
sudo systemctl enable --now gitlab-runner.service
```

### 4. Verify

Four independent checks — all must pass before proceeding to step 5.

```bash
# 4a: systemd unit is up
systemctl status gitlab-runner.service | head -5
# expect: Active: active (running)
```

```bash
# 4b: GitLab API sees the runner as online
curl -sk \
  "http://sora.services:49154/api/v4/projects/omnisight%2FOmniSight-Productizer/runners" \
  -H "PRIVATE-TOKEN: $(cat ~/.config/omnisight/gitlab-claude-token)" \
  | python3 -m json.tool
# expect: array with ≥ 1 entry, "status": "online",
#         "description": "omnisight-prod-host-runner"
```

```bash
# 4c: GitLab UI cross-check
# Open: http://sora.services:49156/omnisight/omnisight-productizer/-/settings/ci_cd
# Expand "Runners". Expect:
#   - one runner with the green online dot
#   - description "omnisight-prod-host-runner"
#   - tags "docker, dind"
```

```bash
# 4d: runner can reach the registry it will push to
sudo -u gitlab-runner docker login sora.services:49154
# expect: "Login Succeeded"
# (uses the gitlab-runner user's docker credential helper; CI itself
#  re-logs in via $CI_REGISTRY_* in the .docker_job before_script)
```

If 4b returns `[]` but 4a is `active`, the most common cause is the
runner registered against an unreachable `--url`. Re-check the URL in
`/etc/gitlab-runner/config.toml` matches what step 2 used and what the
GitLab instance advertises in its `external_url` (omnibus config).

### 5. Trigger the first pipeline retry

Option A (preferred — visible audit trail in the UI):

1. Open <http://sora.services:49156/omnisight/omnisight-productizer/-/pipelines/28>
   (pipeline #28, triggered by tag `v0.5.0-rc5`).
2. Click **Retry**.
3. Watch all 15 jobs progress through `pending` → `running` →
   `success`: build × 3 + sign × 3 + sbom × 3 + attest × 3 +
   audit-emit × 3 (matrix: backend, frontend, bridge).

Option B (scriptable, when the UI is unreachable):

```bash
curl -X POST \
  -H "PRIVATE-TOKEN: $(cat ~/.config/omnisight/gitlab-claude-token)" \
  "http://sora.services:49154/api/v4/projects/omnisight%2FOmniSight-Productizer/pipelines/28/retry"
```

Then poll status:

```bash
curl -s \
  -H "PRIVATE-TOKEN: $(cat ~/.config/omnisight/gitlab-claude-token)" \
  "http://sora.services:49154/api/v4/projects/omnisight%2FOmniSight-Productizer/pipelines/28" \
  | python3 -c 'import sys,json; p=json.load(sys.stdin); print(p["status"])'
# expect eventually: success
```

Expected wall-time end-to-end: 15–25 minutes (dominated by the
parallel image build + cosign/syft installer downloads on first run;
subsequent runs are cache-warm and complete in ~8–12 minutes).

---

## Verification checklist

Operator copy-pastes filled-in values into a comment on ticket O1
(the activation tracker for OP-1478):

```text
- [ ] gitlab-runner version: ________________________
- [ ] /runners API count: ________________________ (expect ≥ 1)
- [ ] Pipeline #28 retry status: ________________________ (expect: success)
- [ ] GitLab CR images visible: backend, frontend, bridge at tag v0.5.0-rc5
- [ ] bundle.json from new image: not 'local-dev' anymore
      (curl http://sora.services:49156/api/v4/projects/omnisight%2FOmniSight-Productizer/registry/repositories
       and confirm three repositories exist)
```

If any line cannot be checked ✓, **stop** — do not proceed to the CR
cutover (OP-1473, `gitlab-cr-cutover-checklist.md`). The cutover
assumes signed images exist in the registry.

---

## Rollback

The runner is fail-safe: stopping it makes pipelines `skip` again
(the same `failed`/`skipped` state that exists today). It does **not**
touch prod traffic, prod images, or the omnisight-backend/frontend
processes on the host.

If runner activity causes prod disturbance (CPU spike, docker daemon
instability, OOM pressure on the host):

```bash
# 1. Stop the runner — in-flight CI jobs die, prod is unaffected.
sudo systemctl stop gitlab-runner.service

# 2. Prevent restart on reboot until the disturbance is root-caused.
sudo systemctl disable gitlab-runner.service

# 3. (Optional) Pause at the GitLab side so the UI shows the runner
#    as paused rather than offline — clearer signal for other operators.
curl -X POST \
  -H "PRIVATE-TOKEN: $(cat ~/.config/omnisight/gitlab-claude-token)" \
  "http://sora.services:49154/api/v4/runners/<RUNNER_ID>" \
  --data "paused=true"
```

After rollback, file a hotfix ticket against META OP-1478 describing
the disturbance (CPU/memory/io profile, time window, correlated host
metrics). Do **not** re-enable the runner until that ticket has a
verified mitigation — re-enablement otherwise just reproduces the
incident.

---

## Cross-references

- [`gitlab-ci-image-build.md`](gitlab-ci-image-build.md) — what
  `.gitlab-ci.yml` does once this runner picks up its jobs.
- [`gitlab-cr-cutover-checklist.md`](gitlab-cr-cutover-checklist.md) —
  the prod cutover this O1 unblocks (OP-1473).
- [`image-retention-policy.md`](image-retention-policy.md) — the
  retention sweep that applies to images this runner publishes.
- ADR-0001 (CI 全面走 GitLab Only) — governance decision driving the
  ghcr.io → GitLab CR migration.
