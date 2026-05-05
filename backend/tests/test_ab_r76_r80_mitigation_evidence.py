"""AB DoD -- R76-R80 mitigation evidence drift guard."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "ops" / "ab_r76_r80_mitigation_evidence.md"
ADR = PROJECT_ROOT / "docs" / "operations" / "anthropic-api-migration-and-batch-mode.md"
RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "anthropic-api-migration-runbook.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing AB R76-R80 evidence file: {path}"
    return path.read_text(encoding="utf-8")


def _normalized_lower(path: Path) -> str:
    return " ".join(_read(path).lower().split())


def test_ab_r76_r80_evidence_doc_exists_and_defines_scope() -> None:
    body = _normalized_lower(EVIDENCE)

    required = [
        "ab r76-r80 mitigation evidence",
        "r76 api key leak / bill-burn mitigation",
        "r77 batch 24h completion sla / lane separation mitigation",
        "r78 rate-limit retry / dlq mitigation",
        "r79 tool schema drift mitigation",
        "r80 tenant-aware batch task/result routing mitigation",
        "current status is `dev-only`",
        "does not claim the one-week api-mode dogfood",
        "first 100-task batch",
        "30-day subscription fallback disable",
    ]

    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"AB R76-R80 evidence doc missing scope terms: {missing}"


def test_r76_r80_matrix_names_all_risks_and_artifacts() -> None:
    body = _read(EVIDENCE)

    for risk_id in ["R76", "R77", "R78", "R79", "R80"]:
        assert risk_id in body

    for phrase in [
        "backend/agents/rate_limiter.py::WorkspaceConfig.__repr__",
        "backend/agents/anthropic_mode_manager.py",
        "docs/ops/anthropic-api-migration-runbook.md",
        "backend/agents/batch_dispatcher.py::submit_in_lane",
        "backend/agents/rate_limiter.py::classify_error",
        "RetryableExecutor",
        "InMemoryDeadLetterQueue",
        "backend/agents/tool_schemas.py::TOOL_SCHEMA_VERSION",
        "to_toolsearch_schemas()",
        "backend/agents/batch_client.py::BatchRequest",
        "backend/agents/batch_dispatcher.py::chunk_by_model_tools",
        "_ActiveBatch.callbacks",
    ]:
        assert phrase in body


def test_source_adr_and_runbook_still_define_r76_r80_controls() -> None:
    adr = _read(ADR)
    runbook = _read(RUNBOOK)

    for risk_id in ["R76", "R77", "R78", "R79", "R80"]:
        assert risk_id in adr

    for phrase in [
        "AS Token Vault + KS.1 envelope encryption",
        "real-time vs batch lane",
        "exponential backoff + retry",
        "ToolSearch",
        "tenant_id",
    ]:
        assert phrase in adr

    for phrase in [
        "Billing → Usage Limits",
        "防 R76",
        "AB.7 retry policy",
        "DLQ",
        "30 天 grace",
    ]:
        assert phrase in runbook


def test_code_contracts_for_r76_r80_are_present() -> None:
    batch_client = _read(PROJECT_ROOT / "backend" / "agents" / "batch_client.py")
    batch_dispatcher = _read(
        PROJECT_ROOT / "backend" / "agents" / "batch_dispatcher.py"
    )
    rate_limiter = _read(PROJECT_ROOT / "backend" / "agents" / "rate_limiter.py")
    tool_schemas = _read(PROJECT_ROOT / "backend" / "agents" / "tool_schemas.py")

    assert "api_key=<redacted>" in rate_limiter
    assert "status_code in (429, 529)" in rate_limiter
    assert "await self.dlq.deposit(entry)" in rate_limiter

    assert 'TOOL_SCHEMA_VERSION = "1.0.0"' in tool_schemas
    assert '"schema_version": TOOL_SCHEMA_VERSION' in tool_schemas

    assert "tenant_id: str | None = None" in batch_client
    assert "tenant_id=previous.tenant_id if previous else None" in batch_client
    assert "task.tenant_id, task.model, task.tools_signature" in batch_dispatcher
    assert "(t.tenant_id, t.task_id): t.callback" in batch_dispatcher


def test_r80_duplicate_task_id_tenant_tests_are_registered() -> None:
    batch_client_tests = _read(PROJECT_ROOT / "backend" / "tests" / "test_batch_client.py")
    batch_dispatcher_tests = _read(
        PROJECT_ROOT / "backend" / "tests" / "test_batch_dispatcher.py"
    )

    assert "test_submit_batch_preserves_tenant_id_mapping" in batch_client_tests
    assert "test_dispatcher_routes_duplicate_task_ids_by_tenant" in batch_dispatcher_tests
    assert "same-task" in batch_dispatcher_tests
