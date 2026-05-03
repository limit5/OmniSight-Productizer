# KS.4.6 - Incident Response Runbook

> Status: active SOP
> Scope: first 24 hours of security incident response: detect, contain,
> rotate, notify customer, preserve forensics, and run blameless postmortem.
> Evidence: private security evidence vault; do not commit raw incident
> artefacts, customer data, secrets, or exploit payloads.

This runbook gives the incident commander one 24-hour operating sequence
for security incidents. It is intentionally procedural: assign owners,
stop the exposure, rotate affected credentials, notify impacted
customers when required, preserve evidence, and close with a blameless
postmortem.

## 1. Severity and roles

Declare an incident when any signal suggests active exploitation,
credential compromise, production data exposure, cross-tenant access,
unauthorized privileged action, or security-control bypass.

| Role | Owner | Responsibility |
|---|---|---|
| Incident commander | Security owner or delegated on-call lead | Own timeline, severity, decisions, customer notification gate |
| Engineering owner | Backend / infrastructure owner for affected surface | Containment, fixes, deploys, rollback, credential rotation |
| Forensics owner | Security engineer or delegated operator | Evidence preservation, log export, hash inventory, chain-of-custody |
| Communications owner | Customer success / legal-approved delegate | Customer-facing notices, status page, support macros |
| Scribe | Any non-blocking responder | Timestamped notes, decisions, owners, links to private evidence |

Use the highest credible severity until evidence narrows the scope.

| Severity | Trigger | Response target |
|---|---|---|
| SEV-1 | Confirmed active exploitation, cross-tenant data access, production secret leak, or customer data exposure | Page immediately; containment decision within 30 minutes |
| SEV-2 | Credible exploit path, high-risk auth bypass, privileged account compromise without confirmed customer impact | Page security + engineering; containment decision within 2 hours |
| SEV-3 | Suspicious activity, scanner report, bug bounty / pentest critical lead without active exploitation | Triage same business day; escalate if evidence grows |

## 2. First 24-hour timeline

### 0-15 minutes - detect and declare

1. Open an incident ticket and start a timestamped timeline.
2. Assign incident commander, engineering owner, forensics owner,
   communications owner, and scribe.
3. Record the first signal source: alert, customer report, audit query,
   bug bounty report, pentest vendor call, CI secret scanner, or operator
   observation.
4. Classify initial severity and affected surfaces: tenant, user,
   integration, secret type, endpoint, host, deployment, and time window.
5. Freeze unrelated deploys for affected services until the incident
   commander releases the hold.

Do not paste raw secrets, customer data, exploit payloads, private logs,
or screenshots into git, chat channels with broad membership, or public
issue trackers.

### 15-60 minutes - contain

Containment takes priority over perfect root cause.

| Incident shape | Minimum containment action |
|---|---|
| Leaked API key, OAuth token, webhook secret, bearer token, or database URL | Disable or revoke the credential; block known attacker source if useful; rotate dependent sessions |
| Cross-tenant access or authz bypass | Disable affected route / feature flag, block risky tenant action, or deploy deny-list guard |
| Compromised account | Revoke sessions, force password reset, require MFA reset, suspend API keys owned by the account |
| Malicious integration / webhook | Disable integration, rotate webhook secret, reject inbound delivery source until validated |
| Host or container compromise | Isolate host from load balancer, snapshot disk for forensics, stop workload only after volatile evidence is captured when feasible |
| LLM prompt injection causing tool misuse | Disable affected tool route or agent invocation path, preserve prompt / tool-call hashes, add temporary allowlist / blocklist gate |

The engineering owner records each containment action in the incident
ticket with timestamp, actor, command or PR link, and expected customer
impact. If containment requires customer-visible downtime, the incident
commander approves the tradeoff and the communications owner prepares a
customer notice.

### 1-4 hours - rotate and verify

Rotate every credential in the plausible blast radius, not only the
credential proven compromised.

| Secret / identity | Rotation action | Verification |
|---|---|---|
| User sessions | Revoke affected sessions; force re-authentication | Audit query shows old sessions invalid or rotated |
| Password / MFA | Force password reset and MFA re-enrollment for affected users | Login works only after reset; old factors rejected |
| API keys / service tokens | Revoke old key, issue new key, update dependent service config | Old key returns unauthorized; new key passes smoke |
| OAuth refresh tokens | Revoke provider grant where supported; reconnect integration | Provider dashboard and OmniSight audit show token rotation |
| Webhook secrets | Replace signing secret and reject old signature after grace window | Replay with old signature fails; new delivery verifies |
| Database / queue credentials | Rotate application secret and restart affected services | Health checks green; old credential cannot connect |
| Cloud / deploy credentials | Rotate provider token, reduce scope if needed, review recent audit trail | Provider audit trail has no new unauthorized calls |

