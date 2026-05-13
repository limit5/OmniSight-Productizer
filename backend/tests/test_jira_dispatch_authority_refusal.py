"""OP-1054: L3 runner refusal tests for operator-window classes.

Pins the A5 contract in ``backend.agents.jira_dispatch``: any
``class:operator-window-*`` or ``class:operator-rehearsal`` label makes
L3 runners refuse pickup, even when subscription labels are also present.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd


def _refused(labels: list[str]) -> bool:
    refused, _ = jd._runner_refuses_pickup(labels)
    return refused


def test_refuses_operator_window_top() -> None:
    assert _refused(["class:operator-window-top", "agent:auto"]) is True


def test_refuses_operator_window_deputy() -> None:
    assert _refused(["class:operator-window-deputy"]) is True


def test_refuses_operator_rehearsal() -> None:
    assert _refused(["class:operator-rehearsal"]) is True


def test_refuses_dual_class_operator_wins() -> None:
    refused, refusal_label = jd._runner_refuses_pickup([
        "class:operator-window-top",
        "class:subscription-claude",
    ])

    assert refused is True
    assert refusal_label == "class:operator-window-top"


def test_accepts_subscription_claude() -> None:
    assert _refused(["class:subscription-claude"]) is False


def test_accepts_subscription_codex() -> None:
    assert _refused(["class:subscription-codex"]) is False


def test_accepts_no_class_label() -> None:
    assert _refused(["agent:auto", "tier:S"]) is False


def test_refusal_emits_audit_event(caplog, monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_INSTANCE_ID", "runner-op1054")

    with caplog.at_level(logging.INFO, logger="backend.agents.jira_dispatch"):
        jd._emit_runner_refusal_audit("OP-1054", "class:operator-window-top")

    records = [
        record
        for record in caplog.records
        if "runner_refusal_by_class" in record.getMessage()
    ]
    assert len(records) == 1

    _, payload_json = records[0].getMessage().split(" ", 1)
    payload = json.loads(payload_json)
    assert payload == {
        "event": "runner_refusal_by_class",
        "refusal_label": "class:operator-window-top",
        "runner_instance": "runner-op1054",
        "ticket_key": "OP-1054",
    }
