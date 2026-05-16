"""RPG.W11.2 — drift guard for ``backend/agents/skill_matrix.py``.

ADR-0008 §"Skill leveling (W12)" names ``backend/agents/skill_matrix.yaml``
as the *sole owner* of the RPG ``skill_id`` namespace. Anywhere a
``skill_id:`` key appears in a YAML file under ``configs/`` must already
be declared by the canonical matrix — otherwise routing, leveling, and
the Character Card branch-picker silently fall off the matrix.

The runtime guard is :func:`backend.agents.skill_matrix.assert_skill_id_space_within_matrix`
(also invoked at module import time). This file pins the contract
behind that helper so a future refactor cannot accidentally relax the
subset check.

The four invariants verified here:

1. ``discover_declared_skill_ids()`` ⊆ ``canonical_skill_ids()`` for the
   real repo state (the live drift guard).
2. ``missing_skill_ids_from_matrix`` returns exactly the off-matrix
   identifiers, deduplicated and sorted, when fed a synthetic input.
3. ``assert_skill_id_space_within_matrix`` raises
   :class:`SkillMatrixDriftError` and names the offending ``skill_id``
   in the message (operator must see *which* slug drifted).
4. A custom ``scan_roots`` containing an off-matrix YAML row trips the
   guard end-to-end (the discover→assert pipe, not just the assert tail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.skill_matrix import (
    DEFAULT_SKILL_ID_SCAN_ROOTS,
    SKILL_MATRIX_PATH,
    SkillMatrixDriftError,
    assert_skill_id_space_within_matrix,
    canonical_skill_ids,
    discover_declared_skill_ids,
    missing_skill_ids_from_matrix,
)


# ── 1. Live repo invariant ──────────────────────────────────────────


def test_canonical_matrix_path_exists_and_is_non_empty() -> None:
    """The canonical matrix YAML must be present — the drift guard is
    vacuous if the source of truth is missing.
    """

    assert SKILL_MATRIX_PATH.exists(), (
        f"Canonical RPG skill matrix missing: {SKILL_MATRIX_PATH}. "
        "The drift guard cannot enforce skill_id ⊆ matrix without it."
    )
    canonical = canonical_skill_ids()
    assert canonical, (
        "canonical_skill_ids() is empty — the drift guard would pass "
        "vacuously. Re-check schema_version and `skills:` mapping in "
        f"{SKILL_MATRIX_PATH.name}."
    )


def test_default_scan_roots_cover_configs_tree() -> None:
    """The default discovery roots must include ``configs/`` — that is
    where per-skill YAML lives (e.g. ``configs/skills/<skill>/skill.yaml``,
    ``tasks.yaml``, ``tests/test_definitions.yaml``).
    """

    assert any(
        Path(root).name == "configs" for root in DEFAULT_SKILL_ID_SCAN_ROOTS
    ), (
        "DEFAULT_SKILL_ID_SCAN_ROOTS must include the repo `configs/` "
        f"tree; got {DEFAULT_SKILL_ID_SCAN_ROOTS!r}."
    )


def test_repo_skill_ids_are_subset_of_canonical_matrix() -> None:
    """RPG.W11.2 invariant: every ``skill_id`` discovered under the
    default scan roots is declared by the canonical matrix YAML.

    A failure here means a new ``skill_id:`` row was added under
    ``configs/`` without being mirrored into
    ``backend/agents/skill_matrix.yaml``. The fix is to add the row to
    the matrix, not to relax this guard — the matrix is the namespace.
    """

    discovered = discover_declared_skill_ids()
    canonical = canonical_skill_ids()
    extra = discovered - canonical
    assert not extra, (
        "RPG skill_id space drifted outside the canonical matrix. "
        f"skill_ids present under configs/ but missing from "
        f"{SKILL_MATRIX_PATH.name}: {sorted(extra)!r}. "
        f"Canonical matrix declares: {sorted(canonical)!r}."
    )


def test_live_assert_skill_id_space_within_matrix_does_not_raise() -> None:
    """The live drift guard must currently pass.

    ``backend/agents/skill_matrix.py`` calls this assertion at module
    import time; if this test fails, importing the module would also
    raise and downstream callers (routing / skill leveling / Character
    Card) would be wedged. This test makes the invariant addressable as
    a discrete contract rather than only as an import-time side effect.
    """

    assert_skill_id_space_within_matrix()


# ── 2. missing_skill_ids_from_matrix returns the right shape ────────


def test_missing_skill_ids_returns_empty_when_subset() -> None:
    canonical = canonical_skill_ids()
    # Take a canonical sample — must report nothing missing.
    sample = next(iter(canonical))
    assert missing_skill_ids_from_matrix([sample]) == ()


def test_missing_skill_ids_reports_off_matrix_ids_sorted() -> None:
    """The helper returns missing IDs sorted ascending so operator
    error messages are deterministic between runs.
    """

    canonical = canonical_skill_ids()
    in_matrix = next(iter(canonical))
    missing = missing_skill_ids_from_matrix(
        [in_matrix, "zeta_skill", "alpha_skill", "alpha_skill"]
    )
    assert missing == ("alpha_skill", "zeta_skill")


def test_missing_skill_ids_strips_and_validates_input() -> None:
    """Inputs are cleaned via the same regex the matrix uses, so a
    stray whitespace-padded entry does not slip past the guard.
    """

    canonical = canonical_skill_ids()
    in_matrix = next(iter(canonical))
    # Whitespace must be stripped — same canonical id should match.
    assert missing_skill_ids_from_matrix([f"  {in_matrix}  "]) == ()


# ── 3. assert_skill_id_space_within_matrix raises on drift ──────────


def test_assert_skill_id_space_raises_for_off_matrix_id() -> None:
    """The guard must raise :class:`SkillMatrixDriftError` (a typed
    :class:`RuntimeError`) when fed an explicit off-matrix slug.
    """

    with pytest.raises(SkillMatrixDriftError):
        assert_skill_id_space_within_matrix(["ghost_skill"])


def test_assert_skill_id_space_error_message_names_offender() -> None:
    """Operator-facing message must include the offending ``skill_id``
    so the fix target is unambiguous — vague "drift detected" messages
    are what RPG.W11.2 is replacing.
    """

    with pytest.raises(SkillMatrixDriftError, match="ghost_skill"):
        assert_skill_id_space_within_matrix(["ghost_skill"])


def test_assert_skill_id_space_accepts_subset_of_canonical() -> None:
    """The empty-missing path is the steady-state contract — calling
    with a canonical subset must be a no-op (no exception).
    """

    canonical = canonical_skill_ids()
    assert_skill_id_space_within_matrix(list(canonical)[:1])


# ── 4. End-to-end: discover → assert with a synthetic scan root ─────


def test_discover_assert_pipeline_trips_on_off_matrix_yaml(
    tmp_path: Path,
) -> None:
    """Drop a YAML row with an off-matrix ``skill_id`` into a temp
    scan root and verify the full discover→assert pipeline raises.

    This is the realistic failure mode: someone adds a new
    ``configs/skills/<new>/skill.yaml`` without touching the matrix.
    The guard catches it without any code path in between.
    """

    rogue = tmp_path / "skill.yaml"
    rogue.write_text("skill_id: rogue_new_skill\n", encoding="utf-8")

    discovered = discover_declared_skill_ids(scan_roots=(tmp_path,))
    assert "rogue_new_skill" in discovered

    with pytest.raises(SkillMatrixDriftError, match="rogue_new_skill"):
        assert_skill_id_space_within_matrix(discovered)


def test_discover_assert_pipeline_passes_when_scan_root_subset(
    tmp_path: Path,
) -> None:
    """Companion to the above: a temp scan root whose ``skill_id``
    rows are all canonical must pass cleanly. Locks in the
    "drift guard does not produce false positives" half of the contract.
    """

    canonical = canonical_skill_ids()
    chosen = next(iter(canonical))
    legit = tmp_path / "skill.yaml"
    legit.write_text(f"skill_id: {chosen}\n", encoding="utf-8")

    discovered = discover_declared_skill_ids(scan_roots=(tmp_path,))
    assert discovered == frozenset({chosen})
    assert_skill_id_space_within_matrix(discovered)


def test_discover_assert_pipeline_handles_nested_yaml(
    tmp_path: Path,
) -> None:
    """``discover_declared_skill_ids`` recurses through nested mapping /
    list YAML structures — verify a deeply nested off-matrix slug still
    trips the guard. This mirrors ``configs/skills/<skill>/tasks.yaml``
    where ``skill_id`` lives inside a list of task entries.
    """

    nested = tmp_path / "tasks.yaml"
    nested.write_text(
        "tasks:\n"
        "  - name: example\n"
        "    skill_id: deeply_nested_rogue\n",
        encoding="utf-8",
    )

    discovered = discover_declared_skill_ids(scan_roots=(tmp_path,))
    assert "deeply_nested_rogue" in discovered
    with pytest.raises(SkillMatrixDriftError, match="deeply_nested_rogue"):
        assert_skill_id_space_within_matrix(discovered)