After each rotation, run the smallest smoke test that proves the new
credential is active and the old one is rejected. Store only
fingerprints, audit row ids, command transcripts with secrets redacted,
and evidence vault references in the incident ticket.

### 4-8 hours - notify customer decision

Customer notification is required when the incident commander and legal /
communications owner determine that customer data, tenant isolation,
availability, or customer-controlled credentials were affected or
reasonably likely affected.

Minimum notification workflow:

1. Identify impacted tenants, users, integrations, data categories, and
   earliest/latest exposure time.
2. Draft notice with what happened, what was affected, what OmniSight
   did, what the customer must do, and when the next update will arrive.
3. Exclude exploit details that would enable unfixed variants.
4. Obtain incident commander and legal / communications approval.
5. Send notices through approved customer channels and record delivery
   timestamp in the incident ticket.
6. If impact is still uncertain, send a holding notice only when delay
   would increase customer risk.

Default customer-update cadence for open SEV-1/SEV-2 incidents is every
4 hours until containment is confirmed, then daily until closure or a
different cadence is approved.

### 8-24 hours - forensics and recovery

Forensics must preserve evidence without expanding the breach.

1. Export relevant audit logs, application logs, deployment events,
   provider audit trails, database access logs, and alert history into
   the private security evidence vault.
2. Compute SHA-256 fingerprints for exported artefacts and record only
   fingerprints plus vault references in the incident ticket.
3. Preserve affected container / host snapshots when host compromise is
   credible. Mark snapshots read-only where the platform supports it.
4. Build the incident timeline from first suspicious signal through
   containment, rotation, notification, recovery, and monitoring.
5. Query for blast radius across tenants, users, integrations, secrets,
   sessions, tool invocations, and deployment changes.
6. Keep raw customer data, plaintext credentials, exploit payloads, and
   private screenshots out of git and broad chat channels.
7. Re-enable disabled services only after containment is verified,
   rotated credentials are active, smoke tests pass, and monitoring is
   clean.

The incident commander may downgrade severity after containment and
blast-radius evidence are documented. Do not close the incident during
the first 24 hours unless customer impact is ruled out, all rotations are
verified, and the forensics owner has enough evidence for postmortem.

## 3. Blameless postmortem

Schedule the postmortem within 5 business days of containment. The
incident commander owns completion; the scribe may draft.

Postmortem template:

```markdown
# Incident <ID> - <short title>

## Summary
- Severity:
- Start / detect / contain / resolve timestamps:
- Impacted tenants / users / services:
- Customer notification required: yes/no

## Timeline
| Time (UTC) | Event | Evidence |
|---|---|---|

## Root cause
- Technical cause:
- Detection gap:
- Control gap:

## What worked
- ...

## What failed
- ...

## Corrective actions
| Owner | Action | Due date | Verification |
|---|---|---|---|

## Customer communications
- Notice sent:
- Follow-up needed:

## Evidence
- Vault references and SHA-256 fingerprints only.
```

Blameless means the document identifies system conditions, missed
signals, unclear ownership, and missing controls.
The postmortem must not name individuals as root cause.
Corrective actions need owners, due dates, and verification criteria.

## 4. Evidence checklist

- [ ] Incident ticket opened with severity, owner roster, and timeline
- [ ] Affected tenants, users, integrations, endpoints, and time window
      recorded
- [ ] Containment actions recorded with timestamps and owners
- [ ] Credential / session rotations completed and old credentials
      verified rejected
- [ ] Customer notification decision recorded with approval owner
- [ ] Customer notices sent when required
- [ ] Audit logs, application logs, provider trails, and snapshots stored
      in private security evidence vault
- [ ] SHA-256 fingerprints recorded for exported evidence
- [ ] Raw secrets, customer data, exploit payloads, and private
      screenshots excluded from git
- [ ] Recovery smoke tests passed after containment
- [ ] Postmortem scheduled within 5 business days
- [ ] Corrective actions assigned with due dates

## 5. Production status

This SOP does not deploy runtime code. Production readiness is
operational: the runbook is active when on-call responders can access it,
the private evidence vault exists, and incident commander / security /
engineering / communications roles are assigned for the current rotation.
