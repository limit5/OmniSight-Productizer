---
audience: internal
risk_id: R12
risk_title: gVisor cost-weight only / not actual runtime
severity: 🔴 high (impact) × 🟢 low (likelihood) — see §3
status: closed — BP.W3.13 makes runsc load-bearing in production
owners: architect / sa_sd / auditor
landed: 2026-05-03 (BP.S.5)
close_out: BP.W3.13 (Phase U gVisor adoption, 2026-05-06)
---

# Risk R12 — gVisor cost-weight label vs actual sandbox runtime

> **TL;DR**: BP.S.5 recorded that `SandboxCostWeight.gvisor_lightweight`
> and Tier-1 design language were nominal labels, not proof that
> production actually ran gVisor. BP.W3.13 closes that gap:
> `ENV=production` now requires `OMNISIGHT_DOCKER_RUNTIME=runsc`,
> Docker must advertise `runsc`, and every production sandbox launch
> verifies `docker inspect --format '{{.HostConfig.Runtime}}'` is
> `runsc`. Compliance claims may cite gVisor only when release evidence
> includes the production launch audit row and the runc/runsc benchmark
> CSV from `scripts/benchmark_gvisor_runtime.sh`.

This document is the single canonical record of R12. Other docs may
*reference* it, but they MUST NOT redefine the risk text or its
mitigations — that is how compliance claims drift.

---

## 1. Why this risk existed (root cause)

There are three independent sources of the "gVisor is in production"
illusion. R12 is the *aggregate* of all three telling the same false
story:

### 1.1 `backend/sandbox_capacity.py` — enum member name

```python
class SandboxCostWeight(float, Enum):
    gvisor_lightweight = 1.0          # ← 1 token = "1 core × 512 MiB"
    docker_t2_networked = 2.0
    phase64c_local_compile = 4.0
    phase64c_qemu_aarch64 = 3.0
    phase64c_ssh_remote = 0.5

DEFAULT_COST = SandboxCostWeight.gvisor_lightweight
```

The name `gvisor_lightweight` is purely a **DRF cost-bucket label**:
"this class of workload (unit test / lint) is the lightest, weight 1.0".
The enum is consumed by `sandbox_capacity` for admission decisions and
by `container.py` to derive `--memory` / `--cpus` from
`COST_WEIGHT_ESTIMATES`. **The enum value never reaches `docker run
--runtime=…`.** The runtime selection lives elsewhere (see §1.3).

A reasonable reader — including a third-party legal-review auditor —
sees `gvisor_lightweight` and concludes "Tier 1 sandboxes run on
gVisor". That conclusion is structurally wrong: nothing in the cost-
weight pathway *causes* gVisor to be the runtime.

### 1.2 `docs/design/tiered-sandbox-architecture.md` — design language

The architecture doc reads (line 22, line 67):

> 環境屬性：瞬態微虛擬機 (Ephemeral MicroVM，如 Firecracker / gVisor)
> 或嚴格限制的 Docker。
>
> 輕量級沙盒引擎 | Docker 搭配 gVisor | 相比純 Docker，gVisor 提供
> User-space kernel 隔離，防止 Agent 利用 Linux 核心漏洞逃逸。

This is a *design statement of intent*. It describes what Tier 1 SHOULD
be when Phase U adoption ships. It does not describe what production
runs today. Without an explicit "Phase U gating" callout (which BP.S.3
added in `sandbox-tier-audit.md` §0 and BP.S.4 added in
`pep-gateway-tier-policy.md` §0), the language reads as descriptive of
the *current* system.

### 1.3 `.env.example` and `backend/container.py` — actual runtime knob

The actual runtime knob lives in `.env.example`:

```ini
# OMNISIGHT_DOCKER_RUNTIME=runsc
```

It is **commented out by default**. `backend/container.py` reads it
and falls through to Docker's default (`runc`) if unset, malformed, or
if the host doesn't have `runsc` installed. Behaviour matrix:

| Operator state | Runtime used | gVisor in effect |
|---|---|---|
| `.env.example` left as-shipped (knob commented) | `runc` | ❌ no |
| `OMNISIGHT_DOCKER_RUNTIME=runsc` set, gVisor installed | `runsc` | ✅ yes |
| `OMNISIGHT_DOCKER_RUNTIME=runsc` set, gVisor NOT installed | `runc` (silent fallback) | ❌ no, no warning |
| `OMNISIGHT_DOCKER_RUNTIME=runc` (explicit) | `runc` | ❌ no |

Note row 3: **silent fallback**. If the operator believes they have
opted in but gVisor isn't installed on the host, no warning is raised
at request time. The fallback is intentional for dev / WSL2 / macOS
ergonomics, but it means "the env var is set" is not equivalent to
"gVisor is the kernel boundary".

There is **no runtime assertion** anywhere in the request path that
the actual `docker inspect` output of a launched sandbox shows
`Runtime: runsc`. There is no Prometheus metric. There is no startup
log line.

### 1.4 `backend/sandbox_tier.py` — tier enum is admission-only

