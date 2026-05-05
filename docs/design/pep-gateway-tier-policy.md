# PEP Gateway — Tier-aware Policy

> **BP.S.4 deliverable.** This document closes the documentation gap left by
> the *already-shipped* tier-aware integration inside
> [`backend/pep_gateway.py`](../../backend/pep_gateway.py) (R0 #306). Tiers
> have been load-bearing in the PEP path since R0 landed; what was missing
> was a reader-facing description of (a) which tier strings the gateway
> actually understands today, (b) how `classify()` orders its rules, (c)
> how the runtime `tier=...` parameter relates to the BP.S.1 `SandboxTier`
> enum and the BP.S.2 operator narrowing layer, and (d) the explicit list of
> things this layer does **not** yet do (yaml loader, case normalisation,
> etc.) so future readers do not assume coverage that is not there.
>
> This row is scoped to **pure documentation, zero runtime change**, in
> line with the BP.S epic header. The companion drift-guard tests are
> BP.S.6's responsibility.

---

## 0. ⚠️ What this doc is **not**

* **Not** a redesign proposal. The tier strings, whitelist tables, and rule
  ordering described below are exactly what `backend/pep_gateway.py` ships
  today. Anything you read here that is not in the code is either marked
  **(future)** or is wrong — please file an issue.
* **Not** a substitute for the per-Guild compliance audit. That belongs to
  [`sandbox-tier-audit.md`](sandbox-tier-audit.md) (BP.S.3). This doc is the
  *runtime enforcement* view; the audit doc is the *standards-mapping*
  view. They overlap on the 4-tier names but answer different questions.
* **Not** a claim that gVisor user-space-kernel isolation is in place at
  runtime. Tiers are enum-named in code; the Phase U gVisor adoption
  (BP.W3.13) is the row that makes the isolation claim load-bearing. See
  R12 callout in `sandbox-tier-audit.md` §0.

---

## 1. Where the tier parameter enters

Every tool call routed through the agent runtime hits exactly one
function:

```python
# backend/pep_gateway.py
async def evaluate(
    *,
    tool: str,
    arguments: dict[str, Any],
    agent_id: str = "",
    tier: str = "t1",
    guild_id: str | None = None,
    propose_fn: Callable[..., Any] | None = None,
    wait_for_decision: Callable[[str, float], Awaitable[Any]] | None = None,
    hold_timeout_s: float = 1800.0,
) -> PepDecision:
```

The caller (`tool_executor_node`, `routers/installer.py`,
`web_sandbox_pep`, etc.) passes the `tier=...` kwarg as a **lower-case
string**: `"t1"` / `"t2"` / `"t3"`. There is also a synonym `"networked"`
which is treated as `"t2"`. Anything else falls through to the most
restrictive `t1` whitelist (see §3.4).

> **Note on case.** The caller-facing strings are `t1` / `t2` / `t3`.
> The BP.S.1 `SandboxTier` enum uses **upper-case** members `T0` / `T1` /
> `T2` / `T3` for stable wire / log / metric labels. These two
> conventions are intentionally distinct today — see §6 for the
> integration roadmap. Callers must keep using the lower-case form when
> talking to `pep_gateway.evaluate(...)`.

---

## 2. The decision dataclass

Every `evaluate()` call returns a `PepDecision`:

| Field           | Type                  | Meaning                                                                                              |
| --------------- | --------------------- | ---------------------------------------------------------------------------------------------------- |
| `id`            | `str`                 | `pep-<10-hex>` ulid-ish id; entered into audit + recent-decisions ring + held-registry.              |
| `ts`            | `float`               | Unix seconds at the moment of `evaluate()`.                                                          |
| `agent_id`      | `str`                 | Caller-supplied agent id (e.g. `"operator:alice@…"` for the installer router).                       |
| `tool`          | `str`                 | Tool name as registered in `TOOL_MAP`.                                                               |
| `command`       | `str`                 | Flattened command string used by the regex match (see §4).                                           |
| `tier`          | `str`                 | The `tier=` kwarg that the caller passed in. Recorded verbatim.                                      |
| `guild_id`      | `str`                 | Optional Guild slug. Empty string preserves the legacy tier-only path; non-empty values gate inheritance through `sandbox_tier`. |
| `action`        | `PepAction`           | `auto_allow` / `hold` / `deny`.                                                                      |
| `rule`          | `str`                 | Which rule fired: e.g. `tier_whitelist`, `tier_unlisted`, `rm_rf_root`, `deploy_prod`.               |
| `reason`        | `str`                 | Human-readable explanation; surfaced in the toast + audit.                                           |
| `impact_scope`  | `str`                 | `"local"` / `"prod"` / `"destructive"`.                                                              |
| `decision_id`   | `Optional[str]`       | Decision-Engine proposal id when `action == hold`; `None` otherwise.                                 |
| `degraded`      | `bool`                | `True` when the circuit breaker is open and the call fell back to fail-closed deny.                  |

The `tier` field is **observability**, not control: by the time
`evaluate()` returns the field is just echoed back so audit and SSE
consumers can filter by tier. The actual enforcement happened earlier in
`classify()`.

---

## 3. The four tier strings the gateway recognises

### 3.1 The whitelist tables

There are three frozen sets in `backend/pep_gateway.py`:

* **`TIER_T1_WHITELIST`** — filesystem (sandboxed read/write), git
  local-only verbs (`status`/`log`/`diff`/`branch`/`add`/`commit`/
  `checkout_branch`/`remote_list`), planning + reporting tools
  (`get_platform_config` / `register_build_artifact` /
  `generate_artifact_report` / `get_next_task` / `update_task_status` /
  `add_task_comment`).
* **`TIER_T2_EXTRA`** — outbound git verbs (`git_push`,
  `git_add_remote`), pull-request creation (`create_pr`), Gerrit
  integration (`gerrit_get_diff` / `gerrit_post_comment` /
  `gerrit_submit_review`), hardware probe sniffing
  (`check_evk_connection`, `list_uvc_devices`).
* **`TIER_T3_EXTRA`** — `run_bash` and `deploy_to_evk`.

### 3.2 The cumulative-whitelist function

```python
# backend/pep_gateway.py
def tier_whitelist(tier: str) -> frozenset[str]:
    t = (tier or "t1").lower()
    if t in ("t1",):
        return TIER_T1_WHITELIST
    if t in ("t2", "networked"):
        return TIER_T1_WHITELIST | TIER_T2_EXTRA
    if t in ("t3",):
        return TIER_T1_WHITELIST | TIER_T2_EXTRA | TIER_T3_EXTRA
    return TIER_T1_WHITELIST  # unknown tier → most restrictive
```

The whitelist is **cumulative**: T2 includes everything T1 allows; T3
includes everything T2 allows. This matches the conceptual ladder
"strict sandbox → networked sandbox → hardware bridge" — anything
runnable in a less-privileged tier remains runnable in a more-privileged
one. If you find yourself wanting a tool that is allowed in T1 but
not T3, the right answer is almost certainly a destructive / production
rule (§4) rather than a tier carve-out.

### 3.3 No T0 entry (deliberate)

There is no `TIER_T0_*` whitelist. T0 is the **control plane** (BP.S.1
`SandboxTier.T0`) — orchestrator state, audit chain, OpenRouter gateway.
T0 callers are expected to *never invoke `pep_gateway.evaluate()` with
`tier="t0"`* because T0 is forbidden from executing AI-generated
payloads in the first place. The gateway therefore has no T0 policy
table; if a `tier="t0"` value somehow reaches `tier_whitelist()` it is
treated as unknown and falls back to the most-restrictive T1 whitelist
(§3.4). This is intentional fail-closed behaviour, not a bug.

### 3.4 Unknown / invalid tier strings

* **`None`** → coerced to `"t1"`.
* **Empty string `""`** → coerced to `"t1"`.
* **Any unrecognised non-empty string** (`"t0"`, `"foo"`, `"T1"`,
  `"strict"`) → returns `TIER_T1_WHITELIST`. There is no error. The
  rationale is fail-closed: a typo in the caller should not silently
  upgrade a tool to a wider whitelist; it should *narrow* it.

This is also why callers must spell the tier as lower-case `"t1"` /
`"t2"` / `"t3"` exactly. Upper-case `"T1"` does **not** match the
current `lower()`-only path's branches (`"t1"` / `"t2"` / `"networked"` /
`"t3"`); upper-case input arrives as `"t1"` after the `.lower()` call,
so it works by coincidence today, but the §6 integration with
`SandboxTier.T1` will normalise this — do not rely on the coincidence.

