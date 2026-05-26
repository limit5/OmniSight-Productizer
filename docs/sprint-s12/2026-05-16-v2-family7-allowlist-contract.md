---
id: SPRINT-S12G-V2-FAMILY7-ALLOWLIST-CONTRACT
version: v1 (2026-05-16)
title: G.A-v2 Family ⑦ — PUBLIC_PATH_ALLOWLIST Single-Source Contract + Path Decision
scope: Contract spec for the single source-of-truth public-path allowlist consumed by every auth-relevant middleware. Doc-only ticket (v2-⑦-1a); no runtime change.
status: Draft — OP-1146 (this ticket); locks Path C per operator decision 2026-05-14 Q2
related:
  - sprint-s12g-A-v2-runtime-defense-contract-spec.md §3 "Family ⑦ — Auth middleware allowlist single-source-of-truth" (parent spec)
  - docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md (incident that surfaced the drift class)
  - backend/auth_baseline.py (today's longest allowlist; lines 74-260 ish)
  - backend/main.py (4 sibling allowlists: `_rate_limit_gate`, `_bootstrap_gate`, `_graceful_shutdown_gate`, K1 password-change gate; lines 639-1195)
  - backend/auth.py (mode-gate `auth_mode()` — placeholder consumer; today references `/health` only in docstring)
  - JIRA OP-1146 (v2-⑦-1a — this spec)
  - JIRA OP-1147+ (v2-⑦-1bc, v2-⑦-2bc, v2-⑦-Reproduce-401, v2-⑦-FixHealth, v2-⑦-ContractTest, v2-⑦-Integration — downstream impl tickets that consume this spec)
---

# G.A-v2 Family ⑦ · PUBLIC_PATH_ALLOWLIST Single-Source Contract — v1 (2026-05-16)

## §0. Reading order

1. §1 — the bug class this spec eliminates (5 minutes).
2. §3 — the Path A / B / C decision tree (operator already locked C 2026-05-14 Q2; this section captures *why*).
3. §4 — the contract (`PUBLIC_PATH_ALLOWLIST` + `is_public()`); this is what impl tickets implement.
4. §5 — consumer enumeration (the five middleware files that must call `is_public()`).
5. §6 — the `/health` punt to `v2-⑦-FixHealth`.
6. §7 — the drift contract (CI test that catches a 6th-middleware-with-its-own-allowlist regression).
7. §8 — migration plan + ticket sequencing.
8. §9 — claude-class override note for the spec ticket.

This spec is the **contract** — not the implementation. Code lands in `v2-⑦-1bc` (allowlist module + tests) and `v2-⑦-2bc` (middleware refactor); decisions about `/health` itself land in `v2-⑦-FixHealth`. Anchors in §4, §5, §6, §7 are stable and will be referenced from those tickets' AC.

---

## §1. Drift class definition

### §1.1 The class, in one sentence

> *"N independently-edited path-prefix sets that morally should agree on a public-path policy but mechanically can disagree — and any disagreement is a security-or-availability bug, depending on which side drifts."*

Concrete instance from 2026-05-14: a fresh-host-reboot probe ran `curl /health` against a backend started with `OMNISIGHT_AUTH_BASELINE_MODE=enforce`. The response was **401 Unauthorized**. Operator expectation: `/health` is a docker healthcheck endpoint and must always answer 200 with no session.

Forensic walk (from `sprint-s12g-A-v2-runtime-defense-contract-spec.md` §0 row ⑦, audited 2026-05-14):

| # | Middleware | File:line | Allowlist contains `/health`? |
|---|---|---|---|
| 1 | `_rate_limit_gate` | `backend/main.py:647` (`_RATE_LIMIT_EXEMPT`) | ✅ yes |
| 2 | K1 password-change gate | `backend/main.py:639-642` (`_PASSWORD_CHANGE_EXEMPT`) | ✅ yes |
| 3 | `_bootstrap_gate` | `backend/main.py:1072-1112` (`_BOOTSTRAP_EXEMPT_*`) | ✅ yes (both `_REL` and `_RAW`) |
| 4 | `_graceful_shutdown_gate` | `backend/main.py:1125` (`_GRACEFUL_SHUTDOWN_EXEMPT_RAW`) | ✅ yes |
| 5 | `auth_baseline` middleware | `backend/auth_baseline.py:74-…` (`AUTH_BASELINE_ALLOWLIST`) | ✅ yes *as of 2026-05-16 commit `bc28bd65`* — but for ~30 days between OP-1131 (probe policy lock) and `bc28bd65` it contained `/api/v1/health` ONLY. The OP-1131 patch added `/health` to the auth_baseline list and is the reason a fresh enforce-mode probe today returns 200 instead of the 2026-05-14 401. |

The 2026-05-14 incident is a literal instance of "4 of 5 whitelists agree on `/health`; the 5th — `auth_baseline` — quietly disagreed". The four agreeing whitelists are *dead code with respect to the actual blocker*: they correctly exempted `/health` from rate-limit / bootstrap-redirect / shutdown-503 / password-change-redirect, but the request never reached the rate-limit / bootstrap / shutdown / password gates because `auth_baseline` had already 401'd it. The four exemptions are not wrong; they're *unreachable on this path*.

This is the bug class. Not a typo, not a missing entry — a **synchronization invariant that was never declared, was never enforceable, and quietly held by accident for a long time before silently failing once.**

### §1.2 Why this is dangerous beyond `/health`

`/health` is benign — the worst case is "external monitor sees red, operator wakes up." For the same drift class applied to a *different* path, the cost asymmetry inverts:

- **False-positive drift on a sensitive admin path** (one allowlist exempts `/api/v1/admin/danger`, four don't): the request is blocked by `auth_baseline` so the security posture is preserved. Operator confusion only.
- **False-negative drift on a "public" path that one allowlist forgot** (four allowlists exempt the path, `auth_baseline` exempts it too, but the K1 password-change gate doesn't): a logged-in-but-must-change-password user can hit an unintended endpoint with stale credentials. Worse, an *anonymous* path that one of the four operational gates forgot to exempt can return 503 / 429 / 307 in conditions where the operator expects 200.
- **Worst: a *future* allowlist** (a 6th middleware that an engineer adds in 2026-Q4 with its own `_GATE_EXEMPT`): inherits today's pattern, drifts within a week, surfaces as a Sev-2 in 2027.

The 5th-drift bug-class is **inevitably-recurring** unless the synchronization invariant is hoisted into a single named object that every middleware reads from.

### §1.3 Why "just review every PR carefully" doesn't dissolve it

Three reasons:

1. **No grep target.** Today, finding "all places that decide a path is public" requires reading every middleware decorator. A new middleware doesn't surface to the auth reviewer unless the author explicitly tags `area:auth` on the PR — and that label is self-reported.
2. **No failing test.** When the 2026-05-14 drift opened, every unit test for `auth_baseline.py` passed (its allowlist was internally consistent), every test for `_rate_limit_gate` passed (its allowlist was internally consistent), and the inter-middleware composition was untested.
3. **Asymmetric incentive.** Adding to an allowlist is "I'm fixing my middleware's noise." Removing from an allowlist is "I'm tightening security." Both are local edits; neither demands a global pass — and neither *can* demand it while the lists are not named-as-one.

The structural fix is to make the synchronization invariant **a property of one named object** (`PUBLIC_PATH_ALLOWLIST`) that every consumer reads via one named function (`is_public()`), so any drift is *visible at grep-time* and any new-middleware-with-its-own-list is *detectable at CI-time*.

---

## §2. Scope of this ticket (v2-⑦-1a)

This ticket is the spec for the contract — output is this document plus the section anchors used by `v2-⑦-1bc` / `v2-⑦-2bc` / `v2-⑦-FixHealth` / `v2-⑦-ContractTest` AC.

| In scope | Out of scope |
|---|---|
| Decision: Path A / B / C with rationale | Implementing `backend/middleware_allowlist.py` (→ `v2-⑦-1bc`) |
| Names: `PUBLIC_PATH_ALLOWLIST`, `is_public(path)`, helper file path | Refactoring the 5 middlewares (→ `v2-⑦-2bc`) |
| Consumer list (5 middleware files) | Deciding whether `/health` stays in or leaves the allowlist (→ `v2-⑦-FixHealth`) |
| Migration sequencing (batched vs incremental) | Writing the regression-failing test (→ `v2-⑦-Reproduce-401`) |
| Drift-contract CI test description | Implementing the drift-contract CI test (→ `v2-⑦-ContractTest`) |
| Section anchors that downstream tickets reference | E2E probe across all middleware × path variants (→ `v2-⑦-Integration`) |

**Runtime impact of this ticket: ZERO.** No code in `backend/`, no migration, no env var, no CI yaml. The only files touched by OP-1146 are this new doc plus the x-ref in `sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑦ section.

---

## §3. Path decision (A / B / C)

Per operator Q2 lock 2026-05-14: **Path C is chosen.** This section captures the three alternatives evaluated and why A and B were rejected; downstream impl tickets reference this section by anchor (`#§3-path-decision-a-b-c`).

### §3.1 Path A — "cheapest one-line fix: add `/health` to `auth_baseline` and move on"

**Description.** A two-character diff: insert `"/health"` next to `"/api/v1/health"` in `AUTH_BASELINE_ALLOWLIST`. Done. Doc-test the live curl, ship.

**Cost.** 1 line + 1 commit + 0 architecture work. The lowest-cost intervention that resolves the named symptom.

**What it doesn't fix.**

- The drift class survives untouched. Five `*_EXEMPT` sets continue to exist, hand-maintained, with no synchronization invariant.
- The 6th-middleware-in-2026-Q4 problem (§1.2) is undeflected.
- The next 5th-drift bug surfaces on a non-`/health` path where the cost asymmetry is much worse.
- The four already-dead-code-on-this-path exemptions in the sibling middlewares (rate-limit / bootstrap / shutdown / password) remain dead code; future readers continue to reason about them as if they were load-bearing.

**Status: REJECTED.**

**Rejection rationale (operator lock 2026-05-14 Q2 paraphrased).** *"We've already paid the discovery cost on `/health`. Paying it again on a more sensitive path is the cost of choosing Path A. The structural fix is cheap enough now that we should not buy a one-line Band-Aid and then re-pay forensics later."*

### §3.2 Path B — "normalize dead code: remove the four sibling exemptions because only `auth_baseline` actually matters"

**Description.** Recognize that requests are stopped at `auth_baseline` before they reach `_rate_limit_gate` / `_bootstrap_gate` / `_graceful_shutdown_gate` / K1, therefore the four sibling `*_EXEMPT` sets are mostly dead code. Delete them; keep `AUTH_BASELINE_ALLOWLIST` as the only allowlist; trust middleware ordering to make all four sibling exemptions equivalent.

**Cost.** Medium — 4 file edits + careful audit that ordering really does make the siblings dead.

**What it doesn't fix.**

- **The "mostly dead code" claim is fragile.** Middleware registration order can change. `auth_mode() == "open"` short-circuits `auth_baseline`, in which case the four sibling allowlists *are* the only filter for their concerns. Removing them silently changes behavior for the open-mode dev flow.
- **It bakes the wrong invariant.** "Order makes this dead" is a property of today's `main.py` registration sequence, not a contract. A future refactor that reorders middlewares re-livens the four lists — and they'll have drifted in the interim because they were "obviously dead."
- **It still leaves 5 allowlists to maintain.** Even after pruning, `auth_baseline` continues to be the only source of truth — but now the other 4 middlewares each carry an *implicit* "trust the layer above" comment, which is invisible to grep.

**Status: REJECTED.**

**Rejection rationale (operator lock 2026-05-14 Q2 paraphrased).** *"Path B normalizes the dead code instead of eliminating it. We still have N hand-maintained lists, just shorter ones. The 5th-drift class survives — it just narrows its surface."*

### §3.3 Path C — "single source of truth: `PUBLIC_PATH_ALLOWLIST` in `backend/middleware_allowlist.py` consumed by all 5 middlewares" *(chosen)*

**Description.** Introduce one new module:

- `backend/middleware_allowlist.py`
  - `PUBLIC_PATH_ALLOWLIST: frozenset[str]` — the *single* declarative set of path prefixes that may bypass *any* "is this caller authorized / in-band / pre-bootstrap-ok" check.
  - `is_public(path: str) -> bool` — the *single* function that every middleware calls before applying its own gate.

Each of the 5 middlewares replaces its private `*_EXEMPT` set with a call to `is_public(request.url.path)` (or an equivalent path-normalization wrapper that delegates the membership decision to the helper). The five private sets are deleted; the helper file is the only source.

**Cost.** Medium — 1 new file + tests, 5 middleware refactors, 1 contract CI test. Higher upfront than A; same order of magnitude as B; pays back the moment any new middleware lands.

**What it fixes.**

- **The drift class is structurally eliminated.** There is *no way* for two middlewares to disagree about whether a path is public, because there is only one declaration.
- **Grep target.** `rg PUBLIC_PATH_ALLOWLIST` and `rg is_public` name every consumer.
- **CI enforcement is possible.** A static-analysis (or import-graph) test can assert "every middleware that has `@app.middleware('http')` or extends `BaseHTTPMiddleware` either imports `is_public` *or* is on a documented exempt list." That test is implemented in `v2-⑦-ContractTest`.
- **Future-proof.** A 6th middleware added in 2026-Q4 must consult `is_public()` or fail the contract test (see §7).

**Status: CHOSEN.**

**Lock evidence.** Operator decision recorded 2026-05-14, Q2 of the G.A-v2 design review; verbatim in `sprint-s12g-A-v2-runtime-defense-contract-spec.md:504-510` and the Q2 row at line 713.

### §3.4 Decision table (summary)

| Aspect | Path A | Path B | Path C ★ |
|---|---|---|---|
| Lines changed | 1 | ~50 (4 delete blocks) | ~200 (new module + 5 refactors + test) |
| `/health` 401 resolved | ✅ | ✅ (via ordering) | ✅ (via `/health` in `PUBLIC_PATH_ALLOWLIST`, pending §6 punt) |
| Drift class eliminated | ❌ | ❌ | ✅ |
| Grep target for "public paths" | None | `auth_baseline.AUTH_BASELINE_ALLOWLIST` | `PUBLIC_PATH_ALLOWLIST` / `is_public` |
| CI enforceable | ❌ | ❌ | ✅ (`v2-⑦-ContractTest`) |
| Future-proof against new middleware | ❌ | ❌ | ✅ |
| Operator decision 2026-05-14 Q2 | Rejected | Rejected | **Chosen** |

---

## §4. The single-source contract

This section defines the API surface that `v2-⑦-1bc` implements. Downstream tickets reference these names verbatim; do not invent variations.

### §4.1 Module location

```
backend/middleware_allowlist.py
```

- New file. Sibling to `backend/auth_baseline.py` so the import line in `auth_baseline.py` is `from backend.middleware_allowlist import is_public, PUBLIC_PATH_ALLOWLIST`.
- Lives in `backend/` (not `backend/middleware/` — there is no such package today and creating one would touch more files than v2-⑦-1bc's scope).

### §4.2 Public names (frozen)

```python
PUBLIC_PATH_ALLOWLIST: frozenset[str]
"""Single declarative set of path prefixes that bypass every
middleware that gates auth / rate-limit / bootstrap-redirect /
shutdown-503 / password-change-redirect.

Match semantics: `request.url.path.startswith(prefix)` after
normalization through `_api_relative_path()` — i.e. both the raw
path and the API-prefix-stripped form are tested against this set.

Adding a prefix is a SECURITY decision. Each entry MUST have a
justification comment in this file. Code review for any PR touching
this set REQUIRES one reviewer from the `non-ai-reviewer` group
(per CLAUDE.md L1 Safety Rules + Family ⑦ drift-contract policy)."""


def is_public(path: str) -> bool:
    """Return True iff `path` is in PUBLIC_PATH_ALLOWLIST.

    Handles BOTH the raw path AND the API-prefix-stripped form
    (i.e. /api/v1/livez and /livez both return True if /livez is
    in the set). Implementation MAY normalize path separators,
    strip trailing slashes, etc., but the membership semantics
    MUST match the existing prefix-match in auth_baseline.py.

    This is the ONLY function any middleware should call to ask
    "is this path public?" — no private allowlists, no inline
    `path.startswith(...)` checks. The drift contract (§7) enforces
    this at CI time."""
```

### §4.3 Initial seed contents

The initial population of `PUBLIC_PATH_ALLOWLIST` is the **union** of today's five sibling allowlists, normalized:

| Source (today) | Contents contributed to seed |
|---|---|
| `AUTH_BASELINE_ALLOWLIST` (auth_baseline.py:74…) | Liveness/readiness probes + `/metrics` + pre-session auth endpoints + docs/OpenAPI + WebSocket handshakes — see file for full justified list |
| `_RATE_LIMIT_EXEMPT` (main.py:647) | `/health`, `/healthz`, `/livez`, `/readyz`, `/auth/login`, `/auth/logout` |
| `_PASSWORD_CHANGE_EXEMPT` (main.py:639-642) | `/auth/change-password`, `/auth/login`, `/auth/logout`, `/auth/whoami`, plus health/livez/readyz/healthz |
| `_GRACEFUL_SHUTDOWN_EXEMPT_RAW` (main.py:1125) | `/healthz`, `/health`, `/livez`, `/readyz` |
| `_BOOTSTRAP_EXEMPT_REL` + `_BOOTSTRAP_EXEMPT_RAW` (main.py:1072-1093) | `/auth/login`, `/auth/logout`, `/auth/change-password`, `/healthz`, `/health`, `/livez`, `/readyz`, `/version`, `/`, `/docs`, `/openapi.json`, `/redoc`, `/favicon.ico`, `/robots.txt`, plus prefix sets `/cloudflare/`, `/_next/`, `/static/`, `/assets/`, `/public/`, plus the static-suffix list (`*.css`, `*.js`, etc.) |

`v2-⑦-1bc` decides the exact seed payload by taking the union, eliminating duplicates, and **preserving every existing justification comment** (the auth_baseline.py allowlist is already heavily commented — those comments migrate verbatim).

Static-asset prefixes (`/_next/`, `/static/`, `/assets/`, `/public/`) and suffix matches (`.css`, `.js`, etc.) are **NOT** path prefixes in the same shape as the auth allowlist — they're bootstrap-specific exemptions. These migrate as a sibling helper `is_static_asset(path)` rather than into `PUBLIC_PATH_ALLOWLIST` itself, keeping the latter focused on "API endpoints that bypass auth/rate/bootstrap." This split is left as a design degree-of-freedom for `v2-⑦-1bc` provided the public semantics of `is_public()` are preserved.

### §4.4 Match semantics

The historical `auth_baseline` allowlist uses `path.startswith(prefix)` against the longest-deterministic-prefix form. `_BOOTSTRAP_EXEMPT_*` uses a mix of exact match (`/`) and `startswith` (`/cloudflare/`). `_RATE_LIMIT_EXEMPT` uses set membership against the API-relative path.

**Resolution for `is_public()`:** the semantics MUST be a superset of every consumer's historical semantics. Concretely:

- Test BOTH `path` and `_api_relative_path(path, api_prefix)`.
- Use exact-set match against `PUBLIC_PATH_ALLOWLIST`. (Prefix matches against the `auth_baseline` historical list become explicit prefix entries — there is no `*` glob; the seed eliminates the ambiguity.)
- A pure prefix-substring (`/cloudflare/`) is encoded as a separate `PUBLIC_PATH_PREFIXES: frozenset[str]` if and only if `v2-⑦-1bc` finds at least one current entry that requires prefix semantics. Otherwise `PUBLIC_PATH_ALLOWLIST` is sufficient. The choice is local to `v2-⑦-1bc`; both shapes satisfy this contract.

The downstream `v2-⑦-2bc` ticket must NOT change observable behavior — the post-refactor allowlist set MUST be a superset (or equal) of the pre-refactor union. Strictly-removing-an-entry is `v2-⑦-FixHealth` territory.

### §4.5 What `is_public()` does NOT do

- It does NOT check session / cookie / bearer. Auth is `auth_baseline.py`'s job; `is_public()` only answers *"would this path be exempt from gating?"*
- It does NOT perform any I/O. No DB read, no env lookup at call time. The set is module-level and immutable (`frozenset`).
- It does NOT log. Logging is the caller's job (each middleware logs its own gate decisions in its own taxonomy).
- It does NOT do regex. Membership is constant-time.

---

## §5. Consumer list (the five middlewares)

The contract is implemented when every middleware below imports and calls `is_public()` and deletes its private `*_EXEMPT` set. Listed in registration order (outermost first in Starlette stack — outermost is the LAST `@app.middleware("http")` declaration in `main.py`).

| # | Middleware (callable name) | File & line | Today's private allowlist (will be deleted in `v2-⑦-2bc`) | Refactor type |
|---|---|---|---|---|
| 1 | `_graceful_shutdown_gate` | `backend/main.py:1129` | `_GRACEFUL_SHUTDOWN_EXEMPT_RAW = {…}` at `main.py:1125` | Drop-in: replace `if path in _GRACEFUL_SHUTDOWN_EXEMPT_RAW or rel in _GRACEFUL_SHUTDOWN_EXEMPT_RAW:` with `if is_public(path):` |
| 2 | `_bootstrap_gate` | `backend/main.py:1158` | `_BOOTSTRAP_EXEMPT_REL`, `_BOOTSTRAP_EXEMPT_RAW`, `_BOOTSTRAP_EXEMPT_REL_PREFIXES`, `_BOOTSTRAP_EXEMPT_RAW_PREFIXES`, `_BOOTSTRAP_STATIC_SUFFIXES` (`main.py:1072-1093`); helper `_bootstrap_path_is_exempt()` at `main.py:1096` | Compound: replace `_bootstrap_path_is_exempt(path, rel)` with `is_public(path) or is_static_asset(path)`. The static-asset suffix table moves to the sibling helper described in §4.3. |
| 3 | `_rate_limit_gate` | `backend/main.py:651` | `_RATE_LIMIT_EXEMPT = {…}` at `main.py:647` | Drop-in: replace `if rel in _RATE_LIMIT_EXEMPT:` with `if is_public(path):` |
| 4 | K1 password-change gate (currently inline, name TBD by `v2-⑦-2bc`) | `backend/main.py:639-642` (`_PASSWORD_CHANGE_EXEMPT` set, plus inline middleware further down — `v2-⑦-2bc` will name it during refactor) | `_PASSWORD_CHANGE_EXEMPT = {…}` | Drop-in: replace set-membership check with `is_public(path)` |
| 5 | `auth_baseline` middleware | `backend/auth_baseline.py:~84+` (the body of the middleware class/function that consumes `AUTH_BASELINE_ALLOWLIST`) | `AUTH_BASELINE_ALLOWLIST` at `backend/auth_baseline.py:74` | Compound: the historical justified list moves into `backend/middleware_allowlist.py` *with all comments preserved*; `auth_baseline.py` retains the OMNISIGHT_AUTH_BASELINE_MODE gate but consults `is_public()` for the allowlist decision |

**Note on `auth.py` (mentioned in the ticket task list as the 5th consumer):**

The original ticket task list cites `backend/auth.py` as the fifth consumer. After auditing today's code, `auth.py` does NOT have a request-level middleware with its own allowlist — its only `/health`-related reference is a docstring comment about strict-mode behavior (line 13). `auth.py` is the *session-layer* helper consumed by routers via `Depends(current_user)`, not a path-gating middleware.

The five actual path-gating middlewares are the four above plus `auth_baseline`. The ticket's mention of `auth.py` should be read as either (a) a forward-looking placeholder in case `auth.py` ever grows its own bypass path table, or (b) shorthand for "the K1 password-change gate currently inlined near `_PASSWORD_CHANGE_EXEMPT`." This spec treats interpretation (b) as authoritative — the K1 password-change gate is the fifth consumer. The downstream `v2-⑦-2bc` AC explicitly enumerates these five callables by name.

If `v2-⑦-Reproduce-401`'s discovery audit surfaces a sixth path-gating middleware (e.g., the API-versioning deprecation middleware or any of the CSRF / CORS layers), it is added to the consumer list at `v2-⑦-2bc` filing time without renegotiating this spec — the contract names *behaviour* ("middleware that decides whether to gate based on path") not a fixed count of 5.

### §5.1 Middleware ordering — why it doesn't matter for the contract

Starlette wraps middlewares in reverse registration order; the *last* `@app.middleware("http")` decorator is the *outermost*. Today's registration order (read from `main.py` top-to-bottom of `@app.middleware("http")` blocks) is roughly:

1. K1 password-change gate (innermost-ish; registered early)
2. `_rate_limit_gate`
3. `auth_baseline` middleware (registered via `auth_baseline.install_middleware(app)`)
4. `_graceful_shutdown_gate`
5. `_bootstrap_gate` (outermost)

After Path C, *which* middleware first short-circuits a `/health` request is unchanged (still the outermost — bootstrap if pre-finalize, graceful-shutdown if draining, etc.). What changes is that each middleware's *exemption decision* is identical, so the request reaches the next layer with the same "this is a public path" answer. The bug class (§1) disappears because the answer is shared, not because the ordering changed.

### §5.2 Pre-merge invariant — `v2-⑦-2bc` exit gate

Before `v2-⑦-2bc` merges, the following invariant MUST hold and be recorded in the PR description:

> `grep -rE '\b(EXEMPT|ALLOWLIST|exempt_paths)\b' backend/main.py backend/auth_baseline.py backend/auth.py` returns ONLY references to `is_public()`, `PUBLIC_PATH_ALLOWLIST`, or `is_static_asset()`. Any other private allowlist constants have been deleted.

This is the "private allowlist eradication" check. `v2-⑦-ContractTest` makes it an automated CI test; the pre-merge check is the manual fallback if CI is offline.

---

## §6. The `/health` punt (`v2-⑦-FixHealth`)

### §6.1 What this spec does NOT decide

This spec does not decide whether `/health` is in `PUBLIC_PATH_ALLOWLIST`. That decision lives in `v2-⑦-FixHealth`, which runs AFTER `v2-⑦-2bc` because the decision is only meaningful in the world where a single source of truth exists.

### §6.2 Why the punt

The 2026-05-14 symptom (`/health → 401`) is what made the drift class visible, but `/health` itself is an *artifact* of the drift — it accidentally became a path that some middlewares exempted and others didn't because the canonical liveness path is being migrated to `/livez` + `/readyz` (Kubernetes-shaped naming). Three options exist:

- **(a) Keep `/health`** in the allowlist as an alias for `/livez`. Maintains compatibility with Caddy + older external monitors that probe `/health` by default. The current state (post-OP-1131 `bc28bd65`) is this option.
- **(b) Remove `/health`** from the allowlist and the route table; require external monitors to switch to `/livez`. Eliminates ambiguity at the cost of an operator-facing breaking change.
- **(c) Keep `/health` but emit a deprecation header** until 2026-Q4 then remove. Two-step.

**The likely outcome, per the parent spec §3 Family ⑦ (line 507) and the OP-1131 probe-policy lock**, is option (a) — `/health` remains as a no-cost alias for `/livez`. Both endpoints share the cheap process-pulse semantics; `/readyz` is the deep readiness gate. The single-source contract this spec defines does not depend on which option `v2-⑦-FixHealth` picks: the contract works identically whether `/health` is in the set or out.

### §6.3 Why this ticket can't decide for `v2-⑦-FixHealth`

Two reasons:

1. **Wrong abstraction layer.** OP-1146 is a contract spec. Deciding individual entries in the contract's seed is one layer below it — the analog is "design the API of `frozenset` before you decide which elements to put in your specific instance."
2. **Wrong evidence base.** Deciding `/health` requires inventorying every external probe that targets it (Caddy upstream check, external uptime monitors, any pre-/v1 dashboards). That inventory work is in scope for `v2-⑦-Reproduce-401` and `v2-⑦-FixHealth`, not here.

### §6.4 Hand-off

`v2-⑦-FixHealth`'s AC must reference §6.1, §6.2, and §6.3 of this doc by anchor and pick one of (a)/(b)/(c) with an operator-facing migration note. The decision lands in this spec as a v1.1 amendment (§13 changelog).

### §6.5 Decision record — **option (a)** *(v2-⑦-FixHealth / OP-1760, 2026-05-27)*

Per the hand-off in §6.4, `v2-⑦-FixHealth` selects **option (a): keep `/health` public in `PUBLIC_PATH_ALLOWLIST` as a no-cost alias for `/livez`.**

- **Anchored rationale (per §6.1–§6.3).** §6.2 already named (a) the *likely outcome* — the parent spec §3 Family ⑦ (line 507) and the OP-1131 probe-policy lock both treat `/health` as a permanent, cheap process-pulse alias for `/livez`. The single-source contract (§4) is option-agnostic, so (a) carries zero contract risk: `/health` simply stays in the seed (§4.3) and is exempted uniformly by every consumer (§5) via `is_public()`.
- **Why not (b)/(c).** (b) Removing `/health` is an operator-facing breaking change for Caddy's upstream health check and older external uptime monitors that probe `/health` by default — no operator demand for that churn exists, and the §6.3 evidence base (external-probe inventory) surfaced no probe that would *break* if `/health` stayed. (c) The deprecation-header two-step only makes sense as a precursor to (b); with (b) declined, (c) is pure overhead.
- **No code/seed change required.** `/health` is already present (`backend/middleware_allowlist.py:101`) and the four `/health`, `/healthz`, `/livez`, `/readyz` probes are exempted uniformly through `auth_baseline`'s `is_public()` routing (OP-1752). This ticket only *records* the decision and de-xfails the OP-1744 reproduction (`backend/tests/test_health_allowlist_drift.py::test_v2_health_401_drift_reproduction`), which now hard-passes.
- **Operator-facing migration note.** **No action required.** `/health` remains a public, unauthenticated liveness alias for `/livez`; existing external monitors and the Caddy upstream check continue to work unchanged. `/readyz` remains the deep readiness gate. No deprecation timeline is set for `/health`.

---

## §7. Drift contract (the CI test that prevents recurrence)

### §7.1 The invariant

> *No request-level middleware in `backend/` may make a "should this path be gated?" decision using any source other than `backend.middleware_allowlist.is_public()`.*

A middleware that violates this invariant — by introducing its own `_EXEMPT` set, calling `request.url.path.startswith(...)` inline against a literal, importing a different allowlist module, etc. — must fail CI.

### §7.2 Implementation outline (for `v2-⑦-ContractTest`)

The CI test (`tests/contract/test_middleware_allowlist_drift.py` or similar location) does the following:

1. **Enumerate every `@app.middleware("http")` registration** by parsing `backend/main.py` and `backend/auth_baseline.py` with `ast.parse`. (Static parse — no import side effects.)
2. **Enumerate every `BaseHTTPMiddleware` subclass** declared in `backend/`.
3. **For each registered middleware**, inspect the function body / class `dispatch` method AST and assert one of:
   - It imports `is_public` from `backend.middleware_allowlist` AND calls it; OR
   - The middleware is on a documented exempt list (`docs/sprint-s12/middleware-allowlist-exempt.md` or similar — created in `v2-⑦-ContractTest` as needed) WITH a written justification.
4. **Fail** with a pointer to this spec § if a new middleware is added that doesn't satisfy one of the above.

The test is **static-AST-based** so it runs in ~100 ms and doesn't need a live app. False-positive escape hatch: the documented exempt list (item 3 bullet 2) handles the legitimate "middleware does not gate by path" case (e.g., a request-timing middleware that touches every request unconditionally).

### §7.3 What the test catches

- A 6th middleware added in 2026-Q4 with `_NEW_GATE_EXEMPT = {...}` and no import of `is_public`.
- Reverting `v2-⑦-2bc` by accident (e.g., a cherry-pick from an old branch reintroduces `_RATE_LIMIT_EXEMPT`).
- An inline shortcut like `if request.url.path == "/internal/maintenance": return await call_next(request)` that bypasses both the allowlist and the test.

### §7.4 What the test does NOT catch

- Logic bugs *inside* `is_public()` itself (those are unit tests against `middleware_allowlist.py` in `v2-⑦-1bc`).
- A consumer that calls `is_public()` and then ignores the result (those are E2E tests in `v2-⑦-Integration`).
- A non-middleware code path that gates by URL (e.g., a router-level dependency). Those live under `Depends(...)` and are not the drift class — they're per-handler decisions, which this contract intentionally does not collapse.

---

## §8. Migration plan

### §8.1 Ticket sequencing

```
v2-⑦-1a  (OP-1146, THIS DOC, claude class, doc-only)
   │
   ├─→ v2-⑦-Reproduce-401  (codex class, writes the failing CI test that
   │                        reproduces the 2026-05-14 /health → 401 incident
   │                        AND discovers any 6th middleware)
   │       │
   │       └─→ v2-⑦-1bc  (codex class, implements backend/middleware_allowlist.py
   │                      + unit tests for is_public)
   │              │
   │              └─→ v2-⑦-2bc  (codex class, class_override_required: yes,
   │                             refactors the 5 middlewares to consume is_public())
   │                     │
   │                     ├─→ v2-⑦-FixHealth   (codex class, resolves /health per §6)
   │                     ├─→ v2-⑦-ContractTest (codex class, the CI test from §7)
   │                     └─→ v2-⑦-Integration  (claude class, E2E probe across
   │                                            all 5 middlewares × all path
   │                                            variants enumerated in the
   │                                            consumer list + Reproduce-401)
```

### §8.2 Batched vs incremental refactor — recommendation

**Recommendation: BATCHED single PR for `v2-⑦-2bc`.** All five middlewares migrate to `is_public()` in one atomic change. Rationale:

1. **Drift-contract enforcement.** §7's CI test fails the moment one middleware has `is_public()` and a sibling still has its private list — the inconsistency is the bug. Landing all five at once is the only way the test can be enabled in the same PR; an incremental rollout requires the test to be skipped or scoped to the migrated subset, which leaves the door open for the drift class to reopen mid-migration.
2. **Low blast radius.** All five middlewares live in `backend/main.py` and `backend/auth_baseline.py` — two files. The diff is mechanical: extract local set → call site. There is no cross-file dependency that prevents atomicity.
3. **`v2-⑦-Reproduce-401` already gates the change.** The regression-failing test from Reproduce-401 transitions from failing to passing in exactly the commit that lands `v2-⑦-2bc`; an incremental rollout fragments that signal.

**Counter-argument (rejected):** "Incremental is safer because the rollback surface is smaller per-PR." Rejected because the rollback surface is `git revert` either way — five small reverts vs. one bigger revert are operationally identical. The risk that matters is *behavioral divergence during migration*, which incremental amplifies.

### §8.3 Backward compatibility

Path C is a pure refactor — `is_public()` returns the *union* of today's five allowlists (per §4.3). No path that is currently exempt becomes gated; no path that is currently gated becomes exempt. The post-refactor enforce-mode behaviour MUST be ≥ today's. The only intentional behaviour change is the `/health` resolution from `v2-⑦-FixHealth`, which is filed separately precisely to keep `v2-⑦-2bc` a pure refactor.

### §8.4 Rollback plan

If `v2-⑦-2bc` lands and an unexpected regression surfaces:

1. `git revert` the `v2-⑦-2bc` commit on `develop` (single SHA, no merge-commit complexity).
2. Re-open `v2-⑦-2bc` with the regression in the AC; add a unit test that captures the regression; resubmit.
3. The drift-contract CI test (§7) does NOT block the revert because reverting also reverts the test enablement.

The single-revert-shape is another reason to batch (§8.2).

### §8.5 Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| `v2-⑦-2bc` refactor accidentally removes an exempt path | Medium | `v2-⑦-Reproduce-401` failing test + §4.3 union-superset rule + §5.2 grep invariant |
| 6th middleware discovered during Reproduce-401 audit | Medium | Add to consumer list at v2-⑦-2bc filing time; no spec amendment needed (§5 note) |
| `is_public()` perf regression vs. inline set check | Low | `frozenset` lookup is O(1); existing inline checks are also O(1) — no shape change |
| Static-asset suffix matcher subtlety (`/file.css` etc.) lost during refactor | Medium | Split into sibling `is_static_asset()` helper per §4.3; tested separately |
| External monitor breakage if `v2-⑦-FixHealth` removes `/health` | Out of scope here | Handled in `v2-⑦-FixHealth` operator-facing migration note |

---

## §9. Auth-area class-override note

### §9.1 The rule

Per `coordination.md:31-57`, work touching `area:auth` or `area:security` normally requires **claude class** (the higher-trust autonomy tier) because misjudgments in those areas carry security risk.

### §9.2 Why this ticket (v2-⑦-1a / OP-1146) is claude class

This ticket is **spec-only** — no runtime code, no test, no env var, no migration. The only files touched are:

- `docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md` (NEW; this file)
- `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` (MINOR; one new x-ref line in the Family ⑦ section)

A spec ticket that produces a doc has no runtime side effects, so the auth-area sensitivity that motivates the class-override rule does not apply at this layer. The spec is reviewable by the same standard as any other architecture doc.

### §9.3 Why `v2-⑦-2bc` (refactor) IS class_override_required

By contrast, `v2-⑦-2bc` rewrites code in five middlewares, including the authentication boundary (`auth_baseline.py`). A bug there is a security incident. `v2-⑦-2bc` is filed with `class_override_required: yes` in its JIRA metadata so that the runner enforces claude-class pickup; the spec ticket does not need that gate.

### §9.4 Why `v2-⑦-1bc` (implement the helper module) is codex class

`v2-⑦-1bc` implements `backend/middleware_allowlist.py` as a *new* file with no callers yet — the existing five middlewares still consume their private lists. Until `v2-⑦-2bc` lands, the new module is dead code. Codex class is appropriate because the change has no runtime effect on the auth boundary; the auth-area sensitivity arrives at `v2-⑦-2bc`.

This tiering follows the v2 spec § "auth class-override" guidance: codex builds the unused helper, claude (or claude-class refactor) wires it in.

---

## §10. Acceptance verification map (for downstream ticket reviewers)

Cross-reference between this spec's sections and the downstream tickets that consume them:

| Downstream ticket | Cites this spec § | What the ticket implements |
|---|---|---|
| `v2-⑦-Reproduce-401` | §1, §5 | Failing CI test reproducing 2026-05-14; audit of all path-gating middlewares |
| `v2-⑦-1bc` | §4 (full), §4.3 | `backend/middleware_allowlist.py` + unit tests |
| `v2-⑦-2bc` | §5, §5.2, §8 | Refactor five middlewares; private allowlist eradication grep invariant |
| `v2-⑦-FixHealth` | §6 | Resolves `/health` per option (a)/(b)/(c) |
| `v2-⑦-ContractTest` | §7 (full) | Static-AST CI test enforcing the drift contract |
| `v2-⑦-Integration` | §5 (consumer list as probe matrix) | E2E across all 5 middlewares × path variants |

If a downstream ticket's AC contains the literal phrase *"per `2026-05-16-v2-family7-allowlist-contract.md` §X.Y"*, the section anchor must remain stable. Renumbering this spec requires a v1.1 amendment with a redirect table.

---

## §11. Open questions (intentionally left to downstream)

These were considered and *deferred* — they don't belong in the spec but are flagged so downstream tickets can pick them up without re-discovery:

1. **Q11.1 — Does `is_public()` accept the API prefix or the raw path?** *Resolution at `v2-⑦-1bc`:* accepts the raw path, normalizes internally via `_api_versioning.api_relative_path()`. Callers always pass `request.url.path`.
2. **Q11.2 — Should there be a separate `is_static_asset()` helper or fold static-asset matching into `is_public()`?** *Recommended at `v2-⑦-1bc`:* separate helper. Static-asset matching is suffix-based; auth-allowlist matching is prefix/exact-based; mixing them blurs the contract.
3. **Q11.3 — How does the contract interact with the API versioning deprecation middleware (`_api_versioning.install_deprecation_headers_middleware`)?** *Resolution at `v2-⑦-Reproduce-401` discovery phase:* if it gates by path, it's a 6th consumer; if it touches every request unconditionally, it's an exempt entry in the §7 documented exempt list.
4. **Q11.4 — Are router-level `Depends(current_user)` calls in scope?** *Resolution here, NOT deferred:* NO. The contract is for *path-based middleware-level* decisions. Per-handler `Depends(...)` is orthogonal and continues to provide RBAC on top of the baseline (as already documented in `auth_baseline.py` lines 37-44).
5. **Q11.5 — Should the contract cover non-HTTP middlewares (WebSocket upgrade, gRPC, etc.)?** *Resolution here:* WebSocket bypass paths are listed in the parent spec's `v2-⑦-Integration` E2E. The contract applies to anything that gates by request path, regardless of protocol; the §7 CI test extends naturally.

---

## §12. Boundary justification (spec ticket boundary block)

This ticket (OP-1146 / v2-⑦-1a) declares the following boundaries per `docs/sop/jira-ticket-conventions.md` §11:

```yaml
scope_components: [docs]
destructive_op_classes: []
external_side_effect: none
filesystem_writes:
  - docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md  (NEW)
  - docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md  (MINOR: x-ref)
runtime_paths_changed: 0
db_migrations: 0
ci_yaml_changed: 0
prod_compose_changed: 0
secrets_touched: []
```

**Exemption from the area-block list (out-of-area: db / devops / embedded / frontend / security / tests / tooling):** the area-block list in the ticket envelope excludes work in those areas because this is a docs-only ticket. The work is `area:backend + area:docs` (note: `area:backend` is included because the spec describes the backend contract surface, not because backend code is touched — no backend file is modified). The downstream `v2-⑦-2bc` will be re-evaluated under the full area-block set at filing time.

---

## §13. Changelog

| Version | Date | Author | Change |
|---|---|---|---|
| v1 | 2026-05-16 | claude-bot (OP-1146) | Initial spec. Path C locked per operator 2026-05-14 Q2. |
| v1.1 | 2026-05-27 | claude-bot (OP-1760) | §6.5 amendment: `/health` resolved to **option (a)** (keep public as a `/livez` alias; no code/seed change, no operator action). De-xfailed the OP-1744 `/health-401` reproduction test → hard pass. |

---

## §14. Hand-off block (for the next ticket)

The next ticket to pick up Family ⑦ work is **v2-⑦-Reproduce-401** (per `sprint-s12g-A-v2-runtime-defense-contract-spec.md:517`). That ticket's AC should reference:

- §1 of this spec for the bug-class definition.
- §5 of this spec as the *starting* enumeration of path-gating middlewares (Reproduce-401 may discover more).
- §6 for the punt to FixHealth (Reproduce-401 does NOT decide `/health`; it just makes the 401 reproducible and audits all path-gating middlewares).

After Reproduce-401 surfaces the full discovery audit, **v2-⑦-1bc** picks up to implement `backend/middleware_allowlist.py` per §4 of this spec.
