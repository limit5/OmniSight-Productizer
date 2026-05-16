"""RPG.W3.2 -- contract tests for ``backend/agents/style_fingerprint.py``."""

from __future__ import annotations

import pytest

from backend.agents.style_fingerprint import (
    DEFAULT_STYLE_WINDOW,
    STYLE_FINGERPRINT_HEX_LENGTH,
    STYLE_FINGERPRINT_VERSION,
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


# --- OP-1347: additional edge-case coverage ----------------------------------


def test_default_style_window_is_twenty() -> None:
    """DEFAULT_STYLE_WINDOW pins the ADR-0008 "last N tasks" contract; a
    silent bump would change every Character Card's fingerprint at the
    next cron tick, so the constant is checked explicitly."""
    assert DEFAULT_STYLE_WINDOW == 20


def test_style_fingerprint_version_constant_is_pinned() -> None:
    """The version string is mixed into the hash payload; bumping it
    intentionally invalidates every stored fingerprint. The constant is
    asserted so an accidental edit is caught at test time, not at the
    next daily sweep."""
    assert STYLE_FINGERPRINT_VERSION == "rpg-style-v1"


def test_compute_style_fingerprint_uses_default_window_when_unspecified() -> None:
    """Omitting ``last_n`` must behave as ``last_n=DEFAULT_STYLE_WINDOW``."""
    tasks = tuple(
        _sample(f"commit {i}", f"tests {i}", f"refactor {i}")
        for i in range(DEFAULT_STYLE_WINDOW + 5)
    )

    assert compute_style_fingerprint(tasks) == compute_style_fingerprint(
        tasks,
        last_n=DEFAULT_STYLE_WINDOW,
    )


def test_compute_style_fingerprint_window_larger_than_history_hashes_all() -> None:
    """``last_n`` larger than ``len(tasks)`` selects every available task
    rather than raising -- callers with thin histories still get a hash."""
    tasks = (
        _sample("commit a", "tests a", "refactor a"),
        _sample("commit b", "tests b", "refactor b"),
    )

    big_window = compute_style_fingerprint(tasks, last_n=100)
    full_window = compute_style_fingerprint(tasks, last_n=len(tasks))

    assert big_window == full_window
    assert len(big_window) == STYLE_FINGERPRINT_HEX_LENGTH


def test_compute_style_fingerprint_is_deterministic_across_calls() -> None:
    """Same inputs => same output, every call. Guards against any future
    refactor that introduces nondeterminism (e.g. dict iteration order
    leaking through ``json.dumps``)."""
    tasks = (
        _sample("commit a", "tests a", "refactor a"),
        _sample("commit b", "tests b", "refactor b"),
    )

    first = compute_style_fingerprint(tasks)
    second = compute_style_fingerprint(tasks)
    third = compute_style_fingerprint(tasks)

    assert first == second == third


@pytest.mark.parametrize(
    "axis",
    ["commit_style", "test_pattern", "refactor_tendency"],
)
def test_compute_style_fingerprint_is_sensitive_to_each_axis(axis: str) -> None:
    """Flipping a single axis must change the hash; otherwise an axis is
    silently dropped from the payload."""
    base_kwargs = {
        "commit_style": "baseline commit",
        "test_pattern": "baseline tests",
        "refactor_tendency": "baseline refactor",
    }
    mutated_kwargs = dict(base_kwargs, **{axis: "mutated"})

    base = compute_style_fingerprint((TaskStyleSignals(**base_kwargs),))
    mutated = compute_style_fingerprint((TaskStyleSignals(**mutated_kwargs),))

    assert base != mutated


@pytest.mark.parametrize("bad_window", [True, False])
def test_compute_style_fingerprint_rejects_boolean_window(bad_window: bool) -> None:
    """``bool`` is a subclass of ``int`` in Python, so the validator
    explicitly rejects it -- otherwise ``last_n=True`` would silently
    behave as ``last_n=1``."""
    with pytest.raises(TypeError):
        compute_style_fingerprint(
            (_sample("commit", "tests", "refactor"),),
            last_n=bad_window,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad_window", [1.0, "5", None])
def test_compute_style_fingerprint_rejects_non_int_window(bad_window: object) -> None:
    """Non-int ``last_n`` values raise ``TypeError`` rather than coercing."""
    with pytest.raises(TypeError):
        compute_style_fingerprint(
            (_sample("commit", "tests", "refactor"),),
            last_n=bad_window,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "axis",
    ["commit_style", "test_pattern", "refactor_tendency"],
)
def test_task_style_signals_reject_non_string_axis(axis: str) -> None:
    """Non-string axis values raise ``TypeError`` so callers passing the
    wrong column type get a clear diagnostic rather than a hash over
    ``repr(...)``."""
    kwargs = {
        "commit_style": "commit",
        "test_pattern": "tests",
        "refactor_tendency": "refactor",
    }
    kwargs[axis] = 123  # type: ignore[assignment]

    with pytest.raises(TypeError):
        TaskStyleSignals(**kwargs)


def test_compute_style_fingerprint_from_rows_raises_on_missing_axis() -> None:
    """``from_mapping`` defaults missing keys to ``""``; validation then
    surfaces the gap as a ``ValueError`` instead of hashing blanks."""
    with pytest.raises(ValueError):
        compute_style_fingerprint_from_rows(
            ({"commit_style": "c", "test_pattern": "t"},),
        )


def test_compute_style_fingerprint_from_rows_coerces_non_string_values() -> None:
    """``from_mapping`` runs ``str()`` on incoming values so callers can
    pass enum/int columns without pre-formatting."""
    coerced = compute_style_fingerprint_from_rows(
        (
            {
                "commit_style": 1,
                "test_pattern": 2,
                "refactor_tendency": 3,
            },
        ),
    )
    explicit = compute_style_fingerprint(
        (_sample("1", "2", "3"),),
    )

    assert coerced == explicit
