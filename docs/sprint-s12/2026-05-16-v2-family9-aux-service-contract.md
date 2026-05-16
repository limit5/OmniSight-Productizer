---
id: SPRINT-S12G-V2-FAMILY9-AUX-SERVICE-CONTRACT
version: v1 (2026-05-16)
title: G.A-v2 Family ⑨ — Optional auxiliary service contract ("available-then-use, unavailable-then-skip")
scope: Contract spec for the reusable "optional auxiliary service" pattern (D1 detection + D2 loud-but-non-fatal exception). Service-agnostic; ai-core is the first consumer. Doc-only ticket (v2-⑨-1a); no runtime, db, devops, embedded, frontend, security, tests, or tooling code is touched by OP-1155.
status: Draft — OP-1155 (this ticket); reframe LOCKED 2026-05-14, Q3
related:
  - sprint-s12g-A-v2-runtime-defense-contract-spec.md §3 "Family ⑨ — Optional auxiliary service contract (ai-core / Local LLM)" (parent spec)
  - 2026-05-16-v2-alertbridge-framework-contract.md (shared alert plumbing — §4 alert routing of this doc cites it)
  - backend/config.py:105 (`llm_fallback_chain` default — first consumer of the AI_CORE_AVAILABLE flag)
  - backend/routers/health.py:386-446 (`_check_provider_chain` — current shallow check; the probe defined here is the deep, cached counterpart for an *auxiliary* provider)
  - JIRA OP-1155 (v2-⑨-1a — this spec)
  - JIRA OP-1156+ (v2-⑨-1bc, v2-⑨-2bc, v2-⑨-3a-Decision, v2-⑨-Integration — downstream impl tickets that consume this spec)
  - JIRA META-POST-RC2-CROSS-STACK-HYGIENE (placeholder for the Codex P1-5 punt — daemon-resource hygiene across co-tenant compose stacks; deferred post-rc2)
---

# G.A-v2 Family ⑨ · Optional auxiliary service contract — v1 (2026-05-16)

## §0. Reading order

1. §1 — the pattern in one sentence + the two states (5 minutes).
2. §2 — the contract surface: probe + gauge + flag + capability + UI.
3. §3 — the `ai_core_probe` reference implementation contract.
4. §4 — alert routing via the AlertBridge framework.
5. §5 — cross-service extensibility (omnisight-ai-engine, omnisight-vision-core, future).
6. §6 — explicit non-scope: Codex P1-5 punt to `META-POST-RC2-CROSS-STACK-HYGIENE`.
7. §7 — `v2-⑨-3a-Decision` scope (the one-shot operator decision for the current crashlooping `ai_gateway`).
8. §8 — cross-stack delegation map (what this family does and does NOT depend on).
9. §9 — open questions, changelog, hand-off block.

This spec is the **contract** — not the implementation. Code lands in `v2-⑨-1bc` (the `ai_core_probe` reference implementation + Prometheus gauge + in-process flag), `v2-⑨-2bc` (the `llm_fallback_chain` builder consumer + capability-inventory propagation), and `v2-⑨-Integration` (chaos test); operator-side hygiene for the current `ai_gateway` container lands in `v2-⑨-3a-Decision`. Section anchors in §2, §3, §4, §5 are stable and will be referenced from those tickets' AC.

The contract is intentionally **service-agnostic**: ai-core is the first consumer because it is the first concrete need, but every clause in §2 reads "the auxiliary service" rather than "ai-core" precisely so that omnisight-ai-engine and omnisight-vision-core can adopt the same pattern in 2026-Q3+ without re-negotiating the contract. §5 enumerates the second and third consumers explicitly.

---

## §1. The pattern, in one sentence

> *"An optional auxiliary service is one where the productizer treats**available → auto-provide**, **unavailable → quietly skip** — the rest of the system continues without error; the gap is visible in metrics + UI but is loud-but-non-fatal, never a page."*

### §1.1 Two states, two behaviours

| State | Probe verdict | Productizer behaviour | Operator-visible signal |
|---|---|---|---|
| Available | `omnisight_aux_service_available{service="…"} == 1` | Service is **exposed**: included in capability inventory; included in fallback chains where applicable; UI shows the corresponding feature enabled; capability-aware code paths may delegate to it. | Capability inventory carries `capability:enable=<service>`; UI tile lit; Prom gauge `== 1`. |
| Unavailable | `omnisight_aux_service_available{service="…"} == 0` | Service is **quietly skipped**: removed from capability inventory; removed from fallback chains; UI hides the feature (no error toast, no degraded badge — the feature is simply not visible to the end-user); capability-aware code paths route around it. | Capability inventory does NOT carry `capability:enable=<service>`; UI tile not rendered; Prom gauge `== 0`; after 24 h continuous unavailability, a `severity=warn` alert fires (§4). |

The reframe locked on 2026-05-14 (Q3 of the G.A-v2 design review) explicitly rejects the historic "auxiliary = hard dependency" mental model that had caused two earlier classes of bug:

- **Past bug class A — startup wedge.** Productizer refused to come up because an optional service was down. The "fix" had historically been to make the dependency hard-and-loud (page on missing ollama); the correct fix per this contract is to make it soft-and-quiet (skip).
- **Past bug class B — orphan-container symptom.** `ai_gateway` (the deprecated wrapper container for the omnisight-ai-core sister project) crashloops on a host where omnisight-ai-core is not deployed. Without a probe deciding "is ai-core healthy?", the dead container can sit there forever AND the fallback chain cannot dynamically include / exclude `ollama`. Today both problems are static. This contract makes both dynamic.

