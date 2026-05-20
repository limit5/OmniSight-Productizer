"""OP-1542 release-cut guard query-shape + findings tests.

OP-1540 added the guard; OP-1533 mis-set its query topic to ``release-v``,
which matched ZERO real cuts (real topics carry ``release-cut``). These
tests pin the corrected ``intopic:release-cut`` shape and the findings
logic so the guard actually sees real cuts.
"""
from __future__ import annotations

from backend.agents import release_cut_guard as guard
from backend.release_cut_metadata import (
    CANONICAL_RELEASE_CUT_HASHTAG,
    RELEASE_CUT_HASHTAGS,
    RELEASE_CUT_PROMOTE_REQUIREMENT,
)


def test_default_query_topic_is_release_cut() -> None:
    # intopic: is a substring match; real cuts are vX.Y.Z-release-cut.
    assert guard.DEFAULT_QUERY_TOPIC == "release-cut"


def test_build_release_cut_query_contains_intopic_release_cut() -> None:
    query = guard.build_release_cut_query(project="omnisight/OmniSight-Productizer")
    assert "intopic:release-cut" in query
    assert "branch:main" in query
    assert "status:open" in query
    assert "project:omnisight/OmniSight-Productizer" in query
    # The OP-1533 wrong-premise topic must be gone.
    assert "intopic:release-v" not in query


def test_query_matches_real_cut_topic_shape() -> None:
    """The substring the query keys on is present in the real cut topic."""
    real_topic = "v0.5.0-rc2-release-cut"
    assert guard.DEFAULT_QUERY_TOPIC in real_topic


def test_findings_flag_missing_canonical_hashtag() -> None:
    mis_tagged = {
        "number": "1026",
        "subject": "[release-cut v9.9.9] Merge develop into main",
        "hashtags": ["auto-promote"],
        "submitRequirements": [
            {"name": RELEASE_CUT_PROMOTE_REQUIREMENT, "status": "SATISFIED"}
        ],
    }
    findings = guard.release_cut_findings([mis_tagged])
    assert len(findings) == 1
    assert findings[0].kind == "missing_canonical_hashtag"


def test_findings_quiet_for_correct_cut() -> None:
    correct = {
        "number": "1024",
        "subject": "[release-cut v9.9.10] Merge develop into main",
        "hashtags": list(RELEASE_CUT_HASHTAGS),
        "submitRequirements": [
            {"name": RELEASE_CUT_PROMOTE_REQUIREMENT, "status": "SATISFIED"}
        ],
    }
    assert CANONICAL_RELEASE_CUT_HASHTAG in RELEASE_CUT_HASHTAGS
    assert guard.release_cut_findings([correct]) == ()


def test_findings_flag_promote_requirement_not_applicable() -> None:
    cut = {
        "number": "1022",
        "subject": "[release-cut v9.9.11] Merge develop into main",
        "hashtags": list(RELEASE_CUT_HASHTAGS),
        "submitRequirements": [
            {"name": RELEASE_CUT_PROMOTE_REQUIREMENT, "status": "NOT_APPLICABLE"}
        ],
    }
    findings = guard.release_cut_findings([cut])
    assert len(findings) == 1
    assert findings[0].kind == "promote_requirement_not_applicable"


def test_query_uses_submit_records_not_invalid_flag(monkeypatch) -> None:
    """OP-1544: Gerrit 3.13 has NO --submit-requirements flag (fatal: not a
    valid option) — the guard must use the supported --submit-records, else
    every guard run crashes. Regression guard for that exact mistake."""
    from pathlib import Path

    captured: dict[str, list[str]] = {}

    class _Result:
        stdout = '{"type":"stats","rowCount":0}\n'

    def _fake_run(cmd, **kwargs):  # noqa: ANN001 — test stub
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(guard.subprocess, "run", _fake_run)
    client = guard.SshGerritClient(
        host="codex-bot@sora.services",
        port=29418,
        key_path=Path("/dev/null"),
        project="omnisight/OmniSight-Productizer",
    )
    client.query("status:open project:x branch:main intopic:release-cut")

    cmd = captured["cmd"]
    assert "--submit-records" in cmd, cmd
    assert "--submit-requirements" not in cmd, cmd