---

## 4. How `classify()` orders its rules

`classify(tool, arguments, tier) -> (action, rule, reason, impact_scope)`
runs four stages in this order, returning on the first match:

1. **Destructive patterns** → `deny`. The pattern table is
   `_DESTRUCTIVE_RULES`: `rm -rf /`, `rm -rf /*`, `chown -R … /`,
   `chmod -R 777 …`, `dd if=/dev/zero|urandom`, `dd of=/dev/sd|nvme|…`,
   `mkfs… /dev/…`, fork-bomb (`:(){ :|:& };:`), `shutdown`/`reboot`/
   `halt`/`poweroff`, `curl … | bash`, `wget … | bash`, redirect-to-
   block-device, `DROP DATABASE`, `DROP SCHEMA public CASCADE`,
   `terraform destroy`, `git push --force`. **Tier-independent.** A
   destructive rule is a destructive rule even in T3.
2. **Production-scope patterns** → `hold`. The pattern table is
   `_PROD_HOLD_RULES`: `deploy.sh prod`, `kubectl --context production`,
   `kubectl -n production`, `terraform apply`, `helm upgrade
   --namespace production`, `ansible-playbook prod`, `aws --profile
   production`, `gcloud --project …prod…`, `psql -h …prod…`,
   `docker push …:prod`. **Tier-independent.** A prod-scope rule is a
   prod-scope rule even in T3 — sudo permission inside the sandbox does
   not grant operator-blast-radius authority outside.
