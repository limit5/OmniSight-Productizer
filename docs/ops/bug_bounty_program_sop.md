# KS.4.5 - Bug Bounty Program Evaluation SOP

> Status: GA-gated SOP
> Scope: HackerOne / Bugcrowd platform comparison, post-GA launch gates,
> payout policy, program scope, triage workflow, and N10 ledger evidence.
> Ledger: [`upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md)

This SOP keeps the bug bounty decision separate from the quarterly
third-party pentest cadence. Pentests remain scheduled point-in-time
assessments. Bug bounty starts only after GA, with a managed private
program first, and becomes public only after OmniSight proves triage,
remediation, and disclosure discipline under controlled volume.

## 1. Decision summary

Default recommendation: launch with a managed private bug bounty after
GA, then review public launch after 30 days of clean operations.

| Decision | Policy |
|---|---|
| Launch timing | Post-GA only; do not launch during beta, migration freeze, or active incident |
| Starting mode | Private / invite-only managed bug bounty |
| Initial provider | HackerOne if existing enterprise security workflow prefers HackerOne Response/Bounty; Bugcrowd if procurement prefers published VDP pricing and CrowdMatch-style researcher activation |
| Public launch gate | 30 calendar days with response targets met, no critical unowned finding, and security owner approval |
| Budget control | Pre-approved quarterly reward pool and per-finding maximums; no uncapped bounty table |
| Evidence | Full reports stay in platform / evidence vault; git stores only program metadata and summary rows |

This is an operational launch policy, not a runtime feature. It does not
deploy code or change production behavior.

## 2. Platform comparison

Evaluate HackerOne and Bugcrowd before signing the first annual term.
Use the table below as the minimum comparison matrix.

| Dimension | HackerOne | Bugcrowd | OmniSight default |
|---|---|---|---|
| Bug bounty product | HackerOne Bounty with managed program, triage services, disclosure guidelines, bounty tables, workflow automation, and integrations | Managed Bug Bounty with CrowdMatch researcher activation, managed triage, platform analytics, integrations, and engagement simulation | Either acceptable if managed triage and private launch are included |
| VDP path | HackerOne Response supports ISO 29147-style disclosure intake and can launch in controlled/private stages before public directory listing | Bugcrowd VDP has self-managed, basic, and fully managed options; Basic public pricing currently starts at $299/$999 per month for first-year paid-upfront VDP plans | Keep VDP separate from paid bounty if procurement wants a low-cost public intake channel before bounty |
| Triage | HackerOne Hai Triage combines AI assist with human analyst validation, duplicate/noise handling, severity ranking, and researcher communication | Bugcrowd managed triage validates and prioritizes findings, backed by its vulnerability intelligence graph and managed services | Require managed triage for first year; self-triage is not allowed at GA |
| Researcher selection | Supports trusted community, selected / verified researchers, private programs, and targeted testing | Supports trusted hacker activation and data-driven CrowdMatch targeting | Start private and invite only verified / high-signal researchers |
| Pricing transparency | Bounty pricing is quote-based; public pages describe platform capabilities, not fixed reward budget | Bug bounty pricing is quote-based; Bugcrowd publishes VDP Basic pricing but not managed bug bounty price | Require quote with platform fee, managed triage fee, reward pool handling, and overage terms |
| Integrations | In-platform automations and 30+ bidirectional integrations | Pre-built connectors, webhooks, APIs, and SDLC integration | Must integrate with the existing remediation tracker before launch |
| Disclosure posture | Platform supports disclosure guidelines, safe harbor, bounty tables, and response targets | Program brief defines targets, goals, scope, rewards, and review expectations | Use conservative disclosure timeline; no public disclosure before fix or security-owner approval |

The security owner records the final provider decision in the private
evidence vault, then appends one row to the N10 "Bug Bounty Programs"
table. If no provider is selected, record `Disposition = deferred` with
the blocking reason.

## 3. Post-GA launch gates

All gates must be true before the first researcher invitation:

1. GA release is complete and not in a rollback or incident window.
2. KS.4.4 quarterly pentest SOP exists and the next pentest is scheduled.
3. Incident response owner, security owner, and engineering owner are
   named in the private program profile.
4. Remediation tracker integration is active and tested with a dummy
   finding.
5. Reward budget is approved for the quarter.
6. Legal approves safe harbor, prohibited activity, data handling, and
   disclosure language.
7. Test accounts, tenant fixtures, and staging / production-equivalent
   target list are ready; no production customer data is provided.
8. Emergency stop contact and pause procedure are documented in the
   platform profile.
9. N10 ledger has a `planned` row under `## Bug Bounty Programs`.

Do not invite researchers until every gate has an owner and evidence
link in the private evidence vault. Do not use production customer
accounts as bounty test accounts.

## 4. Initial program scope

Start narrow. The first private program should cover externally
reachable GA surfaces where security impact is clear and remediation
ownership is known.

### In scope

- GA web application domains and API endpoints owned by OmniSight
- Authentication, session handling, MFA, account recovery, and invite
  flows
- Tenant isolation and authorization boundary failures
- Agent invocation paths exposed to user-facing input
- File upload, artifact download, webhook, and integration callback
  endpoints
- Security headers and CORS issues with demonstrable exploit impact
- Backup / export / audit evidence exposure paths that are externally
  reachable

### Out of scope

- Denial of service, stress testing, spam, social engineering, phishing,
  and physical attacks
- Attacks requiring access to production customer data or another
  tenant's real account
- Scanner-only findings without exploitability or security impact
- Missing best-practice headers without demonstrated impact
- Third-party services not operated by OmniSight unless explicitly
  listed in the platform scope table
- Internal-only development worktrees, local runner hosts, and
  `test_assets/`
- Previously reported findings still inside the remediation SLA

The scope table in the chosen platform is the source of truth for
researchers. This SOP is the internal baseline; the platform profile may
be narrower, but must not be broader without security owner approval.

## 5. Payout policy

Use a conservative table for the first private launch. Amounts are caps,
not automatic awards; final payout depends on impact, exploitability,
report quality, and duplicate status.

| Severity | Initial range (USD) | Examples |
|---|---:|---|
| Critical | 3,000 - 10,000 | Cross-tenant data access, remote code execution, auth bypass affecting many tenants |
| High | 1,000 - 3,000 | Privilege escalation, account takeover with realistic preconditions, sensitive data exposure |
| Medium | 300 - 1,000 | Limited authorization bypass, stored XSS with constrained impact, meaningful CSRF |
| Low | 100 - 300 | Low-impact information disclosure or security control weakness with limited exploitability |
| Informational | 0 | Best-practice note, duplicate, non-exploitable scanner output |

Budget controls:

- Security owner approves the quarterly reward pool before launch.
- Any single payout above USD 5,000 requires security owner and finance
  approval before award.
- Critical findings may exceed the range only with written approval in
  the private evidence vault.
- Do not promise bounties for out-of-scope, duplicate, or non-reproducible
  reports.
- Do not negotiate rewards outside the platform message thread.

## 6. Triage SOP

Managed triage handles initial validation, reproduction, duplicate
checks, severity recommendation, and researcher communication. OmniSight
remains accountable for business impact, remediation, and disclosure.

| Stage | Owner | SLA |
|---|---|---|
| Intake acknowledgement | Platform / managed triage | 2 business days |
| Validity and scope decision | Managed triage + security owner | 5 business days |
| Severity and owner assignment | Security owner | 2 business days after validation |
| Critical containment decision | Security owner + incident commander | Same day |
| Remediation ticket creation | Engineering owner | 2 business days after validation |
| Researcher update | Security owner or managed triage | Every 10 business days while open |
| Retest request | Engineering owner | Within 2 business days after fix deploy |
| Closure / bounty decision | Security owner | 5 business days after retest or accepted duplicate decision |

Critical reports trigger the incident response path if they indicate
active exploitation, production data exposure, credential compromise, or
cross-tenant access. In that case, open an incident ticket before
continuing normal bounty workflow.

## 7. Disclosure policy

The private program starts with coordinated disclosure only. Public
disclosure is allowed only after all of these are true:

1. The finding is fixed or explicitly risk-accepted.
2. Customer notification obligations, if any, are complete.
3. Security owner approves disclosure in the platform thread.
4. The write-up excludes customer data, secrets, internal hostnames,
   exploit chains for unfixed variants, and private evidence links.

Default disclosure hold is 90 days from validation unless the platform
profile or legal approval sets a stricter timeline. Researchers must
not publicly disclose before written approval.

## 8. N10 ledger row template

Append one row to `## Bug Bounty Programs` when the program is planned,
launched, paused, publicly launched, closed, or materially changed:

```markdown
| 2026-Q3 | HackerOne | private | planned | 10000 | <scope-sha256> | <tracker URL> | post-GA launch gates pending |
```

Append one row to `## Bug Bounty Findings` for each accepted valid
finding after triage:

```markdown
| 2026-07-15 | H1-123456 | high | <finding-sha256> | <ticket URL> | 1500 | retest-pending | no customer data in ledger |
```

Do not edit previous rows. If a row is wrong, add a correction row with
`correction -> <program/report/finding-id>` in Notes.

## 9. Evidence checklist

- [ ] Provider comparison completed and stored in evidence vault
- [ ] Platform quote / order form approved
- [ ] Program profile, scope table, safe harbor, and disclosure policy
      reviewed by legal
- [ ] Quarterly reward pool approved
- [ ] Remediation tracker integration tested
- [ ] Managed triage enabled
- [ ] Private researcher invite list approved
- [ ] N10 `Bug Bounty Programs` planned row appended
- [ ] Emergency pause procedure tested in the platform
- [ ] First 30-day private launch review scheduled

## 10. References

- HackerOne Bounty product page:
  <https://www.hackerone.com/product/bounty>
- HackerOne Response / VDP product page:
  <https://www.hackerone.com/product/response-vulnerability-disclosure-program>
- HackerOne product offerings help:
  <https://docs.hackerone.com/en/articles/8365279-product-offerings>
- HackerOne Response setup:
  <https://docs.hackerone.com/en/articles/8509950-response-program-setup>
- HackerOne triage:
  <https://www.hackerone.com/platform/triage>
- Bugcrowd Managed Bug Bounty:
  <https://www.bugcrowd.com/products/bug-bounty/>
- Bugcrowd VDP pricing:
  <https://www.bugcrowd.com/bugcrowd-pricing/vulnerability_disclosure/>
- Bugcrowd managed triage:
  <https://www.bugcrowd.com/products/platform/triage/>
- Bugcrowd bounty brief docs:
  <https://docs.bugcrowd.com/researchers/participating-in-program/reviewing-bounty-briefs/>

## 11. Production status

This SOP does not deploy runtime code. Production readiness is
operational: GA must complete first, then the security owner may execute
the private launch gates, invite researchers, and record program status
in the N10 ledger.
