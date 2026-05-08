"""OP-803 (G1) — schema gate for ``configs/governance/tier-paths.yaml``.

ADR-0005's path -> tier map is the single source of truth for the
G2 backend classifier, the G3 Gerrit hook, and the G4 audit log. A
silent corruption here (e.g. a contributor accidentally drops the
Tier L ``backend/security/**`` rule) would let security changes slip
through Tier S — exactly the misclassification ADR-0005's 4-layer
protection is meant to prevent.

This test sits in the backend-tests shard so the contract is locked
on the same gate that gates merge:

  1. The YAML file exists and parses.
  2. It validates against the JSON Schema in
     ``configs/governance/__schema__/tier-paths.schema.json``.
  3. All four tiers (s, m, l, x) are present.
  4. Each whitelist / force_upgrade list is non-empty.
  5. The ADR-0005 anchor globs are still in place (so a refactor that
     accidentally removes ``backend/security/**`` lights up red here
     instead of in a security incident postmortem).

Module-global state audit (per implement_phase_step.md Step 1):
the test reads the YAML and the schema once per fixture using
``yaml.safe_load`` / ``json.loads``. No module-level cache, no
singleton, no DB. Every test re-derives state from the on-disk file;
pytest workers share nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
GOVERNANCE_DIR = PROJECT_ROOT / "configs" / "governance"
TIER_PATHS_YAML = GOVERNANCE_DIR / "tier-paths.yaml"
TIER_PATHS_SCHEMA = GOVERNANCE_DIR / "__schema__" / "tier-paths.schema.json"


@pytest.fixture(scope="module")
def tier_paths_doc() -> dict[str, Any]:
    with TIER_PATHS_YAML.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def tier_paths_schema() -> dict[str, Any]:
    with TIER_PATHS_SCHEMA.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ─── Group 1: file presence + parseability ───────────────────────────────


class TestFilesExist:
    def test_yaml_file_present(self) -> None:
        assert TIER_PATHS_YAML.exists(), (
            f"tier-paths.yaml missing at {TIER_PATHS_YAML} — "
            f"G2/G3/G4 cannot resolve any change's tier without it"
        )

    def test_schema_file_present(self) -> None:
        assert TIER_PATHS_SCHEMA.exists(), (
            f"tier-paths.schema.json missing at {TIER_PATHS_SCHEMA} — "
            f"YAML cannot be validated without the schema"
        )

    def test_yaml_parses(self, tier_paths_doc) -> None:
        assert isinstance(tier_paths_doc, dict), (
            "tier-paths.yaml top-level must be a mapping"
        )

    def test_schema_parses(self, tier_paths_schema) -> None:
        assert isinstance(tier_paths_schema, dict), (
            "tier-paths.schema.json top-level must be a JSON object"
        )


# ─── Group 2: schema validation ──────────────────────────────────────────


class TestSchemaValidation:
    def test_schema_is_valid_draft_2020_12(
        self, tier_paths_schema
    ) -> None:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(tier_paths_schema)

    def test_schema_advertises_2020_12_dialect(
        self, tier_paths_schema
    ) -> None:
        assert (
            tier_paths_schema.get("$schema")
            == "https://json-schema.org/draft/2020-12/schema"
        )

    def test_yaml_validates_against_schema(
        self, tier_paths_doc, tier_paths_schema
    ) -> None:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(tier_paths_schema)
        violations = sorted(
            validator.iter_errors(tier_paths_doc),
            key=lambda e: tuple(str(p) for p in e.absolute_path),
        )
        rendered = [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}"
            f": {e.message}"
            for e in violations
        ]
        assert not violations, (
            "tier-paths.yaml fails schema validation:\n"
            + "\n".join(rendered)
        )


# ─── Group 3: all four tiers present + non-empty (AC #2) ─────────────────


class TestAllFourTiersPresent:
    """ADR-0005 contract: Tier S/M/L/X — each must be defined, and
    each whitelist / force-upgrade list must be non-empty.

    Reason: the G2 classifier defaults to Tier M when no force-upgrade
    matches and the S whitelist doesn't cover all paths. If an L or X
    list silently empties, every change in that band would silently
    drop to M — masking auth/security/deploy work behind a softer
    review gate. This is the failure mode this group exists to lock.
    """

    def test_top_level_tiers_key_present(self, tier_paths_doc) -> None:
        assert "tiers" in tier_paths_doc, (
            "tier-paths.yaml missing top-level 'tiers' key"
        )

    def test_all_four_tier_keys_present(self, tier_paths_doc) -> None:
        tiers = tier_paths_doc["tiers"]
        assert set(tiers.keys()) == {"s", "m", "l", "x"}, (
            f"tier set drift: expected {{s,m,l,x}}, got {set(tiers.keys())}"
        )

    def test_tier_s_whitelist_globs_non_empty(
        self, tier_paths_doc
    ) -> None:
        globs = tier_paths_doc["tiers"]["s"]["whitelist_globs"]
        assert isinstance(globs, list)
        assert len(globs) >= 1, (
            "tiers.s.whitelist_globs is empty — Tier S would be "
            "unreachable, forcing all changes to Tier M+"
        )

    def test_tier_m_marked_default(self, tier_paths_doc) -> None:
        # Tier M carries no globs by design — it's the implicit
        # fallback. The schema requires the explicit `default: true`
        # marker so a refactor can't accidentally drop the section
        # without a loud schema failure.
        m = tier_paths_doc["tiers"]["m"]
        assert m.get("default") is True, (
            "tiers.m.default must be `true` (Tier M is the implicit "
            "fallback for un-classified production code)"
        )

    def test_tier_l_force_upgrade_globs_non_empty(
        self, tier_paths_doc
    ) -> None:
        globs = tier_paths_doc["tiers"]["l"]["force_upgrade_globs"]
        assert isinstance(globs, list)
        assert len(globs) >= 1, (
            "tiers.l.force_upgrade_globs is empty — security / auth / "
            "alembic changes would silently drop to Tier M"
        )

    def test_tier_x_force_upgrade_globs_non_empty(
        self, tier_paths_doc
    ) -> None:
        globs = tier_paths_doc["tiers"]["x"]["force_upgrade_globs"]
        assert isinstance(globs, list)
        assert len(globs) >= 1, (
            "tiers.x.force_upgrade_globs is empty — deploy / CI / "
            "dependency manifest changes would silently drop to Tier M"
        )


# ─── Group 4: ADR-0005 anchor globs lock ─────────────────────────────────


class TestAdr0005AnchorGlobs:
    """Lock the load-bearing anchor globs from the ADR / ticket spec.

    These are the entries whose silent removal would itself be a
    security incident — e.g. dropping ``backend/security/**`` would
    let security work merge with an AI-only review. The full list is
    not asserted (operators may add to it freely without breaking
    AC) — only the entries that ADR-0005 + OP-803 explicitly name as
    minimum-required.
    """

    def test_tier_s_anchor_whitelist_globs(self, tier_paths_doc) -> None:
        globs = set(tier_paths_doc["tiers"]["s"]["whitelist_globs"])
        for required in (
            "backend/tests/**",
            "messages/*.json",
            "openapi/*.generated.json",
        ):
            assert required in globs, (
                f"Tier S whitelist missing ADR-0005 anchor glob "
                f"{required!r} — current set: {sorted(globs)}"
            )

    def test_tier_s_whitelists_markdown(self, tier_paths_doc) -> None:
        # ADR-0005 names ``*.md``; the file may use either ``*.md``
        # alone (root-only) or pair it with ``**/*.md`` (recursive).
        # Either form satisfies the ADR; we just require markdown be
        # whitelisted somewhere.
        globs = set(tier_paths_doc["tiers"]["s"]["whitelist_globs"])
        assert "*.md" in globs or "**/*.md" in globs, (
            "Tier S whitelist must cover markdown files "
            "(ADR-0005 §3 Tier S whitelist) — neither '*.md' nor "
            "'**/*.md' is present"
        )

    def test_tier_l_anchor_force_upgrade_globs(
        self, tier_paths_doc
    ) -> None:
        globs = set(tier_paths_doc["tiers"]["l"]["force_upgrade_globs"])
        for required in (
            "backend/security/**",
            "backend/auth*.py",
            "backend/alembic/versions/*.py",
        ):
            assert required in globs, (
                f"Tier L force-upgrade missing ADR-0005 anchor glob "
                f"{required!r} — current set: {sorted(globs)}. "
                f"Removing this rule would let security/auth/schema "
                f"work merge under a softer tier."
            )

    def test_tier_x_anchor_force_upgrade_globs(
        self, tier_paths_doc
    ) -> None:
        globs = set(tier_paths_doc["tiers"]["x"]["force_upgrade_globs"])
        for required in (
            "deploy/**",
            "scripts/deploy-*.sh",
            ".github/workflows/**",
            ".gitlab-ci.yml",
            ".gerrit/**",
            "requirements*.txt",
            "package.json",
            "pyproject.toml",
        ):
            assert required in globs, (
                f"Tier X force-upgrade missing OP-803 anchor glob "
                f"{required!r} — current set: {sorted(globs)}. "
                f"Removing this rule would let deploy/CI/dep changes "
                f"merge under a softer tier."
            )


