"""AM-bridge migration smoke tests.

Companion design doc:
``docs/sprint-s12/2026-05-16-v2-alertbridge-am-migration-smoke-test.md``.

Invariant under test (per design §2): for every synthetic alert and
every pair of adapters ``(stdout_email, alertmanager_webhook)``, the
canonical envelopes produced from each adapter's payload MUST be
byte-equal after AM-injected fields are stripped. The strip list
lives in design §3 and the preserve list in design §4; the constant
``_AM_INJECTED_FIELDS`` in this file is the in-code mirror of the
strip list and a change to one MUST be paired with a change to the
other.

The ``MockAlertmanagerWebhookAdapter`` here is a TEST helper, not a
production adapter. The real ``AlertmanagerWebhookAdapter`` is the
subject of a future ticket (see design §9). The mock synthesises the
fields a real AM webhook receiver would inject (``fingerprint``,
``startsAt``, ``endsAt``, ``generatorURL``, ``receiver``, ``route``,
``status``, ``version``, ``externalURL``, ``groupKey``) so the
strip-list contract can be exercised without a real AM dependency.

This ticket extends the existing 11 bridge tests in
``backend/tests/test_alerting_bridge.py`` (OP-1103) with 4 migration
smoke tests; total ``backend/tests/test_alerting_*`` collectible
count is 15.
"""
from __future__ import annotations

from datetime import UTC, datetime
import io
import json

from backend.alerting.bridge import (
    AlertBridge,
    AlertEnvelope,
    CardinalityValidator,
    DedupeKeyGen,
    StdoutEmailAdapter,
    canonical_envelope,
)


FIXED_NOW = datetime(2026, 5, 16, 9, 0, tzinfo=UTC)


# Design §3-strip-list: the union of fields AM injects that MUST be
# removed before canonical envelope comparison. Test
# ``test_strip_list_removes_am_injected_fields`` iterates this and
# asserts post-normalisation absence. A future strip-list change MUST
# update this constant AND the design doc §3 table together.
_AM_INJECTED_FIELDS = (
    "fingerprint",
    "startsAt",
    "endsAt",
    "generatorURL",
    "receiver",
    "route",
    "inhibit_rule_match",
    "silence_id",
    "groupKey",
    "status",
    "version",
    "externalURL",
    "truncatedAlerts",
)


# Design §4-preserve-list: rule-contract fields that MUST round-trip
# byte-equal across adapters. ``test_preserve_list_keeps_contract_fields``
# asserts each entry.
_PRESERVE_CONTRACT_FIELDS = (
    "alertname",
    "severity",
    "area",
    "family",
    "defense_dimension",
    "dedupe_key",
    "fired_at",
    "resolved_at",
    "critical_labels",
)
_PRESERVE_ANNOTATIONS = (
    "summary",
    "description",
    "runbook_url",
    "remediation_hint",
)


# Subset of ``_AM_INJECTED_FIELDS`` that ``canonical_envelope()``'s
# Mapping-input branch already strips in-process (see
# ``backend/alerting/bridge.py:371``). Anything in
# ``_AM_INJECTED_FIELDS`` not in this set MUST be stripped by the
# test-side helper ``_normalize_am_payload``; the union is what the
# design doc §3 calls "the operational strip".
_BRIDGE_INPROC_STRIPS = frozenset(
    {"fingerprint", "startsAt", "endsAt", "generatorURL", "receiver"}
)


def _normalize_am_payload(raw: dict[str, object]) -> dict[str, object]:
    """Apply the full design §3 strip list, then call canonical_envelope().

    The real AM webhook adapter (future ticket, design §9) is expected
    to apply this same normalisation before delivery. Here it is a
    test-side helper so the migration smoke can run against the
    mock AM without a real adapter.
    """
    pruned: dict[str, object] = {k: v for k, v in raw.items() if k not in _AM_INJECTED_FIELDS}
    if "labels" in pruned and isinstance(pruned["labels"], dict):
        pruned["labels"] = {
            k: v for k, v in pruned["labels"].items() if k not in _AM_INJECTED_FIELDS
        }
    if "annotations" in pruned and isinstance(pruned["annotations"], dict):
        pruned["annotations"] = {
            k: v for k, v in pruned["annotations"].items() if k not in _AM_INJECTED_FIELDS
        }
    return canonical_envelope(pruned)


def _annotations() -> dict[str, str]:
    return {
        "summary": "Runner claim stale",
        "description": "Runner claim has not advanced inside the expected window.",
        "runbook_url": "https://ops.example.test/runbooks/runner-claim-stale",
        "remediation_hint": "Inspect runner release-1 before requeueing the claim.",
    }


def _labels(instance: str = "runner-a", *, severity: str = "warn") -> dict[str, str]:
    return {
        "severity": severity,
        "area": "runner",
        "family": "10",
        "defense_dimension": "D1",
        "instance": instance,
    }


