"""OP-825 B0 — F11 regression: pre-locate must detect prior patchset.

Pins the rule that ``pre_locate_check`` returns ``RebaseRequired`` when
a Change-Id already exists on Gerrit for the ticket, and ``None`` when
there is no prior PS. Without this check the runner would push a fresh
Change-Id and split the review thread (incident class F11).
"""
from __future__ import annotations

from backend.agents.runner_health_checks import (
    RebaseRequired,
    pre_locate_check,
)


def test_pre_locate_check_returns_rebase_required_for_known_change_id() -> None:
    prior_change_ids = {"OP-Z": "Iabc1234567890"}
    result = pre_locate_check("OP-Z", prior_change_ids)
    assert result == RebaseRequired(change_id="Iabc1234567890")


def test_pre_locate_check_returns_none_when_no_prior_ps() -> None:
    prior_change_ids: dict[str, str] = {}
    result = pre_locate_check("OP-Z", prior_change_ids)
    assert result is None


def test_pre_locate_check_isolates_change_id_per_ticket_key() -> None:
    # A Change-Id mapped to a different ticket key must not match.
    prior_change_ids = {"OP-OTHER": "Iffffffffffffffff"}
    result = pre_locate_check("OP-Z", prior_change_ids)
    assert result is None