The Tier enum (`SandboxTier.T1` / `T2` / `T3`) is structural admission
metadata (which Guild may run which Tier). It is *not* wired to any
runtime flag — `T1` does not imply `--runtime=runsc`. The Guild × Tier
admission matrix (BP.S.1) and the PEP Gateway tier-aware whitelist
(BP.S.4 doc) operate at the *policy* layer; runtime selection is a
separate, currently-unsynchronised concern.

---

## 2. Impact (why this is 🔴 high)

The label-vs-actual gap converts a documentation drift into a
**compliance / legal exposure** in three concrete ways:

### 2.1 Phase D third-party legal review (primary impact)

`docs/design/blueprint-v2-implementation-plan.md` §11 commits Phase D
to a third-party legal review of medical / automotive / industrial /
military compliance claims. The reviewer reads:

* `tiered-sandbox-architecture.md` §I — "gVisor user-space kernel
  isolation"
* `sandbox_capacity.py` — `gvisor_lightweight` cost class
* compliance modules (Phase D `backend/compliance_matrix/`) citing
  IEC 62304 / ISO 26262 / IEC 61508 / DO-178C clauses on isolation

If those modules also cite gVisor as the discharging control for an
isolation clause, the review will either (a) reject the claim — losing
the milestone — or worse (b) approve the claim based on the false
description, exposing the project to fraud-grade legal risk if a
breach later reveals that no gVisor was running.

**This is the primary reason R12 severity is rated 🔴 high.**

### 2.2 Customer-facing security copy

Sales / marketing / RFI responses are likely to inherit the language
verbatim. "Sandboxed with gVisor user-space kernel isolation" is a
selling point against competitors. Saying it without it being true is:

* Materially misleading under FTC / consumer-protection law
* A false statement of fact in a procurement RFI / RFP response
* A breach of the security warranty section in customer MSAs

### 2.3 Internal incident response

If a sandbox-escape incident occurs, the IR team will reach for the
"gVisor blocks this CVE class" runbook and waste minutes (or hours)
before realising the runtime was `runc` — minutes/hours during which
the attacker continues lateral movement.

---

## 3. Likelihood (why this is 🟢 low — *given mitigations*)

The mitigations in §4 land before any compliance / customer claim
ships, so the "false claim reaches an external audience" likelihood is
🟢 low **on the assumption that the documentation gates hold**.

Without the gates the likelihood would be 🟠 medium — `gvisor_lightweight`
is a name a copy-editor would naturally lift into marketing copy.

**The 🟢 low rating is contingent on the mitigations remaining in
force.** If any mitigation is reverted (auxiliary disclaimer dropped,
audit doc §0 callout removed, Phase D legal review skipped), the
rating snaps back to 🟠 medium and a fresh re-evaluation per
`blueprint-v2-implementation-plan.md` §10.5 is required.

---

## 4. Mitigations and close-out state

### 4.1 Documentation gates (✅ landed by BP.S.3 / BP.S.4 / BP.S.5)

The "do not cite gVisor as an active control" rule is enforced through
**three mutually-reinforcing callouts**:

1. **`docs/design/sandbox-tier-audit.md` §0 R12 disclaimer** (BP.S.3,
   2026-05-02) — Phase D legal review's single source of truth for
   the auxiliary disclaimer. Says: tiers are nominal until BP.W3.13;
   compliance claims may not cite gVisor.
2. **`docs/design/pep-gateway-tier-policy.md` §0 R12 callout** (BP.S.4,
   2026-05-02) — Repeats the disclaimer for any reader who lands on
   the PEP integration doc first.
3. **This file (`docs/security/r12-gvisor-cost-weight-only.md`)**
   (BP.S.5, 2026-05-03) — Detailed write-up; the canonical home of
   R12.

