# AlertRule contract — required schema for every v2-* Prometheus rule

**Status**: authored 2026-05-14, OP-1102 (v2-AlertBridge-1a)
**Owner**: orchestration / on-call
**Scope**: every alert rule that ships under `deploy/prometheus/rules/` once the
v2-AlertBridge framework is live. Existing rules in
`deploy/prometheus/orchestration_alerts.rules.yml` (O9 / #272) keep their
`subsystem` shape until the rule-promotion lint (v2-AlertBridge-2bc) flips
them — they are grandfathered, not exempt.
**Enforced by**: `scripts/promote-alert-rule.py` lint (v2-AlertBridge-2bc) +
`backend/alerting/bridge.py` runtime validator (v2-AlertBridge-1bc). The lint
runs in CI and pre-commit; the runtime validator hard-fails container start
if a registered rule violates the schema.
**Spec source**: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md`
§3 *Shared infrastructure*. This document is the consumer-facing rendering of
that spec section — the spec is the source of truth; this doc is what rule
authors read.

## 1. Why this exists

OmniSight is filing AlertRule tickets (`v2-⑤-AlertRule`, `v2-⑥-AlertRule`,
`v2-⑩-AlertRule`) **before** the Alertmanager (AM) integration in 31.G ships.
That order is deliberate (Q6 operator decision): the rules ship now against
a stdout-plus-email channel adapter, and the future swap to AM is supposed
to be a one-line config change with **no rule-file edits**.

That promise only holds if every rule speaks the same dialect. Seven
well-known AM-integration pitfalls have bitten Prometheus shops before us;
the contract below pre-bakes a mitigation for each so the swap is a
non-event.

| # | Pitfall | Mitigation baked in here |
|---|---|---|
| P1 | Label / annotation drift between rule and AM | §3 fixes the required-label + required-annotation schema |
| P2 | Severity-vocabulary mismatch (`critical` vs `page` vs `P1`) | §4 fixes the enum to `{page, warn, info}` |
| P3 | Dedupe key collisions across rules | §5 fixes the dedupe-key shape and the "critical labels" declaration |
| P4 | Resolved-notification surprise (paging on resolve) | §6 fixes the resolve-emission policy per severity |
| P5 | Time-series cardinality explosion | §7 caps any single label at ≤ 10 distinct values |
| P6 | Receiver routing inconsistency (rule-side vs route-side) | §8 keeps routing in framework config, never in the rule |
| P7 | `group_by` / `group_wait` fragmentation | §9 fixes the standard grouping for every v2-* rule |

Authors who follow §3–§9 do not need to think about these pitfalls again.
The lint refuses anything off-spec, and the runtime validator refuses to
register a rule the lint would have caught.

## 2. The contract at a glance

A conformant rule looks like this (full worked example in §11):

```yaml
- alert: OmniSightAlembicDrift
  expr: omnisight_readyz_migrations_pending == 1
  for: 5m
  labels:
    severity: page                  # §4 enum
    area: deployment                # §3.1
    family: "6"                     # §3.1 — circled-digit family id, ASCII alias
    defense_dimension: D1           # §3.1
    # critical_labels declaration → §5
  annotations:
    summary: "Alembic head drift detected for >5 min"
    description: |
      omnisight_readyz_migrations_pending is 1; the running image's bundled
      migration head is ahead of the database head. Backend is refusing
      readiness until the gap closes.
    runbook_url: "https://docs.sora.services/runbooks/omnisight-alembic-drift"
    remediation_hint: "Run `omnisight rescue alembic upgrade` from the deploy host."
  # framework-only fields (consumed by bridge.py, stripped before Prometheus parses):
  __bridge:
    critical_labels: [family, instance]      # §5
    cardinality_caps:                        # §7 — per-label
      instance: 10
    register_in: [stdout, email]             # §8 — overridden by severity map by default
```

The five sections that follow define each block formally.

## 3. Required labels

### 3.1 The four-label minimum

Every alert MUST carry these four labels, and ONLY these four are routed on:

| Label | Type | Allowed values | Purpose |
|---|---|---|---|
| `severity` | enum | `page`, `warn`, `info` | Routing key (§8); resolve policy (§6); paging tier |
| `area` | enum | `deployment`, `runner`, `backend`, `db`, `security`, `integration`, `ops`, `docs` | Owner / group-by partition (§9); mirrors the runner's recognised `area:*` JIRA labels |
| `family` | string | `"5"`, `"6"`, `"7"`, `"8"`, `"9"`, `"10"`, or the cross-family literal `"shared"` | Maps the alert back to its G.A-v2 defense family. ASCII digit, not the circled glyph (`⑤`); the spec uses ⑤/⑥/⑩ in prose but rules must be parser-safe. |
| `defense_dimension` | enum | `D1`, `D2`, `D3`, `D4`, `D5` | Which of the 5 defense dimensions this alert reports on. See §2 of the runtime-defense spec. |

The lint rejects:
- a rule missing any of these four;
- a rule using a value outside the enum (e.g. `severity: critical`, `area: foo`);
- a rule that adds *additional* routing-class labels (anything ending in
  `_severity`, `_tier`, `_priority`) — those are routing-key collisions and
  always belong in `severity`.

### 3.2 Non-routing labels (optional)

Beyond the four routing labels, a rule MAY add arbitrary descriptive labels
(`instance`, `image_repo`, `phase`, `ticket_id`, …). These show up in the
notification payload but DO NOT take part in routing decisions. They are
subject to the cardinality cap in §7.

### 3.3 Why not `subsystem`?

The legacy `subsystem` label (used by `orchestration_alerts.rules.yml`) is
deliberately not part of this contract. It was a pre-v2 routing key; in the
v2 model `area` does that job and `family` carries the cross-family
provenance. Grandfathered rules that already use `subsystem` are left
untouched until they are individually migrated through the lint.

## 4. Required annotations

Every alert MUST set all four:

| Annotation | Required content | Notes |
|---|---|---|
| `summary` | One sentence, ≤120 chars, present tense, no template values in the first 60 chars | Becomes the email subject and the Slack/Discord one-liner. |
| `description` | Block scalar; explains *what is true now* (not what to do — that's `remediation_hint`) | Goldilocks length: 2–6 lines. Templated values are fine. |
| `runbook_url` | Absolute URL to a `docs.sora.services/runbooks/<slug>` page | A runbook MUST exist before the rule files; the lint resolves the URL with a HEAD request in CI and fails on 404 / 5xx (`OMNISIGHT_PROMOTE_ALERT_SKIP_RUNBOOK_CHECK=1` to suppress in air-gapped runs). |
| `remediation_hint` | One-sentence operator-actionable next step | This is the "what to do" line. See §4.1. |

`remediation_hint` is mandatory even on `info` rules — a rule whose only
useful response is "nothing, this is informational" should say exactly that
(`"No action required; informational only."`). Empty `remediation_hint` is
a `D2-remediation-on-user-facing` violation (per §2 of the runtime-defense
spec).

### 4.1 `remediation_hint` shape

The hint must:
- Start with an imperative verb (`Run`, `Check`, `Page`, `Roll back`, `Inspect`).
- Reference a concrete artefact (CLI command, file path, dashboard, URL).
- Be safe to read on a phone at 03:00 — no Markdown, no nested templating.

Bad: `"investigate further"` (no concrete next step).
Bad: `"$$LABELS.instance is in trouble"` (no action).
Good: `"Run scripts/omnisight-rescue/runner_rescue.py release {{ $labels.instance }} after on-call ack."`

## 5. Dedupe key format

Alertmanager (and the v0 stdout-email adapter) groups firing alerts by a
deterministic dedupe key. Without a contract here, two unrelated rules can
collide on the *same* key and start swallowing each other's notifications.

### 5.1 The formula

```
dedupe_key = sha256_hex(
    alertname || "\x1f" ||
    family    || "\x1f" ||
    "|".join(sorted(f"{k}={v}" for k, v in critical_labels.items()))
)[:16]
```

- `alertname` is the rule's `alert:` field.
- `family` is the routing label from §3.1.
- `critical_labels` is the rule's declared subset (§5.2).
- `\x1f` (ASCII unit separator) is a delimiter that cannot appear in label
  values per Prometheus's own naming rules — collision is structurally
  impossible.
- The 16-hex-char prefix is what flows on the wire; the full sha256 is
  retained in the bridge's audit log for forensics.

The bridge generates this — rule authors do not compute it by hand. The
rule's only job is to declare *which* labels are critical (§5.2).

### 5.2 Declaring `critical_labels`

A rule must enumerate its critical labels under the framework-only
`__bridge.critical_labels` block:

```yaml
__bridge:
  critical_labels: [family, instance]
```

Semantics: two firing samples that share `(alertname, family, *critical_labels)`
deduplicate to the same notification. Samples that differ on a critical
label produce *separate* notifications.

Rule of thumb:
- Include the label the on-call needs to know which **thing** is broken
  (`instance`, `image_repo`, `runner_id`).
- Do NOT include labels that just identify the alert itself
  (`alertname`, `family`) — they are folded in by the formula already.
- Do NOT include high-cardinality descriptive labels (`exemplar_trace_id`)
  — they defeat dedupe.

The lint rejects:
- `critical_labels` empty (every rule needs at least one);
- `critical_labels` referencing a label not present in `labels:`;
- `critical_labels` containing `alertname` or `family` (duplicated by formula).

## 6. Resolved-notification policy

Resolutions (the firing-stops moment) are emitted by Prometheus
unconditionally. The bridge decides whether to *deliver* them, per severity:

| `severity` | Resolved notification | Rationale |
|---|---|---|
| `page` | Delivered, downgraded to `info` priority, with `[RESOLVED]` prefix on the subject | The on-call who got paged needs the all-clear. |
| `warn` | Silently dropped at the bridge | Warns are dashboarded; a noisy resolve would double the pager-fatigue cost without operational value. |
| `info` | Silently dropped at the bridge | Info is intentionally low-signal; resolve has zero new information. |

This policy is implemented in `backend/alerting/bridge.py` (v2-AlertBridge-1bc),
not per rule. A rule author cannot override it. AM-bridge migration must
preserve this behaviour — the migration test (`v2-AlertBridge-AMMigrationTest`)
asserts the resolve-handling delta is zero across the swap.

## 7. Cardinality cap

Each label declared on a rule (routing or descriptive) must produce **≤10
distinct values** in steady state. The cap is per label, per rule, evaluated
over a rolling 24-hour window.

### 7.1 Why ≤10

A page-class alert with 100 distinct `instance` values is not 100 actionable
notifications — it's 100 ways for the same underlying incident to overwhelm
the on-call queue. AM stops grouping cleanly past about a dozen members per
group, and the stdout adapter has no grouping at all. 10 is a safe ceiling
for both regimes and is the same number the spec §3 lays out.

### 7.2 Declaration

```yaml
__bridge:
  cardinality_caps:
    instance: 10
    image_repo: 5
```

Caps may be **lower** than 10 (tighter than the framework default) but never
higher. The lint rejects any cap >10.

### 7.3 Runtime enforcement

`backend/alerting/bridge.py` keeps a rolling 24h cardinality counter per
`(rule, label)`. If a label crosses its cap, the bridge:
1. Drops further samples for that rule until the window rolls.
2. Emits a meta-alert `OmniSightAlertCardinalityCap` (severity=`warn`,
   area=`ops`, family=`shared`, defense_dimension=`D1`) pointing at the
   offending rule.
3. Logs the offender to `audit_log` for post-mortem.

The meta-alert is itself a contract-conformant rule, so it cannot bypass
its own cap.

### 7.4 Estimating cardinality at filing time

The lint runs a heuristic: if a `critical_labels` entry is `instance` /
`pod` / `host` and the cap is left at 10, it's accepted (Prometheus typical
fleet size). If a label is named `trace_id` / `request_id` / `user_id` /
`ticket_id`, the lint hard-rejects on naming-class alone — those are
high-cardinality by construction.

## 8. Receiver routing (severity → channel)

The rule does NOT declare its delivery channel. Routing is a framework
config concern, kept in `backend/alerting/bridge.py`:

```python
SEVERITY_CHANNEL_MAP = {
    "page": ["email", "stdout"],   # v0; future: ["pagerduty", "email"]
    "warn": ["email", "stdout"],
    "info": ["stdout"],
}
```

The migration to AM is a one-key swap in this map. No rule edit is required.

### 8.1 Per-rule overrides

A rule MAY declare `__bridge.register_in: [<channel>...]` to *restrict* its
delivery to a subset of the severity-map default (never to add channels
beyond it). Typical use is silencing a warn during a known-noisy window
without dropping it from logs:

```yaml
__bridge:
  register_in: [stdout]   # warn rule, but skip email this sprint
```

The lint rejects any `register_in` entry not in the severity map for the
rule's severity.

## 9. Grouping

Every v2-* rule uses the same Alertmanager grouping parameters. These are
**fixed in the framework**, not declared per rule. Quoted here so authors
know what to expect when reading paginated notifications:

| Parameter | Value | Why |
|---|---|---|
| `group_by` | `[severity, area]` | One group per (severity, area); collapses related incidents in the same domain without merging unrelated ones. |
| `group_wait` | `30s` | Wait 30s for siblings before sending the first notification of a new group. |
| `group_interval` | `5m` | Subsequent notifications for the same group at most every 5 min. |
| `repeat_interval` | `4h` | Re-page an unacknowledged firing alert every 4h. |

These values match what 31.G AM bootstrap (`v2-AlertBridge-AMMigrationTest`)
will assert at swap-time. They are not negotiable per rule.

## 10. Channel adapter interface

`backend/alerting/bridge.py` defines a thin adapter contract (v2-AlertBridge-1bc
implements; this section is the spec the implementation will conform to):

```python
class ChannelAdapter(Protocol):
    name: str                                   # e.g. "stdout", "email", "alertmanager_webhook"
    def deliver(self, envelope: AlertEnvelope) -> None: ...
    def healthcheck(self) -> AdapterHealth: ...
```

`AlertEnvelope` is the canonical, channel-agnostic notification payload:

```python
@dataclass(frozen=True)
class AlertEnvelope:
    alertname: str
    severity: Literal["page", "warn", "info"]
    area: str
    family: str
    defense_dimension: Literal["D1", "D2", "D3", "D4", "D5"]
    labels: Mapping[str, str]                   # full label set incl. routing + descriptive
    annotations: Mapping[str, str]              # summary, description, runbook_url, remediation_hint
    dedupe_key: str                             # 16-hex prefix per §5
    fired_at: datetime
    resolved_at: datetime | None                # set on resolve
    critical_labels: tuple[str, ...]            # echoed for audit
```

v0 ships two adapters:
- `StdoutAdapter` — writes a single JSON line per envelope to stdout
  (consumed by `journalctl` and the test harness).
- `EmailAdapter` — SMTP, single-recipient, subject = `[{severity}] {summary}`.

The AM webhook adapter (`AlertmanagerAdapter`) is filed under 31.G-LibsGate
and slots into the same interface at swap time.

## 11. AM-bridge migration contract

The AM swap is the v0→v1 cutover. The migration test
(`v2-AlertBridge-AMMigrationTest`) asserts payload portability via a
canonical envelope, NOT byte equivalence (codex P1-7 amendment, v1.3 spec):

> Synthetic alert fires → stdout adapter delivers payload `P`; switch
> adapter to AM webhook → AM delivers payload `P'` where
> `canonical_envelope(P) == canonical_envelope(P')` after normalisation.

### 11.1 Canonical envelope normalisation

The migration test strips these AM-injected fields before comparing
(they exist on the AM side and have no stdout counterpart):

- `fingerprint` (AM internal dedupe; we have our own per §5)
- `startsAt` / `endsAt` (AM timestamps; we keep `fired_at` / `resolved_at`)
- `generatorURL` (AM-side URL into the originating Prometheus)
- `receiver` (AM route-side label injected by routing config)

The migration test preserves and asserts equality on:

- `alertname`
- `severity`, `area`, `family`, `defense_dimension`
- `summary`, `description`, `runbook_url`, `remediation_hint`
- the rule's declared `critical_labels` set
- the `dedupe_key` per §5

### 11.2 What this means for rule authors

A rule author writes the rule once. The framework, not the rule, decides
how the envelope reaches the receiver. The migration test guarantees that
guarantee. A rule that hand-rolls a `fingerprint` annotation, or that
hard-codes a channel in its labels, breaks the contract and is rejected by
the lint.

## 12. Consumer rules

Three rules cite this contract at filing time (per Exercised AC of OP-1102):

| Rule | Spec ticket | Severity | Area | Family | Defense dim |
|---|---|---|---|---|---|
| `OmniSightAlembicDrift` | v2-⑥-AlertRule | `page` | `deployment` | `"6"` | `D1` |
| `OmniSightStaleImage` | v2-⑤-AlertRule | `warn` | `deployment` | `"5"` | `D1` |
| `runner_claim_stale` / `runner_pickup_block_rate` / `runner_state_drift` | v2-⑩-AlertRule | `page` / `warn` / `warn` | `runner` | `"10"` | `D1` |

Future v2-* alert rules MUST also conform; the lint is the gate.

## 13. Filing-time discipline

When filing a child AlertRule ticket (`v2-⑤/⑥/⑩-AlertRule` or any future
addition):

1. Cite this document by path (`docs/sop/alert-rule-contract.md`) in the
   ticket description, alongside the runtime-defense spec.
2. Include the rule's planned `severity` / `area` / `family` /
   `defense_dimension` values in the ticket description so reviewers can
   sanity-check the labels before code lands.
3. Reference the runbook URL the rule will set in `annotations.runbook_url`
   — if the runbook is not yet authored, the AlertRule ticket must block
   on a `*-Runbook` ticket per the standard filing-time check.

The lint then verifies at code-review time that the rule shipped matches
what the ticket said it would ship.

## 14. Open questions / forward references

- **Multi-instance dedupe across replicated rules**: when the same rule is
  evaluated by N replicated Prometheus instances, dedupe still works
  because the `dedupe_key` is content-derived. AM-bridge swap inherits the
  same property.
- **Severity escalation**: there is no in-rule `warn-for-30m-then-page`
  primitive. If you need escalation, file two rules (a warn and a page)
  with different `for:` thresholds — the bridge does not synthesise.
- **PagerDuty / Slack channels**: deferred until AM swap. The current
  severity map will gain entries; no rule changes will be required.

---

**Change log**

- 2026-05-14: Initial version (OP-1102, v2-AlertBridge-1a). Establishes
  §3 four-label minimum, §4 four-annotation minimum, §5 dedupe-key formula,
  §6 resolve policy, §7 cardinality cap, §8 routing-in-config, §9 fixed
  grouping, §10 adapter interface, §11 canonical-envelope migration
  contract. Cited by v2-⑤-AlertRule / v2-⑥-AlertRule / v2-⑩-AlertRule.
