# v2-AlertBridge — AM-bridge migration smoke test design

**Ticket**: OP-1157 (`v2-AlertBridge-AMMigrationTest`)
**Authored**: 2026-05-16
**Status**: design doc + implementation skeleton; the real Alertmanager
adapter is a separate future ticket (see §9).
**Scope**: `docs`, `tests`. No runtime changes; `backend/alerting/bridge.py`
is NOT modified by this ticket. Boundary-block exemption justified in the
ticket description.
**Authority**: Sprint S12.G v1.4 spec §"Shared infrastructure"; parent
contract [`2026-05-16-v2-alertbridge-framework-contract.md`](./2026-05-16-v2-alertbridge-framework-contract.md)
§9 (channel adapter ABI) and §10 (AM-bridge migration contract). Codex
P1-7 review amendment (see §2.1) ratified the invariant change from
**byte-equivalence of raw payloads** to **byte-equivalence of canonical
envelopes after AM-injected-field stripping**.
**Implementation skeleton**:
[`backend/tests/test_alerting_am_migration_smoke.py`](../../backend/tests/test_alerting_am_migration_smoke.py).

---

## §0. Why this document exists

OP-1144 (the framework contract) promises operators a one-line config
swap from `channel_adapter: stdout_email` to
`channel_adapter: alertmanager_webhook` with **no rule edits and no
rule-ticket re-files**. That promise is only as good as the test that
proves it. This document specifies that test.

The test is a *smoke* test in two distinct senses:

1. **Pre-deploy smoke**: it runs in `pytest` as part of CI, against a
   mock Alertmanager (AM) webhook adapter. No real AM instance is
   required, so any backend developer can run it from a laptop. This is
   the version implemented in
   `backend/tests/test_alerting_am_migration_smoke.py`.

2. **Operator smoke** (out of scope for this ticket): once a real AM
   adapter ships, the same canonical-envelope contract will be replayed
   against a live AM in the staging cluster. That replay reuses the
   strip list (§3) and preserve list (§4) defined here verbatim; the
   only delta is the transport. The future ticket cites this doc's
   `§3-strip-list` and `§4-preserve-list` anchors as the authoritative
   reference.

The two senses share a single mathematical invariant — the canonical
envelope contract — so a passing pre-deploy smoke test means the
operator smoke test is expected to pass too, modulo network plumbing.

---

## §1. Test scenario

The smoke test fires a synthetic alert through two adapters in
parallel and compares the *canonical envelopes* produced from each
adapter's payload.

```
        ┌──────────────────────┐
        │  synthetic alert     │
        │  (rule + labels +    │
        │   annotations)       │
        └──────────┬───────────┘
                   │
        ┌──────────┴──────────┐
        │ AlertBridge.fire()  │   (one call, two adapters)
        └──────────┬──────────┘
                   │
        ┌──────────┼──────────────────────────────────┐
        │          │                                  │
        ▼          ▼                                  │
  StdoutEmail   MockAlertmanagerWebhook  ◀── test helper, not
   Adapter      Adapter (in this file)        production code
        │          │
        ▼          ▼
   stdout JSON   AM-shaped JSON
   (1 line)     (fingerprint, startsAt, endsAt,
                 generatorURL, receiver labels added)
        │          │
        ▼          ▼
  canonical_envelope(payload)
        │          │
        ▼          ▼
       ┌──── must be byte-equal ────┐
```

The synthetic alert is a faithful copy of `runner_claim_stale` (the
same fixture the v2-AlertBridge-1bc tests in
`backend/tests/test_alerting_bridge.py` already use). Using the same
fixture is intentional: it keeps the smoke test diff against the
existing bridge tests trivial to review, and it means a regression in
the contract shows up in both files simultaneously.

### §1.1 Synthetic alert payload

