"""Integration tests for the phase 31 plugin scaffold dispatch.

Exercises :data:`governance_engine.plugins.registry.DEFAULT_REGISTRY` end-to-end:
that ``validate_all`` dispatches each synthetic phase ticket to the correct
plugin (via call-counter mocks), ignores non-matching phases, and fans out
to multiple plugins for a multi-phase ticket. G.A-v1-6/-7 ship the stubs +
loader; this test locks the dispatch contract before G.B adds real rules.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.plugins.registry import DEFAULT_REGISTRY
from governance_engine.schema.v1 import TicketContractV1

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
PHASE_SUFFIXES = tuple("abcdefghijk")
ALL_PHASE_IDS = tuple(f"31.{s.upper()}" for s in PHASE_SUFFIXES)


def _ticket(labels: list[str], ticket_key: str = "OP-1086") -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update({
        "schema_version": "v1",
        "ticket_key": ticket_key,
        "labels": labels,
        "phase_plugin_version": "v1",
    })
    return TicketContractV1.model_validate(payload)


@pytest.fixture
def tracked_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, MagicMock]:
    """Wrap every DEFAULT_REGISTRY plugin's `validate` with a call-counter.

    Returns a mapping ``phase_id -> MagicMock`` so each test can assert which
    plugins were dispatched. Each mock wraps the original method so behaviour
    (returns ``[]``) is preserved while call counts are observable.
    """
    plugins = [
        plugin for plugin in DEFAULT_REGISTRY.all() if plugin.phase_id.startswith("31.")
    ]
    assert {p.phase_id for p in plugins} == set(ALL_PHASE_IDS), (
        "DEFAULT_REGISTRY must register all 11 phase 31.A-K stubs before "
        "the dispatch contract can be exercised"
    )
    tracked: dict[str, MagicMock] = {}
    for plugin in plugins:
        mock = MagicMock(wraps=plugin.validate)
        monkeypatch.setattr(plugin, "validate", mock)
        tracked[plugin.phase_id] = mock
    return tracked


def test_default_registry_dispatches_to_correct_phase(
    tracked_registry: dict[str, MagicMock],
) -> None:
    for phase_id in ALL_PHASE_IDS:
        for mock in tracked_registry.values():
            mock.reset_mock()

        ticket = _ticket(labels=[f"phase:{phase_id}"])
        errors = DEFAULT_REGISTRY.validate_all(ticket)

        assert errors == [], (
            f"stubs ship no rules; phase:{phase_id} should yield no errors"
        )
        assert tracked_registry[phase_id].call_count == 1, (
            f"plugin {phase_id} should be invoked exactly once for "
            f"ticket labelled phase:{phase_id}"
        )
        for other_phase_id, mock in tracked_registry.items():
            if other_phase_id == phase_id:
                continue
            assert mock.call_count == 0, (
                f"plugin {other_phase_id} must not be invoked for "
                f"ticket labelled phase:{phase_id}"
            )


def test_dispatch_ignores_non_matching_phase(
    tracked_registry: dict[str, MagicMock],
) -> None:
    ticket = _ticket(labels=["phase:99.Z"])

    errors = DEFAULT_REGISTRY.validate_all(ticket)

    assert errors == []
    for phase_id, mock in tracked_registry.items():
        assert mock.call_count == 0, (
            f"plugin {phase_id} must not be invoked for non-matching "
            "phase:99.Z label"
        )


def test_dispatch_handles_multiple_phase_labels(
    tracked_registry: dict[str, MagicMock],
) -> None:
    ticket = _ticket(labels=["phase:31.A", "phase:31.B"])

    errors = DEFAULT_REGISTRY.validate_all(ticket)

    assert errors == []
    assert tracked_registry["31.A"].call_count == 1
    assert tracked_registry["31.B"].call_count == 1
    for phase_id, mock in tracked_registry.items():
        if phase_id in {"31.A", "31.B"}:
            continue
        assert mock.call_count == 0, (
            f"plugin {phase_id} must not be invoked for multi-phase "
            "ticket labelled phase:31.A + phase:31.B"
        )
