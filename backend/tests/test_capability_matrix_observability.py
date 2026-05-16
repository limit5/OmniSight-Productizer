"""OP-1148 — capability matrix safe-default observability tests."""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from backend import metrics as m
from backend.agents import capability_matrix


def _write_matrix(path: Path, body: str) -> capability_matrix.CapabilityMatrix:
    path.write_text(textwrap.dedent(body).strip(), encoding="utf-8")
    return capability_matrix.load_capability_matrix(path)


def _unexpected_matrix(path: Path) -> capability_matrix.CapabilityMatrix:
    return _write_matrix(
        path,
        """
        schema_version: 1
        capabilities:
          - code_edit
          - run_tests
          - run_lint
          - gerrit_push
          - jira_update
          - mcp_search
          - memory_recall
        read_only_default:
          - mcp_search
          - memory_recall
        expected_coverage:
          Story:
            backend: [M]
            tooling: [S]
        matrix:
          Story:
            docs:
              S: [code_edit, run_lint, jira_update, mcp_search, memory_recall]
            tooling:
              M: [code_edit, run_tests, run_lint, gerrit_push, jira_update, mcp_search, memory_recall]
        """,
    )


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_legitimate_fallback_returns_safe_default_without_warn_or_counter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    m.reset_for_tests()
    matrix = _unexpected_matrix(tmp_path / "matrix.yaml")

    with caplog.at_level(logging.WARNING, logger="backend.agents.capability_matrix"):
        caps = matrix.resolve("Story", "unknown-area", "S", ticket_id="OP-1148")

    assert caps == matrix.read_only_default
    assert not any(
        "capability_matrix.fallback_used" in rec.message for rec in caplog.records
    )
    samples = [
        sample for family in m.REGISTRY.collect()
        if family.name == "omnisight_capability_matrix_unexpected_fallback"
        for sample in family.samples
        if sample.name == "omnisight_capability_matrix_unexpected_fallback_total"
    ]
    assert samples == []


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_unexpected_miss_warns_and_increments_counter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    m.reset_for_tests()
    matrix = _unexpected_matrix(tmp_path / "matrix.yaml")

    with caplog.at_level(logging.WARNING, logger="backend.agents.capability_matrix"):
        caps = matrix.resolve("Story", "backend", "M", ticket_id="OP-1148")

    assert caps == matrix.read_only_default
    event = next(
        rec for rec in caplog.records
        if "capability_matrix.fallback_used" in rec.message
    )
    assert event.area == "backend"
    assert event.tier == "M"
    assert event.issuetype == "Story"
    assert event.missing_keys == ["area"]
    assert event.ticket_id == "OP-1148"
    body = m.render_exposition()[0].decode()
    assert (
        'omnisight_capability_matrix_unexpected_fallback_total'
        '{area="backend",issuetype="Story",tier="M"} 1.0'
    ) in body


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_strict_mode_raises_unexpected_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    m.reset_for_tests()
    matrix = _unexpected_matrix(tmp_path / "matrix.yaml")
    monkeypatch.setenv(capability_matrix.STRICT_FALLBACK_ENV, "1")

    with pytest.raises(capability_matrix.CapabilityMatrixUnexpectedMiss) as exc_info:
        matrix.resolve("Story", "backend", "M", ticket_id="OP-1148")

    err = exc_info.value
    assert err.ticket_type == "Story"
    assert err.area == "backend"
    assert err.tier == "M"
    assert err.default_capabilities == matrix.read_only_default


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_counter_labels_share_series_and_stay_bounded(tmp_path: Path) -> None:
    m.reset_for_tests()
    matrix = _unexpected_matrix(tmp_path / "matrix.yaml")

    matrix.resolve("Story", "backend", "M")
    matrix.resolve("Story", "backend", "M")
    matrix.resolve("Story", "tooling", "S")

    samples = [
        sample for family in m.REGISTRY.collect()
        if family.name == "omnisight_capability_matrix_unexpected_fallback"
        for sample in family.samples
        if sample.name == "omnisight_capability_matrix_unexpected_fallback_total"
    ]
    by_labels = {
        (sample.labels["area"], sample.labels["tier"], sample.labels["issuetype"]): sample.value
        for sample in samples
    }
    assert by_labels[("backend", "M", "Story")] == 2.0
    assert by_labels[("tooling", "S", "Story")] == 1.0
    for label in ("area", "tier", "issuetype"):
        assert len({sample.labels[label] for sample in samples}) <= 10


def test_expected_coverage_rejects_high_cardinality_labels(tmp_path: Path) -> None:
    areas = "\n".join(f"    area{i}: [M]" for i in range(11))
    p = tmp_path / "matrix.yaml"
    body = f"""\
schema_version: 1
capabilities:
  - mcp_search
  - memory_recall
read_only_default:
  - mcp_search
  - memory_recall
expected_coverage:
  Story:
{areas}
matrix:
  Story:
    docs:
      M: [mcp_search, memory_recall]
"""

    with pytest.raises(capability_matrix.CapabilityMatrixError, match="cardinality"):
        _write_matrix(p, body)