The audit register (`docs/audit/2026-04-27-deep-audit.md` §6 "Risk
register 對照表") cross-references this file as R12's tracked record.

### 4.2 Auxiliary-disclaimer module headers (forward-looking, Phase D)

Per `blueprint-v2-implementation-plan.md` §11 (R1 mitigation),
**every** Phase D `backend/compliance_matrix/{medical,automotive,
industrial,military}.py` module SHOULD ship with a top-of-file header
disclaimer that explicitly enumerates the controls **not** discharged
by this codebase. R12 dictates that these headers MUST list:

> ※ **Sandbox isolation control NOT discharged**: this codebase does
>   not currently run gVisor (`runsc`) as the active container runtime
>   in production. References to gVisor in `tiered-sandbox-
>   architecture.md` and the `gvisor_lightweight` cost-weight class
>   are nominal; runtime is `runc` unless the operator has explicitly
>   configured `OMNISIGHT_DOCKER_RUNTIME=runsc` and installed gVisor
>   on every host. Compliance assessors MUST verify the runtime via
>   `docker info | grep Runtimes` and `docker inspect <container> |
>   grep Runtime` before discharging any container-escape clause via
>   sandbox isolation. See `docs/security/r12-gvisor-cost-weight-only.md`.

This is a forward-looking requirement on the Phase D modules — those
modules do not exist yet at the time R12 is recorded. The requirement
is recorded here so that whoever writes them knows what to include.

### 4.3 BP.W3.13 runtime close-out

BP.W3.13 makes the runtime claim load-bearing:

* `backend/config.py` still defaults `docker_runtime` to `runsc`.
* `backend/container.py::resolve_runtime()` hard-fails in
  `ENV=production` unless the configured runtime is `runsc` and Docker
  lists `runsc` in `docker info --format '{{json .Runtimes}}'`.
* `backend/container.py::_assert_container_runtime()` verifies the
  launched container via `docker inspect --format '{{.HostConfig.Runtime}}'`
  and removes the container if production sees anything other than
  `runsc`.
* `omnisight_sandbox_launch_total{tier="t1",runtime="runsc",result="success"}`
  and the `sandbox_launched.after.runtime=runsc` audit row are the
  operational evidence for each launch.
* `scripts/benchmark_gvisor_runtime.sh` records the runc/runsc
  performance envelope before the deployment can move from
  `deployed-active` to `deployed-observed`.

The old documentation-only mitigation remains useful for historical
review, but R12 status is now `closed` once the BP.W3.13 commit is in
the release branch and the first production launch evidence is attached.

### 4.4 What does NOT mitigate R12

For absolute clarity:

* ❌ Setting `OMNISIGHT_DOCKER_RUNTIME=runsc` in `.env` on a single
  host — not enough; silent fallback if gVisor not installed.
* ❌ The `SandboxCostWeight.gvisor_lightweight` enum existing — that
  is the *cause* of the risk, not its mitigation.
* ❌ The Tier enum (`SandboxTier.T1`) being defined — admission policy,
  not runtime selection.
* ❌ A single docs PR mentioning gVisor — the three-callout pattern
  (audit + pep + this file) is the minimum, because compliance review
  may land on any of the three docs first.

---

## 5. Detection — how to verify the risk stays closed

For an operator or auditor who needs to confirm the runtime claim:

```bash
# 1. Check the host runtime configuration.
docker info 2>/dev/null | grep -i 'Runtimes\|Default Runtime'
# Expected: runsc appears in the runtime list.

# 2. Inspect a live sandbox container's actual runtime.
docker inspect $(docker ps -q --filter "name=omnisight-agent-") \
  --format '{{.Name}} runtime={{.HostConfig.Runtime}}'
# Expected: "/omnisight-agent-… runtime=runsc"

# 3. Verify the env knob.
grep -E '^OMNISIGHT_DOCKER_RUNTIME' .env
# Expected in production: ENV=production and OMNISIGHT_DOCKER_RUNTIME=runsc

# 4. Check container.py reads the knob (sanity).
grep -n 'OMNISIGHT_DOCKER_RUNTIME\|_assert_container_runtime' backend/container.py | head
```

If any production check shows `runc`, empty runtime, or
`sandbox_runtime_fallback`, treat it as a deployment regression and
roll back to the last build with verified `runsc` launches.

---

## 6. Cross-references

* `backend/sandbox_capacity.py` — `SandboxCostWeight.gvisor_lightweight`
  (the cost-weight enum that *names* gVisor)
* `backend/container.py` — `OMNISIGHT_DOCKER_RUNTIME` reader (the
  actual runtime knob)
* `backend/sandbox_tier.py` — Tier enum (admission, not runtime)
* `.env.example` lines 100-103 — documented opt-in for `runsc`
* `docs/design/tiered-sandbox-architecture.md` §I — design language
  describing Tier 1 as "Docker + gVisor"
* `docs/design/sandbox-tier-audit.md` §0 / §3 / §7 — R12 callouts
  (BP.S.3, 2026-05-02)
* `docs/design/pep-gateway-tier-policy.md` §0 / §7 — R12 callouts
  (BP.S.4, 2026-05-02)
* `docs/audit/2026-04-27-deep-audit.md` §6 Risk register table — R12
  row pointing here
* `docs/design/blueprint-v2-implementation-plan.md` §11 risk table
  R12 row + §10.5 re-evaluation rule + Phase U / BP.W3.13 close-out
  reference
* `TODO.md` BP.W3.13 — the actual gVisor adoption row that closes R12
* Sister R-series risks: R10 (RLM library temptation), R11 (`|| true`
  swallowed errors), R13 (Hardware Bridge Daemon nominal), R14 (self-
  improvement L-level gap)

---

## 7. Change log

| Date | What | Why | Who |
|---|---|---|---|
| 2026-05-03 | Initial publication (BP.S.5). Risk text aggregated from blueprint §11 row + audit §6 row + sandbox-tier-audit §0 callout. Three-callout mitigation pattern recorded. Phase U / BP.W3.13 close-out path documented. Detection runbook §5 added. | Audit register row 340 had `BP.S.5 待 record`; Phase D legal-review readiness needs a single canonical risk write-up before any compliance module ships. | Agent-row7-self-agent |
| 2026-05-06 | BP.W3.13 close-out. Production runtime now fails closed unless Docker can launch and inspect Tier 1 sandboxes as `runsc`; benchmark script records runc/runsc performance evidence. | Close R12's label-vs-runtime gap and allow future compliance language to cite measured gVisor runtime evidence instead of the cost-weight enum name. | Codex/GPT-5.5 |
