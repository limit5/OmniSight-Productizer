"""RPG.W2.3 — drift guard for ``backend/agents/guild_registry.py``.

ADR-0008 declares the RPG Guild + class registry an *importer* of
upstream sources of truth, never an authoritative duplicate:

* ``backend.sandbox_tier.Guild`` owns the Guild enum slugs (BP.B).
* ``configs/model_mapping.yaml`` owns the per-Guild model preference
  (BP.F — "Mixed-mode model mapping"); routing reads it via
  ``routing_policy._load_model_routing_matrix``.
* ``backend.agents.routing_policy.ROUTING_POLICY_CONSUMED_AGENT_CLASS_LABELS``
  enumerates which ``agent_class`` strings the MP multi-provider
  orchestrator will actually route to (subscription-* / api-* per
  ADR-0007's vendor matrix).

This drift guard pins the two subset invariants RPG.W2.3 promises:

1. ``guild_registry.GUILDS`` ⊆ BP.F (``configs/model_mapping.yaml``
   ``guilds:`` keys). If a new Guild slug is added to the registry it
   must also be wired into BP.F's per-Guild model preference, otherwise
   ``routing_policy._preferred_provider_family_for_task`` silently
   falls back to no preference for that Guild.

2. The routable subset of ``AGENT_CLASS_GUILD_MATRIX`` keys ⊆
   ``ROUTING_POLICY_CONSUMED_AGENT_CLASS_LABELS``. Two registry keys
   are documented non-routable escape hatches and are excluded from
   the comparison:

       ``unassigned``     — placeholder per ``agent_class_schema.yaml``
                            for TODO items pending operator class.
       ``local-llm-qwen`` — local LLM slot; routing_policy is for
                            remote subscription/API providers only.

   Both are listed in ``config/agent_class_schema.yaml`` allowed_values,
   so the test also verifies every registry key is at least known to
   the canonical schema (catches typo drift in either direction).

A test failing here means the RPG registry has drifted off one of the
two upstream label sources. The fix is to update whichever side
genuinely changed — never to relax the guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from backend.agents.guild_registry import (
    AGENT_CLASS_GUILD_MATRIX,
    GUILDS,
)
from backend.agents.routing_policy import (
    ROUTING_POLICY_CONSUMED_AGENT_CLASS_LABELS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_MAPPING_PATH = REPO_ROOT / "configs" / "model_mapping.yaml"
AGENT_CLASS_SCHEMA_PATH = REPO_ROOT / "config" / "agent_class_schema.yaml"

# Registry keys that intentionally have no MP routing_policy mapping —
# documented in ``config/agent_class_schema.yaml`` allowed_values. Keep
# this set small; every addition needs a one-line rationale.
NON_ROUTABLE_AGENT_CLASSES: frozenset[str] = frozenset(
    {
        # placeholder per agent_class_schema.yaml `unknown_value`; MP
        # routing has nothing to dispatch to until operator classifies.
        "unassigned",
        # local Qwen runner; routing_policy only ranks remote
        # subscription/API providers.
        "local-llm-qwen",
    }
)


def _bp_f_guild_labels() -> frozenset[str]:
    """Return the Guild slugs declared by BP.F's model mapping."""

    raw = yaml.safe_load(MODEL_MAPPING_PATH.read_text(encoding="utf-8")) or {}
    guilds = raw.get("guilds")
    if not isinstance(guilds, dict):
        return frozenset()
    return frozenset(str(slug).strip() for slug in guilds if isinstance(slug, str))


def _agent_class_schema_allowed_values() -> frozenset[str]:
    """Return the ``allowed_values`` list from the agent_class schema."""

    raw = yaml.safe_load(AGENT_CLASS_SCHEMA_PATH.read_text(encoding="utf-8")) or {}
    allowed = raw.get("allowed_values")
    if not isinstance(allowed, list):
        return frozenset()
    return frozenset(str(value).strip() for value in allowed if isinstance(value, str))


