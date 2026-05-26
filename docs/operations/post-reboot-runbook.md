# Post-reboot operator runbook (WSL2 forced-shutdown limit + Family ⑤/⑥ checklist)

**Owner**: on-call / deploy operator
**When to use**: the first login after the OmniSight host (a WSL2 guest
on a Windows host) has rebooted — Windows Update, a scheduled restart,
a user-triggered restart, a BugCheck (BSOD), or a hard power loss.
**Spec sources**:

- Family ⑧ graceful-shutdown contract —
  `docs/sprint-s12/2026-05-16-v2-family8-graceful-shutdown-contract.md`
  §8 (WSL2 constraints), §8.2 (recoverable-vs-not table),
  §8.4 (the mandatory "productizer cannot detect last forced shutdown"
  limitation), §9.3 (D1 detection delegated to Family ⑤ + ⑥),
  §7 (post-restart probe).
- Family ⑤ image-surfacing contract —
  `docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md`
  §7 (`OmniSightStaleImage` alert routing).
- Family ⑥ image/DB drift contract —
  `docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md`
  §5 (`/readyz` 503 `remediation` field), §7 (rescue CLI scope).

**Companion runbooks**:

- `docs/operations/shutdown-restart.md` — the cooperative
  shutdown/restart flow (`scripts/shutdown.sh` / `scripts/restart.sh`).
  *That* document covers a **graceful** stop you initiate; *this* one
  covers the aftermath of a reboot you did **not** drive, where the
  graceful path never ran.
- `docs/sop/runbook-family6-alembic-drift.md` — the
  `OmniSightAlembicDrift` page response, referenced from step 3 below.

> **Why this runbook is separate from `shutdown-restart.md`.** A
> cooperative shutdown (`scripts/shutdown.sh`, `wsl --shutdown` with no
> Windows reboot) gets the full 180 s drain envelope and the
> post-restart probe passes immediately. A Windows-side reboot does
> not — see §1. The recovery posture is different enough that conflating
> the two would bury the part that matters: **after a forced reboot you
> cannot trust that anything drained, and the productizer cannot tell
> you whether the reboot was forced at all.**

---

## 1. The ~10-second WSL2 `RB_POWER_OFF` hard limit

When Windows initiates a host reboot, WSL2 propagates a SIGTERM-like
notification to the guest's PID 1 (systemd) and then, after
**approximately 10 seconds**, the Linux kernel inside the WSL2 VM
receives `RB_POWER_OFF` and the entire VM is hard-stopped (Family ⑧
§8.1).

10 seconds is **far less** than the 180 s `TimeoutStopSec` envelope the
systemd unit declares for a cooperative stop. Under a Windows-side
reboot the guest **cannot** complete the `scripts/shutdown.sh` drain
sequence — the VM is gone before the script finishes, regardless of what
systemd is doing. Raising `TimeoutStopSec` does not help: the budget cap
is imposed Windows-side, outside the guest's control.

### 1.1 What is recoverable vs. what is not (Family ⑧ §8.2)

| Class of host shutdown | Time budget | Graceful contract holds? | Recovery mechanism |
|---|---|---|---|
| Cooperative `systemctl stop omnisight-compose-prod` | 180 s | **Yes** — full graceful drain via `scripts/shutdown.sh`. | Operator restart; post-restart probe (§3 / Family ⑧ §7) passes immediately. |
| Cooperative `wsl --shutdown` (no Windows reboot) | 60–180 s | **Mostly** — usually enough for a full graceful stop. | Operator restart; probe passes. |
| Windows reboot (Update / Schedule / User-triggered) | **~10 s** (`RB_POWER_OFF`) | **No** — the VM is gone before the script finishes. | Postgres WAL replay recovers the DB; Family ⑤ + ⑥ alerts + the §3 probe surface any inconsistency. |
| Windows BugCheck (BSOD) | **0 s** (hard kill) | **No** | Same as Windows reboot, but worse — inspect the §3 probe output manually on next boot. |
| Operator unplugs the host | **0 s** | **No** | Postgres WAL replay; operator inspection. |

The bottom three rows are **not defended against** by the graceful
shutdown contract. They are defended against by, in order:

1. **Postgres' own crash-safety** — WAL replay and fsync-on-commit make
   the DB correct on next boot without operator action. The per-service
   `stop_grace_period` is an *optimization* over WAL replay, never a
   substitute for it.
2. **Family ⑤'s `OmniSightStaleImage` alert** — fires post-reboot if the
   running image is older than the GHCR `:latest` digest (step 2 below).
3. **Family ⑥'s `OmniSightAlembicDrift` alert** — fires post-reboot if
   `alembic_version` does not match the running image's expected head
   (step 3 below).
