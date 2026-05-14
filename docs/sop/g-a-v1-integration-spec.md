# G.A-v1 Contract Integration — Operator Spec

**Status**: shipped as part of S12.G G.A-v1 sprint (V1-1..V1-19, 2026-05-14).
**Spec**: `docs/sprint-s12/sprint-s12g-ga-v1-spec.md` §3.2 + master S12.G v2 §0.
**Authoritative test**: `governance_engine/tests/test_ga_v1_integration.py` (OP-1097).
**Predecessor**: [G.A-v0 kernel](g-a-v0-kernel-spec.md).

## What v1 ships

The v1 contract extends the v0 ticket kernel with five operational
extensions and the cross-phase preflight that depends on them:

| Piece                                  | File                                                       | Built in |
| -------------------------------------- | ---------------------------------------------------------- | -------- |
| v1 Pydantic contract + validators      | `governance_engine/schema/v1.py`, `v1_validators.py`       | V1-1..V1-2 |
| Static JSON Schema export + parity tests | `schema/v1.schema.json`, `tests/test_v1_json_schema_round_trip.py` | V1-3..V1-4 |
| Plugin contract + loader + 11 stubs    | `plugins/base.py`, `registry.py`, `phase_31[a-k].py`       | V1-5..V1-7 |
| Plugin-version preflight               | `preflight/plugin_version.py`                              | V1-15 |
| 6 roster-only preflight modules        | `preflight/{credential_escalation,cross_phase_blockers,dependency_artifacts,meta_blockedby,mutex_consistency,path_reciprocity}.py` | V1-9..V1-14 |
| Deterministic orchestrator             | `preflight/orchestrator.py`                                | V1-16 |
| Roster GPG verifier + fixture          | `security/roster_verifier.py`, `tests/fixtures/sample-roster.yaml{,.asc}` | V1-17..V1-18 |
| Integration proof (this ticket)        | `tests/test_ga_v1_integration.py`                          | V1-19 |

### Schema extensions

1. **`schema_version`** — admits `v0` or `v1` so legacy tickets keep validating during rollout.
2. **`ticket_key` / `labels` / `blocked_by`** — structured roster identity, replacing v0-era prose blocker refs.
3. **`cross_phase_blockers`** — typed `{blocker_phase, blocker_artifact, reason}` objects (v2 §0 lines 56–58).
4. **`phase_plugin_version`** — locks every roster ticket to a single plugin generation (§0 line 45). Mismatched ticket/plugin generations are surfaced by `plugin-version-mismatch`.
5. **Payload + evidence enum extensions** — `external_payload_class` gains `credential-material` (§0 line 63); `evidence_class` gains `structural`, `behavioral-smoke`, `semantic`, `longitudinal` (§0 lines 64–65).

## Flow of a v1 ticket through the contract

```
payload (YAML / dict)
   │
   ▼  TicketContractV1.model_validate(payload)         # schema gate
   │  raises ValidationError on shape / enum / missing-field issues
   ▼  jsonschema.validate(payload, v1.schema.json)     # non-Python parity
   │  same shape, same enums, no Python required
   ▼  DEFAULT_REGISTRY.validate_all(contract)          # per-phase plugins
   │  dispatches on phase:<id> label; returns list[PluginError]
   ▼  run_all_preflight_checks(roster, registry, …)    # 7 cross-phase rules
   │  returns [] when clean; list[PreflightError] otherwise
   ▼  verify_roster_signature(roster_path, fingerprint) # L1 signature gate
```

## Seven preflight rules

`run_all_preflight_checks(roster, registry, claimed_child_count)` runs
the seven cross-phase rules in stable report order. Each error exposes
a stable `rule_id`:

1. **`plugin-version-mismatch`** — every v1 ticket needs a phase plugin
   whose `plugin_version="v1"` and whose `schema_versions_supported`
   admits the ticket's `schema_version`.
2. **`credential-material-not-l1-non-subscription`** — payloads marked
   `credential-material` must run on `authority_required="L1"` AND a
   ticket class that is **not** subscription-*.
3. **`cross-phase-blocker-shape`** — `blocker_phase` must match
   `^31\.[A-K]$` or be in `{G.A-v0, G.A-v1, G.B, G.C, G.D}`;
   `blocker_artifact` is RFC-1123-ish (`^[a-z][a-z0-9-]{0,62}$`);
   `reason` is non-empty (≤500 chars).
4. **`dependency-orphan`** — every artifact in a ticket's
   `dependency_artifacts` must appear in some **other** roster ticket's
   `cross_phase_blockers[].blocker_artifact`.
5. **`meta-blockedby-count-mismatch`** — for each meta ticket the caller
   supplies an expected child count; the preflight asserts the actual
   number of roster tickets that list the meta in `blocked_by` matches.
6. **`mutex-asymmetric`** — two non-meta tickets sharing any
   `scope_components` entry must declare each other in `mutex_with`.
7. **`path-collision`** — if ticket A `required_paths` lists path `X`,
   no other ticket may include `X` in its `forbidden_paths`.

Each rule honours an individual disable env-var of the form
`OMNISIGHT_PREFLIGHT_<rule_id>_ENABLED=0` (S12.G v2 §4 risk register).

## Invoking the preflight in-process

The G.A-v1 sprint does **not** ship a standalone preflight CLI — the
orchestrator is intentionally a pure function so operators and CI can
compose it directly:

```python
from governance_engine.plugins.registry import DEFAULT_REGISTRY
from governance_engine.preflight.orchestrator import run_all_preflight_checks
from governance_engine.preflight.plugin_version import PluginRegistry
from governance_engine.schema.v1 import TicketContractV1

roster = [TicketContractV1.model_validate(p) for p in payloads]
registry = PluginRegistry(plugins={p.phase_id: p for p in DEFAULT_REGISTRY.all()})
errors = run_all_preflight_checks(roster, registry, claimed_child_count)
if errors:
    raise SystemExit(f"{len(errors)} preflight error(s): {errors}")
```

The integration proof at `governance_engine/tests/test_ga_v1_integration.py`
is the canonical worked example — it composes the four runtime gates
against an 18-ticket synthetic roster that exercises every v1 field at
least once, plus seven mutation tests (one per `rule_id`).

## Pinning the roster

V1-18 ships `tests/fixtures/sample-roster.yaml` plus its detached L1 GPG signature (`.asc`).
`verify_roster_signature(roster_path, trusted_fingerprint)` is the entry point for
operators verifying the production roster before any L2-deputy authority is honored.
The verifier shells out to `gpg --verify` (no Python dep, S12.G v2 §7) and returns a
graceful `VerifyResult(is_valid=False)` when the gpg binary is missing.

## Boundaries

`governance_engine/schema/`, `plugins/`, `preflight/`, and `security/` are owned by
V1-1..V1-18. V1-19 only consumes them — no new fields, no new rules, no edits to
earlier-sibling deliverables. This document and the integration test are the only
two files V1-19 touches (per OP-1097 §3 boundary block).