# ─── Group 5: glob hygiene ───────────────────────────────────────────────


class TestGlobHygiene:
    """Catch glob typos that the schema's regex can't (it only checks
    non-empty + no leading slash). These are pure-syntactic guards;
    no assumption about which paths are sensitive."""

    def test_no_glob_uses_backslash(self, tier_paths_doc) -> None:
        # Globs are POSIX-form by contract. A backslash usually
        # signals a Windows-path slip.
        diffs: list[str] = []
        for tier_key in ("s", "l", "x"):
            list_key = (
                "whitelist_globs"
                if tier_key == "s"
                else "force_upgrade_globs"
            )
            for glob in tier_paths_doc["tiers"][tier_key][list_key]:
                if "\\" in glob:
                    diffs.append(f"tiers.{tier_key}.{list_key}: {glob!r}")
        assert not diffs, (
            "globs must use forward slashes only:\n" + "\n".join(diffs)
        )

    def test_no_glob_uses_leading_slash(self, tier_paths_doc) -> None:
        diffs: list[str] = []
        for tier_key in ("s", "l", "x"):
            list_key = (
                "whitelist_globs"
                if tier_key == "s"
                else "force_upgrade_globs"
            )
            for glob in tier_paths_doc["tiers"][tier_key][list_key]:
                if glob.startswith("/"):
                    diffs.append(f"tiers.{tier_key}.{list_key}: {glob!r}")
        assert not diffs, (
            "globs must be repo-relative (no leading slash):\n"
            + "\n".join(diffs)
        )

    def test_no_duplicate_globs_within_a_list(
        self, tier_paths_doc
    ) -> None:
        # Schema enforces uniqueItems; this test is a defence-in-depth
        # mirror that pinpoints the offending list when it fires.
        diffs: list[str] = []
        for tier_key in ("s", "l", "x"):
            list_key = (
                "whitelist_globs"
                if tier_key == "s"
                else "force_upgrade_globs"
            )
            globs = tier_paths_doc["tiers"][tier_key][list_key]
            if len(set(globs)) != len(globs):
                seen: set[str] = set()
                dups: list[str] = []
                for g in globs:
                    if g in seen:
                        dups.append(g)
                    seen.add(g)
                diffs.append(
                    f"tiers.{tier_key}.{list_key} duplicates: "
                    f"{sorted(set(dups))}"
                )
        assert not diffs, "\n".join(diffs)


# ─── Group 6: schema-version pin ─────────────────────────────────────────


class TestSchemaVersionPinned:
    """Bumping schema_version is a coordinated change with the G2
    loader. Lock to 1 so the bump can't slip in unnoticed."""

    def test_schema_version_is_one(self, tier_paths_doc) -> None:
        assert tier_paths_doc["schema_version"] == 1, (
            f"schema_version must be 1 (got "
            f"{tier_paths_doc['schema_version']!r}) — bumping requires "
            f"a coordinated G2 loader update + this assertion update"
        )
