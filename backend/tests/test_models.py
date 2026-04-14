"""Fix-D D2 — Pydantic validation coverage for backend.models.

These are contract tests, not type checks. Each case exercises a
branch of Pydantic's validator: a required field, an enum boundary,
a default factory, or a round-trip through ``model_dump`` +
``Model(**dump)``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.models import (
    AISuggestion,
    Agent,
    AgentCreate,
    AgentProgress,
    AgentStatus,
    AgentType,
    AgentWorkspace,
    ChatRequest,
    MessageRole,
    Notification,
    NotificationLevel,
    OrchestratorMessage,
    Simulation,
    SimulationRequest,
    SimulationStatus,
    SimulationTrack,
    SubTask,
    Task,
    TaskCreate,
    TaskPriority,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Required fields
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_agent_requires_id_name_type():
    with pytest.raises(ValidationError) as exc:
        Agent()  # type: ignore[call-arg]
    missing = {e["loc"][0] for e in exc.value.errors()}
    assert {"id", "name", "type"}.issubset(missing)


def test_task_requires_id_title():
    with pytest.raises(ValidationError) as exc:
        Task()  # type: ignore[call-arg]
    missing = {e["loc"][0] for e in exc.value.errors()}
    assert "id" in missing and "title" in missing


def test_notification_requires_id_level_title_message():
    with pytest.raises(ValidationError):
        Notification(id="n1", level="warning", title="t")  # type: ignore[call-arg]


def test_simulation_requires_track_and_module():
    with pytest.raises(ValidationError) as exc:
        Simulation(id="s1")  # type: ignore[call-arg]
    missing = {e["loc"][0] for e in exc.value.errors()}
    assert {"track", "module"}.issubset(missing)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enum coercion + rejection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_agent_type_rejects_bogus_value():
    with pytest.raises(ValidationError):
        Agent(id="a1", name="A", type="bogus_agent_type")  # type: ignore[arg-type]


def test_task_priority_accepts_enum_and_string():
    t1 = Task(id="t1", title="x", priority="high")  # type: ignore[arg-type]
    t2 = Task(id="t2", title="x", priority=TaskPriority.high)
    assert t1.priority is TaskPriority.high
    assert t2.priority is TaskPriority.high


def test_task_status_rejects_unknown_string():
    with pytest.raises(ValidationError):
        Task(id="t1", title="x", status="bogus")  # type: ignore[arg-type]


def test_notification_level_enum():
    n = Notification(id="n1", level="critical", title="t", message="m")  # type: ignore[arg-type]
    assert n.level is NotificationLevel.critical
    with pytest.raises(ValidationError):
        Notification(id="n1", level="loud", title="t", message="m")  # type: ignore[arg-type]


def test_message_role_enum():
    m = OrchestratorMessage(id="m1", role="user", content="hi")  # type: ignore[arg-type]
    assert m.role is MessageRole.user
    with pytest.raises(ValidationError):
        OrchestratorMessage(id="m1", role="bot", content="hi")  # type: ignore[arg-type]


def test_simulation_track_and_status_enums():
    s = Simulation(id="s1", track="algo", module="isp")  # type: ignore[arg-type]
    assert s.track is SimulationTrack.algo
    assert s.status is SimulationStatus.running  # default
    with pytest.raises(ValidationError):
        Simulation(id="s1", track="magic", module="isp")  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Defaults + default_factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_agent_defaults_are_independent_instances():
    a1 = Agent(id="a1", name="A1", type=AgentType.firmware)
    a2 = Agent(id="a2", name="A2", type=AgentType.firmware)
    a1.sub_tasks.append(SubTask(id="s1", label="x"))
    assert a2.sub_tasks == []  # default_factory not shared
    assert a1.progress.total == 0
    assert a1.workspace.status == "none"


def test_task_timestamp_default_is_iso_format():
    t = Task(id="t1", title="x")
    # ISO-8601 has a T separator
    assert "T" in t.created_at


def test_notification_defaults():
    n = Notification(id="n1", level="info", title="t", message="m")
    assert n.read is False
    assert n.auto_resolved is False
    assert n.dispatch_status == "pending"
    assert n.send_attempts == 0
    assert n.action_url is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Round-trip (model_dump → Model(**dump))
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_agent_round_trip_preserves_nested():
    original = Agent(
        id="a1", name="Alpha", type=AgentType.firmware,
        status=AgentStatus.running,
        progress=AgentProgress(current=3, total=5),
        sub_tasks=[SubTask(id="s1", label="boot"), SubTask(id="s2", label="isp")],
        workspace=AgentWorkspace(branch="main", path="/tmp/ws", commit_count=2),
    )
    restored = Agent(**original.model_dump())
    assert restored == original
    assert restored.progress.current == 3
    assert len(restored.sub_tasks) == 2
    assert restored.workspace.branch == "main"


def test_task_round_trip_lists_preserved():
    original = Task(
        id="t1", title="T", priority=TaskPriority.high,
        labels=["urgent", "firmware"], depends_on=["t0"],
        child_task_ids=["tc1", "tc2"],
    )
    restored = Task(**original.model_dump())
    assert restored.labels == ["urgent", "firmware"]
    assert restored.depends_on == ["t0"]
    assert restored.child_task_ids == ["tc1", "tc2"]


def test_orchestrator_message_with_suggestion_round_trip():
    sug = AISuggestion(id="sg1", type="spawn", title="T", description="D")
    msg = OrchestratorMessage(id="m1", role=MessageRole.orchestrator,
                              content="here", suggestion=sug)
    restored = OrchestratorMessage(**msg.model_dump())
    assert restored.suggestion is not None
    assert restored.suggestion.type == "spawn"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Create / Update subset models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_agent_create_minimal():
    c = AgentCreate(name="A", type=AgentType.firmware)
    assert c.sub_type == ""
    assert c.ai_model is None


def test_task_create_minimal():
    c = TaskCreate(title="T")
    assert c.priority is TaskPriority.medium
    assert c.labels == []


def test_chat_request_accepts_empty_string_by_default():
    # There is no min_length on ChatRequest.message — document the current
    # contract so a future tightening breaks this test on purpose.
    r = ChatRequest(message="")
    assert r.message == ""


def test_simulation_request_enum_validated():
    r = SimulationRequest(track="npu", module="yolov5")  # type: ignore[arg-type]
    assert r.track is SimulationTrack.npu
    with pytest.raises(ValidationError):
        SimulationRequest(track="bogus", module="x")  # type: ignore[arg-type]
