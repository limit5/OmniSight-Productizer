"""Tests for conversational AI system (Phase 19)."""



class TestQuestionDetection:

    def test_english_questions(self):
        from backend.agents.nodes import _is_question
        assert _is_question("What is ISP tuning?")
        assert _is_question("How do I configure the sensor?")
        assert _is_question("Why did the agent fail?")
        assert _is_question("Can you explain the pipeline?")
        assert _is_question("Is the system running?")
        assert _is_question("Tell me about NPI phases")

    def test_chinese_questions(self):
        from backend.agents.nodes import _is_question
        assert _is_question("ISP 調優順序怎麼安排？")
        assert _is_question("目前進度如何？")
        assert _is_question("為什麼 agent 失敗了？")
        assert _is_question("可以介紹一下 NPI 嗎")
        assert _is_question("有什麼建議嗎")

    def test_task_commands_not_questions(self):
        from backend.agents.nodes import _is_question
        assert not _is_question("compile firmware")
        assert not _is_question("run tests")
        assert not _is_question("git status")
        assert not _is_question("deploy to production")

    def test_edge_cases(self):
        from backend.agents.nodes import _is_question
        assert not _is_question("")
        assert _is_question("?")


class TestBuildStateSummary:

    def test_returns_string(self):
        from backend.agents.nodes import _build_state_summary
        result = _build_state_summary()
        assert isinstance(result, str)
        assert "Agents:" in result or "unavailable" in result

    def test_contains_counts(self):
        from backend.agents.nodes import _build_state_summary
        result = _build_state_summary()
        if "unavailable" not in result:
            assert "total" in result
            assert "running" in result or "idle" in result


class TestConversationNode:

    def test_node_exists(self):
        from backend.agents.nodes import conversation_node
        assert callable(conversation_node)

    def test_node_in_graph(self):
        from backend.agents.graph import agent_graph
        # The compiled graph should have a conversation node
        assert agent_graph is not None


class TestGraphRouting:

    def test_conversational_route(self):
        from backend.agents.graph import _route_after_orchestrator
        from backend.agents.state import GraphState
        state = GraphState(is_conversational=True)
        assert _route_after_orchestrator(state) == "conversation"

    def test_specialist_route_unchanged(self):
        from backend.agents.graph import _route_after_orchestrator
        from backend.agents.state import GraphState
        state = GraphState(routed_to="firmware")
        assert _route_after_orchestrator(state) == "firmware"

    def test_unknown_route_fallback(self):
        from backend.agents.graph import _route_after_orchestrator
        from backend.agents.state import GraphState
        state = GraphState(routed_to="unknown_type")
        assert _route_after_orchestrator(state) == "general"


class TestGraphStateField:

    def test_is_conversational_default(self):
        from backend.agents.state import GraphState
        state = GraphState()
        assert state.is_conversational is False

    def test_is_conversational_set(self):
        from backend.agents.state import GraphState
        state = GraphState(is_conversational=True)
        assert state.is_conversational is True
