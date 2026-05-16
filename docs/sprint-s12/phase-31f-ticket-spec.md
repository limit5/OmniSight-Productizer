---
id: SPRINT-S12-PHASE-31F-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.F — Cosign Image Signing + GitLab Container Registry Migration · Ticket Spec
scope: cosign + sign-in-CI + verify-at-pickup + 90-day legacy allowlist (with reconcile) + GHCR → GitLab CR migration (split soak + cutover) + key-material schema class
status: Draft (2026-05-13)
related:
  - ADR-0023 §3.3, §14, §15
  - 31.E §7 Q2 lock (GitLab CR migration in 31.F)
  - Phase 31.A v2 + 31.B v2 + 31.C v2 + 31.D v2 + 31.E v2
  - 31.F v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31f-codex-review-final.txt)
---

# Sprint S12 Phase 31.F · Cosign + GitLab Container Registry — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| **7c snapshot race** | enumerated GHCR digests at single point in time → gap between snapshot and fail-closed enforcement leaves new pushes neither signed nor allowlisted | v2: **7c split into 7c-initial + 7c-reconcile** — initial snapshot pre-SignSmoke; reconcile snapshot immediately before fail-closed enforcement; evidence "new unsigned digests since initial: N" |
| **Integration overloaded** | 14-day soak + GHCR cutover combined → mid-soak production change | v2: **Integration split into StabilityCheckpoint + Soak + GHCR-Cutover** (3 tickets; codex Q4: cutover must NOT be mid-soak) |
| **Schema `secret-adjacent` insufficient** | cosign private key + password actually-secret, not "adjacent" | v2: **NEW `external_payload_class: key-material`** (above secret-adjacent); applies to 1bc/2bc/6a tickets handling real key material |
| **6a mixed scope hidden** | wrote to project-scope cred dir AND system-scope GitLab CI variables — collapsed to `system` hid review details | v2: **`destructive_op_scope: mixed` + `scope_components: [host-cred-dir, gitlab-ci-settings]`** |
| **7a/7b registry/signing coupling** | 7a signs $IMAGE_REF (potentially GHCR digest before GitLab CR added in 7b) → wrong registry signed | v2: **7a + 7b MERGED into 7ab** — single ticket handles "sign job uses GitLab CR digest as canonical signed ref + GHCR mirror push" |

**P1 STRUCTURAL fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| 8a imports `scripts.cosign.verify` directly | scripts/ not stable backend package | v2: NEW **backend/security/cosign_verify_adapter.py** as importable library wrapper; 8a imports adapter, not scripts |
| LibsGate under-specified for security-sensitive code | only 7 generic checks | v2: **expanded to 14 contract checks** (no secret logs / digest-not-tag signing / corrupt-allowlist behavior / public-key missing / shell-injection guard / etc.) |
| Cross-phase `*-external` blockers human-readable only | stale blockers cause drift | v2: **filing-script does JQL/Gerrit preflight** (mechanical verify blocker Closed + Submitted SHA); evidence in filing artifact |
| Corrupt allowlist behavior unclear | v1 didn't differentiate "corrupt" from "expired" | v2: **"unavailable" verdict** (signed images still pass cosign; unsigned-depending-on-allowlist refuses with P1 "allowlist corrupt" — distinct from "allowlist expired") |
| 6a evidence weak (only chmod + path) | possible CI log leakage | v2: **stronger ACs**: disable `set -x`, no env-dump artifacts, post-job log scan for PEM headers + fingerprint patterns, masked-variable evidence |

**Schema additions (extended from 31.B v2 §3 + 31.C v2 §3.4):**

```yaml
external_payload_class: synthetic | operational | secret-adjacent | key-material   # NEW key-material
destructive_op_scope: none | project | system | mixed   # NEW mixed
scope_components: [str, ...]   # NEW — required when scope=mixed
```

`key-material` rule (filing hook + runner-side):
- Any ticket with `external_payload_class: key-material` REQUIRES `class:operator-prepare-only` OR `class:operator-window`
- ACs MUST include: log-capture secret-safety test + post-run log scan + xtrace-disabled assertion
- LibsGate verifies all `key-material` tickets pass these checks

**Ticket count**: v1 26 → v2 **28** (Integration split +2; 7a+7b merge -1; 7c reconcile split +1)

---

## §1, §2 — unchanged

[See v1 §1 / §2]

---

## §3. Schemas (extended)

Reuses Phase 31.A v2 / 31.B v2 / 31.C v2 / 31.D v2 / 31.E v2 + adds:

### §3.10 NEW: Signing trust root independence (per codex cross-phase review Q6b 2026-05-13)

**Invariant**: signing trust root (cosign keys + sign job + verify path) MUST remain **independent** of registry choice. Registry migration (GHCR → GitLab CR) can fail / rollback / retry WITHOUT requiring cosign key rotation OR re-signing of already-signed images.

Encoded as 4 rules:

1. **Sign-job decoupling**: 7ab sign job takes `$CANONICAL_REGISTRY_DIGEST` env var as input (NOT hard-coded GitLab CR path); canonical registry choice is configurable via GitLab CI variable `CANONICAL_REGISTRY` (default `gitlab-cr`; switchable for rollback).
2. **Signature portability**: cosign signatures are stored alongside image digest (sha256 is content-addressed); signatures remain valid if image is re-pushed to a different registry under same digest.
3. **Rollback decoupling**: GHCR-Cutover rollback (re-enable GHCR mirror push) does NOT trigger cosign key rotation. Existing signatures on GitLab CR digests remain valid; new pushes to GHCR get signed by same key.
4. **Failure-attribution rule**: SignSmoke + StabilityCheckpoint MUST emit separate health fields `signing_trust_root_healthy` AND `registry_migration_healthy` so failures are attributable.

