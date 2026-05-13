"""OP-852 Cognee KG integration tests.

The 8 cases mirror the master plan §3.4 test plan:

1. ``test_initial_ingestion_walks_all_kinds`` — fresh ECL run hits all 4 datasets.
2. ``test_incremental_ingestion_on_commit`` — incremental_paths re-ingests only the changed code surface.
3. ``test_code_kg_query_returns_cognee_repo_map`` — B8 replacement renders a Cognee preamble.
4. ``test_lessons_kg_query_returns_cognee_results`` — B10 replacement returns Cognee-shaped LessonSearchResult.
5. ``test_neo4j_unavailable_falls_back_to_b8_b10`` — typed CogneeNeo4jUnavailable falls back.
6. ``test_query_timeout_falls_back_to_b10`` — asyncio timeout falls back.
7. ``test_multi_tenant_dataset_filter`` — different tenant_id names map to disjoint datasets.
8. ``test_full_rebuild_idempotent`` — running ECL twice is a no-op (same source counts).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import patch

import pytest

from backend.agents import cognee_integration as ci


# ── Stub Cognee module + adapters ──────────────────────────────────────


class _FakeCognee:
    """Minimal stand-in for the ``cognee`` SDK surface we exercise."""

    def __init__(
        self,
        *,
        search_response: Sequence[dict[str, Any]] | None = None,
        raise_on_search: BaseException | None = None,
        slow_search: float | None = None,
    ) -> None:
        self.added: list[tuple[str, str]] = []
        self.cognified: list[tuple[str, ...]] = []
        self.searches: list[tuple[str, tuple[str, ...]]] = []
        self._search_response = list(search_response or [])
        self._raise_on_search = raise_on_search
        self._slow_search = slow_search

    async def add(self, content: str, *, dataset_name: str) -> None:
        self.added.append((dataset_name, content[:32]))

    async def cognify(self, *, datasets: Sequence[str]) -> None:
        self.cognified.append(tuple(datasets))

    async def search(self, query: str, *, datasets: Sequence[str]) -> list[dict[str, Any]]:
        self.searches.append((query, tuple(datasets)))
        if self._slow_search is not None:
            await asyncio.sleep(self._slow_search)
        if self._raise_on_search is not None:
            raise self._raise_on_search
        return list(self._search_response)


def _adapter(cognee: _FakeCognee, *, tenant_id: str = "t-default", timeout: float = 5.0) -> ci.CogneeAdapter:
    config = ci.CogneeConfig(tenant_id=tenant_id, query_timeout=timeout)
    return ci.CogneeAdapter(config, cognee_module=cognee)


# ── Repo fixtures ──────────────────────────────────────────────────────


def _init_repo(repo_root: Path, files: dict[str, str]) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
    for rel, body in files.items():
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo_root, check=True)


@pytest.fixture
def repo_with_code_and_lessons(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        {
            "backend/a.py": "import b\n",
            "backend/b.py": "value = 1\n",
            "frontend/page.tsx": "export const x = 1;\n",
            "docs/sop/lessons/L-OP-1-alpha.md": "Alpha lesson body.",
            "docs/sop/lessons/L-OP-2-beta.md": "Beta lesson body.",
        },
    )
    return repo


# ── 1. Initial ingestion ───────────────────────────────────────────────


def test_initial_ingestion_walks_all_kinds(repo_with_code_and_lessons: Path) -> None:
    fake = _FakeCognee()
    adapter = _adapter(fake)

    snapshots = [{"key": "OP-1", "title": "first", "description": "desc", "status": "TODO"}]
    gerrit_changes = [{"change_id": "I1", "subject": "fix", "diff": "diff", "branch": "develop"}]

    report = asyncio.run(
        ci.run_ecl_pipeline(
            repo_with_code_and_lessons,
            jira_snapshots=snapshots,
            gerrit_changes=gerrit_changes,
            adapter=adapter,
        )
    )

    # Code: 3 files (2 .py + 1 .tsx). Lessons: 2 L-*.md. JIRA: 1. Gerrit: 1.
    assert report.code.sources_seen == 3
    assert report.code.sources_ingested == 3
    assert report.lessons.sources_seen == 2
    assert report.jira.sources_seen == 1
    assert report.gerrit.sources_seen == 1
    assert report.total_ingested == 7

    # Each kind should land in its own (tenant:kind) dataset namespace.
    datasets = {dataset for dataset, _ in fake.added}
    assert datasets == {
        "t-default:code",
        "t-default:lesson",
        "t-default:jira",
        "t-default:gerrit",
    }


# ── 2. Incremental ingestion on commit ─────────────────────────────────


def test_incremental_ingestion_on_commit(repo_with_code_and_lessons: Path) -> None:
    fake = _FakeCognee()
    adapter = _adapter(fake)

    report = asyncio.run(
        ci.run_ecl_pipeline(
            repo_with_code_and_lessons,
            adapter=adapter,
            incremental_paths=["backend/a.py"],
        )
    )

    # Only the requested code path should be re-ingested; other kinds
    # are skipped on incremental runs.
    assert report.code.sources_ingested == 1
    assert report.lessons.sources_ingested == 0
    assert report.jira.sources_ingested == 0
    assert report.gerrit.sources_ingested == 0
    assert [dataset for dataset, _ in fake.added] == ["t-default:code"]


# ── 3. Code KG query → Cognee repo map (B8 replacement) ────────────────


def test_code_kg_query_returns_cognee_repo_map(repo_with_code_and_lessons: Path) -> None:
    fake = _FakeCognee(
        search_response=[
            {"identifier": "backend/a.py", "score": 0.9, "kind": "code"},
            {"identifier": "backend/b.py", "score": 0.6, "kind": "code"},
        ]
    )
    adapter = _adapter(fake)

    preamble = ci.build_repo_map_via_cognee(
        repo_with_code_and_lessons,
        ticket_text="touch backend/a.py",
        adapter=adapter,
    )

    assert "Repo Map Context (Cognee KG)" in preamble
    assert "backend/a.py" in preamble
    assert "backend/b.py" in preamble
    # Only the code dataset (per AC #4) was queried.
    assert fake.searches[0][1] == ("t-default:code",)


# ── 4. Lessons KG query (B10 replacement) ──────────────────────────────


def test_lessons_kg_query_returns_cognee_results(repo_with_code_and_lessons: Path) -> None:
    fake = _FakeCognee(
        search_response=[
            {"identifier": "L-OP-1-alpha.md", "content": "alpha body", "score": 0.42},
        ]
    )
    adapter = _adapter(fake)

    lessons_dir = repo_with_code_and_lessons / "docs" / "sop" / "lessons"
    results = ci.retrieve_lessons_via_cognee(
        lessons_dir,
        ticket_title="alpha",
        acceptance_criteria="run alpha",
        adapter=adapter,
    )

    assert len(results) == 1
    assert results[0].path == lessons_dir / "L-OP-1-alpha.md"
    assert results[0].text == "alpha body"
    assert results[0].score == pytest.approx(0.42)
    # Only the lesson dataset was queried (AC #5).
    assert fake.searches[0][1] == ("t-default:lesson",)


# ── 5. Neo4j unavailable degrades to B8 + B10 ──────────────────────────


def test_neo4j_unavailable_falls_back_to_b8_b10(repo_with_code_and_lessons: Path) -> None:
    fake = _FakeCognee(raise_on_search=ConnectionError("connection refused"))
    adapter = _adapter(fake)

    # B8 fallback path: the Cognee adapter raises CogneeNeo4jUnavailable
    # and the helper delegates to ``build_repo_map_system_prefix`` (the
    # B8 PageRank baseline). We patch the underlying B8 call so the test
    # verifies the contract — fallback dispatch — without depending on a
    # working tree-sitter install in the test environment.
    with patch.object(ci, "_b8_fallback", return_value="B8-FALLBACK") as fake_b8:
        repo_map = ci.build_repo_map_via_cognee(
            repo_with_code_and_lessons,
            ticket_text="backend/a.py",
            adapter=adapter,
        )
    assert repo_map == "B8-FALLBACK"
    fake_b8.assert_called_once()

    # B10 fallback path: same adapter + ConnectionError → B10 BM25 result.
    lessons_dir = repo_with_code_and_lessons / "docs" / "sop" / "lessons"
    fake_lessons = _FakeCognee(raise_on_search=ConnectionError("connection refused"))
    results = ci.retrieve_lessons_via_cognee(
        lessons_dir,
        ticket_title="alpha",
        acceptance_criteria="run alpha",
        adapter=_adapter(fake_lessons),
    )
    assert results, "B10 fallback should return at least one BM25 hit"
    assert results[0].path.name.startswith("L-OP-")


# ── 6. Query timeout falls back to baseline ────────────────────────────


def test_query_timeout_falls_back_to_b10(repo_with_code_and_lessons: Path) -> None:
    # Slow search > timeout will trigger asyncio.TimeoutError, which the
    # adapter wraps as CogneeQueryTimeout, which the helper catches +
    # falls back to B10.
    fake = _FakeCognee(slow_search=0.05)
    adapter = _adapter(fake, timeout=0.001)

    lessons_dir = repo_with_code_and_lessons / "docs" / "sop" / "lessons"
    results = ci.retrieve_lessons_via_cognee(
        lessons_dir,
        ticket_title="alpha",
        acceptance_criteria="run alpha",
        adapter=adapter,
    )
    assert results, "timeout should drop to B10 BM25 result"
    assert results[0].path.parent == lessons_dir


# ── 7. Multi-tenant dataset filter ─────────────────────────────────────


def test_multi_tenant_dataset_filter(repo_with_code_and_lessons: Path) -> None:
    fake = _FakeCognee(search_response=[{"identifier": "x", "score": 1.0}])

    adapter_a = _adapter(fake, tenant_id="tenant-a")
    asyncio.run(adapter_a.search("q", kinds=[ci.SOURCE_KIND_LESSON]))

    adapter_b = _adapter(fake, tenant_id="tenant-b")
    asyncio.run(adapter_b.search("q", kinds=[ci.SOURCE_KIND_LESSON]))

    # Each tenant queried its own (tenant:kind) dataset — no cross-tenant leak.
    assert fake.searches[0][1] == ("tenant-a:lesson",)
    assert fake.searches[1][1] == ("tenant-b:lesson",)


# ── 8. Full-rebuild idempotency ────────────────────────────────────────


def test_full_rebuild_idempotent(repo_with_code_and_lessons: Path) -> None:
    fake = _FakeCognee()
    adapter = _adapter(fake)

    first = asyncio.run(
        ci.run_ecl_pipeline(repo_with_code_and_lessons, adapter=adapter)
    )
    second = asyncio.run(
        ci.run_ecl_pipeline(repo_with_code_and_lessons, adapter=adapter)
    )

    # The same source counts on both runs prove the pipeline is shape-stable
    # across reruns (Cognee's add() replaces by identifier rather than
    # duplicating, so a full rebuild is a safe recovery primitive — see
    # docs/operations/cognee-runbook.md).
    assert first.total_seen == second.total_seen
    assert first.total_ingested == second.total_ingested
    # The fake sees exactly twice the add calls (one per ECL run).
    per_run = first.total_ingested
    assert len(fake.added) == per_run * 2


# ── Bonus: CogneeNotInstalled bubbles up as fallback, not crash ────────


def test_missing_cognee_package_falls_back_silently(repo_with_code_and_lessons: Path) -> None:
    """When `cognee` is not importable the helpers must still return a result."""
    with patch.object(
        ci, "_import_cognee_module", side_effect=ci.CogneeNotInstalled("missing")
    ), patch.object(ci, "_b8_fallback", return_value="B8-FALLBACK"):
        repo_map = ci.build_repo_map_via_cognee(
            repo_with_code_and_lessons,
            ticket_text="backend/a.py",
        )
        assert repo_map == "B8-FALLBACK"

        lessons_dir = repo_with_code_and_lessons / "docs" / "sop" / "lessons"
        results = ci.retrieve_lessons_via_cognee(
            lessons_dir,
            ticket_title="alpha",
            acceptance_criteria="run alpha",
        )
        assert results, "B10 should still produce hits when Cognee is absent"


# ── AUDIT-29b-6 (OP-1024): anti-pattern parsing + recall ───────────────


_FIXTURE_COOKBOOK = """# Architecture Anti-patterns Cookbook

