"""OP-1442 — feature-duplication detector contracts."""

from __future__ import annotations

import pytest

from backend.agents import feature_dup_detector as fdd


def test_extract_added_symbols_from_diff_finds_same_function_in_different_files() -> None:
    diff = """diff --git a/backend/a.py b/backend/a.py
@@ -0,0 +1,2 @@
+def duplicate_feature():
+    return True
diff --git a/backend/b.py b/backend/b.py
@@ -0,0 +1,3 @@
+class DuplicateHelper:
+    pass
"""

    assert fdd.extract_added_symbols_from_diff(diff) == frozenset(
        {"duplicate_feature", "DuplicateHelper"}
    )


def test_detect_flags_symbol_overlap_across_different_files() -> None:
    candidate = fdd.PatchSignal(
        ticket_key="OP-1442",
        files_changed=frozenset({"backend/agents/feature_dup_detector.py"}),
        added_symbols=frozenset({"detect_duplicate_feature"}),
    )
    other = fdd.PatchSignal(
        ticket_key="OP-1439",
        change_number="911",
        files_changed=frozenset({"backend/agents/ops_audit.py"}),
        added_symbols=frozenset({"detect_duplicate_feature"}),
    )

    hits = fdd.detect_feature_duplicates(candidate, [other])

    assert len(hits) == 1
    assert hits[0].other.ticket_key == "OP-1439"
    assert hits[0].shared_symbols == ("detect_duplicate_feature",)
    assert "symbol-overlap" in hits[0].reasons


def test_detect_flags_changed_file_jaccard_above_threshold() -> None:
    candidate = fdd.PatchSignal(
        ticket_key="OP-1",
        files_changed=frozenset({"backend/a.py", "backend/b.py", "backend/c.py"}),
    )
    other = fdd.PatchSignal(
        ticket_key="OP-2",
        files_changed=frozenset({"backend/a.py", "backend/b.py", "backend/d.py"}),
    )

    assert fdd.detect_feature_duplicates(candidate, [other]) == []

    stronger = fdd.PatchSignal(
        ticket_key="OP-2",
        files_changed=frozenset({"backend/a.py", "backend/b.py"}),
    )
    hits = fdd.detect_feature_duplicates(candidate, [stronger])
    assert hits[0].file_jaccard == pytest.approx(2 / 3)
    assert "changed-file-similarity" in hits[0].reasons


def test_detect_flags_hot_area_jira_description_similarity_only_for_hot_areas() -> None:
    candidate = fdd.PatchSignal(
        ticket_key="OP-1",
        description="runner pickup detects duplicate feature overlap before merge",
        areas=frozenset({"core"}),
    )
    other = fdd.PatchSignal(
        ticket_key="OP-2",
        description="runner pickup detects duplicate feature overlap before merge",
        areas=frozenset({"backend"}),
    )

    hits = fdd.detect_feature_duplicates(candidate, [other])

    assert len(hits) == 1
    assert hits[0].jira_similarity == pytest.approx(1.0)
    assert "jira-description-similarity" in hits[0].reasons

    non_hot = fdd.PatchSignal(
        ticket_key="OP-3",
        description=other.description,
        areas=frozenset({"backend"}),
    )
    assert fdd.detect_feature_duplicates(
        fdd.PatchSignal(
            ticket_key="OP-4",
            description=candidate.description,
            areas=frozenset({"tooling"}),
        ),
        [non_hot],
    ) == []


def test_patch_signal_from_gerrit_change_extracts_ticket_files_and_symbols() -> None:
    change = {
        "number": "123",
        "subject": "[OP-1442] feature dup detector",
        "url": "https://gerrit.invalid/c/project/+/123",
        "currentPatchSet": {
            "files": [
                {"file": "backend/agents/feature_dup_detector.py"},
                {"file": "/COMMIT_MSG"},
            ]
        },
    }
    diff = (
        "diff --git a/backend/agents/feature_dup_detector.py "
        "b/backend/agents/feature_dup_detector.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def detect_duplicate_feature():\n"
        "+    return []\n"
    )

    signal = fdd.patch_signal_from_gerrit_change(change, diff_text=diff)

    assert signal is not None
    assert signal.ticket_key == "OP-1442"
    assert signal.files_changed == frozenset({"backend/agents/feature_dup_detector.py"})
    assert signal.added_symbols == frozenset({"detect_duplicate_feature"})
