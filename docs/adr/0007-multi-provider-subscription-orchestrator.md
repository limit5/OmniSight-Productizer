# ADR 0007 — Multi-Provider Subscription Orchestrator + Cost Calculator

**Status**: Accepted (2026-05-06)

**Targeted release**: v0.4.0 (2026-06-02 cut, per [governance migration plan](../../README.md))

**Authority**: This ADR establishes Priority MP — a new top-level priority alongside BP / WP / KS / FX series.

---

## Context

OmniSight runs 1 LLM-driven agent per `auto-runner-codex.py` invocation today. The agent uses **OpenAI Codex subscription** (~$200/mo Codex Pro) which has:

- 5-hour rolling cap (resets per-token, per-window)
- Weekly cap (also token-based)
- After cap hit: hard block until reset

Cumulative session data (this 2026-05-06 session): **13 epics merged, ~95 commits, ~5 hours wall time**. Per-epic cost = $200 / ~30 epics-per-month = **~$6.67 per epic** with subscription. Equivalent API call estimate (~14k tokens/epic × $3-15/M tokens) = **~$8-12 per epic**. **Subscription dominates economically** for OmniSight's task profile.

But subscription has a ceiling — 5h / weekly caps mean throughput ramp impossible past the included usage. Three forces:

1. **Operator fragmentation**: parallel work blocked by single-provider cap. If user needs 4 epics done in 2 hours, single-codex can't deliver — but Anthropic Claude Code Pro/Max + OpenAI Codex + Gemini Code Assist + Grok could in parallel.
2. **Provider outage**: any single vendor going down halts development pipeline. Multi-vendor = circuit breaker.
3. **Economic optimisation**: some tasks cheaper on subscription; some better on API (e.g., when subscription cap is exhausted but work is queued — paying $5 to ship now beats waiting 2 hours).

User asked (2026-05-06): "Anthropic + OpenAI + Google + xAI 訂閱版 CLI 都接進來 + cap-aware switching + cost calculator showing time vs $ tradeoff before dispatch."

---

## Decision

Build **Priority MP — Multi-Provider Subscription Orchestrator** with these properties:

### Shared `agent_class` schema

`config/agent_class_schema.yaml` is the canonical machine-readable source for every
`agent_class` value consumed by MP routing, MP cost estimation, TODO `[class:X]`
labels, and ADR 0008's RPG `class` field. ADR 0007 and ADR 0008 may describe how
each system uses the values, but they must not carry a second hand-maintained value
list. Any `agent_class` addition, rename, or removal must update the YAML schema and
then update both ADRs' prose / tables that reference that class in the same change.

### Provider coverage

**MVP (v0.4.0)** — 2 first-class providers with full quota tracking + auto-switch:
- Anthropic Claude Code (Pro / Max 5x / Max 20x) via `claude` CLI
- OpenAI Codex (Plus / Pro / Business) via `codex` CLI

**Structural slot reserved for v0.5.0+** — design supports them but no impl in v0.4.0:
- Google Gemini (Advanced + Code Assist) — adapter shell + capability matrix entries
- xAI Grok — adapter shell + capability matrix entries

Rationale: 2026-05-06 ecosystem maturity — Anthropic + OpenAI are first-class agentic CLIs; Gemini's CLI is more chat-mode; xAI's CLI is experimental. Wait 2-3 months for the latter two before commit.

### Architecture: Provider Orchestrator (backend)

```
backend/agents/
├── provider_orchestrator.py     # central registry + routing
├── provider_quota_tracker.py    # 5h-rolling + weekly cap state per provider
├── cost_estimator.py            # token + time prediction per (task, provider)
├── provider_adapters/
│   ├── anthropic_subscription.py
│   ├── openai_subscription.py
│   ├── gemini_subscription.py   # placeholder for v0.5.0
│   ├── xai_subscription.py      # placeholder for v0.6.0
│   ├── anthropic_api.py         # BYOK API fallback
│   ├── openai_api.py
│   └── google_api.py
└── routing_policy.py            # cap-hit handling + provider selection
```

