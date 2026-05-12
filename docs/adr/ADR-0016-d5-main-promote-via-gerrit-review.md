# ADR-0016 — D5 develop→main promotion via Gerrit review change, not direct push

- **Status**: **Superseded by [ADR-0020](ADR-0020-release-cut-as-single-merge-change.md) (2026-05-12).** Retained for historical context. The `refs/for/main` *bulk-chain* mechanism this ADR specified (one Gerrit change per intervening `main..develop` commit) was fundamentally wrong for release-cut semantics — every release after the first hits Gerrit's "no new changes" rejection (the develop commits already carry Change-Ids) and/or `receive.maxBatchChanges`. ADR-0020 replaces it with a **single merge change** on `refs/for/main` + `submit-type: MERGE_ALWAYS` + a quad-keyed `release-cut-promote` conditional submit-requirement. The only piece carried forward is the `milestone:R3-fastforward` hashtag — ADR-0016's "path C" forward-compat hook is the seam ADR-0020 builds on. (Status flip done by AUDIT-26f / OP-985, which owns the AUDIT-13/13a/13b cleanup pass.)
- **Original status**: Accepted
- **Date**: 2026-05-12
- **Superseded**: 2026-05-12 by ADR-0020 (AUDIT-26 — OP-979)
- **Deciders**: operator (`nanakusa sora`), claude-bot (implementation), codex-bot (R3 failure evidence)
- **Tickets**: OP-960 (AUDIT-13 implementation), OP-925 (R3 failure that surfaced this), OP-958 (AUDIT-11), OP-959 (AUDIT-12); superseded by OP-982 (AUDIT-26c — ADR-0020) / OP-985 (AUDIT-26f — this status flip)

## Context

Sprint D's D5 step advances `refs/heads/main` to the current `develop` tip after the release milestone has been accepted. The original implementation (OP-766, shipped 2026-04-28) used a direct fast-forward push:

```bash
git push gerrit develop:main
```

This worked in pre-prod environments where bot accounts had elevated push rights on `refs/heads/main`. In production it does not: Gerrit's ACL correctly refuses any bot direct push to `refs/heads/main`. The `main` branch must advance through Gerrit Code Review — both for audit reasons (every transition to prod-shipped code is reviewable) and for safety (the bot can never bypass the review gate, even via a stolen SSH key).

