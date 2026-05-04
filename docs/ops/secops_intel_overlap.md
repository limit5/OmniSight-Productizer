# SecOps Intel Overlap Contract (BP.I.4)

> Scope: merge the overlapping responsibilities between BP.I SecOps
> Threat Intel, N2 Renovate vulnerability handling, and S2-8 GitHub
> Secret Scanning without creating duplicate bots or duplicate PRs.

## Ownership

| Surface | Owner | What it does | What it must not do |
|---|---|---|---|
| BP.I SecOps Intel | `backend/secops_intel.py` + `backend/secops_intel_hooks.py` | Pre-decision threat context for dependency, platform, blueprint, and security-review prompts | Open dependency fix PRs, mutate lockfiles, store secret values, or replace GitHub repo settings |
| N2 Renovate | `renovate.json` + `docs/ops/renovate_policy.md` | Dependency update PRs, vulnerability fast-path PRs, grouped lockfile regeneration, tiered auto-merge | Re-render Intel briefs or make architecture decisions |
| S2-8 GitHub Secret Scanning | GitHub repo settings | Push-time secret detection and provider-backed alerting | Replace diff-time review or store leaked secret material in repo artefacts |

The rule is intentionally asymmetric: BP.I can enrich decisions with
source-backed intelligence, but N2 and S2-8 remain the systems of record
for dependency remediation and pushed-secret alerts.

## Dependency CVE Flow

1. BP.I pre-install and pre-blueprint hooks may call
   `search_latest_cve()` and `query_zero_day_feeds()` before an agent
   chooses a new dependency or architecture component.
2. If the package is already tracked in `backend/requirements.in`,
   `package.json`, or a lockfile, remediation belongs to N2 Renovate.
   The Intel brief should say that the fix must flow through the
   Renovate vulnerability fast-path.
3. If the package is not yet tracked, BP.I may recommend avoiding,
   pinning, or replacing the candidate before it enters the repo.
4. BP.I must not open a competing fix PR or regenerate lockfiles. That
   would race Renovate and break the single-source lockfile policy.

## Secret Leak Flow

1. Diff-time detection stays with the Security Engineer role. It flags
   suspicious values before the patch lands. It never quotes the secret
   value in comments, logs, HANDOFF, or generated briefs.
2. S2-8 GitHub Secret Scanning is the push-time backstop. Operators must
   enable it under GitHub Code security settings alongside Dependabot
   alerts.
3. BP.I may add secret-scanning posture to a SecOps brief, but it only
   names the control and the affected path or pattern class. It does not
   persist the leaked value or call out a raw token.
4. If a real secret has reached the remote, the required action is
   revoke and rotate first; code cleanup and evidence updates follow.

## Operator Checklist

- GitHub Secret Scanning: on.
- Dependabot alerts: on, so Renovate can receive GitHub advisory signals.
- Renovate App installed and `Allow auto-merge` enabled per N2.
- CODEOWNERS present enough for Renovate minor/major review routing.
- Security Engineer role remains the first diff-time secret scanner.

## Cross-References

- `docs/ops/renovate_policy.md` owns N2 group, tier, and vulnerability
  fast-path policy.
- `configs/roles/security-engineer.md` owns diff-time appsec review and
  the S2-8 secret-scanning handoff.
- `backend/secops_intel_hooks.py` owns the current passive BP.I hook
  entry points. They remain non-blocking until a later task explicitly
  changes that contract.
