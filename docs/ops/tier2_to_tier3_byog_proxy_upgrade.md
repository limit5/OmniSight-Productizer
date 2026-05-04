# Tier 2 to Tier 3 BYOG Proxy Upgrade Runbook (KS.3.8)

> Operator-facing migration path for tenants moving from Tier 2 CMEK
> storage to Tier 3 BYOG proxy. The sequence is strict:
> **OmniSight export -> customer imports into proxy -> OmniSight clears
> provider key material**.

## 1. Preconditions

Confirm the tenant is already healthy on Tier 2 CMEK before starting:

```bash
python - <<'PY'
from backend.security import cmek_revoke_detector as d
for row in d.latest_cmek_health_results():
    print(row)
PY
```

Expected result for the tenant: `ok=True`, `revoked=False`, and
`reason="describe_ok"`.

Also confirm the customer change-control ticket lists:

| Item | Required evidence |
|---|---|
| Proxy endpoint | Customer-owned HTTPS URL reachable from OmniSight egress allowlist |
| mTLS material | Tenant client CA, server certificate fingerprint, and cert expiry date |
| Nonce signing | Shared nonce HMAC key location or customer KMS / Vault reference |
| Provider inventory | Provider names, model allowlist, and existing OmniSight fingerprints |
| Import owner | Customer SRE who can write proxy config / secret mounts |
| Clear owner | OmniSight operator approved to purge SaaS-side key material |

Do not begin the export unless the customer has an empty
`omnisight-proxy` deployment ready to receive the keys.

## 2. Deploy the Customer Proxy

The customer deploys the same `omnisight-proxy` image published by the
KS.3.1 image pipeline. Provider key material stays in customer
infrastructure after import.

Minimum runtime environment:

```bash
OMNISIGHT_PROXY_ADDR=:8443
OMNISIGHT_PROXY_AUTH_ENABLED=true
OMNISIGHT_PROXY_ID=proxy-acme-prod
OMNISIGHT_PROXY_TENANT_ID=t-acme
OMNISIGHT_PROXY_TLS_CERT_FILE=/run/certs/proxy-server.crt
OMNISIGHT_PROXY_TLS_KEY_FILE=/run/certs/proxy-server.key
OMNISIGHT_PROXY_CLIENT_CA_FILE=/run/certs/omnisight-client-ca.pem
OMNISIGHT_PROXY_PINNED_CLIENT_CERT_SHA256=sha256:<64-hex-fingerprint>
OMNISIGHT_PROXY_NONCE_HMAC_KEY_FILE=/run/secrets/nonce-hmac.key
OMNISIGHT_PROXY_PROVIDER_CONFIG_FILE=/etc/omnisight-proxy/providers.json
OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL=https://ai.sora-dev.app/api/v1/byog/proxies/proxy-acme-prod/heartbeat
OMNISIGHT_PROXY_SAAS_AUDIT_URL=https://ai.sora-dev.app/api/v1/byog/proxies/proxy-acme-prod/audit
OMNISIGHT_PROXY_CUSTOMER_AUDIT_LOG_FILE=/var/log/omnisight-proxy/audit.ndjson
```

Start with an empty provider catalog to prove mTLS and heartbeat before
keys are imported:

```json
{
  "providers": [
    {
      "name": "connectivity-check",
      "base_url": "https://example.invalid",
      "key_source": {
        "type": "local_file",
        "path": "/run/secrets/connectivity-check"
      }
    }
  ]
}
```

Verify the proxy heartbeat appears connected in OmniSight:

```http
GET /api/v1/byog/proxies/proxy-acme-prod/health
```

Expected result: `connected=true`, `stale=false`, and
`stale_threshold_seconds=60`.

## 3. Export From OmniSight

The OmniSight operator creates a one-time export bundle from the Tier 2
credential store. The bundle must be sealed to a customer migration public key
and must contain only the providers approved in the change-control
ticket.

Record the export in the customer ticket:

| Field | Value |
|---|---|
| Tenant | `t-acme` |
| Export id | `byog-export-<timestamp>` |
| Provider fingerprints | Fingerprint list from the Tier 2 credential rows |
| Bundle SHA-256 | SHA-256 of the sealed export bundle |
| Expiry | Export expires after the approved migration window |

The plaintext keys may be visible only inside the export worker memory
and the sealed customer bundle. Do not paste plaintext keys into chat,
ticketing systems, logs, or HANDOFF entries.

