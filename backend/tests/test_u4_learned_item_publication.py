"""OP-2567 U4-B — pure publication state machine + hash tests.

Everything here is offline and I/O-free: the module under test is the
PURE layer (no SQL, no DB). The frozen transition table, the ordered
reduction, the V3.3 scope key, the G4 live-set hash, and the
advisory-lock key documentation helper.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from backend.learned_item_publication import (
    LEGAL_TRANSITIONS,
    PUBLICATION_STATES,
    advisory_lock_key,
    compute_live_set_hash,
    is_legal_transition,
    is_live,
    publication_scope_key,
    reduce_publication_state,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PURE_MODULE = BACKEND_ROOT / "learned_item_publication.py"


# ━━ Transition table ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTransitions:
    def test_every_legal_transition_accepted(self) -> None:
        for prior, targets in LEGAL_TRANSITIONS.items():
            for new in targets:
                assert is_legal_transition(prior, new), (prior, new)

    @pytest.mark.parametrize(
        "prior,new",
        [
            ("approved", "published"),  # cannot skip publishing
            (None, "publishing"),  # first event must be approved
            ("published", "publishing"),  # no republish of live item
        ],
    )
    def test_illegal_transitions_rejected(self, prior, new) -> None:
        assert not is_legal_transition(prior, new)

    @pytest.mark.parametrize("terminal", ("revoked", "superseded"))
    def test_terminal_states_allow_nothing(self, terminal: str) -> None:
        for new in PUBLICATION_STATES:
            assert not is_legal_transition(terminal, new)

    def test_unknown_prior_state_rejected(self) -> None:
        assert not is_legal_transition("not-a-state", "approved")

    def test_table_covers_exactly_the_frozen_states(self) -> None:
        assert set(LEGAL_TRANSITIONS) == set(PUBLICATION_STATES) | {None}


# ━━ Reduction / is_live ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _events(*states: str) -> list[dict[str, str]]:
    return [{"state": s} for s in states]


class TestReduction:
    def test_empty_reduces_to_none(self) -> None:
        assert reduce_publication_state([]) is None
        assert not is_live([])

    def test_retry_chain_ends_live(self) -> None:
        chain = _events(
            "approved", "publishing", "publish_failed", "publishing",
            "published",
        )
        assert reduce_publication_state(chain) == "published"
        assert is_live(chain)

    def test_revoked_chain_is_not_live(self) -> None:
        chain = _events("approved", "publishing", "published", "revoked")
        assert reduce_publication_state(chain) == "revoked"
        assert not is_live(chain)

    def test_superseded_chain_is_not_live(self) -> None:
        chain = _events("approved", "publishing", "published", "superseded")
        assert reduce_publication_state(chain) == "superseded"
        assert not is_live(chain)

    def test_mid_flight_chain_is_not_live(self) -> None:
        chain = _events("approved", "publishing")
        assert reduce_publication_state(chain) == "publishing"
        assert not is_live(chain)

    def test_accepts_objects_with_state_attribute(self) -> None:
        class Event:
            def __init__(self, state: str) -> None:
                self.state = state

        chain = [Event("approved"), Event("publishing"), Event("published")]
        assert reduce_publication_state(chain) == "published"
        assert is_live(chain)

    def test_caller_supplies_order_no_sorting(self) -> None:
        # The module folds in the GIVEN order — reversing the chain
        # must change the outcome (proving it does not sort).
        chain = _events("approved", "publishing", "published")
        assert reduce_publication_state(list(reversed(chain))) == "approved"


# ━━ Scope key (freeze V3.3) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScopeKey:
    def test_tenant_scope(self) -> None:
        assert publication_scope_key("tenant", "t-1") == "tenant:t-1"

    def test_global_scope(self) -> None:
        assert publication_scope_key("global", None) == "global:-"

    def test_non_frozen_audience_rejected(self) -> None:
        with pytest.raises(ValueError):
            publication_scope_key("project", "p-1")

    def test_global_with_tenant_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            publication_scope_key("global", "t-1")

    def test_tenant_without_tenant_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            publication_scope_key("tenant", None)

    def test_advisory_lock_key_is_identity(self) -> None:
        # Key derivation is server-side hashtext — the helper only
        # documents/normalizes (no strip/lower).
        assert advisory_lock_key("tenant:t-1") == "tenant:t-1"
        assert advisory_lock_key("Global: X ") == "Global: X "


# ━━ Live-set hash (freeze G4) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _member(vid: str, **overrides):
    member = {
        "version_id": vid,
        "rendered_payload_sha256": "f" * 64,
        "delivery_mode": "retrieved",
        "publication_event_seq": 7,
    }
    member.update(overrides)
    return member


class TestLiveSetHash:
    def test_deterministic(self) -> None:
        members = [_member("v-1"), _member("v-2")]
        assert compute_live_set_hash(members) == compute_live_set_hash(
            members
        )

    def test_shape_is_64_hex(self) -> None:
        h = compute_live_set_hash([_member("v-1")])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_order_independent(self) -> None:
        members = [_member(f"v-{i}") for i in range(8)]
        shuffled = members[:]
        random.Random(42).shuffle(shuffled)
        assert compute_live_set_hash(members) == compute_live_set_hash(
            shuffled
        )

    @pytest.mark.parametrize(
        "override",
        [
            {"version_id": "v-CHANGED"},
            {"rendered_payload_sha256": "e" * 64},
            {"delivery_mode": "always_injected"},
            {"publication_event_seq": 8},
        ],
    )
    def test_any_component_change_changes_hash(self, override) -> None:
        base = [_member("v-1"), _member("v-2")]
        changed = [_member("v-1", **override), _member("v-2")]
        assert compute_live_set_hash(base) != compute_live_set_hash(changed)

    def test_membership_change_changes_hash(self) -> None:
        assert compute_live_set_hash(
            [_member("v-1")]
        ) != compute_live_set_hash([_member("v-1"), _member("v-2")])

    def test_missing_key_raises(self) -> None:
        member = _member("v-1")
        del member["delivery_mode"]
        with pytest.raises(ValueError):
            compute_live_set_hash([member])

    def test_unknown_key_raises(self) -> None:
        member = _member("v-1")
        member["extra"] = "nope"
        with pytest.raises(ValueError):
            compute_live_set_hash([member])

    def test_empty_membership_hashes(self) -> None:
        h = compute_live_set_hash([])
        assert len(h) == 64


# ━━ String-ban guard ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


LEDGER_TABLE_NAMES = (
    "learned_item_versions",
    "learned_item_evidence",
    "memory_eval_runs",
    "memory_eval_cases",
    "memory_approvals",
    "memory_publications",
    "memory_transition_events",
    "learned_item_snapshots",
)


class TestPureModuleStringBans:
    def test_no_ledger_table_name_in_pure_module(self) -> None:
        # The pure module must stay SQL-free — only the sanctioned
        # writer boundary may name the ledger tables (A1's dormant
        # sweep allowlists it alone).
        source = PURE_MODULE.read_text()
        for table in LEDGER_TABLE_NAMES:
            assert table not in source, table
