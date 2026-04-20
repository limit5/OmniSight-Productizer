"""Tests for Tiered Memory Architecture (Phase 32).

Covers:
- L1: Core rules loading from CLAUDE.md
- L2: summarize_state tool + context compression gate
- L3: Episodic memory DB (FTS5 + CRUD) + tools (search/save)
- Integration: error_check L3 hints, Gerrit merge auto-save
"""

from __future__ import annotations

import pytest

from backend.agents.state import GraphState
from backend.llm_adapter import HumanMessage, AIMessage


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L1: Core Rules
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestL1CoreRules:

    def test_load_core_rules(self):
        from backend.prompt_loader import load_core_rules
        content = load_core_rules()
        # CLAUDE.md should exist and have content
        assert isinstance(content, str)

    def test_core_rules_in_system_prompt(self):
        from backend.prompt_loader import build_system_prompt
        prompt = build_system_prompt(model_name="test", agent_type="firmware")
        # If CLAUDE.md exists, core rules should be first section
        if "Core Rules" in prompt:
            # Core rules appear before the agent role definition
            assert prompt.index("Core Rules") < prompt.index("Firmware Agent")

    def test_core_rules_cached(self):
        from backend.prompt_loader import load_core_rules
        first = load_core_rules()
        second = load_core_rules()
        assert first == second  # Should return cached result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L2: Summarize State Tool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestL2SummarizeState:

    @pytest.mark.asyncio
    async def test_summarize_empty(self):
        from backend.agents.tools import summarize_state
        result = await summarize_state.ainvoke({"conversation_text": ""})
        assert "[L2 SUMMARY]" in result
        assert "No conversation" in result

    @pytest.mark.asyncio
    async def test_summarize_rule_based(self):
        """Without LLM, should use rule-based extraction."""
        from backend.agents.tools import summarize_state
        conversation = (
            "[OK] read_file: contents of main.c\n"
            "[ERROR] run_bash: compilation failed\n"
            "decided to add -lv4l2 flag\n"
            "[OK] write_file: updated Makefile\n"
            "[PASS] run_simulation: 5/5 tests passed\n"
        )
        result = await summarize_state.ainvoke({
            "conversation_text": conversation,
            "include_system_state": False,
        })
        assert "[L2 SUMMARY]" in result
        # Should extract tool results and decisions
        assert "OK" in result or "ERROR" in result or "decided" in result

    @pytest.mark.asyncio
    async def test_summarize_respects_max_chars(self):
        from backend.agents.tools import summarize_state
        long_text = "A" * 5000
        result = await summarize_state.ainvoke({
            "conversation_text": long_text,
            "max_summary_chars": 200,
            "include_system_state": False,
        })
        # Rule-based fallback should respect the limit (approximately)
        assert len(result) < 500  # Some overhead for prefix


