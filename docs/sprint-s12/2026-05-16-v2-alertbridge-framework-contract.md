# v2-AlertBridge-Framework — contract spec

**Ticket**: OP-1144 (`v2-AlertBridge-1a`)
**Authored**: 2026-05-16
**Status**: contract spec — runtime impl is `v2-AlertBridge-1bc` (downstream)
**Scope**: docs only; no runtime, db, devops, embedded, frontend, security,
tests, or tooling side effects. Boundary-block exemption justified in the
ticket description: spec ticket touches `docs/sprint-s12/` only and adds one
cross-reference line to the existing sprint-A spec.
**Authority**: Sprint S12.G v1.4 spec §"Shared infrastructure (precedes
⑤ and ⑥)" — see
[`sprint-s12g-A-v2-runtime-defense-contract-spec.md`](./sprint-s12g-A-v2-runtime-defense-contract-spec.md)
§3 *Shared infrastructure*.
**Locked operator decision**: Q6 — file AlertRule tickets BEFORE 31.G
Alertmanager (AM) integration ships; design the contract so the future AM
bridge connection is a zero-surprise config swap (no rule edits, no rule
ticket re-files).
**Consumer-facing rendering**: `docs/sop/alert-rule-contract.md` (the
shorter rule-author-facing summary).
**This document is the source of truth** for the AlertRule contract. The
SOP doc tracks this doc; if the two disagree, this doc wins and the SOP doc
is updated.

---

## §0. Why this contract exists

OmniSight is filing AlertRule tickets (`v2-⑤-AlertRule`,
`v2-⑥-AlertRule`, `v2-⑩-AlertRule`) before the Alertmanager (AM)
integration in 31.G ships. That order is deliberate (Q6 operator decision):
the rules ship now against a stdout-plus-email channel adapter, and the
future swap to AM is a one-line config change with **no rule-file edits and
no rule-ticket re-files**.

That promise only holds if every rule speaks the same dialect. Seven
well-known AM-integration pitfalls have bitten Prometheus shops before us;
the contract below pre-bakes a mitigation for each so the swap is a
non-event.

| # | Pitfall | Mitigation §                                                |
|---|---|---|
| P1 | Label / annotation drift between rule and AM             | §1 — required-label schema, §2 — required annotations  |
| P2 | Severity-vocabulary mismatch (`critical` vs `page` vs `P1`) | §3 — fixed enum `{page, warn, info}`                |
| P3 | Dedupe key collisions across rules                       | §4 — formula + critical-labels declaration             |
| P4 | Resolved-notification surprise (paging on resolve)       | §5 — severity-keyed resolve policy                     |
| P5 | Time-series cardinality explosion                        | §6 — ≤10 distinct values per label, hard-fail lint     |
| P6 | Receiver routing inconsistency (rule-side vs route-side) | §7 — routing lives in framework config, never in rule  |
| P7 | `group_by` / `group_wait` fragmentation                  | §8 — fixed grouping params for every v2-* rule         |

Authors who follow §1–§8 do not need to think about these pitfalls again.
The lint (`v2-AlertBridge-2bc`) refuses anything off-spec, and the runtime
validator (`v2-AlertBridge-1bc`) refuses to register a rule the lint would
have caught.

The contract is then completed by §9 (channel adapter ABI) and §10 (the
AM-bridge migration contract) — those two together are what makes the swap
itself a non-event.

---

## §1. Required label schema (P1, P6)

Every AlertRule MUST carry these four labels, and ONLY these four are
routed on:

| Label              | Type   | Allowed values                                                                                                          | Purpose                                                                                                  |
|---|---|---|---|
| `severity`         | enum   | `page`, `warn`, `info`                                                                                                  | Routing key (§7); resolve policy (§5); paging tier; P1/P2/P3 mapping (§3.1).                             |
| `area`             | enum   | `deployment`, `runner`, `backend`, `db`, `security`, `integration`, `ops`, `docs`                                       | Owner / group-by partition (§8); mirrors the runner's recognised `area:*` JIRA labels (SOP §2).          |
| `family`           | string | `"5"`, `"6"`, `"7"`, `"8"`, `"9"`, `"10"`, or the cross-family literal `"shared"`                                       | Maps the alert back to its G.A-v2 defense family. ASCII digit, not the circled glyph (`⑤`).             |
| `defense_dimension`| enum   | `D1`, `D2`, `D3`, `D4`, `D5`                                                                                            | Which of the 5 defense dimensions the alert reports on (see §2 of the runtime-defense spec).             |

