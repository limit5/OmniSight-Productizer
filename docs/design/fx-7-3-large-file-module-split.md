# ADR FX.7.3 — Module-Split Plan for the 9 Backend Files Over 2 000 Lines

> **Status:** Accepted (planning only — execution scheduled per §7).
> **Date:** 2026-05-04
> **Owner:** Claude / Opus (with Codex spillover for the mechanical waves).
> **Audit row:** `docs/audit/2026-05-03-deep-audit.md` §DT14-DT18.
> **TODO row:** Priority FX → `FX.7.3`.

## 0. Scope of this ADR

This document is a **planning artefact**, not an execution log. It

1. names the 9 files in scope (frozen list — re-bloat after this date is a *new* violation, not an addition to FX.7.3),
2. records the rules a split must obey to preserve public API contracts and import order,
3. proposes a per-file decomposition (module names + rough line ranges, **subject to revision** when the executing wave actually opens the file),
4. ranks the files by risk × value and pins each to a *wave* on the FX.7 / FX.8+ timeline,
5. specifies the drift guards that ship before the first wave so the split cannot silently regress.

What this ADR is **not**:

- it does **not** dictate final module names — the wave PR may rename if the actual code reads better; this doc records the *current best guess*.
- it does **not** preclude leaving the original file as a thin re-export shim. In fact §3.3 mandates a shim for two waves to keep external importers green.
- it does **not** guarantee all 9 files will be split. `tools.py` and `system.py` may stay monolithic if the wave opens them and finds the seams I propose are wrong.

## 1. The 9 files (frozen 2026-05-04)

Snapshot from `wc -l` on master @ `bb6681ed` (after FX.7.2).

| # | Path | LOC | Top-level | Notes |
|---|------|-----|-----------|-------|
| F1 | `backend/onvif_device.py` | 2 389 | 52 fn / 22 cls | Standalone protocol stack — **0 known importers**. |
| F2 | `backend/depth_sensing.py` | 3 215 | 47 fn / 36 cls | 1 test importer; standalone domain. |
| F3 | `backend/db.py` | 3 639 | 79 fn / 0 cls | Schema DDL + domain-row helpers. Imported from `main.py` only on init. |
| F4 | `backend/routers/tenant_projects.py` | 3 878 | 12 routes + 28 fn + 5 cls | Project CRUD + members + shares. ~11 test files. |
| F5 | `backend/routers/bootstrap.py` | 3 351 | 14 routes + 38 fn + 27 models | Setup wizard + service lifecycle. 1 test file. |
| F6 | `backend/routers/system.py` | 2 530 | 48 routes + 69 fn + 2 cls | 10+ thematic groups; 20+ importers. |
| F7 | `backend/agents/tools.py` | 2 437 | 50 `@tool` fn | LLM-callable tool surface. 17+ importers, ContextVar state. |
| F8 | `backend/routers/invoke.py` | 2 923 | 4 routes + 53 fn | Coach-state pipeline + agent error history. |
| F9 | `backend/auth.py` | 2 169 | 70 fn + 2 dataclass | Session + RBAC + Argon2 + bootstrap admin. ~20 importers. |

> **Drift baseline.** Total 26 531 LOC across the 9. The §6 drift-guard CI test pins the *count* (no new file ≥ 2 000 LOC accepted) and the *list* (these 9 — extending requires a follow-up audit row). After splits, the originals shrink under the threshold or become re-export shims; the threshold check still runs on every file in `backend/`.

## 2. Why split at all — and why not "just lint at 1 000 LOC"

The audit (`§DT14-DT18`) flagged these as "refactor candidates", not bugs. The rationale for picking up FX.7.3 *now* rather than deferring indefinitely:

