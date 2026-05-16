"""OP-1296 -- input-validation audit tests for memory_tool_handler."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.memory_tool_handler import (
    DEFAULT_TIER,
    ERR_BAD_INPUT,
    ERR_STORAGE_FULL,
    FEDERATION_ENV,
    MEMORY_PATH_PREFIX,
    TIER_L_OPTIN_ENV,
    MemoryTier,
    MemoryToolConfig,
    MemoryToolHandler,
    RecallRequest,
    UnknownMemoryTier,
    classify_tier,
    enforce_recall,
    evaluate_recall,
    list_seeded_lessons,
    parse_federation,
    seed_lessons_from,
    tier_filter,
    tier_is_recallable,
)


@pytest.fixture
def memory_handler(tmp_path: Path) -> MemoryToolHandler:
    return MemoryToolHandler(
        MemoryToolConfig(
            fleet_id="op-1296",
            storage_root=tmp_path / "memory" / "op-1296",
            cap_mb=1,
            progress_path=tmp_path / "progress.txt",
            ticket_key="OP-1296",
        )
    )


def _request(tier: MemoryTier = MemoryTier.S) -> RecallRequest:
    return RecallRequest(
        query="input validation audit",
        tier=tier,
        query_fleet="codex",
        target_fleet="codex",
    )


def _no_audit(_request: RecallRequest, _decision: object) -> None:
    return None


def test_handle_validates_none_empty_wrong_type_and_large_values(
    memory_handler: MemoryToolHandler,
) -> None:
    """Audit handler-level validation for malformed tool_use payloads."""
    with pytest.raises(AttributeError):
        memory_handler.handle(None)  # type: ignore[arg-type]

    with pytest.raises(AttributeError):
        memory_handler.handle([])  # type: ignore[arg-type]

    assert memory_handler.handle({})["error"] == ERR_BAD_INPUT
    assert memory_handler.handle({"command": [], "path": MEMORY_PATH_PREFIX})[
        "error"
    ] == ERR_BAD_INPUT
    assert memory_handler.handle({"command": "view", "path": ""})[
        "error"
    ] == ERR_BAD_INPUT

    too_large = "x" * (2 * 1024 * 1024)
    result = memory_handler.handle(
        {
            "command": "create",
            "path": f"{MEMORY_PATH_PREFIX}/too-large.md",
            "file_text": too_large,
        }
    )
    assert result["error"] == ERR_STORAGE_FULL


def test_command_argument_validation_edges(memory_handler: MemoryToolHandler) -> None:
    """Each mutating command rejects wrong-type or empty required arguments."""
    assert memory_handler.handle(
        {
            "command": "create",
            "path": f"{MEMORY_PATH_PREFIX}/bad-create.md",
            "file_text": ["not", "text"],
        }
    )["error"] == ERR_BAD_INPUT

    memory_handler.handle(
        {
            "command": "create",
            "path": f"{MEMORY_PATH_PREFIX}/base.md",
            "file_text": "alpha\n",
        }
    )

    assert memory_handler.handle(
        {
            "command": "str_replace",
            "path": f"{MEMORY_PATH_PREFIX}/base.md",
            "old_str": ["alpha"],
            "new_str": "beta",
        }
    )["error"] == ERR_BAD_INPUT
    assert memory_handler.handle(
        {
            "command": "insert",
            "path": f"{MEMORY_PATH_PREFIX}/base.md",
            "insert_line": None,
            "text": "beta",
        }
    )["error"] == ERR_BAD_INPUT
    assert memory_handler.handle(
        {
            "command": "insert",
            "path": f"{MEMORY_PATH_PREFIX}/base.md",
            "insert_line": -1,
            "text": "beta",
        }
    )["error"] == ERR_BAD_INPUT
    assert memory_handler.handle(
        {
            "command": "insert",
            "path": f"{MEMORY_PATH_PREFIX}/base.md",
            "insert_line": 0,
            "text": {"not": "text"},
        }
    )["error"] == ERR_BAD_INPUT
    assert memory_handler.handle(
        {
            "command": "rename",
            "path": f"{MEMORY_PATH_PREFIX}/base.md",
            "new_path": "",
        }
    )["error"] == ERR_BAD_INPUT
    assert memory_handler.handle(
        {
            "command": "rename",
            "path": f"{MEMORY_PATH_PREFIX}/base.md",
            "new_path": ["not", "path"],
        }
    )["error"] == ERR_BAD_INPUT


def test_storage_helper_input_validation_edges(
    tmp_path: Path, memory_handler: MemoryToolHandler
) -> None:
    """Public storage helpers handle empty inputs and expose wrong-type gaps."""
    assert classify_tier(content=None, filename="") == DEFAULT_TIER
    assert classify_tier(content="", filename="") == DEFAULT_TIER
    assert classify_tier(content="tier: L\n" + ("x" * 100_000), filename="") == "L"
    with pytest.raises(TypeError):
        classify_tier(content=123, filename="x.md")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        classify_tier(content=None, filename=None)  # type: ignore[arg-type]

    assert tier_is_recallable("", tier_l_optin=False) is False
    assert (
        tier_is_recallable(None, tier_l_optin=True) is False  # type: ignore[arg-type]
    )
    assert tier_is_recallable("L" * 100_000, tier_l_optin=True) is False

    empty_lessons = tmp_path / "empty-lessons"
    empty_lessons.mkdir()
    assert seed_lessons_from(memory_handler, empty_lessons) == 0
    assert seed_lessons_from(memory_handler, tmp_path / "missing") == 0
    with pytest.raises(AttributeError):
        seed_lessons_from(memory_handler, None)  # type: ignore[arg-type]
    assert list_seeded_lessons(memory_handler) == []
    with pytest.raises(AttributeError):
        list_seeded_lessons(None)  # type: ignore[arg-type]


def test_policy_helper_input_validation_edges() -> None:
    """Public policy helpers cover None, empty, wrong-type, and large values."""
    assert parse_federation(None) == frozenset()
    assert parse_federation("") == frozenset()
    assert parse_federation("codex, , claude") == frozenset({"codex", "claude"})
    assert len(parse_federation(",".join(f"fleet-{i}" for i in range(1000)))) == 1000
    with pytest.raises(AttributeError):
        parse_federation(123)  # type: ignore[arg-type]

    with pytest.raises(UnknownMemoryTier):
        MemoryTier.parse(None)  # type: ignore[arg-type]
    with pytest.raises(UnknownMemoryTier):
        MemoryTier.parse("")
    with pytest.raises(UnknownMemoryTier):
        MemoryTier.parse("not-a-tier")

    assert evaluate_recall(_request(), env={}).permitted is True
    with pytest.raises(AttributeError):
        evaluate_recall(None, env={})  # type: ignore[arg-type]
    with pytest.raises(UnknownMemoryTier):
        evaluate_recall(
            RecallRequest(
                query="wrong tier",
                tier="S",  # type: ignore[arg-type]
                query_fleet="codex",
                target_fleet="codex",
            ),
            env={},
        )

    assert enforce_recall(
        _request(MemoryTier.L), env={TIER_L_OPTIN_ENV: "1"}
    ).permitted
    assert enforce_recall(
        RecallRequest(
            query="cross fleet",
            tier=MemoryTier.M,
            query_fleet="codex",
            target_fleet="claude",
        ),
        env={FEDERATION_ENV: "claude"},
        audit_emitter=_no_audit,
    ).permitted

    assert tier_filter([], request=_request(), env={}, audit_emitter=_no_audit) == []
    records = [{"id": str(i)} for i in range(1000)]
    assert (
        tier_filter(records, request=_request(), env={}, audit_emitter=_no_audit)
        == records
    )
    with pytest.raises(TypeError):
        tier_filter(  # type: ignore[arg-type]
            None, request=_request(), env={}, audit_emitter=_no_audit
        )
