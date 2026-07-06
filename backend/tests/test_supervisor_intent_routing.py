"""Deterministic supervisor-intent routing pre-guard (audit codex#1, 2026-07-06).

`_is_supervisor_intent` must pin ticket-inspection / rescue and fleet
observability to the conversational (own-tools) path BEFORE any LLM router
runs — so a supervisor ask can never be sent to a shell-capable specialist
(the OP-2533 → run_bash → PEP HOLD incident). See backend/agents/nodes.py.
"""

from backend.agents.nodes import _is_supervisor_intent


class TestTicketInspectionRescue:
    def test_recheck_ticket_zh(self):
        assert _is_supervisor_intent("再看一次 OP-2533 確認 stoploss 沒了")

    def test_stuck_ticket_zh(self):
        assert _is_supervisor_intent("OP-2531 卡住了幫我看看為什麼")

    def test_fix_and_comment_zh(self):
        assert _is_supervisor_intent("OP-2533 幫我修好，並留言")

    def test_check_status_en(self):
        assert _is_supervisor_intent("check the status of OP-2530")

    def test_requeue_en(self):
        assert _is_supervisor_intent("requeue OP-2530 and strip its labels")

    def test_triage_en(self):
        assert _is_supervisor_intent("triage OP-2533")

    def test_what_happened_en(self):
        assert _is_supervisor_intent("what happened to OP-2533?")

    def test_investigate_en(self):
        assert _is_supervisor_intent("investigate OP-2533 please")

    def test_why_stuck_zh(self):
        assert _is_supervisor_intent("OP-2533 為什麼還沒動")

    def test_handle_it_zh(self):
        assert _is_supervisor_intent("幫我處理一下 OP-2531")


class TestFleetObservability:
    def test_fleet_status_zh(self):
        assert _is_supervisor_intent("現在車隊狀況怎麼樣")

    def test_quota_zh(self):
        assert _is_supervisor_intent("各家 provider 的配額還剩多少")

    def test_incidents_en(self):
        assert _is_supervisor_intent("any recent incidents in the fleet?")


class TestNonSupervisor:
    def test_plain_question_not_supervisor(self):
        # a general knowledge question is NOT supervisor intent (no ticket/fleet)
        assert not _is_supervisor_intent("What is ISP tuning?")

    def test_build_command_not_supervisor(self):
        assert not _is_supervisor_intent("Compile the firmware driver")

    def test_ticket_ref_without_verb_not_forced(self):
        # bare ticket id with no inspect/rescue verb → don't force (let router decide)
        assert not _is_supervisor_intent("OP-2533")

    def test_empty(self):
        assert not _is_supervisor_intent("")
        assert not _is_supervisor_intent(None)
