"""Stage-1 wiring tests for auto-runner-sdk.

Validates the runner glue without touching the real Anthropic API:

  * TODO scanner finds first pending item, honours filter, skips [x]/[O]/[!]
  * _mark_item_failed flips [ ] → [!]
  * run_one_item drives a real handler-backed AnthropicClient with a
    canned SDK response sequence, ending in "✅ 項目完成"
  * Cost accounting captures cache_read tokens correctly
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.agents import runner_handlers
from backend.agents.anthropic_native_client import AnthropicClient
from backend.agents.cost_guard import CostGuard, InMemoryCostStore
from backend.agents.runner_handlers import make_runner_dispatcher


_RUNNER_PATH = (
    Path(__file__).resolve().parents[2] / "auto-runner-sdk.py"
)


def _load_runner_module() -> Any:
    """Load auto-runner-sdk.py despite the hyphen in its filename."""
    sys.modules.pop("runner_sdk_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "runner_sdk_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Sandboxed project root with TODO/SOP/HANDOFF files; runner sandboxed."""
    sop_dir = tmp_path / "docs" / "sop"
    sop_dir.mkdir(parents=True)
    (sop_dir / "implement_phase_step.md").write_text(
        "# SOP\nfollow these rules strictly.\n"
    )
    (tmp_path / "TODO.md").write_text(
        "## Priority X\n\n"
        "### A1. First section\n"
        "- [ ] A1.1 the first task — describe the API surface\n"
        "- [ ] A1.2 the second task\n"
        "\n"
        "### A2. Second section\n"
        "- [x] A2.1 already done\n"
        "- [ ] A2.2 still pending\n"
    )
    (tmp_path / "HANDOFF.md").write_text("# HANDOFF\nempty\n")

    monkeypatch.setattr(runner_handlers, "BASE_DIR", tmp_path.resolve())
    mod = _load_runner_module()
    mod.BASE_DIR = tmp_path.resolve()
    mod.TODO_FILE = tmp_path / "TODO.md"
    mod.HANDOFF_FILE = tmp_path / "HANDOFF.md"
    mod.SOP_FILE = sop_dir / "implement_phase_step.md"
    return tmp_path, mod


# ─── TODO scanner ────────────────────────────────────────────────


def test_get_next_pending_returns_first(fake_project):
    _, mod = fake_project
    sec, item, ctx = mod.get_next_pending_item()
    assert sec == "### A1. First section"
    assert item == "- [ ] A1.1 the first task — describe the API surface"
    assert "A1.2" in ctx


def test_get_next_pending_advances_after_completion(fake_project):
    _, mod = fake_project
    text = mod.TODO_FILE.read_text().replace("- [ ] A1.1", "- [x] A1.1", 1)
    mod.TODO_FILE.write_text(text)
    sec, item, _ = mod.get_next_pending_item()
    assert "A1.2" in item


def test_filter_section_id_match(fake_project, monkeypatch):
    _, mod = fake_project
    monkeypatch.setattr(mod, "RUNNER_FILTER", {"A2"})
    sec, item, _ = mod.get_next_pending_item()
    assert sec == "### A2. Second section"
    assert "A2.2" in item


def test_filter_letter_prefix(fake_project, monkeypatch):
    _, mod = fake_project
    monkeypatch.setattr(mod, "RUNNER_FILTER", {"A"})
    sec, _item, _ = mod.get_next_pending_item()
    assert sec == "### A1. First section"


def test_no_pending_returns_none(fake_project):
    _, mod = fake_project
    text = mod.TODO_FILE.read_text().replace("- [ ]", "- [x]")
    mod.TODO_FILE.write_text(text)
    sec, item, ctx = mod.get_next_pending_item()
    assert sec is None
    assert item is None
    assert ctx is None


def test_mark_item_failed_flips_to_bang(fake_project):
    _, mod = fake_project
    item_line = "- [ ] A1.1 the first task — describe the API surface"
    mod._mark_item_failed(item_line)
    text = mod.TODO_FILE.read_text()
    assert "- [!] A1.1" in text
    assert "- [ ] A1.1" not in text


# ─── End-to-end agentic loop with mocked SDK ─────────────────────


