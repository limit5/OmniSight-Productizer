# Email Domain Authentication - Operator Runbook

> Date: 2026-05-03
> Scope: FS.4 Email Service domain authentication for
> **Resend / Postmark / AWS SES** transactional email senders. This
> document covers DNS setup only: SPF, DKIM, DMARC, and provider
> return-path / MAIL FROM alignment. API credentials, templates, and
> bounce / complaint webhooks are covered by FS.4.1-FS.4.3.

Provider references:
* Resend domain verification:
  <https://resend.com/docs/knowledge-base/what-if-my-domain-is-not-verifying>
* Postmark domain verification:
  <https://postmarkapp.com/support/article/1046-how-do-i-verify-a-domain>
* AWS SES identities:
  <https://docs.aws.amazon.com/ses/latest/dg/creating-identities.html>
* AWS SES authentication:
  <https://docs.aws.amazon.com/ses/latest/dg/configure-identities.html>

---

## TL;DR

| Task | Required record | Owner |
|---|---|---|
| Prove the sender domain is controlled by OmniSight ops | Provider DKIM TXT/CNAME records | DNS admin |
| Make SPF DMARC-aligned | Provider return-path / MAIL FROM subdomain | DNS admin + provider console |
| Publish receiver policy | `_dmarc.<sender-domain>` TXT | DNS admin |
| Confirm production readiness | `dig` + provider dashboard + test email headers | Operator |

Do not add provider API keys to DNS records or this file. DNS records
must contain only the hostnames / values shown by the provider console.

---

## 1. Naming Policy

Use one dedicated sender domain or subdomain for OmniSight
transactional email:

```text
mail.example.com        # recommended sender domain
bounces.mail.example.com # return-path / MAIL FROM subdomain
```

Rules:

1. Keep the visible `From:` domain stable across providers. Example:
   `OmniSight <no-reply@mail.example.com>`.
2. Use a provider-owned return-path / MAIL FROM subdomain for SPF
   alignment. Example: `bounces.mail.example.com`.
3. Do not mix bulk marketing traffic into the same sender domain.
4. Keep each provider's DKIM records exactly as generated; do not
   normalize underscores, selector names, or trailing dots unless the
   DNS host requires that UI-specific form.

---

## 2. Baseline DMARC

Create DMARC after at least one provider has verified DKIM. Start in
monitoring mode, then tighten after 7-14 clean days of aggregate
reports.

### Monitoring mode

| Field | Value |
|---|---|
| Type | `TXT` |
| Name | `_dmarc.mail.example.com` |
| Value | `v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com; adkim=s; aspf=s; fo=1` |

### Quarantine mode

```text
v=DMARC1; p=quarantine; pct=25; rua=mailto:dmarc-reports@example.com; adkim=s; aspf=s; fo=1
```

Increase `pct` gradually (`25` -> `50` -> `100`) after reports show
only expected senders.

### Reject mode

```text
v=DMARC1; p=reject; rua=mailto:dmarc-reports@example.com; adkim=s; aspf=s; fo=1
```

Only move to `p=reject` after all active providers have passing DKIM
and at least one aligned SPF or DKIM path. DMARC passes when either
SPF or DKIM passes and aligns with the visible `From:` domain; this
runbook still configures both so provider failover does not degrade
delivery.

---

## 3. Resend

### 3.1 Provider console

1. Open Resend dashboard -> **Domains** -> **Add Domain**.
2. Add the chosen sender domain, e.g. `mail.example.com`.
3. Copy every generated DNS record from Resend.
4. If Resend shows a return-path / MX record, use the configured
   return-path subdomain, e.g. `bounces.mail.example.com`.

### 3.2 DNS records

Add the records exactly as Resend prints them. The record shapes are:

| Purpose | Type | Name | Value |
|---|---|---|---|
| DKIM | `TXT` or `CNAME` | Provider-generated selector under `mail.example.com` | Provider-generated value |
| SPF / return-path | `MX` | `bounces.mail.example.com` | Provider-generated mail exchanger |
| SPF / return-path | `TXT` | `bounces.mail.example.com` | Provider-generated SPF TXT |

If the domain already has an SPF TXT record at the same name, merge
mechanisms into one TXT value. Do not publish two separate `v=spf1`
records at the same DNS name.

### 3.3 Verify

```bash
dig +short TXT <resend-dkim-host>
dig +short MX bounces.mail.example.com
dig +short TXT bounces.mail.example.com
dig +short TXT _dmarc.mail.example.com
```

Then click **Restart verification** / verify in the Resend dashboard.
Send a test email and inspect headers:

```text
dkim=pass header.d=mail.example.com
dmarc=pass header.from=mail.example.com
```

---

## 4. Postmark

### 4.1 Provider console

1. Open Postmark -> **Sender Signatures** -> **Domains** -> **Add
   Domain**.
