"""OP-804 (G2) - table-driven tests for the ADR-0005 tier classifier."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.governance import tier_classifier
from backend.governance.tier_classifier import (
    Tier,
    classify_tier,
    compose_with_existing_label,
    path_to_tier_reasons,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        # Tier S whitelist: backend tests.
        (["backend/tests/test_x.py"], "s"),
        (["backend/tests/unit/test_x.py"], "s"),
        (["./backend/tests/test_x.py"], "s"),
        ([r"backend\tests\test_x.py"], "s"),
        (["backend/tests/fixtures/sample.json"], "s"),
        # Tier S whitelist: docs.
        (["README.md"], "s"),
        (["docs/adr/ADR-0005-tier-authority-levels.md"], "s"),
        (["docs/sop/runbook.md"], "s"),
        (["CHANGELOG.md", "docs/release/notes.md"], "s"),
        # Tier S whitelist: locale and generated OpenAPI.
        (["messages/en.json"], "s"),
        (["messages/zh-TW.json"], "s"),
        (["openapi/client.generated.json"], "s"),
        (["backend/tests/test_x.py", "README.md"], "s"),
        # Tier L force-upgrade rules.
        (["backend/security/crypto.py"], "l"),
        (["backend/security/nested/crypto.py"], "l"),
        (["backend/auth.py"], "l"),
        (["backend/auth_helpers.py"], "l"),
        (["backend/auth_oidc.py"], "l"),
        (["backend/alembic/versions/0200_add_table.py"], "l"),
        (["backend/tests/test_x.py", "backend/auth.py"], "l"),
        (["README.md", "backend/security/crypto.py"], "l"),
        # Tier X force-upgrade rules.
        (["deploy/k8s/app.yaml"], "x"),
        (["deploy/terraform/main.tf"], "x"),
        (["scripts/deploy-prod.sh"], "x"),
        ([".github/workflows/ci.yml"], "x"),
        ([".gitlab-ci.yml"], "x"),
        ([".gerrit/project.config"], "x"),
        (["requirements.txt"], "x"),
        (["requirements-dev.txt"], "x"),
        (["package.json"], "x"),
        (["pyproject.toml"], "x"),
        (["backend/auth.py", "deploy/k8s/app.yaml"], "x"),
        (["backend/tests/test_x.py", ".github/workflows/ci.yml"], "x"),
        # Tier M fallback.
        (["backend/foo.py"], "m"),
        (["backend/routers/items.py"], "m"),
        (["scripts/check-style.sh"], "m"),
        (["openapi/schema.yaml"], "m"),
        (["messages/en.json", "backend/foo.py"], "m"),
        ([], "m"),
    ],
)
def test_classify_tier_table(paths: list[str], expected: Tier) -> None:
    assert classify_tier(paths) == expected


@pytest.mark.parametrize(
    ("current", "computed", "expected"),
    [
        ("m", "s", "m"),
        ("s", "l", "l"),
        ("l", "m", "l"),
        ("x", "s", "x"),
        ("s", "x", "x"),
        ("m", "m", "m"),
    ],
)
def test_compose_with_existing_label_is_monotonic(
    current: Tier, computed: Tier, expected: Tier
) -> None:
    assert compose_with_existing_label(current, computed) == expected


def test_classify_tier_auth_force_upgrade_wins_over_whitelist() -> None:
    assert classify_tier(["backend/tests/test_x.py", "backend/auth.py"]) == "l"


def test_path_to_tier_reasons_lists_matching_rules() -> None:
    assert path_to_tier_reasons(
        ["backend/tests/test_x.py", "backend/auth.py", "backend/foo.py"]
    ) == {
        "backend/tests/test_x.py": ["whitelist:s:backend/tests/**"],
        "backend/auth.py": ["force-upgrade:l:backend/auth*.py"],
        "backend/foo.py": ["default:m:no force-upgrade or whitelist match"],
    }


def test_path_to_tier_reasons_reports_highest_force_upgrade_candidate() -> None:
    reasons = path_to_tier_reasons(["deploy/app.yaml", "requirements.txt"])

    assert reasons["deploy/app.yaml"] == ["force-upgrade:x:deploy/**"]
    assert reasons["requirements.txt"] == ["force-upgrade:x:requirements*.txt"]


def test_load_tier_path_rules_allows_test_injected_config(tmp_path: Path) -> None:
    config_path = tmp_path / "tier-paths.yaml"
    config_path.write_text(
        "\n".join(
            [
                "schema_version: 1",
                "tiers:",
                "  s:",
                "    whitelist_globs:",
                '      - "safe/**"',
                "  m:",
                "    default: true",
                "  l:",
                "    force_upgrade_globs:",
                '      - "large/**"',
                "  x:",
                "    force_upgrade_globs:",
                '      - "extreme/**"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    rules = tier_classifier._load_tier_path_rules(config_path)

    assert rules.s_whitelist_globs == ("safe/**",)
    assert rules.l_force_upgrade_globs == ("large/**",)
    assert rules.x_force_upgrade_globs == ("extreme/**",)


def test_default_config_is_loaded_from_governance_yaml_once() -> None:
    assert tier_classifier.DEFAULT_CONFIG_PATH == (
        PROJECT_ROOT / "configs" / "governance" / "tier-paths.yaml"
    )
    assert tier_classifier._RULES.s_whitelist_globs
    assert tier_classifier._RULES.l_force_upgrade_globs
    assert tier_classifier._RULES.x_force_upgrade_globs


def test_unknown_existing_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown tier"):
        compose_with_existing_label("bad", "s")  # type: ignore[arg-type]


def test_unknown_computed_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown tier"):
        compose_with_existing_label("s", "bad")  # type: ignore[arg-type]