4. **The §3 post-restart probe** — surfaces the symptoms into the
   systemd unit status (`systemctl status omnisight-compose-prod` goes
   `failed` if the probe does not pass).

None of these four needs to know *whether* the prior shutdown was
forced — and that is the point of §2.

---

## 2. ⚠ MANDATORY: the productizer cannot detect a forced shutdown

> **This is the load-bearing limitation of this runbook.** Per Family ⑧
> §8.4 (mandatory paragraph) and §9.3 (D1 detection is delegated, not
> implemented): **the productizer running inside WSL2 cannot tell
> whether the prior shutdown was cooperative or forced.** Do not expect
> a "last shutdown was forced" signal from any OmniSight surface — there
> is none, and there will never be one from inside the guest.

Why the guest is blind to the shutdown reason (Family ⑧ §8.4):

- **Windows BugCheck data** — the `MEMORY.DMP` file and the System
  event-log entries (Event ID 1074 / 1076) live on the Windows
  filesystem under `C:\Windows\`. The WSL2 guest has no read access by
  default, and parsing Windows event logs from Linux would need a
  Windows-side tool the productizer does not bundle.
- **Windows Update reboot scheduling** lives in the Windows registry
  (`HKLM\…\WindowsUpdate\Auto Update\RebootRequired`) — same
  accessibility problem.
- **Scheduled shutdowns** live in Task Scheduler XML — same problem.
- **The closest signal the guest *can* read** is the WSL2 init reboot
  timestamp (`last -x | grep reboot`, `/var/log/wtmp`). But that only
  tells you *that* the host rebooted, never *why*: Windows Update,
  operator-triggered, and crash all leave identical `wtmp` traces.

### 2.1 Operator action — check the Windows side yourself

Because the productizer cannot read the reason, **the operator must**.
On the first login after any host reboot:

1. Open **Event Viewer → Windows Logs → System** and filter for these
   Event IDs to classify the prior shutdown:

   | Event ID | Meaning |
   |---|---|
   | **1074** | A process (Windows Update, user, or app) initiated a clean shutdown/restart — typically *cooperative*. |
   | **1076** | The reason for an *unexpected* shutdown was recorded on the next boot (operator supplied a reason). |
   | **6008** | The previous shutdown was **unexpected** — i.e. **forced** (power loss / BugCheck / hard stop). |
   | 6005 / 6006 | Event Log service started / stopped cleanly — corroborating bookends for a clean cycle. |

   A `6008` (or a `1076` follow-up) is the signal that the reboot was
   **forced**. A clean `1074` followed by `6006`/`6005` indicates a
   *cooperative* reboot.

2. **If the reboot was cooperative** (1074 + clean bookends): the
   graceful drain almost certainly ran. Proceed to the step-3 checklist
   as a routine confirmation; you should expect everything green.

3. **If the reboot was forced** (6008 / BugCheck / hard power loss):
   the graceful drain did **not** run. Do **not** assume the automatic
   post-restart probe (Family ⑧ §7) succeeded silently on first boot —
   re-run it manually and read its output (step 3.1 below), then walk
   the full Family ⑤/⑥ alert checklist (steps 3.2–3.3), because a forced
   reboot can leave the alert pipeline itself in a partial state until
   Prometheus + Alertmanager are themselves healthy.

> **MUST NOT.** This runbook does **not** instruct you to install a
> Windows-side hook, scheduled task, or service to perform this check
> automatically — that is out of scope per Family ⑧ §10 (the productizer
> ships no Windows-side installer). The check is, and remains, a manual
> operator step. Optional operator-side hooks (a pre-shutdown WSL task,
> Group Policy shutdown-script budget) are described in Family ⑧ §8.3
> for operators who choose to configure them on their own host; they are
> not provided or required by OmniSight.

---

## 3. Post-reboot checklist

Run **all three** steps after every host reboot. After a *forced* reboot
(§2.1 step 3) treat them as mandatory rather than confirmatory.

### 3.1 Re-run the Family ⑧ §7 post-restart probe

The probe runs automatically from the systemd `ExecStartPost=` hook on
first boot, but after a forced reboot you must not assume it passed
silently. Re-run it and read the log output. The probe sequence (Family
⑧ §7.1) verifies, within the 60 s RTO budget:

1. **Postgres reachable** — `pg_isready` against the `db_ha` service.
2. **`alembic_version` row present** — a non-empty `version_num`.
3. **DB head matches the image's expected head** — compares the
   `alembic_version` row against `GET /version`'s
   `alembic_head_in_image` (this only *reads* the symptom; drift
   handling is delegated to Family ⑥ — step 3.3).
4. **Backend `/readyz` returns 200.**

```bash
# Trigger the oneshot probe (or inspect its first-boot result):
systemctl status omnisight-compose-prod        # FAILED ⇒ the probe did not pass
journalctl -u omnisight-postrestart-probe.service -n 50