- **Code-review blast radius.** A 3 800-line router that touches 12 routes means *every* PR opens the same file; reviewers context-swap between unrelated routes; the `Co-Authored-By` trailer collides; conflict resolution churns. SP-5.6a's bloat (cited in the SOP) was the canary.
- **Local-import circular avoidance** (FX.7.2 root cause). Many of the 1 528 surviving function-internal imports live in F3 / F6 / F9 — files that became "import attractors" because module-level imports from inside them would close cycles. Splitting by domain lets those imports live at module level in the smaller submodule without re-creating the cycle.
- **Dead-code visibility.** The audit's `__init__.py` false-positive (FX.7.1) only happened because dead-export detection cannot reason at scale across 3 000-line files. Smaller modules let static analyzers (`vulture`, `pyflakes --doctests`) actually point at unused symbols.
- **Test-fixture sprawl.** ~11 tests import `tenant_projects` directly; each fixture file fakes ~80 % of the module's surface. After split, each test file imports the 1 sub-module it actually exercises, fixtures shrink, fixture drift bugs (the SOP's `_reset_for_tests` class) become rarer.

A single "max LOC" lint rule is not a substitute because it would either (a) be set so high it doesn't bind, or (b) force splits along arbitrary line boundaries instead of *semantic* seams. This ADR records the semantic seams; the lint rule (§6) only protects them after the work is done.

## 3. Splitting rules (apply to every wave)

### 3.1 Public API is frozen

Any symbol currently importable as `from backend.X import Y` (where `X` is one of the 9 files) **must remain importable from that path** for at least one full release after the split. Mechanism:

- Move the implementation to `backend/X_impl/<submodule>.py` (or `backend/routers/X/<submodule>.py` for routers — see §3.4).
- In the original `backend/X.py`, replace the moved code with `from backend.X_impl.<submodule> import *  # noqa: F401, F403` — **explicit re-exports, not star** when the moved file has private helpers we don't want leaking. Pure star is acceptable only when the module is deliberately a flat namespace (e.g. `tools.py`).
- A new test (`backend/tests/test_X_public_surface_drift_guard.py`) snapshots `dir(backend.X)` *before* the split and asserts the post-split surface is a superset.

### 3.2 Router files keep their FastAPI `router` object at the original path

`backend/main.py` does `from backend.routers.X import router` for each. Breaking that import surface forces a coordinated 2-file PR (router + main) and risks lifespan ordering bugs. Therefore:

- For F4 / F5 / F6 / F8, the original `backend/routers/X.py` becomes a *router-aggregation* file: it instantiates the single `APIRouter()` and includes sub-routers from `backend/routers/X/<group>.py`.
- Each sub-router gets its own `APIRouter(prefix=..., tags=...)`, mounted via `router.include_router(sub_router)`.
- Route paths and tags are **bit-identical** before and after — the OpenAPI `paths:` keys must not change. A new test (`test_openapi_route_set_drift_guard.py`, see §6) snapshots all `(method, path, operation_id)` tuples and fails on diff.

### 3.3 Re-export shim lifetime: 2 waves minimum

After a split lands, the shim file (the now-thin `backend/X.py`) lives for at least **2 wave cycles** (≈ 4 weeks at the §7 cadence). Only after that may a follow-up PR delete the shim and migrate every importer to the new path. This buys two safety properties:

1. Bisecting between the split commit and any later regression doesn't have to chase import paths that disappeared.
2. Out-of-tree consumers (operator scripts, the runner workspace, sister project `omnisight-ai-core`) get one release window to update.

### 3.4 Routers: package layout, not flat files

When a router file becomes a package (F4 / F5 / F6 / F8), the layout is:

```
backend/routers/<name>/
    __init__.py         # re-exports `router` for backwards-compat
    _aggregator.py      # builds the top-level APIRouter, calls include_router()
    <group>.py          # one per thematic seam (see §4)
    _models.py          # Pydantic models shared across groups
    _helpers.py         # row-to-dict converters, validation predicates
```

`__init__.py` is **2 lines** — `from ._aggregator import router` + `__all__ = ["router"]`. No business logic in `__init__.py` (the FX.7.1 lesson).

### 3.5 No behaviour changes in the same commit as a split

