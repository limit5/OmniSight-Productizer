"""SP-B-X-006 / OP-1064 — unit tests for the stale-refresh strategy picker.

Covers the three picker strategies, env-var policy parsing, the cost
estimator, and the ``extract_mutated_path`` helper that the
``run_with_tools`` loop uses to track which files have been edited.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from backend.agents import stale_refresh_strategy as srs


# ─── pick_refresh_target ─────────────────────────────────────────


def test_pick_most_edited_returns_max_count_path():
    touched = {"a.py": 1, "b.py": 5, "c.py": 3}
    assert srs.pick_refresh_target(touched, "most-edited") == "b.py"


def test_pick_most_edited_breaks_tie_with_first_max():
    # ``max`` on equal keys returns the first seen; insertion order wins.
    touched = {"a.py": 2, "b.py": 2}
    assert srs.pick_refresh_target(touched, "most-edited") == "a.py"


def test_pick_random_touched_returns_member_of_input():
    touched = {f"f{i}.py": 1 for i in range(5)}
    rng = random.Random(42)
    pick = srs.pick_refresh_target(touched, "random-touched", rng=rng)
    assert pick in touched


def test_pick_random_touched_is_deterministic_with_seeded_rng():
    touched = {"a.py": 1, "b.py": 1, "c.py": 1}
    rng1 = random.Random(123)
    rng2 = random.Random(123)
    assert srs.pick_refresh_target(touched, "random-touched", rng=rng1) == \
        srs.pick_refresh_target(touched, "random-touched", rng=rng2)


def test_pick_all_touched_round_robin_cycles_via_iteration():
    touched = {"a.py": 1, "b.py": 1, "c.py": 1}
    paths = list(touched.keys())
    picks = [
        srs.pick_refresh_target(touched, "all-touched", iteration=i)
        for i in range(7)
    ]
    assert picks == [paths[i % 3] for i in range(7)]


def test_pick_empty_returns_none():
    assert srs.pick_refresh_target({}, "most-edited") is None
    assert srs.pick_refresh_target(None, "most-edited") is None
    assert srs.pick_refresh_target([], "random-touched") is None


def test_pick_unknown_strategy_falls_back_to_most_edited():
    touched = {"a.py": 1, "b.py": 7}
    assert srs.pick_refresh_target(touched, "no-such-strategy") == "b.py"


def test_pick_accepts_sequence_input_for_all_touched():
    paths = ["x.py", "y.py", "z.py"]
    picks = [
        srs.pick_refresh_target(paths, "all-touched", iteration=i)
        for i in range(6)
    ]
    assert picks == ["x.py", "y.py", "z.py", "x.py", "y.py", "z.py"]


# ─── env-var policy config ───────────────────────────────────────


def test_get_refresh_every_n_default(monkeypatch):
    monkeypatch.delenv(srs.ENV_EVERY_N, raising=False)
    assert srs.get_refresh_every_n() == srs.DEFAULT_EVERY_N_ITER


def test_get_refresh_every_n_reads_env(monkeypatch):
    monkeypatch.setenv(srs.ENV_EVERY_N, "25")
    assert srs.get_refresh_every_n() == 25


def test_get_refresh_every_n_rejects_garbage_falls_back(monkeypatch):
    monkeypatch.setenv(srs.ENV_EVERY_N, "not-an-int")
    assert srs.get_refresh_every_n() == srs.DEFAULT_EVERY_N_ITER


def test_get_refresh_every_n_floor_of_one(monkeypatch):
    monkeypatch.setenv(srs.ENV_EVERY_N, "0")
    assert srs.get_refresh_every_n() == 1


def test_get_refresh_strategy_default(monkeypatch):
    monkeypatch.delenv(srs.ENV_STRATEGY, raising=False)
    assert srs.get_refresh_strategy() == srs.DEFAULT_STRATEGY


@pytest.mark.parametrize("strategy", srs.VALID_STRATEGIES)
def test_get_refresh_strategy_accepts_valid(monkeypatch, strategy):
    monkeypatch.setenv(srs.ENV_STRATEGY, strategy)
    assert srs.get_refresh_strategy() == strategy


def test_get_refresh_strategy_rejects_unknown(monkeypatch):
    monkeypatch.setenv(srs.ENV_STRATEGY, "not-real")
    assert srs.get_refresh_strategy() == srs.DEFAULT_STRATEGY


def test_get_refresh_max_tokens_default(monkeypatch):
    monkeypatch.delenv(srs.ENV_MAX_TOKENS, raising=False)
    assert srs.get_refresh_max_tokens() == srs.DEFAULT_MAX_TOKENS


def test_get_refresh_max_tokens_reads_env(monkeypatch):
    monkeypatch.setenv(srs.ENV_MAX_TOKENS, "500")
    assert srs.get_refresh_max_tokens() == 500


# ─── estimate_tokens ─────────────────────────────────────────────


def test_estimate_tokens_empty_is_zero():
    assert srs.estimate_tokens("") == 0


def test_estimate_tokens_rough_4_chars_per_token():
    assert srs.estimate_tokens("a" * 400) == 101  # 400//4 + 1


# ─── cost-gate skip marker ───────────────────────────────────────


def test_build_refresh_marker_shape():
    marker = srs.build_refresh_marker("foo.py", 20, "most-edited")
    assert marker == "[stale-refresh-injected] view foo.py at iter 20 (strategy=most-edited)"


def test_build_skip_marker_shape():
    marker = srs.build_skip_marker("foo.py", 10, 2000, 1000)
    assert marker.startswith("[stale-refresh-skipped] cost-budget-exceeded")
    assert "foo.py" in marker
    assert "estimated_tokens=2000" in marker
    assert "max_tokens=1000" in marker


# ─── extract_mutated_path ────────────────────────────────────────


def test_extract_mutated_path_edit_tool():
    tu = {"name": "Edit", "input": {"file_path": "/tmp/foo.py"}}
    assert srs.extract_mutated_path(tu) == "/tmp/foo.py"


def test_extract_mutated_path_write_tool():
    tu = {"name": "Write", "input": {"file_path": "/tmp/bar.py"}}
    assert srs.extract_mutated_path(tu) == "/tmp/bar.py"


def test_extract_mutated_path_text_editor_str_replace():
    tu = {
        "name": "str_replace_based_edit_tool",
        "input": {"command": "str_replace", "path": "/tmp/baz.py"},
    }
    assert srs.extract_mutated_path(tu) == "/tmp/baz.py"


def test_extract_mutated_path_text_editor_view_returns_none():
    tu = {
        "name": "str_replace_based_edit_tool",
        "input": {"command": "view", "path": "/tmp/baz.py"},
    }
    assert srs.extract_mutated_path(tu) is None


def test_extract_mutated_path_unknown_tool_returns_none():
    tu = {"name": "Glob", "input": {"pattern": "*.py"}}
    assert srs.extract_mutated_path(tu) is None


def test_extract_mutated_path_missing_path_returns_none():
    tu = {"name": "Edit", "input": {}}
    assert srs.extract_mutated_path(tu) is None


# ─── read_file_for_refresh ───────────────────────────────────────


def test_read_file_for_refresh_returns_content(tmp_path: Path):
    target = tmp_path / "sample.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    assert srs.read_file_for_refresh(target) == "print('hi')\n"


def test_read_file_for_refresh_missing_returns_none(tmp_path: Path):
    assert srs.read_file_for_refresh(tmp_path / "nope.py") is None


def test_read_file_for_refresh_outside_worktree_rejected(tmp_path: Path):
    inside = tmp_path / "ok.py"
    inside.write_text("x = 1", encoding="utf-8")
    other_root = tmp_path / "other"
    other_root.mkdir()
    # Reading ``inside`` while restricting to ``other_root`` should reject.
    assert srs.read_file_for_refresh(inside, worktree_root=other_root) is None


def test_read_file_for_refresh_inside_worktree_allowed(tmp_path: Path):
    inside = tmp_path / "ok.py"
    inside.write_text("y = 2", encoding="utf-8")
    assert srs.read_file_for_refresh(inside, worktree_root=tmp_path) == "y = 2"
