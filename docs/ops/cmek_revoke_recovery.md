# CMEK Revoke Recovery Runbook (KS.2.6)

> Operator-facing recovery path for Tier 2 tenants whose customer-managed
> KMS key was disabled or whose OmniSight IAM / service account / Vault
> policy access was revoked.

## 1. Triage

Confirm the API response has:

```json
{
  "error_code": "cmek_revoked",
  "retryable": false,
  "recovery_runbook": "docs/ops/cmek_revoke_recovery.md"
}
```

Do not retry the same request. A retry cannot succeed until the customer
restores KMS access and the CMEK health detector observes a healthy key.

## 2. Customer-Side Restore

Ask the tenant admin to restore the same key resource that was configured
in the CMEK wizard:

| Provider | Customer action |
|---|---|
| AWS KMS | Re-enable the key if disabled, then restore `kms:DescribeKey`, `kms:Encrypt`, and `kms:Decrypt` to the OmniSight role. |
| Google Cloud KMS | Re-enable the primary crypto key version if disabled, then restore `roles/cloudkms.cryptoKeyEncrypterDecrypter` to the OmniSight service account. |
| Vault Transit | Re-enable encrypt/decrypt support for the transit key, then restore `update` on the encrypt and decrypt paths. |

Do not rotate to a new key in this runbook. Tier 1 / Tier 2 upgrade,
downgrade, and re-encrypt flows are separate KS.2 rows.

## 3. OmniSight Verification

Wait for the next CMEK detector tick. The detector runs every 30 seconds
and the KS.2.5 contract requires revoke detection within 60 seconds.

Verify that the latest health snapshot for the tenant is healthy:

```bash
python - <<'PY'
from backend.security import cmek_revoke_detector as d
for row in d.latest_cmek_health_results():
    print(row)
PY
```

Expected result for the tenant: `ok=True`, `revoked=False`, and
`reason="describe_ok"`.

## 4. Resume

Once the detector reports healthy, ask the customer to submit a new
request. Requests that were already in flight before revoke detection
are allowed to finish; only new request-start guards return 403.

If the tenant still receives `cmek_revoked`, check that all backend
workers have completed a detector tick after the customer-side restore.