| Field             | Value                                                                 |
|---|---|
| `alertname`       | `runner_claim_stale`                                                  |
| `severity`        | `warn` (page-class smoke also exercised; see §1.2)                    |
| `area`            | `runner`                                                              |
| `family`          | `10` (ASCII digit, not `⑩`; see framework contract §1.1)               |
| `defense_dimension` | `D1`                                                                |
| `labels`          | `{severity, area, family, defense_dimension, instance: runner-a}`     |
| `annotations`     | `{summary, description, runbook_url, remediation_hint}` (all four required by the framework contract §2) |
| `critical_labels` | `("instance",)`                                                       |

### §1.2 Severity matrix

The test parametrises over `severity ∈ {info, warn, page}` — the full
enum from framework contract §3. This catches the routing-class bug
where a future AM adapter forgets to honour the `info`-stdout-only
policy from `SEVERITY_CHANNEL_MAP` (`backend/alerting/bridge.py:34`).
Per the implementation skeleton in
`backend/tests/test_alerting_am_migration_smoke.py` (≥4 tests, ≥200
lines), the parametrisation is implicit — the four named tests cover
each branch explicitly without `pytest.mark.parametrize`, because each
branch carries a different oracle.

---

## §2. Canonical envelope contract

The invariant is:

> For every synthetic alert `A`, for every pair of adapters
> `(stdout_email, alertmanager_webhook)`, the canonical envelopes
> `canonical_envelope(stdout_payload(A))` and
> `canonical_envelope(am_payload(A))` MUST be byte-equal.

`canonical_envelope()` is defined at `backend/alerting/bridge.py:353`
(OP-1103). It takes either an `AlertEnvelope` dataclass instance or an
adapter-side `Mapping[str, object]` payload (post-wire), and returns a
deterministic `dict[str, object]` with:

- the 12 contract fields from the docstring of `bridge.py` (alertname,
  severity, area, family, defense_dimension, labels, annotations,
  dedupe_key, fired_at, resolved_at, critical_labels);
- labels and annotations sorted by key (so dict-iteration order can't
  cause the byte comparison to fail);
- AM-injected fields stripped on the Mapping branch
  (`bridge.py:371-374`), but emitted intact on the AlertEnvelope
  branch (no AM injection has happened yet).

### §2.1 Why byte-equivalence of *canonical* envelopes (Codex P1-7)

Earlier drafts of this ticket required byte-equivalence of the *raw*
payloads. Codex's P1-7 review pointed out that this is impossible: AM
*will* inject `fingerprint`, `startsAt`, `endsAt`, `generatorURL`, and
receiver-routing labels. Demanding raw byte equivalence would force
the migration test to either (a) shim those fields out of AM (changing
production behaviour to satisfy the test — backwards), or (b) fail
forever, gating no migration. Neither is acceptable.

The amended invariant — canonical envelope byte-equivalence, after
strip — captures what operators actually care about: the rule contract
(severity, area, family, defense_dimension, annotations) survives the
swap unchanged. AM-side metadata is welcomed; it just isn't part of
the contract.

### §2.2 Why byte-equivalence, not just structural equality

Two `dict[str, object]` values that compare `==` in Python may not be
byte-equal once serialised, because JSON serialisation has knobs:
key order, whitespace, float vs int representation, etc. The bridge
adapters pin those knobs (`sort_keys=True`, `separators=(",", ":")`
at `bridge.py:255`) precisely so the wire form is canonical. The
smoke test therefore compares `json.dumps(..., sort_keys=True,
separators=(",", ":"))` of both envelopes, not just the dicts. A test
that compared dict equality would miss a regression where an adapter
silently drops `sort_keys=True` and starts emitting iteration-order
JSON.

---

## §3. Strip list — fields AM injects that MUST be removed

The following fields are emitted by Alertmanager (or by a webhook
receiver in front of AM) and MUST be stripped before the canonical
envelope comparison. The current implementation in
`bridge.py:371-374` strips a subset of these on the
`Mapping`-input branch:

```python
for key in ("fingerprint", "startsAt", "endsAt", "generatorURL", "receiver"):
    labels.pop(key, None)
    annotations.pop(key, None)
```

### §3-strip-list (authoritative; future tickets cite this anchor)

| Field             | Where AM injects it | Why it varies                                   | Stripped by                    |
|---|---|---|---|
| `fingerprint`     | Top-level + label   | SHA over all labels at receive time             | `bridge.py:371` (pop from labels & annotations) + smoke-test helper drops top-level |
| `startsAt`        | Top-level           | Set at first-firing receive; floats with clock skew | `bridge.py:371` + smoke-test helper drops top-level |
| `endsAt`          | Top-level           | Set at resolve time; absent on firing            | `bridge.py:371` + smoke-test helper drops top-level |
| `generatorURL`    | Top-level           | Prometheus rule-eval URL; deployment-specific    | `bridge.py:371` + smoke-test helper drops top-level |
| `receiver`        | Label injected by AM route | Route-side configuration, NOT rule-side  | `bridge.py:371`                |
| `route`           | Label, route-injected | Same as receiver; route-side                   | smoke-test helper extends strip list to include `route` |
| `inhibit_rule_match` | Label, inhibition-rule-injected | Inhibition rules add this; route-side  | smoke-test helper extends strip list |
| `silence_id`      | Label, silence-injected | Silences add a UUID label; ephemeral          | smoke-test helper extends strip list |
| `groupKey`        | Top-level + label   | AM groups multiple alerts; deployment-specific  | smoke-test helper drops top-level + label |
| `commonLabels`    | Top-level           | AM emits the intersection of all alert labels in a group | smoke-test only ever asserts on a single alert; not present |
| `commonAnnotations` | Top-level         | Same                                            | not present in single-alert smoke                |
| `status`          | Top-level           | AM emits `firing` / `resolved`; the canonical envelope encodes this via `resolved_at` being non-null | smoke-test helper translates `status: resolved` → `resolved_at`, then drops `status` |
| `version`         | Top-level           | AM webhook payload schema version (`"4"`)        | smoke-test helper drops |
| `externalURL`     | Top-level           | AM server URL; deployment-specific               | smoke-test helper drops |
| `truncatedAlerts` | Top-level           | AM emits when a group exceeds `group_max_alerts` | smoke-test only fires single alerts; helper drops if present |

The smoke-test helper (`_normalize_am_payload` in
`backend/tests/test_alerting_am_migration_smoke.py`) is the operational
strip — it applies the full table above before passing the result to
`canonical_envelope()`. The bridge's own strip
(`bridge.py:371`) is the in-process backstop and covers the labels
that AM injects *into the labels map* (versus top-level). Both must
agree on the union, and the smoke test asserts that they do.

### §3.1 What is *not* on the strip list, intentionally

- `dedupe_key` — set by OmniSight at fire time
  (`DedupeKeyGen.generate(...)`), invariant across adapters. Any AM
  installation that re-derives a fingerprint MUST NOT clobber our
  `dedupe_key`; the framework contract §4 pins this.
- `alertname` — AM forwards it unchanged.
- `fired_at` — OmniSight emits this from `_clock()` at fire time;
  AM's `startsAt` is *not* a substitute (AM's clock differs).

---

## §4. Preserve list — contract fields that MUST round-trip

These fields are the rule contract. They MUST survive the round-trip
through any adapter; the smoke test asserts byte-equality of each.

### §4-preserve-list (authoritative; future tickets cite this anchor)