2. Add the sender domain, e.g. `mail.example.com`.
3. Copy the DKIM record and verify it.
4. Configure the custom Return-Path domain, e.g.
   `bounces.mail.example.com`, so Postmark's envelope sender aligns
   for SPF / DMARC.

### 4.2 DNS records

Add the records exactly as Postmark prints them. The record shapes are:

| Purpose | Type | Name | Value |
|---|---|---|---|
| DKIM | `TXT` | Provider-generated selector under `mail.example.com` | Provider-generated public key |
| Custom Return-Path | `CNAME` | `bounces.mail.example.com` | Provider-generated Postmark return-path target |
| DMARC | `TXT` | `_dmarc.mail.example.com` | See section 2 |

Postmark may also show SPF verification for older sender-signature
flows. Prefer the custom Return-Path path for SPF alignment; keep only
one `v=spf1` TXT record per DNS name if a TXT record is required.

### 4.3 Verify

```bash
dig +short TXT <postmark-dkim-host>
dig +short CNAME bounces.mail.example.com
dig +short TXT _dmarc.mail.example.com
```

Then click Postmark's DKIM / Return-Path / DMARC verification controls.
Send a test email and inspect headers:

```text
dkim=pass header.d=mail.example.com
spf=pass smtp.mailfrom=bounces.mail.example.com
dmarc=pass header.from=mail.example.com
```

---

## 5. AWS SES

### 5.1 Provider console

1. Open AWS SES in the production sending Region.
2. Go to **Configuration** -> **Identities** -> **Create identity**.
3. Choose **Domain** and add the sender domain, e.g.
   `mail.example.com`.
4. Enable Easy DKIM and publish every generated CNAME record.
5. Configure a custom MAIL FROM domain, e.g.
   `bounces.mail.example.com`.

SES identities are Region-scoped. Repeat identity creation and DNS
verification for every Region used by the production adapter.

### 5.2 DNS records

Add the records exactly as SES prints them. The record shapes are:

| Purpose | Type | Name | Value |
|---|---|---|---|
| Easy DKIM | `CNAME` | SES-generated DKIM selector under `mail.example.com` | SES-generated `dkim.amazonses.com` target |
| Custom MAIL FROM MX | `MX` | `bounces.mail.example.com` | SES-generated feedback SMTP host |
| Custom MAIL FROM SPF | `TXT` | `bounces.mail.example.com` | `v=spf1 include:amazonses.com -all` |
| DMARC | `TXT` | `_dmarc.mail.example.com` | See section 2 |

Do not put the SES SPF include on the root sender domain unless that
DNS name is also used as the envelope MAIL FROM. For DMARC SPF
alignment, the MAIL FROM domain must align with the visible `From:`
domain.

### 5.3 Verify

```bash
dig +short CNAME <ses-dkim-selector>._domainkey.mail.example.com
dig +short MX bounces.mail.example.com
dig +short TXT bounces.mail.example.com
dig +short TXT _dmarc.mail.example.com
```

Wait until SES shows the identity and custom MAIL FROM domain as
verified. Send a test email and inspect headers:

```text
dkim=pass header.d=mail.example.com
spf=pass smtp.mailfrom=bounces.mail.example.com
dmarc=pass header.from=mail.example.com
```

---

## 6. Cutover Checklist

Before changing the active email provider / sender configuration in
production:

- [ ] DKIM is verified in the selected provider console.
- [ ] Return-Path / MAIL FROM is verified in the selected provider
      console.
- [ ] `_dmarc.<sender-domain>` exists and starts at `p=none`.
- [ ] `dig` returns the expected records from a public resolver.
- [ ] One test email to Gmail or another external mailbox shows
      `dkim=pass` and `dmarc=pass`.
- [ ] FS.4.3 webhook endpoint is configured for bounce / complaint
      feedback before sustained traffic.

Production smoke:

```bash
dig @1.1.1.1 +short TXT _dmarc.mail.example.com
dig @8.8.8.8 +short TXT _dmarc.mail.example.com
```

If either resolver returns stale data, wait for DNS TTL before sending
production traffic.

---

## 7. Rollback

DNS rollback is slow because of TTLs. Prefer provider-level rollback:

1. Disable the new provider's sending key or remove it from the active
   email provider configuration.
2. Route traffic back to the previously verified provider / sender
   domain.
3. Leave the new DKIM / return-path DNS records in place until the
   incident is closed; removing records during TTL propagation makes
   diagnosis harder.
4. Keep DMARC at `p=none` until the sender set is stable again.

Emergency DNS rollback:

```bash
dig +trace TXT _dmarc.mail.example.com
dig +trace CNAME <provider-dkim-host>
dig +trace MX bounces.mail.example.com
```

Use `dig +trace` output to identify whether the authoritative DNS host
or a recursive resolver is stale before editing records again.
