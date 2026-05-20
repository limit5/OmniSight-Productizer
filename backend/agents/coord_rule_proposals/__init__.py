"""Operator-reviewed draft Tier-1 rule proposals (ADR-0021 §10.2).

The coordinator's weekly self-retrospective (``backend.agents.learning_loop``)
groups Tier-2 decisions by ``(situation_profile_class, action_type)``; a class
that consistently produced the expected outcome graduates to a *draft* Tier-1
rule written here as ``<slug>.py`` and the operator is @-mentioned to review it.

These files are **not** auto-loaded by the Tier-1 rule registry
(``load_registry`` discovers rules from ``pipeline_coordinator_rules``). A
proposal becomes active only when an operator reviews it, fills in the
predicate, and moves the rule into ``pipeline_coordinator_rules.py`` — graduated
rules are never auto-merged (ADR-0021 §13 risk table).
"""