| Field               | Authority                              | Lint enforces             |
|---|---|---|
| `alertname`         | Framework contract §1                  | promote-alert lint        |
| `severity`          | Framework contract §1, §3              | yes — enum `{page,warn,info}` |
| `area`              | Framework contract §1                  | yes — enum                |
| `family`            | Framework contract §1, §1.1            | yes — ASCII digit         |
| `defense_dimension` | Framework contract §1                  | yes — enum `D1..D5`       |
| `summary`           | Framework contract §2                  | yes — required annotation |
| `description`       | Framework contract §2                  | yes                       |
| `runbook_url`       | Framework contract §2                  | yes                       |
| `remediation_hint`  | Framework contract §2                  | yes                       |
| `dedupe_key`        | Framework contract §4                  | runtime validator         |
| `critical_labels`   | Framework contract §4                  | runtime validator         |
| `fired_at`          | OmniSight-side timestamp               | n/a (set by bridge)       |
| `resolved_at`       | OmniSight-side timestamp; framework §5 | runtime validator         |
| **all other rule-declared labels** not in §3 | (instance, image_repo, phase, ticket_id, …) | cardinality lint (§6 of framework) |

### §4.1 "Rule-declared labels not in the strip list"

The synthetic alert in §1.1 declares `instance: runner-a`. That label
is rule-emitted, not AM-injected, so it lives in `labels.instance` and
MUST round-trip. The smoke test asserts on it explicitly so a future
strip-list bug that over-strips (e.g. accidentally adding `instance`
to the strip list) fails loudly rather than silently.

---

## §5. Rollout path

The framework contract §10 promises a zero-surprise migration. The
phased rollout below is how that promise is operationalised. Each
phase has a defined entry gate, a defined exit gate, and a defined
rollback (§6).

### Phase a — deploy AM, all rules still on stdout+email

- **Entry**: AM container running in staging, reachable from the
  bridge. Smoke test (§7) green in CI.
- **Action**: deploy a real-AM channel adapter alongside the existing
  `stdout_email` adapter. Config remains
  `channel_adapter: stdout_email` for every rule.
- **Exit**: bridge serves both adapters from the same envelope schema
  (no rule receives the AM-routed envelope yet); `healthcheck` of the
  new adapter returns OK against the staging AM.

### Phase b — flip one canary rule to `alertmanager_webhook`

- **Entry**: phase (a) exit gate met.
- **Action**: change `channel_adapter` for ONE low-traffic rule
  (recommendation: `runner_claim_stale`, since it's the fixture the
  smoke test pins) from `stdout_email` to `alertmanager_webhook`.
- **Exit**: alert fires through AM; AM forwards to the OmniSight
  receiver; canonical envelope at the receiver matches the canonical
  envelope the bridge emitted (operator smoke; same code as the §1
  scenario, run against live AM).

### Phase c — verify smoke test passes against live AM

- **Entry**: phase (b) exit gate met.
- **Action**: a 24-hour soak. Every fire of the canary rule is
  shadow-compared (the bridge emits *both* the stdout and the AM
  payload; an audit log captures both; an audit job runs
  `canonical_envelope()` on each pair and asserts byte equality).
- **Exit**: zero envelope mismatches in 24 hours.

### Phase d — flip remaining rules

- **Entry**: phase (c) exit gate met.
- **Action**: change `channel_adapter` for every other rule. No rule
  file is edited; only the framework-level config map.
- **Exit**: every rule firing in the next 7 days has a green
  canonical-envelope shadow-compare.

---

## §6. Rollback path

If any phase fails its exit gate, rollback is **one config flip**:

```diff
-    channel_adapter: alertmanager_webhook
+    channel_adapter: stdout_email
```

Rule files are NOT edited during rollback. The framework contract §10
promises rule files are immutable under migration, and the rollback
keeps that promise symmetric.

A failed phase (b) leaves exactly one rule mis-routed; rolling it back
restores the pre-migration steady state. A failed phase (d) means a
broader fleet rollback — the framework config maps every rule's
adapter in one file, so the rollback diff is mechanically the same
size whether it touches one rule or all of them.

The migration must never enter a state where the only path forward is
a rule-file edit, because that would prove the framework contract §10
wrong. If such a state appears mid-migration, halt and re-file a new
contract ticket — do not patch the rule files.