def _envelope(*, severity: str = "warn") -> AlertEnvelope:
    labels = _labels(severity=severity)
    critical_labels = ("instance",)
    return AlertEnvelope(
        alertname="runner_claim_stale",
        severity=severity,  # type: ignore[arg-type]
        area="runner",
        family="10",
        defense_dimension="D1",
        labels=labels,
        annotations=_annotations(),
        dedupe_key=DedupeKeyGen.generate("runner_claim_stale", "10", labels, critical_labels),
        fired_at=FIXED_NOW,
        resolved_at=None,
        critical_labels=critical_labels,
    )


class MockAlertmanagerWebhookAdapter:
    """Test-only mock of the future production AM webhook adapter.

    Synthesises the AM-injected fields documented in design §3 so the
    smoke test can exercise the strip list without a real AM container.
    The real adapter (future ticket, design §9) MUST produce a payload
    that is byte-equal to this mock's output after both go through
    ``canonical_envelope()``.
    """

    name = "mock_am_webhook"

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def deliver(self, envelope: AlertEnvelope) -> None:
        base = canonical_envelope(envelope)
        labels = dict(base["labels"])  # type: ignore[arg-type]
        # AM injects route-side labels — see design §3 table.
        labels["receiver"] = "omnisight-alerts"
        labels["route"] = "default"
        labels["fingerprint"] = "am-generated-" + envelope.dedupe_key
        payload: dict[str, object] = {
            **base,
            "labels": labels,
            # Top-level AM-injected fields.
            "fingerprint": "am-generated-" + envelope.dedupe_key,
            "startsAt": base["fired_at"],
            "endsAt": "",
            "generatorURL": "http://prom.example.test/graph?g0.expr=runner_claim_stale",
            "status": "firing" if envelope.resolved_at is None else "resolved",
            "version": "4",
            "externalURL": "http://alertmanager.example.test",
            "groupKey": "{}/{severity=\"warn\"}:{alertname=\"runner_claim_stale\"}",
        }
        self.payloads.append(payload)

    def healthcheck(self) -> None:  # pragma: no cover - parity with ChannelAdapter Protocol
        return None


