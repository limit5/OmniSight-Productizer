"""RPG.W3.2 -- contract tests for ``backend/agents/style_fingerprint.py``."""

from __future__ import annotations

import pytest

from backend.agents.style_fingerprint import (
    STYLE_FINGERPRINT_HEX_LENGTH,
    TaskStyleSignals,
    compute_style_fingerprint,
    compute_style_fingerprint_from_rows,
)


def _sample(
    commit_style: str,
    test_pattern: str,
    refactor_tendency: str,
) -> TaskStyleSignals:
    return TaskStyleSignals(
        commit_style=commit_style,
        test_pattern=test_pattern,
        refactor_tendency=refactor_tendency,
    )


def test_compute_style_fingerprint_returns_sha256_hex() -> None:
    fp = compute_style_fingerprint(
        (
            _sample(
                "ticket-prefixed descriptive subject",
                "focused pytest module",
                "surgical edits only",
            ),
        )
    )

    assert len(fp) == STYLE_FINGERPRINT_HEX_LENGTH
    assert all(ch in "0123456789abcdef" for ch in fp)


def test_compute_style_fingerprint_normalizes_axis_text() -> None:
    left = compute_style_fingerprint(
        (
            _sample(
                "Ticket-Prefixed   Descriptive Subject",
                "Focused\nPytest\tModule",
                "Surgical Edits Only",
            ),
        )
    )
    right = compute_style_fingerprint(
        (
            _sample(
                "ticket-prefixed descriptive subject",
                "focused pytest module",
                "surgical edits only",
            ),
        )
    )

    assert left == right


def test_compute_style_fingerprint_uses_newest_last_n_tasks() -> None:
    tasks = (
        _sample("old commit", "old tests", "old refactor"),
        _sample("middle commit", "middle tests", "middle refactor"),
        _sample("new commit", "new tests", "new refactor"),
    )

    assert compute_style_fingerprint(tasks, last_n=2) == compute_style_fingerprint(
        tasks[1:],
        last_n=2,
    )
    assert compute_style_fingerprint(tasks, last_n=3) != compute_style_fingerprint(
        tasks[1:],
        last_n=2,
    )


def test_compute_style_fingerprint_preserves_task_order() -> None:
    first = _sample("commit a", "tests a", "refactor a")
    second = _sample("commit b", "tests b", "refactor b")

    assert compute_style_fingerprint((first, second)) != compute_style_fingerprint(
        (second, first)
    )


def test_compute_style_fingerprint_from_rows_matches_dataclass_input() -> None:
    rows = (
        {
            "commit_style": "body trailers",
            "test_pattern": "single focused test",
            "refactor_tendency": "no adjacent cleanup",
        },
    )
    samples = (
        _sample("body trailers", "single focused test", "no adjacent cleanup"),
    )

    assert compute_style_fingerprint_from_rows(rows) == compute_style_fingerprint(samples)


def test_compute_style_fingerprint_empty_history_is_blank() -> None:
    assert compute_style_fingerprint(()) == ""
    assert compute_style_fingerprint_from_rows(()) == ""


@pytest.mark.parametrize("last_n", [0, -1])
def test_compute_style_fingerprint_rejects_non_positive_window(last_n: int) -> None:
    with pytest.raises(ValueError):
        compute_style_fingerprint(
            (_sample("commit", "tests", "refactor"),),
            last_n=last_n,
        )


def test_task_style_signals_require_all_axes() -> None:
    with pytest.raises(ValueError):
        TaskStyleSignals(
            commit_style="commit",
            test_pattern="",
            refactor_tendency="refactor",
        )