---

## §7. Failure handling — AM unreachable mid-migration

The AM adapter MUST treat AM unreachability as a per-fire transient,
not as a per-rule configuration error. The framework contract §9
specifies the fall-back behaviour and the smoke test enforces it:

| Failure mode                                  | Bridge behaviour              | Alert loss? |
|---|---|---|
| AM webhook returns 5xx                        | Fall through to stdout+email  | No          |
| AM webhook times out (`> 5s`)                 | Fall through to stdout+email  | No          |
| AM TLS handshake fails                        | Fall through to stdout+email  | No          |
| AM webhook returns 2xx but body is non-200 JSON | Log warning; treat as delivered | No        |
| AM container missing entirely                 | Adapter `healthcheck()` returns `ok=False`; bridge falls back to stdout+email per fire | No |

The "fall through to stdout+email" path is not retried — it is a
*delivery* path, not a *queue*. Re-firing the same alert at the next
evaluation interval is the standard recovery mechanism (per framework
contract §5 on the resolve-policy and the dedupe key in §4).

The smoke test cannot dial real AM, so it asserts the fall-through by
constructing a `_FailingMockAlertmanagerWebhookAdapter` that always
raises, and verifying the bridge surfaces a controlled error rather
than swallowing the alert. The real-AM implementation will follow the
same contract; that test is gated by the future real-AM-adapter
ticket and is out of scope here.

---

## §8. Test assertions — strip / preserve correspondence

The strip list (§3) and preserve list (§4) MUST match the test
assertions byte-for-byte. This is enforced two ways:

1. **By construction**: the smoke-test helper
   `_AM_INJECTED_FIELDS` constant in
   `backend/tests/test_alerting_am_migration_smoke.py` is the exact
   union of the strip list above. `test_strip_list_removes_am_injected_fields`
   iterates that constant and asserts each field is absent post-
   normalisation.

2. **By cross-reference**: the docstring of
   `test_strip_list_removes_am_injected_fields` cites this doc's
   §3-strip-list anchor. A future reviewer changing one without the
   other will see the mismatch on PR review (and the lesson in
   `docs/sop/lessons-learned.md` reminds reviewers to check both).

The preserve list is enforced by
`test_preserve_list_keeps_contract_fields`, which asserts byte
equality of each preserve-list field across the stdout and AM
adapters.

---

## §9. Out of scope — the real-AM adapter

This ticket DOES NOT ship the real AM adapter. The implementation
skeleton in `backend/tests/test_alerting_am_migration_smoke.py` uses
a `MockAlertmanagerWebhookAdapter` test helper that synthesises the
AM-shaped payload in-process. The future real-AM-adapter ticket
(TBD) MUST:

- implement `class AlertmanagerWebhookAdapter` in
  `backend/alerting/bridge.py` (or a sibling module);
- expose `from_env()` per the convention in
  `StdoutEmailAdapter.from_env()` (`bridge.py:214`);
- reuse `canonical_envelope()` unchanged;
- pass every test in
  `backend/tests/test_alerting_am_migration_smoke.py` with its
  `MockAlertmanagerWebhookAdapter` swapped for the real one;
- cite this doc's `§3-strip-list` and `§4-preserve-list` anchors in
  its commit message.

---

## §10. References

- Framework contract:
  [`2026-05-16-v2-alertbridge-framework-contract.md`](./2026-05-16-v2-alertbridge-framework-contract.md)
- Runtime implementation: `backend/alerting/bridge.py` (OP-1103,
  merged)
- Existing 11 bridge tests:
  `backend/tests/test_alerting_bridge.py`
- This doc's test skeleton:
  `backend/tests/test_alerting_am_migration_smoke.py`
- Lint counterpart (in-progress): OP-1151
  (`v2-AlertBridge-2bc`)
- Codex P1-7 review amendment ratifying the canonical-envelope
  invariant: cited in §2.1.
