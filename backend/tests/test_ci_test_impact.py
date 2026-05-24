"""OP-742 — unit tests for ``scripts/ci_test_impact.py``.

Exercises the three layers of the impact analyser (direct map, import
graph, high-fan-out fallback) plus the area-label policy table and
classify_changes top-level entrypoint. The tests build a small
synthetic repo on a tmp_path so the import-graph walker has predictable
input — running against the real repo would couple the assertions to
unrelated module growth.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ci_test_impact as impact  # noqa: E402


# ── tmp_path repo helper ─────────────────────────────────────────


def _make_repo(tmp_path: Path) -> Path:
    """Construct a small synthetic repo:

      backend/foo.py             (imports backend.lib.helper)
      backend/lib/helper.py      (leaf)
      backend/agents/orchestra.py (imports backend.foo)
      backend/tests/test_foo.py
      backend/tests/test_helper.py
      backend/tests/test_orchestra.py
      backend/tests/test_deploy_base.py   (deploy/CI subset)
      tests/test_gitlab_ci_smoke.py       (deploy/CI subset)
      docs/note.md
      app/page.tsx
    """

    (tmp_path / "backend" / "lib").mkdir(parents=True)
    (tmp_path / "backend" / "agents").mkdir(parents=True)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "app").mkdir()

    (tmp_path / "backend" / "__init__.py").write_text("")
    (tmp_path / "backend" / "lib" / "__init__.py").write_text("")
    (tmp_path / "backend" / "agents" / "__init__.py").write_text("")
    (tmp_path / "backend" / "tests" / "__init__.py").write_text("")

    (tmp_path / "backend" / "lib" / "helper.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "backend" / "foo.py").write_text(
        "from backend.lib.helper import helper\n"
        "def foo():\n    return helper()\n",
    )
    (tmp_path / "backend" / "agents" / "orchestra.py").write_text(
        "import backend.foo\n"
        "def go(): return backend.foo.foo()\n",
    )

    (tmp_path / "backend" / "tests" / "test_foo.py").write_text(
        "from backend.foo import foo\ndef test_x(): assert foo() == 1\n"
    )
    (tmp_path / "backend" / "tests" / "test_helper.py").write_text(
        "from backend.lib.helper import helper\ndef test_x(): assert helper() == 1\n"
    )
    (tmp_path / "backend" / "tests" / "test_orchestra.py").write_text(
        "from backend.agents.orchestra import go\ndef test_x(): assert go() == 1\n"
    )

    # Deploy/CI behavioural subset — discovered by name convention.
    (tmp_path / "backend" / "tests" / "test_deploy_base.py").write_text(
        "def test_deploy(): assert True\n"
    )
    (tmp_path / "tests" / "test_gitlab_ci_smoke.py").write_text(
        "def test_ci(): assert True\n"
    )

    (tmp_path / "docs" / "note.md").write_text("# note\n")
    (tmp_path / "app" / "page.tsx").write_text("export default function P() {return null}\n")
    return tmp_path


# ── Direct map ───────────────────────────────────────────────────


class TestDirectMap:
    def test_module_change_maps_to_test_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        out = impact.direct_test_files("backend/foo.py", repo)
        assert "backend/tests/test_foo.py" in out

    def test_test_file_maps_to_itself(self, tmp_path):
        repo = _make_repo(tmp_path)
        out = impact.direct_test_files("backend/tests/test_foo.py", repo)
        assert out == ["backend/tests/test_foo.py"]

    def test_non_python_file_yields_empty(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert impact.direct_test_files("docs/note.md", repo) == []

    def test_module_with_no_test_yields_empty(self, tmp_path):
        repo = _make_repo(tmp_path)
        # No test_helper exists anywhere except the named one we wrote;
        # for a module with no test file this should be empty.
        (repo / "backend" / "ghost.py").write_text("x = 1\n")
        assert impact.direct_test_files("backend/ghost.py", repo) == []


# ── Import graph ─────────────────────────────────────────────────


class TestImportGraph:
    def test_reverse_graph_records_importers(self, tmp_path):
        repo = _make_repo(tmp_path)
        graph = impact.build_reverse_import_graph(repo)
        # backend.lib.helper is imported by backend.foo
        assert "backend.foo" in graph["backend.lib.helper"]
        # backend.foo is imported by backend.agents.orchestra
        assert "backend.agents.orchestra" in graph["backend.foo"]

    def test_transitive_importers_walks_chain(self, tmp_path):
        repo = _make_repo(tmp_path)
        graph = impact.build_reverse_import_graph(repo)
        # Editing helper should pull foo + orchestra (transitively).
        out = impact.transitive_importers("backend.lib.helper", graph)
        assert "backend.foo" in out
        assert "backend.agents.orchestra" in out

    def test_module_to_test_file_resolves_leaf(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert (
            impact._module_to_test_file("backend.foo", repo)
            == "backend/tests/test_foo.py"
        )

    def test_module_to_test_file_returns_none_when_missing(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert impact._module_to_test_file("backend.absent", repo) is None


# ── Deploy / CI subset discovery ─────────────────────────────────


class TestDeployCiSubset:
    def test_discovers_deploy_and_ci_tests_by_convention(self, tmp_path):
        repo = _make_repo(tmp_path)
        out = set(impact.deploy_ci_test_files(repo))
        assert "backend/tests/test_deploy_base.py" in out
        assert "tests/test_gitlab_ci_smoke.py" in out

    def test_does_not_sweep_in_unrelated_suites(self, tmp_path):
        # Anchored globs must not match false friends like the "ci"
        # substring inside test_pricing / test_decision.
        repo = _make_repo(tmp_path)
        (repo / "backend" / "tests" / "test_pricing.py").write_text(
            "def test_x(): assert True\n"
        )
        (repo / "backend" / "tests" / "test_decision_api.py").write_text(
            "def test_x(): assert True\n"
        )
        out = set(impact.deploy_ci_test_files(repo))
        assert "backend/tests/test_pricing.py" not in out
        assert "backend/tests/test_decision_api.py" not in out

    def test_empty_when_no_deploy_tests_present(self, tmp_path):
        # Bare repo (no tests dirs) yields an empty subset.
        assert impact.deploy_ci_test_files(tmp_path) == []


# ── classify_changes (top-level entry) ──────────────────────────


class TestClassifyChanges:
    def test_empty_change_is_skip(self, tmp_path):
        result = impact.classify_changes([], tmp_path)
        assert result.policy == "skip"

    def test_docs_only_is_skip(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = impact.classify_changes(["docs/note.md"], repo)
        assert result.policy == "skip"
        assert "docs-only" in result.reason

    def test_security_area_forces_full(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = impact.classify_changes(
            ["backend/foo.py"], repo, declared_areas=["security"],
        )
        assert result.policy == "full"
        assert "security" in result.reason

    def test_frontend_only_is_frontend_policy(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = impact.classify_changes(["app/page.tsx"], repo)
        assert result.policy == "frontend-only"

    def test_deploy_change_runs_deploy_ci_subset(self, tmp_path):
        # OP-1706 / finding #28: a deploy/ change must run a real test
        # subset, NOT lint-only.
        repo = _make_repo(tmp_path)
        result = impact.classify_changes(
            ["deploy/systemd/foo.service"], repo,
        )
        assert result.policy == "affected"
        files = set(result.test_files)
        assert "backend/tests/test_deploy_base.py" in files
        assert "tests/test_gitlab_ci_smoke.py" in files

    def test_gitlab_ci_change_runs_deploy_ci_subset(self, tmp_path):
        # .gitlab-ci.yml used to fall through to lint-only — it must now
        # trigger the deploy/CI behavioural subset.
        repo = _make_repo(tmp_path)
        result = impact.classify_changes([".gitlab-ci.yml"], repo)
        assert result.policy == "affected"
        files = set(result.test_files)
        assert "backend/tests/test_deploy_base.py" in files
        assert "tests/test_gitlab_ci_smoke.py" in files

    def test_deploy_plus_docs_still_runs_subset(self, tmp_path):
        # Mixing in docs must not let the change collapse to skip/lint-only.
        repo = _make_repo(tmp_path)
        result = impact.classify_changes(
            ["deploy/k8s/app.yaml", "docs/note.md"], repo,
        )
        assert result.policy == "affected"
        assert result.test_files

    def test_deploy_change_with_no_deploy_tests_falls_back_to_full(self, tmp_path):
        # A deploy change must never silently green-light: with no deploy
        # tests in the tree we fall back to full rather than lint-only/skip.
        result = impact.classify_changes(
            ["deploy/systemd/foo.service"], tmp_path,
        )
        assert result.policy == "full"
        assert "no test mapping" in result.reason

    def test_tooling_only_is_still_lint_only(self, tmp_path):
        # Pure tooling changes (scripts/, tools/) keep lint-only — the
        # impact-scoping carve-out is unchanged for non-deploy tooling.
        repo = _make_repo(tmp_path)
        result = impact.classify_changes(["tools/format_all.sh"], repo)
        assert result.policy == "lint-only"

    def test_high_fan_out_path_forces_full(self, tmp_path):
        # The high-fan-out list is checked against the literal path
        # string regardless of repo contents.
        result = impact.classify_changes(
            ["backend/db.py", "backend/foo.py"], tmp_path,
        )
        assert result.policy == "full"
        assert "high-fan-out" in result.reason
        assert "backend/db.py" in result.reason

    def test_affected_selects_direct_plus_transitive(self, tmp_path):
        repo = _make_repo(tmp_path)
        graph = impact.build_reverse_import_graph(repo)
        result = impact.classify_changes(
            ["backend/lib/helper.py"], repo, import_graph=graph,
        )
        assert result.policy == "affected"
        # Direct (test_helper) + transitive (test_foo, test_orchestra).
        files = set(result.test_files)
        assert "backend/tests/test_helper.py" in files
        assert "backend/tests/test_foo.py" in files
        assert "backend/tests/test_orchestra.py" in files

    def test_affected_falls_back_to_full_when_no_tests_match(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "backend" / "ghost.py").write_text("x = 1\n")
        graph = impact.build_reverse_import_graph(repo)
        result = impact.classify_changes(
            ["backend/ghost.py"], repo, import_graph=graph,
        )
        # No test_ghost.py exists, no importers — must NOT silently
        # green-light. Falls back to full.
        assert result.policy == "full"
        assert "no test mapping" in result.reason

    def test_test_file_only_change_runs_that_test(self, tmp_path):
        repo = _make_repo(tmp_path)
        graph = impact.build_reverse_import_graph(repo)
        result = impact.classify_changes(
            ["backend/tests/test_foo.py"], repo, import_graph=graph,
        )
        assert result.policy == "affected"
        assert "backend/tests/test_foo.py" in result.test_files

    def test_mixed_frontend_and_docs_collapses_to_frontend(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = impact.classify_changes(
            ["app/page.tsx", "docs/note.md"], repo,
        )
        assert result.policy == "frontend-only"


# ── Area-policy table sanity ─────────────────────────────────────


class TestAreaPolicyTable:
    @pytest.mark.parametrize("area, expected", [
        ("docs", "skip"),
        ("frontend", "frontend-only"),
        ("backend", "affected"),
        # OP-1706 / finding #28: devops (deploy/CI) is no longer lint-only.
        ("devops", "affected"),
        ("tests", "affected"),
        ("tooling", "lint-only"),
        ("security", "full"),
    ])
    def test_spec_table_exact(self, area, expected):
        assert impact.AREA_TO_TEST_POLICY[area] == expected


# ── CLI ──────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_returns_json_payload(self, tmp_path):
        repo = _make_repo(tmp_path)
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPTS_DIR / "ci_test_impact.py"),
                "--repo-root", str(repo),
                "docs/note.md",
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["policy"] == "skip"
        assert "docs-only" in payload["reason"]
