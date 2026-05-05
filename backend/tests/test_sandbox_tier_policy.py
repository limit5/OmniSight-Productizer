"""BP.S.6 — Guild × Tier sandbox policy contract tests.

This file is the dedicated drift guard for BP.S.1..BP.S.5:

* ``backend.sandbox_tier`` declares the structural Guild × Tier matrix.
* ``docs/design/sandbox-tier-audit.md`` cites that matrix for Phase D
  auxiliary compliance checks.
* ``configs/sandbox_tier_policy.yaml`` documents the operator narrowing
  layer. The runtime loader is future work, so these tests pin the parsing
  contract without adding a production code path.
* ``backend.pep_gateway`` consumes the matrix when callers pass
  ``guild_id``.

SOP §1 module-global audit: these tests read immutable module constants and
static files only; every worker derives the same expected matrix from the
same repository contents. No mutable cache, singleton, or env knob is added.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml

from backend import pep_gateway as pep
from backend.sandbox_tier import (
    GUILD_TIER_ADMISSION_MATRIX,
    Guild,
    GuildTierViolation,
    SandboxTier,
    admitted_tiers,
    assert_admitted,
    is_admitted,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "configs" / "sandbox_tier_policy.yaml"
AUDIT_DOC_PATH = REPO_ROOT / "docs" / "design" / "sandbox-tier-audit.md"

EXPECTED_MATRIX: dict[Guild, frozenset[SandboxTier]] = {
    Guild.architect: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.sa_sd: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.ux: frozenset({SandboxTier.T0}),
    Guild.pm: frozenset({SandboxTier.T0}),
    Guild.gateway: frozenset({SandboxTier.T0}),
    Guild.bsp: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.hal: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.algo_cv: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.optical: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.isp: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.audio: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.frontend: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.backend: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.sre: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.qa: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.auditor: frozenset({SandboxTier.T0}),
    Guild.red_team: frozenset({SandboxTier.T1, SandboxTier.T2}),
    Guild.forensics: frozenset(
        {SandboxTier.T0, SandboxTier.T1, SandboxTier.T2, SandboxTier.T3}
    ),
    Guild.intel: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.reporter: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.custom: frozenset({SandboxTier.T0, SandboxTier.T2}),
}

PEP_TO_SANDBOX_TIER = {
    "t0": SandboxTier.T0,
    "t1": SandboxTier.T1,
    "t2": SandboxTier.T2,
    "networked": SandboxTier.T2,
    "t3": SandboxTier.T3,
}


def _policy_doc(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if payload is not None:
        return payload
    with POLICY_PATH.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, dict), "sandbox_tier_policy.yaml must parse as a mapping"
    return doc


def _effective_tiers_from_policy(
    guild: Guild,
    payload: dict[str, Any] | None = None,
) -> frozenset[SandboxTier]:
    """Mirror the BP.S.2 yaml narrowing contract for test assertions only."""
    doc = _policy_doc(payload)
    assert doc.get("schema_version") == 1
    guilds = doc.get("guilds") or {}
    assert isinstance(guilds, dict), "guilds must be a mapping when present"
    for slug, spec in guilds.items():
        try:
            known_guild = Guild(slug)
        except ValueError as exc:
            raise AssertionError(f"unknown guild slug in policy: {slug!r}") from exc
        assert isinstance(spec, dict), f"{known_guild.value} policy must be a mapping"
        raw_tiers = spec.get("allowed_tiers")
        assert isinstance(raw_tiers, list), (
            f"{known_guild.value}.allowed_tiers must be a list"
        )
        try:
            parsed = frozenset(SandboxTier(tier) for tier in raw_tiers)
        except ValueError as exc:
            raise AssertionError(
                f"{known_guild.value}.allowed_tiers contains an unknown tier"
            ) from exc
        structural = GUILD_TIER_ADMISSION_MATRIX[known_guild]
        if not parsed.issubset(structural):
            raise AssertionError(
                f"{known_guild.value}.allowed_tiers widens the structural matrix"
            )
    spec = guilds.get(guild.value)
    if spec is None:
        return admitted_tiers(guild)
    return frozenset(SandboxTier(tier) for tier in spec["allowed_tiers"])


def _audit_doc_matrix() -> dict[Guild, frozenset[SandboxTier]]:
    text = AUDIT_DOC_PATH.read_text(encoding="utf-8")
    rows: dict[Guild, frozenset[SandboxTier]] = {}
    row_re = re.compile(r"^\| `(?P<guild>[^`]+)` \| \*\*(?P<tiers>[^*]+)\*\*")
    for line in text.splitlines():
        match = row_re.match(line)
        if match is None:
            continue
        guild = Guild(match.group("guild"))
        tiers = frozenset(
            SandboxTier(part.strip()) for part in match.group("tiers").split(",")
        )
        rows[guild] = tiers
    return rows


class TestSandboxTierMatrix:
    def test_tier_enum_wire_values_are_stable(self) -> None:
        assert [tier.value for tier in SandboxTier] == ["T0", "T1", "T2", "T3"]

    def test_guild_enum_slug_set_is_stable(self) -> None:
        assert [guild.value for guild in Guild] == [
            "architect",
            "sa_sd",
            "ux",
            "pm",
            "gateway",
            "bsp",
            "hal",
            "algo_cv",
            "optical",
            "isp",
            "audio",
            "frontend",
            "backend",
            "sre",
            "qa",
            "auditor",
            "red_team",
            "forensics",
            "intel",
            "reporter",
            "custom",
        ]

    def test_matrix_is_read_only_mapping(self) -> None:
        assert isinstance(GUILD_TIER_ADMISSION_MATRIX, MappingProxyType)
        with pytest.raises(TypeError):
            GUILD_TIER_ADMISSION_MATRIX[Guild.auditor] = frozenset(  # type: ignore[index]
                {SandboxTier.T0, SandboxTier.T1}
            )

    def test_matrix_matches_bp_s1_contract(self) -> None:
        assert dict(GUILD_TIER_ADMISSION_MATRIX) == EXPECTED_MATRIX

    def test_every_guild_has_non_empty_frozen_tier_set(self) -> None:
        for guild in Guild:
            tiers = admitted_tiers(guild)
            assert tiers
            assert isinstance(tiers, frozenset)
            assert all(isinstance(tier, SandboxTier) for tier in tiers)

    def test_is_admitted_matches_exact_matrix_and_denial_complement(self) -> None:
        admitted = denied = 0
        for guild in Guild:
            for tier in SandboxTier:
                expected = tier in EXPECTED_MATRIX[guild]
                assert is_admitted(guild, tier) is expected
                admitted += int(expected)
                denied += int(not expected)
        assert admitted == 40
        assert denied == 44

    def test_assert_admitted_names_forbidden_pair_and_permitted_tiers(self) -> None:
        with pytest.raises(GuildTierViolation) as exc:
            assert_admitted(Guild.auditor, SandboxTier.T1)
        message = str(exc.value)
        assert "auditor" in message
        assert "T1" in message
        assert "T0" in message

    def test_forensics_is_the_only_all_tier_guild(self) -> None:
        all_tiers = frozenset(SandboxTier)
        assert admitted_tiers(Guild.forensics) == all_tiers
        assert [
            guild
            for guild, tiers in GUILD_TIER_ADMISSION_MATRIX.items()
            if tiers == all_tiers
        ] == [Guild.forensics]


class TestSandboxTierAuditDoc:
    def test_audit_doc_declares_one_row_per_guild(self) -> None:
        rows = _audit_doc_matrix()
        assert set(rows) == set(Guild)
        assert len(rows) == 21

    def test_audit_doc_admitted_tiers_match_runtime_matrix(self) -> None:
        assert _audit_doc_matrix() == EXPECTED_MATRIX

    def test_audit_doc_calls_out_notable_denials(self) -> None:
        text = AUDIT_DOC_PATH.read_text(encoding="utf-8")
        assert "`auditor` × {T1, T2, T3}" in text
        assert "`gateway` × {T1, T2, T3}" in text
        assert "`red_team` × {T0, T3}" in text
        assert "All cloud-brain Guilds × {T1, T3}" in text


class TestSandboxTierPolicyYaml:
    def test_policy_yaml_parses_default_schema(self) -> None:
        doc = _policy_doc()
        assert doc["schema_version"] == 1
        assert doc["guilds"] == {}

    def test_omitted_guild_inherits_structural_matrix(self) -> None:
        assert _effective_tiers_from_policy(Guild.backend, {"schema_version": 1}) == (
            EXPECTED_MATRIX[Guild.backend]
        )

    def test_explicit_subset_narrows_structural_matrix(self) -> None:
        payload = {
            "schema_version": 1,
            "guilds": {
                "backend": {
                    "allowed_tiers": ["T0"],
                    "reason": "air-gapped: no outbound network",
                },
            },
        }
        assert _effective_tiers_from_policy(Guild.backend, payload) == frozenset(
            {SandboxTier.T0}
        )
        assert _effective_tiers_from_policy(Guild.frontend, payload) == (
            EXPECTED_MATRIX[Guild.frontend]
        )

    def test_explicit_empty_list_forbids_guild_from_all_tiers(self) -> None:
        payload = {
            "schema_version": 1,
            "guilds": {
                "red_team": {
                    "allowed_tiers": [],
                    "reason": "incident lockdown",
                },
            },
        }
        assert _effective_tiers_from_policy(Guild.red_team, payload) == frozenset()

    def test_policy_rejects_unknown_guild_slug(self) -> None:
        payload = {
            "schema_version": 1,
            "guilds": {"red-team": {"allowed_tiers": ["T1"]}},
        }
        with pytest.raises(AssertionError, match="unknown guild slug"):
            _effective_tiers_from_policy(Guild.red_team, payload)

    def test_policy_rejects_unknown_tier_label(self) -> None:
        payload = {
            "schema_version": 1,
            "guilds": {"backend": {"allowed_tiers": ["T4"]}},
        }
        with pytest.raises(AssertionError, match="unknown tier"):
            _effective_tiers_from_policy(Guild.backend, payload)

    def test_policy_rejects_widening_past_structural_matrix(self) -> None:
        payload = {
            "schema_version": 1,
            "guilds": {"auditor": {"allowed_tiers": ["T0", "T1"]}},
        }
        with pytest.raises(AssertionError, match="widens"):
            _effective_tiers_from_policy(Guild.auditor, payload)


class TestPepGatewaySandboxTierIntegration:
    def test_pep_tier_to_sandbox_mapping_matches_documented_contract(self) -> None:
        for pep_tier, sandbox_tier in PEP_TO_SANDBOX_TIER.items():
            assert pep._coerce_pep_tier(pep_tier) is sandbox_tier

    def test_tier_whitelist_is_cumulative_and_networked_aliases_t2(self) -> None:
        t1 = pep.tier_whitelist("t1")
        t2 = pep.tier_whitelist("t2")
        t3 = pep.tier_whitelist("t3")
        assert t1 < t2 < t3
        assert pep.tier_whitelist("networked") == t2

    def test_unknown_or_empty_tier_falls_back_to_t1_whitelist(self) -> None:
        assert pep.tier_whitelist("") == pep.tier_whitelist("t1")
        assert pep.tier_whitelist("not-real") == pep.tier_whitelist("t1")

    def test_admitted_guild_inherits_existing_tier_whitelist(self) -> None:
        assert pep.guild_tier_whitelist("backend", "t2") == pep.tier_whitelist("t2")
        assert pep.guild_tier_whitelist("bsp", "t1") == pep.tier_whitelist("t1")

    def test_inadmissible_or_unknown_guild_inherits_empty_whitelist(self) -> None:
        assert pep.guild_tier_whitelist("backend", "t1") == frozenset()
        assert pep.guild_tier_whitelist("not_a_guild", "t2") == frozenset()

    def test_classify_denies_inadmissible_guild_before_tool_whitelist(self) -> None:
        action, rule, reason, scope = pep.classify(
            "read_file",
            {"path": "src/main.c"},
            "t1",
            guild_id="backend",
        )
        assert action is pep.PepAction.deny
        assert rule == "guild_tier_inadmissible"
        assert "backend" in reason
        assert "T1" in reason
        assert scope == "local"

    def test_classify_denies_unknown_guild_id(self) -> None:
        action, rule, reason, scope = pep.classify(
            "read_file",
            {"path": "src/main.c"},
            "t2",
            guild_id="not_a_guild",
        )
        assert action is pep.PepAction.deny
        assert rule == "guild_unknown"
        assert "not_a_guild" in reason
        assert scope == "local"

    def test_classify_allows_admitted_guild_through_tier_rules(self) -> None:
        action, rule, _reason, scope = pep.classify(
            "git_push",
            {"remote": "origin"},
            "t2",
            guild_id="backend",
        )
        assert action is pep.PepAction.auto_allow
        assert rule == "tier_whitelist"
        assert scope == "local"

    def test_guild_policy_matrix_exposes_all_guilds_and_pep_tiers(self) -> None:
        matrix = pep.guild_policy_matrix()
        assert set(matrix) == {guild.value for guild in Guild}
        for guild in Guild:
            assert set(matrix[guild.value]) == {"t1", "t2", "t3"}
            for pep_tier, tools in matrix[guild.value].items():
                sandbox_tier = PEP_TO_SANDBOX_TIER[pep_tier]
                if sandbox_tier in admitted_tiers(guild):
                    assert tools == sorted(pep.tier_whitelist(pep_tier))
                else:
                    assert tools == []
