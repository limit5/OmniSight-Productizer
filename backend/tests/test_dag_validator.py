"""Phase 56-DAG-A — schema + semantic validator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticError

from backend import dag_validator as dv
from backend.dag_schema import DAG, SCHEMA_VERSION, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures: minimal valid DAG factories
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _t(task_id="T1", required_tier="t1", toolchain="cmake",
       inputs=None, expected_output="build/out.bin",
       depends_on=None, **kw):
    return Task(
        task_id=task_id,
        description=f"task {task_id}",
        required_tier=required_tier,
        toolchain=toolchain,
        inputs=inputs or [],
        expected_output=expected_output,
        depends_on=depends_on or [],
        **kw,
    )


def _dag(tasks: list[Task], dag_id="REQ-1") -> DAG:
    return DAG(dag_id=dag_id, tasks=tasks)


@pytest.fixture(autouse=True)
def _reset():
    dv.reload_tier_rules_for_tests()
    yield
    dv.reload_tier_rules_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schema rules
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_task_id_must_be_alnum_dash_underscore():
    with pytest.raises(PydanticError, match="alphanumeric"):
        _t(task_id="bad id!")


def test_task_cannot_depend_on_itself():
    with pytest.raises(PydanticError, match="depend on itself"):
        _t(task_id="T1", depends_on=["T1"])


def test_task_depends_on_must_have_no_dupes():
    with pytest.raises(PydanticError, match="duplicates"):
        _t(depends_on=["X", "X"])


def test_dag_must_have_at_least_one_task():
    with pytest.raises(PydanticError, match="at least one"):
        DAG(dag_id="empty", tasks=[])


def test_unknown_schema_version_rejected():
    with pytest.raises(PydanticError, match="schema_version"):
        DAG(dag_id="x", schema_version=99, tasks=[_t()])


def test_required_tier_must_be_known():
    with pytest.raises(PydanticError):
        _t(required_tier="t99")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Happy path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_minimal_valid_dag_passes():
    dag = _dag([_t(task_id="T1", expected_output="build/a.bin")])
    res = dv.validate(dag)
    assert res.ok, res.summary()


def test_two_step_dag_with_dep_passes():
    dag = _dag([
        _t(task_id="T1", required_tier="t1", toolchain="cmake",
           expected_output="build/i2c_driver.bin"),
        _t(task_id="T2", required_tier="t3", toolchain="flash_board",
           inputs=["build/i2c_driver.bin"],
           expected_output="logs/evk_boot.log",
           depends_on=["T1"]),
    ])
    res = dv.validate(dag)
    assert res.ok, res.summary()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cycle / unknown_dep / duplicate_id
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_cycle_detected():
    a = _t(task_id="A", expected_output="x/a.bin", depends_on=["B"])
    b = _t(task_id="B", expected_output="x/b.bin", depends_on=["A"])
    res = dv.validate(_dag([a, b]))
    assert not res.ok
    assert any(e.rule == "cycle" for e in res.errors)


def test_unknown_dep_flagged():
    res = dv.validate(_dag([
        _t(task_id="T1", expected_output="x/a.bin", depends_on=["GHOST"]),
    ]))
    assert any(e.rule == "unknown_dep" and e.task_id == "T1" for e in res.errors)


def test_duplicate_task_id_flagged():
    res = dv.validate(_dag([
        _t(task_id="T1", expected_output="x/a.bin"),
        _t(task_id="T1", expected_output="x/b.bin"),
    ]))
    assert any(e.rule == "duplicate_id" for e in res.errors)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tier capability
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_t1_cannot_call_flash_board():
    res = dv.validate(_dag([
        _t(task_id="T1", required_tier="t1", toolchain="flash_board",
           expected_output="logs/x.log"),
    ]))
    err = next((e for e in res.errors if e.rule == "tier_violation"), None)
    assert err is not None
    assert "DENIED" in err.message
    assert "flash_board" in err.message


def test_t3_cannot_run_cmake():
    res = dv.validate(_dag([
        _t(task_id="T1", required_tier="t3", toolchain="cmake",
           expected_output="build/out.bin"),
    ]))
    assert any(e.rule == "tier_violation" for e in res.errors)


def test_unknown_toolchain_in_known_tier_is_flagged():
    res = dv.validate(_dag([
        _t(task_id="T1", required_tier="t1", toolchain="rebar3-magic",
           expected_output="build/out.bin"),
    ]))
    err = next((e for e in res.errors if e.rule == "tier_violation"), None)
    assert err is not None
    assert "not in allow-list" in err.message


def test_networked_tier_accepts_pip():
    res = dv.validate(_dag([
        _t(task_id="T1", required_tier="networked", toolchain="pip",
           expected_output="data/dataset.tar.gz"),
    ]))
    assert res.ok, res.summary()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  I/O entity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("good_output", [
    "build/i2c_driver.bin",
    "logs/evk_boot.log",
    "src/foo/bar.so",
    "git:abc1234",
    "git:1234567890abcdef1234567890abcdef12345678",
    "issue:OMNI-123",
    "issue:GH-42",
])
def test_io_entity_accepts_valid_forms(good_output):
    res = dv.validate(_dag([
        _t(task_id="T1", expected_output=good_output),
    ]))
    assert not any(e.rule == "io_entity" for e in res.errors), res.summary()


@pytest.mark.parametrize("bad_output", [
    "complete the build",        # English sentence
    "system optimised",
    "binary",                    # no path
    "git:zzz",                   # not hex
    "issue:",                    # empty
    "/abs/file/with spaces.bin", # contains space
])
def test_io_entity_rejects_non_entity(bad_output):
    res = dv.validate(_dag([
        _t(task_id="T1", expected_output=bad_output),
    ]))
    assert any(e.rule == "io_entity" for e in res.errors), res.summary()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dep closure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_input_must_come_from_upstream_or_external():
    # T1 declares an input that no one produces and isn't external.
    res = dv.validate(_dag([
        _t(task_id="T1", inputs=["docs/i2c_spec.pdf"],
           expected_output="build/x.bin"),
    ]))
    assert any(e.rule == "dep_closure" for e in res.errors)


def test_external_prefix_passes():
    res = dv.validate(_dag([
        _t(task_id="T1", inputs=["external:docs/i2c_spec.pdf"],
           expected_output="build/x.bin"),
        _t(task_id="T2", inputs=["user:src/hal_interface.h"],
           expected_output="build/y.bin"),
    ]))
    assert not any(e.rule == "dep_closure" for e in res.errors), res.summary()


def test_input_provided_by_upstream_passes():
    res = dv.validate(_dag([
        _t(task_id="T1", expected_output="build/i2c_driver.bin"),
        _t(task_id="T2",
           inputs=["build/i2c_driver.bin"],
           expected_output="logs/evk_boot.log",
           depends_on=["T1"], required_tier="t3", toolchain="flash_board"),
    ]))
    assert res.ok, res.summary()


def test_input_from_transitive_upstream_passes():
    res = dv.validate(_dag([
        _t(task_id="A", expected_output="build/A.bin"),
        _t(task_id="B", inputs=["build/A.bin"],
           expected_output="build/B.bin", depends_on=["A"]),
        _t(task_id="C", inputs=["build/A.bin"],  # A is transitively above C via B
           expected_output="logs/C.log", depends_on=["B"],
           required_tier="t3", toolchain="flash_board"),
    ]))
    assert not any(e.rule == "dep_closure" for e in res.errors), res.summary()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MECE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_two_tasks_same_output_fails_mece():
    res = dv.validate(_dag([
        _t(task_id="A", expected_output="build/dup.bin"),
        _t(task_id="B", expected_output="build/dup.bin"),
    ]))
    assert any(e.rule == "mece" for e in res.errors)


def test_unanimous_output_overlap_ack_passes_mece():
    res = dv.validate(_dag([
        _t(task_id="A", expected_output="reports/parallel.json",
           output_overlap_ack=True),
        _t(task_id="B", expected_output="reports/parallel.json",
           output_overlap_ack=True),
    ]))
    assert not any(e.rule == "mece" for e in res.errors), res.summary()


def test_partial_overlap_ack_still_fails_mece():
    res = dv.validate(_dag([
        _t(task_id="A", expected_output="x/y.json", output_overlap_ack=True),
        _t(task_id="B", expected_output="x/y.json"),  # no ack
    ]))
    assert any(e.rule == "mece" for e in res.errors)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validator returns ALL errors, not first-fail
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_all_errors_collected():
    """One bad DAG, multiple distinct rule violations — all surface."""
    res = dv.validate(_dag([
        _t(task_id="A", required_tier="t3", toolchain="cmake",   # tier_violation
           expected_output="just text",                          # io_entity
           inputs=["unknown.bin"],                                # dep_closure
           depends_on=["GHOST"]),                                 # unknown_dep
    ]))
    rules = {e.rule for e in res.errors}
    assert {"tier_violation", "io_entity", "dep_closure", "unknown_dep"}.issubset(rules)


def test_validation_result_summary_string():
    res = dv.validate(_dag([_t(task_id="T1", expected_output="x/a.bin")]))
    assert res.summary() == "DAG validation: OK"
    bad = dv.validate(_dag([
        _t(task_id="X", required_tier="t1", toolchain="flash_board",
           expected_output="bad output"),
    ]))
    assert "FAILED" in bad.summary()
    assert "tier_violation" in bad.summary() or "io_entity" in bad.summary()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Metric publish
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_validate_increments_pass_metric_on_success():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    dv.validate(_dag([_t(task_id="T1", expected_output="x/a.bin")]))
    samples = list(m.dag_validation_total.collect()[0].samples)
    passed = [s for s in samples
              if s.labels.get("result") == "passed" and s.name.endswith("_total")]
    assert passed and passed[0].value >= 1


def test_validate_increments_per_rule_metric_on_failure():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    dv.validate(_dag([
        _t(task_id="A", required_tier="t1", toolchain="flash_board",
           expected_output="bad text"),
    ]))
    samples = list(m.dag_validation_error_total.collect()[0].samples)
    rules_seen = {s.labels.get("rule")
                  for s in samples if s.name.endswith("_total") and s.value > 0}
    assert "tier_violation" in rules_seen