## 4. Customer Import Into Proxy

The customer imports the sealed bundle into the proxy's chosen key
source. Supported import targets mirror the KS.3.3 proxy config schema:

| Proxy key source | Customer import action |
|---|---|
| `local_file` | Decrypt the sealed bundle on a customer host and write each key to a `0600` secret mount. |
| `kms` | Store each key as a customer KMS ciphertext file and reference `kms_provider`, `kms_key_id`, and `kms_ciphertext_file`. |
| `vault` | Write each key to the customer Vault path and reference `vault_address`, token file, mount, path, and field. |

Update `providers.json` with the production providers. Example:

```json
{
  "providers": [
    {
      "name": "anthropic",
      "base_url": "https://api.anthropic.com",
      "models": ["claude-3-5-sonnet-latest"],
      "key_source": {
        "type": "local_file",
        "path": "/run/secrets/anthropic_api_key"
      }
    },
    {
      "name": "openai",
      "base_url": "https://api.openai.com",
      "models": ["gpt-4.1"],
      "key_source": {
        "type": "kms",
        "kms_provider": "aws",
        "kms_key_id": "arn:aws:kms:us-east-1:111122223333:key/example",
        "kms_ciphertext_file": "/run/secrets/openai_api_key.kms"
      }
    }
  ]
}
```

Restart or reload the proxy according to the customer's deployment
system, then confirm the next heartbeat reports the expected
`provider_count`.

## 5. Cut Traffic to Tier 3

Register the proxy URL and certificate material for the tenant in
OmniSight. After registration, send one low-risk LLM request through
each provider and confirm:

- The SaaS request path uses the proxy URL, not direct provider egress.
- mTLS succeeds with the pinned customer certificate.
- Signed nonce verification succeeds and replayed nonces are rejected.
- Streaming responses reach the user without buffering the whole body.
- Customer proxy audit logs include full prompt and response.
- OmniSight audit metadata contains only time, provider, model, status,
  and token counts; no prompt or response payload appears in SaaS logs.

If the proxy is unreachable or the mTLS handshake fails, stop the
migration and keep the tenant on Tier 2 until the customer fixes the
proxy. Do not silently fall back to direct provider egress during a
Tier 3 cutover.

## 6. Clear OmniSight Key Material

After the customer signs off that proxy import and test traffic passed,
clear SaaS-side provider key material for the tenant.

Required evidence before clearing:

| Evidence | Acceptance |
|---|---|
| Proxy health | Latest health response is connected and non-stale |
| Provider smoke | One successful proxied request per migrated provider |
| Audit split | Customer log has full payload; OmniSight has metadata only |
| Export receipt | Customer confirms import bundle SHA-256 and import timestamp |

Clear every migrated Tier 2 credential row. OmniSight no longer stores
encrypted provider keys or wrapped key material for those providers
after this step. Then run a sampled decrypt check against the old Tier
2 credential ids; the expected result is failure because the key
material has been purged.

Record the purge in the customer ticket:

```text
tenant=t-acme
proxy_id=proxy-acme-prod
cleared_provider_count=<n>
cleared_credential_ids=<fingerprint-only-list>
sampled_decrypt=failed_as_expected
operator=<omnisight-operator>
```

## 7. Rollback Boundary

Before Section 6 completes, rollback is allowed by disabling proxy mode
for the tenant and keeping the Tier 2 CMEK credentials active.

After Section 6 completes, Tier 3 is a zero-trust exit boundary:
OmniSight must not recreate provider keys from backups or downgrade the
tenant automatically. A customer who wants to leave Tier 3 must run a
new onboarding flow and provide fresh provider credentials through the
target tier.

## 8. Completion Checklist

- [ ] Customer proxy deployed with mTLS and signed nonce enabled.
- [ ] Heartbeat shows `connected=true` and `stale=false`.
- [ ] OmniSight export bundle is sealed to customer migration key.
- [ ] Customer import completed into `local_file`, `kms`, or `vault`.
- [ ] One proxied request per provider succeeded.
- [ ] Customer audit log contains full prompt and response.
- [ ] OmniSight audit metadata contains no prompt or response.
- [ ] SaaS-side provider key material cleared.
- [ ] Sampled decrypt of old Tier 2 credential ids fails as expected.
- [ ] Customer ticket records export SHA-256, import timestamp, purge
      timestamp, and proxy id.
