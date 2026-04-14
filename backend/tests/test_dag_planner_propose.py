"""Phase 56-DAG-C-S1 — dag_planner.propose_mutation + parse_response."""

from __future__ import annotations

import pytest

from backend import dag_planner as dp
from backend.dag_schema import DAG, Task
from backend.dag_validator import ValidationError as DagErr


def _t(task_id="T1", required_tier="t1", toolchain="cmake",
       expected_output="build/out.bin", **kw):
    return Task(task_id=task_id, description=f"t {task_id}",
                required_tier=required_tier, toolchain=toolchain,
                expected_output=expected_output, **kw)


def _dag(tasks=None, dag_id="REQ-1"):
    return DAG(dag_id=dag_id, tasks=tasks or [_t()])


@pytest.fixture(autouse=True)
def _reset():
    dp._reset_prompt_cache_for_tests()
    yield
    dp._reset_prompt_cache_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  load_system_prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_system_prompt_is_loaded_and_cached():
    s1 = dp.load_system_prompt()
    s2 = dp.load_system_prompt()
    assert "Lead Orchestrator" in s1
    assert s1 is s2  # cached — same object identity
    # Front-matter is stripped.
    assert not s1.startswith("---")


def test_system_prompt_fallback_when_file_missing(monkeypatch):
    dp._reset_prompt_cache_for_tests()
    fake = dp._PROJECT_ROOT / "no-such-file.md"
    monkeypatch.setattr(dp, "ORCHESTRATOR_PROMPT_PATH", fake)
    s = dp.load_system_prompt()
    assert "Orchestrator" in s
    assert "JSON" in s


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  build_user_prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_build_user_prompt_includes_dag_json_and_all_errors():
    dag = _dag()
    errs = [
        DagErr(rule="cycle", task_id=None, message="A↔B cycle"),
        DagErr(rule="tier_violation", task_id="T1",
               message="flash_board on t1"),
    ]
    body = dp.build_user_prompt(dag, errs)
    assert "PRIOR DAG" in body
    assert "REQ-1" in body  # dag_id appears in the JSON
    assert "VALIDATOR ERRORS" in body
    assert "cycle" in body
    assert "tier_violation" in body
    assert "flash_board on t1" in body
    # task_id: null for graph-level errors.
    assert "task_id: null" in body


def test_build_user_prompt_marks_empty_errors_as_bug():
    body = dp.build_user_prompt(_dag(), [])
    assert "planner bug" in body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _extract_json
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_extract_bare_json():
    src = '{"dag_id": "X", "tasks": []}'
    assert dp._extract_json(src) == src


def test_extract_strips_json_fence():
    src = 'Here you go:\n```json\n{"a": 1}\n```'
    assert dp._extract_json(src) == '{"a": 1}'


def test_extract_strips_bare_fence():
    src = '```\n{"a": 1}\n```'
    assert dp._extract_json(src) == '{"a": 1}'


def test_extract_brace_balance_when_no_fence():
    src = 'sure — {"k": {"nested": true}} trailing junk'
    assert dp._extract_json(src) == '{"k": {"nested": true}}'


def test_extract_empty_input_returns_empty():
    assert dp._extract_json("") == ""
    assert dp._extract_json("   ") == ""


def test_extract_non_json_passthrough():
    """Caller expects parse_response to raise — extraction should not
    silently invent structure."""
    assert dp._extract_json("no braces here") == "no braces here"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  parse_response
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _valid_response() -> str:
    return """{
      "schema_version": 1,
      "dag_id": "REQ-1",
      "tasks": [
        {
          "task_id": "T1",
          "description": "compile",
          "required_tier": "t1",
          "toolchain": "cmake",
          "inputs": [],
          "expected_output": "build/x.bin",
          "depends_on": []
        }
      ]
    }"""


def test_parse_response_happy():
    dag = dp.parse_response(_valid_response())
    assert dag.dag_id == "REQ-1"
    assert dag.tasks[0].toolchain == "cmake"


def test_parse_response_with_fence():
    wrapped = f"```json\n{_valid_response()}\n```"
    assert dp.parse_response(wrapped).dag_id == "REQ-1"


def test_parse_response_with_prose_prefix():
    wrapped = "Here is the corrected DAG:\n\n" + _valid_response()
    assert dp.parse_response(wrapped).dag_id == "REQ-1"


def test_parse_response_empty_raises():
    with pytest.raises(dp.OrchestratorResponseError, match="empty"):
        dp.parse_response("")


def test_parse_response_malformed_json_raises():
    with pytest.raises(dp.OrchestratorResponseError, match="not valid JSON"):
        dp.parse_response("{dag_id: no quotes}")


def test_parse_response_wrong_schema_raises():
    bad = '{"dag_id": "X"}'  # missing tasks
    with pytest.raises(dp.OrchestratorResponseError, match="DAG schema"):
        dp.parse_response(bad)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  propose_mutation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_propose_returns_new_dag_and_tokens():
    captured = {}

    async def ask(system, user):
        captured["system"] = system
        captured["user"] = user
        return (_valid_response(), 512)

    errs = [DagErr(rule="cycle", task_id=None, message="A↔B")]
    new_dag, toks = await dp.propose_mutation(_dag(), errs, ask_fn=ask)
    assert isinstance(new_dag, DAG)
    assert toks == 512
    # System prompt carries orchestrator role + JSON-only instruction.
    assert "Lead Orchestrator" in captured["system"]
    # User prompt carries BOTH the prior DAG and the cycle error.
    assert "REQ-1" in captured["user"]
    assert "cycle" in captured["user"]


@pytest.mark.asyncio
async def test_propose_empty_errors_rejected():
    async def ask(s, u):
        return (_valid_response(), 1)
    with pytest.raises(ValueError, match="no errors"):
        await dp.propose_mutation(_dag(), [], ask_fn=ask)


@pytest.mark.asyncio
async def test_propose_restores_dag_id_if_orchestrator_changes_it(caplog):
    """If the orchestrator hallucinates a new dag_id we force the
    original back — otherwise mutation chain linkage in Phase 56-DAG-B
    breaks."""
    import logging
    drift = _valid_response().replace('"REQ-1"', '"REQ-HALLUCINATED"')

    async def ask(s, u):
        return (drift, 1)

    caplog.set_level(logging.WARNING, logger="backend.dag_planner")
    new_dag, _ = await dp.propose_mutation(
        _dag(dag_id="REQ-1"),
        [DagErr(rule="cycle", task_id=None, message="x")],
        ask_fn=ask,
    )
    assert new_dag.dag_id == "REQ-1"
    assert any("changed dag_id" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_propose_bubbles_parse_error():
    async def ask(s, u):
        return ("this isn't JSON at all", 5)
    with pytest.raises(dp.OrchestratorResponseError):
        await dp.propose_mutation(
            _dag(),
            [DagErr(rule="cycle", task_id=None, message="x")],
            ask_fn=ask,
        )
