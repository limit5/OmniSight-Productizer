"""C9 Mastra Observational Memory spike harness tests (OP-859)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "spike_c9_mastra_compare.py"
_SPEC = importlib.util.spec_from_file_location("spike_c9_mastra_compare", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
c9 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = c9
_SPEC.loader.exec_module(c9)


def test_mastra_install_verification_uses_expected_packages() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="1.2.3\n", stderr="")

    versions = c9.verify_mastra_install(runner)

    assert versions == {
        "@mastra/memory": "1.2.3",
        "@mastra/core": "1.2.3",
        "mastra": "1.2.3",
    }
    assert calls == [
        ("npm", "view", "@mastra/memory", "version"),
        ("npm", "view", "@mastra/core", "version"),
        ("npm", "view", "mastra", "version"),
    ]


def test_mastra_install_verification_raises_catalog_error() -> None:
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")

    with pytest.raises(c9.MastraInstallFailed, match="@mastra/memory"):
        c9.verify_mastra_install(runner)


def test_ingestion_happy_path_loads_progress_and_jira_changelogs(tmp_path: Path) -> None:
    progress = tmp_path / "progress.txt"
    progress.write_text(
        "\n".join([
            json.dumps({
                "ticket_key": "OP-830",
                "created_at": "2026-05-10T10:00:00Z",
                "summary": "same tool args error class triggered loop reset",
                "tags": ["loop-detector"],
            }),
            json.dumps({
                "ticket_key": "OP-847",
                "created_at": "2026-05-10T11:00:00Z",
                "summary": "thin acceptance criteria caused rubric Goodhart warning",
                "tags": ["goodhart"],
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    changelogs = tmp_path / "jira.json"
    changelogs.write_text(
        json.dumps({
            "issues": [{
                "key": "OP-843",
                "changelog": {
                    "histories": [{
                        "created": "2026-05-10T12:00:00Z",
                        "items": [{
                            "field": "Decision",
                            "fromString": "replace",
                            "toString": "complement not replace vendor claim",
                        }],
                    }],
                },
            }],
        }),
        encoding="utf-8",
    )

    corpus = c9.load_corpus(progress_path=progress, jira_changelogs_path=changelogs)
    adapter = c9.MastraOMAdapter()
    adapter.ingest(corpus.records)
    hits = adapter.query("find prior ticket about rubric Goodhart risk", k=2)

    assert corpus.source == "artifact-sample"
    assert len(corpus.records) == 3
    assert len(adapter.records) == 3
    assert hits[0].ticket_key == "OP-847"


def test_comparison_metric_correctness_on_built_in_sample() -> None:
    result = c9.evaluate(c9.built_in_corpus(), k=3)
    rows = {row["adapter"]: row for row in result["results"]}

    assert rows["c1_memory_tool"]["recall_at_k"] == pytest.approx(1.0)
    assert rows["c3_cognee"]["recall_at_k"] == pytest.approx(1.0)
    assert rows["mastra_observational_memory"]["recall_at_k"] == pytest.approx(1.0)
    assert rows["c1_memory_tool"]["setup_loc"] < rows["mastra_observational_memory"]["setup_loc"]
    assert result["decision"] == "reject"
