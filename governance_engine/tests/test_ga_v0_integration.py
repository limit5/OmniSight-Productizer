"""OP-1056: G.A-v0 Integration proof (synthetic 31.A end-to-end).

Per S12.G v2 spec §5.IAC: synthesize 31.A ticket payloads through the
v0 kernel and prove the positive + negative chain works. CONSUMER of
A1-A7 — does not modify schema, runner refusal helper, or override CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents.jira_dispatch import _runner_refuses_pickup
from governance_engine.schema.forbidden_combinations import (
    validate_forbidden_combinations,
)
from governance_engine.schema.v0 import TicketClass, TicketContract

A3_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
OVERRIDE_CLI = REPO_ROOT / "scripts" / "governance" / "manual-override.py"


def _load_a3_golden() -> dict[str, object]:
    return yaml.safe_load(A3_FIXTURE_PATH.read_text(encoding="utf-8"))


# Shared scaffold for the 3 synthetic positive payloads — values that
# don't vary between 1b / 2a / 3a (spec §6). Each test case overlays
# only the fields the spec actually changes.
_BASE_31A_PAYLOAD: dict[str, object] = {
    "schema_version": "v0",
    "authority_required": "L3",
    "l1_exclusive_reason": None,
    "l2_reason": None,
    "non_goals": ["DO NOT exceed boundary"],
    "test_scope": "defer-to-integration",
    "destructive_op_classes": [],
    "destructive_op_scope": "none",
    "external_side_effect": "none",
    "external_payload_class": "operational",
    "runtime_capability": "unit-only",
    "execution_mode": "unit-testable",
    "mutex_with": [],
    "evidence_class": "unit",
    "on_scope_creep": "file-followup",
    "scope_summary_max_chars": 200,
    "tag_type": "story",
    "reversibility": "reversible",
    "environment_scope": "local",
    "external_systems": [],
    "dependency_artifacts": [],
}


def _payload(**overrides: object) -> dict[str, object]:
    return {**_BASE_31A_PAYLOAD, **overrides}


# Spec §6 31.A-1b / 2a / 3a — each overlays only what changes from
# _BASE_31A_PAYLOAD. files_touched_max=1 and required_paths length 1
# for all three (one shell script / yaml file per ticket).
_PAYLOAD_31A_1B = _payload(
    **{"class": "subscription-codex"},
    loc_delta_max=60, files_touched_max=1,
    required_paths=["scripts/setup-dev-env-full/01-node.sh"],
    forbidden_paths=[
        "scripts/setup-dev-env-full/01-base-tools.sh",
        "scripts/setup-dev-env-full.sh",
    ],
    interface_contract="outputs: 01-node.sh; inputs: apt base from 31.A-1a",
    scope_components=["devops", "tooling"],
    dependency_artifacts=["scripts/setup-dev-env-full/01-base-tools.sh"],
)
_PAYLOAD_31A_2A = _payload(
    **{"class": "subscription-claude"},
    loc_delta_max=200, files_touched_max=1,
    required_paths=["scripts/setup-dev-env-full/02-creds.template.yaml"],
    forbidden_paths=[
        "scripts/setup-dev-env-full/02-creds.sh",
        "scripts/setup-dev-env-full/02-ssh-keys.sh",
    ],
    interface_contract="outputs: 02-creds.template.yaml >=16 entries; inputs: none",
    scope_components=["devops", "security"],
)
_PAYLOAD_31A_3A = _payload(
    **{"class": "subscription-codex"},
    loc_delta_max=50, files_touched_max=1,
    required_paths=["scripts/setup-dev-env-full/03-clone-productizer.sh"],
    forbidden_paths=[
        "scripts/setup-dev-env-full/03-clone-sora-bridge.sh",
        "scripts/setup-dev-env-full/03-git-remote-config.sh",
    ],
    interface_contract="outputs: Productizer clone from Gerrit; inputs: git + claude-bot SSH key",
    scope_components=["devops", "tooling"],
    external_side_effect="read",
    dependency_artifacts=[
        "scripts/setup-dev-env-full/01-base-tools.sh",
        "scripts/setup-dev-env-full/02-ssh-keys.sh",
    ],
    external_systems=["gerrit"],
)


def test_a3_golden_fixture_validates() -> None:
    contract = TicketContract.model_validate(_load_a3_golden())

    assert validate_forbidden_combinations(contract) == []


@pytest.mark.parametrize(
    ("ticket_id", "payload"),
    [
        ("31.A-1b", _PAYLOAD_31A_1B),
        ("31.A-2a", _PAYLOAD_31A_2A),
        ("31.A-3a", _PAYLOAD_31A_3A),
    ],
)
def test_synthetic_31a_payloads_validate(
    ticket_id: str, payload: dict[str, object]
) -> None:
    contract = TicketContract.model_validate(payload)

    assert validate_forbidden_combinations(contract) == [], ticket_id


def test_negative_missing_required_field_raises() -> None:
    bad = deepcopy(_PAYLOAD_31A_1B)
    bad.pop("destructive_op_classes")

    with pytest.raises(ValidationError):
        TicketContract.model_validate(bad)


def test_negative_operator_top_without_l1_reason_hits_rule_3() -> None:
    # Pydantic @model_validator catches this at parse time; use
    # model_construct to prove the forbidden-combinations layer also
    # catches it independently (matches test_v0_kernel.py convention).
    base = TicketContract.model_validate(_load_a3_golden()).model_dump()
    base.update(
        {
            "ticket_class": TicketClass.OPERATOR_WINDOW_TOP,
            "l1_exclusive_reason": None,
        }
    )
    contract = TicketContract.model_construct(**base)

    errors = validate_forbidden_combinations(contract)

    assert [e.rule_id for e in errors] == [3]


def test_negative_destructive_op_with_l3_authority_hits_rule_1() -> None:
    bad = deepcopy(_PAYLOAD_31A_1B)
    bad["destructive_op_classes"] = ["rm-rf"]
    bad["authority_required"] = "L3"
    contract = TicketContract.model_validate(bad)

    errors = validate_forbidden_combinations(contract)

    assert [e.rule_id for e in errors] == [1]


def test_runner_refuses_operator_window_top_label() -> None:
    labels = [
        "tier:S",
        "agent:auto",
        "class:operator-window-top",
        "class:subscription-claude",
    ]

    refused, refusal_label = _runner_refuses_pickup(labels)

    assert refused is True
    assert refusal_label == "class:operator-window-top"


def test_override_cli_self_test_passes(tmp_path: Path) -> None:
    # The CLI's self-test deletes its own audit log in a finally block,
    # so we verify the audit line shape via the ``override recorded:``
    # stdout summary cmd_apply prints — same JSON it would append to
    # the audit log.
    env = os.environ.copy()
    env.setdefault("HOME", str(tmp_path))

    result = subprocess.run(
        [sys.executable, str(OVERRIDE_CLI), "self-test"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"self-test exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "self-test PASS" in result.stdout

    summaries = [
        line[len("override recorded: ") :]
        for line in result.stdout.splitlines()
        if line.startswith("override recorded: ")
    ]
    assert summaries, "self-test stdout must include an override summary"
    record = json.loads(summaries[-1])
    expected_fields = {
        "ts_utc",
        "ticket",
        "rule_id",
        "reason",
        "l1_fingerprint_hash",
        "operator_uid",
        "hostname",
    }
    assert expected_fields <= set(record.keys())
    assert record["ticket"] == "OP-9999"
    assert record["rule_id"] == 5
    assert len(record["l1_fingerprint_hash"]) == 64  # sha256 hex
