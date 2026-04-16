# O10 — Security Hardening Runbook (#273)

> Status: shipped
> Scope: queue / dist_lock / JIRA token / worker identity / merger-agent-bot
> Related: `backend/security_hardening.py`, `.gerrit/project.config.example`

O10 closes the five security gaps that were left open when we shipped
the distributed orchestration plane (O0–O9).  Everything lives in one
inspectable module (`backend/security_hardening.py`) so an auditor can
read a single file and understand the posture.

## 1. Queue payload HMAC + TLS

**Threat:** a worker pulls a forged CATC from Redis and executes it
(attacker gets arbitrary-code exec on a sandbox host).

**Defence:**
* TLS transport — use `rediss://` / `amqps://` URLs.  Boot refuses to
  start when `OMNISIGHT_ENV=production` and the queue URL is
  plaintext (`assert_production_queue_tls`).
* HMAC envelope — orchestrator signs every CATC via
  `sign_envelope(payload, key)` before pushing; worker's `handle()`
  calls `queue_backend.verify_pulled_message(msg)` and DLQs any
  message whose signature is missing / stale / wrong.

**Deploy:**
```bash
# 1. Mint a long random secret, 32+ bytes.
openssl rand -hex 32 > /opt/omnisight/queue-hmac.key

# 2. Export on every orchestrator + worker pod.
export OMNISIGHT_QUEUE_HMAC_KEY="$(cat /opt/omnisight/queue-hmac.key)"
export OMNISIGHT_QUEUE_HMAC_KEY_ID="k1"

# 3. Rotate: bump KEY_ID, roll orchestrators first (now pushing kN+1),
#    then roll workers (now accepting kN+1).  Messages signed kN
#    still flow until their TTL expires; old workers still verify
#    kN until their heartbeat goes stale.  Re-enqueue nothing.
```

**What if the HMAC env var is unset?** `verify_pulled_message` falls
back to no-op behaviour — compatible with pre-O10 deployments but
gives up the defence.  A production boot-time validator will warn if
`OMNISIGHT_QUEUE_HMAC_KEY` is unset.

## 2. Redis ACL roles

**Threat:** orchestrator account compromise leaks cluster-admin; any
worker can FLUSHALL Redis and kill the orchestration plane.

**Defence:** three least-privilege principals.

| role | can do | cannot do |
| --- | --- | --- |
| `omnisight-orchestrator` | push / pull / lock / sweep on `omnisight:*` | `FLUSHDB`, `CONFIG`, `CLUSTER`, `ACL` |
| `omnisight-worker` | `XREADGROUP`, `XACK`, `ZADD` lock extend, own-key `SET/DEL` | `XGROUP`, `XTRIM`, `FLUSHDB`, etc. |
| `omnisight-observer` | read-only (`+@read`) | any write verb |

**Deploy:**
```bash
# Generate users.acl from our Python source of truth.
python -m backend.security_hardening render-acl > /etc/redis/users.acl

# Redis config:
#   aclfile /etc/redis/users.acl
#   # (remove the `default` user's `+@all nopass`)
redis-cli ACL LOAD

# Set passwords (replace with your own):
redis-cli ACL SETUSER omnisight-orchestrator \
    ">$(openssl rand -hex 32)" on
```

Verify via `redis-cli ACL WHOAMI` inside each pod — if a worker pod
reports `omnisight-orchestrator`, someone gave it the wrong creds.

## 3. Worker attestation

**Threat:** attacker spins up a new worker, registers under an
existing tenant's id, and starts pulling that tenant's CATCs.