# Manual spot-check of the same invariants the probe asserts:
pg_isready -h db_ha -p 5432 -t 2
curl -fsS http://localhost:8000/readyz | jq '.ready, .migrations'
```

A `failed` unit, a missing `alembic_version` row, or a `/readyz` that
never reaches 200 within 60 s means the stack did **not** reach a
known-good state — escalate via the relevant family runbook below.

### 3.2 Check the Family ⑤ `OmniSightStaleImage` alert state

A forced reboot can bring the host back up on a stale image (e.g. an
image was pulled-but-not-restarted before the reboot, or the
auto-redeploy timer had not yet fired). `OmniSightStaleImage` (Family ⑤
§7) fires when the running `image_sha` does not match the GHCR `:latest`
digest and that digest is older than the 24 h threshold.

```bash
# Did the alert fire / is it firing? (Alertmanager, once 31.G ships;
# v0 delivery is email + stdout — check the operator inbox / journal.)
# Confirm the running-vs-shipped truth directly via the evidence file:
jq '.result_state, .comparisons' docs/audit/AUDIT-deployment/latest.json
curl -fsS http://localhost:8000/version | jq '.image_sha, .build_time, .git_ref'
```

If `result_state` is `WARN_STALE_IMAGE` / `PAGE_STALE_IMAGE`, the host
came back on the wrong code. Remediate by redeploying the current image
(`scripts/auto-redeploy.sh`, or `docker compose pull && up -d backend`);
this is a forward-only action and needs no special authority. After a
forced reboot, also confirm the alert pipeline itself is healthy
(Prometheus + Alertmanager up) before trusting an *absence* of alert —
the pipeline may have rebooted too.

### 3.3 Check the Family ⑥ `OmniSightAlembicDrift` alert state

A forced reboot mid-migration can leave the image and DB transiently
misaligned. `OmniSightAlembicDrift` (Family ⑥ §4.5) pages when the
running image's bundled alembic head disagrees with the live
`alembic_version` row.

```bash
curl -fsS http://localhost:8000/metrics | grep '^omnisight_alembic_drift'
curl -fsS http://localhost:8000/readyz | jq '.migrations, .remediation'
```

The `/readyz` 503 body carries a structured `remediation` block (Family
⑥ §5) naming the operator-actionable options:

- **AHEAD** (image carries unapplied migrations): auto-heals via the
  startup hook; if it persists > 5 min the hook did not run.
- **BEHIND** (`image_head < db_head`): the container refuses to start
  (exit 78). Take the **forward path first** — redeploy a newer image.
  Only if no newer image is reachable, use the operator-only downgrade
  path `omnisight rescue drift --confirm` (Family ⑥ §7 — requires an L2
  fingerprint per ADR-0033 and captures a backup first). Full triage is
  in `docs/sop/runbook-family6-alembic-drift.md`.

> **Do NOT** run raw `alembic downgrade` against a prod DB to "unstick"
> a BEHIND state — that bypasses the backup, audit, and eligibility
> checks and is an incident class per Family ⑥ §7.3. The rescue CLI is
> the only supported downgrade path.

---

## 4. Quick reference — the three checks at a glance

| # | Check | Command anchor | Spec |
|---|---|---|---|
| 0 | Classify the reboot (Event Viewer 1074/1076/6008) | §2.1 | Family ⑧ §8.4 |
| 1 | Re-run post-restart probe | §3.1 | Family ⑧ §7 |
| 2 | `OmniSightStaleImage` state | §3.2 | Family ⑤ §7 |
| 3 | `OmniSightAlembicDrift` state | §3.3 | Family ⑥ §4.5 / §5 / §7 |

Remember: there is **no** OmniSight signal for "the last shutdown was
forced" (§2). Steps 1–3 detect the *consequences* of a bad reboot
without needing to know the cause — which is exactly why the manual
Event Viewer check in step 0 is the operator's job alone.

---

## 5. Related

- `docs/operations/shutdown-restart.md` — cooperative shutdown / restart.
- `docs/sop/runbook-family6-alembic-drift.md` — `OmniSightAlembicDrift`
  page response.
- `docs/sprint-s12/2026-05-16-v2-family8-graceful-shutdown-contract.md`
  — WSL2 constraints (§8), forced-shutdown limitation (§8.4), detection
  delegation (§9.3), post-restart probe (§7).
- `docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md`
  — `OmniSightStaleImage` (§7), `/version` endpoint (§3).
- `docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md`
  — `/readyz` `remediation` (§5), rescue CLI (§7).
- `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md` — the
  incident that motivated the Family ⑤/⑥ post-reboot alert coverage.
</content>
</invoke>