3. **Tier whitelist membership** → `auto_allow` if `tool` is in the
   tier's cumulative whitelist (§3.2). The fired rule is
   `tier_whitelist`, the impact scope is `local`.
4. **Fall-through** → `hold` with rule `tier_unlisted`. The tool is not
   destructive (it would have been denied), not production-scope (it
   would have been held with a prod rule), but it is also not on the
   tier's whitelist. It is held so the operator can wave it through —
   not denied, because allow-list-only deny is too brittle for an
   evolving LLM tool surface. Operators expand the tier whitelist over
   time as patterns repeat.

**Order matters.** A `git push --force` to a prod-scope branch fires
`git_push_force` (deny) before it ever reaches `kubectl_prod_context`;
a `kubectl --context production get pods` fires `kubectl_prod_context`
(hold) before tier whitelist membership is even consulted. The rule
ordering is the *priority*, not just a code path.

---

## 5. Tier and the HOLD path

When `classify()` returns `hold`, `evaluate()` raises a Decision-Engine
proposal via `_propose_hold()`. The DE proposal carries the tier
verbatim in its payload so the operator UI can render "tool X (tier
t2)" and the audit row can later be filtered by tier. The HOLD outcome
is one of:

* **`approved`** → the decision becomes `auto_allow` but `decision_id`
  is preserved so the UI can cross-reference. The tool call proceeds.
* **`rejected`** → the decision becomes `deny`. The tool call returns
  `[BLOCKED]` to the agent. The agent typically picks a different
  verification path or skips the step.
* **`timeout`** → the decision becomes `deny` (fail-closed). The
  default deadline is `hold_timeout_s=1800.0` (30 min); the installer
  router uses 600 s; web-sandbox preview uses its own.

Tier does not change the timeout, the proposal shape, or the operator
UX. It is purely an *attribute of the decision*, not a *path through
the decision logic*.

### 5.1 Circuit breaker

If `propose_fn` or `wait_for_decision` raises, the breaker counts the
failure. After N consecutive failures it opens and stays open for a
cool-down window. While open, the gateway short-circuits HOLD requests
to a **fail-closed deny** (`degraded=True`, `action=deny`,
`reason="PEP circuit open — fallback deny"`). Tier still travels with
the decision so operators investigating an outage can filter by tier
the same way they filter audit rows by tenant.

The breaker is global — it does **not** keep per-tier state. A flood of
T1 HOLD failures will trip the breaker for T2 and T3 callers too. This
is by design: the failure modes (DE unavailable, audit log unavailable)
are not per-tier; they are infrastructure-wide.

---

## 6. Relationship to BP.S.1 `SandboxTier` and BP.S.2 yaml policy

This is the documentation gap that prompted BP.S.4. There are
**two coexisting tier vocabularies** in the codebase today:

| Surface                                  | Tier label form        | Owner             | Today's role                                                                |
| ---------------------------------------- | ---------------------- | ----------------- | --------------------------------------------------------------------------- |
| `pep_gateway.classify(tier=…)`           | lower-case `"t1"`/`"t2"`/`"t3"`/`"networked"` | R0 #306 (this doc) | Live runtime enforcement at the tool-call boundary.                         |
| `backend/sandbox_tier.SandboxTier`       | upper-case `T0`/`T1`/`T2`/`T3`               | BP.S.1            | Declarative enum used by Guild × Tier admission matrix + audit doc + yaml. |
| `configs/sandbox_tier_policy.yaml`       | upper-case `T0`/`T1`/`T2`/`T3`               | BP.S.2            | Operator narrowing of the matrix at deploy time. **No loader yet.**         |
| `docs/design/sandbox-tier-audit.md`      | upper-case `T0`/`T1`/`T2`/`T3`               | BP.S.3            | Phase D auxiliary compliance reference.                                     |