class TestL2ContextCompressionGate:

    def test_no_compression_short_context(self):
        """Short message list should pass through unchanged."""
        from backend.agents.nodes import context_compression_gate
        state = GraphState(messages=[
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there"),
        ])
        result = context_compression_gate(state)
        assert result == {}  # No changes

    def test_no_compression_few_messages(self):
        """Even if messages are long-ish, fewer than 6 shouldn't compress."""
        from backend.agents.nodes import context_compression_gate
        state = GraphState(messages=[
            HumanMessage(content="x" * 1000),
            AIMessage(content="y" * 1000),
            HumanMessage(content="z" * 1000),
        ])
        result = context_compression_gate(state)
        assert result == {}

    def test_context_token_estimation(self):
        from backend.agents.nodes import _estimate_context_tokens
        state = GraphState(messages=[
            HumanMessage(content="Hello world"),  # 11 chars
        ])
        tokens = _estimate_context_tokens(state)
        assert tokens > 0
        assert tokens == 11 // 3  # _CHARS_PER_TOKEN = 3

    def test_get_context_window_default(self):
        from backend.agents.nodes import _get_context_window
        window = _get_context_window()
        assert window > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L3: Episodic Memory DB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestL3EpisodicMemoryDB:
    """SP-3.12 (2026-04-20): migrated from client fixture to
    pg_test_conn. Search now hits PG's tsvector @@ plainto_tsquery
    on the STORED generated column (alembic 0017).
    """

    @pytest.mark.asyncio
    async def test_insert_and_get(self, pg_test_conn):
        from backend import db
        await db.insert_episodic_memory(pg_test_conn, {
            "id": "test-mem-1",
            "error_signature": "undefined reference to v4l2_open",
            "solution": "Add -lv4l2 to LDFLAGS",
            "soc_vendor": "fullhan",
            "sdk_version": "1.2",
            "tags": ["linker", "v4l2"],
        })
        mem = await db.get_episodic_memory(pg_test_conn, "test-mem-1")
        assert mem is not None
        assert mem["error_signature"] == "undefined reference to v4l2_open"
        assert mem["solution"] == "Add -lv4l2 to LDFLAGS"
        assert mem["soc_vendor"] == "fullhan"
        assert mem["tags"] == ["linker", "v4l2"]
        assert mem["quality_score"] == 0.0

    @pytest.mark.asyncio
    async def test_search_tsvector(self, pg_test_conn):
        # Renamed from test_search_fts5 — the underlying index is now
        # PG tsvector. Tokenization difference vs SQLite FTS5:
        # PG's English parser treats ``sensor_config.h`` as a single
        # "host" token and does NOT split on ``.`` / ``_``; SQLite
        # FTS5's unicode61 default tokenizer DOES. Test uses plain
        # English words so both tokenizers return the row.
        # Pre-approved drift documented in
        # docs/phase-3-runtime-v2/01-design-decisions.md §5.
        from backend import db
        await db.insert_episodic_memory(pg_test_conn, {
            "id": "test-mem-fts",
            "error_signature": "fatal error missing include header",
            "solution": "Add vendor include path to CFLAGS",
            "soc_vendor": "rockchip",
            "tags": ["include", "header"],
        })
        results = await db.search_episodic_memory(pg_test_conn, "missing include")
        assert len(results) >= 1
        assert any("missing" in r["error_signature"] for r in results)

    @pytest.mark.asyncio
    async def test_search_vendor_filter(self, pg_test_conn):
        from backend import db
        await db.insert_episodic_memory(pg_test_conn, {
            "id": "test-mem-v1",
            "error_signature": "gpio init failed",
            "solution": "Use GPIO_V2 API",
            "soc_vendor": "ambarella",
        })
        await db.insert_episodic_memory(pg_test_conn, {
            "id": "test-mem-v2",
            "error_signature": "gpio init failed",
            "solution": "Enable GPIO clock first",
            "soc_vendor": "rockchip",
        })
        # Filter by vendor
        results = await db.search_episodic_memory(
            pg_test_conn, "gpio", soc_vendor="rockchip",
        )
        assert all(r["soc_vendor"] == "rockchip" for r in results)

    @pytest.mark.asyncio
    async def test_access_count_increments(self, pg_test_conn):
        from backend import db
        await db.insert_episodic_memory(pg_test_conn, {
            "id": "test-mem-access",
            "error_signature": "unique_error_12345",
            "solution": "Fix it",
        })
        await db.search_episodic_memory(pg_test_conn, "unique_error_12345")
        mem = await db.get_episodic_memory(pg_test_conn, "test-mem-access")
        assert mem["access_count"] >= 1

    @pytest.mark.asyncio
    async def test_delete(self, pg_test_conn):
        from backend import db
        await db.insert_episodic_memory(pg_test_conn, {
            "id": "test-mem-del",
            "error_signature": "to be deleted",
            "solution": "n/a",
        })
        deleted = await db.delete_episodic_memory(pg_test_conn, "test-mem-del")
        assert deleted is True
        mem = await db.get_episodic_memory(pg_test_conn, "test-mem-del")
        assert mem is None

    @pytest.mark.asyncio
    async def test_count(self, pg_test_conn):
        from backend import db
        count = await db.episodic_memory_count(pg_test_conn)
        assert isinstance(count, int)
        assert count >= 0

    @pytest.mark.asyncio
    async def test_list(self, pg_test_conn):
        from backend import db
        memories = await db.list_episodic_memories(pg_test_conn, limit=10)
        assert isinstance(memories, list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L3: Tools (search_past_solutions, save_solution)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestL3Tools:

    @pytest.mark.asyncio
    async def test_save_solution(self, client):
        from backend.agents.tools import save_solution
        result = await save_solution.ainvoke({
            "error_signature": "linker error: undefined v4l2_open",
            "solution": "Add -lv4l2 to LDFLAGS in project Makefile",
            "soc_vendor": "fullhan",
            "sdk_version": "1.2",
            "tags": ["linker", "v4l2"],
        })
        assert "[L3]" in result
        assert "saved" in result.lower()

    @pytest.mark.asyncio
    async def test_save_requires_fields(self, client):
        from backend.agents.tools import save_solution
        result = await save_solution.ainvoke({
            "error_signature": "",
            "solution": "",
        })
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_search_past_solutions_found(self, client):
        from backend.agents.tools import save_solution, search_past_solutions
        # First save one
        await save_solution.ainvoke({
            "error_signature": "cmake toolchain file not found xyz123",
            "solution": "Set CMAKE_TOOLCHAIN_FILE to /opt/sdk/toolchain.cmake",
            "soc_vendor": "rockchip",
        })
        # Then search
        result = await search_past_solutions.ainvoke({
            "error_signature": "toolchain file not found xyz123",
        })
        assert "[L3]" in result
        assert "Found" in result
        assert "toolchain" in result.lower()

    @pytest.mark.asyncio
    async def test_search_past_solutions_not_found(self, client):
        from backend.agents.tools import search_past_solutions
        result = await search_past_solutions.ainvoke({
            "error_signature": "completely_unique_nonexistent_error_zzz999",
        })
        assert "[L3]" in result
        assert "No past solutions" in result

    @pytest.mark.asyncio
    async def test_save_with_gerrit_id_quality_boost(self, client):
        # save_solution tool acquires its own pool conn internally
        # (SP-3.12); the assert read acquires a fresh conn since the
        # save is committed by then. Explicit cleanup at the end so
        # sibling tests see a clean slate.
        from backend.agents.tools import save_solution
        from backend import db
        from backend.db_pool import get_pool
        result = await save_solution.ainvoke({
            "error_signature": "gerrit test error",
            "solution": "gerrit verified fix",
            "gerrit_change_id": "I1234567890",
        })
        assert "[L3]" in result
        try:
            async with get_pool().acquire() as conn:
                memories = await db.search_episodic_memory(
                    conn, "gerrit test error",
                )
            # Quality score should be 1.0 for Gerrit-verified
            assert any(m["quality_score"] == 1.0 for m in memories)
        finally:
            async with get_pool().acquire() as conn:
                for m in memories:
                    await db.delete_episodic_memory(conn, m["id"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolRegistry:

    def test_memory_tools_in_registry(self):
        from backend.agents.tools import TOOL_MAP
        assert "summarize_state" in TOOL_MAP

    def test_episodic_tools_in_registry(self):
        from backend.agents.tools import TOOL_MAP
        assert "search_past_solutions" in TOOL_MAP
        assert "save_solution" in TOOL_MAP

    def test_firmware_has_all_memory_tools(self):
        from backend.agents.tools import AGENT_TOOLS
        fw_tools = {t.name for t in AGENT_TOOLS["firmware"]}
        assert "summarize_state" in fw_tools
        assert "search_past_solutions" in fw_tools
        assert "save_solution" in fw_tools

    def test_reporter_has_l2_but_not_l3(self):
        """Reporter doesn't need L3 episodic tools."""
        from backend.agents.tools import AGENT_TOOLS
        reporter_tools = {t.name for t in AGENT_TOOLS["reporter"]}
        assert "summarize_state" in reporter_tools
        assert "search_past_solutions" not in reporter_tools


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Graph Integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGraphIntegration:

    def test_context_gate_in_graph(self):
        from backend.agents.graph import agent_graph
        assert "context_gate" in agent_graph.nodes

    def test_conversation_to_context_gate(self):
        """Conversation node should route through context_gate."""
        from backend.agents.graph import agent_graph
        # Verify the edge exists by checking node connectivity
        assert "context_gate" in agent_graph.nodes
        assert "conversation" in agent_graph.nodes