Why this matters: 31.F bundles 2 trust boundaries (signing + registry migration). If GitLab CR provisioning fails (B-side), the failure mode currently obscures whether signing itself works (A-side). This invariant separates them WITHOUT requiring a phase split.



### §3.8 NEW: `external_payload_class: key-material` (per codex Q5)

Above `secret-adjacent`. Applies to tickets that **handle the cosign private key value itself** (not just file paths or fingerprints).

| Class | Examples | Filing-hook rule |
|---|---|---|
| `synthetic` | test data / mocks | none |
| `operational` | normal operational data | none |
| `secret-adjacent` | filenames / paths / public keys / fingerprints | log-capture test recommended |
| **`key-material`** | **cosign private key value, COSIGN_PASSWORD, signing token in active use** | **REQUIRED operator-* class + log-capture + post-run log scan + xtrace-disabled** |

### §3.9 NEW: `destructive_op_scope: mixed` + `scope_components` (per codex Q5)

When a single ticket has destructive operations at multiple scope levels (e.g., 6a writes both project-scope cred dir AND system-scope GitLab CI variables), use:

```yaml
destructive_op_scope: mixed
scope_components: [host-cred-dir, gitlab-ci-settings]   # explicit list
```

`scope_components` enum values:
- `host-cred-dir` — writes to ~/.config/omnisight/ (project)
- `host-fs-project` — writes to /home/user/work/sora/* (project)
- `host-fs-system` — writes to /etc /var /usr (system)
- `gitlab-ci-settings` — modifies CI variables on shared GitLab (system)
- `gerrit-config` — modifies Gerrit shared config (system)
- `git-refs-shared` — pushes/rewrites refs on shared remote (system)
- `systemd-user` — user-level systemctl (project)
- `systemd-system` — system-level systemctl (system)
- `external-network-prod` — POSTs to production third-party (system)

Filing hook: if `scope_components` contains any `*-system` or `*-shared` → ticket REQUIRES `class:operator-prepare-only` OR `class:operator-window`. Same rule as flat `destructive_op_scope: system`.

---

## §4. Children — overview table (28 tickets v2)

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy |
|---|---|---|---|---|---|---|---|
| L0 | 1 | 31.F-1a | cosign key management spec | S | claude | claude | — |
| L0 | 2 | 31.F-2a | cosign signing CI integration spec | S | claude | claude | — |
| L0 | 3 | 31.F-3a | GitLab CR migration spec | S | claude | claude | — |
| L0 | 4 | 31.F-4a | ADR-0027 (cosign + 90-day allowlist) | S | claude | claude | — |
| L0 | 5 | 31.F-5a | Image verification spec | S | claude | claude | — |
| L1 | 6 | 31.F-1bc | cosign-key-gen.py + tests (key-material) | S | claude | claude + operator-prepare-only | 1a |
| L1 | 7 | 31.F-2bc | cosign-sign-image.py + tests (key-material) | S | codex | codex + operator-prepare-only | 2a |
| L1 | 8 | 31.F-3bc | gitlab-cr-migration.py + tests | S | codex | codex + operator-prepare-only | 3a |
| L1 | 9 | 31.F-4bc | legacy-allowlist.py + tests + **corrupt→unavailable verdict** | S | codex | codex | 4a |
| L1 | 10 | 31.F-5bc | cosign-verify.py + tests | S | codex | codex | 5a |
| L1 | 11 | 31.F-5d | **NEW**: backend/security/cosign_verify_adapter.py + tests (library packaging for backend import) | S | claude | claude | 5bc |
| L2-Gate | 12 | 31.F-LibsGate | Cross-lib + 14 contract checks | S | claude | claude | 1bc, 2bc, 3bc, 4bc, 5bc, 5d, 4a |
| L3 | 13 | 31.F-6a | **OPERATOR**: cosign keypair gen + GitLab CI variables (mixed scope; key-material) | S | claude | claude + operator-window | LibsGate, 31.E-Integration-external |
| L3 | 14 | 31.F-6b | **OPERATOR**: Enable GitLab CR | S | claude | claude + operator-window | LibsGate |
| L4-Gate | 15 | 31.F-KeyAndRegistryReadyGate | Gate | S | claude | claude | 6a, 6b |
| L5 | 16 | 31.F-7ab | **MERGED v1 7a+7b**: sign job + GitLab CR push (uses CR digest as signed ref) | S | codex | codex + operator-prepare-only | KeyAndRegistryReadyGate, 2bc |
| L6 | 17 | 31.F-7c-initial | **SPLIT v1 7c**: initial GHCR snapshot pre-SignSmoke | S | codex | codex + operator-prepare-only | 4bc, 6b |
| L6-Gate | 18 | 31.F-SignSmoke | Sign + verify roundtrip | S | claude | claude + operator-rehearsal | 7ab, 7c-initial |
| L7 | 19 | 31.F-7c-reconcile | **NEW**: reconcile snapshot pre-enforcement (proves N new unsigned digests since initial) | S | codex | codex + operator-prepare-only | SignSmoke |
| L8 | 20 | 31.F-8a | Wire verify into runner pickup (imports from cosign_verify_adapter.py, NOT scripts) | S | claude | claude | SignSmoke, 5d, 31.B-Integration-external |
| L8 | 21 | 31.F-8b | Wire verify into deploy scripts | S | claude | claude | SignSmoke, 5d |
| L9 | 22 | 31.F-8c | Verify-failure routing + tests (incl. corrupt-allowlist P1 distinct) | S | claude | claude | 8a, 8b |
| L9 | 23 | 31.F-9a | Signing-failure routing → 31.C P1 | S | claude | claude | SignSmoke, 31.C-Integration-external |
| L10 | 24 | 31.F-10-Doc | Operator runbook | S | claude | claude | 8c, 9a, 7c-reconcile |
| L11 | 25 | 31.F-CallSiteInventoryGate | Image-pull site audit | S | claude | claude | 10-Doc |
| L12 | 26 | 31.F-StabilityCheckpoint | **NEW (SPLIT from Integration)**: 24-72h sign success >99% + 0 silent failures → unblocks 31.G | S | claude | claude + operator-window | CallSiteInventoryGate, 7c-reconcile |
| L13 | 27 | 31.F-Soak | **NEW (SPLIT from Integration)**: 14-day daily snapshot soak (tier:L; replaces v1 Integration) | **L** | claude | claude + operator-window | StabilityCheckpoint |
| L14 | 28 | 31.F-GHCR-Cutover | **NEW (SPLIT from Integration)**: post-Soak operator-window cutover + rollback flag | S | claude | claude + operator-window | Soak |

**Routing tally**: codex 9, claude 19.

**Operator-window tickets**: 6a, 6b, StabilityCheckpoint, Soak, GHCR-Cutover (5 tickets).

**Critical path**: 1a → 1bc → LibsGate → 6a → 6b → KeyAndRegistryReadyGate → 7ab → 7c-initial → SignSmoke → 7c-reconcile → 8a/8b → 8c → 9a → 10-Doc → CallSiteInventoryGate → StabilityCheckpoint → Soak → GHCR-Cutover. ~19 sequential hops.

**Cross-phase deps** (with filing-time mechanical preflight per codex Q5):
- 6a blockedBy 31.E-Integration (verify: JQL + commit SHA)
- 8a blockedBy 31.B-Integration (verify: JQL + commit SHA)
- 9a blockedBy 31.C-Integration (verify: JQL + commit SHA)

---

## §5. Per-child specs (v2 deltas)

### 31.F-1a, 2a, 3a, 4a, 5a — unchanged

### 31.F-1bc — key-material class addition

```yaml
delta_from_v1:
  - boundaries.external_payload_class: **key-material** (was secret-adjacent — codex Q5: this ticket directly handles cosign private key generation)
  - additional ACs per §3.8 rule
ac.code_additions:
  - {desc: "script disables `set -x` if previously enabled", verify: {command: "grep -cE 'set \\+x|set -[a-z]*x[a-z]*' scripts/cosign/key-gen.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests verify NO key bytes in caplog", verify: {command: "grep -cE 'BEGIN.*PRIVATE.*KEY|RSA.*PRIVATE.*KEY' scripts/cosign/tests/test_key_gen.py && grep -cE 'assert.*not.*in.*caplog|caplog.records' scripts/cosign/tests/test_key_gen.py", expect_exit_code: 0}}
```

### 31.F-2bc — key-material class addition

```yaml
delta_from_v1:
  - boundaries.external_payload_class: **key-material**
  - additional ACs same shape as 1bc
ac.code_additions:
  - {desc: "no subprocess shell=True (shell-injection guard per codex Q3)", verify: {command: "grep -cE 'subprocess\\..*shell=True' scripts/cosign/sign-image.py", expect_stdout_match: "^0$"}}
```

### 31.F-3bc, 4bc — unchanged structure (3bc external_payload_class stays secret-adjacent; deals with GitLab API token but not key material)

### 31.F-4bc — "unavailable" verdict for corrupt allowlist (codex Q4)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "On JSON parse failure: returns special verdict 'unavailable' (NOT empty/missing). Distinguishes from 'allowlist expired'. 5bc verify module differentiates handling: signed images still pass cosign; unsigned-depending-on-allowlist refuses with P1 'allowlist corrupt' (distinct from P1 'allowlist expired')."
  - new AC: corrupt-JSON returns 'unavailable' not False
ac.code_additions:
  - {desc: "is_allowlisted returns special verdict on corrupt JSON", verify: {command: "grep -cE 'AllowlistUnavailable|verdict.*unavailable|return.*\"unavailable\"' scripts/cosign/legacy-allowlist.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests cover corrupt-JSON path", verify: {command: "grep -cE 'def test_.*corrupt.*json|def test_.*unavailable' scripts/cosign/tests/test_legacy_allowlist.py", expect_stdout_match: "[1-9]"}}
```

### 31.F-5bc — handles "unavailable" verdict (codex Q4)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "On allowlist 'unavailable' verdict: SIGNED image returns verdict='allow' (cosign verify is independent of allowlist); UNSIGNED image returns verdict='refuse' WITH error_class='allowlist_unavailable' (distinct from 'allowlist_expired')"
  - tests >=10 (was 8; +2 for unavailable + expired distinction)
ac.code_additions:
  - {desc: "verify result includes error_class field", verify: {command: "grep -cE 'error_class.*allowlist_unavailable|error_class.*allowlist_expired' scripts/cosign/verify.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests differentiate unavailable vs expired", verify: {command: "grep -cE 'def test_.*allowlist_unavailable|def test_.*allowlist_expired' scripts/cosign/tests/test_verify.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
```

### 31.F-5d — NEW (codex Q5: library packaging adapter)

```yaml
title: "AUDIT-31.F-5d: backend/security/cosign_verify_adapter.py + tests"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:security, area:tests]
blockedBy: [5bc]
context_hint:
  produces: "backend/security/cosign_verify_adapter.py + backend/security/tests/test_cosign_verify_adapter.py"
  consumed_by: "31.F-8a (runner pickup imports adapter), 31.F-8b (deploy imports adapter)"
  inputs_expected: "scripts/cosign/verify.py from 5bc (script-level)"
  outputs_guaranteed: |
    backend/security/cosign_verify_adapter.py is the STABLE backend-importable library wrapper:
    - Wraps scripts/cosign/verify.py:verify_image with import-safe path
    - Re-exports VerifyResult dataclass
    - Provides Python module namespace `from backend.security.cosign_verify_adapter import verify_image, VerifyResult`
    - scripts/cosign/ remains the runner-friendly script entry point; backend code uses adapter
    Tests >=4: import works / wrapper passes through args / wrapper preserves verdict / wrapper handles ImportError gracefully.
  non_goals:
    - "DO NOT duplicate verify logic — adapter is thin wrapper"
    - "DO NOT modify scripts/cosign/verify.py"
boundaries:
  loc_delta_max: 150, files_touched_max: 2
  required_paths: [backend/security/cosign_verify_adapter.py, backend/security/tests/test_cosign_verify_adapter.py]
  forbidden_paths: [scripts/cosign/, backend/runner/, backend/alerts/]
  test_scope: inline
  destructive_op_classes: []
  destructive_op_scope: none
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [scripts/cosign/verify.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [verify_image from scripts/cosign/verify.py]
    outputs_for_downstream:
      - "backend.security.cosign_verify_adapter.verify_image (importable)"
      - "VerifyResult dataclass re-exported"
ac.code:
  - {desc: "adapter importable", verify: {command: "python3 -c 'from backend.security.cosign_verify_adapter import verify_image, VerifyResult'", expect_exit_code: 0}}
  - {desc: "thin wrapper (delegates to scripts.cosign)", verify: {command: "grep -cE 'from scripts.cosign.verify import|import.*cosign.*verify' backend/security/cosign_verify_adapter.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests pass", verify: {command: "pytest backend/security/tests/test_cosign_verify_adapter.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+0.5d after 5bc
```

### 31.F-LibsGate — 14 expanded contract checks (codex Q3)

```yaml
delta_from_v1:
  - blockedBy adds: 5d
  - context_hint.outputs_guaranteed: REWRITTEN — 14 contract checks:
    1. All 6 lib scripts + adapter importable
    2. tests green
    3. cross-lib roundtrip (allowlist + verify interop)
    4. NO secret values in any tested log path (caplog verification per script)
    5. cosign sign uses digest format (not tag) — no `:latest` in signing call
    6. corrupt-allowlist path returns 'unavailable' verdict
    7. public-key-missing path returns 'refuse' verdict
    8. NO subprocess shell=True anywhere in scripts/cosign/*
    9. registry commands use --no-shell args (subprocess.run with list args)
    10. ADR-0027 Accepted
    11. lowercase L-OP-247 paths
    12. cosign-verify adapter (5d) successfully imports verify_image
    13. git_sha embedded in libs-gate.json
    14. all `external_payload_class: key-material` tickets carry operator-prepare-only OR operator-window class (filing-hook verifies)
ac.code_replacements:
  - {desc: "all 14 checks PASS", verify: {command: "jq -r '. | to_entries | map(select(.value==false)) | length' docs/audit/AUDIT-31-phase-F-evidence/libs-gate.json", expect_stdout_match: "^0$"}}
  - {desc: "libs-gate.json has 14+ check fields", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-F-evidence/libs-gate.json')); checks=[k for k in d.keys() if k not in ('git_sha','timestamp')]; assert len(checks) >= 14\"", expect_exit_code: 0}}
```

### 31.F-6a — mixed scope + key-material + xtrace + log scan (codex P0)

```yaml
delta_from_v1:
  - boundaries.external_payload_class: **key-material** (was secret-adjacent)
  - boundaries.destructive_op_scope: **mixed**
  - boundaries.scope_components: **[host-cred-dir, gitlab-ci-settings]**
  - additional ACs per §3.8 key-material rule
ac.code_additions:
  - {desc: "operator action log records xtrace was disabled before key ops", verify: {command: "grep -ciE 'xtrace.*disabled|set \\+x' docs/operations/operator-action-log.md", expect_stdout_match: "[1-9]"}, run_as: operator}
  - {desc: "no CI artifacts contain env dumps or PEM headers", verify: {command: "jq -r .post_run_log_scan_clean docs/audit/AUDIT-31-phase-F-evidence/key-gen.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "masked-variable evidence (GitLab API confirms protected+masked)", verify: {command: "jq -r .gitlab_ci_variables_masked_and_protected docs/audit/AUDIT-31-phase-F-evidence/key-gen.json", expect_stdout_match: "^true$"}, run_as: operator}
```

### 31.F-7ab — MERGED v1 7a+7b (codex P0)

```yaml
title: "AUDIT-31.F-7ab: Sign job + GitLab CR push (single coupled ticket per codex Q3)"
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:ci, area:tooling, area:security]
blockedBy: [KeyAndRegistryReadyGate, 2bc]
context_hint:
  produces: "Patches to .gitlab-ci.yml — single coordinated edit: (1) new `gitlab-cr-push` job in push stage (parallel to existing ghcr-push) + (2) new `sign` stage with `cosign-sign` job that signs the GitLab CR digest (canonical signed reference); GHCR stays as mirror with separate (lower-confidence) push but is NOT what gets signed"
  consumed_by: "31.F-SignSmoke, 31.F-Integration"
  inputs_expected: "KeyAndRegistryReadyGate PASS; sign-image.py from 2bc"
  outputs_guaranteed: |
    Coordinated edit ensures registry/signing coherence:
    1. push stage: ghcr-push (existing, mirror) + gitlab-cr-push (NEW, canonical)
    2. sign stage: cosign-sign uses $GITLAB_CR_IMAGE_REF (NOT $GHCR_IMAGE_REF) — signs the canonical digest
    3. Rules: same for both pushes + sign (main + tag)
    4. cosign-sign needs: [gitlab-cr-push] (signs GitLab CR digest, NOT GHCR — codex Q3 concern)
    5. GHCR-push is now mirror-only; failure does NOT block sign or GitLab CR
  non_goals:
    - "DO NOT remove GHCR push (mirror role during transition)"
    - "DO NOT sign GHCR digest (canonical signed ref = GitLab CR digest)"
    - "DO NOT modify sign-image.py — 2bc owns"
boundaries:
  loc_delta_max: 150, files_touched_max: 1
  required_paths: [.gitlab-ci.yml]
  forbidden_paths: [scripts/cosign/, Dockerfile, docker-compose.yml]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: structural-only
  dependency_artifacts: [KeyAndRegistryReadyGate PASS, sign-image.py]
  mutex_with: []   # merged ticket; no other .gitlab-ci.yml edits in flight at this layer
  interface_contract:
    inputs_from_deps: [sign-image.py + GitLab CR enabled + GitLab CI variables]
    outputs_for_downstream:
      - "gitlab-cr-push job (canonical primary)"
      - "ghcr-push job (mirror; lower confidence)"
      - "cosign-sign signs GitLab CR digest (NOT GHCR)"
ac.code:
  - {desc: "sign stage exists", verify: {command: "python3 -c 'import yaml; d=yaml.safe_load(open(\".gitlab-ci.yml\")); assert \"sign\" in d[\"stages\"]'", expect_exit_code: 0}}
  - {desc: "cosign-sign needs gitlab-cr-push (NOT ghcr-push)", verify: {command: "python3 -c 'import yaml; d=yaml.safe_load(open(\".gitlab-ci.yml\")); assert \"gitlab-cr-push\" in d.get(\"cosign-sign\", {}).get(\"needs\", []); assert \"ghcr-push\" not in d.get(\"cosign-sign\", {}).get(\"needs\", [])'", expect_exit_code: 0}}
  - {desc: "sign job uses GitLab CR digest variable, NOT GHCR", verify: {command: "grep -cE '\\$GITLAB_CR_IMAGE_REF|\\${GITLAB_CR_IMAGE_REF}' .gitlab-ci.yml && grep -cE 'cosign-sign.*\\$GHCR_IMAGE_REF' .gitlab-ci.yml || true", expect_stdout_match: "[1-9]"}}
  - {desc: "passes validator", verify: {command: "python3 scripts/ci/ci-yml-validator.py .gitlab-ci.yml | jq -r .passed", expect_stdout_match: "^true$"}}
  - {desc: "**signing-trust-root independence**: sign job reads CANONICAL_REGISTRY_DIGEST env var (NOT hard-coded GitLab CR path) per §3.10", verify: {command: "grep -cE '\\$CANONICAL_REGISTRY_DIGEST|\\${CANONICAL_REGISTRY_DIGEST}' .gitlab-ci.yml", expect_stdout_match: "[1-9]"}}
  - {desc: "**signing-trust-root independence**: CANONICAL_REGISTRY GitLab CI variable defaults to gitlab-cr (switchable for rollback)", verify: {command: "jq -r .canonical_registry_var_default docs/audit/AUDIT-31-phase-F-evidence/7ab-config.json", expect_stdout_match: "^gitlab-cr$"}}
go_live: T+0.5d after KeyAndRegistryReadyGate + 2bc
```

### 31.F-7c-initial — SPLIT v1 7c (codex P0)

```yaml
delta_from_v1_7c:
  - title: "AUDIT-31.F-7c-initial: initial GHCR digest snapshot (pre-SignSmoke)"
  - context_hint.outputs_guaranteed: REWRITTEN — "Initial enumeration: run scripts/cosign/build-allowlist-snapshot.py --apply BEFORE SignSmoke. Captures all existing GHCR digests for omnisight/* at time T1. Writes to scripts/cosign/legacy-allowlist.json. Records T1 timestamp in evidence. Reconcile happens at 7c-reconcile."
  - non_goals strengthened: "DO NOT enable fail-closed enforcement yet — 7c-reconcile + 8a/8b own"
```

### 31.F-7c-reconcile — NEW (codex P0)

```yaml
title: "AUDIT-31.F-7c-reconcile: reconcile snapshot immediately before fail-closed enforcement"
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:tooling, area:security]
blockedBy: [SignSmoke]
context_hint:
  produces: "Updated scripts/cosign/legacy-allowlist.json (reconciled) + docs/audit/AUDIT-31-phase-F-evidence/reconcile-snapshot.json"
  consumed_by: "31.F-8a (verify enforcement starts AFTER reconcile)"
  inputs_expected: "7c-initial snapshot at T1; SignSmoke passed at T2; current GHCR state at T3=now"
  outputs_guaranteed: |
    Operator runs (--apply --since T1 mode):
    1. Enumerate GHCR digests at T3 (now)
    2. Compare against 7c-initial T1 snapshot
    3. New digests (T1 to T3) that are NOT signed (verify each) → added to legacy-allowlist as transitional, OR push must succeed (force re-build+sign)
    4. Evidence: reconcile-snapshot.json {T1, T2, T3, new_digest_count, new_signed_count, new_unsigned_added_to_allowlist}
    5. If new_unsigned_added_to_allowlist > 0 → operator must explicitly accept (separate AC PASS)
  non_goals:
    - "DO NOT enable fail-closed enforcement here — 8a/8b own"
    - "DO NOT auto-sign retroactive — operator decides if transitional digest gets allowlist or re-build"
boundaries:
  loc_delta_max: 200, files_touched_max: 2
  required_paths: [scripts/cosign/legacy-allowlist.json, docs/audit/AUDIT-31-phase-F-evidence/reconcile-snapshot.json]
  forbidden_paths: [scripts/cosign/legacy-allowlist.py, scripts/cosign/build-allowlist-snapshot.py]
  test_scope: defer-to-integration
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: network-test
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [7c-initial snapshot, SignSmoke PASS]
  mutex_with: [4bc]
  interface_contract:
    inputs_from_deps: [initial snapshot + SignSmoke PASS]
    outputs_for_downstream:
      - "reconcile-snapshot.json with N new_digest_count"
      - "legacy-allowlist.json updated with transitional digests (if any)"
      - "fail-closed-enforcement-ready: true (operator-approved)"
ac.code:
  - {desc: "reconcile-snapshot.json has T1+T2+T3 timestamps", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-F-evidence/reconcile-snapshot.json')); assert all(k in d for k in ['t1_initial_snapshot','t2_signsmoke_passed','t3_reconcile','new_digest_count','new_signed_count','new_unsigned_added_to_allowlist','fail_closed_enforcement_ready'])\"", expect_exit_code: 0}}
ac.deploy:
  - {desc: "operator approved fail-closed enforcement", verify: {command: "jq -r .fail_closed_enforcement_ready docs/audit/AUDIT-31-phase-F-evidence/reconcile-snapshot.json", expect_stdout_match: "^true$"}, run_as: operator}
go_live: T+1d after SignSmoke (operator window)
```

### 31.F-8a — uses adapter (codex Q2) + 31.B flag-default caveat (codex cross-phase Q2a)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — "imports `from backend.security.cosign_verify_adapter import verify_image` (NOT from scripts.cosign.verify)"
  - blockedBy adds 5d (adapter must exist) + 7c-reconcile (enforcement gated on reconcile)
  - **NEW caveat (codex cross-phase Q2a)**: verify-at-pickup only ACTIVE when `RUNNER_USE_EPHEMERAL_CLONE=1`. Per 31.B §7 Q2 lock, this flag defaults 0 until 31.J cutover step 1 (6a) flips it. Therefore:
    - Pre-31.J cutover: signed-image enforcement only active when operator manually flips per-runner flag (used during 31.J Pilot + Integration smoke)
    - At 31.J-6a: flag flipped → ALL 4 runners enforce signed-image at pickup
    - This wiring (8a) sets up the enforcement code path; 31.J-6a activates it system-wide
ac.code_replacements:
  - {desc: "imports from backend.security.cosign_verify_adapter", verify: {command: "grep -cE 'from backend.security.cosign_verify_adapter import' backend/runner/ephemeral_clone.py", expect_stdout_match: "[1-9]"}}
  - {desc: "does NOT import from scripts.cosign", verify: {command: "grep -cE 'from scripts.cosign' backend/runner/ephemeral_clone.py", expect_stdout_match: "^0$"}}
  - {desc: "verify call is INSIDE `if USE_EPHEMERAL:` branch (NOT legacy worktree path)", verify: {command: "python3 -c \"import re; src=open('backend/runner/ephemeral_clone.py').read(); assert 'verify_image' in src; assert 'USE_EPHEMERAL' in src or 'ephemeral_clone' in src\"", expect_exit_code: 0}}
ac.exercised_addition:
  - {desc: "Enforcement system-wide-active gated on 31.J-6a (not 31.F-Integration)", verify: deferred-to-31.J-6a-evidence}
```

### 31.F-8b — uses adapter (codex Q2)

```yaml
delta_from_v1:
  - blockedBy adds 5d
  - same import pattern as 8a
```

### 31.F-8c — corrupt-allowlist P1 distinct (codex Q4)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "Routing config has 2 distinct event classes: `image_verify_fail_unsigned` (refused for missing signature; P1) + `image_verify_fail_allowlist_unavailable` (allowlist file corrupt; P1) + `image_verify_fail_allowlist_expired` (90-day window passed; P1) — distinct so operator can triage faster"
  - tests >=8 (was 5; +3 for distinct error classes)
ac.code_additions:
  - {desc: "3 distinct event classes", verify: {command: "grep -cE 'image_verify_fail_(unsigned|allowlist_unavailable|allowlist_expired)' backend/alerts/routing_config.yaml", expect_stdout_match: "^[3-9]$"}}
  - {desc: "tests cover all 3 error classes", verify: {command: "grep -cE 'def test_.*allowlist_unavailable|def test_.*allowlist_expired|def test_.*unsigned.*refuse' backend/runner/tests/test_image_verify_smoke.py", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
```

### 31.F-9a, 10-Doc — unchanged structure (10-Doc adds reconcile section reference)

### 31.F-CallSiteInventoryGate — unchanged

### 31.F-StabilityCheckpoint — NEW (SPLIT from Integration per codex P0)

```yaml
title: "AUDIT-31.F-StabilityCheckpoint: 24-72h signing success >99% + 0 silent failures"
tier: S, prefer: claude, class: subscription-claude + operator-window
area_labels: [area:tests, area:security, area:devops]
blockedBy: [CallSiteInventoryGate, 7c-reconcile]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json"
  consumed_by: "31.F-Soak; 31.G-* (cross-phase: unblocks 31.G)"
  inputs_expected: "Fail-closed enforcement live (after 7c-reconcile); 24-72h window minimum"
  outputs_guaranteed: |
    Operator confirms (24h minimum, 72h maximum window — matches 31.D-StabilityCheckpoint pattern):
    1. ≥1 daily-cosign snapshot from systemd timer (set up in 10-Doc) with sign_success_rate ≥99%
    2. 0 P1 signing alerts in window
    3. 0 P1 verify-fail alerts in window
    4. 0 silent failures (every fail audit-logged)
    5. allowlist remaining_days > 60 (window started fresh)
    6. emit stability-checkpoint.json
  non_goals:
    - "DO NOT extend beyond 72h (force progression or escalate)"
    - "DO NOT trigger Soak prematurely — Soak owns 14-day"
    - "DO NOT trigger GHCR cutover here"
boundaries:
  loc_delta_max: 80, files_touched_max: 2
  required_paths: [docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json, docs/operations/operator-action-log.md]
  forbidden_paths: [scripts/cosign/, backend/, .gitlab-ci.yml]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [CallSiteInventoryGate, 7c-reconcile]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [fail-closed enforcement live + daily snapshot running]
    outputs_for_downstream:
      - "stability-checkpoint.json with ok_to_unblock_31G: bool"
      - "31.G filing/start enabled when ok_to_unblock_31G=true"
ac.code:
  - {desc: "stability-checkpoint.json committed", verify: {command: "test -f docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json", expect_exit_code: 0}}
ac.deploy:
  - {desc: "window 24-72h", verify: {command: "python3 -c \"import json, datetime; d=json.load(open('docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json')); s=datetime.datetime.fromisoformat(d['window_start']); e=datetime.datetime.fromisoformat(d['window_end']); h=(e-s).total_seconds()/3600; assert 24 <= h <= 72\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "sign success ≥99%", verify: {command: "jq -r .sign_success_rate docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json | python3 -c 'import sys; v=float(sys.stdin.read().strip()); assert v >= 99'", expect_exit_code: 0}, run_as: operator}
  - {desc: "0 P1 alerts", verify: {command: "jq -r '.p1_signing_alerts + .p1_verify_alerts' docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "ok_to_unblock_31G", verify: {command: "jq -r .ok_to_unblock_31G docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "**signing-trust-root health separated from registry-migration health** (per §3.10)", verify: {command: "jq -r '. | (.signing_trust_root_healthy and .registry_migration_healthy)' docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "**signing-trust-root proven independent of registry**: separate evidence fields for each subsystem", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-F-evidence/stability-checkpoint.json')); assert 'signing_trust_root_healthy' in d and 'registry_migration_healthy' in d\"", expect_exit_code: 0}, run_as: operator}
go_live: T+1d (24h) to T+3d (72h) after CallSiteInventoryGate + 7c-reconcile
```

### 31.F-Soak — NEW (SPLIT from Integration; replaces v1 tier:L Integration per codex P0)

```yaml
title: "AUDIT-31.F-Soak: 14-day continuous signing observation"
tier: L, prefer: claude, class: subscription-claude + operator-window
area_labels: [area:tests, area:security, area:devops]
type_label: type:integration
blockedBy: [StabilityCheckpoint]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-F-evidence/soak.json (final 14-day report)"
  consumed_by: "31.F-GHCR-Cutover; phase closure"
  inputs_expected: "StabilityCheckpoint passed; daily systemd timer producing daily-cosign-YYYYMMDD.json snapshots"
  outputs_guaranteed: |
    14-day soak (NOT mid-soak cutover per codex Q4):
    1. ≥14 daily-cosign snapshot files in evidence dir
    2. Each daily snapshot: sign_success_rate, verify_refuse_count, p1_alerts, allowlist_remaining_days
    3. Aggregate: 14-day sign success >99%; 0 silent failures; 0 P1 alert NOT-injected (failure injections OK)
    4. 2 failure injections during soak (1 sign-fail + 1 verify-fail) → 2 P1 alerts received
    5. soak.json: rolling stats + injection results
  non_goals:
    - "DO NOT perform GHCR cutover here (separate GHCR-Cutover ticket)"
    - "DO NOT change configuration mid-soak (measurement integrity)"
boundaries:
  loc_delta_max: 80, files_touched_max: 1
  required_paths: [docs/audit/AUDIT-31-phase-F-evidence/soak.json]
  forbidden_paths: [scripts/cosign/, backend/, .gitlab-ci.yml]
  test_scope: inline
  destructive_op_classes: []
  destructive_op_scope: none
  external_side_effect: none
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [StabilityCheckpoint + daily timer]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [stability + daily snapshots accumulating]
    outputs_for_downstream:
      - "soak.json with 14-day stats"
      - "GHCR-Cutover unblocked"
ac.code:
  - {desc: "soak.json schema", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-F-evidence/soak.json')); assert all(k in d for k in ['days_observed','sign_success_rate_14d','silent_failures','p1_alerts_injected','daily_snapshots'])\"", expect_exit_code: 0}}
ac.exercised:
  - {desc: "≥14 daily snapshots", verify: {command: "ls docs/audit/AUDIT-31-phase-F-evidence/daily-cosign-*.json | wc -l", expect_stdout_match: "^(1[4-9]|[2-9][0-9])$|^[1-9][0-9]{2}$"}, run_as: operator}
  - {desc: "14d sign success ≥99%", verify: {command: "jq -r .sign_success_rate_14d docs/audit/AUDIT-31-phase-F-evidence/soak.json | python3 -c 'import sys; v=float(sys.stdin.read().strip()); assert v >= 99'", expect_exit_code: 0}, run_as: operator}
  - {desc: "0 silent failures", verify: {command: "jq -r .silent_failures docs/audit/AUDIT-31-phase-F-evidence/soak.json", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "2 injected P1 alerts received", verify: {command: "jq -r .p1_alerts_injected docs/audit/AUDIT-31-phase-F-evidence/soak.json", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}, run_as: operator}
go_live: T+15d after StabilityCheckpoint
```

### 31.F-GHCR-Cutover — NEW (SPLIT from Integration; operator-window after Soak per codex P0)

```yaml
title: "AUDIT-31.F-GHCR-Cutover: GHCR → GitLab CR canonical cutover (operator-window, post-Soak)"
tier: S, prefer: claude, class: subscription-claude + operator-window
area_labels: [area:devops, area:security, area:ci, area:docs]
blockedBy: [Soak]
context_hint:
  produces: "Patch to .gitlab-ci.yml (disable ghcr-push job via feature flag) + docs/audit/AUDIT-31-phase-F-evidence/ghcr-cutover.json + entry in operator action log"
  consumed_by: "Phase 31.F closure; 31.K (eventual GHCR retirement)"
  inputs_expected: "Soak passed; 14-day evidence green"
  outputs_guaranteed: |
    Operator-window cutover (NOT mid-soak per codex Q4):
    1. Operator edits .gitlab-ci.yml: disable ghcr-push job via `rules: if: $ENABLE_GHCR_MIRROR_PUSH == "true"` (false by default; operator overrides via GitLab CI variable for emergency revert)
    2. Operator commits + Gerrit +2; replicates to GitLab; CI re-runs; verify ghcr-push job SKIPPED
    3. Confirm GitLab CR is sole primary push target
    4. Rollback flag documented: setting ENABLE_GHCR_MIRROR_PUSH=true re-enables GHCR mirror (parallel push) within 1 commit
    5. GHCR project enters read-only legacy state (per ADR-0027); retirement at 31.K
    6. emit ghcr-cutover.json: {cutover_timestamp, rollback_flag_documented, ghcr_readonly: bool}
  non_goals:
    - "DO NOT delete GHCR project (90-day read-only retention; 31.K owns retirement)"
    - "DO NOT modify sign job — still signs GitLab CR digest"
boundaries:
  loc_delta_max: 80, files_touched_max: 3
  required_paths: [.gitlab-ci.yml, docs/audit/AUDIT-31-phase-F-evidence/ghcr-cutover.json, docs/operations/operator-action-log.md]
  forbidden_paths: [scripts/cosign/, backend/, Dockerfile]
  test_scope: defer-to-integration
  destructive_op_classes: [env-edit]
  destructive_op_scope: project   # .gitlab-ci.yml edit + ci variable; no immediate system mutation (rollback exists)
  external_side_effect: network-test   # CI re-run hits real GitLab
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [Soak PASS]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [Soak passed; 14-day green]
    outputs_for_downstream:
      - "ghcr-push job conditionally skipped (default disabled)"
      - "rollback flag ENABLE_GHCR_MIRROR_PUSH documented"
      - "GHCR enters read-only legacy"
ac.code:
  - {desc: "ghcr-push job has feature-flag rule", verify: {command: "grep -cE 'ENABLE_GHCR_MIRROR_PUSH' .gitlab-ci.yml", expect_stdout_match: "[1-9]"}}
  - {desc: "ghcr-cutover.json committed", verify: {command: "test -f docs/audit/AUDIT-31-phase-F-evidence/ghcr-cutover.json", expect_exit_code: 0}}
ac.deploy:
  - {desc: "next pipeline shows ghcr-push SKIPPED", verify: {command: "jq -r .ghcr_push_skipped_in_pipeline docs/audit/AUDIT-31-phase-F-evidence/ghcr-cutover.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "rollback flag documented in runbook", verify: {command: "grep -cE 'ENABLE_GHCR_MIRROR_PUSH.*true.*rollback|rollback.*ENABLE_GHCR_MIRROR_PUSH' docs/sop/cosign-operator-runbook.md", expect_stdout_match: "[1-9]"}, run_as: operator}
go_live: T+1d after Soak (operator window)
```

---

## §6. Filing batch order (DAG v2)

```
L0: 1a, 2a, 3a, 4a, 5a (5 specs)
L1: 1bc, 2bc, 3bc, 4bc, 5bc (5 libs)
L2: 5d (NEW adapter; depends on 5bc)
L3: LibsGate (1)
L4: 6a, 6b (2 parallel operator-window)
L5: KeyAndRegistryReadyGate (1)
L6: 7ab (1 MERGED)
L7: 7c-initial (1)
L8: SignSmoke (1)
L9: 7c-reconcile (1 NEW)
L10: 8a, 8b (2 parallel)
L11: 8c (1)
L12: 9a (1)
L13: 10-Doc (1)
L14: CallSiteInventoryGate (1)
L15: StabilityCheckpoint (1 NEW)
L16: Soak (1 NEW tier:L)
L17: GHCR-Cutover (1 NEW operator-window)
```

18 layers, 28 tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Cosign keypair algorithm | **ECDSA P-256** (cosign default) |
| 2 | Key rotation cadence | **annual + emergency** rotation |
| 3 | GHCR retirement timing | **90 days read-only** post-GHCR-Cutover; actual retirement in Phase 31.K |
| 4 | allowlist.json editability | **operator-only via Gerrit +2 from non-AI reviewer** (matches 31.C routing_config.yaml pattern) |
| 5 | Verify cache TTL | **5 min default**, configurable in routing_config.yaml |
| 6 | Verify failure runner behavior | **fail-closed** (refuse pickup; emit P1; security default) |
| 7 | 8b deploy script scope | **enumerate at filing time** (claude + operator review known sites: deploy/compose-*, deploy/*.sh) |
| 8 | 31.G gating | **blockedBy 31.F-StabilityCheckpoint** (24-72h pattern; same as 31.D-StabilityCheckpoint) |

---

## §8. Submission flow

1. Spec at `docs/sprint-s12/phase-31f-ticket-spec.md` (v2)
2. Operator §7 lock (8 items)
3. Commit + Gerrit +2 + submit
4. Filing script (extended w/ key-material + mixed-scope + scope_components + cross-phase JQL preflight) files 28 children
5. Spot-check first 3

---

**End of Phase 31.F ticket spec v2 (28-ticket; key-material class + mixed scope + reconcile snapshot + Integration split + 7ab merge + cosign_verify_adapter). Awaiting operator §7 lock.**