The lint rejects:

- a rule missing any of these four;
- a rule using a value outside the enum (e.g. `severity: critical`,
  `area: foo`);
- a rule that adds *additional* routing-class labels (anything ending in
  `_severity`, `_tier`, `_priority`) — those are routing-key collisions and
  always belong in `severity`.

### §1.1 ASCII discipline on `family`

The sprint spec uses the circled-digit glyphs (⑤, ⑥, ⑩) in prose because
they are the canonical family identifiers. The rule files use ASCII digits
(`"5"`, `"6"`, `"10"`) because Prometheus's rule-file parser and AM's
label-matchers are non-UTF-aware in several production deployments. The
contract pins the ASCII form so the wire format never carries multi-byte
glyphs. The promote-alert lint reads the ASCII digit and renders the glyph
back when emitting human-facing summaries.

### §1.2 Non-routing labels (optional)

Beyond the four routing labels, a rule MAY add arbitrary descriptive labels
(`instance`, `image_repo`, `phase`, `ticket_id`, …). These show up in the
notification payload but DO NOT take part in routing decisions. They are
subject to the cardinality cap in §6.

### §1.3 Why not `subsystem`?

The legacy `subsystem` label (used by
`deploy/prometheus/orchestration_alerts.rules.yml` from O9 / #272) is
deliberately not part of this contract. It was a pre-v2 routing key; in
the v2 model `area` does that job and `family` carries the cross-family
provenance. Grandfathered rules that already use `subsystem` are left
untouched until they are individually migrated through the lint.

---

## §2. Required annotations (P1)

Every AlertRule MUST set all four:

| Annotation         | Required content                                                                                                                 | Notes                                                                                                                  |
|---|---|---|
| `summary`          | One sentence, ≤120 chars, present tense, no template values in the first 60 chars.                                              | Becomes the email subject and the Slack/Discord one-liner.                                                             |
| `description`      | Block scalar; explains *what is true now* — not what to do (that's `remediation_hint`).                                          | Goldilocks length: 2–6 lines. Templated values are fine.                                                               |
| `runbook_url`      | Absolute URL to a `docs.sora.services/runbooks/<slug>` page.                                                                     | A runbook MUST exist before the rule files; the lint resolves the URL with a HEAD request in CI and fails on 404/5xx. |
| `remediation_hint` | One-sentence operator-actionable next step.                                                                                      | Mandatory even on `info` rules — see §2.1.                                                                             |

`remediation_hint` is mandatory even on `info` rules. A rule whose only
useful response is "nothing, this is informational" must say exactly that
(`"No action required; informational only."`). Empty `remediation_hint` is
a `D2-remediation-on-user-facing` violation per §2 of the runtime-defense
spec.

### §2.1 `remediation_hint` shape

The hint MUST:

- Start with an imperative verb (`Run`, `Check`, `Page`, `Roll back`,
  `Inspect`).
- Reference a concrete artefact (CLI command, file path, dashboard, URL).
- Be safe to read on a phone at 03:00 — no Markdown, no nested templating.

Examples:

- ❌ Bad: `"investigate further"` (no concrete next step).
- ❌ Bad: `"$$LABELS.instance is in trouble"` (no action).
- ✅ Good: `"Run scripts/omnisight-rescue/runner_rescue.py release {{ $labels.instance }} after on-call ack."`

### §2.2 `runbook_url` discipline

The lint resolves `runbook_url` with a HEAD request in CI and refuses any
rule whose runbook returns 404 or 5xx. The escape hatch
`OMNISIGHT_PROMOTE_ALERT_SKIP_RUNBOOK_CHECK=1` exists for air-gapped
runs; CI never sets it.

A rule may not be filed without an existing runbook. If the runbook is not
yet authored, the rule's ticket must block on a `*-Runbook` ticket per
the standard filing-time check.

---

## §3. Severity enum (P2)

`severity` is constrained to exactly three values:

| Value  | Paging tier | AM downstream priority | Resolve policy (§5) | Typical use                                                              |
|---|---|---|---|---|
| `page` | P1          | P1                     | Delivered (downgraded) | Service-down, paging-class incident; on-call must respond.            |
| `warn` | P2          | P2                     | Silently dropped       | Dashboarded; on-call sees in business hours.                          |
| `info` | P3          | P3                     | Silently dropped       | Audit-trail entry; no-op for human responder.                         |

### §3.1 P1/P2/P3 downstream mapping

The PagerDuty / Alertmanager priority tier the bridge will assign at swap
time is fixed in the framework config (`SEVERITY_PRIORITY_MAP`):

```python
SEVERITY_PRIORITY_MAP = {
    "page": "P1",
    "warn": "P2",
    "info": "P3",
}
```

The rule does NOT carry a priority label. The mapping lives in the
framework so that the AM-swap is a config edit, not a rule edit.

### §3.2 Why this enum, why not `critical`?

`critical` is the historic Prometheus default. We reject it for two
reasons:

1. The word *critical* is overloaded: it conflates paging urgency with
   business impact. `page` says exactly what it means: this triggers a
   pager.
2. The downstream P1/P2/P3 vocabulary is what the on-call rotation tool
   speaks; mapping straight from `severity` keeps the wire format
   minimal.

---

## §4. Dedupe-key formula (P3)

Alertmanager (and the v0 stdout-email adapter) groups firing alerts by a
deterministic dedupe key. Without a contract here, two unrelated rules can
collide on the *same* key and start swallowing each other's notifications.

### §4.1 Formula

```text
dedupe_key = sha256_hex(
    alertname || "\x1f" ||
    family    || "\x1f" ||
    "|".join(sorted(f"{k}={v}" for k, v in critical_labels.items()))
)[:16]
```

- `alertname` is the rule's `alert:` field.
- `family` is the routing label from §1.
- `critical_labels` is the rule's declared subset (§4.2).
- `\x1f` (ASCII unit separator) is a delimiter that cannot appear in
  label values per Prometheus's own naming rules — collision is
  structurally impossible.
- The 16-hex-char prefix is what flows on the wire; the full sha256 is
  retained in the bridge's audit log for forensics.

The bridge generates this — rule authors do not compute it by hand. The
rule's only job is to declare *which* labels are critical (§4.2).

### §4.2 Declaring `critical_labels`

A rule MUST enumerate its critical labels under the framework-only
`__bridge.critical_labels` block (the bridge strips `__bridge` before the
Prometheus parser sees the file):

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
- Do NOT include high-cardinality descriptive labels
  (`exemplar_trace_id`) — they defeat dedupe.

The lint rejects:

- `critical_labels` empty (every rule needs at least one);
- `critical_labels` referencing a label not present in `labels:`;
- `critical_labels` containing `alertname` or `family` (duplicated by
  formula).

### §4.3 Collision-resistance budget

The 16-hex-char prefix gives ~64 bits of entropy. Across a fleet of
~10⁴ active alerts that is ~10⁻¹² collision probability per pair — well
inside the operational tolerance. The full sha256 is logged so a
forensic check can resolve any apparent collision against the source
labels.

---

## §5. Resolved-notification policy (P4)

Resolutions (the firing-stops moment) are emitted by Prometheus
unconditionally. The bridge decides whether to *deliver* them, per
severity:

| `severity` | Resolved notification                                                                                | Rationale                                                                                          |
|---|---|---|
| `page`     | Delivered, downgraded to P3 priority, with `[RESOLVED]` prefix on the subject and `info` body shape. | The on-call who got paged needs the all-clear.                                                      |
| `warn`     | Silently dropped at the bridge.                                                                       | Warns are dashboarded; a noisy resolve would double the pager-fatigue cost without operational value.|
| `info`     | Silently dropped at the bridge.                                                                       | Info is intentionally low-signal; resolve has zero new information.                                 |

This policy is implemented in `backend/alerting/bridge.py`
(`v2-AlertBridge-1bc`), not per rule. A rule author cannot override it.
The AM-bridge migration MUST preserve this behaviour — the migration test
(`v2-AlertBridge-AMMigrationTest`) asserts the resolve-handling delta is
zero across the swap.

### §5.1 Resolved-page envelope shape

The downgraded resolved-page envelope keeps the original `alertname`,
`family`, `area`, `defense_dimension`, `runbook_url`, and the original
`critical_labels` (so the on-call can correlate against the firing
notification). The bridge mutates only:

- `severity` → `info` for downstream priority,
- `summary` → `[RESOLVED] {orig_summary}`,
- adds `resolved_at` timestamp,
- adds `resolution_duration = resolved_at - fired_at` to `annotations`.

---

## §6. Cardinality cap (P5)

Each label declared on a rule (routing or descriptive) MUST produce **≤10
distinct values** in steady state. The cap is per-label, per-rule,
evaluated over a rolling 24-hour window.

### §6.1 Why ≤10

A page-class alert with 100 distinct `instance` values is not 100
actionable notifications — it's 100 ways for the same underlying incident
to overwhelm the on-call queue. AM stops grouping cleanly past about a
dozen members per group, and the stdout adapter has no grouping at all.
10 is a safe ceiling for both regimes; the same number is fixed in the
runtime-defense spec §3.

### §6.2 Declaration

```yaml
__bridge:
  cardinality_caps:
    instance: 10
    image_repo: 5
```

Caps MAY be **lower** than 10 (tighter than the framework default), but
never higher. The lint rejects any cap >10.

### §6.3 Runtime enforcement

`backend/alerting/bridge.py` (`v2-AlertBridge-1bc`) keeps a rolling 24h
cardinality counter per `(rule, label)`. If a label crosses its cap, the
bridge:

1. Drops further samples for that rule until the window rolls.
2. Emits a meta-alert `OmniSightAlertCardinalityCap` (severity=`warn`,
   area=`ops`, family=`shared`, defense_dimension=`D1`) pointing at the
   offending rule.
3. Logs the offender to `audit_log` for post-mortem.

The meta-alert is itself a contract-conformant rule, so it cannot bypass
its own cap.

### §6.4 Filing-time heuristic

The lint runs a heuristic estimator:

- If a `critical_labels` entry is `instance` / `pod` / `host` and the cap
  is left at 10, it's accepted (typical Prometheus fleet size).
- If a label is named `trace_id` / `request_id` / `user_id` / `ticket_id`,
  the lint hard-rejects on naming-class alone — those are
  high-cardinality by construction and have no place on a routed alert.

### §6.5 Validator failure mode

When the runtime validator hard-fails a registered rule on cardinality, it
hard-fails container start — not just the rule registration. Rationale:
an alert rule that cannot enforce its own cap is a silent failure mode
(alerts continue to fire but dedupe collapses). A loud failure at start
is preferable to silent degradation in production.

---

## §7. Receiver routing (P6)

The rule does NOT declare its delivery channel. Routing is a framework
config concern, kept in `backend/alerting/bridge.py`:

```python
SEVERITY_CHANNEL_MAP = {
    "page": ["email", "stdout"],   # v0; future: ["pagerduty", "email"]
    "warn": ["email", "stdout"],
    "info": ["stdout"],
}
```

The migration to AM is a one-key swap in this map. No rule edit is
required. No re-file of an AlertRule ticket is required.

### §7.1 Per-rule overrides

A rule MAY declare `__bridge.register_in: [<channel>...]` to *restrict*
its delivery to a subset of the severity-map default (never to add
channels beyond it). Typical use is silencing a warn during a known-noisy
window without dropping it from logs:

```yaml
__bridge:
  register_in: [stdout]   # warn rule, but skip email this sprint
```

The lint rejects any `register_in` entry not in the severity map for the
rule's severity.

### §7.2 Channel-config separation rationale

Keeping routing in framework config — never in the rule — is what makes
§10 (AM migration) a config swap and not a per-rule edit. A rule that
hand-codes its receiver locks itself into a single channel; the lint
treats such a rule as malformed and rejects it.

---

## §8. Grouping config (P7)

Every v2-* rule uses the same Alertmanager grouping parameters. These are
**fixed in the framework**, not declared per rule. Quoted here so authors
know what to expect when reading paginated notifications:

| Parameter         | Value              | Why                                                                                                |
|---|---|---|
| `group_by`        | `[severity, area]` | One group per `(severity, area)`; collapses related incidents in the same domain without merging unrelated ones. |
| `group_wait`      | `30s`              | Wait 30s for siblings before sending the first notification of a new group.                        |
| `group_interval`  | `5m`               | Subsequent notifications for the same group at most every 5 min.                                   |
| `repeat_interval` | `4h`               | Re-page an unacknowledged firing alert every 4h.                                                   |

These values match what 31.G AM bootstrap (`v2-AlertBridge-AMMigrationTest`)
will assert at swap-time. They are not negotiable per rule.

### §8.1 Why `group_by: [severity, area]` and not `[alertname]`

`group_by: [alertname]` is the AM default. We reject it because:

- An incident often fires several rules at once (e.g. a deployment
  rollback fires `OmniSightAlembicDrift`, `OmniSightStaleImage`, and
  `runner_state_drift` simultaneously). `[severity, area]` collapses
  these into one on-call interrupt.
- Different rules with the same `alertname` across replicated Prometheus
  instances (`OmniSightAlembicDrift` from prom-A and prom-B) would
  otherwise generate two distinct groups. Our grouping is content-based
  so a single incident produces a single notification regardless of how
  many Prometheus instances saw it.

---

## §9. Channel adapter ABI

The framework defines a thin adapter contract. `v2-AlertBridge-1bc`
implements this protocol; this section is the spec the implementation
will conform to.

### §9.1 Protocol definition

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Protocol, runtime_checkable


SeverityLiteral = Literal["page", "warn", "info"]
DefenseDimensionLiteral = Literal["D1", "D2", "D3", "D4", "D5"]


@dataclass(frozen=True)
class AlertEnvelope:
    """Canonical, channel-agnostic notification payload.

    This is the wire format between the bridge and any ChannelAdapter.
    AM-bridge migration MUST preserve every field in this dataclass —
    the canonical envelope is the portability guarantee (§10).
    """

    alertname: str
    severity: SeverityLiteral
    area: str
    family: str
    defense_dimension: DefenseDimensionLiteral

    labels: Mapping[str, str]        # full label set incl. routing + descriptive
    annotations: Mapping[str, str]   # summary, description, runbook_url, remediation_hint

    dedupe_key: str                  # 16-hex prefix per §4
    fired_at: datetime
    resolved_at: datetime | None     # set on resolve; None while firing

    critical_labels: tuple[str, ...] # echoed for audit


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a single ChannelAdapter.deliver call.

    `ok=True` means the channel accepted the envelope. It does NOT mean
    the human read the page — only that the upstream transport returned
    success.

    `retryable` distinguishes 5xx-class failures (the bridge will retry
    with exponential backoff) from 4xx-class failures (the bridge logs
    and gives up — a 4xx means the envelope itself is malformed, and
    retrying it will not help).
    """

    ok: bool
    channel: str               # echo of ChannelAdapter.name
    delivered_at: datetime
    retryable: bool = False    # only meaningful when ok=False
    error: str | None = None   # human-readable; never carries secrets


@dataclass(frozen=True)
class AdapterHealth:
    """Returned by ChannelAdapter.healthcheck.

    The bridge consults this on container start and refuses to come up if
    any registered adapter reports unhealthy. The check itself MUST NOT
    deliver test traffic to the real channel — see §9.4.
    """

    ok: bool
    detail: str                # human-readable status, e.g. "smtp reachable"


@runtime_checkable
class ChannelAdapter(Protocol):
    """The single integration point between the bridge and a transport.

    Every adapter (stdout, email, alertmanager_webhook, pagerduty, slack)
    is a class that implements this protocol. The bridge holds a list of
    instances and routes per §7.
    """

    name: str                  # e.g. "stdout", "email", "alertmanager_webhook"

    def deliver(self, envelope: AlertEnvelope) -> DeliveryResult: ...

    def healthcheck(self) -> AdapterHealth: ...
```

### §9.2 v0 adapters (`v2-AlertBridge-1bc` scope)

- `StdoutAdapter` — writes a single JSON line per envelope to stdout
  (consumed by `journalctl` and the test harness). Never fails;
  `DeliveryResult.ok=True` always.
- `EmailAdapter` — SMTP, single-recipient, subject =
  `[{severity}] {summary}`. 5xx → `retryable=True`; 4xx →
  `retryable=False`.

### §9.3 Future adapter (`AlertmanagerAdapter`, 31.G-LibsGate)

Slots into the same Protocol at swap-time. Implementation details are
deferred to 31.G; this contract only constrains the ABI surface so the
swap is a config edit on `SEVERITY_CHANNEL_MAP` (§7) plus an import of
the new class.

### §9.4 `healthcheck` must not deliver test traffic

A common adapter-design mistake is to "verify" the channel by sending a
synthetic alert during startup. We forbid that:

- It violates the "no side effects on container start" property the
  rest of the runner contract relies on.
- It creates audit-log noise that can't be distinguished from real
  alerts during a forensic investigation.

`healthcheck` MUST limit itself to non-delivery probes (SMTP `NOOP`,
HTTP `HEAD` against the AM `/-/ready` endpoint, etc.).

### §9.5 Adapter registration

Adapters register with the bridge via a `register_channel_adapter`
factory at container start. The bridge holds a dict
`{adapter.name: adapter}` and routes envelopes to the named adapters in
`SEVERITY_CHANNEL_MAP[envelope.severity]`. If a name in the map has no
registered adapter, the bridge hard-fails container start (loud failure
mode per §6.5).

---

## §10. AM-bridge migration contract

The AM swap is the v0→v1 cutover. The migration test
(`v2-AlertBridge-AMMigrationTest`) asserts payload portability via a
**canonical envelope**, NOT byte equivalence (codex P1-7 amendment, v1.3
spec):

> Synthetic alert fires → stdout adapter delivers payload `P`; switch
> adapter to AM webhook → AM delivers payload `P'` where
> `canonical_envelope(P) == canonical_envelope(P')` after normalisation.

### §10.1 Why not byte-equivalence

Byte-equivalence was the v1.2 invariant; codex P1-7 review caught that it
cannot hold: AM injects several fields (`fingerprint`, `startsAt`,
`endsAt`, `generatorURL`, the AM-route-side `receiver` label) that the
stdout adapter has no counterpart for. A byte-equivalence assertion would
fail on cosmetic, AM-internal data.

The right invariant is *semantic portability*: the rule author's payload
(what they declared in `labels` and `annotations`) must round-trip
identically. AM's own bookkeeping fields are stripped before comparison.

### §10.2 Canonical envelope normalisation rules

The migration test strips these AM-injected fields before comparing
(they exist on the AM side and have no stdout counterpart):

- `fingerprint` (AM internal dedupe; we have our own per §4)
- `startsAt` (AM timestamp; we keep `fired_at`)
- `endsAt` (AM timestamp; we keep `resolved_at`)
- `generatorURL` (AM-side URL into the originating Prometheus)
- AM-injected receiver-route labels (anything matching `^_amtool_` or
  the configured `receiver` label injected by routing config)

The migration test preserves and asserts equality on:

- `alertname`
- `severity`, `area`, `family`, `defense_dimension`
- `summary`, `description`, `runbook_url`, `remediation_hint`
- the rule's declared `critical_labels` set (echoed in
  `AlertEnvelope.critical_labels`)
- the `dedupe_key` per §4 (this MUST match on both sides because the
  formula is content-derived; if it doesn't, the contract is broken)

In pseudocode:

```python
def canonical_envelope(raw: Mapping) -> Mapping:
    stripped_keys = {
        "fingerprint", "startsAt", "endsAt", "generatorURL", "receiver",
    }
    out = {k: v for k, v in raw.items() if k not in stripped_keys}
    # also strip AM-injected route-side label names
    out["labels"] = {
        k: v for k, v in raw.get("labels", {}).items()
        if not k.startswith("_amtool_")
    }
    return out

assert canonical_envelope(stdout_payload) == canonical_envelope(am_payload)
```

### §10.3 Migration is a config change

Operator-facing: the AM-bridge migration is a single-line config change:

```diff
- channel_adapter: stdout_email
+ channel_adapter: alertmanager_webhook
```

Combined with re-pointing `SEVERITY_CHANNEL_MAP` (§7) to the new
adapter's name. NO rule files are touched. NO AlertRule tickets are
re-filed. The migration test runs in CI against both adapters and asserts
the canonical-envelope invariant.

### §10.4 What this means for rule authors

A rule author writes the rule once. The framework, not the rule, decides
how the envelope reaches the receiver. The migration test guarantees that
guarantee. A rule that hand-rolls a `fingerprint` annotation, or that
hard-codes a channel in its labels, breaks the contract and is rejected
by the lint.

### §10.5 Rollback contract

The AM-bridge migration must be reversible by the same one-line config
flip. If a post-migration regression appears, the operator flips
`channel_adapter` back to `stdout_email` and the system returns to v0
behaviour with no rule edits. The migration test exercises both
directions (`stdout → AM` and `AM → stdout`) and asserts the canonical
envelope on each.

---

## §11. Worked example — `OmniSightAlembicDrift`

```yaml
- alert: OmniSightAlembicDrift
  expr: omnisight_readyz_migrations_pending == 1
  for: 5m
  labels:
    severity: page                  # §3 enum
    area: deployment                # §1 enum
    family: "6"                     # §1 — circled-digit family id, ASCII alias
    defense_dimension: D1           # §1 enum
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
    critical_labels: [family, instance]     # §4
    cardinality_caps:                       # §6 — per-label
      instance: 10
    # register_in omitted — let severity-map default apply (§7)
```

The lint checks (in order):

1. All four required labels present and within enum (§1).
2. All four required annotations present (§2).
3. `severity` ∈ `{page, warn, info}` (§3).
4. `__bridge.critical_labels` non-empty, doesn't contain `alertname` /
   `family`, every referenced label exists in `labels:` (§4).
5. `__bridge.cardinality_caps` entries all ≤10 (§6).
6. `runbook_url` resolves to 2xx via HEAD (§2).
7. No locally-declared receiver channel (would conflict with §7).

A conformant rule then needs zero further plumbing to ship.

---

## §12. Downstream impact — `v2-AlertBridge-1bc` AC checklist

The downstream impl ticket (`v2-AlertBridge-1bc`) should be able to
reference this doc's section anchors when describing its AC checklist.
Expected mapping:

| `v2-AlertBridge-1bc` AC                                                               | This doc §                                              |
|---|---|
| Rule validator: required labels                                                       | §1, §1.3                                                |
| Rule validator: required annotations                                                  | §2, §2.1, §2.2                                          |
| Rule validator: severity enum                                                         | §3                                                      |
| Rule validator: critical-labels declaration                                           | §4.2                                                    |
| Rule validator: cardinality-cap declaration ≤10                                       | §6.2                                                    |
| Runtime: `dedupe_key` formula                                                         | §4.1                                                    |
| Runtime: resolved-notification severity-keyed policy                                  | §5, §5.1                                                |
| Runtime: cardinality counter + meta-alert + container hard-fail                       | §6.3, §6.5                                              |
| Runtime: `SEVERITY_CHANNEL_MAP` config, `register_in` override                        | §7, §7.1                                                |
| Runtime: grouping params surfaced on envelope                                         | §8                                                      |
| ABI: `ChannelAdapter` protocol + `AlertEnvelope` + `DeliveryResult` + `AdapterHealth` | §9.1                                                    |
| ABI: v0 adapter implementations (stdout, email)                                       | §9.2                                                    |
| ABI: `healthcheck` no-side-effect rule                                                | §9.4                                                    |
| ABI: registration + hard-fail on missing adapter name                                 | §9.5                                                    |

A `v2-AlertBridge-1bc` AC checklist item that has no §-anchor in this doc
indicates either an undocumented requirement (this doc is incomplete) or
out-of-scope creep on the impl ticket. In either case, raise the question
on this ticket (OP-1144) before implementing.

---

## §13. Downstream impact — `v2-AlertBridge-2bc` (rule-promotion lint)

`v2-AlertBridge-2bc` will implement `scripts/promote-alert-rule.py`.
The lint reads each rule YAML and applies the checks enumerated in §11,
plus:

- HEAD-resolves `runbook_url`.
- Renders `family: "5"` back to `⑤` for the lint summary output (cosmetic
  only; the YAML stays ASCII per §1.1).
- Rejects any rule that uses `subsystem` as a routing label (legacy O9
  pattern; see §1.3).

The lint is what gates `deploy/prometheus/rules/` additions in CI.

---

## §14. Open questions / forward references

- **Multi-instance dedupe across replicated rules**: when the same rule is
  evaluated by N replicated Prometheus instances, dedupe still works
  because the `dedupe_key` is content-derived. AM-bridge swap inherits
  the same property.
- **Severity escalation**: there is no in-rule
  `warn-for-30m-then-page` primitive. If you need escalation, file two
  rules (a warn and a page) with different `for:` thresholds — the
  bridge does not synthesise.
- **PagerDuty / Slack channels**: deferred until AM swap. The current
  severity map will gain entries; no rule changes will be required.
- **Per-rule `repeat_interval` overrides**: explicitly disallowed by §8.
  If a rule genuinely needs a different cadence, file a contract
  amendment ticket; do not work around the map locally.
- **L3 page channel**: when an L3-authorised channel exists (operator
  hardware key + on-call escalation tree), it will be added as a new
  severity value (`page_l3`). Until then, `page` is the highest tier
  this contract recognises.

---

## §15. Filing-time discipline (for downstream AlertRule tickets)

When filing a child AlertRule ticket (`v2-⑤/⑥/⑩-AlertRule` or any future
addition):

1. Cite this document by path
   (`docs/sprint-s12/2026-05-16-v2-alertbridge-framework-contract.md`) in
   the ticket description, alongside the runtime-defense spec.
2. Include the rule's planned `severity` / `area` / `family` /
   `defense_dimension` values in the ticket description so reviewers can
   sanity-check the labels before code lands.
3. Reference the runbook URL the rule will set in
   `annotations.runbook_url` — if the runbook is not yet authored, the
   AlertRule ticket must block on a `*-Runbook` ticket per the standard
   filing-time check.
4. Declare expected `critical_labels` and `cardinality_caps` in the
   ticket description; reviewers check these against the cardinality
   heuristic (§6.4) before approving the file.

The lint then verifies at code-review time that the rule shipped matches
what the ticket said it would ship.

---

## §16. Authority and source-of-truth

- This document is the source of truth for the v2-AlertBridge contract.
- `docs/sop/alert-rule-contract.md` is a consumer-facing rendering and is
  derived from this doc. If the two disagree, this doc wins and the SOP
  doc is corrected.
- The Sprint S12.G v1.4 spec
  (`docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md`
  §"Shared infrastructure") cites this doc as the contract spec for the
  `v2-AlertBridge-Framework` row; the sprint spec carries the policy
  rationale (Q6 operator decision), and this doc carries the contract
  surface.

---

## §17. Change log

- 2026-05-16: Initial version (OP-1144, `v2-AlertBridge-1a`). Establishes
  §1 four-label minimum, §2 four-annotation minimum, §3 severity enum,
  §4 dedupe-key formula, §5 resolve policy, §6 cardinality cap, §7
  routing-in-config, §8 fixed grouping, §9 adapter Protocol + envelope +
  delivery-result + health-check, §10 canonical-envelope migration
  contract (incl. codex P1-7 amendment). Cited by `v2-AlertBridge-1bc`,
  `v2-AlertBridge-2bc`, `v2-AlertBridge-AMMigrationTest`,
  `v2-⑤-AlertRule`, `v2-⑥-AlertRule`, `v2-⑩-AlertRule`.