def test_stdout_adapter_to_canonical_envelope_round_trips() -> None:
    """Fire through StdoutEmailAdapter; canonical envelope MUST equal the expected normalised form.

    Design §1 scenario, stdout half. Asserts that the stdout JSON line
    on the wire round-trips through ``json.loads`` and back to
    ``canonical_envelope()`` byte-equal — confirming the
    ``sort_keys=True, separators=(",", ":")`` discipline at
    ``backend/alerting/bridge.py:255``.
    """
    stream = io.StringIO()
    bridge = AlertBridge(
        StdoutEmailAdapter(stream=stream),
        validator=CardinalityValidator(clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )
    fired = bridge.fire(
        alertname="runner_claim_stale",
        severity="info",
        area="runner",
        family="10",
        defense_dimension="D1",
        labels=_labels(severity="info"),
        annotations=_annotations(),
        critical_labels=("instance",),
    )

    expected = canonical_envelope(fired)
    on_wire = stream.getvalue().rstrip("\n")
    assert json.loads(on_wire) == expected

    # Byte-level canonicalisation: the wire form MUST be the canonical
    # serialisation, not just any equivalent JSON encoding.
    assert on_wire == json.dumps(expected, sort_keys=True, separators=(",", ":"))


def test_mock_am_webhook_adapter_canonical_envelope_matches_stdout() -> None:
    """Same alert through stdout AND mock AM webhook — canonical envelopes MUST be byte-equal.

    Design §1 scenario, the core invariant. Both adapters receive the
    same ``AlertBridge.fire(**kwargs)`` and the resulting payloads,
    once normalised through ``canonical_envelope()``, must serialise
    to the same bytes.
    """
    stream = io.StringIO()
    stdout_bridge = AlertBridge(
        StdoutEmailAdapter(stream=stream),
        validator=CardinalityValidator(clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )
    am = MockAlertmanagerWebhookAdapter()
    am_bridge = AlertBridge(
        am,
        validator=CardinalityValidator(clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )

    kwargs = {
        "alertname": "runner_claim_stale",
        "severity": "info",
        "area": "runner",
        "family": "10",
        "defense_dimension": "D1",
        "labels": _labels(severity="info"),
        "annotations": _annotations(),
        "critical_labels": ("instance",),
    }
    stdout_bridge.fire(**kwargs)  # type: ignore[arg-type]
    am_bridge.fire(**kwargs)  # type: ignore[arg-type]

    stdout_env = json.loads(stream.getvalue())
    am_env = _normalize_am_payload(am.payloads[0])

    # Structural equality of the canonical dicts.
    assert stdout_env == am_env
    # Byte equality of the canonical serialisation — the contract
    # delivered to the design doc §2.2 reviewer.
    assert json.dumps(stdout_env, sort_keys=True, separators=(",", ":")) == json.dumps(
        am_env, sort_keys=True, separators=(",", ":")
    )


def test_strip_list_removes_am_injected_fields() -> None:
    """AM-style payload with the §3 strip list MUST have those fields stripped.

    Design §3-strip-list. Builds a hand-rolled AM-shaped payload
    (faithful to the AM webhook v4 schema) and asserts that every
    field in ``_AM_INJECTED_FIELDS`` is absent from the canonical
    envelope output. ``canonical_envelope()`` covers the
    label-level strip directly; the top-level fields never make it
    into the canonical schema because the function only emits the
    12 contract fields documented in ``backend/alerting/bridge.py``
    module docstring.
    """
    am = MockAlertmanagerWebhookAdapter()
    am.deliver(_envelope(severity="info"))
    raw = am.payloads[0]

    # Sanity: every strip-list field is present in the raw payload
    # somewhere (top-level or label-level). If this fails the mock
    # has drifted from the §3 table.
    raw_keys = set(raw.keys()) | set(raw["labels"])  # type: ignore[arg-type]
    for field in _AM_INJECTED_FIELDS:
        if field in ("inhibit_rule_match", "silence_id", "truncatedAlerts"):
            # These three are only emitted in specific AM conditions
            # (inhibition active, silence active, group truncated). The
            # mock does not synthesise them on a plain firing alert, so
            # the strip-list assertion below is the only check that
            # matters for them.
            continue
        assert field in raw_keys, f"mock AM payload missing strip-list field {field!r}"

    normalised = _normalize_am_payload(raw)

    # No AM-injected field may survive normalisation, at top level OR
    # inside the labels/annotations maps.
    normalised_keys = (
        set(normalised.keys())
        | set(normalised["labels"])  # type: ignore[arg-type]
        | set(normalised["annotations"])  # type: ignore[arg-type]
    )
    for field in _AM_INJECTED_FIELDS:
        assert field not in normalised_keys, (
            f"strip-list field {field!r} leaked through _normalize_am_payload() — "
            f"design §3 strip list out of sync with this file's _AM_INJECTED_FIELDS"
        )

    # Subset assertion: ``canonical_envelope()`` alone must catch the
    # in-process strip subset (``_BRIDGE_INPROC_STRIPS``) — the
    # bridge-side backstop documented at bridge.py:371. Anything in
    # that subset surviving raw → canonical_envelope() would mean
    # bridge.py drifted from this file's mirror constant.
    raw_bridge_only = canonical_envelope(raw)
    bridge_only_keys = (
        set(raw_bridge_only.keys())
        | set(raw_bridge_only["labels"])  # type: ignore[arg-type]
        | set(raw_bridge_only["annotations"])  # type: ignore[arg-type]
    )
    for field in _BRIDGE_INPROC_STRIPS:
        assert field not in bridge_only_keys, (
            f"bridge-side in-process strip drift: {field!r} survived "
            f"canonical_envelope() — bridge.py:371 must include it"
        )


def test_preserve_list_keeps_contract_fields() -> None:
    """Contract fields (§4) MUST survive normalisation byte-equal.

    Design §4-preserve-list. Asserts that every preserve-list field
    on the AlertEnvelope is byte-equal to the same field on the
    canonical envelope of the AM-shaped payload — i.e. the AM
    round-trip does not perturb the rule contract.
    """
    envelope = _envelope(severity="info")
    am = MockAlertmanagerWebhookAdapter()
    am.deliver(envelope)

    direct = canonical_envelope(envelope)
    via_am = _normalize_am_payload(am.payloads[0])

    # Top-level contract fields.
    for field in _PRESERVE_CONTRACT_FIELDS:
        assert via_am[field] == direct[field], (
            f"preserve-list field {field!r} drifted across AM round-trip: "
            f"direct={direct[field]!r} via_am={via_am[field]!r}"
        )

    # Annotation fields.
    direct_ann = direct["annotations"]
    via_am_ann = via_am["annotations"]
    for field in _PRESERVE_ANNOTATIONS:
        assert via_am_ann[field] == direct_ann[field], (  # type: ignore[index]
            f"preserve-list annotation {field!r} drifted across AM round-trip"
        )

    # The rule-declared non-routing label (``instance``) must also
    # survive — design §4.1.
    assert via_am["labels"]["instance"] == direct["labels"]["instance"] == "runner-a"  # type: ignore[index]

    # Byte equality of the full canonical serialisation: the
    # invariant the migration test exists to prove.
    assert json.dumps(direct, sort_keys=True, separators=(",", ":")) == json.dumps(
        via_am, sort_keys=True, separators=(",", ":")
    )