intro paragraph that mentions nothing actionable.

---

## Index

| # | Pattern | Symptom |
|---|---|---|
| 1 | [Numbered flat-file registry](#1-numbered-flat-file-registry) | shared file conflicts |
| 2 | [Migration ticket fighting in-flight tickets](#2-migration-ticket-fighting-in-flight-tickets) | migration vs siblings |
| 3 | [Ad-hoc pattern without domains line](#3-ad-hoc-pattern-without-domains-line) | needs inference |

---

## 1. Numbered flat-file registry

**Domains**: docs, backend, tooling

**Symptom**: a single mutable shared file with sequential ids and N writers.

**Cure**: decompose into per-file entries with a build script.

---

## 2. Migration ticket fighting in-flight tickets

**Domains**: backend, db

**Symptom**: a structural migration lands while sibling alembic migrations write the old schema.

**Cure**: a migration freeze label that the runner pickup gate honours.

---

## 3. Ad-hoc pattern without domains line

**Symptom**: a long-running runner daemon retries an external call forever with no circuit breaker.

**Cure**: add a circuit breaker per external service.

---

## Cross-cutting principles

1. Idempotency is non-negotiable.

---

## See also

- some other doc
"""

_COOKBOOK_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "sop" / "architecture-anti-patterns.md"
)


@pytest.fixture
def fixture_cookbook(tmp_path: Path) -> Path:
    path = tmp_path / "architecture-anti-patterns.md"
    path.write_text(_FIXTURE_COOKBOOK, encoding="utf-8")
    return path


def test_parse_antipatterns_reads_domains_line(fixture_cookbook: Path) -> None:
    records = ci.parse_antipatterns(fixture_cookbook)
    assert [r.pattern_id for r in records] == ["1", "2", "3"]
    assert records[0].title == "Numbered flat-file registry"
    assert records[0].domains == ("docs", "backend", "tooling")
    assert records[1].domains == ("backend", "db")
    # The Index / Cross-cutting principles / See also headings are not patterns.
    assert all("Cross-cutting" not in r.text for r in records)
    # Stable identifier used as the Cognee node id.
    assert records[0].identifier == "antipattern-1-numbered-flat-file-registry"


def test_parse_antipatterns_infers_domains_when_missing(fixture_cookbook: Path) -> None:
    records = ci.parse_antipatterns(fixture_cookbook)
    pattern3 = next(r for r in records if r.pattern_id == "3")
    # No **Domains**: line — inferred from the body ("runner daemon",
    # "circuit breaker") and must be non-empty.
    assert pattern3.domains
    assert "backend" in pattern3.domains


def test_parse_antipatterns_missing_file_returns_empty(tmp_path: Path) -> None:
    assert ci.parse_antipatterns(tmp_path / "nope.md") == []


def test_collect_antipattern_sources_per_pattern(fixture_cookbook: Path) -> None:
    sources = ci.collect_antipattern_sources(fixture_cookbook)
    assert len(sources) == 3
    assert all(s.kind == ci.SOURCE_KIND_ANTIPATTERN for s in sources)
    assert {s.identifier for s in sources} == {
        "antipattern-1-numbered-flat-file-registry",
        "antipattern-2-migration-ticket-fighting-in-flight-tickets",
        "antipattern-3-ad-hoc-pattern-without-domains-line",
    }
    assert sources[1].metadata["domains"] == ["backend", "db"]
    # Re-ingestion is idempotent because identifiers are stable, not random.
    again = ci.collect_antipattern_sources(fixture_cookbook)
    assert [s.identifier for s in sources] == [s.identifier for s in again]


def test_retrieve_antipatterns_via_cognee_follows_search_order(fixture_cookbook: Path) -> None:
    # Cognee returns pattern #3 first, then #2; the helper preserves that
    # order and resolves identifiers back to the parsed records.
    fake = _FakeCognee(
        search_response=[
            {"identifier": "antipattern-3-ad-hoc-pattern-without-domains-line", "score": 0.9},
            {"identifier": "antipattern-2-migration-ticket-fighting-in-flight-tickets", "score": 0.7},
        ]
    )
    matches = ci.retrieve_antipatterns_via_cognee(
        fixture_cookbook,
        ticket_title="x",
        acceptance_criteria="y",
        top_k=2,
        adapter=_adapter(fake),
    )
    assert [m.record.pattern_id for m in matches] == ["3", "2"]
    # Searched the antipattern dataset, not the lesson dataset.
    assert fake.searches[0][1] == ("t-default:antipattern",)


def test_retrieve_antipatterns_area_match_is_biased_first(fixture_cookbook: Path) -> None:
    # Cognee orders #1 then #2, but the ticket is area:db — pattern #2
    # (Domains: backend, db) must be promoted ahead of #1, with the matched
    # domain recorded.
    fake = _FakeCognee(
        search_response=[
            {"identifier": "antipattern-1-numbered-flat-file-registry", "score": 0.9},
            {"identifier": "antipattern-2-migration-ticket-fighting-in-flight-tickets", "score": 0.6},
        ]
    )
    matches = ci.retrieve_antipatterns_via_cognee(
        fixture_cookbook,
        ticket_title="add a migration",
        acceptance_criteria="alembic revision",
        declared_areas=["db"],
        top_k=2,
        adapter=_adapter(fake),
    )
    assert matches[0].record.pattern_id == "2"
    assert matches[0].matched_domains == ("db",)
    assert matches[1].matched_domains == ()


def test_retrieve_antipatterns_falls_back_when_cognee_absent(fixture_cookbook: Path) -> None:
    with patch.object(
        ci.CogneeAdapter, "from_env", side_effect=ci.CogneeNotInstalled("missing")
    ):
        matches = ci.retrieve_antipatterns_via_cognee(
            fixture_cookbook,
            ticket_title="migration freeze",
            acceptance_criteria="alembic siblings old schema",
            declared_areas=["db"],
            top_k=2,
        )
    # Keyword fallback still produces results, and the area:db match is
    # promoted to the front.
    assert matches
    assert matches[0].record.pattern_id == "2"
    assert matches[0].matched_domains == ("db",)


def test_retrieve_antipatterns_against_real_cookbook() -> None:
    records = ci.parse_antipatterns(_COOKBOOK_PATH)
    # The cookbook has 14 numbered patterns (Index / Cross-cutting / See also
    # are not patterns).
    assert [r.pattern_id for r in records] == [str(n) for n in range(1, 15)]
    # Every pattern carries a Domains line (AUDIT-29b-6 added them).
    assert all(r.domains for r in records)
    # An area:db ticket auto-surfaces pattern #10 "Migration ticket fighting
    # in-flight tickets" (Domains: backend, db, docs) even with Cognee absent.
    with patch.object(
        ci.CogneeAdapter, "from_env", side_effect=ci.CogneeNotInstalled("missing")
    ):
        matches = ci.retrieve_antipatterns_via_cognee(
            _COOKBOOK_PATH,
            ticket_title="migration ticket conflicts with in-flight siblings",
            acceptance_criteria="freeze the scope before pickup",
            declared_areas=["db"],
            top_k=3,
        )
    surfaced = {m.record.pattern_id for m in matches}
    assert "10" in surfaced
    assert any(m.matched_domains for m in matches)