**Key components**:

- **QuotaTracker** persists state to PG (alembic 0198 — `provider_quota_state` table). Tracks `rolling_5h_tokens_used`, `weekly_tokens_used`, `last_cap_hit_at`, `last_reset_predicted_at` per provider per worker (uvicorn workers all read same PG row, so cap state is consistent).

- **CostEstimator** combines:
  - Per-task token prediction from historical data (this session's 13-epic baseline)
  - Per-provider rate (`$0` for subscription within cap; `~$3-15/M tokens` for API)
  - Per-provider wall-time prediction (per `agent_class` label)

- **RoutingPolicy** runs at task-dispatch time:
  - Reads task's `agent_class` / `tier` / `complexity` labels
  - Reads each provider's quota state
  - Returns ordered list of acceptable providers
  - On cap hit during execution: pauses task → migrates to next acceptable provider → resumes (task-boundary switch only; mid-task switch is hard fail-out)

### Architecture: Cost Calculator UI (frontend)

User-facing UX is the **Provider Constellation** (design A) with **v3 mixed onboarding** (design C wizard for first-time + design A overlay tooltips + design B war room toggle for power users).

**Provider Constellation (A) — daily mode**:
- Center: Project Core (task batch + total estimate)
- 4 corners: provider Energy Spheres (reuse `OAuthEnergySphere` component pattern from Login)
- Sphere visual encoding:
  - Size = predicted token allocation
  - Color = quota state (green ≥70%, yellow 30-70%, red <30%, gray = no subscription)
  - Pulse rate = real-time activity
- Connection beams between selected providers and Project Core
- Slider: Cheap ↔ Fast → spheres reflow live as user drags

**War Room (B) — power-user toggle**:
- 4 detachable panels: Quota Tracker / Cost Calculator / Tasks Backlog / Tradeoff Slider
- Drag-rearrangeable
- Animated connection lines between panels
- Mobile (<1024px) falls back to (A) with toast "War Room desktop only"

**Onboarding (C wizard 2-step + A overlay tooltips) — first-time path**:
- Step 1/2: Welcome — 4 spheres orbit-converge animation
- Step 2/2: "Open the workshop" — 4 spheres fly to corners, fade into (A)
- (A) overlay: 6 spotlight tooltips on the actual UI elements (~30 sec)
- Total first-time: ~70 sec (skippable any time)
- Subsequent: (A) bare, ~10 sec to dispatch
- Help menu → Replay tour resets the flag

### Trigger from Dashboard

```tsx
// app/page.tsx
<Button onClick={openMP} className="...">
  ⚡ Plan with All Providers
</Button>
```

→ full-screen modal (`inset-2 backdrop-blur-2xl`) → branch on `user_preferences.seen_mp_tour`.

### Cross-system integration

- **BP.F** (Mixed-mode Model Mapping) provides per-Guild model preference; orchestrator consults it when picking provider
- **BP.C** (T-shirt Sizer) provides task complexity → cost estimator uses it as input feature
- **Z series** (LLM Provider Observability) provides rate-limit header parsing — reuse for cap detection
- **WP.7** (Feature Flag Tiered Registry) — `OMNISIGHT_MP_*` knobs per provider on/off
- **Q.2** (Multi-device parity) — `user_preferences` infra for `seen_mp_tour` flag

---

## Consequences

### Positive

- **Throughput ceiling lifted**: parallel multi-provider dispatch unlocks 2-4× wall-time speedup on large epics (cap-bound today)
- **Provider outage resilience**: any single vendor down → orchestrator routes to alternative
- **Economic optimisation**: 90% subscription routing + 10% API spillover when cap exhausted = cheaper than pure-API model
- **Cost transparency**: user sees $ + time tradeoff before dispatch, no surprises
- **Capability matching**: BP.F / BP.C labels feed routing — best provider per task type
- **Visual continuity**: reuses Login's OAuth energy sphere visual language → consistent OmniSight aesthetic

### Negative

- **Subscription account proliferation**: requires Claude Pro/Max + Codex Plus/Pro accounts (operator setup cost). Future: Gemini Advanced + Grok subscription too
- **Cap-detection complexity**: each vendor has different cap-signaling format; abstraction must handle 4× edge cases
- **Mid-task switching not supported**: cap hit mid-execution = current task fails out; must restart on different provider (loses agent context). Mitigation: prefer task-boundary switching
- **First-time UX cost ~70 sec**: (C) wizard + (A) overlay is not optional unless user explicitly skips. May friction urgent-mode use
- **Frontend animation perf**: 4 sphere + 4 beam at 60fps = GPU-bound; respect `prefers-reduced-motion` (degrade to static)

### Neutral

- **Drift-guard maintenance**: provider list at 4 must stay in sync between frontend `lib/providers.ts` + backend `SUPPORTED_PROVIDERS` + orchestrator registry — drift-guard test enforces
- **Subscription cost predictability**: $200 (Codex) + $200 (Claude Max) = $400/month operator cost; visible in Settings billing summary
- **Onboarding can be re-played**: Help menu → "Replay multi-provider tour" resets seen_mp_tour flag

---

## Phased rollout (Priority MP work waves)

```
Week 1 (2026-05-06 → 2026-05-13)  — MP.W1-W3
  ├── MP.W1 Backend orchestrator + quota tracker + alembic 0198
  ├── MP.W2 Cost estimator + token prediction
  └── MP.W3 Anthropic + OpenAI subscription wiring

Week 2 (2026-05-13 → 2026-05-20)  — MP.W4-W7
  ├── MP.W4 Provider Constellation (A) UI + spheres + slider
  ├── MP.W5 Onboarding (C wizard 2-step + A overlay tooltips)
  ├── MP.W6 War Room (B) power-user toggle
  └── MP.W7 Dashboard trigger button + modal integration

Week 3 (2026-05-20 → 2026-05-27)  — MP.W8-W12
  ├── MP.W8 SSE real-time quota update channel
  ├── MP.W9 BP.F + BP.C integration (capability matching, T-shirt size)
  ├── MP.W10 Z-series rate-limit header reuse
  ├── MP.W11 Help menu replay tour
  └── MP.W12 Mobile responsive degradation

Week 4 (2026-05-27 → 2026-06-02)  — MP.W13-W16 + v0.4.0 cut
  ├── MP.W13 Gemini structural slot (placeholder, no impl)
  ├── MP.W14 xAI structural slot (placeholder, no impl)
  ├── MP.W15 Tests + drift guards
  └── MP.W16 docs/operations/multi-provider-setup.md
                                  → v0.4.0 release branch cut
```

---

## Vendor capability matrix (for routing policy)

| Provider | Subscription tier | Context window | Agentic loop | Cap signal | MVP? |
|---|---|---|---|---|---|
| Anthropic Claude | Pro / Max 5x / Max 20x | 1M | ★★★★★ | `usage_exceeded` 429 + retry-after | ✅ |
| OpenAI Codex | Plus / Pro / Business | 200K | ★★★★☆ | `rate_limit_exceeded` 429 + reset_at | ✅ |
| Google Gemini | Advanced / Code Assist | 2M | ★★★☆☆ | per-query rate (no rolling cap) | placeholder |
| xAI Grok | SuperGrok | unclear | ★★☆☆☆ | undocumented | placeholder |

---

## Cost prediction model

Initial baseline from this session's 13 epics:

| `agent_class` label | Avg tokens/item | Avg wall-time/item | $ at API rate |
|---|---|---|---|
| `subscription-codex` | ~12k | 4 min | ~$0.05 |
| `api-anthropic` (predicted) | ~25k larger context | 6 min | ~$0.20 |
| `api-openai` (predicted) | ~15k | 5 min | ~$0.08 |

Per-tenant calibration: model adjusts based on actual user task profile (e.g., "this user's BP epic items run 30% slower than baseline → add 30% pad to predictions").

---

## Risks

- **R-MP.1 — Cap signal false negatives**: vendor doesn't surface cap hit until task fails. Mitigation: pre-flight quota check + conservative threshold (refuse new task if predicted_tokens > remaining_quota × 0.8)
- **R-MP.2 — Cost estimator drift**: real cost diverges from estimate as user task profile shifts. Mitigation: per-tenant continuous calibration + warn if est-vs-actual diverges >50%
- **R-MP.3 — Provider account expiry**: Pro/Max subscription expires mid-flight. Mitigation: monitor account status via vendor API; alert operator before lapse
- **R-MP.4 — Constellation animation perf on slower devices**: spheres + beams at 60fps. Mitigation: `prefers-reduced-motion` static fallback + IntersectionObserver to pause off-screen
- **R-MP.5 — First-time onboarding skip rate**: users skip (C) wizard, miss critical concept. Mitigation: log skip rate in audit; if >50% skip, simplify to v2.5 (drop wizard, only overlay)

---

## Related

- [ADR 0001 — Five-branch Git Flow](0001-five-branch-gitflow.md) — v0.4.0 milestone cut from develop
- [ADR 0005 — Tier S/M/L/X authority](0005-tier-authority-levels.md) — agent_class labels feed orchestrator routing
- [ADR 0008 — Agent RPG class & skill leveling](0008-agent-rpg-class-skill-leveling.md) — shares `agent_class` schema (MP.W0 source of truth); MP.W17 (upfront tool baseline) ↔ RPG.W13 (runtime proficiency leveling) are static/adaptive complements
- [Priority BP.F](../../TODO.md) — Mixed-mode Model Mapping (orchestrator consumer)
- [Priority BP.C](../../TODO.md) — T-shirt Sizer (cost estimator input)
- [Priority Z series](../../TODO.md) — LLM Provider Observability (rate-limit header reuse)
- [Priority MP](../../TODO.md) — this ADR's implementation plan (W0-W17)
- 2026-05-06 deep audit — D5 observability gap (Sentry/OpenTelemetry) feeds MP debugging surface

---

## 2026-05-06 amendment — added W0 (prereq) + W17 (tool baseline)

After ADR 0008 (Agent RPG) landed, two gaps became visible in the original W1-W16 plan:

- **MP.W0** (`agent_class` label re-slice, blocking prereq before W1) — W1 orchestrator routing + W2 cost estimator seed both consume `agent_class` labels. Without re-slicing the existing TODO + 13 historical epics with these labels, W1 cannot run end-to-end tests and W2 cannot seed baseline data. ADR 0008 RPG `class` field shares the same canonical schema, so W0 establishes a single source of truth.
- **MP.W17** (MCP/A2A upfront tooling robustness, parallel to W4-W16) — distinct from RPG.W13 (runtime proficiency leveling). W17 = "out-of-box baseline quality" (audit existing servers, harden top-10 tools, add 6 missing servers, standard wrapper, A2A envelope schema, system-prompt tool catalog). RPG.W13 = "ongoing leveling discovered through actual task execution + new tech adoption". Both are kept because static-baseline-quality and runtime-adaptive-leveling solve different halves of tooling reliability — collapsing them would force the runtime layer to also fix baseline gaps, slowing leveling progression.

---

## Decision drivers (from operator 2026-05-06 conversation)

1. **MP into v0.4.0** (vs v0.5.0): operator prefers earlier ship despite +2 weeks milestone slip — economic ROI on cap-aware routing is too high to defer
2. **Anthropic + OpenAI MVP**: structural slot reserved for Gemini + xAI but no impl yet (vendor maturity)
3. **Cost calculator UI**: Provider Constellation (A) on dashboard popover, NOT new page — visual continuity with Login UX is high value
4. **Onboarding flow**: v3 mixed (C wizard 2-step + A overlay + B power toggle) — best balance of cinematic first-time + low-friction subsequent + power-user expert mode
