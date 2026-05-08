"""OP-799 Sprint META scope-overlap audit tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_sprint_overlap.py"


def _load_script():
    sys.modules.pop("audit_sprint_overlap", None)
    spec = importlib.util.spec_from_file_location("audit_sprint_overlap", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["audit_sprint_overlap"] = module
    spec.loader.exec_module(module)
    return module


def _child(key: str, paths: list[str]):
    mod = _load_script()
    body = "\n".join(f"- {path}" for path in paths)
    return mod.ChildIssue(key=key, summary=key, description=f"## Files / Paths\n{body}\n")


def test_sprint_e_retrospective_flags_five_shared_docs_site_files() -> None:
    mod = _load_script()
    e1 = _child(
        "OP-785",
        [
            "docs-site/mkdocs.yml (NEW)",
            "docs-site/requirements.txt (NEW)",
            "docs-site/docs/index.md (NEW)",
            "docs-site/docs/lessons/index.md (NEW)",
            "docs-site/docs/stylesheets/extra.css (NEW)",
        ],
    )
    e2 = _child(
        "OP-786",
        [
            "docs-site/mkdocs.yml (NEW)",
            "docs-site/requirements.txt (NEW)",
            "docs-site/docs/index.md (NEW)",
            "docs-site/docs/lessons/index.md (NEW)",
            "docs-site/docs/stylesheets/extra.css (NEW)",
        ],
    )

    overlaps = mod.find_overlaps([e1, e2])

    assert {(hit.path, hit.child_a, hit.child_b) for hit in overlaps} == {
        ("docs-site/mkdocs.yml", "OP-785", "OP-786"),
        ("docs-site/requirements.txt", "OP-785", "OP-786"),
        ("docs-site/docs/index.md", "OP-785", "OP-786"),
        ("docs-site/docs/lessons/index.md", "OP-785", "OP-786"),
        ("docs-site/docs/stylesheets/extra.css", "OP-785", "OP-786"),
    }


def test_sprint_d_retrospective_declared_distinct_paths_are_clean() -> None:
    mod = _load_script()
    children = [
        _child("OP-762", ["docs/sop/migration-plan-2026-05.md (MODIFY)"]),
        _child("OP-773", ["docs/sop/feature-flag-sdk.md (NEW)"]),
        _child("OP-774", ["backend/tests/test_api_versioning_op774.py (NEW)"]),
        _child("OP-779", ["backend/tests/test_deploy_audit_op779.py (NEW)"]),
    ]

    assert mod.find_overlaps(children) == ()


def test_synthetic_overlap_then_clean() -> None:
    mod = _load_script()
    first = _child("OP-A", ["backend/agents/jira_dispatch.py (MODIFY)"])
    overlapping = _child("OP-B", ["backend/agents/jira_*.py (MODIFY)"])
    clean = _child("OP-B", ["backend/agents/scheduler.py (MODIFY)"])

    overlaps = mod.find_overlaps([first, overlapping])
    assert len(overlaps) == 1
    assert overlaps[0].path == "backend/agents/jira_dispatch.py <-> backend/agents/jira_*.py"
    assert mod.find_overlaps([first, clean]) == ()


def test_added_directory_overlap_is_flagged() -> None:
    mod = _load_script()
    first = _child("OP-A", ["docs-site/ (ADD)"])
    second = _child("OP-B", ["docs-site/docs/ (ADD)"])

    assert mod.find_overlaps([first, second]) == (
        mod.Overlap(path="docs-site/", child_a="OP-A", child_b="OP-B"),
    )


def test_render_report_returns_nonempty_conflict_summary() -> None:
    mod = _load_script()
    children = (_child("OP-A", ["docs/a.md (NEW)"]), _child("OP-B", ["docs/a.md (NEW)"]))
    overlaps = mod.find_overlaps(children)

    report = mod.render_report("OP-META", children, overlaps)

    assert "result: FAIL" in report
    assert "docs/a.md: OP-A <-> OP-B" in report
