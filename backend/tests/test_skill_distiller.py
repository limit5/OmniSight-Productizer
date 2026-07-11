"""BP.M.2 -- Architect Guild skill distiller (post-U4-I redirect).

Original BP.M.2 assertions pinning the ``auto_distilled_skills``
sink shape have been retargeted to pin the U4-I substrate producer
submit call (frozen archive — the distiller no longer writes it).
Thresholds, scrub semantics, and the L1 gate assertions are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend import skill_distiller as sd


@dataclass
class _FakeRun:
    id: str = "task-123"
    kind: str = "architect/blueprint"
    status: str = "completed"
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


@dataclass
class _FakeStep:
    idempotency_key: str
    output: Any = None
    error: str | None = None


class _FakeConn:
    """Minimal asyncpg-shaped stub — the redirected distiller now
    passes the conn straight through to the producer (which we
    monkeypatch), so ``execute``/``fetchrow`` are recording no-ops."""

    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrow_calls.append((sql, args))
        return None


class _SubmitRecorder:
    """Replaces ``learned_item_producer.submit_quarantined_version``
    so the redirect can be asserted without exercising the DB."""

    def __init__(
        self, *, version_id: str = "v-fake-1", created: bool = True
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._version_id = version_id
        self._created = created

    async def __call__(self, conn: Any, **kwargs: Any):
        self.calls.append({"conn": conn, **kwargs})
        return sd.__dict__.get("SubmitResult") or _SubmitResultShim(
            version_id=self._version_id, created=self._created
        )


class _SubmitResultShim:
    def __init__(self, *, version_id: str, created: bool) -> None:
        self.version_id = version_id
        self.created = created


@pytest.fixture()
def recorder(monkeypatch) -> _SubmitRecorder:
    rec = _SubmitRecorder()

    async def _stub(conn, **kwargs):
        rec.calls.append({"conn": conn, **kwargs})
        return _SubmitResultShim(version_id="v-fake-1", created=True)

    monkeypatch.setattr(sd, "submit_quarantined_version", _stub, raising=True)
    return rec


def _steps(n: int) -> list[_FakeStep]:
    return [
        _FakeStep(
            idempotency_key=f"step-{i}",
            output={"summary": f"completed stage {i}"},
        )
        for i in range(n)
    ]


def test_is_enabled_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    assert sd.is_enabled() is False


@pytest.mark.parametrize("level", ["", "off", " l3 "])
def test_is_enabled_rejects_non_l1_levels(monkeypatch, level: str) -> None:
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", level)
    assert sd.is_enabled() is False


@pytest.mark.parametrize("level", ["l1", "l1+l3", "all"])
def test_is_enabled_accepts_l1_levels(monkeypatch, level: str) -> None:
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", level)
    assert sd.is_enabled() is True


def test_should_distill_requires_success() -> None:
    run = _FakeRun(status="failed", metadata={"tool_calls": 9, "iterations": 9})
    assert sd.should_distill(run, _steps(9)) is False


def test_should_distill_rejects_below_both_thresholds() -> None:
    run = _FakeRun(metadata={"tool_calls": 5, "iterations": 3})
    assert sd.should_distill(run, _steps(3)) is False


def test_should_distill_on_tool_call_threshold() -> None:
    run = _FakeRun(metadata={"tool_calls": 6, "iterations": 1})
    assert sd.should_distill(run, _steps(1)) is True


def test_should_distill_on_iteration_threshold() -> None:
    run = _FakeRun(metadata={"tool_calls": 0, "iterations": 4})
    assert sd.should_distill(run, _steps(1)) is True


def test_step_count_is_iteration_fallback() -> None:
    run = _FakeRun(metadata={"tool_calls": 0})
    stats = sd.trajectory_stats(run, _steps(4))
    assert stats.iterations == 4
    assert sd.should_distill(run, _steps(4)) is True


def test_non_dict_metadata_falls_back_to_steps() -> None:
    run = _FakeRun(metadata=None)
    run.metadata = "not-a-dict"  # type: ignore[assignment]
    stats = sd.trajectory_stats(run, _steps(3))
    assert stats.tool_calls == 0
    assert stats.iterations == 3
    assert stats.success is True


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"tool_call_count": "7"}, 7),
        ({"total_tool_calls": 7.9}, 7),
        ({"tool_calls": ["a", "b", "c"]}, 3),
        ({"tool_calls": {"count": "8"}}, 8),
    ],
)
def test_tool_call_metadata_coercions(metadata: dict[str, Any], expected: int) -> None:
    run = _FakeRun(metadata=metadata)
    assert sd.trajectory_stats(run, []).tool_calls == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"iteration_count": "4"}, 4),
        ({"total_iterations": 5.8}, 5),
    ],
)
def test_iteration_metadata_coercions(metadata: dict[str, Any], expected: int) -> None:
    run = _FakeRun(metadata=metadata)
    assert sd.trajectory_stats(run, []).iterations == expected


def test_bool_counts_are_ignored() -> None:
    run = _FakeRun(metadata={"tool_calls": True, "iterations": False})
    stats = sd.trajectory_stats(run, _steps(2))
    assert stats.tool_calls == 0
    assert stats.iterations == 2


def test_success_metadata_overrides_completed_status() -> None:
    run = _FakeRun(status="completed", metadata={"success": False})
    assert sd.trajectory_stats(run, _steps(6)).success is False


def test_success_metadata_overrides_failed_status() -> None:
    run = _FakeRun(status="failed", metadata={"success": True})
    assert sd.trajectory_stats(run, _steps(6)).success is True


def test_step_output_tool_calls_are_summed_when_metadata_absent() -> None:
    run = _FakeRun()
    steps = [
        _FakeStep("a", output={"tool_calls": 2}),
        _FakeStep("b", output={"tool_call_count": 4}),
    ]
    stats = sd.trajectory_stats(run, steps)
    assert stats.tool_calls == 6
    assert sd.should_distill(run, steps) is True


def test_step_output_json_string_is_counted() -> None:
    run = _FakeRun()
    steps = [
        _FakeStep("a", output='{"tool_calls": 2}'),
        _FakeStep("b", output='{"tool_call_count": 3}'),
    ]
    assert sd.trajectory_stats(run, steps).tool_calls == 5


def test_build_markdown_contains_review_queue_metadata() -> None:
    run = _FakeRun(
        metadata={
            "tenant_id": "t-acme",
            "task_id": "task-acme",
            "tool_calls": 8,
            "iterations": 2,
        }
    )
    skill_name, markdown = sd.build_markdown(run, _steps(2))
    assert skill_name == "auto-architect-blueprint"
    assert "source: architect_guild" in markdown
    assert "status: draft" in markdown
    assert "tool_call_count: 8" in markdown
    assert "source_task_id: 'task-acme'" in markdown
    assert "## Human Review Notes" in markdown


def test_build_markdown_uses_tenant_override() -> None:
    run = _FakeRun(metadata={"tenant_id": "t-from-run", "tool_calls": 6})
    _, markdown = sd.build_markdown(run, _steps(1), tenant_id="t-override")
    assert "tenant_id: 't-override'" in markdown


def test_build_markdown_uses_source_task_id_alias() -> None:
    run = _FakeRun(metadata={"source_task_id": "task-source", "tool_calls": 6})
    _, markdown = sd.build_markdown(run, _steps(1))
    assert "source_task_id: 'task-source'" in markdown
    assert "- source task: `task-source`" in markdown


def test_build_markdown_empty_steps_fallback() -> None:
    run = _FakeRun(metadata={"tool_calls": 6})
    _, markdown = sd.build_markdown(run, [])
    assert "- _(trajectory steps unavailable)_" in markdown


def test_build_markdown_limits_step_summary() -> None:
    run = _FakeRun(metadata={"tool_calls": 6})
    _, markdown = sd.build_markdown(run, _steps(14))
    assert "`step-11`" in markdown
    assert "`step-12`" not in markdown
    assert "2 additional trajectory step(s) omitted" in markdown


def test_build_markdown_step_error_wins_over_output() -> None:
    run = _FakeRun(metadata={"tool_calls": 6})
    steps = [_FakeStep("bad", output={"summary": "ignored"}, error="boom\ntrace")]
    _, markdown = sd.build_markdown(run, steps)
    assert "`bad` -- failed: boom trace" in markdown
    assert "ignored" not in markdown


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"status": "ok"}, "`s` -- ok"),
        ({"message": "done"}, "`s` -- done"),
    ],
)
def test_build_markdown_step_detail_fallbacks(output: Any, expected: str) -> None:
    run = _FakeRun(metadata={"tool_calls": 6})
    _, markdown = sd.build_markdown(run, [_FakeStep("s", output=output)])
    assert expected in markdown


def test_build_markdown_slugifies_kind() -> None:
    run = _FakeRun(kind="Architect Guild: Debug++", metadata={"tool_calls": 6})
    skill_name, markdown = sd.build_markdown(run, _steps(1))
    assert skill_name == "auto-architect-guild-debug"
    assert "# Skill: Architect Guild: Debug++" in markdown


def test_build_markdown_kind_fallback_slug() -> None:
    run = _FakeRun(kind="!!!", metadata={"tool_calls": 6})
    skill_name, _ = sd.build_markdown(run, _steps(1))
    assert skill_name == "auto-trajectory-skill"


# ━━ U4-I redirect assertions (replace legacy sink assertions) ━━━━━━━


@pytest.mark.asyncio
async def test_distill_submits_quarantined_version(recorder) -> None:
    conn = _FakeConn()
    run = _FakeRun(
        metadata={
            "tenant_id": "t-acme",
            "task_id": "task-acme",
            "tool_calls": 6,
            "iterations": 1,
        }
    )
    result = await sd.distill(run, _steps(2), conn=conn)

    assert result.written is True
    assert result.draft is not None
    assert result.draft.id == "v-fake-1"
    assert result.draft.tenant_id == "t-acme"
    assert result.draft.source_task_id == "task-acme"
    # Legacy assertion retargeted: the substrate row is quarantined,
    # not the auto_distilled_skills "draft" status.
    assert result.draft.status == "quarantined"

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["kind"] == "skill"
    assert call["audience"] == "tenant"
    assert call["tenant_id"] == "t-acme"
    assert call["created_by"] == "skill_distiller"
    payload = call["payload"]
    # A2 record shape: scope + procedure_steps come from the fixed
    # template; the parser rejects an empty/unparseable draft.
    assert payload["scope"].startswith("Use this draft")
    assert len(payload["procedure_steps"]) >= 1


@pytest.mark.asyncio
async def test_distill_tenant_argument_wins_over_run_metadata(
    recorder,
) -> None:
    conn = _FakeConn()
    run = _FakeRun(metadata={"tenant_id": "t-run", "tool_calls": 6})
    result = await sd.distill(run, _steps(1), tenant_id="t-arg", conn=conn)
    assert result.draft is not None
    assert result.draft.tenant_id == "t-arg"
    # The submit call carries the same tenant_id.
    assert recorder.calls[0]["tenant_id"] == "t-arg"
    # Markdown template still reflects the override.
    assert "tenant_id: 't-arg'" in result.draft.markdown_content


@pytest.mark.asyncio
async def test_distill_returns_producer_version_id(monkeypatch) -> None:
    """Idempotent re-observation: producer returned created=False +
    an existing id → the distiller surfaces both without extra writes."""
    async def _stub(conn, **kwargs):
        return _SubmitResultShim(version_id="v-existing", created=False)

    monkeypatch.setattr(sd, "submit_quarantined_version", _stub, raising=True)
    conn = _FakeConn()
    run = _FakeRun(metadata={"tenant_id": "t-run", "tool_calls": 6})
    result = await sd.distill(run, _steps(1), conn=conn)
    assert result.written is True
    assert result.draft is not None
    assert result.draft.id == "v-existing"


@pytest.mark.asyncio
async def test_distill_emits_audit_log(recorder, monkeypatch) -> None:
    from backend import audit as _audit
    from backend.db_context import current_tenant_id, set_tenant_id

    captured: list[dict[str, Any]] = []

    async def fake_log(**kwargs: Any) -> None:
        captured.append({**kwargs, "tenant_context": current_tenant_id()})

    monkeypatch.setattr(_audit, "log", fake_log, raising=True)
    set_tenant_id("t-prior")
    try:
        conn = _FakeConn()
        run = _FakeRun(
            metadata={
                "tenant_id": "t-acme",
                "task_id": "task-audit",
                "tool_calls": 6,
                "iterations": 1,
            }
        )
        result = await sd.distill(run, _steps(2), conn=conn)
    finally:
        assert current_tenant_id() == "t-prior"
        set_tenant_id(None)

    assert result.written is True
    assert result.draft is not None
    assert len(captured) == 1
    row = captured[0]
    assert row["tenant_context"] == "t-acme"
    assert row["action"] == "skill_distilled"
    # U4-I: entity_kind retargeted from "auto_distilled_skill" →
    # "learned_item_version"; entity_id is the substrate version id.
    assert row["entity_kind"] == "learned_item_version"
    assert row["entity_id"] == result.draft.id
    assert row["actor"] == "system:skill-distiller"
    assert row["after"]["skill_name"] == "auto-architect-blueprint"
    assert row["after"]["source_task_id"] == "task-audit"
    assert row["after"]["tool_calls"] == 6
    assert len(row["after"]["markdown_sha256"]) == 64


@pytest.mark.asyncio
async def test_distill_swallows_audit_log_error(recorder, monkeypatch) -> None:
    from backend import audit as _audit
    from backend.db_context import current_tenant_id, set_tenant_id

    async def boom(**kwargs: Any) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(_audit, "log", boom, raising=True)
    set_tenant_id("t-prior")
    try:
        conn = _FakeConn()
        run = _FakeRun(metadata={"tenant_id": "t-acme", "tool_calls": 6})
        result = await sd.distill(run, _steps(1), conn=conn)
    finally:
        assert current_tenant_id() == "t-prior"
        set_tenant_id(None)

    assert result.written is True
    assert result.draft is not None


@pytest.mark.asyncio
async def test_distill_skips_below_threshold(recorder) -> None:
    conn = _FakeConn()
    run = _FakeRun(metadata={"tool_calls": 5, "iterations": 3})
    result = await sd.distill(run, _steps(1), conn=conn)
    assert result.written is False
    assert "below threshold" in result.skipped_reason
    # Threshold gate happens BEFORE the substrate submit.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_distill_skips_when_scrub_hits_exceed_safety_threshold(
    recorder,
) -> None:
    conn = _FakeConn()
    run = _FakeRun(metadata={"tenant_id": "t-acme", "tool_calls": 6})
    steps = [
        _FakeStep(
            f"sensitive-{i}",
            output={
                "summary": (
                    f"user{i}a@example.test user{i}b@example.test "
                    f"user{i}c@example.test"
                )
            },
        )
        for i in range(9)
    ]
    result = await sd.distill(
        run,
        steps,
        conn=conn,
    )
    assert result.written is False
    assert result.draft is None
    assert result.hits["email"] == 27
    assert result.skipped_reason == "too many secret hits (27)"
    # Scrub gate happens BEFORE the substrate submit.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_architect_hook_honours_l1_gate(recorder, monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    conn = _FakeConn()
    run = _FakeRun(metadata={"tool_calls": 9, "iterations": 9})
    result = await sd.architect_guild_hook(run, _steps(2), conn=conn)
    assert result.written is False
    assert result.skipped_reason == "disabled"
    assert recorder.calls == []

    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l1")
    result = await sd.architect_guild_hook(run, _steps(2), conn=conn)
    assert result.written is True
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_distill_scrubs_secret_before_insert(recorder) -> None:
    conn = _FakeConn()
    run = _FakeRun(metadata={"tenant_id": "t-acme", "tool_calls": 6})
    steps = [
        _FakeStep(
            "leaky",
            output={
                "summary": (
                    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
                    "was rotated"
                )
            },
        )
    ]
    result = await sd.distill(run, steps, conn=conn)
    assert result.written is True
    assert result.draft is not None
    assert "[GITHUB_PAT]" in result.draft.markdown_content
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in (
        result.draft.markdown_content
    )
