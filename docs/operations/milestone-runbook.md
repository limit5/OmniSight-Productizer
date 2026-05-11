# Release Milestone Runbook

> Ticket: OP-868
>
> Scope: fixVersion-backed milestone definition and acceptance checks.

## Define A Milestone

Create the JIRA fixVersion and the matching `release_milestones` row:

```bash
python3 scripts/milestone_define.py --version vX.Y.Z
```

The command refuses duplicates with `MilestoneAlreadyExists`. The database
table is created by Alembic revision `0221_release_milestones.py`.

Dry-run proof for operators:

```bash
python3 scripts/milestone_define.py \
  --version v0.99-rc1 \
  --database-url sqlite:////tmp/op868-milestones.db \
  --dry-run
```

## Check Acceptance

Run the acceptance checker and write a JSON report:

```bash
python3 scripts/milestone_check.py \
  --version vX.Y.Z \
  --out /tmp/milestone-vX.Y.Z.json
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Ready |
| `1` | Not ready |
| `2` | Operational error |

The checker verifies that all fixVersion tickets are `公開済み` or
`Archived`, no open `Blocker` priority tickets remain in the same
fixVersion, and required Gerrit `Verified` approvals are green on merged
commits.

If OP-739 Verified label support is unavailable, run with:

```bash
python3 scripts/milestone_check.py \
  --version vX.Y.Z \
  --out /tmp/milestone-vX.Y.Z.json \
  --verified-label-missing
```

That path emits `MilestoneVerifiedLabelMissing` and leaves the milestone
not ready with an explicit TODO marker in the JSON report.

## Force Accept Override

Adding `milestone:force-accept` to any ticket in the fixVersion skips the
Verified-label requirement only. JIRA status and open blocker checks still
apply. The checker appends a JSONL audit row to:

```text
logs/release-milestones/audit.jsonl
```

## Nightly Cron

The nightly cron checks the current and next `release_milestones` rows
and comments on the META ticket only when a milestone status changes:

```cron
17 3 * * * cd /home/user/sora-bridge && \
  python3 scripts/milestone_check.py \
    --nightly \
    --out /var/log/omnisight/milestone-nightly.json \
    --meta-ticket OP-761
```

Dry-run synthetic acceptance proof:

```bash
python3 scripts/milestone_check.py \
  --version v0.99-rc1 \
  --database-url sqlite:////tmp/op868-milestones.db \
  --out /tmp/op868-milestone-report.json \
  --dry-run
```