def test_bp_f_model_mapping_present() -> None:
    """BP.F's model_mapping.yaml must exist and declare guild entries."""

    assert MODEL_MAPPING_PATH.exists(), f"BP.F source of truth missing: {MODEL_MAPPING_PATH.relative_to(REPO_ROOT)}"
    bp_f_labels = _bp_f_guild_labels()
    assert bp_f_labels, (
        "BP.F model_mapping.yaml `guilds:` section is empty — the drift "
        "guard cannot verify registry ⊆ BP.F when BP.F is itself missing."
    )


def test_guild_registry_subset_of_bp_f() -> None:
    """RPG.W2.3 invariant #1: ``GUILDS`` ⊆ BP.F ``guilds:``.

    Every Guild slug the RPG registry declares must also have a model
    preference row in BP.F. If this fails, either add the new Guild to
    ``configs/model_mapping.yaml`` (preferred) or remove it from the
    registry — never delete this assertion.
    """

    bp_f_labels = _bp_f_guild_labels()
    extra = GUILDS - bp_f_labels
    assert not extra, (
        "RPG Guild registry has drifted beyond BP.F model mapping. "
        f"Slugs missing from configs/model_mapping.yaml `guilds:`: "
        f"{sorted(extra)!r}. BP.F currently knows: {sorted(bp_f_labels)!r}."
    )


def test_agent_class_matrix_subset_of_schema() -> None:
    """Every ``AGENT_CLASS_GUILD_MATRIX`` key must be in the canonical schema.

    ``config/agent_class_schema.yaml`` (MP.W0.1) is the authoritative
    list of allowed ``agent_class`` values. Drift here means the registry
    invented an agent_class label that the orchestrator's CLI / TODO
    parser will reject as ``unassigned``.
    """

    schema_labels = _agent_class_schema_allowed_values()
    assert schema_labels, (
        "agent_class_schema.yaml `allowed_values` is empty — the drift "
        "guard cannot verify registry keys against the canonical schema."
    )
    matrix_keys = frozenset(AGENT_CLASS_GUILD_MATRIX)
    extra = matrix_keys - schema_labels
    assert not extra, (
        "RPG AGENT_CLASS_GUILD_MATRIX has agent_class keys absent from "
        f"the canonical schema: {sorted(extra)!r}. Allowed values per "
        f"config/agent_class_schema.yaml: {sorted(schema_labels)!r}."
    )


def test_routable_agent_class_keys_subset_of_routing_policy_consumed() -> None:
    """RPG.W2.3 invariant #2: routable matrix keys ⊆ routing_policy consumed.

    Every ``AGENT_CLASS_GUILD_MATRIX`` key that is *not* a documented
    non-routable escape hatch must be a label that MP routing_policy
    actually consumes (subscription-* / api-* per ADR-0007's vendor
    matrix). Otherwise the registry would claim to assign Guilds to a
    class that the orchestrator could never dispatch.
    """

    matrix_keys = frozenset(AGENT_CLASS_GUILD_MATRIX)
    routable_keys = matrix_keys - NON_ROUTABLE_AGENT_CLASSES
    extra = routable_keys - ROUTING_POLICY_CONSUMED_AGENT_CLASS_LABELS
    assert not extra, (
        "RPG AGENT_CLASS_GUILD_MATRIX has routable keys absent from MP "
        f"routing_policy consumed labels: {sorted(extra)!r}. "
        "routing_policy consumes: "
        f"{sorted(ROUTING_POLICY_CONSUMED_AGENT_CLASS_LABELS)!r}. "
        "Either add the agent_class to "
        "ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS, or add it to "
        "NON_ROUTABLE_AGENT_CLASSES in this guard with rationale."
    )


@pytest.mark.parametrize("agent_class", sorted(NON_ROUTABLE_AGENT_CLASSES))
def test_non_routable_exceptions_are_known_to_schema(agent_class: str) -> None:
    """Pin the non-routable allowlist to the canonical schema.

    The escape-hatch values that bypass invariant #2 must still appear
    in ``config/agent_class_schema.yaml`` allowed_values. This keeps
    the allowlist from silently absorbing typos.
    """

    schema_labels = _agent_class_schema_allowed_values()
    assert agent_class in schema_labels, (
        f"{agent_class!r} is listed as a non-routable RPG escape hatch "
        "but is not in agent_class_schema.yaml allowed_values: "
        f"{sorted(schema_labels)!r}."
    )
