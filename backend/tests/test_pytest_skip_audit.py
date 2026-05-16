"""FX2.D6.3 runtime ``pytest.skip`` gate audit.

The 2026-05-06 deep audit flagged the suite's runtime skip volume as
debt. Keep the audit executable: every runtime ``pytest.skip`` /
``pytest.importorskip`` call in ``backend/tests`` must fit one of the
approved gate classes below, and the total count is pinned so additions
or removals force a deliberate re-audit.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_ROOT = _REPO_ROOT / "backend" / "tests"
_EXPECTED_RUNTIME_SKIP_GATES = 143


@dataclass(frozen=True)
class RuntimeSkipGate:
    path: Path
    lineno: int
    call_name: str
    reason: str

    @property
    def relpath(self) -> str:
        return self.path.relative_to(_REPO_ROOT).as_posix()

    @property
    def audit_text(self) -> str:
        return f"{self.relpath}:{self.lineno} {self.call_name} {self.reason}"


@dataclass(frozen=True)
class GateClass:
    name: str
    pattern: re.Pattern[str]


_APPROVED_GATE_CLASSES = (
    GateClass(
        "optional-python-dependency",
        re.compile(
            r"importorskip|"
            r"prometheus_client not installed|"
            r"psycopg2 not installed|"
            r"pyyaml not installed|"
            r"prom not installed|prom installed|"
            r"openai SDK not installed|"
            r"config module unavailable|"
            r"playwright is installed",
            re.IGNORECASE,
        ),
    ),
    GateClass(
        "external-tool-or-local-artifact",
        re.compile(
            r"TS twin|TS twin audit|"
            r"Node|node|"
            r"simulate\.sh|bash not available|"
            r"docker is not available|"
            r"slim not built locally|pull time|"
            r"optional service .* absent from compose|"
            r"skill-android not present|"
            r"no fingerprints in signers file|"
            r"Golden file created|"
            r"no urllib\.request\.Request|"
            r"whitelisted non-network script",
            re.IGNORECASE,
        ),
    ),
    GateClass(
        "database-or-live-service-environment",
        re.compile(
            r"SQLite .*DROP COLUMN|SQLite build lacks FTS5|"
            r"OMNI_TEST_PG_URL|PG-backed test skipped|"
            r"alembic upgrade head failed|"
            r"psycopg2 not installed|"
            r"OMNISIGHT_RAG_PGVECTOR_TEST_DSN|pgvector unavailable|"
            r"PG pool not initialised|"
            r"sandbox env is not configured|"
            r"live test skipped|"
            r"Gemini .*not supported|Gemini rejects",
            re.IGNORECASE,
        ),
    ),
    GateClass(
        "feature-landing-sentinel",
        re.compile(
            r"settings\.as_enabled|Settings\.as_enabled|pre-AS\.3\.1|"
            r"secrets router not available|"
            r"jira_prereq_audit\.py is a skeleton",
            re.IGNORECASE,
        ),
    ),
    GateClass(
        "empty-fixture-or-noop-path",
        re.compile(
            r"No NPI phases loaded|No milestones in first phase|"
            r"optional service .* absent from compose|"
            r"_NoOp stub not under test|"
            r"no fingerprints in signers file|"
            r"Golden file created|"
            r"no urllib\.request\.Request",
            re.IGNORECASE,
        ),
    ),
)


def _is_pytest_skip_call(node: ast.Call) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name):
        return None
    if func.value.id != "pytest":
        return None
    if func.attr not in {"skip", "importorskip"}:
        return None
    return func.attr


def _reason_for_call(node: ast.Call, call_name: str) -> str:
    parts: list[str] = []
    if call_name == "importorskip":
        parts.append("importorskip")
    if node.args:
        parts.append(ast.unparse(node.args[0]))
    for keyword in node.keywords:
        if keyword.arg == "reason":
            parts.append(f"reason={ast.unparse(keyword.value)}")
    return " ".join(parts) or "<no reason>"


def _collect_runtime_skip_gates() -> list[RuntimeSkipGate]:
    gates: list[RuntimeSkipGate] = []
    for path in sorted(_TEST_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _is_pytest_skip_call(node)
            if call_name is None:
                continue
            gates.append(
                RuntimeSkipGate(
                    path=path,
                    lineno=node.lineno,
                    call_name=call_name,
                    reason=_reason_for_call(node, call_name),
                )
            )
    return sorted(
        gates,
        key=lambda gate: (gate.relpath, gate.lineno, gate.call_name),
    )


def _class_for_gate(gate: RuntimeSkipGate) -> str | None:
    for gate_class in _APPROVED_GATE_CLASSES:
        if gate_class.pattern.search(gate.audit_text):
            return gate_class.name
    return None


def test_runtime_pytest_skip_gate_count_is_pinned() -> None:
    gates = _collect_runtime_skip_gates()

    assert len(gates) == _EXPECTED_RUNTIME_SKIP_GATES


def test_runtime_pytest_skip_gates_are_audited_into_known_classes() -> None:
    gates = _collect_runtime_skip_gates()
    unclassified = [
        gate.audit_text
        for gate in gates
        if _class_for_gate(gate) is None
    ]

    assert unclassified == []


def test_runtime_pytest_skip_gate_classes_remain_representative() -> None:
    gates = _collect_runtime_skip_gates()
    counts = {gate_class.name: 0 for gate_class in _APPROVED_GATE_CLASSES}
    for gate in gates:
        gate_class = _class_for_gate(gate)
        assert gate_class is not None, gate.audit_text
        counts[gate_class] += 1

    assert counts == {
        "optional-python-dependency": 63,
        "external-tool-or-local-artifact": 44,
        "database-or-live-service-environment": 20,
        "feature-landing-sentinel": 11,
        "empty-fixture-or-noop-path": 5,
    }