### §1.2 What this pattern is NOT

The contract is precise about what it does and does not promise:

- **NOT** a circuit breaker. A circuit breaker reacts to *call failures*; the pattern here is preemptive — it samples the service's health on a periodic cadence and routes around it ahead of any caller having to fail. The two mechanisms compose (a flagged-unavailable service does not even appear in the fallback chain, so no caller ever attempts it; a momentary in-call failure is the circuit breaker's job).
- **NOT** a load-balancer. Auxiliary services are usually single-instance local processes (ollama, vision-core's local inference daemon). The pattern says "is it there?" — not "which of N replicas should I pick?"
- **NOT** a discovery service. The probe targets a *known* endpoint per the in-process config. Service discovery (Consul, etcd, DNS-SD) is orthogonal and out of scope.
- **NOT** a request-time fallback. The flag is consulted by *builders* (the `llm_fallback_chain` builder, the capability-inventory builder) — not by per-request hot paths. Per-request code reads the *already-built* chain. This keeps the hot path free of probe-state reads.

### §1.3 Why this is service-agnostic from day one

The 2026-05-14 reframe (operator Q3) deliberately raised the abstraction layer from "ai-core hygiene" to "optional auxiliary service contract" precisely because the same pattern will be needed for omnisight-ai-engine (2026-Q3 plan) and omnisight-vision-core (2026-Q4 plan). The cost of writing the spec service-agnostic is near-zero (substitute `<service>` for `ai_core` in §2 and §3); the cost of *not* doing so is that each of the future sister services re-derives the pattern, drifts in details, and the cross-family invariants (alert severity, gauge name, capability propagation) silently disagree. §5 covers extensibility; §2 names "the auxiliary service" rather than "ai-core" throughout.

---

## §2. The minimum contract for any auxiliary service

This is the API surface that `v2-⑨-1bc` implements for ai-core specifically and that any future auxiliary service MUST implement to claim conformance with this family.

The contract has five obligations (§2.1–§2.5); a downstream impl ticket is conformant iff and only iff it satisfies all five.

### §2.1 Periodic probe (D1 — detection)

The auxiliary service MUST have a **periodic in-process probe** that:

- runs every **60 s** (`PROBE_CADENCE_SECONDS = 60`);
- hits the service's documented health endpoint;
- has a **per-attempt timeout** of **2 s** (`PROBE_TIMEOUT_SECONDS = 2`) — generous enough that a single GC pause on the auxiliary doesn't flip the flag, tight enough that a hung TCP connection doesn't block the probe loop;
- applies **flap suppression** (§3.3) so a single failed sample does not change the flag — see also §3.4 for the chosen 3-of-5 sample window;
- writes the resulting verdict into:
  - the Prometheus gauge `omnisight_aux_service_available{service="<name>"}` (`1.0` for available, `0.0` for unavailable; see §2.2);
  - the in-process feature flag `AUX_SERVICE_AVAILABLE[<name>]` (boolean; see §2.3).

The probe runs in a `asyncio.create_task(...)` background coroutine launched at app startup (in the FastAPI `lifespan` start hook, alongside the other background daemons). It MUST NOT raise into the lifespan — an exception inside the probe is captured, logged at `WARN`, and converted into a *probe-down* sample (which still flows through flap suppression, so a transient probe bug doesn't flip the flag instantly).

### §2.2 Prometheus gauge contract

```python
omnisight_aux_service_available = Gauge(
    "omnisight_aux_service_available",
    "1.0 if the named optional auxiliary service is healthy and "
    "should be used; 0.0 if it is unavailable and the productizer "
    "is routing around it. Sample cadence: 60 s. Per family ⑨ "
    "contract (docs/sprint-s12/2026-05-16-v2-family9-aux-service-"
    "contract.md §2.2).",
    labelnames=["service"],
)
```

- Label values for `service` are *kebab-case ASCII* (e.g. `"ai_core"`, `"vision_core"`, `"ai_engine"`). Underscores allowed. No glyphs (see AlertBridge §1.1 for the same ASCII discipline rationale).
- Cardinality: ≤ 10 distinct `service` values across the fleet, well under the AlertBridge §6 cap.
- The gauge MUST be initialised to `0.0` at app start (per `service` registered) so that the absence of a probe sample (e.g. during the first 60 s after startup) reads as "unavailable" — failing closed for the downstream chain builder. The first successful probe sample flips it to `1.0`.
- The gauge MUST NOT be exposed for services that are not registered (calling `.labels(service="…")` with an unregistered name is a programmer error and is rejected by §2.4 capability-inventory schema validation at builder time, not at probe time).

### §2.3 In-process feature flag

A module-level `dict[str, bool]` (`AUX_SERVICE_AVAILABLE`) maintained by the probe loop is the synchronous side of the same signal. It exists because the Prometheus gauge is *write-only from the probe's perspective*; in-process code paths that need to ask "is the auxiliary service available right now?" must not have to scrape the gauge.

```python
AUX_SERVICE_AVAILABLE: dict[str, bool] = {}  # populated by the probe; read by builders
```

Per-service convenience aliases live in the consumer module:

```python
# in backend/agents/llm.py or wherever the chain builder lives
AI_CORE_AVAILABLE = AUX_SERVICE_AVAILABLE.get("ai_core", False)
```

The dict is *appendable only* from the probe loop; consumers read but do not write. The dict is a regular `dict` (not `threading.Lock`-protected) because Python's GIL gives single-assignment safety for `dict[str] = bool` and the read pattern in builders is "snapshot the bool, then decide" — never compare-and-set. The contract test (§7 of the AlertBridge spec) does not need to verify this because the gauge and flag are derived from the same probe-loop write, so divergence is structurally impossible.

### §2.4 Capability inventory integration

The capability inventory builder MUST consult `AUX_SERVICE_AVAILABLE` when deciding which `capability:enable=<service>` labels to expose. Specifically:

- The inventory builder runs at every relevant lifecycle moment (startup, settings reload, explicit `POST /admin/refresh-capabilities`, and once per probe cycle for the affected service).
- When `AUX_SERVICE_AVAILABLE[<name>] is True`, the inventory carries `capability:enable=<service>`.
- When `AUX_SERVICE_AVAILABLE[<name>] is False`, the inventory **does not carry** that label — neither `capability:enable=<service>=false` nor `capability:disable=<service>`. The contract is *absence* not *negation*. This matches the existing `capability:enable=*` label convention from `docs/sop/jira-label-conventions.md` §"capability inventory".
- Downstream consumers of the capability inventory (the JIRA dispatcher's `capability:enable=*` matching for runner pickup, the UI's feature-tile renderer, the per-request RBAC layer's deferred-feature check) MUST treat absence as "feature unavailable" — no further check needed.

The `llm_fallback_chain` builder is one specific consumer; see §3.5.

### §2.5 UI behaviour

The user-facing UI MUST reflect the state without raising error UI:

| State | UI behaviour |
|---|---|
| Available | Feature tile / nav entry rendered; controls active; no special badge. |
| Unavailable | Feature tile / nav entry **not rendered**. No "degraded" badge, no toast, no console error. |

The reasoning: an end-user who never installed omnisight-ai-core has no concept of ai-core being "missing" — there is nothing to be missing, only a feature that's available on some installs and not others. The contract treats the unavailable state as **the absence of the feature**, not as the presence of a broken feature. The 24 h alert (§4) is for the *operator* of an install that *did* deploy the auxiliary, signalling that something broke.

The frontend reads the capability inventory (§2.4); rendering decisions cascade from there. No frontend code needs to know about probes, gauges, or flags directly — that boundary stays inside `backend/`.

---

## §3. `ai_core_probe` — reference implementation contract

This section names the specific implementation details for the first consumer (ai-core). Downstream tickets reference these names verbatim; do not invent variations.

### §3.1 Module location

```
backend/agents/ai_core_probe.py
```

- New file. Sibling to `backend/agents/llm.py` so the chain builder's import line is `from backend.agents.ai_core_probe import AI_CORE_AVAILABLE`.
- Lives in `backend/agents/` because the probe is a productizer-side concern (deciding which providers to use), not an alerting or auth concern.

### §3.2 Public names (frozen)

```python
PROBE_CADENCE_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 2.0
PROBE_FLAP_WINDOW = 5          # samples
PROBE_FLAP_THRESHOLD = 3       # samples within window that must agree

AUX_SERVICE_AVAILABLE: dict[str, bool] = {}

AI_CORE_AVAILABLE: bool  # property-style read of AUX_SERVICE_AVAILABLE["ai_core"]


async def probe_once(service_name: str, endpoint: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Single probe attempt. Returns True iff the endpoint responded
    HTTP 2xx within `timeout`. Network failures, 4xx, 5xx, and timeout
    all return False. Never raises into the caller — the function is
    total over its declared input space."""


async def probe_loop(service_name: str, endpoint: str) -> None:
    """Long-running coroutine. Samples `probe_once` every PROBE_CADENCE_SECONDS;
    runs the result through flap suppression (§3.3); updates the gauge and
    the flag. Started from the FastAPI lifespan hook; cancelled at shutdown."""
```

### §3.3 Endpoint, retry, and timeout

For ai-core specifically:

- Endpoint: `http://omnisight-ai-core:8080/health` (the project-internal docker-network DNS name; configurable via `OMNISIGHT_AI_CORE_PROBE_URL` env var with the docker-network default).
- HTTP verb: `GET`; no body.
- Success criterion: HTTP 2xx within the 2 s timeout. Body content is NOT inspected (the contract does not couple to ai-core's `/health` body shape, which is the sister project's contract to evolve).
- Retry: **none** within a single 60 s sample. A single sample is a single attempt; failure is captured as one "down" sample and flows into flap suppression. Retrying within a sample would (a) blur the cadence and (b) double-count a sustained outage.
- Timeout: 2 s. Outside that window the sample is "down" with `reason="timeout"` recorded in the structured log line.
- Per-attempt log line at `DEBUG` (always), at `INFO` only on *transition* events (up→down, down→up). The transition log line carries `service`, `previous_state`, `new_state`, `samples_in_window`. No PII; no auth headers; no upstream body.

### §3.4 Flap detection (3-of-5 sliding window)

A single failed sample MUST NOT flip the flag from "available" to "unavailable", and vice versa. The probe loop keeps the last `PROBE_FLAP_WINDOW = 5` samples in a deque and updates the flag iff at least `PROBE_FLAP_THRESHOLD = 3` of those samples agree on a state distinct from the current flag value.

| Current flag | Window contents (oldest → newest) | Resulting flag | Reasoning |
|---|---|---|---|
| `True` | `[up, up, up, up, up]` | `True` | unanimous up; no change. |
| `True` | `[up, up, up, up, down]` | `True` | single down sample is suppressed. |
| `True` | `[up, up, down, down, down]` | `False` | 3 of 5 down → flip. |
| `False` | `[down, up, up, up, down]` | `False` | 3 down within 5 (the 1 from the head + the recent 2) — but the *recent* state is mostly up; flap suppression specifically requires 3 *contiguous-or-not* samples *of the new state* before flipping. In this case the *new* state would need to be `True`, and only 3 of 5 samples are `up`. Tie-break: flip to `True` per the threshold rule. |
| `False` | `[down, down, up, up, up]` | `True` | trailing run of 3 up → flip. |

The state machine deliberately accepts a 3-of-5 *non-contiguous* trigger because the alternative (require 3 contiguous) extends the worst-case recovery time from 3 cycles (3 min) to ~5 cycles (5 min) for no extra noise reduction (a 3-of-5-non-contiguous pattern is itself rare under steady operation).

**Bootstrap rule.** The probe loop starts with an empty deque. Until the deque holds 5 samples (i.e. for the first 5 cycles ≈ 5 min after app start), the flag remains at its initial value (`False`). This guarantees that the chain builder during cold-start has a deterministic, conservative answer: ai-core is treated as unavailable until the probe loop has had at least 5 minutes to collect evidence. This is the "fail closed at startup" guarantee.

### §3.5 The `llm_fallback_chain` consumer

The `v2-⑨-2bc` ticket implements the chain-builder consumer. The contract:

```python
# Pseudocode for the chain builder; v2-⑨-2bc implements this in backend/agents/llm.py
def build_active_fallback_chain() -> list[str]:
    raw_chain = [p.strip() for p in settings.llm_fallback_chain.split(",") if p.strip()]
    if not AUX_SERVICE_AVAILABLE.get("ai_core", False):
        # ai-core unavailable → ollama is the only provider that depended on it; remove.
        raw_chain = [p for p in raw_chain if p != "ollama"]
    return raw_chain
```

Properties this implementation MUST preserve:

- **No mutation of the source-of-truth setting.** `settings.llm_fallback_chain` is the *declared* chain (operator config). The builder returns the *active* chain (the declared one ∩ available providers). The two MUST NOT be conflated; the operator's intent is preserved across probe state transitions.
- **Idempotent.** Calling `build_active_fallback_chain()` twice with no state change returns the same list.
- **Pure.** No I/O (no network, no DB, no file). The function reads `settings.llm_fallback_chain` (in-process) and `AUX_SERVICE_AVAILABLE["ai_core"]` (in-process) and returns a list.
- **Race-free under probe-state flip.** Because the builder is called by request-time code that snapshots the dict-read (§2.3), a probe-state flip mid-request affects only *subsequent* requests, not the in-flight one.

### §3.6 Tests `v2-⑨-1bc` must include

(For traceability — the test list is the AC of `v2-⑨-1bc`, not of this spec; reproduced here so the impl ticket can cite §3.6 by anchor.)

1. **`test_probe_once_returns_true_on_2xx`** — happy path.
2. **`test_probe_once_returns_false_on_timeout`** — the endpoint is replaced with a sleep > 2 s sink; sample is `False`.
3. **`test_probe_once_returns_false_on_5xx`** — endpoint returns 500.
4. **`test_probe_once_returns_false_on_connection_refused`** — endpoint port is closed.
5. **`test_probe_loop_updates_flag_on_3_of_5_down`** — fixture deque, threshold flip from `True` to `False`.
6. **`test_probe_loop_suppresses_single_down`** — deque `[up,up,up,up,down]` → flag stays `True`.
7. **`test_probe_loop_recovers_on_3_contiguous_up`** — deque `[down,down,up,up,up]` → flag flips to `True`.
8. **`test_probe_loop_bootstrap_keeps_flag_false_until_5_samples`** — first 4 cycles after start: flag is `False` even if all samples are `up`.
9. **`test_probe_loop_emits_transition_log_only_on_state_change`** — INFO log fires on the cycle that flipped, not on the next 4 unanimous cycles.
10. **`test_gauge_initialises_to_zero`** — at module import time, before the loop runs.
11. **`test_gauge_reflects_flag_after_flip`** — gauge `== 0.0` after flag flips to `False`; `== 1.0` after flip to `True`.
12. **`test_chain_builder_excludes_ollama_when_ai_core_unavailable`** — covers the §3.5 builder.
13. **`test_chain_builder_keeps_ollama_when_ai_core_available`** — symmetric.
14. **`test_chain_builder_does_not_mutate_settings_llm_fallback_chain`** — asserts `settings.llm_fallback_chain` is unchanged after the builder runs.

The `v2-⑨-Integration` chaos test (`test_chaos_ai_core_up_down_up_cycle`) replaces the probe endpoint with a controllable harness, drives the up/down/up cycle, and asserts the chain transitions within 1 probe cycle (60 s ± jitter) of the state flip — see the parent spec's `v2-⑨-Integration` row.

---

## §4. Alert routing

The `omnisight_aux_service_available` gauge flowing through Alertmanager produces exactly one rule per family: `OmniSightAuxServiceUnavailable24h`. The rule conforms to the AlertBridge framework contract (`2026-05-16-v2-alertbridge-framework-contract.md`); the cross-references are noted inline.

### §4.1 Rule shape

```yaml
groups:
  - name: family-9-aux-service
    rules:
      - alert: OmniSightAuxServiceUnavailable24h
        expr: avg_over_time(omnisight_aux_service_available[24h]) == 0
        for: 0s   # the 24h is in the expr; the `for` clause is unnecessary
        labels:
          severity: warn
          area: integration
          family: "9"
          defense_dimension: D2
        annotations:
          summary: "Optional auxiliary service {{ $labels.service }} unavailable for 24h."
          description: |
            The optional auxiliary service "{{ $labels.service }}" has not
            answered its health probe for the last 24 hours.

            The productizer is running without it (per family ⑨ contract:
            available-then-use, unavailable-then-skip). End-users are not
            seeing errors; capability-aware features are silently absent.

            This alert exists so an operator who deployed the auxiliary
            knows it broke. If the auxiliary was intentionally taken down,
            silence this rule and proceed; otherwise inspect the auxiliary's
            own logs (it is a separate sister project — see runbook).
          runbook_url: https://docs.sora.services/runbooks/aux-service-24h-unavailable
          remediation_hint: |
            Check the auxiliary service's own logs (it is a separate sister
            project). If the auxiliary should be running, restart it per its
            own runbook; otherwise silence this rule.
        __bridge:
          critical_labels: [service]
```

### §4.2 Why `warn` and not `page`

The reframe says explicitly: an unavailable auxiliary is **loud-but-non-fatal**. Paging on an unavailable auxiliary is a contract violation — it would imply the productizer cannot function without it, which contradicts §1.

Per AlertBridge §3 the three severities are `page`, `warn`, `info`. Auxiliary unavailability is dashboarded, surfaces in business-hours review, and never paginates an on-call rotation. This maps to `warn` exactly.

The 24 h window (`avg_over_time(...)[24h] == 0`) is chosen so that:

- A short flap is suppressed at the probe layer (§3.4) and does not even reach the rule.
- A 24 h continuous outage is the threshold at which the operator's expectation diverges from reality — past 24 h, the operator who deployed the auxiliary almost certainly *thinks* it's still running.
- A 48 h or 72 h window would be too tolerant; a 1 h or 4 h window would page (`warn` here, but still cluttered) too often for what is, by contract, a non-fatal condition.

### §4.3 Routing via AlertBridge

Per the AlertBridge framework contract:

- `severity: warn` → routed via `SEVERITY_CHANNEL_MAP["warn"]` (v0: `["email", "stdout"]`; post-31.G swap: AM webhook + email).
- `group_by: [severity, area]` per AlertBridge §8 — auxiliary-service unavailabilities collapse into a single notification per (warn, integration) group, so two auxiliaries down at once produce one email, not two.
- Dedupe key: includes `service` (declared as a critical label in `__bridge.critical_labels`), so two distinct auxiliary services have distinct dedupe keys.
- Resolved-notification policy: `warn` resolves silently per AlertBridge §5 — when the auxiliary recovers, the operator sees the firing alert clear in dashboards without an inbox notification.

### §4.4 Per-service routing variant — operator-controlled silencing

Because §1 says some installs may *intentionally* run without a given auxiliary (e.g. an install with no GPU running without omnisight-vision-core), the operator MUST be able to silence the alert per-service without disabling the framework:

- Recommended approach: Alertmanager inhibition rule scoped to `{service="<name>"}`. The AlertBridge framework supports this without per-rule edits.
- Alternative: the install's compose / deploy config sets `OMNISIGHT_AUX_SERVICE_DISABLE=ai_core,vision_core` (comma-separated). When a service is in this list, the probe is **not started** and no samples reach the gauge, so the 24 h-avg condition is never evaluated. The capability inventory simply never adds the corresponding `capability:enable=<service>` label, and the UI does not render the feature. This is the "we don't ship this auxiliary on this install" path.

The choice between AM-inhibition and `OMNISIGHT_AUX_SERVICE_DISABLE` is a `v2-⑨-1bc` impl call; both shapes satisfy the contract.

### §4.5 What the alert does NOT do

- It does NOT page. (See §4.2.)
- It does NOT auto-recover the auxiliary. (Out of scope; the auxiliary is a separate sister project.)
- It does NOT modify the chain. The chain mutation happens in `build_active_fallback_chain()` (§3.5) and is independent of whether the alert fired.
- It does NOT span multiple auxiliaries. One rule, multiple `service` label values; the operator sees one row per affected service in the dashboard.

---

## §5. Cross-service extensibility

ai-core is the first consumer. The contract is service-agnostic so the second and third consumers do not re-derive it.

### §5.1 Concrete candidates (2026 roadmap)

| Service | Health endpoint (planned) | Status as of 2026-05-16 | Expected gauge label value |
|---|---|---|---|
| `omnisight-ai-core` | `http://omnisight-ai-core:8080/health` | First consumer; ticket `v2-⑨-1bc` builds the reference impl. | `service="ai_core"` |
| `omnisight-ai-engine` | TBD (sister project; planned 2026-Q3) | Backlog; will adopt this contract at filing time. | `service="ai_engine"` |
| `omnisight-vision-core` | TBD (sister project; planned 2026-Q4, GPU-optional install) | Backlog; will adopt this contract at filing time. | `service="vision_core"` |

The same probe loop, the same gauge name (different label), the same in-process flag dict (different key), the same chain-builder-style consumer (specific to whatever the new service feeds — vision-core would feed a *vision capability chain*, not the LLM chain).

### §5.2 Onboarding a new auxiliary — minimum delta

For an N-th auxiliary service the impl ticket (call it `v2-⑨-Nbc`) MUST:

1. **Reuse** `backend/agents/ai_core_probe.py`'s `probe_once` + `probe_loop` — these are service-agnostic (they take `service_name` and `endpoint` as args; see §3.2 signatures).
2. **Register** the probe loop for the new service in the FastAPI lifespan hook.
3. **Add** a per-service convenience alias in the consumer module (e.g. `VISION_CORE_AVAILABLE = AUX_SERVICE_AVAILABLE.get("vision_core", False)`).
4. **Wire** the corresponding capability-inventory builder branch (e.g. `if VISION_CORE_AVAILABLE: caps.append("capability:enable=vision_core")`).
5. **Wire** the corresponding chain consumer if the service participates in a fallback chain (vision-core would do this for a `vision_pipeline_chain`; ai-engine for whatever it routes).
6. **Update** the AlertBridge rule's `service` label support — the rule template (§4.1) is multi-service already; only the runbook URL may need a per-service variant if the operator-side hygiene differs.

No new file (other than the consumer module's small wiring), no new probe-loop logic, no new alert rule.

### §5.3 What this contract intentionally does NOT cover for new auxiliaries

- **Per-service health-endpoint semantics.** Each auxiliary defines its own `/health`; we only require 2xx-or-not. If a future auxiliary needs deeper health (e.g. "GPU is warm enough"), it owns that decision behind its own `/health`.
- **Per-service alert thresholds different from 24 h.** Out of scope; the contract pins the 24 h `warn` threshold. A service that genuinely needs a different policy is no longer auxiliary by the §1 definition — it has graduated to "non-fatal but lower-tolerance" and needs its own family.
- **Cross-auxiliary dependencies.** If auxiliary A depends on auxiliary B (e.g. ai-engine needs ai-core), each probe is independent. The capability-inventory builder may encode "feature X requires both ai-core AND ai-engine" but the probe contract does not.

---

## §6. Out-of-scope: cross-stack daemon hygiene (Codex P1-5 punt)

### §6.1 What this contract does NOT close

The Codex P1-5 review (audit `docs/audit/codex-reviews/g-a-v2-codex-review-2026-05-14.txt`) correctly flagged that the v1.2 reframe from "cross-stack hygiene" → "optional auxiliary service contract" abandoned the original retro gap about **daemon-resource hygiene** — specifically, the risk that a crashlooping sister container can hammer the shared docker daemon CPU / log volume on a co-tenant host even after the productizer has logically routed around it.

This family **does not close that gap**. It addresses *productizer-side* behaviour when an auxiliary is unavailable; it does not address *host-side* hygiene when a sister project's container is misbehaving.

### §6.2 The placeholder META

The v1.4 parent spec explicitly names the future META ticket that will own the broader cross-project lifecycle policy:

**`META-POST-RC2-CROSS-STACK-HYGIENE`** — to be filed during post-rc2 sprint planning.

That META will cover, at minimum:

- Daemon orphan detection across co-tenant compose stacks (a `restart: always` container in a stack whose top-level compose file has been removed).
- Resource caps (log volume, CPU shares) enforced per-sister-project so a crashlooping wrapper container cannot starve the host.
- A cross-project shutdown contract analogous to Family ⑧'s in-project graceful-shutdown contract, but spanning sister projects on the same host.

### §6.3 Why the punt is correct

Three reasons the punt is the right call for this ticket:

1. **Different abstraction layer.** Family ⑨ is the productizer's contract with an *optional service*. Cross-stack hygiene is the host's contract with *all of its co-tenants*. The two are orthogonal: the productizer can be perfectly conformant with Family ⑨ while a sister container still hammers the host daemon, because Family ⑨ doesn't touch the host daemon at all.
2. **Different stakeholders.** Family ⑨'s stakeholders are the productizer engineer and the operator. Cross-stack hygiene's stakeholders include the operators of *other* sister projects who may not even know omnisight exists on the same host. Conflating the two would require negotiating contract details with parties outside the omnisight engineering org.
3. **Different urgency.** Family ⑨ unblocks the 2026-05-14 `ai_gateway` situation in this sprint. Cross-stack hygiene is a post-rc2 problem because the prerequisite (rc2 ship) is the right time to start defining cross-project policy; doing it now would couple this contract to a much larger negotiation.

### §6.4 Traceability

When `META-POST-RC2-CROSS-STACK-HYGIENE` is filed, its `related:` block MUST link back to:

- This spec §6 as the deferring source.
- The parent spec `sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑨ note about the Codex P1-5 punt.
- The Codex P1-5 review line in `docs/audit/codex-reviews/g-a-v2-codex-review-2026-05-14.txt`.

A grep for `META-POST-RC2-CROSS-STACK-HYGIENE` across `docs/` MUST return at least these three locations after filing; if not, the META has lost its deferred-from breadcrumb and the punt is no longer recoverable.

---

## §7. `v2-⑨-3a-Decision` scope — the one-shot operator decision for the current `ai_gateway` container

### §7.1 What the decision is

The current `ai_gateway` container on the prod host (the deprecated wrapper for omnisight-ai-core) is in a crashloop and has been `docker stop`'d by the operator (post-2026-05-14 state). The operator must record a one-shot decision among:

- **(a)** keep stopped (current state); revisit when omnisight-ai-core work resumes.
- **(b)** delete entirely (`docker rm`); the wrapper is genuinely obsolete; if omnisight-ai-core work resumes, a fresh container is fine.
- **(c)** restore (`docker compose up ai_gateway`) under operator-window supervision because some live state in that container is needed for the next ai-core work cycle.

Recommended path is **(a)** — leave stopped. This costs nothing, retains the container shell for forensic inspection, and lets the next ai-core work cycle decide. **(b)** is appropriate only if the operator has audited the container for state worth preserving and found none. **(c)** is appropriate only if there is an imminent ai-core work resumption and the operator wants the wrapper warm.

### §7.2 What the decision does NOT affect

The decision is **local docker hygiene**. It does NOT affect:

- The contract in §2 (the probe and chain builder work identically whether `ai_gateway` is stopped, deleted, or running — the probe targets `omnisight-ai-core` directly, not the wrapper).
- The capability inventory (same reason).
- The alert routing (same reason).
- Any other ticket in this family.

It DOES affect:

- Local docker-daemon CPU/log usage on this specific host (the Codex P1-5 concern, partially — even a stopped container occupies a small amount of state, but a deleted one occupies none).
- Operator clarity when reading `docker ps -a` on this host.

### §7.3 Why this is its own ticket

It is a **decision**, not a code change. Per `docs/sop/jira-ticket-conventions.md` §11, decisions that affect runtime state but not source code are filed as `class: claude + operator-window` so the operator's act of choosing is the visible artefact (recorded in the ticket comment, the resolution field, and the post-decision `docker ps` snapshot). No source change is needed.

### §7.4 Hand-off

`v2-⑨-3a-Decision`'s AC must reference §7.1 of this spec by anchor and pick exactly one of (a)/(b)/(c) with an operator-facing one-line justification recorded in the ticket comment.

---

## §8. Cross-stack delegation map — which v2 families this family depends on (and does NOT)

The family-⑨ contract sits inside a wider set of v2 contracts. This map names exactly what it depends on so a future spec amendment cannot accidentally couple this family to a family that does not own the relevant invariant.

### §8.1 Hard dependencies (this family WILL NOT WORK without these)

| Dependency | What it gives us | Citation |
|---|---|---|
| v2-A1 (defense_contract schema) | The `D1` + `D2` enum values used in §4.1 `defense_dimension` label. | parent spec §4 row 1 |
| v2-AlertBridge-1a (framework contract) | The `severity`/`area`/`family`/`defense_dimension` label vocabulary, the `__bridge.critical_labels` block shape, the `SEVERITY_CHANNEL_MAP` swap-time guarantee for §4. | `2026-05-16-v2-alertbridge-framework-contract.md` §1, §3, §4, §7 |
| v2-AlertBridge-1bc (framework impl) | The bridge that consumes §4's alert rule (without it, the rule exists but nothing routes it). | parent spec — shared infra row |

### §8.2 Soft dependencies (this family benefits from but does not require)

| Dependency | What it benefits | If absent |
|---|---|---|
| v2-⑤-Image (image-vs-DB drift) | If `ai_gateway` is stale-image and the operator runs §7's restore path (c), the drift family would catch it. | The contract still works; (c) just risks a stale-image restart that the operator has to manually verify. |
| v2-⑩ (Runner Defense Contract) | If the runner picks up `v2-⑨-Integration`, the runner's own substrate is more robust. | The chaos test still runs; it just shares pickup reliability with every other v2 ticket. |
| v2-⑧ (Graceful shutdown) | The probe loop is cancelled at app shutdown like any other background task; Family ⑧'s contract makes that cancellation graceful rather than abrupt. | Probe loop cancellation is best-effort; an abrupt SIGKILL leaves the loop's deque in a non-issue state because the process is going down anyway. |

### §8.3 NON-dependencies (explicitly NOT depended on, and why)

| NOT depended on | Why explicitly declared |
|---|---|
| v2-⑥ (Image-vs-DB drift, runtime side) | Family ⑥'s rescue path is for the productizer's own image / DB; auxiliary services are by definition out of that scope. |
| v2-⑦ (Allowlist single-source) | The probe targets a docker-network-internal endpoint; it does not traverse the public-path allowlist. The probe runs *inside* the productizer; it does not need an exemption. |
| `META-POST-RC2-CROSS-STACK-HYGIENE` | The punt in §6. Calling it out as a non-dependency here is what keeps the punt structurally valid — if this family later grew a dependency on cross-stack hygiene, the punt would have to be reopened. |

### §8.4 Downstream-of-this-family

Five downstream tickets cite anchors in this doc:

| Downstream ticket | Cites this spec § | What the ticket implements |
|---|---|---|
| `v2-⑨-1bc` | §2, §3 (full) | `backend/agents/ai_core_probe.py` + gauge + flag + tests per §3.6 |
| `v2-⑨-2bc` | §2.4, §3.5 | `llm_fallback_chain` builder consumer + capability-inventory propagation |
| `v2-⑨-3a-Decision` | §7 | One-shot operator decision for the `ai_gateway` container |
| `v2-⑨-Integration` | §3.4 (flap), §3.5 (chain builder), §4 (alert), §5 (extensibility test) | Chaos test: synthetic up/down/up cycle; chain transitions within 1 probe cycle; alert fires `warn` after 24 h |
| (future) `v2-⑨-Nbc` for a new auxiliary | §5 (full) | Adopts the same pattern for omnisight-ai-engine / omnisight-vision-core |

If a downstream ticket's AC contains the literal phrase *"per `2026-05-16-v2-family9-aux-service-contract.md` §X.Y"*, the section anchor must remain stable. Renumbering this spec requires a v1.1 amendment with a redirect table (§9.2 changelog).

---

## §9. Open questions, changelog, hand-off

### §9.1 Open questions (intentionally left to downstream)

These were considered and *deferred* — they don't belong in the spec but are flagged so downstream tickets can pick them up without re-discovery:

1. **Q9.1.1 — Should the probe loop emit a Prometheus *counter* (probe success / failure count) in addition to the gauge?** *Recommended at `v2-⑨-1bc`:* yes, `omnisight_aux_service_probe_total{service,result}` as a low-cost counter. The gauge answers "is it up now?"; the counter answers "how often has it failed in the last hour?" Both are cheap; the test list in §3.6 should grow one assertion if `v2-⑨-1bc` adopts.
2. **Q9.1.2 — Does the per-service convenience alias (`AI_CORE_AVAILABLE`) leak into router code, or stay inside the consumer module?** *Recommended at `v2-⑨-2bc`:* stay inside the consumer module. Router-level code reads the capability inventory, not the per-service alias. The alias exists for the chain builder's own readability.
3. **Q9.1.3 — Does the probe loop survive a settings reload that changes `OMNISIGHT_AI_CORE_PROBE_URL`?** *Resolution at `v2-⑨-1bc`:* yes — the loop reads the setting on each cycle, not once at start. Settings reload is a no-op for the loop's lifecycle.
4. **Q9.1.4 — Does the operator-window `OMNISIGHT_AUX_SERVICE_DISABLE` env var (§4.4) accept globs or only exact names?** *Resolution here, NOT deferred:* exact names only. Globbing would be a foot-gun (`*` would disable every future auxiliary the operator forgets about). The exact-names list is short and operator-readable.
5. **Q9.1.5 — Does the contract cover *non-HTTP* auxiliary services (gRPC, Unix-socket, named-pipe)?** *Resolution here:* the probe interface in §3.2 names HTTP because both candidates today (ai-core, ai-engine) are HTTP. The contract permits future per-service variants of `probe_once` so long as the *output* (a boolean per cycle into the same gauge + flag) is preserved. A future gRPC auxiliary would use a sibling `probe_once_grpc` and register it the same way; this is a `v2-⑨-Nbc` decision when the first non-HTTP auxiliary lands.

### §9.2 Changelog

| Version | Date | Author | Change |
|---|---|---|---|
| v1 | 2026-05-16 | claude-bot (OP-1155) | Initial spec. Reframe LOCKED per operator 2026-05-14 Q3 (auxiliary = optional, not hard dependency). Codex P1-5 punt to `META-POST-RC2-CROSS-STACK-HYGIENE` explicit per parent spec v1.4. |

### §9.3 Hand-off block (for the next ticket)

The next ticket to pick up Family ⑨ work is **v2-⑨-1bc** (per parent spec §3 Family ⑨ row 2). That ticket's AC should reference:

- §2 (the five-obligation minimum contract).
- §3 (the `ai_core_probe` reference impl: module location, public names, endpoint/retry/timeout, flap detection, the chain consumer, the test list).
- §4 (the AlertBridge rule shape — the rule itself ships as part of `v2-⑨-1bc` once the gauge is wired, or splits to a sibling `v2-⑨-AlertRule` at `v2-⑨-1bc` filing time).

After `v2-⑨-1bc` ships the probe and gauge:

- **v2-⑨-2bc** consumes the in-process flag in the chain builder per §3.5 and propagates state to the capability inventory per §2.4.
- **v2-⑨-3a-Decision** records the one-shot operator decision for the current `ai_gateway` container per §7.
- **v2-⑨-Integration** runs the chaos test per §3.6's last paragraph and the parent spec's `v2-⑨-Integration` row; tier:M per SOP §3.2 because it spans probe + fallback chain + capability inventory + alert routing.

When `META-POST-RC2-CROSS-STACK-HYGIENE` is filed (post-rc2), §6.4's traceability requirement applies: the new META must link back to this spec §6 as the deferring source.

---

## §10. Boundary justification (spec ticket boundary block)

This ticket (OP-1155 / v2-⑨-1a) declares the following boundaries per `docs/sop/jira-ticket-conventions.md` §11:

```yaml
scope_components: [docs]
destructive_op_classes: []
external_side_effect: none
filesystem_writes:
  - docs/sprint-s12/2026-05-16-v2-family9-aux-service-contract.md  (NEW)
  - docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md  (MINOR: x-ref)
runtime_paths_changed: 0
db_migrations: 0
ci_yaml_changed: 0
prod_compose_changed: 0
secrets_touched: []
```

**Exemption from the area-block list (out-of-area: backend / db / devops / embedded / frontend / security / tests / tooling):** the area-block list in the ticket envelope excludes work in those areas because this is a docs-only ticket. The work is `area:docs` only; the spec describes future backend / alerting contract surfaces but does not modify any backend / alerting / db / devops / embedded / frontend / security / tests / tooling file. The downstream `v2-⑨-1bc`, `v2-⑨-2bc`, and `v2-⑨-Integration` will be re-evaluated under the full area-block set at filing time (backend + tests at minimum; possibly deploy for the alert rule's prometheus rules file).