class _Usage:
    def __init__(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _Block:
    def __init__(self, type_: str, **attrs: Any) -> None:
        self.type = type_
        for k, v in attrs.items():
            setattr(self, k, v)


class _CannedResponse:
    def __init__(self, *, content: list[Any], stop_reason: str, usage: _Usage):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


def _build_canned_sdk(turn_responses: list[_CannedResponse]) -> MagicMock:
    """Wire a MagicMock SDK whose messages.create returns scripted turns."""
    call_idx = [0]

    def _create(**_kwargs: Any) -> _CannedResponse:
        i = call_idx[0]
        call_idx[0] += 1
        if i >= len(turn_responses):
            raise RuntimeError(
                f"messages.create called {i + 1} times; "
                f"only {len(turn_responses)} canned responses provided"
            )
        return turn_responses[i]

    mock = MagicMock()
    mock.messages.create.side_effect = _create
    return mock


def test_run_one_item_drives_loop_to_completion(
    fake_project, monkeypatch
) -> None:
    """End-to-end: real handlers + mocked SDK that scripts a 3-turn loop."""
    project_root, mod = fake_project
    todo_path = mod.TODO_FILE
    item_line = "- [ ] A1.1 the first task — describe the API surface"

    turns = [
        _CannedResponse(
            content=[
                _Block(
                    "tool_use",
                    id="tu_1",
                    name="Read",
                    input={"file_path": str(todo_path)},
                ),
            ],
            stop_reason="tool_use",
            usage=_Usage(
                input_tokens=1200,
                output_tokens=40,
                cache_creation_input_tokens=1200,
            ),
        ),
        _CannedResponse(
            content=[
                _Block(
                    "tool_use",
                    id="tu_2",
                    name="Edit",
                    input={
                        "file_path": str(todo_path),
                        "old_string": item_line,
                        "new_string": item_line.replace("- [ ]", "- [x]", 1),
                    },
                ),
            ],
            stop_reason="tool_use",
            usage=_Usage(
                input_tokens=80,
                output_tokens=70,
                cache_read_input_tokens=1200,
            ),
        ),
        _CannedResponse(
            content=[_Block("text", text="✅ 項目完成")],
            stop_reason="end_turn",
            usage=_Usage(
                input_tokens=60,
                output_tokens=10,
                cache_read_input_tokens=1200,
            ),
        ),
    ]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-for-testing")
    client = AnthropicClient(
        default_model="claude-opus-4-7",
        dispatcher=make_runner_dispatcher(),
    )
    client._client = _build_canned_sdk(turns)  # type: ignore[attr-defined]

    cost_guard = CostGuard(store=InMemoryCostStore())

    success, result = asyncio.run(
        mod.run_one_item(
            client=client,
            cost_guard=cost_guard,
            section_title="### A1. First section",
            item_line=item_line,
            section_context="(ctx)",
            sop_text=mod.SOP_FILE.read_text(),
            todo_text=todo_path.read_text(),
            handoff_text=mod.HANDOFF_FILE.read_text(),
        )
    )

    assert success is True
    assert result is not None
    assert result.iterations == 3
    assert result.stop_reason == "end_turn"
    # Edit handler ran — TODO is updated on disk
    assert "- [x] A1.1" in todo_path.read_text()
    # Token bookkeeping aggregated
    assert result.usage.input_tokens == 1200 + 80 + 60
    assert result.usage.cache_read_input_tokens == 1200 + 1200
    # Cost was recorded — guard's store has 1 estimate + 1 actual
    estimates = cost_guard.store._estimates  # type: ignore[attr-defined]
    actuals = cost_guard.store._actuals  # type: ignore[attr-defined]
    assert len(estimates) == 1
    assert len(actuals) == 1
    estimate = next(iter(estimates.values()))
    assert estimate.cost_usd_estimated > 0


def test_run_one_item_marks_failure_when_no_completion_signal(
    fake_project, monkeypatch
) -> None:
    """If the model stops without ✅ 項目完成, run_one_item returns False."""
    project_root, mod = fake_project
    item_line = "- [ ] A1.1 the first task — describe the API surface"

    # Single turn: model stops with stop_reason=end_turn but no completion text.
    turns = [
        _CannedResponse(
            content=[_Block("text", text="I cannot complete this.")],
            stop_reason="end_turn",
            usage=_Usage(input_tokens=100, output_tokens=20),
        ),
    ]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    client = AnthropicClient(
        default_model="claude-opus-4-7",
        dispatcher=make_runner_dispatcher(),
    )
    client._client = _build_canned_sdk(turns)  # type: ignore[attr-defined]

    success, result = asyncio.run(
        mod.run_one_item(
            client=client,
            cost_guard=CostGuard(store=InMemoryCostStore()),
            section_title="### A1. First section",
            item_line=item_line,
            section_context="(ctx)",
            sop_text=mod.SOP_FILE.read_text(),
            todo_text=mod.TODO_FILE.read_text(),
            handoff_text=mod.HANDOFF_FILE.read_text(),
        )
    )
    assert success is False
    assert result is not None
    assert result.stop_reason == "end_turn"