These are not two implementations of the same thing — they are *two
adjacent layers* of the same control:

* `SandboxTier` is the **structural admission** layer. It says *"can
  this Guild be dispatched into this tier at all?"* and is consulted by
  `assert_admitted(guild, tier)` in `backend/sandbox_tier.py`. Phase D
  auxiliary compliance modules cite it.
* `pep_gateway.evaluate(tier=…)` is the **per-call enforcement** layer.
  It says *"given that this caller is already inside Tier X, is the
  specific tool call they just made auto-allowed, held, or denied?"*

A request that is structurally inadmissible (e.g. `auditor` Guild
trying to dispatch into Tier 1) should ideally be refused **before** it
reaches `pep_gateway.evaluate()` by `assert_admitted`. BP.D.8/B1 also
adds a PEP-side fail-closed check for callers that pass `guild_id`, so
legacy dispatch paths can not accidentally inherit a tier whitelist for
an inadmissible Guild × Tier pair.

### 6.1 Guild-aware PEP policy matrix

BP.D.8/B1 adds `guild_id=` as a non-breaking policy dimension on
`pep_gateway.evaluate()` and `classify()`. The inheritance rule is:

* Omitted or empty `guild_id` keeps the R0 tier-only behaviour.
* Known `guild_id` + admitted PEP tier inherits the existing tier
  whitelist unchanged.
* Known `guild_id` + inadmissible PEP tier denies with
  `rule="guild_tier_inadmissible"` before the tool whitelist is applied.
* Unknown `guild_id` denies with `rule="guild_unknown"` rather than
  silently falling back to a tier whitelist.

The PEP-to-sandbox tier mapping is `t0 → SandboxTier.T0`, `t1 → T1`,
`t2` / `networked → T2`, and `t3 → T3`. The exported
`guild_tier_whitelist(guild_id, tier)` helper materialises the inherited
matrix for callers/tests, and `GET /pep/policy` exposes
`guild_policy_matrix` for operator visibility. The yaml narrowing layer
is still not loaded at runtime (§6.2).

### 6.2 Today: no yaml loader

`configs/sandbox_tier_policy.yaml` is a **pure spec file** today. There
is no Python module that reads it at runtime. The header in the yaml
file calls out "Resolution order (loader contract — to be implemented
in BP.S.4)"; that header is *aspirational*. Per the BP.S epic header
("純命名 + 文件化、零 runtime 改動") and the explicit BP.S.4 row scope
("純命名 + 文件化"), the loader is **not** part of BP.S.4. It is
called out in §7 as future work.

Until the loader lands, `sandbox_tier.GUILD_TIER_ADMISSION_MATRIX` is
the *only* admission source consulted by callers. The yaml file
documents what an operator *will* be able to narrow once the loader
exists; today operators who need narrowing must edit the structural
matrix in code (and ship a release).

### 6.3 Why the two vocabularies diverge in case