**Defence:** each worker ships with a `WorkerIdentity` containing:
* `worker_id`
* `tenant_id`
* `capabilities`
* `tls_cert_fingerprint` (hex SHA-256 of the worker's leaf cert)
* `pre_shared_key` (symmetric secret, never on the wire)

On boot, the worker presents `issue_attestation(identity)` as a JSON
blob to the orchestrator.  The orchestrator's `AttestationVerifier`
rejects the registration unless:

1. `worker_id` is in the allowlist.
2. `tenant_id` matches what was provisioned.
3. `tls_cert_fingerprint` matches.
4. `capabilities` list hasn't been tampered.
5. HMAC signature under the PSK verifies.
6. `iat` is within TTL (5 min default).
7. The nonce hasn't been seen recently (replay defence).

**Deploy:** drop the allowlist into the orchestrator env:
```yaml
workers:
  - worker_id: w-fw-01
    tenant_id: t-acme
    capabilities: [firmware, vision]
    tls_cert_fingerprint: 7c:f3:...
    pre_shared_key_ref: vault://kv/omnisight/workers/w-fw-01/psk
```

## 4. JIRA token at-rest encryption

**Threat:** JIRA PAT leaks via `settings.notification_jira_token`
plaintext in env / config dumps / error logs.

**Defence:** token is encrypted by `backend.secret_store` (Fernet),
loaded from `OMNISIGHT_JIRA_TOKEN_CIPHERTEXT`.  Only the last 4 chars
(fingerprint) appear in logs + integration-status endpoint.

**Migrate a running deployment:**
```bash
# 1. Encrypt the existing PAT.
python -c "from backend import secret_store; \
    print(secret_store.encrypt('jira-pat-abcdef'))"
# → gAAAAA…

# 2. Set the ciphertext env; unset the plaintext.
export OMNISIGHT_JIRA_TOKEN_CIPHERTEXT="gAAAAA..."
unset OMNISIGHT_NOTIFICATION_JIRA_TOKEN

# 3. Verify:
python -c "from backend.jira_adapter import describe_jira_token; \
    print(describe_jira_token())"
# → {'configured': True, 'source': 'encrypted', 'fingerprint': '…cdef'}
```

If plaintext token is detected, a warning is logged so operators know
to migrate.

## 5. Merger bot least-privilege + hash-chain audit

**Threat:** the `merger-agent-bot` account picks up Submit / Push Force
/ Delete Change / project-admin rights somewhere downstream, and
skirts the dual-+2 hard gate.

**Defence A — deny-by-default in `project.config`:**
`.gerrit/project.config.example` explicitly DENIES every dangerous
verb to the `ai-reviewer-bots` group.  CI enforces this with
`verify_merger_least_privilege`:

```bash
python -m backend.security_hardening verify-gerrit-config
# → OK or VIOLATIONS list (exit 1)
```

**Defence B — hash-chain audit** (`MergerVoteAuditChain`):
every merger vote (`+2` / abstain / refuse) appends to a SHA-256-
chained log.  Any insertion / mutation breaks `chain.verify()` at the
first bad row.  The chain is dual-sinked:
1. `backend.audit` tenant log (durable, SQLite).
2. In-process `get_global_merger_chain()` (process-local, for the UI
   + tests to inspect head without DB round-trip).

## 6. Penetration test matrix

`backend/tests/test_o10_pentests.py` encodes the 5 scenarios from the
TODO list as pass/fail tests:

| scenario | test class | asserts |
| --- | --- | --- |
| 偽造 CATC | `TestScenarioForgedCatc` | unsigned / tampered / wrong-key / replayed message rejected |
| 竊取鎖 | `TestScenarioLockTheft` | release/extend on someone else's task_id fails; TTL prevents squatting |
| 注入 merger prompt | `TestScenarioMergerPromptInjection` | security path, oversized, new-logic gates all refuse vote |
| worker 偽裝 | `TestScenarioWorkerSpoofing` | PSK/tenant/TLS/capability/nonce tamper all raise `AttestationError` |
| 偽冒 merger-agent-bot 投票 | `TestScenarioForgedMergerVote` | chain tamper detected, config escalation flagged, submit-rule still demands human |

Run:
```bash
pytest backend/tests/test_o10_pentests.py backend/tests/test_security_hardening.py -v
```

## 7. Incident playbook

* **HMAC key leaked** → rotate (`OMNISIGHT_QUEUE_HMAC_KEY_ID=k2`,
  roll orchestrator+workers).  Old-key messages die via TTL.
* **PSK leaked** → reissue worker identity; bump `pre_shared_key`
  field in the allowlist; attacker's nonces will no longer verify
  after old workers drain.
* **JIRA token rotation** → re-encrypt (`secret_store.encrypt`), swap
  `OMNISIGHT_JIRA_TOKEN_CIPHERTEXT`, hot-reload.
* **Merger vote audit reports tamper** → freeze merger
  (`OMNISIGHT_MERGER_ENABLED=false`), run `audit verify --tenant …`,
  diff the broken row vs Gerrit's own vote log.  Rebuild chain only
  after root-cause review.
* **Gerrit config drift** → CI catches drift pre-merge; for emergency
  server-side audit, `python -m backend.security_hardening
  verify-gerrit-config /path/to/project.config`.