The failure surfaced 2026-05-12 during the first live RELEASE-v0.5.0-rc1 R3 attempt (OP-925). Codex ran the D5 script eight times in 70 minutes; every attempt produced `release_audit` rows with `outcome=push_rejected`, while the runner kept reverting the ticket via the (then-broken) AUDIT-9 ops-only path. Three cascading bugs were filed: AUDIT-11 (ops-only label not honored), AUDIT-12 (Gerrit 3.13 query response shape), and **AUDIT-13 (this ADR's subject — the ACL refusal)**.

## Decision

D5 promotion goes through a **Gerrit review change**, not a direct push. The mechanism — implemented in `backend/agents/auto_promote_main.py` (OP-960, commit `3ad103fb`) — is:

```bash
git push -o hashtag=auto-promote \
         -o hashtag=milestone:R3-fastforward \
         -o topic=develop-to-main \
         gerrit develop:refs/for/main
```

Concretely:

| Knob | Value | Source |
|---|---|---|
| Destination ref | `refs/for/main` (Gerrit magic ref) | RFC: Gerrit creates one review change per commit between current `main` and `develop` tip |
| Hashtags | `auto-promote`, `milestone:R3-fastforward` | `PROMOTE_HASHTAGS` in `auto_promote_main.py` |
| Topic | `develop-to-main` | `PROMOTE_TOPIC` — groups all per-commit review changes in one Gerrit topic page |
| Batch ceiling | `receive.maxBatchChanges` (Gerrit default 10) | If `develop` is more than N commits ahead of `main`, the single push is rejected by Gerrit; `auto_promote_main` refuses with `BatchTooLarge` and emits an operator alert rather than letting `CalledProcessError` escape |
| Non-FF | Refused; alert | `main` having commits absent from `develop` indicates a manual override or split-brain; never auto-merged |

**What submits the change(s) is intentionally not this bot's job.** The submit step remains operator-gated by design (humans-in-the-loop). Three submit paths are in scope, sequenced by maturity:

| Path | Status | Mechanism |
|---|---|---|
| **A — Operator clicks "Submit" in Gerrit UI** | ✅ available today | Operator reviews the change, casts `Code-Review: +2`, clicks Submit. Same flow as any other Gerrit change. |
| **B — Operator clicks "Advance main now" in OmniSight web UI** | 🚧 Sprint H4 (OP-949 — 公開済み but only the JIRA-side replacement; the main-promote button is a follow-up) | Web UI surfaces the pending auto-promote topic; one-click submit through Gerrit API on operator's behalf. |
| **C — `merger-bot` casts +2 + auto-submit on `milestone:R3-fastforward` hashtag** | ⏳ deferred — `area:devops` follow-up | Gerrit conditional submit-requirement: `applicableIf: hashtag:milestone:R3-fastforward AND author:auto-promote-bot`, plus an explicit `submittableIf` clause that lets merger-bot's Code-Review: +2 count toward the submit gate when the hashtag is present. This is the same conditional-+2 pattern as O6 Merger Agent (OP-269), scoped strictly to develop→main fast-forward changes. |

The `milestone:R3-fastforward` hashtag is the **forward-compatibility hook** for path C. Even though path C is not built today, every auto-promote change carries the hashtag so the submit-rule can light up later without changing the push-side code.

## Alternatives considered

### Alt-1 — Adjust Gerrit ACL to permit bot direct push to `refs/heads/main`

**Rejected.** This was the original OP-766 model. It bypasses Code Review for the most security-critical branch. A stolen bot SSH key would let an attacker push arbitrary code to prod with no audit trail beyond the receive log. Even with branch protection rules + signed pushes, the principle "main moves only through review" is non-negotiable.

### Alt-2 — Pure operator-only flow (no bot involvement)

**Rejected as primary** (kept as fallback via path A above). Pure manual would mean the operator constructs and pushes the review change themselves — adds friction with no safety benefit, since the bot constructing the review change is verifiable and the submit step is still operator-gated.

### Alt-3 — Cherry-pick + submit via Gerrit's `merge` change type

**Rejected.** Gerrit's merge-change type creates a merge commit, which is wrong for a fast-forward semantic. We want `main` to point at the same commit as `develop` tip, not a synthetic merge commit. The `refs/for/main` magic-ref path naturally yields fast-forward changes when develop is strictly ahead.

### Alt-4 — Bot pushes a separate "release branch" that operator merges manually

**Rejected.** Adds a third branch (`release-staging/v0.5.0-rc1` or similar) and an extra merge step. The Gerrit review change *is* the staging artifact; no second branch is necessary.

## Consequences

### Operational

- **R8 in the RELEASE chain is no longer the only human gate.** R3 now also waits on an operator submit in Gerrit. For v0.5.0-rc1 (and any release until path B or C ships), expect two human touches per release: (a) submit the develop→main auto-promote change after R3 runs, and (b) approve at R8.
- **OP-925 will reach 公開済み via AUDIT-9's ops-only forward-transition** (CLI exit 0 + 0 commits + `runner:no-commits-expected` label). The actual main advancement is decoupled — OP-925 going green does not mean main moved. Downstream R4 (image build) assumes main has advanced; operators should aim to submit the auto-promote change immediately after R3 closes, before R4 starts.
- **R4 risk mitigation**: a runner alert fires if R4 picks up while main is still behind develop. If you want belt-and-suspenders, file a follow-up ticket to add a pre-pickup check on R4 that rejects pickup until `git ls-remote origin refs/heads/main` equals develop.

### Schema / API lock-in

- `PROMOTE_HASHTAGS = ("auto-promote", "milestone:R3-fastforward")` is part of the contract. The future Gerrit submit-requirement (path C) keys off `milestone:R3-fastforward`. Changing the hashtag breaks path C downstream.
- `PROMOTE_TOPIC = "develop-to-main"` groups per-commit changes in the Gerrit UI. The web UI in path B will list "Pending auto-promotes" by querying this topic — also part of the contract.
- `backend/agents/auto_promote_main.PushOutcome` is the structured result type. Callers (the D5 cron script, future web UI, future merger-bot submit-rule) all key off its fields. Schema-stable as of OP-960.

### Audit

- Every push attempt writes a row to `release_audit` with `outcome ∈ {success, push_rejected, non_ff_refused, batch_too_large, ...}`. Path C, when it lands, will add `outcome=auto_submitted`.
- Receive-log on Gerrit (`refs/for/main` push) is the second audit trail. Together with `release_audit` they form the "did main really move and why" forensic record.

## Out-of-scope follow-ups

Captured as separate tickets per the OP-960 scope note ("this ticket spans area:backend + area:tests only"):

| Item | Area | Ticket |
|---|---|---|
| `scripts/auto_promote_develop_to_main.sh` cron rewrite to use the new `auto_promote_main` module | `area:tooling` | TBD — file as AUDIT-13a |
| Gerrit conditional submit-requirement on `milestone:R3-fastforward` (path C) | `area:devops` | TBD — file as AUDIT-13b |
| Web UI "Advance main now" affordance (path B) | `area:frontend` | Sprint H H4 follow-up — separate ticket |
| OP-925 R3 re-pick verification (live release env) | `area:tests`, `area:devops` | OP-925 itself — covered when operator removes `claim:human-blocked` |
| R4 pre-pickup main-currency check (belt-and-suspenders) | `area:backend` | TBD — file if v0.5.0-rc1 surfaces the R4-before-submit race |

## Verification

- `pytest backend/tests/test_auto_promote_main.py` — green at OP-960 PS-final
- Manual: codex auto-pickup of OP-925 (post AUDIT-11/12/13) — pending live verification 2026-05-12

## References

- OP-960 commit `3ad103fb` and Change-Id `Ib92cd470f9cf90cef3b92f0b42f62beb76f83780`
- AUDIT-13 ticket OP-960 description (root cause + 3-option decision matrix)
- L-OP-XXX (TBD) — operational lesson: "infrastructure-blocker tickets must surface as separate tickets, not inline AC checks, when they cascade across multiple roles"
- O6 Merger Agent OP-269 — precedent for conditional bot +2
- Sprint D pipeline runbook `docs/operations/release-cut-runbook.md` (R3 / D5 implementation reference)
- OP-925 incident comments 2026-05-12 08:42-09:40 (the failure cascade that triggered this ADR)