`SandboxTier.T0`, `T1`, etc. — upper-case — was chosen by BP.S.1 for
"stable wire / log / metric label". The PEP's `t1` / `t2` / `t3` —
lower-case — predates BP.S.1 (it landed with R0 #306) and matches the
existing audit/SSE convention there. Normalising to a single case
across both surfaces is a §7 concern; today, callers translate at the
boundary:

```python
# Recommended caller pattern when both layers are in play:
from backend.sandbox_tier import SandboxTier, assert_admitted, Guild
from backend import pep_gateway

assert_admitted(Guild.bsp, SandboxTier.T1)        # admission (BP.S.1)
decision = await pep_gateway.evaluate(            # enforcement (R0)
    tool="run_bash",
    arguments={"command": "make -j4"},
    agent_id="bsp:agent-42",
    guild_id=Guild.bsp.value,
    tier="t1",                                    # NB: lower-case
)
```

Until §7 lands, keep the PEP wire convention lower-case at caller
boundaries. `tier_whitelist()` lower-cases defensively, but audit / SSE /
metric labels are clearer when callers pass the explicit lower-case
literal rather than `SandboxTier.T1.value`.

---

## 7. Forward roadmap (out of scope for BP.S.4)

The following work is acknowledged but **explicitly not** part of this
row. Each item has its own future row.

* **Yaml loader for `configs/sandbox_tier_policy.yaml`.** Implements
  the resolution order documented in the yaml header (omitted Guild →
  matrix default; explicit list → narrowing; explicit `[]` → forbid).
  Must reject any operator list that *widens* past the structural
  matrix. Tracked outside BP.S.4; mentioned in BP.S.6 ("policy
  parsing").
* **`assert_admitted(guild, tier)` enforcement at the runtime
  boundary.** BP.D.8/B1 makes the PEP deny inadmissible Guild × Tier
  pairs when `guild_id` is supplied. A separate dispatcher row still
  needs to call `assert_admitted` before the tool reaches the PEP so the
  audit row can distinguish "rejected at routing" from "rejected at
  policy enforcement".
* **Case normalisation.** Picking one casing — most likely lower-case
  to match the existing PEP wire format — and updating
  `SandboxTier.value`, `sandbox-tier-audit.md`, the yaml schema, and
  the audit / SSE labels in lockstep. Cosmetic but trip-wire-prone, so
  separate row.
* **Tier-aware rule overlays.** Today `_DESTRUCTIVE_RULES` and
  `_PROD_HOLD_RULES` are tier-independent. There is no current request
  to differentiate (a `terraform destroy` is just as destructive in T1
  as in T3), but if a future row needs e.g. "auto-allow `git push` in
  T2 only when the target is `gerrit-review` not `gerrit-public`", the
  overlay would land here.
* **gVisor adoption (R12 close-out).** BP.W3.13 — the current Tier 1 /
  Tier 2 are plain Docker; the gVisor reference in
  `sandbox_capacity.py` is a DRF cost-weight constant only. No
  compliance claim in this doc may be read as kernel-isolation
  evidence until BP.W3.13 lands. See `sandbox-tier-audit.md` §0 R12
  callout.

---

## 8. Cross-references

* **Code source-of-truth (this layer)**:
  [`backend/pep_gateway.py`](../../backend/pep_gateway.py) —
  `classify`, `tier_whitelist`, `guild_tier_whitelist`,
  `guild_policy_matrix`, `evaluate`, `PepDecision`, `_DESTRUCTIVE_RULES`,
  `_PROD_HOLD_RULES`, `TIER_T{1,2,3}*` whitelist tables.
* **Code source-of-truth (admission layer)**:
  [`backend/sandbox_tier.py`](../../backend/sandbox_tier.py) — `Guild`,
  `SandboxTier`, `GUILD_TIER_ADMISSION_MATRIX`, `is_admitted`,
  `assert_admitted`, `GuildTierViolation`.
* **Operator narrowing spec** (loader is future):
  [`configs/sandbox_tier_policy.yaml`](../../configs/sandbox_tier_policy.yaml).
* **Per-Guild × per-Tier audit** (Phase D consumer):
  [`docs/design/sandbox-tier-audit.md`](sandbox-tier-audit.md).
* **4-tier model design**:
  [`docs/design/tiered-sandbox-architecture.md`](tiered-sandbox-architecture.md).
* **Capacity (cost) dimension** — orthogonal axis:
  [`backend/sandbox_capacity.py`](../../backend/sandbox_capacity.py).
* **Risk register entries** affecting this layer:
  * **R0 #306** — PEP Gateway middleware itself (closed by R0 landing).
  * **R12** — gVisor cost-weight only / not actual runtime; BP.S.5
    risk row + BP.W3.13 close-out.
  * **R13** — Hardware Daemon nominal until Phase T (T3 RPC layer).
* **Drift-guard test** (future): BP.S.6
  `backend/tests/test_sandbox_tier_policy.py` — will include a
  `policy parsing / PEP integration` group that, once the §7 yaml
  loader exists, asserts the loader's effective set never widens past
  `GUILD_TIER_ADMISSION_MATRIX`.

---

## 9. Change log

| Date       | Change                                                              | Author                |
| ---------- | ------------------------------------------------------------------- | --------------------- |
| 2026-05-02 | Initial publication (BP.S.4). Documents the *already-shipped* PEP   | Agent-row-bp-s-4-self |
|            | tier integration verbatim; explicitly lists what is **not** yet      |                       |
|            | in the PEP path (Guild kwarg, yaml loader, T0 entry, case            |                       |
|            | normalisation) under §7. Zero runtime changes. Companion drift       |                       |
|            | guard remains BP.S.6's responsibility.                               |                       |
| 2026-05-05 | BP.D.8/B1: documents landed `guild_id` policy dimension, inherited   | Codex/GPT-5.5         |
|            | `guild_tier_whitelist()`, and `/pep/policy.guild_policy_matrix`.     |                       |

> Future updates to this doc MUST be paired with a corresponding update
> to `backend/pep_gateway.py` whitelist tables / `classify()` rule
> ordering (or vice versa). Once BP.S.6 lands, drift between the two
> sides will fail CI.
