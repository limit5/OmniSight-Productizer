"""OP-1097 (G.A-v1-19): Integration proof for the full v1 ticket contract.

Composes V1-1..V1-18 into one end-to-end roster test covering the four
runtime gates the sprint built (pydantic schema, JSON Schema round-trip,
plugin registry, preflight orchestrator) plus the V1-17/V1-18 roster
signature verifier. Mutation tests cover each of the seven preflight
``rule_id`` values so a regression in any rule is caught here even when
the unit tests for that rule are themselves broken. Consumer-only —
does not modify schema, plugins, preflight, or security modules.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.plugins.base import BasePhasePlugin  # noqa: E402
from governance_engine.plugins.registry import DEFAULT_REGISTRY  # noqa: E402
from governance_engine.preflight.orchestrator import run_all_preflight_checks  # noqa: E402
from governance_engine.preflight.plugin_version import (  # noqa: E402
    PluginRegistry as PreflightPluginRegistry,
)
from governance_engine.schema.v1 import TicketContractV1  # noqa: E402
from governance_engine.security.roster_verifier import verify_roster_signature  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_ROSTER_PATH = FIXTURE_DIR / "sample-roster.yaml"
SAMPLE_ROSTER_PUBKEY = FIXTURE_DIR / "test-l1-public-key.asc"
TEST_L1_FINGERPRINT = "F995558736203E70E98F29175464E20846F314B2"

V1_SCHEMA_JSON_PATH = REPO_ROOT / "governance_engine" / "schema" / "v1.schema.json"
V1_SCHEMA_JSON: dict[str, Any] = json.loads(V1_SCHEMA_JSON_PATH.read_text(encoding="utf-8"))

META_KEY = "OP-3000"
CLAIMED_CHILD_COUNT: dict[str, int] = {META_KEY: 17}


def _base_payload() -> dict[str, Any]:
    return {
        "schema_version": "v1", "class": "subscription-claude",
        "loc_delta_max": 80, "files_touched_max": 1,
        "required_paths": ["workspace/op-0000/sentinel.txt"],
        "forbidden_paths": ["workspace/op-0000/forbidden.txt"],
        "non_goals": ["DO NOT modify earlier sibling deliverables"],
        "interface_contract": "synthetic v1 roster member",
        "test_scope": "integration",
        "destructive_op_classes": [], "destructive_op_scope": "none",
        "scope_components": ["scope-default"],
        "external_side_effect": "none", "external_payload_class": "operational",
        "runtime_capability": "unit-only", "dependency_artifacts": [],
        "execution_mode": "unit-testable", "mutex_with": [],
        "evidence_class": "unit", "on_scope_creep": "file-followup",
        "scope_summary_max_chars": 200, "tag_type": "story",
        "l1_exclusive_reason": None, "l2_reason": None,
        "authority_required": "L3", "reversibility": "reversible",
        "environment_scope": "local", "external_systems": [],
        "ticket_key": "OP-0000",
        "labels": ["phase:31.A", "class:subscription-claude"],
        "blocked_by": [META_KEY], "cross_phase_blockers": [],
        "phase_plugin_version": "v1",
    }


def _ticket(ticket_key: str, **overrides: Any) -> dict[str, Any]:
    payload = _base_payload()
    payload["ticket_key"] = ticket_key
    payload["required_paths"] = [f"workspace/{ticket_key.lower()}/sentinel.txt"]
    payload["forbidden_paths"] = [f"workspace/{ticket_key.lower()}/forbidden.txt"]
    payload.update(overrides)
    return payload


_PRODUCES_APT = [{"blocker_phase": "31.A", "blocker_artifact": "apt-bootstrap",
                  "reason": "shell baseline must exist before downstream tickets"}]
_PRODUCES_KERNEL = [{"blocker_phase": "G.A-v1", "blocker_artifact": "schema-kernel",
                     "reason": "v1 schema kernel must land before structural proofs"}]
_PRODUCES_V0 = [{"blocker_phase": "G.A-v0", "blocker_artifact": "v0-kernel",
                 "reason": "v0 baseline must be in place before v1 rollout"}]


def _synthetic_payloads() -> list[dict[str, Any]]:
    """18 tickets that together exercise every v1 field at least once."""
    tickets: list[dict[str, Any]] = [
        # T01 — META anchor: tag_type=meta, op-rehearsal, blocked_by=[].
        _ticket(META_KEY, tag_type="meta", execution_mode="operator-rehearsal",
                evidence_class="audit-log", blocked_by=[],
                scope_components=["meta-anchor"],
                non_goals=["DO NOT close until all 17 children land"]),
        # T02 — phase 31.A; produces `apt-bootstrap`.
        _ticket("OP-3001", labels=["phase:31.A", "class:subscription-codex"],
                **{"class": "subscription-codex"}, scope_components=["scope-31a"],
                cross_phase_blockers=_PRODUCES_APT),
        # T03 — phase 31.B; depends on `apt-bootstrap`.
        _ticket("OP-3002", labels=["phase:31.B", "class:subscription-claude"],
                scope_components=["scope-31b"], execution_mode="integration",
                evidence_class="integration", dependency_artifacts=["apt-bootstrap"]),
        # T04 — phase 31.C; produces `schema-kernel`; structural evidence.
        _ticket("OP-3003", labels=["phase:31.C", "class:subscription-claude"],
                scope_components=["scope-31c"], evidence_class="structural",
                cross_phase_blockers=_PRODUCES_KERNEL),
        # T05 — phase 31.D; depends on `schema-kernel`; behavioral-smoke.
        _ticket("OP-3004", labels=["phase:31.D", "class:subscription-claude"],
                scope_components=["scope-31d"], evidence_class="behavioral-smoke",
                dependency_artifacts=["schema-kernel"]),
        # T06 — phase 31.E; semantic evidence_class.
        _ticket("OP-3005", labels=["phase:31.E", "class:subscription-codex"],
                **{"class": "subscription-codex"}, scope_components=["scope-31e"],
                evidence_class="semantic"),
        # T07 — phase 31.F; spike + longitudinal evidence_class.
        _ticket("OP-3006", labels=["phase:31.F", "class:subscription-claude"],
                scope_components=["scope-31f"], tag_type="spike",
                evidence_class="longitudinal"),
        # T08 — phase 31.G; epic + L1 + public payload + staging destructive op.
        _ticket("OP-3007", labels=["phase:31.G", "class:operator-rehearsal"],
                **{"class": "operator-rehearsal"}, scope_components=["scope-31g"],
                tag_type="epic", execution_mode="operator-rehearsal",
                evidence_class="operator", environment_scope="staging",
                external_payload_class="public", authority_required="L1",
                destructive_op_classes=["filesystem"], destructive_op_scope="staging"),
        # T09 — phase 31.H; operator-window-top (requires l1_exclusive_reason).
        _ticket("OP-3008", labels=["phase:31.H", "class:operator-window-top"],
                **{"class": "operator-window-top"}, scope_components=["scope-31h"],
                execution_mode="operator-rehearsal", evidence_class="operator",
                environment_scope="staging", authority_required="L1",
                l1_exclusive_reason="override", on_scope_creep="abort"),
        # T10 — phase 31.I; operator-window-deputy + L2 + sensitive payload.
        _ticket("OP-3009", labels=["phase:31.I", "class:operator-window-deputy"],
                **{"class": "operator-window-deputy"}, scope_components=["scope-31i"],
                execution_mode="operator-rehearsal", evidence_class="operator",
                environment_scope="staging", authority_required="L2",
                l2_reason="approval-only", external_side_effect="read",
                external_payload_class="sensitive", external_systems=["jira"]),
        # T11 — phase 31.J; credential-material + irreversible.
        _ticket("OP-3010", labels=["phase:31.J", "class:operator-rehearsal"],
                **{"class": "operator-rehearsal"}, scope_components=["scope-31j"],
                execution_mode="operator-rehearsal", evidence_class="audit-log",
                environment_scope="production", authority_required="L1",
                external_payload_class="credential-material",
                external_side_effect="irreversible", reversibility="irreversible",
                runtime_capability="operator-witnessed",
                destructive_op_classes=["key-rotation"],
                destructive_op_scope="production",
                external_systems=["jira", "gerrit"], on_scope_creep="escalate"),
        # T12 — phase 31.K; integration runtime_capability.
        _ticket("OP-3011", labels=["phase:31.K", "class:subscription-claude"],
                scope_components=["scope-31k"], execution_mode="integration",
                evidence_class="integration", runtime_capability="integration"),
        # T13 — phase 31.A spare; schema_version=v0 to exercise the v0 literal.
        _ticket("OP-3012", labels=["phase:31.A", "class:subscription-codex"],
                **{"class": "subscription-codex"},
                scope_components=["scope-31a-spare"], schema_version="v0"),
        # T14 — phase 31.B spare; half of mutex pair with T15.
        _ticket("OP-3013", labels=["phase:31.B", "class:subscription-claude"],
                scope_components=["scope-mutex-pair"], mutex_with=["OP-3014"]),
        # T15 — phase 31.C spare; half of mutex pair with T14.
        _ticket("OP-3014", labels=["phase:31.C", "class:subscription-claude"],
                scope_components=["scope-mutex-pair"], mutex_with=["OP-3013"]),
        # T16 — phase 31.D spare; reversibility=destructive + L1 + write side-effect.
        _ticket("OP-3015", labels=["phase:31.D", "class:operator-rehearsal"],
                **{"class": "operator-rehearsal"},
                scope_components=["scope-31d-spare"], authority_required="L1",
                destructive_op_classes=["git-history"], destructive_op_scope="local",
                external_side_effect="write", execution_mode="operator-rehearsal",
                evidence_class="audit-log", environment_scope="staging",
                reversibility="destructive"),
        # T17 — phase 31.E spare; exercises l1_exclusive_reason on non-top class.
        _ticket("OP-3016", labels=["phase:31.E", "class:subscription-codex"],
                **{"class": "subscription-codex"},
                scope_components=["scope-31e-spare"],
                l1_exclusive_reason="meta-closure", scope_summary_max_chars=500),
        # T18 — phase 31.F spare; cross_phase_blocker with G.A-v0 phase value.
        _ticket("OP-3017", labels=["phase:31.F", "class:subscription-claude"],
                scope_components=["scope-31f-spare"],
                cross_phase_blockers=_PRODUCES_V0),
    ]
    assert len(tickets) == 18, f"roster must be 18 tickets (got {len(tickets)})"
    return tickets


def _preflight_registry() -> PreflightPluginRegistry:
    return PreflightPluginRegistry(
        plugins={plugin.phase_id: plugin for plugin in DEFAULT_REGISTRY.all()}
    )


def _validated_roster(payloads: list[dict[str, Any]]) -> list[TicketContractV1]:
    return [TicketContractV1.model_validate(payload) for payload in payloads]


def _index_of(payloads: list[dict[str, Any]], ticket_key: str) -> int:
    for index, payload in enumerate(payloads):
        if payload["ticket_key"] == ticket_key:
            return index
    raise AssertionError(f"ticket {ticket_key!r} not in roster")


# ---- Positive path ---------------------------------------------------------


def test_roster_size_is_eighteen() -> None:
    assert len(_synthetic_payloads()) == 18


def test_every_v1_field_used_at_least_once() -> None:
    expected = set(_base_payload().keys())
    seen: set[str] = set()
    for payload in _synthetic_payloads():
        seen.update(payload.keys())
    assert expected <= seen, f"missing v1 fields: {sorted(expected - seen)}"


def test_each_ticket_validates_through_pydantic() -> None:
    for payload in _synthetic_payloads():
        TicketContractV1.model_validate(payload)


def test_each_ticket_round_trips_through_v1_json_schema() -> None:
    for payload in _synthetic_payloads():
        jsonschema.validate(payload, V1_SCHEMA_JSON)


def test_default_registry_validate_all_returns_no_errors() -> None:
    for payload in _synthetic_payloads():
        contract = TicketContractV1.model_validate(payload)
        assert DEFAULT_REGISTRY.validate_all(contract) == []


def test_run_all_preflight_checks_on_clean_roster_returns_no_errors() -> None:
    errors = run_all_preflight_checks(
        _validated_roster(_synthetic_payloads()),
        _preflight_registry(),
        CLAIMED_CHILD_COUNT,
    )
    assert errors == [], f"unexpected preflight errors: {errors}"


# ---- V1-18 roster signature ------------------------------------------------


@pytest.fixture()
def hermetic_gnupg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gpg = shutil.which("gpg")
    if gpg is None:
        pytest.skip("gpg not installed")
    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir(mode=0o700)
    completed = subprocess.run(
        [gpg, "--batch", "--no-tty", "--homedir", str(gnupg_home),
         "--import", str(SAMPLE_ROSTER_PUBKEY)],
        check=False, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        pytest.fail(f"failed to import test L1 public key: {completed.stderr}")
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home))
    return gnupg_home


def test_sample_roster_signature_verifies(hermetic_gnupg_home: Path) -> None:
    result = verify_roster_signature(SAMPLE_ROSTER_PATH, TEST_L1_FINGERPRINT)
    assert result.is_valid is True
    assert result.signer_fingerprint == TEST_L1_FINGERPRINT
    assert result.error is None


# ---- Mutation tests — one per preflight rule_id ----------------------------


def test_mutation_credential_material_with_l3_subscription_flags_rule() -> None:
    """AC-specified mutation: payload->credential-material, authority kept L3."""
    payloads = _synthetic_payloads()
    payloads[_index_of(payloads, "OP-3001")]["external_payload_class"] = "credential-material"

    errors = run_all_preflight_checks(
        _validated_roster(payloads), _preflight_registry(), CLAIMED_CHILD_COUNT
    )

    matching = [e for e in errors
                if e.rule_id == "credential-material-not-l1-non-subscription"]
    assert len(matching) == 1, errors
    assert matching[0].ticket_key == "OP-3001"


def test_mutation_invalid_blocker_phase_flags_cross_phase_blocker_shape() -> None:
    payloads = _synthetic_payloads()
    payloads[_index_of(payloads, "OP-3001")]["cross_phase_blockers"] = [{
        "blocker_phase": "not-a-real-phase",
        "blocker_artifact": "apt-bootstrap",
        "reason": "broken on purpose",
    }]

    errors = run_all_preflight_checks(
        _validated_roster(payloads), _preflight_registry(), CLAIMED_CHILD_COUNT
    )

    matching = [e for e in errors if e.rule_id == "cross-phase-blocker-shape"]
    assert len(matching) == 1, errors
    assert matching[0].ticket_key == "OP-3001"


def test_mutation_unproduced_artifact_flags_dependency_orphan() -> None:
    payloads = _synthetic_payloads()
    payloads[_index_of(payloads, "OP-3002")]["dependency_artifacts"] = [
        "apt-bootstrap", "ghost-artifact",
    ]

    errors = run_all_preflight_checks(
        _validated_roster(payloads), _preflight_registry(), CLAIMED_CHILD_COUNT
    )

    matching = [e for e in errors if e.rule_id == "dependency-orphan"]
    assert [e.missing_artifact for e in matching] == ["ghost-artifact"]
    assert matching[0].ticket_key == "OP-3002"


def test_mutation_claimed_count_mismatch_flags_meta_blockedby() -> None:
    roster = _validated_roster(_synthetic_payloads())

    errors = run_all_preflight_checks(roster, _preflight_registry(), {META_KEY: 99})

    matching = [e for e in errors if e.rule_id == "meta-blockedby-count-mismatch"]
    assert len(matching) == 1
    assert (matching[0].meta_key, matching[0].claimed, matching[0].actual) == (
        META_KEY, 99, 17,
    )


def test_mutation_asymmetric_mutex_flags_mutex_asymmetric() -> None:
    payloads = _synthetic_payloads()
    payloads[_index_of(payloads, "OP-3014")]["mutex_with"] = []

    errors = run_all_preflight_checks(
        _validated_roster(payloads), _preflight_registry(), CLAIMED_CHILD_COUNT
    )

    matching = [e for e in errors if e.rule_id == "mutex-asymmetric"]
    assert len(matching) == 1, errors
    assert {matching[0].ticket_key_a, matching[0].ticket_key_b} == {"OP-3013", "OP-3014"}
    assert matching[0].shared_component == "scope-mutex-pair"


def test_mutation_required_vs_forbidden_collision_flags_path_collision() -> None:
    payloads = deepcopy(_synthetic_payloads())
    contested = "workspace/contested.sh"
    payloads[_index_of(payloads, "OP-3001")]["required_paths"] = [contested]
    payloads[_index_of(payloads, "OP-3002")]["forbidden_paths"] = [contested]

    errors = run_all_preflight_checks(
        _validated_roster(payloads), _preflight_registry(), CLAIMED_CHILD_COUNT
    )

    matching = [e for e in errors if e.rule_id == "path-collision"]
    assert len(matching) == 1
    assert (matching[0].ticket_key_owner, matching[0].ticket_key_excluder,
            matching[0].path) == ("OP-3001", "OP-3002", contested)


class _Phase31AV2Stub(BasePhasePlugin):
    """Test-only plugin pinned to v2 to engineer a version mismatch."""
    phase_id = "31.A"
    plugin_version = "v2"  # type: ignore[assignment]


def test_mutation_plugin_v2_for_phase_31a_flags_plugin_version_mismatch() -> None:
    roster = _validated_roster(_synthetic_payloads())
    plugins = {p.phase_id: p for p in DEFAULT_REGISTRY.all()}
    plugins["31.A"] = _Phase31AV2Stub()
    bad_registry = PreflightPluginRegistry(plugins=plugins)

    errors = run_all_preflight_checks(roster, bad_registry, CLAIMED_CHILD_COUNT)

    matching = [e for e in errors if e.rule_id == "plugin-version-mismatch"]
    # Three roster tickets carry phase:31.A — META, T02 (OP-3001), T13 (OP-3012).
    assert {e.ticket_key for e in matching} == {META_KEY, "OP-3001", "OP-3012"}
    assert all(e.actual_plugin_version == "v2" for e in matching)
