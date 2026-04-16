"""O5 (#268) — intent_bridge tests + orchestrator intake integration.

Uses a ``FakeAdapter`` that records every call so we can assert the
bridge drives the right methods at the right times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from backend import intent_bridge as ib
from backend import intent_source as isrc
from backend.intent_source import (
    IntentStatus,
    IntentStory,
    SubtaskPayload,
    SubtaskRef,
)


# ──────────────────────────────────────────────────────────────
#  Fake adapter — implements the IntentSource protocol
# ──────────────────────────────────────────────────────────────


@dataclass
class FakeAdapter:
    vendor: str = "jira"
    subtask_prefix: str = "PROJ"
    _next: int = 1001
    created: list[tuple[str, list[SubtaskPayload]]] = field(default_factory=list)
    statuses: list[tuple[str, IntentStatus, str]] = field(default_factory=list)
    comments: list[tuple[str, str]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)

    async def fetch_story(self, ticket: str) -> IntentStory:
        return IntentStory(vendor=self.vendor, ticket=ticket,
                           summary="stub")

    async def create_subtasks(self, parent: str,
                              payloads: list[SubtaskPayload]
                              ) -> list[SubtaskRef]:
        if "create" in self.fail_on:
            raise isrc.AdapterError(
                self.vendor, "create_subtasks", "boom",
            )
        self.created.append((parent, list(payloads)))
        refs: list[SubtaskRef] = []
        for _ in payloads:
            refs.append(SubtaskRef(
                vendor=self.vendor,
                ticket=f"{self.subtask_prefix}-{self._next}",
                url=f"https://fake/{self._next}",
                parent=parent,
            ))
            self._next += 1
        return refs

    async def update_status(self, ticket: str, status: IntentStatus,
                            *, comment: str = "") -> dict[str, Any]:
        if "status" in self.fail_on:
            raise isrc.AdapterError(
                self.vendor, "update_status", "boom",
            )
        self.statuses.append((ticket, status, comment))
        return {"ok": True, "ticket": ticket, "status": status.value}

    async def comment(self, ticket: str, body: str) -> dict[str, Any]:
        self.comments.append((ticket, body))
        return {"ok": True}

    async def verify_webhook(self, headers, body):
        return True

    def parse_webhook(self, body):
        return ("", "")


@pytest.fixture(autouse=True)
def _reset():
    ib.reset_bridge_for_tests()
    isrc.reset_registry_for_tests()
    yield
    ib.reset_bridge_for_tests()
    isrc.reset_registry_for_tests()


# ──────────────────────────────────────────────────────────────
#  on_intake_queued — sub-tasks created + parent flipped
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_intake_queued_creates_subtasks_and_flips_parent():
    fake = FakeAdapter()
    isrc.register_source(fake)

    from backend.catc import TaskCard
    cards = [
        (f"task-{i}", TaskCard.from_dict({
            "jira_ticket": f"PROJ-1{i:03d}",
            "acceptance_criteria": f"AC {i}",
            "navigation": {
                "entry_point": f"src/mod_{i}.c",
                "impact_scope": {"allowed": [f"src/mod_{i}/**"],
                                 "forbidden": []},
            },
            "handoff_protocol": ["Push"],
        })) for i in range(3)
    ]
    rec = await ib.on_intake_queued(
        parent="PROJ-1", vendor="jira", cards_with_task_ids=cards,
        dag_id="DAG-1",
    )
    assert rec is not None
    assert len(fake.created) == 1
    assert fake.created[0][0] == "PROJ-1"
    # 3 sub-tasks created by the fake, plus 3 per-sub-task status flips
    # + 1 parent status flip.
    parent_flips = [s for s in fake.statuses if s[0] == "PROJ-1"]
    child_flips = [s for s in fake.statuses if s[0] != "PROJ-1"]
    assert len(parent_flips) == 1
    assert parent_flips[0][1] == IntentStatus.in_progress
    assert len(child_flips) == 3
    assert all(s[1] == IntentStatus.in_progress for s in child_flips)

    # Record must map each CATC task_id to a sub-task ticket.
    for task_id, _card in cards:
        assert task_id in rec.task_to_subtask


@pytest.mark.asyncio
async def test_on_intake_queued_adapter_error_does_not_propagate():
    fake = FakeAdapter(fail_on={"create"})
    isrc.register_source(fake)
    rec = await ib.on_intake_queued(
        parent="PROJ-1", vendor="jira", cards_with_task_ids=[],
    )
    assert rec is None
    # No records left behind.
    assert ib.get_record("PROJ-1") is None


# ──────────────────────────────────────────────────────────────
#  on_worker_gerrit_pushed — sub-task flips to reviewing
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_worker_gerrit_pushed_flips_subtask_to_reviewing():
    fake = FakeAdapter()
    isrc.register_source(fake)
    from backend.catc import TaskCard
    cards = [
        ("task-A", TaskCard.from_dict({
            "jira_ticket": "PROJ-1001",
            "acceptance_criteria": "AC A",
            "navigation": {
                "entry_point": "src/foo.c",
                "impact_scope": {"allowed": ["src/foo/**"], "forbidden": []},
            },
        })),
    ]
    await ib.on_intake_queued(
        parent="PROJ-1", vendor="jira",
        cards_with_task_ids=cards, dag_id="DAG-1",
    )
    fake.statuses.clear()

    # Simulate Worker push — the recorded sub-task ticket is a
    # FakeAdapter-assigned PROJ-1001.
    record = ib.get_record("PROJ-1")
    sub_ticket = record.task_to_subtask["task-A"]
    await ib.on_worker_gerrit_pushed(
        task_id="task-A", jira_ticket="PROJ-1001", parent="PROJ-1",
        change_id="I123", review_url="https://gerrit/123",
        vendor="jira",
    )
    # Status flip recorded for the resolved sub-task ticket.
    assert any(s[0] == sub_ticket and s[1] == IntentStatus.reviewing
               for s in fake.statuses)
    assert record.subtask_status[sub_ticket] == IntentStatus.reviewing
    assert record.gerrit_change_for[sub_ticket] == "I123"


@pytest.mark.asyncio
async def test_on_worker_gerrit_pushed_without_parent_still_updates():
    fake = FakeAdapter()
    isrc.register_source(fake)
    await ib.on_worker_gerrit_pushed(
        task_id="task-A", jira_ticket="PROJ-9001", parent="",
        change_id="I1", review_url="",
        vendor="jira",
    )
    assert any(s[0] == "PROJ-9001" and s[1] == IntentStatus.reviewing
               for s in fake.statuses)


# ──────────────────────────────────────────────────────────────
#  on_gerrit_change_merged — sub-task + parent → Done
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_gerrit_change_merged_flips_parent_when_all_done():
    fake = FakeAdapter()
    isrc.register_source(fake)
    from backend.catc import TaskCard
    cards = [
        ("task-A", TaskCard.from_dict({
            "jira_ticket": "PROJ-1001",
            "acceptance_criteria": "AC A",
            "navigation": {
                "entry_point": "src/foo.c",
                "impact_scope": {"allowed": ["src/foo/**"], "forbidden": []},
            },
        })),
        ("task-B", TaskCard.from_dict({
            "jira_ticket": "PROJ-1002",
            "acceptance_criteria": "AC B",
            "navigation": {
                "entry_point": "src/bar.c",
                "impact_scope": {"allowed": ["src/bar/**"], "forbidden": []},
            },
        })),
    ]
    await ib.on_intake_queued(
        parent="PROJ-1", vendor="jira",
        cards_with_task_ids=cards, dag_id="DAG-1",
    )
    record = ib.get_record("PROJ-1")
    sub_a = record.task_to_subtask["task-A"]
    sub_b = record.task_to_subtask["task-B"]

    # Worker pushes for A + B register Gerrit change ids.
    await ib.on_worker_gerrit_pushed(
        task_id="task-A", jira_ticket="PROJ-1001", parent="PROJ-1",
        change_id="IAAA", review_url="", vendor="jira",
    )
    await ib.on_worker_gerrit_pushed(
        task_id="task-B", jira_ticket="PROJ-1002", parent="PROJ-1",
        change_id="IBBB", review_url="", vendor="jira",
    )
    fake.statuses.clear()

    # Merge A — parent should NOT flip yet.
    await ib.on_gerrit_change_merged(
        change_id="IAAA", commit_msg=f"CATC-Ticket: {sub_a}",
        vendor="jira",
    )
    parent_dones = [s for s in fake.statuses
                    if s[0] == "PROJ-1" and s[1] == IntentStatus.done]
    assert not parent_dones

    # Merge B — all done, parent flips.
    await ib.on_gerrit_change_merged(
        change_id="IBBB", commit_msg=f"CATC-Ticket: {sub_b}",
        vendor="jira",
    )
    parent_dones = [s for s in fake.statuses
                    if s[0] == "PROJ-1" and s[1] == IntentStatus.done]
    assert len(parent_dones) == 1
    assert record.parent_status == IntentStatus.done


@pytest.mark.asyncio
async def test_on_gerrit_change_merged_no_record_falls_through():
    fake = FakeAdapter()
    isrc.register_source(fake)
    # Commit msg still has CATC-Ticket trailer so the sub-task is
    # recognisable even without a parent record.
    await ib.on_gerrit_change_merged(
        change_id="IZZZ", commit_msg="CATC-Ticket: PROJ-8888",
        vendor="jira",
    )
    dones = [s for s in fake.statuses
             if s[0] == "PROJ-8888" and s[1] == IntentStatus.done]
    assert len(dones) == 1


# ──────────────────────────────────────────────────────────────
#  Orchestrator intake end-to-end through the bridge
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_intake_creates_subtasks_via_bridge():
    fake = FakeAdapter()
    isrc.register_source(fake)

    from backend import orchestrator_gateway as og
    from backend import queue_backend as qb
    from backend.dag_schema import DAG, Task

    og.reset_registry_for_tests()
    qb.set_backend_for_tests(qb.InMemoryQueueBackend())
    try:
        dag = DAG(dag_id="PROJ-7", tasks=[
            Task(task_id="A", description="a",
                 required_tier="t1", toolchain="cmake",
                 expected_output="src/a/x.bin"),
            Task(task_id="B", description="b",
                 required_tier="t1", toolchain="cmake",
                 expected_output="src/b/y.bin"),
        ])

        async def _split(_t, _s):
            return (dag.model_dump_json(), 100)

        outcome = await og.intake(
            {"issue": {"key": "PROJ-7",
                       "fields": {"summary": "build both"}}},
            splitter=_split, token_budget=10_000,
        )
        assert outcome.state == "queued"

        # The bridge should have been driven with the CATC cards and
        # created exactly two sub-tasks in the fake tracker.
        assert len(fake.created) == 1
        parent, payloads = fake.created[0]
        assert parent == "PROJ-7"
        assert len(payloads) == 2
        # Parent status flipped to in_progress at least once.
        assert any(s[0] == "PROJ-7" and s[1] == IntentStatus.in_progress
                   for s in fake.statuses)
    finally:
        og.reset_registry_for_tests()
        qb.set_backend_for_tests(None)
