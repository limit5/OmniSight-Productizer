"""OP-1430 -- replay hand-resolved Gerrit conflict fixtures."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from backend import merger_agent as ma


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "merger_replay"
EXPECTED_CASES = (
    "change_665",
    "change_685",
    "change_689",
    "change_698",
    "change_883",
    "change_899",
    "change_930",
    "change_932",
)
ALLOW_MARKER_TOKEN = "op-698-allow-conflict-marker"
_CONFLICT_RE = re.compile(
    r"^<<<<<<<[^\n]*(?:\n|$).*?^=======(?:\n|$).*?^>>>>>>>[^\n]*(?:\n|$)",
    re.MULTILINE | re.DOTALL,
)


def _run(coro):
    return asyncio.run(coro)


def _read_fixture_text(path: Path) -> str:
    text = path.read_text()
    first, sep, rest = text.partition("\n")
    if ALLOW_MARKER_TOKEN in first:
        return rest if sep else ""
    return text


def _has_conflict_markers(text: str) -> bool:
    return bool(_CONFLICT_RE.search(text))


def _metadata(case_name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / case_name / "metadata.json").read_text())


def _fixture_path(case_name: str, side: str, repo_path: str) -> Path:
    return FIXTURE_ROOT / case_name / side / repo_path


def _resolved_blocks(conflict_text: str, resolved_text: str) -> list[str]:
    matches = list(_CONFLICT_RE.finditer(conflict_text))
    assert matches, "fixture conflict text must contain conflict markers"

    segments: list[str] = []
    cursor = 0
    for match in matches:
        segments.append(conflict_text[cursor:match.start()])
        cursor = match.end()
    segments.append(conflict_text[cursor:])

    blocks: list[str] = []
    resolved_cursor = 0
    for idx, segment in enumerate(segments[:-1]):
        assert resolved_text.startswith(segment, resolved_cursor)
        resolved_cursor += len(segment)

        next_segment = segments[idx + 1]
        if next_segment:
            next_cursor = resolved_text.find(next_segment, resolved_cursor)
            assert next_cursor >= 0
        else:
            next_cursor = len(resolved_text)

        blocks.append(resolved_text[resolved_cursor:next_cursor])
        resolved_cursor = next_cursor

    assert resolved_text.startswith(segments[-1], resolved_cursor)
    resolved_cursor += len(segments[-1])
    assert resolved_cursor == len(resolved_text)
    return blocks


def replay_case(name: str) -> ma.ConflictRequest:
    """Load an OP-1430 fixture into the merger's ConflictRequest shape."""
    meta = _metadata(name)
    conflict_files = meta["conflict_files"]
    primary = conflict_files[0]
    additional = conflict_files[1:]

    return ma.ConflictRequest(
        change_id=meta["after_commit_message"].split("Change-Id: ")[-1].split()[0],
        project=meta["project"],
        file_path=primary,
        conflict_text=_read_fixture_text(_fixture_path(name, "before", primary)),
        head_commit_message=meta["before_commit_message"],
        incoming_commit_message=meta["after_commit_message"],
        patchset_revision=meta["after_patchset"],
        change_number=meta["change_number"],
        jira_ticket=f"OP-{meta['change_number']}",
        jira_description=meta["jira_description"],
        sibling_file_contents={
            path: _read_fixture_text(_fixture_path(name, "before", path))
            for path in additional
        },
        additional_files=additional,
        push_locally=False,
    )


class _FakeLLM:
    def __init__(self, payload: dict[str, Any] | str) -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> tuple[str, int]:
        self.calls.append(prompt)
        if isinstance(self.payload, str):
            return self.payload, 100
        return json.dumps(self.payload), 100


class _ExplodingPusher:
    async def push(self, **_kwargs):
        raise AssertionError("replay harness uses push_locally=False")


class _ExplodingReviewer:
    async def post_review(self, **_kwargs):
        raise AssertionError("replay harness uses push_locally=False")


async def _passing_test_runner(_req: ma.ConflictRequest) -> ma.TestRunResult:
    return ma.TestRunResult(ok=True, summary="OP-1430 replay fixture")


async def _audit_sink(_action: str, _entity_id: str, _payload: dict[str, Any]) -> None:
    return None


@pytest.fixture(autouse=True)
def _reset_failure_counts():
    ma.reset_failure_counts_for_tests()
    yield
    ma.reset_failure_counts_for_tests()


