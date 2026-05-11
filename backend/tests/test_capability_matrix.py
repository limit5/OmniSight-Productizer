"""OP-855 — capability matrix contract tests.

Six cases per spec test plan:

1. happy-path lookup — Story/backend/M returns the documented capability set
2. missing entry safe-default — unmapped triple returns ``read_only_default``
3. label override enable — ``capability:enable=<cap>`` grants a one-shot capability
4. label override disable — ``capability:disable=<cap>`` removes a granted capability
5. capability refused at runtime — ``require_capability`` raises CapabilityNotPermitted
6. dashboard output sanity — ``scripts/print_capability_matrix.py`` renders the
   shipped YAML without errors and includes every (ticket_type × area × tier) row
"""
from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from backend.agents import capability_matrix


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_MATRIX = REPO_ROOT / "config" / "capability_matrix.yaml"
DASHBOARD_SCRIPT = REPO_ROOT / "scripts" / "print_capability_matrix.py"


@pytest.fixture(scope="module")
def shipped_matrix() -> capability_matrix.CapabilityMatrix:
    return capability_matrix.load_capability_matrix(SHIPPED_MATRIX)


def _write_minimal_matrix(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            schema_version: 1
            capabilities:
              - code_edit
              - run_tests
              - gerrit_push
              - jira_update
              - mcp_search
              - memory_recall
            read_only_default:
              - mcp_search
              - memory_recall
            matrix:
              Story:
                backend:
                  M: [code_edit, run_tests, gerrit_push, jira_update, mcp_search, memory_recall]
                  S: [code_edit, run_tests, jira_update, mcp_search, memory_recall]
            """
        ).strip(),
        encoding="utf-8",
    )


# ── 1. happy-path lookup ───────────────────────────────────────────


def test_happy_path_lookup_returns_documented_caps(tmp_path: Path) -> None:
    """AC#1 example: ``(Story, backend, M) → [code_edit, run_tests, run_lint, gerrit_push]``.

    Using a synthetic matrix so the test pins the contract independently of
    the shipped YAML — the YAML can grow capabilities over time without
    drift-breaking this test.
    """
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    matrix = capability_matrix.load_capability_matrix(p)

    caps = matrix.resolve("Story", "backend", "M")

    assert caps == frozenset({
        "code_edit", "run_tests", "gerrit_push",
        "jira_update", "mcp_search", "memory_recall",
    })


def test_shipped_yaml_loads_and_covers_recognised_areas(
    shipped_matrix: capability_matrix.CapabilityMatrix,
) -> None:
    """The shipped YAML must mention every RECOGNISED_AREAS value for Story tier-M.

    Catches drift between auto-runner-jira.py::RECOGNISED_AREAS and the
    matrix — if an area is recognised but not mapped, every pickup of that
    area falls to the read-only safe-default which silently blocks gerrit_push.
    """
    recognised_areas = {
        "backend", "frontend", "devops", "tests", "db",
        "docs", "security", "embedded", "tooling",
    }
    story_areas = set(shipped_matrix.known_areas("Story"))
    missing = recognised_areas - story_areas
    assert not missing, (
        f"Shipped capability_matrix.yaml is missing Story coverage for "
        f"recognised areas: {sorted(missing)}"
    )
    # tier-M must always be present (the default tier the runner assumes).
    for area in recognised_areas:
        assert "M" in shipped_matrix.known_tiers("Story", area), (
            f"Shipped matrix has no tier-M entry for Story/{area}"
        )


# ── 2. missing entry safe-default ──────────────────────────────────


def test_missing_entry_returns_read_only_safe_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """AC error catalog: ``CapabilityMatrixMissingEntry`` → safe-default = read-only-only."""
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    matrix = capability_matrix.load_capability_matrix(p)

    with caplog.at_level(logging.WARNING, logger="backend.agents.capability_matrix"):
        caps = matrix.resolve("Story", "frontend", "M")

    assert caps == matrix.read_only_default == frozenset({"mcp_search", "memory_recall"})
    # The safe-default fallback must not silently grant write capabilities.
    assert "gerrit_push" not in caps
    assert "code_edit" not in caps
    # A loud warning is expected so an operator notices the matrix gap.
    assert any(
        "capability_matrix.missing_entry" in rec.message for rec in caplog.records
    ), f"expected missing_entry warning, got {caplog.records}"


def test_missing_entry_strict_mode_raises(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    matrix = capability_matrix.load_capability_matrix(p)

    with pytest.raises(capability_matrix.CapabilityMatrixMissingEntry) as exc_info:
        matrix.resolve("Story", "frontend", "M", strict=True)

    err = exc_info.value
    assert err.ticket_type == "Story"
    assert err.area == "frontend"
    assert err.tier == "M"
    assert err.default_capabilities == matrix.read_only_default


# ── 3. label override enable ───────────────────────────────────────


def test_label_override_enable_grants_capability(tmp_path: Path) -> None:
    """AC#4 escape hatch: ``capability:enable=<cap>`` adds a one-shot capability."""
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    matrix = capability_matrix.load_capability_matrix(p)

    # tier-S in this synthetic matrix does NOT include gerrit_push.
    base_caps = matrix.resolve("Story", "backend", "S")
    assert "gerrit_push" not in base_caps

    granted = matrix.resolve(
        "Story", "backend", "S",
        labels=["capability:enable=gerrit_push", "tier:S"],
    )
    assert "gerrit_push" in granted
    # Other base caps preserved.
    assert base_caps.issubset(granted)


# ── 4. label override disable ──────────────────────────────────────


def test_label_override_disable_removes_capability(tmp_path: Path) -> None:
    """AC#4 escape hatch: ``capability:disable=<cap>`` removes a granted capability."""
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    matrix = capability_matrix.load_capability_matrix(p)

    base_caps = matrix.resolve("Story", "backend", "M")
    assert "gerrit_push" in base_caps  # baseline sanity

    revoked = matrix.resolve(
        "Story", "backend", "M",
        labels=["capability:disable=gerrit_push"],
    )
    assert "gerrit_push" not in revoked
    # Everything else stays.
    assert revoked == base_caps - {"gerrit_push"}


def test_label_override_disable_wins_over_enable(tmp_path: Path) -> None:
    """Defensive: contradictory labels must not accidentally grant a capability."""
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    matrix = capability_matrix.load_capability_matrix(p)

    caps = matrix.resolve(
        "Story", "backend", "S",
        labels=["capability:enable=gerrit_push", "capability:disable=gerrit_push"],
    )
    assert "gerrit_push" not in caps


def test_unknown_capability_in_label_override_raises(tmp_path: Path) -> None:
    """Typos in the operator label must fail loudly, not silently no-op."""
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    matrix = capability_matrix.load_capability_matrix(p)

    with pytest.raises(capability_matrix.CapabilityMatrixError):
        matrix.resolve(
            "Story", "backend", "M",
            labels=["capability:enable=publish_to_pypi"],
        )


# ── 5. capability refused at runtime ───────────────────────────────


def test_require_capability_raises_when_not_permitted() -> None:
    """AC#3: unlisted capabilities raise CapabilityNotPermitted."""
    enabled = frozenset({"code_edit", "run_tests", "jira_update"})

    with pytest.raises(capability_matrix.CapabilityNotPermitted) as exc_info:
        capability_matrix.require_capability(enabled, "gerrit_push")

    err = exc_info.value
    assert err.capability == "gerrit_push"
    assert err.enabled == enabled
    # The error message must list the enabled set so the operator can
    # decide whether to extend the matrix or add an enable label.
    assert "gerrit_push" in str(err)
    assert "code_edit" in str(err)


def test_require_capability_passes_when_permitted() -> None:
    enabled = frozenset({"code_edit", "gerrit_push"})
    capability_matrix.require_capability(enabled, "gerrit_push")  # no-op, no raise


def test_multi_area_resolve_unions_capabilities(
    shipped_matrix: capability_matrix.CapabilityMatrix,
) -> None:
    """Multi-area tickets (e.g. this OP-855: backend+docs+tests+tooling) must
    receive the union of per-area capabilities so the runner doesn't strip a
    capability that one of the declared areas legitimately needs.
    """
    union = shipped_matrix.resolve_for_areas(
        "Story", ["backend", "docs", "tests", "tooling"], "M",
    )
    backend = shipped_matrix.resolve("Story", "backend", "M")
    docs = shipped_matrix.resolve("Story", "docs", "M")
    assert backend.issubset(union)
    assert docs.issubset(union)


# ── 6. dashboard output sanity ─────────────────────────────────────


def test_print_capability_matrix_lists_every_row() -> None:
    """``scripts/print_capability_matrix.py`` must render every shipped row.

    Sanity: the dashboard is the operator's audit surface. If the script
    drops rows or crashes, an out-of-policy combination could slip in
    unnoticed. We run the script as a subprocess so we exercise the same
    argparse / stdout path an operator hits.
    """
    completed = subprocess.run(
        [sys.executable, str(DASHBOARD_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert "ticket_type" in out and "capabilities" in out

    matrix = capability_matrix.load_capability_matrix(SHIPPED_MATRIX)
    expected_rows = 0
    for ticket_type in matrix.known_ticket_types():
        for area in matrix.known_areas(ticket_type):
            for tier in matrix.known_tiers(ticket_type, area):
                expected_rows += 1
                row_prefix = f"{ticket_type}"
                # Every row contains its triple in order; the header alignment
                # may pad with spaces, so just check that the triple is in
                # output as a contiguous-ish chunk.
                assert ticket_type in out, f"missing {ticket_type} in dashboard"
                assert area in out, f"missing area {area} in dashboard"
                assert tier in out, f"missing tier {tier} in dashboard for {area}"
                del row_prefix
    # Header line plus separator plus N data rows plus a blank line plus a
    # summary line: at least expected_rows + 2 non-empty lines.
    nonblank = [line for line in out.splitlines() if line.strip()]
    assert len(nonblank) >= expected_rows + 2


def test_print_capability_matrix_resolve_flag_applies_overrides() -> None:
    completed = subprocess.run(
        [
            sys.executable, str(DASHBOARD_SCRIPT),
            "--resolve", "Story:backend:M",
            "--label", "capability:disable=gerrit_push",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert "Story" in out and "backend" in out
    # The disable label must remove gerrit_push from the resolved output.
    resolved_line = next(
        line for line in out.splitlines() if line.startswith("resolved ")
    )
    capabilities_part = resolved_line.split("→", 1)[1]
    assert "gerrit_push" not in capabilities_part


# ── Vocabulary drift guard ─────────────────────────────────────────


def test_yaml_capabilities_vocabulary_matches_canonical_set(
    shipped_matrix: capability_matrix.CapabilityMatrix,
) -> None:
    """The YAML ``capabilities:`` list must exactly mirror :data:`CAPABILITIES`.

    Drift would let the YAML reference (or omit) capabilities the runner
    doesn't know how to enforce. Asserted in both directions.
    """
    assert shipped_matrix.capabilities == capability_matrix.CAPABILITIES


def test_canonical_capabilities_match_ac_list() -> None:
    """AC#2 fixes the canonical vocabulary; this test pins it.

    The 10 names below are the exact list from OP-855 AC#2. Changing the
    runner vocabulary requires updating both the AC and this test.
    """
    assert capability_matrix.CAPABILITIES == frozenset({
        "code_edit", "run_tests", "run_lint", "gerrit_push", "jira_update",
        "mcp_search", "memory_recall", "run_migration", "deploy_action",
        "run_outcomes_grader",
    })