A wave PR may *only* move code. No bug fixes, no logic tweaks, no Pydantic v1→v2, no error-message rewording. If a bug surfaces during the split, open a separate row, fix it on master, then rebase the split. This is the FX.7.2 anti-bulldozer rule applied to refactors — making the split "code-motion only" means a single `git diff -M50 --stat` should show ~100 % rename similarity for the moved chunks.

### 3.6 The pre-commit fingerprint grep (SOP §3) still runs

Even though no code changes, the moved chunks may *contain* legacy compat fingerprints (`_conn()`, `await conn.commit()`, SQLite `?` placeholders). The SOP-mandated grep at commit time catches them. If a fingerprint is found, do **not** clean it up in the same commit — file a follow-up FX.7.x row. Same anti-bulldozer rule.

## 4. Per-file split design

The line ranges below come from the structural survey done 2026-05-04. They are **first-cut targets**, not final commitments. The wave PR may revise.

### F1. `backend/onvif_device.py` (2 389 LOC)

**Why first:** zero external importers (ipcam_rtsp_server wraps it but doesn't import internals); pure protocol code; no DB; no FastAPI; no async pool. Lowest blast radius of all 9.

**Target package:** `backend/onvif/` (rename from `onvif_device.py` to a package — old import stays via shim in §3.3).

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_constants.py` | 1–186 | NS_* URIs, enums, status codes |
| `_models.py` | 190–460 | UserLevel, ONVIFService, DeviceInfo, NetworkInterface, MediaProfile, PTZConfig dataclasses |
| `_exceptions.py` | 464–615 | Exception hierarchy |
| `_soap.py` | 516–760 | XML escape, SOAP envelope, WS-UsernameToken digest |
| `device.py` | 740–1740 | `ONVIFDevice` class + dispatch |
| `ops/_device.py` | 1759–1910 | `_op_device_*` operations |
| `ops/_media.py` | 1910–2000 | `_op_media_*` |
| `ops/_events.py` | 2000–2123 | `_op_events_*` |
| `ops/_ptz.py` | 2123–2282 | `_op_ptz_*` |

**Risk:** XML namespace constants are referenced in nearly every op-module — must live in `_constants.py` and be imported, not duplicated. The `threading.Lock()` in `ONVIFDevice` for subscription state stays inside the class and *does not become* a module-global on import.

**Estimate:** 1.5 days. Mostly mechanical — `git mv` + import rewiring.

---

### F2. `backend/depth_sensing.py` (3 215 LOC)

**Why second:** 1 test importer; pure-numeric domain; high natural hierarchy (data → math → adapters → algorithms → orchestration).

**Target package:** `backend/depth/`

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_models.py` | 1–290 | 13 enums + SensorConfig / DepthFrame / StereoConfig / Point clouds / calibration result dataclasses |
| `_math.py` | 295–540 | Matrix ops, transforms, quaternions, bounds |
| `sensors/_base.py` + `sensors/{sony,melexis}.py` | 557–770 | Abstract `DepthSensor` + 2 concrete |
| `algorithms/structured_light.py` | 768–1300 | StructuredLightCodec + stereo pipeline |
| `algorithms/point_cloud.py` | 1316–1850 | PointCloudProcessor |
| `algorithms/registration.py` | 1863–2400 | Registration + SLAM engines + calibration |
| `runners.py` | 2493–3215 | Test runners, scene generation, recipe orchestration |

**Risk:** Stateful codec classes (StructuredLightCodec, StereoPipeline, PointCloudProcessor) hold per-frame buffers — splitting by file does not split state, the buffer ownership stays inside the class. SLAM/registration algorithms may rely on shared `_math.py` helpers; circular-import check needs `_math` to depend on nothing else in `backend.depth`.

**Estimate:** 2 days. Algorithm code is dense; reading-time dominates.

---

### F3. `backend/db.py` (3 639 LOC)

**Why third:** schema DDL is purely mechanical to relocate; the 79 helper functions cluster cleanly by domain noun; only `main.py` and tests import the public surface.

**Target package:** `backend/db_pkg/` (avoid name collision with the existing `backend.db` shim which becomes a re-exporter).

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_init.py` | 1–180 | `init()`, dialect prep, DSN/path resolution |
| `schema/_create.py` | 550–1100 | All `CREATE TABLE` statements |
| `schema/_alter.py` | 1101–1550 | `_render_add_column()`, idempotent ALTER helpers |
| `domain/agents.py` | 2030–2300 | Agent + task + notification row factories |
| `domain/episodic.py` | 2400–2900 | Episodic memory + turn-state |
| `domain/users.py` | 2900–3639 | User/history row factories |

**Risk:** the dev-only module-level `_db` aiosqlite handle (cited in survey) **must not** become a module-global in any of the new submodules — it stays in `_init.py` and is only used by code paths that are dev-only. Production goes through `db_pool` and never touches `_db`. The split is the right moment to add a `# DEV-ONLY: production must reach DB via backend.db_pool` comment on the `_db` definition; no other comments needed.

**Estimate:** 2 days. Schema DDL is the bulk of the LOC but the easiest to move; domain helpers carry import-graph weight.

---

### F4. `backend/routers/tenant_projects.py` (3 878 LOC)

**Why fourth:** clear thematic seams (CRUD vs members vs shares); ~11 test importers but each test typically targets one seam; quota/budget validation crosses CRUD/patch but stays inside the seam.

**Target package:** `backend/routers/tenant_projects/`

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_models.py` | 1–400 | Pydantic models, validation predicates |
| `_helpers.py` | scattered | Row-to-dict converters, quota/budget validation |
| `crud.py` | 401–1840 | Create / list / patch / archive / restore routes |
| `members.py` | 2407–2950 | Member add/patch/delete routes + models |
| `shares.py` | 3160–3878 | Share create/get/delete routes + models |

**Risk:** `gc_archived_projects` background task is referenced from CRUD (archive/restore retention); make sure it imports from `crud.py` cleanly and is not re-instantiated. ~11 test files: each test must change one import line; expect a 12-file PR (1 router-package + 11 test imports). This is the first wave that pays the §3.4 package-layout overhead.

**Estimate:** 2.5 days (1 day code, 1.5 days test rewiring + drift-guard fixtures).

---

### F5. `backend/routers/bootstrap.py` (3 351 LOC)

**Why fifth:** each handler is largely self-contained; only 1 test importer; but **shell subprocess + dotenv I/O risk** means the wave needs careful smoke testing.

**Target package:** `backend/routers/bootstrap/`

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_models.py` | scattered | The 27 Pydantic models |
| `admin_password.py` | 67–368 | Admin password rotation (1 route) |
| `tenant_init.py` | 368–600 | Tenant bootstrap (1 route) |
| `llm_setup.py` | 637–860 | LLM provisioning + Ollama detect (2 routes) |
| `networking.py` | 880–1296 | CF tunnel skip + vertical setup (2 routes) |
| `services.py` | 1296–2118 | Service lifecycle: start / tick / wait-ready / health (4 routes) |
| `smoke.py` | 2964–3076 | Subset DAG smoke test (1 route) |
| `finalize.py` | 3090–3351 | Status / finalize / reset (3 routes) |

**Risk:** `services.py` contains streaming SSE handlers (`start-services`, `service-tick`); the SSE generator must not lose its async context when moved. Verify by hitting `/api/v1/bootstrap/start-services` post-split with `curl -N` and confirming stream chunks still arrive. Also: the middleware references this router for the initial-setup gate — that import path is preserved by the §3.4 `__init__.py` shim.

**Estimate:** 2.5 days. SSE smoke testing dominates.

---

### F6. `backend/routers/system.py` (2 530 LOC)

**Why sixth:** 48 routes, 10+ thematic groups, 20+ importers. Highest blast radius among routers. Splitting **before** F4/F5 would risk cascading test failures in unrelated areas.

**Target package:** `backend/routers/system/`

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_models.py` | scattered | Shared response models |
| `info.py` | 146–284 | System info / devices / EVK |
| `deploy.py` | 338–600 | Deploy + release version + manifest |
| `pipeline.py` | 369–500 | Pipeline status / start / advance / timeline |
| `health.py` | 611–925 | Status / capacity / forecast / spec |
| `repos.py` | 951–1105 | Vendor SDKs + repos + logging |
| `tokens.py` | 1105–1600 | Daily / hourly / heatmap / burn-rate |
| `prompts.py` | 1803–1920 | Turns + compression + prompts |
| `roles.py` | 2364–2521 | Roles, model rules, NPI phases |

**Risk:** `tokens.py` carries the **module-global daily-budget reset state** (lines 1289–1327 per survey). Per SOP §1's module-global cross-worker rule, this is a *known* pre-existing concern (each worker holds its own counter; reset depends on wall-clock). The split must keep the global *inside* `tokens.py` only — no helper extracts it to `_models.py` or `_helpers.py`. Splitting is also the right moment to add a `# WARNING: per-worker counter, see SOP §1` docstring.

The pipeline timeline streaming function (lines 390–500) has the same SSE concern as F5's `services.py`.

**Estimate:** 3 days. 48 routes × 9 sub-modules × per-route smoke ping = the most test-heavy wave.

---

### F7. `backend/agents/tools.py` (2 437 LOC)

**Why seventh:** 17+ importers, ContextVar (`active_workspace`, `active_agent_id`) is the agent-runtime contract; bash-execution timeout + dangerous-pattern regex are critical security boundaries.

**Target package:** `backend/agents/tools/`

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_context.py` | 32–115 | ContextVars, safety/path-escape predicates |
| `fs.py` | 121–386 | File-system tools (read/write/create/patch/list) |
| `git.py` | 389–557 | Git status/log/diff/commit/branch/push |
| `bash.py` | 560–640 | Bash execution + timeout + exfil block |
| `gerrit_review.py` | 642–950 | Gerrit + issue tracking + reports |
| `platform_sdk.py` | 952–1110 | Platform/SDK + artifact registration |
| `memory.py` | 1113–1600 | L2/L3 memory tools + snapshots |
| `mcp_image.py` | 1605–2130 | Simulations + MCP + image generation |
| `_llm_adapter.py` | 2135–2437 | LLM-adapter utilities + MCP spec loading |

**Risk highest of all 9 splits.** ContextVar discipline must survive — every sub-module that reads `active_workspace` does it via `from backend.agents.tools._context import active_workspace`, never via `from backend.agents.tools import active_workspace` (which would still work via shim, but creates an import-cycle once `tools/__init__.py` imports the sub-modules). The dangerous-pattern regex stays in `_context.py` and is imported by `bash.py` + `fs.py` — not duplicated.

The 17+ test importers are the single biggest reason this is wave 7, not wave 1: the test rewire is the dominant cost.

**Estimate:** 3 days. Test rewire is half the effort.

---

### F8. `backend/routers/invoke.py` (2 923 LOC)

**Why eighth:** coach-state pipeline (`_analyze_state` → `_detect_coaching_triggers` → `_plan_actions` → `_build_coach_context` → `_build_report`) is a tight chain — splitting by helper function doesn't reduce coupling, only by *role*. 6+ test importers, agent-runtime caller from `agents/nodes.py`.

**Target package:** `backend/routers/invoke/`

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_state.py` | 402–1000 | State analysis + URL/image detection |
| `coach/triggers.py` | 1007–1400 | Coach trigger detection + action planning |
| `coach/context.py` | 1641–1930 | Context building + image/URL/build intent |
| `report.py` | 2276–2646 | Report templating + tenant resolution |
| `_handlers.py` | 2665–2923 | Route handlers (stream/halt/resume/create) |

**Risk:** the agent error history (`clear_agent_error_history`) is module-local. Splitting must keep the registry inside one of `_state.py` or a new `_error_history.py` — *not* duplicated across the coach chain. Streaming response buffering (line ~2700) has the same SSE concern as F5/F6.

**Estimate:** 2.5 days.

---

### F9. `backend/auth.py` (2 169 LOC)

**Why last:** 20+ importers across the entire backend; `_DUMMY_PASSWORD_HASH` is computed at import (timing-oracle defence) and **must continue to be computed at import** of the original `backend.auth` path; any split risks breaking that contract.

**Target package:** `backend/auth/`

| Submodule | Rough lines | Content |
|-----------|-------------|---------|
| `_roles.py` | 1–165 | Roles tuple, role_at_least, mode selection |
| `password.py` | 77–160 | Argon2id + PBKDF2 legacy + `_DUMMY_PASSWORD_HASH` |
| `_models.py` | 163–213 | User / Session dataclasses |
| `_network.py` | 278–330 | UA hash, IP subnet helpers |
| `users.py` | 328–840 | DB lookups, user find/create/update, password reset |
| `bootstrap_admin.py` | 662–1000 | Default-admin creation + change-needed detection |
| `mfa.py` | 1009–1240 | MFA + device lockout |
| `sessions.py` | 1238–1650 | Session metadata, revocation, device tracking |
| `deps.py` | 1790–2099 | FastAPI dependencies: csrf_check, require_role, project_role_at_least |

**Risk highest of all router/non-router splits.**

1. `_DUMMY_PASSWORD_HASH` must remain a module-level constant of `backend.auth` (the original path). If it moves to `password.py`, then `backend.auth/__init__.py` must `from .password import _DUMMY_PASSWORD_HASH` to preserve import-time computation. A test (`test_dummy_hash_computed_at_import_drift_guard.py`) must assert the constant exists on `backend.auth` and matches the recomputation.
2. The 20+ importer list includes `account_linking.py`, `security/oauth_client.py`, `auth_baseline.py`, `routers/events.py`, `mfa.py` — every one of them reads the surface (e.g. `from backend.auth import require_role`). Shim must export every public symbol.
3. The SOP §1 module-global rule requires a one-line docstring at `_DUMMY_PASSWORD_HASH`'s definition: *"Per-worker constant, derived from a fixed input — same value on every worker, no coordination needed."*

**Estimate:** 3 days. The `_DUMMY_PASSWORD_HASH` and CSRF surface require extra care.

## 5. Risk × value × dependency ordering (rationale for §7)

| Wave | File | External importers | LOC | Internal coupling | Risk score | Value score | Net priority |
|------|------|-------------------:|----:|-------------------|-----------:|------------:|-------------:|
| W1 | F1 onvif_device | 0 | 2 389 | low | **1** | 2 | 1 (do first) |
| W2 | F2 depth_sensing | 1 | 3 215 | low | **2** | 3 | 2 |
| W3 | F3 db | ~all (transitively) but only `init()` is called | 3 639 | medium | **3** | 5 | 3 |
| W4 | F4 tenant_projects | 11 tests | 3 878 | low | **3** | 4 | 4 |
| W5 | F5 bootstrap | 1 | 3 351 | medium (SSE) | **4** | 4 | 5 |
| W6 | F6 system | 20+ | 2 530 | medium (SSE + module-global tokens) | **5** | 5 | 6 |
| W7 | F7 tools | 17+ | 2 437 | high (ContextVar) | **6** | 4 | 7 |
| W8 | F8 invoke | 6+ | 2 923 | high (coach chain + SSE + error history) | **6** | 3 | 8 |
| W9 | F9 auth | 20+ | 2 169 | very high (`_DUMMY_PASSWORD_HASH`, CSRF) | **7** | 4 | 9 |

Risk × value rationale:

- **Risk** = importers + state-coupling (ContextVar / module-global / SSE / import-time side-effects).
- **Value** = (a) review-blast-radius reduction, (b) local-import unblock potential, (c) future feature velocity in that surface.
- **Net priority** orders by *risk-ascending* (cheap wins first) — this is the deliberate inverse of "value-descending" because the ADR's contract is "no behaviour change". Front-loading high-value/high-risk waves would burn the safety budget on the splits least likely to land cleanly.

## 6. Drift guards (must ship before W1)

These tests are **prerequisites** for any wave PR being mergeable. They land as a single FX.7.3-prep commit before W1.

### 6.1 `backend/tests/test_large_file_drift_guard.py`

```python
# Pseudocode — actual test file lands with W1-prep commit.
ALLOWED_FROZEN = {                       # the 9 from §1, snapshot 2026-05-04
    "backend/onvif_device.py": 2389,
    "backend/depth_sensing.py": 3215,
    ...
}
HARD_CAP = 2000                          # any file > HARD_CAP must be in ALLOWED_FROZEN

def test_no_new_files_over_2000_lines():
    for path in glob("backend/**/*.py"):
        loc = count_loc(path)
        if loc > HARD_CAP:
            assert path in ALLOWED_FROZEN, (
                f"{path} has {loc} LOC > {HARD_CAP}. New files >2000 LOC require an ADR row."
            )

def test_frozen_files_only_shrink():
    for path, baseline in ALLOWED_FROZEN.items():
        if exists(path):                  # missing = split into a package, fine
            assert count_loc(path) <= baseline, f"{path} grew past frozen baseline"
```

The "only shrink" axis matters: a file already over the cap can't *grow* without an ADR amendment — refactor pressure stays one-directional.

### 6.2 `backend/tests/test_openapi_route_set_drift_guard.py`

Snapshots `(method, path, operation_id, tags)` for every route registered on `app`. Wave PRs that split a router file but accidentally drop or rename a route fail this test. Snapshot lives in `backend/tests/data/openapi_route_baseline.json` and is updated by the same wave PR that intentionally adds/removes routes.

### 6.3 `backend/tests/test_public_surface_<module>_drift_guard.py` (one per file in §1)

```python
EXPECTED = {"User", "Session", "require_role", "csrf_check", ...}

def test_backend_auth_public_surface():
    import backend.auth as m
    surface = {n for n in dir(m) if not n.startswith("_")} | {"_DUMMY_PASSWORD_HASH"}
    missing = EXPECTED - surface
    assert not missing, f"removed from public surface: {missing}"
```

Each wave PR adds the relevant `EXPECTED` set as part of the same commit that lands the split.

### 6.4 Import-graph cycle detector (nightly)

Add `pylint --disable=all --enable=cyclic-import backend/` to the nightly CI job. Per the FX.7.2 follow-up note in `HANDOFF.md`, hoisting more module-level imports without this guard is risky. Each wave PR may close cycles silently; the nightly catches re-introductions. Failure does **not** block PR merge — it opens a `cycle:` issue.

## 7. Schedule

The 9 waves are paced at the FX.7 / FX.8 cadence, ~2-3 days each, with a 1-week pause every 3 waves to absorb regressions.

| Wave | Date (target) | File | Owner | Notes |
|------|---------------|------|-------|-------|
| W0 (prep) | 2026-05-05 → 2026-05-06 | drift guards (§6.1-6.4) | Claude | Lands before W1; no code motion. |
| W1 | 2026-05-07 → 2026-05-08 | F1 onvif_device | Codex (mechanical) | 0 importers — safest first wave. |
| W2 | 2026-05-09 → 2026-05-11 | F2 depth_sensing | Codex (mechanical) | |
| W3 | 2026-05-12 → 2026-05-14 | F3 db | Claude (architectural) | DDL split + dev-only `_db` discipline. |
| **Pause week 1** | 2026-05-15 → 2026-05-21 | observe regressions in W1-W3 surfaces | — | Roll forward only if no `[D]` regressions. |
| W4 | 2026-05-22 → 2026-05-25 | F4 tenant_projects | Claude | First router package layout — sets the template. |
| W5 | 2026-05-26 → 2026-05-29 | F5 bootstrap | Claude | SSE smoke required. |
| W6 | 2026-05-30 → 2026-06-03 | F6 system | Claude | Largest test wave. |
| **Pause week 2** | 2026-06-04 → 2026-06-10 | observe regressions in W4-W6 surfaces | — | |
| W7 | 2026-06-11 → 2026-06-15 | F7 agents/tools | Claude (architectural) | ContextVar discipline. |
| W8 | 2026-06-16 → 2026-06-19 | F8 invoke | Claude | Coach chain split. |
| W9 | 2026-06-20 → 2026-06-24 | F9 auth | Claude (architectural) | `_DUMMY_PASSWORD_HASH` import-time invariant. |
| **Pause week 3 + shim removal** | 2026-06-25 → 2026-07-08 | drop W1 / W2 shims | Claude | Then iteratively per §3.3. |

**Total**: ~7 weeks calendar, ~4 weeks of effort interleaved with FX.7.4-7.12 / FX.8.

**Pause-week semantics:** the pause is *not* idle time — it is when other FX rows (FX.7.4 HANDOFF YAML, FX.7.6 alembic enforcement, etc.) advance. The split rolls forward only after the previous wave's `[D]` (deployed) status is observation-clean.

**Cancellation criteria:** if any wave's PR opens > 1 production-affecting bug in the first 24 h post-deploy, halt the schedule, file a regression row, and **do not start the next wave** until the regression is closed and a retro is appended to this ADR.

## 8. What FX.7.3 itself completes today

FX.7.3 is **planning**, per its TODO text. Today's row closes when:

- [x] this ADR exists and is reachable via `docs/design/README.md`,
- [x] the 9 files are frozen (the LOC table in §1),
- [x] the wave schedule is pinned (the table in §7),
- [ ] the drift guards (§6) are *specified* but not yet shipped — they ship as part of W0 on 2026-05-05.

The execution waves (W0 through W9) become **new TODO rows** under a future Priority `MS` (Module Split) sub-epic, *not* sub-bullets of FX.7.3. This keeps FX.7.3 closed and makes per-wave status visible to the audit-correction reader.

## 9. Open questions / non-decisions

These are flagged here so the W0 author has them queued.

1. **Should `tools.py` stay flat?** Counter-argument: the 50 `@tool` decorators are *deliberately* a flat namespace because the LLM tool-call surface is name-based. A package layout is fine for human readers but may confuse the MCP/function-calling glue. **Decision deferred to W7** — open the file, look at how `tool_dispatcher.py` enumerates tools, decide then.
2. **Should `db.py` schema split go further than `_create.py` / `_alter.py`?** One could split per-table, but tables aren't a stable unit (Alembic adds them). Per-domain (agents / users / episodic) is the proposed cut, but a per-table cut is theoretically possible. **Decision deferred to W3.**
3. **Should the FX.7.3 drift guard count *test* files?** Several test files are also > 2 000 LOC (`test_integration_settings.py`, `test_mobile_build_error_autofix.py`, etc.). The audit explicitly excluded them from DT14-DT18 because tests are leaf nodes — splitting them is a separate quality concern (FX.5 territory). The drift guard in §6.1 globs `backend/**/*.py` *including* tests but uses a separate `ALLOWED_FROZEN` for tests so production-file rules are not relaxed by test-file growth.

## 10. References

- `docs/audit/2026-05-03-deep-audit.md` §DT14-DT18 — original audit row.
- `docs/sop/implement_phase_step.md` §1 — module-global state rule (cited for §6.4 nightly cycle check).
- `docs/sop/implement_phase_step.md` §3 — pre-commit fingerprint grep (cited for §3.6 anti-bulldozer rule).
- `HANDOFF.md` 2026-05-04 FX.7.2 — local-import hoist that explicitly handed off to FX.7.3 in its `Next gate`.
- `TODO.md` Priority FX → FX.7.3 — owning row.