def test_all_replay_fixtures_load():
    assert sorted(path.name for path in FIXTURE_ROOT.iterdir()) == sorted(EXPECTED_CASES)

    for case_name in EXPECTED_CASES:
        meta = _metadata(case_name)
        assert meta["case"] == case_name
        assert meta["conflict_files"]
        assert meta["before_commit_message"]
        assert meta["after_commit_message"]
        assert meta["jira_description"]

        for repo_path in meta["conflict_files"]:
            before = _read_fixture_text(_fixture_path(case_name, "before", repo_path))
            after = _read_fixture_text(_fixture_path(case_name, "after", repo_path))
            ancestor = _read_fixture_text(_fixture_path(case_name, "ancestor", repo_path))
            assert _has_conflict_markers(before)
            assert not _has_conflict_markers(after)
            assert ancestor or ancestor == ""


@pytest.mark.parametrize("case_name", EXPECTED_CASES)
def test_replay_case_matches_operator_resolution_or_abstains(case_name: str):
    meta = _metadata(case_name)
    req = replay_case(case_name)

    if meta["expected_replay"] == "abstain_multi_file":
        deps = ma.MergerDeps(
            llm=_FakeLLM("LLM must not run for multi-file abstain"),
            pusher=_ExplodingPusher(),
            reviewer=_ExplodingReviewer(),
            test_runner=_passing_test_runner,
            audit=_audit_sink,
        )
        outcome = _run(ma.resolve_conflict(req, deps=deps))

        assert outcome.reason is ma.MergerReason.abstained_multi_file
        assert outcome.metadata["additional_files"] == req.additional_files
        return

    if meta["expected_replay"] == "abstain_oversized":
        deps = ma.MergerDeps(
            llm=_FakeLLM("LLM must not run for oversized abstain"),
            pusher=_ExplodingPusher(),
            reviewer=_ExplodingReviewer(),
            test_runner=_passing_test_runner,
            audit=_audit_sink,
        )
        outcome = _run(ma.resolve_conflict(req, deps=deps))

        assert outcome.reason is ma.MergerReason.abstained_oversized
        assert outcome.metadata["conflict_lines"] > ma.MAX_CONFLICT_LINES
        return

    expected = _read_fixture_text(_fixture_path(case_name, "after", req.file_path))
    blocks = _resolved_blocks(req.conflict_text, expected)
    deps = ma.MergerDeps(
        llm=_FakeLLM({
            "resolved_block": blocks[0],
            "resolved_blocks": blocks,
            "confidence": 0.99,
            "rationale": "OP-1430 replay of operator-resolved patch set",
            "new_logic_detected": False,
        }),
        review_llm=_FakeLLM("CONFIRM\nfixture output matches operator resolution"),
        pusher=_ExplodingPusher(),
        reviewer=_ExplodingReviewer(),
        test_runner=_passing_test_runner,
        audit=_audit_sink,
    )

    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.deferred_push_to_caller
    assert outcome.resolved_text == expected
    assert not _has_conflict_markers(outcome.resolved_text)


def test_change_932_replay_keeps_shared_setup_tests_split():
    req = replay_case("change_932")
    expected = _read_fixture_text(_fixture_path(
        "change_932", "after", req.file_path,
    ))
    blocks = _resolved_blocks(req.conflict_text, expected)
    deps = ma.MergerDeps(
        llm=_FakeLLM({
            "resolved_block": blocks[0],
            "resolved_blocks": blocks,
            "confidence": 0.99,
            "rationale": "OP-1435 replay preserves split test methods",
            "new_logic_detected": False,
        }),
        review_llm=_FakeLLM("CONFIRM\nfixture output keeps both test methods"),
        pusher=_ExplodingPusher(),
        reviewer=_ExplodingReviewer(),
        test_runner=_passing_test_runner,
        audit=_audit_sink,
    )

    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.deferred_push_to_caller
    assert outcome.resolved_text == expected
    assert outcome.resolved_text.count(
        "async def test_logs_arbiter_and_underlying_merger_reason"
    ) == 1
    assert outcome.resolved_text.count(
        "async def test_drift_verify_red_does_not_call_caller_push"
    ) == 1
    assert "merger_reason=refused_no_conflict" in outcome.resolved_text
    assert "verify_result=red" in outcome.resolved_text
    assert "SPLIT TESTS WITH SHARED SETUP" in deps.llm.calls[0]
    assert "do not collapse them into one merged test method" in deps.llm.calls[0]
