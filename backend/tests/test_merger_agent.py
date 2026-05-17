"""O6 (#269) — Merger Agent tests.

Covers the spec's six mandated integration scenarios plus unit tests
for the pure helpers:

  1. Simple conflict → Merger +2 + mock human +2 → submit allowed
     (submit-rule simulated — two +2 labels satisfy the dual-sign gate)
  2. Ambiguous / low-confidence conflict → Merger abstain (score 0)
  3. Security-sensitive file → Merger refuses, no push, no vote
  4. Test-gate failure → Merger does not push; escalation increments
  5. Merger +2 alone (no human +2) → submit-rule rejects
  6. Human +2 alone (no Merger +2) → submit-rule rejects

Plus:

  * 3-strike escalation: after MAX_FAILURES_PER_CHANGE failures the
    merger refuses to retry.
  * ``is_security_sensitive`` coverage for every pattern family.
  * ``parse_conflict_block`` single + multi-block.
  * ``new_logic_detected`` path forces abstain even at confidence 0.95.
  * Push fails but vote is never attempted.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from backend import merge_arbiter as arb
from backend import merger_agent as ma


# ──────────────────────────────────────────────────────────────
#  Fixtures / helpers
# ──────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


SIMPLE_CONFLICT = (
    "def greet(name):\n"
    "<<<<<<< HEAD\n"
    "    return f'Hello {name}!'\n"
    "=======\n"
    "    return f'Hi {name}!'\n"
    ">>>>>>> feature/greeting\n"
    "\n"
    "def farewell(name):\n"
    "    return f'Bye {name}!'\n"
)


OVERSIZED_CONFLICT_HEAD = "\n".join(f"line_head_{i}" for i in range(15))
OVERSIZED_CONFLICT_INCOMING = "\n".join(f"line_inc_{i}" for i in range(15))
OVERSIZED_CONFLICT = (
    "<<<<<<< HEAD\n"
    f"{OVERSIZED_CONFLICT_HEAD}\n"
    "=======\n"
    f"{OVERSIZED_CONFLICT_INCOMING}\n"
    ">>>>>>> feature/big\n"
)


def _base_request(
    *,
    file_path: str = "backend/greetings.py",
    conflict: str = SIMPLE_CONFLICT,
    change_id: str = "Iabc123",
    additional_files: list[str] | None = None,
) -> ma.ConflictRequest:
    return ma.ConflictRequest(
        change_id=change_id,
        project="omnisight",
        file_path=file_path,
        conflict_text=conflict,
        head_commit_message="friendly greeting",
        incoming_commit_message="shorter greeting",
        file_context="def greet(name):  # wrapper",
        patchset_revision="deadbeef",
        workspace="/tmp/fake-workspace",
        additional_files=additional_files or [],
    )


class _FakeLLM:
    """LLM double — returns a scripted JSON response."""

    def __init__(self, payload: dict[str, Any] | str | Exception,
                 tokens: int = 100) -> None:
        self.payload = payload
        self.tokens = tokens
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> tuple[str, int]:
        self.calls.append(prompt)
        if isinstance(self.payload, Exception):
            raise self.payload
        if isinstance(self.payload, str):
            return (self.payload, self.tokens)
        import json
        return (json.dumps(self.payload), self.tokens)


def _confirming_review_llm(tokens: int = 100) -> _FakeLLM:
    return _FakeLLM("CONFIRM\npreserves both sides' intent", tokens=tokens)


@dataclass
class _FakePusher:
    ok: bool = True
    sha: str = "abc12345"
    review_url: str = "http://gerrit.example.com/c/1"
    reason: str = ""
    calls: list[dict] = field(default_factory=list)

    async def push(self, **kwargs: Any) -> ma.PatchsetPushResult:
        self.calls.append(kwargs)
        return ma.PatchsetPushResult(
            ok=self.ok,
            sha=self.sha if self.ok else "",
            review_url=self.review_url if self.ok else "",
            reason=self.reason,
        )


@dataclass
class _FakeReviewer:
    ok: bool = True
    reason: str = ""
    calls: list[dict] = field(default_factory=list)

    async def post_review(self, **kwargs: Any) -> ma.ReviewerResult:
        self.calls.append(kwargs)
        return ma.ReviewerResult(ok=self.ok, reason=self.reason)


@dataclass
class _FakeHashtagSetter:
    ok: bool = True
    reason: str = ""
    raises: BaseException | None = None
    calls: list[dict] = field(default_factory=list)

    async def add_hashtag(self, **kwargs: Any) -> ma.HashtagSetterResult:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return ma.HashtagSetterResult(ok=self.ok, reason=self.reason)


def _test_runner(ok: bool = True, summary: str = "pytest x passed"):
    async def runner(_req: ma.ConflictRequest) -> ma.TestRunResult:
        return ma.TestRunResult(ok=ok, summary=summary, command="pytest -x")
    return runner


def _audit_sink():
    events: list[tuple[str, str, dict]] = []

    async def sink(action: str, entity_id: str, payload: dict) -> None:
        events.append((action, entity_id, payload))
    return events, sink


def _write_file(root, rel_path: str, text: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _sorted_components(components: list[set[str]]) -> list[list[str]]:
    return sorted(sorted(component) for component in components)


@pytest.fixture(autouse=True)
def _reset_counters():
    ma.reset_failure_counts_for_tests()
    yield
    ma.reset_failure_counts_for_tests()


# ──────────────────────────────────────────────────────────────
#  Pure helpers
# ──────────────────────────────────────────────────────────────


class TestParseConflict:

    def test_single_block(self):
        blocks = ma.parse_conflict_block(SIMPLE_CONFLICT)
        assert len(blocks) == 1
        b = blocks[0]
        assert b.head_lines == ["    return f'Hello {name}!'"]
        assert b.incoming_lines == ["    return f'Hi {name}!'"]
        assert b.head_label == "HEAD"
        assert b.incoming_label == "feature/greeting"
        assert b.n_conflict_lines == 2

    def test_multiple_blocks(self):
        text = (
            "prefix\n"
            "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> f1\n"
            "middle\n"
            "<<<<<<< HEAD\nC\nD\n=======\nE\n>>>>>>> f2\n"
            "suffix\n"
        )
        blocks = ma.parse_conflict_block(text)
        assert len(blocks) == 2
        assert blocks[0].head_lines == ["A"]
        assert blocks[1].head_lines == ["C", "D"]
        assert blocks[1].incoming_label == "f2"

    def test_no_conflict(self):
        assert ma.parse_conflict_block("no markers here\n") == []

    @pytest.mark.parametrize(
        ("text", "head_lines", "incoming_lines", "incoming_label"),
        [
            (
                "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> feature/normal\n",
                ["A"],
                ["B"],
                "feature/normal",
            ),
            (
                "<<<<<<< HEAD\n=======\nB\n>>>>>>> feature/empty-head\n",
                [],
                ["B"],
                "feature/empty-head",
            ),
            (
                "<<<<<<< HEAD\nA\n=======\n>>>>>>> feature/empty-incoming\n",
                ["A"],
                [],
                "feature/empty-incoming",
            ),
            (
                "<<<<<<< HEAD\r\nA\r\n=======\r\nB\r\n>>>>>>> feature/crlf\r\n",
                ["A"],
                ["B"],
                "feature/crlf",
            ),
            (
                "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> feature/no-eof-newline",
                ["A"],
                ["B"],
                "feature/no-eof-newline",
            ),
            (
                "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>>\n",
                ["A"],
                ["B"],
                "",
            ),
            (
                "<<<<<<<\n=======\n>>>>>>>\n",
                [],
                [],
                "",
            ),
        ],
    )
    def test_operator_audit_fixture_cases(
        self,
        text: str,
        head_lines: list[str],
        incoming_lines: list[str],
        incoming_label: str,
    ):
        blocks = ma.parse_conflict_block(text)
        assert len(blocks) == 1
        assert blocks[0].head_lines == head_lines
        assert blocks[0].incoming_lines == incoming_lines
        assert blocks[0].incoming_label == incoming_label

    def test_operator_audit_nested_markers_flagged(self):
        text = (
            "<<<<<<< HEAD\n"
            "outer head\n"
            "<<<<<<< HEAD\n"
            "inner head\n"
            "=======\n"
            "inner incoming\n"
            ">>>>>>> feature/inner\n"
            "=======\n"
            "outer incoming\n"
            ">>>>>>> feature/outer\n"
        )
        blocks = ma.parse_conflict_block(text)
        assert len(blocks) == 1
        assert blocks[0].has_nested_markers is True


class TestClassifyConflictRisk:

    def test_single_file_narrow_conflict_is_low(self):
        conflict = (
            "<<<<<<< HEAD\n"
            "count = 1\n"
            "=======\n"
            "count = 2\n"
            ">>>>>>> feature/greeting\n"
        )
        req = _base_request(conflict=conflict)
        blocks = ma.parse_conflict_block(req.conflict_text)

        risk = ma.classify_conflict_risk(req, blocks)

        assert risk.tier is ma.MergerRiskTier.low
        assert risk.reasons == ()

    def test_single_file_signature_overlap_is_medium(self):
        conflict = (
            "<<<<<<< HEAD\n"
            "def greet(name):\n"
            "    return f'Hello {name}!'\n"
            "=======\n"
            "def greet(person):\n"
            "    return f'Hi {person}!'\n"
            ">>>>>>> feature/greeting\n"
        )
        req = _base_request(conflict=conflict)
        blocks = ma.parse_conflict_block(req.conflict_text)

        risk = ma.classify_conflict_risk(req, blocks)

        assert risk.tier is ma.MergerRiskTier.medium
        assert "signature_overlap" in risk.reasons

    def test_multi_file_low_coupling_is_medium(self):
        req = _base_request(additional_files=["backend/alpha.py"])
        blocks = ma.parse_conflict_block(req.conflict_text)

        risk = ma.classify_conflict_risk(
            req,
            blocks,
            coupling_components=[
                {"backend/greetings.py"},
                {"backend/alpha.py"},
            ],
        )

        assert risk.tier is ma.MergerRiskTier.medium
        assert "multi_file_low_coupling" in risk.reasons

    def test_multi_file_high_coupling_that_fits_batch_is_medium(self):
        req = _base_request(additional_files=["backend/alpha.py"])
        blocks = ma.parse_conflict_block(req.conflict_text)

        risk = ma.classify_conflict_risk(
            req,
            blocks,
            coupling_components=[
                {"backend/greetings.py", "backend/alpha.py"},
            ],
        )

        assert risk.tier is ma.MergerRiskTier.medium
        assert "multi_file_high_coupling" in risk.reasons

    def test_real_multi_file_conflict_tier_matches_manual_classification(
        self,
        tmp_path,
    ):
        _write_file(
            tmp_path,
            "backend/greetings.py",
            "from backend.alpha import Alpha\n\nvalue = Alpha()\n",
        )
        _write_file(tmp_path, "backend/alpha.py", "class Alpha:\n    pass\n")
        req = _base_request(additional_files=["backend/alpha.py"])
        req.workspace = str(tmp_path)
        blocks = ma.parse_conflict_block(req.conflict_text)
        coupling_components = ma._classify_coupling(
            [req.file_path, *req.additional_files],
            req.workspace,
        )

        risk = ma.classify_conflict_risk(req, blocks, coupling_components)

        assert _sorted_components(coupling_components) == [
            ["backend/alpha.py", "backend/greetings.py"],
        ]
        assert risk.tier is ma.MergerRiskTier.medium
        assert "multi_file_high_coupling" in risk.reasons

    def test_data_flow_coupled_conflict_routes_high_coupling(self, tmp_path):
        conflict = (
            "<<<<<<< HEAD\n"
            "payload[\"caller_workspace\"] = workspace\n"
            "=======\n"
            "payload[\"caller_workspace\"] = request.workspace\n"
            ">>>>>>> feature/caller-workspace\n"
        )
        _write_file(
            tmp_path,
            "backend/webhooks.py",
            "def enqueue(workspace):\n"
            "    return {\"caller_workspace\": workspace}\n",
        )
        _write_file(
            tmp_path,
            "backend/merge_arbiter.py",
            "def verify(task):\n"
            "    return task.caller_workspace\n",
        )
        _write_file(
            tmp_path,
            "backend/tests/test_proactive_merger_op714.py",
            "def test_caller_workspace(payload):\n"
            "    assert payload[\"caller_workspace\"].endswith(\"runner\")\n",
        )
        req = _base_request(
            file_path="backend/webhooks.py",
            conflict=conflict,
            additional_files=[
                "backend/merge_arbiter.py",
                "backend/tests/test_proactive_merger_op714.py",
            ],
        )
        req.workspace = str(tmp_path)
        blocks = ma.parse_conflict_block(req.conflict_text)
        coupling_components = ma._classify_coupling(
            [req.file_path, *req.additional_files],
            req.workspace,
            req.conflict_text,
        )

        risk = ma.classify_conflict_risk(req, blocks, coupling_components)

        assert _sorted_components(coupling_components) == [
            [
                "backend/merge_arbiter.py",
                "backend/tests/test_proactive_merger_op714.py",
                "backend/webhooks.py",
            ],
        ]
        assert risk.tier is ma.MergerRiskTier.medium
        assert "multi_file_high_coupling" in risk.reasons

    def test_multi_file_high_coupling_oversize_is_high(self):
        req = _base_request(
            conflict=OVERSIZED_CONFLICT,
            additional_files=["backend/alpha.py"],
        )
        blocks = ma.parse_conflict_block(req.conflict_text)

        risk = ma.classify_conflict_risk(
            req,
            blocks,
            coupling_components=[
                {"backend/greetings.py", "backend/alpha.py"},
            ],
        )

        assert risk.tier is ma.MergerRiskTier.high
        assert "multi_file_coupled_oversize" in risk.reasons

    def test_multi_file_count_exceeding_batch_is_high(self):
        req = _base_request(
            additional_files=[
                "backend/alpha.py",
                "backend/beta.py",
                "backend/gamma.py",
                "backend/delta.py",
            ],
        )
        blocks = ma.parse_conflict_block(req.conflict_text)

        risk = ma.classify_conflict_risk(
            req,
            blocks,
            coupling_components=[
                {"backend/greetings.py"},
                {"backend/alpha.py"},
                {"backend/beta.py"},
                {"backend/gamma.py"},
                {"backend/delta.py"},
            ],
        )

        assert risk.tier is ma.MergerRiskTier.high
        assert "multi_file_count_exceeds_batch" in risk.reasons


class TestClassifyCoupling:

    def test_independent_files_stay_separate(self, tmp_path):
        _write_file(tmp_path, "backend/alpha.py", "def alpha():\n    return 1\n")
        _write_file(tmp_path, "backend/beta.py", "def beta():\n    return 2\n")

        components = ma._classify_coupling(
            ["backend/alpha.py", "backend/beta.py"], str(tmp_path),
        )

        assert _sorted_components(components) == [
            ["backend/alpha.py"],
            ["backend/beta.py"],
        ]

    def test_import_edge_connects_files(self, tmp_path):
        _write_file(
            tmp_path,
            "backend/alpha.py",
            "from backend.beta import Beta\n\nvalue = Beta()\n",
        )
        _write_file(tmp_path, "backend/beta.py", "class Beta:\n    pass\n")

        components = ma._classify_coupling(
            ["backend/alpha.py", "backend/beta.py"], str(tmp_path),
        )

        assert _sorted_components(components) == [
            ["backend/alpha.py", "backend/beta.py"],
        ]

    def test_symbol_overlap_connects_references(self, tmp_path):
        _write_file(tmp_path, "backend/provider.py", "def shared_symbol():\n    pass\n")
        _write_file(
            tmp_path,
            "backend/consumer.py",
            "def call():\n    return shared_symbol()\n",
        )

        components = ma._classify_coupling(
            ["backend/provider.py", "backend/consumer.py"], str(tmp_path),
        )

        assert _sorted_components(components) == [
            ["backend/consumer.py", "backend/provider.py"],
        ]

    def test_test_source_pair_connects_file_and_test(self, tmp_path):
        _write_file(tmp_path, "backend/widget.py", "def render():\n    return 'ok'\n")
        _write_file(
            tmp_path,
            "backend/tests/test_widget.py",
            "def test_render():\n    assert True\n",
        )

        components = ma._classify_coupling(
            ["backend/widget.py", "backend/tests/test_widget.py"], str(tmp_path),
        )

        assert _sorted_components(components) == [
            ["backend/tests/test_widget.py", "backend/widget.py"],
        ]

    def test_mixed_signals_form_transitive_components(self, tmp_path):
        _write_file(
            tmp_path,
            "backend/alpha.py",
            "from backend.beta import Beta\n\ndef build():\n    return Beta()\n",
        )
        _write_file(tmp_path, "backend/beta.py", "class Beta:\n    pass\n")
        _write_file(
            tmp_path,
            "backend/gamma.py",
            "def use_beta():\n    return Beta()\n",
        )
        _write_file(tmp_path, "backend/loose.py", "def loose():\n    return None\n")

        components = ma._classify_coupling(
            [
                "backend/alpha.py",
                "backend/beta.py",
                "backend/gamma.py",
                "backend/loose.py",
            ],
            str(tmp_path),
        )

        assert _sorted_components(components) == [
            ["backend/alpha.py", "backend/beta.py", "backend/gamma.py"],
            ["backend/loose.py"],
        ]

    def test_data_flow_field_connects_three_file_contract(self, tmp_path):
        conflict = (
            "<<<<<<< HEAD\n"
            "payload[\"caller_workspace\"] = workspace\n"
            "=======\n"
            "payload[\"caller_workspace\"] = request.workspace\n"
            ">>>>>>> feature/caller-workspace\n"
        )
        _write_file(
            tmp_path,
            "backend/webhooks.py",
            "def enqueue(workspace):\n"
            "    return {\"caller_workspace\": workspace}\n",
        )
        _write_file(
            tmp_path,
            "backend/merge_arbiter.py",
            "def verify(task):\n"
            "    return task.caller_workspace\n",
        )
        _write_file(
            tmp_path,
            "backend/tests/test_proactive_merger_op714.py",
            "def test_caller_workspace(payload):\n"
            "    assert payload[\"caller_workspace\"].endswith(\"runner\")\n",
        )

        components = ma._classify_coupling(
            [
                "backend/webhooks.py",
                "backend/merge_arbiter.py",
                "backend/tests/test_proactive_merger_op714.py",
            ],
            str(tmp_path),
            conflict,
        )

        assert _sorted_components(components) == [
            [
                "backend/merge_arbiter.py",
                "backend/tests/test_proactive_merger_op714.py",
                "backend/webhooks.py",
            ],
        ]

    def test_data_flow_field_requires_whole_conflict_set(self, tmp_path):
        conflict = (
            "<<<<<<< HEAD\n"
            "payload[\"caller_workspace\"] = workspace\n"
            "=======\n"
            "payload[\"caller_workspace\"] = request.workspace\n"
            ">>>>>>> feature/caller-workspace\n"
        )
        _write_file(
            tmp_path,
            "backend/webhooks.py",
            "def enqueue(workspace):\n"
            "    return {\"caller_workspace\": workspace}\n",
        )
        _write_file(
            tmp_path,
            "backend/merge_arbiter.py",
            "def verify(task):\n"
            "    return task.caller_workspace\n",
        )
        _write_file(
            tmp_path,
            "backend/tests/test_proactive_merger_op714.py",
            "def test_caller_workspace(payload):\n"
            "    assert payload[\"caller_workspace\"].endswith(\"runner\")\n",
        )

        components = ma._classify_coupling(
            ["backend/webhooks.py", "backend/merge_arbiter.py"],
            str(tmp_path),
            conflict,
        )

        assert _sorted_components(components) == [
            ["backend/merge_arbiter.py"],
            ["backend/webhooks.py"],
        ]

    def test_syntax_errors_degrade_to_singleton(self, tmp_path):
        _write_file(tmp_path, "backend/broken.py", "def broken(:\n")
        _write_file(tmp_path, "backend/ok.py", "def ok():\n    return True\n")

        components = ma._classify_coupling(
            ["backend/broken.py", "backend/ok.py"], str(tmp_path),
        )

        assert _sorted_components(components) == [
            ["backend/broken.py"],
            ["backend/ok.py"],
        ]


class TestSecuritySensitive:

    @pytest.mark.parametrize("p", [
        "backend/auth/session.py",
        "secrets/production.env",
        "config/production.yml",
        ".github/workflows/ci.yml",
        "docker-compose.yml",
        "Dockerfile.backend",
        "ci/deploy.sh",
        "backend/authentication/jwt.py",
    ])
    def test_security_paths_match(self, p: str):
        assert ma.is_security_sensitive(p)

    @pytest.mark.parametrize("p", [
        "backend/greetings.py",
        "src/main.c",
        "app/routes/hello.ts",
        "docs/README.md",
    ])
    def test_non_security_paths_pass(self, p: str):
        assert not ma.is_security_sensitive(p)


# ──────────────────────────────────────────────────────────────
#  Scenario 1 — Merger +2 (simple conflict)
# ──────────────────────────────────────────────────────────────


def test_scenario_simple_conflict_merger_plus_two():
    """Spec scenario: simple conflict + mock human +2 → submit allowed.

    We model the submit-rule as a local helper that simulates the Gerrit
    rule in O7 (two +2 labels from distinct actors, one human one bot).
    """
    llm = _FakeLLM({
        "resolved_block": "    return f'Hello {name}!'\n",
        "confidence": 0.95,
        "rationale": "HEAD intent preserved; incoming differed only in wording",
        "new_logic_detected": False,
    })
    pusher = _FakePusher()
    reviewer = _FakeReviewer()
    events, audit = _audit_sink()
    deps = ma.MergerDeps(
        llm=llm, pusher=pusher, reviewer=reviewer,
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True), audit=audit,
    )

    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

    assert outcome.reason is ma.MergerReason.plus_two_voted
    assert int(outcome.voted_score) == 2
    assert outcome.push_sha == "abc12345"
    assert outcome.confidence == pytest.approx(0.95)
    assert pusher.calls, "pusher was never called"
    assert reviewer.calls, "reviewer was never called"
    assert reviewer.calls[0]["score"] == 2

    # Submit-rule simulation: two +2 labels from distinct actor-groups
    votes = [("merger-agent-bot", int(outcome.voted_score)),
             ("human-alice", 2)]
    assert _simulate_submit_rule(votes) == "allow"

    # Audit captured the +2
    assert any(a == "merger.plus_two_voted" for a, _, _ in events)


def test_op1406_confirmed_sandwich_pushes_after_second_llm_agrees():
    proposer = _FakeLLM({
        "resolved_block": "    return f'Hello {name}!'\n",
        "confidence": 0.95,
        "rationale": "HEAD wording preserves the greeting intent",
        "new_logic_detected": False,
    }, tokens=200)
    reviewer_llm = _FakeLLM(
        "CONFIRM\nBoth sides only differ in wording; intent is preserved.",
        tokens=50,
    )
    pusher = _FakePusher()
    deps = ma.MergerDeps(
        llm=proposer,
        review_llm=reviewer_llm,
        pusher=pusher,
        reviewer=_FakeReviewer(),
        test_runner=_test_runner(True),
    )

    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

    assert outcome.reason is ma.MergerReason.plus_two_voted
    assert pusher.calls
    assert len(proposer.calls) == 1
    assert len(reviewer_llm.calls) == 1
    assert "Original conflict:" in reviewer_llm.calls[0]
    assert "LLM-A proposed resolved file:" in reviewer_llm.calls[0]
    assert outcome.metadata["merger_sandwich_decision"] == "confirm"
    assert outcome.metadata["review_model"] == ma.REVIEW_MODEL


def test_op1406_review_object_abstains_and_keeps_transcripts():
    proposer = _FakeLLM({
        "resolved_block": "    return f'Hello {name}!'\n",
        "confidence": 0.95,
        "rationale": "HEAD wording only",
        "new_logic_detected": False,
    })
    reviewer_llm = _FakeLLM(
        "OBJECT\nIncoming side wanted the shorter greeting; rationale is thin."
    )
    pusher = _FakePusher()
    deps = ma.MergerDeps(
        llm=proposer,
        review_llm=reviewer_llm,
        pusher=pusher,
        reviewer=_FakeReviewer(),
        test_runner=_test_runner(True),
    )

    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

    assert outcome.reason is ma.MergerReason.refused_review_objected
    assert pusher.calls == []
    assert outcome.metadata["merger_sandwich_decision"] == "object"
    assert "resolved_block" in outcome.metadata["proposal_response"]
    assert "OBJECT" in outcome.metadata["review_response"]
    assert "Original conflict:" in outcome.metadata["review_prompt"]


def test_op1413_arbiter_routes_review_object_with_jira_transcripts():
    class _Jira:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.descriptions: list[str] = []

        async def open_abstain_ticket(self, **kwargs: Any) -> arb.JiraTicketResult:
            self.calls.append(kwargs)
            self.descriptions.append(arb.build_abstain_ticket_description(**kwargs))
            return arb.JiraTicketResult(ok=True, ticket="OP-MERGE-1")

    class _Notifier:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def notify(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    async def merger(req: ma.ConflictRequest) -> ma.ResolutionOutcome:
        deps = ma.MergerDeps(
            llm=_FakeLLM({
                "resolved_block": "    return f'Hello {name}!'\n",
                "confidence": 0.95,
                "rationale": "HEAD wording only",
                "new_logic_detected": False,
            }),
            review_llm=_FakeLLM("OBJECT\nDoes not justify dropping incoming."),
            pusher=_FakePusher(),
            reviewer=_FakeReviewer(),
            test_runner=_test_runner(True),
        )
        return await ma.resolve_conflict(req, deps=deps)

    jira = _Jira()
    notifier = _Notifier()
    task = arb.MergeConflictTask(
        change_id="Iabc123",
        project="omnisight",
        file_path="backend/greetings.py",
        conflict_text=SIMPLE_CONFLICT,
        push_locally=True,
        jira_ticket="OP-1413",
    )
    outcome = _run(arb.on_merge_conflict_webhook(
        task,
        deps=arb.ArbiterDeps(merger=merger, jira=jira, notifier=notifier),
    ))

    assert outcome.reason is arb.ArbiterReason.merger_abstained_jira_ticket_opened
    assert jira.calls
    assert jira.descriptions
    description = jira.descriptions[0]
    assert "## 2-LLM Sandwich Disagreement" in description
    assert "LLM-A proposal transcript" in description
    assert "LLM-B review transcript" in description
    assert "Original conflict:" in description
    assert "resolved_block" in description
    assert "OBJECT" in description
    assert "<details><summary>" in description
    assert notifier.calls
    payload = notifier.calls[0]["payload"]
    assert payload["merger_sandwich_decision"] == "object"
    assert "resolved_block" in payload["proposal_transcript"]
    assert "OBJECT" in payload["review_transcript"]


# ──────────────────────────────────────────────────────────────
#  Scenario 2 — Ambiguous conflict → abstain
# ──────────────────────────────────────────────────────────────


def test_scenario_low_confidence_abstain():
    llm = _FakeLLM({
        "resolved_block": "    return f'{name}!'\n",
        "confidence": 0.55,
        "rationale": "both halves differ semantically; cannot decide",
        "new_logic_detected": False,
    })
    pusher = _FakePusher()
    reviewer = _FakeReviewer()
    deps = ma.MergerDeps(
        llm=llm, pusher=pusher, reviewer=reviewer,
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )

    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

    assert outcome.reason is ma.MergerReason.abstained_low_confidence
    assert int(outcome.voted_score) == 0
    assert pusher.calls == []
    assert reviewer.calls == []


def test_new_logic_forces_abstain_even_at_high_confidence():
    llm = _FakeLLM({
        "resolved_block": "    return greet_helper(name)\n",
        "confidence": 0.95,
        "rationale": "Both halves combined via new helper greet_helper",
        "new_logic_detected": True,
    })
    deps = ma.MergerDeps(
        llm=llm, pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))
    assert outcome.reason is ma.MergerReason.refused_new_logic_detected
    assert int(outcome.voted_score) == 0
    # Confidence clamped
    assert outcome.confidence <= 0.3


# ──────────────────────────────────────────────────────────────
#  Scenario 3 — Security-sensitive file → refuse
# ──────────────────────────────────────────────────────────────


def test_scenario_security_file_refusal():
    llm = _FakeLLM({
        "resolved_block": "ok\n",
        "confidence": 0.99,
        "rationale": "trivial",
        "new_logic_detected": False,
    })
    pusher = _FakePusher()
    reviewer = _FakeReviewer()
    deps = ma.MergerDeps(
        llm=llm, pusher=pusher, reviewer=reviewer,
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    req = _base_request(
        file_path=".github/workflows/ci.yml",
        conflict=SIMPLE_CONFLICT,
    )
    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.refused_security_file
    assert int(outcome.voted_score) == 0
    # LLM NEVER called on security-file refusal.
    assert llm.calls == []
    assert pusher.calls == []
    assert reviewer.calls == []


# ──────────────────────────────────────────────────────────────
#  Scenario 4 — Test gate fails → no push, escalation
# ──────────────────────────────────────────────────────────────


def test_scenario_test_failure_blocks_push():
    llm = _FakeLLM({
        "resolved_block": "ok\n",
        "confidence": 0.95,
        "rationale": "trivial",
        "new_logic_detected": False,
    })
    pusher = _FakePusher()
    reviewer = _FakeReviewer()
    deps = ma.MergerDeps(
        llm=llm, pusher=pusher, reviewer=reviewer,
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(False, "2 failed"),
    )
    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

    assert outcome.reason is ma.MergerReason.refused_test_failure
    assert pusher.calls == []
    assert reviewer.calls == []
    assert outcome.test_result and outcome.test_result["ok"] is False
    # Failure counter bumped for the 3-strike rule
    assert ma.get_failure_count("Iabc123") == 1


# ──────────────────────────────────────────────────────────────
#  Scenario 5 — Merger +2 alone → submit-rule rejects
# ──────────────────────────────────────────────────────────────


def test_scenario_merger_only_submit_rejected():
    """Simulate: merger gave +2 but human never did — submit rule must reject."""
    votes = [("merger-agent-bot", 2)]
    assert _simulate_submit_rule(votes) == "reject_missing_human"


# ──────────────────────────────────────────────────────────────
#  Scenario 6 — Human +2 alone → submit-rule rejects
# ──────────────────────────────────────────────────────────────


def test_scenario_human_only_submit_rejected():
    """Simulate: human +2 without merger-agent +2 — rule still rejects
    because the conflict-correctness signal is missing."""
    votes = [("human-alice", 2)]
    assert _simulate_submit_rule(votes) == "reject_missing_merger"


# Additional critical path: N AI +2 without human still rejects.
def test_many_ai_votes_without_human_rejected():
    votes = [
        ("merger-agent-bot", 2),
        ("lint-bot", 2),
        ("security-bot", 2),
    ]
    assert _simulate_submit_rule(votes) == "reject_missing_human"


# ──────────────────────────────────────────────────────────────
#  Multi-file gate + oversized gate + no-conflict + push failure
# ──────────────────────────────────────────────────────────────


def test_multi_file_abstain(tmp_path, caplog):
    _write_file(tmp_path, "backend/greetings.py", "def greet(name):\n    return name\n")
    _write_file(
        tmp_path,
        "backend/utils.py",
        "from backend.greetings import greet\n\nvalue = greet('Ada')\n",
    )
    llm = _FakeLLM({
        "resolved_block": "ok\n", "confidence": 0.99,
        "rationale": "trivial", "new_logic_detected": False,
    })
    deps = ma.MergerDeps(
        llm=llm, pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    req = _base_request(additional_files=["backend/utils.py"])
    req.workspace = str(tmp_path)
    req.jira_ticket = "OP-1424"
    caplog.set_level(logging.INFO, logger=ma.__name__)

    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.abstained_multi_file
    assert llm.calls == []
    assert outcome.metadata["coupling_components"] == [
        ["backend/greetings.py", "backend/utils.py"],
    ]
    assert "multi-file coupling summary jira=OP-1424" in caplog.text
    assert "backend/utils.py" in caplog.text


def test_oversized_conflict_abstain():
    deps = ma.MergerDeps(
        llm=_FakeLLM({"resolved_block": "x", "confidence": 0.99,
                      "rationale": "", "new_logic_detected": False}),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    req = _base_request(conflict=OVERSIZED_CONFLICT)
    outcome = _run(ma.resolve_conflict(req, deps=deps))
    assert outcome.reason is ma.MergerReason.abstained_oversized


def test_no_conflict_refusal():
    deps = ma.MergerDeps(
        llm=_FakeLLM({"resolved_block": "", "confidence": 0.99,
                      "rationale": "", "new_logic_detected": False}),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    req = _base_request(conflict="no markers here\n")
    outcome = _run(ma.resolve_conflict(req, deps=deps))
    assert outcome.reason is ma.MergerReason.refused_no_conflict


def test_nested_conflict_markers_refused_before_llm(caplog):
    llm = _FakeLLM({
        "resolved_block": "should not be used\n",
        "confidence": 0.99,
        "rationale": "",
        "new_logic_detected": False,
    })
    deps = ma.MergerDeps(
        llm=llm,
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    conflict = (
        "<<<<<<< HEAD\n"
        "outer head\n"
        "<<<<<<< HEAD\n"
        "inner head\n"
        "=======\n"
        "inner incoming\n"
        ">>>>>>> feature/inner\n"
        "=======\n"
        "outer incoming\n"
        ">>>>>>> feature/outer\n"
    )
    with caplog.at_level(logging.WARNING):
        outcome = _run(ma.resolve_conflict(
            _base_request(conflict=conflict), deps=deps,
        ))

    assert outcome.reason is ma.MergerReason.refused_nested_markers
    assert outcome.metadata == {"nested_marker_start_lines": [1]}
    assert llm.calls == []
    assert "nested_marker_warning" in caplog.text


def test_crlf_conflict_file_processes_deferred_resolution():
    llm = _FakeLLM({
        "resolved_block": "    return f'Hello {name}!'\r\n",
        "confidence": 0.95,
        "rationale": "keeps greeting behavior",
        "new_logic_detected": False,
    })
    deps = ma.MergerDeps(
        llm=llm,
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    conflict = (
        "def greet(name):\r\n"
        "<<<<<<< HEAD\r\n"
        "    return f'Hello {name}!'\r\n"
        "=======\r\n"
        "    return f'Hi {name}!'\r\n"
        ">>>>>>> feature/greeting\r\n"
    )
    req = _base_request(conflict=conflict)
    req.push_locally = False

    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.deferred_push_to_caller
    assert llm.calls
    assert "<<<<<<<" not in outcome.resolved_text
    assert "return f'Hello {name}!'" in outcome.resolved_text


def test_all_empty_conflict_hunk_processes_as_real_conflict():
    llm = _FakeLLM({
        "resolved_block": "",
        "confidence": 0.95,
        "rationale": "delete empty conflict hunk",
        "new_logic_detected": False,
    })
    deps = ma.MergerDeps(
        llm=llm,
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    req = _base_request(conflict="prefix\n<<<<<<<\n=======\n>>>>>>>\nsuffix\n")
    req.push_locally = False

    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.deferred_push_to_caller
    assert llm.calls
    assert outcome.resolved_text == "prefix\nsuffix\n"


def test_push_fail_no_vote_and_escalates():
    llm = _FakeLLM({
        "resolved_block": "ok\n", "confidence": 0.95,
        "rationale": "trivial", "new_logic_detected": False,
    })
    pusher = _FakePusher(ok=False, reason="SSH key rejected")
    reviewer = _FakeReviewer()
    deps = ma.MergerDeps(
        llm=llm, pusher=pusher, reviewer=reviewer,
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))
    assert outcome.reason is ma.MergerReason.refused_push_failed
    assert reviewer.calls == []
    assert ma.get_failure_count("Iabc123") == 1


def test_llm_unavailable_abstain_and_counts():
    deps = ma.MergerDeps(
        llm=_FakeLLM(""),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))
    assert outcome.reason is ma.MergerReason.refused_llm_unavailable
    assert ma.get_failure_count("Iabc123") == 1


def test_llm_invalid_json_abstain_and_counts():
    deps = ma.MergerDeps(
        llm=_FakeLLM("this is not JSON"),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))
    assert outcome.reason is ma.MergerReason.refused_llm_invalid_json
    assert ma.get_failure_count("Iabc123") == 1


def test_three_strike_escalation_refuses_retry():
    """3 consecutive LLM failures → the 4th attempt is refused outright."""
    deps = ma.MergerDeps(
        llm=_FakeLLM(""),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    for _ in range(ma.MAX_FAILURES_PER_CHANGE):
        out = _run(ma.resolve_conflict(_base_request(), deps=deps))
        assert int(out.voted_score) == 0

    assert ma.get_failure_count("Iabc123") == ma.MAX_FAILURES_PER_CHANGE
    final = _run(ma.resolve_conflict(_base_request(), deps=deps))
    assert final.reason is ma.MergerReason.refused_escalated


def test_success_resets_failure_counter():
    """After an abstain + a success the counter is back to 0."""
    deps_fail = ma.MergerDeps(
        llm=_FakeLLM(""),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    _run(ma.resolve_conflict(_base_request(), deps=deps_fail))
    assert ma.get_failure_count("Iabc123") == 1

    deps_ok = ma.MergerDeps(
        llm=_FakeLLM({
            "resolved_block": "ok\n", "confidence": 0.95,
            "rationale": "ok", "new_logic_detected": False,
        }),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    out = _run(ma.resolve_conflict(_base_request(), deps=deps_ok))
    assert out.reason is ma.MergerReason.plus_two_voted
    assert ma.get_failure_count("Iabc123") == 0


# ──────────────────────────────────────────────────────────────
#  Prompt construction
# ──────────────────────────────────────────────────────────────


def test_build_prompt_contains_system_and_blocks():
    blocks = ma.parse_conflict_block(SIMPLE_CONFLICT)
    prompt = ma.build_prompt(_base_request(), blocks)
    assert "merge conflict resolution expert" in prompt
    assert ma.MERGER_PROMPT_VERSION in prompt
    assert "HEAD" in prompt
    assert "feature/greeting" in prompt
    assert "backend/greetings.py" in prompt


def test_op1435_prompt_contains_take_both_rubric_and_version():
    blocks = ma.parse_conflict_block(SIMPLE_CONFLICT)
    prompt = ma.build_prompt(_base_request(), blocks)

    assert ma.MERGER_PROMPT_VERSION == "merger-prompt-v3-op1435"
    assert "Take-both feature-preservation rubric" in prompt
    assert "ADD-vs-ADD distinct symbols" in prompt
    assert "RENAME DETECTION" in prompt
    assert "DOCSTRING COLLISION" in prompt
    assert "SIGNATURE OVERLAP" in prompt
    assert "SPLIT TESTS WITH SHARED SETUP" in prompt
    assert "ABSTAIN BIAS" in prompt
    assert "both sides add distinct symbols" in prompt
    assert "same shape, similar docstring, or" in prompt
    assert "identical body" in prompt
    assert "docstring entries for the same parameter" in prompt
    assert "keep both parameters in the resolved signature" in prompt
    assert "do not collapse them into one merged test method" in prompt
    assert "Duplicate the complete setup in each method" in prompt
    assert "name the unresolved reason class" in prompt


def test_context_pack_contains_full_conflict_file_git_log_and_jira_desc():
    conflict = (
        "class GuildRunner:\n"
        "    def resolve(self, guild):\n"
        "        guild_id = guild.id\n"
        "<<<<<<< HEAD\n"
        "        return self.lookup(guild)\n"
        "=======\n"
        "        return self.lookup_by_id(guild_id)\n"
        ">>>>>>> feature/guild-id\n"
        "\n"
        "    def audit(self, guild_id):\n"
        "        return guild_id\n"
    )
    req = _base_request(file_path="backend/agents/llm.py", conflict=conflict)
    req.jira_ticket = "OP-1403"
    req.jira_description = "Teach merger bot to preserve guild and guild_id semantics."
    req.sibling_file_contents = {
        "backend/agents/nodes.py": "def node(guild_id):\n    return guild_id\n",
        "backend/metrics.py": "guild_id_metric = 'guild_id'\n",
    }
    req.git_logs = {
        "backend/agents/llm.py": (
            "commit abc123\n"
            "Author: test\n\n"
            "    Preserve guild object while adding guild_id callsites\n"
        )
    }
    req.symbol_table = {
        "backend/agents/llm.py": (
            "class GuildRunner line 1 calls: lookup, lookup_by_id"
        )
    }

    blocks = ma.parse_conflict_block(conflict)
    pack = ma.build_context_pack(req, blocks)

    assert "class GuildRunner" in pack
    assert "def audit(self, guild_id)" in pack
    assert "Preserve guild object while adding guild_id callsites" in pack
    assert "Teach merger bot to preserve guild and guild_id semantics" in pack
    assert "def node(guild_id)" in pack
    assert "class GuildRunner line 1 calls" in pack


def test_context_pack_size_cap_prioritises_conflict_then_git_log(monkeypatch):
    monkeypatch.setattr(ma, "CONTEXT_PACK_TOKEN_LIMIT", 40)
    conflict = "important_conflict_line\n" * 20
    req = _base_request(conflict=conflict)
    req.jira_description = "jira text that should lose to earlier sections"
    req.sibling_file_contents = {
        "backend/other.py": "sibling text that should be truncated"
    }
    req.git_logs = {req.file_path: "recent git intent\n"}

    pack = ma.build_context_pack(req, [])

    assert "important_conflict_line" in pack
    assert "recent git intent" in pack or "[context-pack truncated]" in pack
    assert len(pack) <= ma.CONTEXT_PACK_TOKEN_LIMIT * 4
    assert "sibling text that should be truncated" not in pack


def test_prompt_size_gate_trims_context_pack_sections(monkeypatch, caplog):
    req = _base_request()
    req.jira_ticket = "OP-1420"
    req.jira_description = "keep jira context"
    req.git_logs = {req.file_path: "GIT_CONTEXT " * 9000}
    req.sibling_file_contents = {"backend/sibling.py": "SIBLING_CONTEXT " * 9000}
    req.symbol_table = {"backend/greetings.py": "SYMBOL_CONTEXT " * 9000}
    req.push_locally = False

    blocks = ma.parse_conflict_block(req.conflict_text)
    risk = ma.classify_conflict_risk(req, blocks)
    trimmed_context = ma.build_context_pack(
        req,
        blocks,
        omit_sections=("git_log", "sibling_files", "symbol_table"),
    )
    trimmed_prompt = ma.build_prompt(
        req, blocks, risk, context_pack=trimmed_context,
    )
    monkeypatch.setattr(
        ma,
        "PROMPT_INPUT_HARD_LIMIT_BYTES",
        len(trimmed_prompt.encode("utf-8")) + 16,
    )

    llm = _FakeLLM({
        "resolved_block": "    return f'Hello {name}!'\n",
        "confidence": 0.95,
        "rationale": "HEAD intent preserved",
        "new_logic_detected": False,
    })
    deps = ma.MergerDeps(
        llm=llm,
        pusher=_FakePusher(),
        reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )

    caplog.set_level(logging.INFO, logger=ma.__name__)
    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.deferred_push_to_caller
    assert len(llm.calls) == 1
    assert "keep jira context" in llm.calls[0]
    assert "GIT_CONTEXT" not in llm.calls[0]
    assert "SIBLING_CONTEXT" not in llm.calls[0]
    assert "SYMBOL_CONTEXT" not in llm.calls[0]
    assert "merger_prompt_size_bytes=" in caplog.text
    assert "sections_trimmed=[git_log,sibling_files,symbol_table]" in caplog.text


def test_prompt_size_gate_abstains_after_full_trim(monkeypatch, caplog):
    req = _base_request()
    req.jira_ticket = "OP-1420"
    req.git_logs = {req.file_path: "GIT_CONTEXT " * 2000}
    req.sibling_file_contents = {"backend/sibling.py": "SIBLING_CONTEXT " * 2000}
    req.symbol_table = {"backend/greetings.py": "SYMBOL_CONTEXT " * 2000}

    blocks = ma.parse_conflict_block(req.conflict_text)
    risk = ma.classify_conflict_risk(req, blocks)
    trimmed_context = ma.build_context_pack(
        req,
        blocks,
        omit_sections=("git_log", "sibling_files", "symbol_table"),
    )
    trimmed_prompt = ma.build_prompt(
        req, blocks, risk, context_pack=trimmed_context,
    )
    monkeypatch.setattr(
        ma,
        "PROMPT_INPUT_HARD_LIMIT_BYTES",
        len(trimmed_prompt.encode("utf-8")) - 1,
    )

    llm = _FakeLLM({
        "resolved_block": "    return f'Hello {name}!'\n",
        "confidence": 0.95,
        "rationale": "HEAD intent preserved",
        "new_logic_detected": False,
    })
    deps = ma.MergerDeps(
        llm=llm,
        pusher=_FakePusher(),
        reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )

    caplog.set_level(logging.INFO, logger=ma.__name__)
    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.abstained_prompt_oversized
    assert len(llm.calls) == 0
    assert outcome.metadata["sections_trimmed"] == [
        "git_log", "sibling_files", "symbol_table",
    ]
    assert outcome.metadata["prompt_size_bytes"] > outcome.metadata[
        "prompt_limit_bytes"
    ]
    assert "merger_prompt_size_bytes=" in caplog.text
    assert "sections_trimmed=[git_log,sibling_files,symbol_table]" in caplog.text


def test_context_pack_marks_missing_sibling_file(tmp_path, caplog):
    req = _base_request(additional_files=["backend/missing.py"])
    req.workspace = str(tmp_path)
    req.jira_ticket = "OP-1414"

    caplog.set_level(logging.WARNING, logger=ma.__name__)
    pack = ma.build_context_pack(req, ma.parse_conflict_block(req.conflict_text))

    assert "## Sibling file: backend/missing.py" in pack
    assert "workspace read failed: missing_file" in pack
    assert "file not found" in pack
    assert "jira=OP-1414" in caplog.text
    assert "path=backend/missing.py" in caplog.text


def test_context_pack_marks_empty_sibling_file(tmp_path, caplog):
    path = tmp_path / "backend" / "empty.py"
    path.parent.mkdir()
    path.write_text("")
    req = _base_request(additional_files=["backend/empty.py"])
    req.workspace = str(tmp_path)
    req.jira_ticket = "OP-1414"

    caplog.set_level(logging.WARNING, logger=ma.__name__)
    pack = ma.build_context_pack(req, ma.parse_conflict_block(req.conflict_text))

    assert "## Sibling file: backend/empty.py" in pack
    assert "(file is empty)" in pack
    assert "workspace read failed" not in pack
    assert "backend/empty.py" not in caplog.text


def test_context_pack_marks_unsafe_sibling_path(tmp_path, caplog):
    req = _base_request(additional_files=["../outside.py"])
    req.workspace = str(tmp_path)
    req.jira_ticket = "OP-1414"

    caplog.set_level(logging.WARNING, logger=ma.__name__)
    pack = ma.build_context_pack(req, ma.parse_conflict_block(req.conflict_text))

    assert "## Sibling file: ../outside.py" in pack
    assert "workspace read failed: unsafe_path" in pack
    assert "outside workspace" in pack
    assert "jira=OP-1414" in caplog.text
    assert "path=../outside.py" in caplog.text


def test_git_region_log_timeout_returns_marker_and_logs_warning(
    tmp_path, monkeypatch, caplog,
):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".git").mkdir()
    blocks = [
        ma.ConflictBlock(
            start_line=10,
            end_line=14,
            head_label="HEAD",
            incoming_label="feature/greeting",
            head_lines=["    return f'Hello {name}!'"],
            incoming_lines=["    return f'Hi {name}!'"],
        )
    ]

    def slow_git_log(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git log", timeout=20)

    monkeypatch.setattr(ma.subprocess, "run", slow_git_log)
    caplog.set_level("WARNING", logger=ma.__name__)

    log = ma._git_region_log(str(ws), "backend/greetings.py", blocks)

    assert "git log timed out after 20s" in log
    assert "partial history may exist" in log
    assert "git region log timed out after 20s" in caplog.text
    assert "backend/greetings.py" in caplog.text


def test_op1403_synthetic_guild_shape_prompt_mentions_guild_and_guild_id():
    conflict = (
        "class GuildService:\n"
        "    def merge(self, guild):\n"
        "        guild_id = guild.id\n"
        "<<<<<<< HEAD\n"
        "        return self.by_guild(guild)\n"
        "=======\n"
        "        return self.by_id(guild_id)\n"
        ">>>>>>> feature/guild-id\n"
        "\n"
        "    def by_guild(self, guild):\n"
        "        return guild.name\n"
        "\n"
        "    def by_id(self, guild_id):\n"
        "        return guild_id\n"
    )
    llm = _FakeLLM({
        "resolved_block": (
            "        self.by_guild(guild)\n"
            "        return self.by_id(guild_id)\n"
        ),
        "confidence": 0.96,
        "rationale": "preserves guild object and guild_id lookup semantics",
        "new_logic_detected": False,
    })

    class _ExplodingPusher:
        async def push(self, **kwargs):
            raise AssertionError("pusher should be skipped")

    class _ExplodingReviewer:
        async def post_review(self, **kwargs):
            raise AssertionError("reviewer should be skipped")

    deps = ma.MergerDeps(
        llm=llm,
        pusher=_ExplodingPusher(),
        reviewer=_ExplodingReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    req = _base_request(file_path="backend/agents/llm.py", conflict=conflict)
    req.push_locally = False
    req.jira_ticket = "OP-883"
    req.jira_description = "Resolve guild versus guild_id naming collision."
    req.git_logs = {
        req.file_path: "commit cafe123\n    Introduce guild_id alongside guild\n"
    }

    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.deferred_push_to_caller
    prompt = llm.calls[0]
    assert "def by_guild(self, guild)" in prompt
    assert "def by_id(self, guild_id)" in prompt
    assert "Resolve guild versus guild_id naming collision" in prompt
    assert "Introduce guild_id alongside guild" in prompt


def test_op1404_synthetic_add_vs_add_micro_conflict_preserves_both_helpers():
    conflict = (
        "def run_pipeline(value):\n"
        "    return normalize(value)\n"
        "\n"
        "<<<<<<< HEAD\n"
        "def trim_value(value):\n"
        "    return value.strip()\n"
        "=======\n"
        "def coerce_value(value):\n"
        "    return str(value)\n"
        ">>>>>>> feature/coerce-value\n"
    )
    llm = _FakeLLM({
        "resolved_block": (
            "def trim_value(value):\n"
            "    return value.strip()\n"
            "\n"
            "def coerce_value(value):\n"
            "    return str(value)\n"
        ),
        "confidence": 0.97,
        "rationale": "ADD-vs-ADD distinct helper functions; take both.",
        "new_logic_detected": False,
    })

    class _ExplodingPusher:
        async def push(self, **kwargs):
            raise AssertionError("pusher should be skipped")

    deps = ma.MergerDeps(
        llm=llm,
        pusher=_ExplodingPusher(),
        reviewer=_FakeReviewer(),
        review_llm=_confirming_review_llm(),
        test_runner=_test_runner(True),
    )
    req = _base_request(
        file_path="backend/pipeline.py",
        conflict=conflict,
        change_id="Iop1404",
    )
    req.push_locally = False
    req.jira_ticket = "OP-1404"
    req.jira_description = (
        "Both sides added distinct helper functions; prefer take-both."
    )

    outcome = _run(ma.resolve_conflict(req, deps=deps))

    assert outcome.reason is ma.MergerReason.resolved_deterministic_merge
    assert outcome.resolved_text is not None
    assert "def trim_value(value):" in outcome.resolved_text
    assert "def coerce_value(value):" in outcome.resolved_text
    assert outcome.metadata["deterministic_patterns"] == ["add/add distinct symbol"]
    assert llm.calls == []


def test_deterministic_merge_adds_disjoint_methods_to_class():
    conflict = (
        "class TestBuildThenVerify:\n"
        "    timeout = 120\n"
        "\n"
        "    def existing(self):\n"
        "        return 'ok'\n"
        "\n"
        "<<<<<<< HEAD\n"
        "    def test_pytest_timeout_marks_slow(self):\n"
        "        return 'slow'\n"
        "=======\n"
        "    async def test_replay_verifies_without_push(self):\n"
        "        return 'green'\n"
        ">>>>>>> feature/replay-verify\n"
    )
    req = _base_request(
        file_path="backend/tests/test_merge_arbiter.py",
        conflict=conflict,
        change_id="Iop1433",
    )
    blocks = ma.parse_conflict_block(req.conflict_text)

    outcome = ma.try_deterministic_merge(req, blocks)

    assert outcome is not None
    assert outcome.reason is ma.MergerReason.resolved_deterministic_merge
    assert outcome.resolved_text is not None
    assert "def test_pytest_timeout_marks_slow" in outcome.resolved_text
    assert "async def test_replay_verifies_without_push" in outcome.resolved_text
    assert "<<<<<<<" not in outcome.resolved_text
    assert outcome.metadata["deterministic_patterns"] == ["add_method_to_class"]
    compile(outcome.resolved_text, "test_merge_arbiter.py", "exec")


def test_deterministic_merge_rejects_shared_method_body_edit_in_class():
    conflict = (
        "class TestBuildThenVerify:\n"
        "    def existing(self):\n"
        "        return 'ok'\n"
        "\n"
        "<<<<<<< HEAD\n"
        "    def test_replay(self):\n"
        "        return 'head'\n"
        "=======\n"
        "    def test_replay(self):\n"
        "        return 'incoming'\n"
        ">>>>>>> feature/replay-verify\n"
    )
    req = _base_request(
        file_path="backend/tests/test_merge_arbiter.py",
        conflict=conflict,
        change_id="Iop1433",
    )

    outcome = ma.try_deterministic_merge(
        req, ma.parse_conflict_block(req.conflict_text),
    )

    assert outcome is None


def test_deterministic_merge_rejects_class_attribute_edit():
    conflict = (
        "class TestBuildThenVerify:\n"
        "    def existing(self):\n"
        "        return 'ok'\n"
        "\n"
        "<<<<<<< HEAD\n"
        "    timeout = 120\n"
        "=======\n"
        "    def test_replay(self):\n"
        "        return 'green'\n"
        ">>>>>>> feature/replay-verify\n"
    )
    req = _base_request(
        file_path="backend/tests/test_merge_arbiter.py",
        conflict=conflict,
        change_id="Iop1433",
    )

    outcome = ma.try_deterministic_merge(
        req, ma.parse_conflict_block(req.conflict_text),
    )

    assert outcome is None


# ──────────────────────────────────────────────────────────────
#  Submit-rule simulator (emulates O7 Gerrit rule for tests)
# ──────────────────────────────────────────────────────────────


AI_BOT_GROUP = {
    "merger-agent-bot", "lint-bot", "security-bot", "ai-reviewer-x",
}


def _simulate_submit_rule(votes: list[tuple[str, int]]) -> str:
    """Local analogue of the O7 submit-rule.

    Rule: allow iff there is at least one Code-Review +2 from
    ``merger-agent-bot`` AND at least one Code-Review +2 from an actor
    NOT in the ``AI_BOT_GROUP``.  Otherwise reject with a stable code.
    """
    merger_plus2 = any(
        actor == "merger-agent-bot" and score == 2 for actor, score in votes
    )
    human_plus2 = any(
        actor not in AI_BOT_GROUP and score == 2 for actor, score in votes
    )
    if merger_plus2 and human_plus2:
        return "allow"
    if not merger_plus2 and human_plus2:
        return "reject_missing_merger"
    return "reject_missing_human"


# ──────────────────────────────────────────────────────────────
#  GitPatchsetPusher — real git workspace (no remote push)
# ──────────────────────────────────────────────────────────────


class TestGitPatchsetPusher:

    def _init_repo(self, tmp_path):
        import subprocess
        ws = tmp_path / "repo"
        ws.mkdir()
        def g(*args):
            return subprocess.run(
                ["git", *args], cwd=ws, capture_output=True, text=True,
            )
        g("init", "-q", "-b", "main")
        g("config", "user.name", "merger-test")
        g("config", "user.email", "merger-test@example.com")
        (ws / "hello.py").write_text("x = 1\n")
        g("add", ".")
        g("commit", "-q", "-m", "baseline")
        return ws

    def test_push_fails_gracefully_without_remote(self, tmp_path):
        ws = self._init_repo(tmp_path)
        pusher = ma.GitPatchsetPusher()
        res = _run(pusher.push(
            change_id="Itest", project="omnisight",
            workspace=str(ws), file_path="hello.py",
            resolved_text="x = 42  # merger\n",
            commit_message="merger resolution",
        ))
        # No remote configured → git push exits non-zero, pusher reports failure.
        assert res.ok is False
        assert "push" in res.reason.lower() or "remote" in res.reason.lower()
        # But the file WAS written + committed locally.
        assert (ws / "hello.py").read_text() == "x = 42  # merger\n"

    def test_push_refuses_without_workspace(self):
        pusher = ma.GitPatchsetPusher()
        res = _run(pusher.push(
            change_id="Itest", project="omnisight",
            workspace=None, file_path="hello.py",
            resolved_text="x = 42\n", commit_message="m",
        ))
        assert res.ok is False
        assert "workspace" in res.reason.lower()

    def test_push_amend_uses_reset_author(self, tmp_path):
        """OP-1196 phase 2 regression — the amend step MUST adopt the
        workspace's ambient git identity, not preserve the original
        commit's author. Without --reset-author, Gerrit rejects the
        push with `not registered in your account, and you lack
        'forge author' permission` because merger-agent-bot lacks
        forgeAuthor (per O10 mirror, kept active by OP-1196 phase 1α).

        Simulate the scenario: workspace has a HEAD commit authored
        by SOMEONE-ELSE; configure user.email to merger-bot; run the
        pusher; assert the amended HEAD's author becomes merger-bot.
        """
        import subprocess
        ws = tmp_path / "repo"
        ws.mkdir()

        def g(*args, env=None):
            return subprocess.run(
                ["git", *args], cwd=ws, capture_output=True, text=True, env=env,
            )

        g("init", "-q", "-b", "main")
        # Set workspace's user (= merger-bot identity, the AFTER state).
        g("config", "user.name", "merger-agent-bot")
        g("config", "user.email", "rt3628+merger-bot@gmail.com")
        # Seed the HEAD with a commit by SOMEONE ELSE — this is the
        # `Author:` we expect --reset-author to overwrite.
        original_env = {
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "codex-bot",
            "GIT_AUTHOR_EMAIL": "rt3628+codex-bot@gmail.com",
            "GIT_COMMITTER_NAME": "codex-bot",
            "GIT_COMMITTER_EMAIL": "rt3628+codex-bot@gmail.com",
        }
        (ws / "hello.py").write_text("x = 1\n")
        g("add", ".", env=original_env)
        g("commit", "-q", "-m", "baseline by codex-bot", env=original_env)

        # Confirm pre-condition: HEAD's author IS codex-bot.
        pre = g("log", "-1", "--format=%an <%ae>").stdout.strip()
        assert "codex-bot" in pre, f"setup broken: pre-amend author = {pre!r}"

        # Now run the pusher. It will write resolved_text + add + amend.
        # We DON'T configure a remote — the push step will fail. That's
        # fine; we only care that the AMEND step ran correctly. Inspect
        # HEAD's author after the run.
        pusher = ma.GitPatchsetPusher()
        _run(pusher.push(
            change_id="Itest1196phase2", project="omnisight",
            workspace=str(ws), file_path="hello.py",
            resolved_text="x = 42  # merger resolution\n",
            commit_message="merger resolution",
        ))

        # Post-condition: HEAD's author is now merger-agent-bot.
        post = g("log", "-1", "--format=%an <%ae>").stdout.strip()
        assert "merger-agent-bot" in post, (
            f"--reset-author regression: HEAD's author after amend is "
            f"{post!r}, expected merger-agent-bot. The push step would "
            f"have been rejected by Gerrit's forgeAuthor block."
        )
        # And the file content was applied.
        assert (ws / "hello.py").read_text() == "x = 42  # merger resolution\n"
        # And the Merger-Change-Id trailer was added.
        body = g("log", "-1", "--format=%B").stdout
        assert "Merger-Change-Id: Itest1196phase2" in body


# ──────────────────────────────────────────────────────────────
#  Metric counters fire on expected paths
# ──────────────────────────────────────────────────────────────


class TestMetrics:

    def test_plus_two_increments_counter(self):
        from backend import metrics as m
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        before = _read_counter(m.merger_plus_two_total)
        deps = ma.MergerDeps(
            llm=_FakeLLM({
                "resolved_block": "ok\n", "confidence": 0.95,
                "rationale": "", "new_logic_detected": False,
            }),
            pusher=_FakePusher(), reviewer=_FakeReviewer(),
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True),
        )
        _run(ma.resolve_conflict(_base_request(), deps=deps))
        after = _read_counter(m.merger_plus_two_total)
        assert after == before + 1

    def test_security_refusal_increments(self):
        from backend import metrics as m
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        before = _read_counter(m.merger_security_refusal_total)
        deps = ma.MergerDeps(
            llm=_FakeLLM({"resolved_block": "", "confidence": 0,
                          "rationale": "", "new_logic_detected": False}),
            pusher=_FakePusher(), reviewer=_FakeReviewer(),
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True),
        )
        _run(ma.resolve_conflict(
            _base_request(file_path=".github/workflows/ci.yml"),
            deps=deps,
        ))
        after = _read_counter(m.merger_security_refusal_total)
        assert after == before + 1


def _read_counter(counter) -> float:
    try:
        samples = list(counter.collect())
        total = 0.0
        for s in samples:
            for ss in s.samples:
                if ss.name.endswith("_total"):
                    total += ss.value
        return total
    except Exception:
        return 0.0


class TestSubmitRuleSimulator:

    def test_rule_truth_table(self):
        assert _simulate_submit_rule(
            [("merger-agent-bot", 2), ("human-a", 2)]
        ) == "allow"
        assert _simulate_submit_rule(
            [("merger-agent-bot", 2)]
        ) == "reject_missing_human"
        assert _simulate_submit_rule(
            [("human-a", 2)]
        ) == "reject_missing_merger"
        assert _simulate_submit_rule(
            [("merger-agent-bot", 2), ("human-a", -1)]
        ) == "reject_missing_human"
        # N AI +2 + 0 human  →  reject
        assert _simulate_submit_rule(
            [("merger-agent-bot", 2), ("lint-bot", 2),
             ("security-bot", 2)]
        ) == "reject_missing_human"
        # N AI +2 + human +2 →  allow
        assert _simulate_submit_rule(
            [("merger-agent-bot", 2), ("lint-bot", 2),
             ("security-bot", 2), ("human-a", 2)]
        ) == "allow"


# ──────────────────────────────────────────────────────────────
#  OP-694 — Conflict-resolved hashtag is set on success
# ──────────────────────────────────────────────────────────────


class TestOp694HashtagOnSuccess:
    """The Gerrit Merger-Plus-2 submit-requirement is gated on the
    `Merge-Conflict-Resolved` hashtag (`applicableIf = ...`). The
    merger must set this hashtag after a successful resolution +
    +2 vote — otherwise the +2 it cast counts toward nothing
    (Merger-Plus-2 stays NOT_APPLICABLE)."""

    @staticmethod
    def _good_llm():
        return _FakeLLM({
            "resolved_block": "    return f'Hello {name}!'\n",
            "confidence": 0.95,
            "rationale": "HEAD intent preserved",
            "new_logic_detected": False,
        })

    def test_success_sets_conflict_resolved_hashtag(self):
        """Happy path: +2 voted → hashtag setter called with the canonical name."""
        hashtag_setter = _FakeHashtagSetter(ok=True)
        deps = ma.MergerDeps(
            llm=self._good_llm(),
            pusher=_FakePusher(),
            reviewer=_FakeReviewer(),
            hashtag_setter=hashtag_setter,
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True),
        )
        outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

        assert outcome.reason is ma.MergerReason.plus_two_voted
        assert len(hashtag_setter.calls) == 1
        call = hashtag_setter.calls[0]
        assert call["hashtag"] == ma.CONFLICT_RESOLVED_HASHTAG
        assert call["hashtag"] == "Merge-Conflict-Resolved"
        assert call["change_id"]
        assert call["project"]
        assert outcome.metadata.get("hashtag_set_ok") is True

    def test_hashtag_failure_does_not_block_plus_two(self):
        """Hashtag-set returns ok=False: the +2 vote remains; outcome
        records the failure for ops to spot. Conservative fall-through:
        change behaves as a non-conflict change (Merger-Plus-2
        NOT_APPLICABLE) so it can still merge on Human +2 alone."""
        hashtag_setter = _FakeHashtagSetter(ok=False, reason="403 Forbidden")
        deps = ma.MergerDeps(
            llm=self._good_llm(),
            pusher=_FakePusher(),
            reviewer=_FakeReviewer(),
            hashtag_setter=hashtag_setter,
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True),
        )
        outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

        assert outcome.reason is ma.MergerReason.plus_two_voted
        assert int(outcome.voted_score) == 2
        assert outcome.metadata.get("hashtag_set_ok") is False
        assert "403" in outcome.metadata.get("hashtag_set_reason", "")

    def test_hashtag_setter_exception_handled_gracefully(self):
        """A raised exception in the hashtag setter must NOT propagate
        and undo the merger's success path — the +2 has already landed."""
        hashtag_setter = _FakeHashtagSetter(
            raises=RuntimeError("network down"),
        )
        deps = ma.MergerDeps(
            llm=self._good_llm(),
            pusher=_FakePusher(),
            reviewer=_FakeReviewer(),
            hashtag_setter=hashtag_setter,
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True),
        )
        outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))

        assert outcome.reason is ma.MergerReason.plus_two_voted
        assert outcome.metadata.get("hashtag_set_ok") is False
        assert "network down" in outcome.metadata.get("hashtag_set_reason", "")

    def test_default_hashtag_setter_degrades_on_old_client(self):
        """Production safety: GerritClientHashtagSetter probes for
        client.add_hashtag and reports unsupported cleanly so a stale
        gerrit_client doesn't crash the merger path."""
        class _NoAddHashtag:
            pass

        setter = ma.GerritClientHashtagSetter(client=_NoAddHashtag())
        result = _run(setter.add_hashtag(
            change_id="Itest", project="omnisight", hashtag="x",
        ))
        assert result.ok is False
        assert "add_hashtag" in result.reason


# ──────────────────────────────────────────────────────────────
#  OP-1196 phase 3 — push_locally=False deferred-push path
# ──────────────────────────────────────────────────────────────


class TestPushLocallyDeferredToCaller:
    """When the caller (gerrit-jira-bridge daemon) sets
    ``ConflictRequest.push_locally=False``, the merger pipeline must:

      * Run through every gate + the LLM call as normal.
      * Populate ``ResolutionOutcome.resolved_text`` with the
        LLM-produced file content.
      * SKIP the in-process pusher step.
      * SKIP the in-process reviewer step.
      * Return ``MergerReason.deferred_push_to_caller`` so the caller
        knows to do the push + +2 vote itself.

    Backwards-compat: a request with default ``push_locally=True``
    still runs through the pusher (existing tests assert this; this
    test class only asserts the new opt-in path).
    """

    def test_deferred_push_skips_pusher_returns_resolved_text(self):
        llm = _FakeLLM({
            "resolved_block": "x = 42  # merger\n",
            "confidence": 0.97,
            "rationale": "picked HEAD; INCOMING was stale",
            "new_logic_detected": False,
        })

        # Pusher MUST NOT be called. Use a stub that raises if invoked
        # so we get a clear failure rather than a silent regression.
        class _ExplodingPusher:
            async def push(self, **kwargs):
                raise AssertionError(
                    "OP-1196 phase 3 regression: pusher invoked despite "
                    "push_locally=False"
                )

        # Reviewer MUST NOT be called either.
        class _ExplodingReviewer:
            async def post_review(self, **kwargs):
                raise AssertionError(
                    "OP-1196 phase 3 regression: reviewer invoked "
                    "despite push_locally=False"
                )

        events, audit = _audit_sink()
        deps = ma.MergerDeps(
            llm=llm, pusher=_ExplodingPusher(), reviewer=_ExplodingReviewer(),
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True), audit=audit,
        )

        req = _base_request()
        req.push_locally = False

        outcome = _run(ma.resolve_conflict(req, deps=deps))

        assert outcome.reason is ma.MergerReason.deferred_push_to_caller
        assert int(outcome.voted_score) == 0  # not voted yet
        assert outcome.confidence == 0.97
        # Critical: resolved_text MUST be populated so the caller can
        # apply it. The text is the FULL FILE with the conflict block
        # replaced by the LLM's resolved_block — not just the
        # resolved_block alone (the in-process pusher writes the FULL
        # FILE to the workspace too; the daemon-side push will do the
        # same).
        assert outcome.resolved_text, (
            "resolved_text must be populated for deferred-push path"
        )
        assert "x = 42  # merger" in outcome.resolved_text
        # Conflict markers must be GONE from the resolved file (that's
        # the whole point — the LLM replaced the conflict region).
        assert "<<<<<<<" not in outcome.resolved_text
        assert ">>>>>>>" not in outcome.resolved_text
        # diff_preview also surfaces for human-readable display.
        assert outcome.diff_preview  # non-empty

    def test_push_locally_true_still_calls_pusher(self):
        """Backwards-compat: the default path is unchanged."""
        llm = _FakeLLM({
            "resolved_block": "ok\n", "confidence": 0.95,
            "rationale": "", "new_logic_detected": False,
        })
        pusher = _FakePusher()
        reviewer = _FakeReviewer()
        deps = ma.MergerDeps(
            llm=llm, pusher=pusher, reviewer=reviewer,
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True),
        )
        req = _base_request()  # push_locally default True

        outcome = _run(ma.resolve_conflict(req, deps=deps))

        assert outcome.reason is ma.MergerReason.plus_two_voted
        assert len(pusher.calls) == 1     # pusher WAS called
        assert outcome.push_sha == pusher.sha

    def test_deferred_push_includes_resolved_text_in_to_dict(self):
        """The JSON-serialised outcome (sent back to the daemon over
        HTTP) must include resolved_text — otherwise the daemon has
        nothing to push."""
        llm = _FakeLLM({
            "resolved_block": "FINAL CONTENT\n", "confidence": 0.96,
            "rationale": "", "new_logic_detected": False,
        })

        class _ExplodingPusher:
            async def push(self, **kwargs):
                raise AssertionError("pusher should be skipped")

        class _ExplodingReviewer:
            async def post_review(self, **kwargs):
                raise AssertionError("reviewer should be skipped")

        deps = ma.MergerDeps(
            llm=llm, pusher=_ExplodingPusher(),
            reviewer=_ExplodingReviewer(),
            review_llm=_confirming_review_llm(),
            test_runner=_test_runner(True),
        )
        req = _base_request()
        req.push_locally = False

        outcome = _run(ma.resolve_conflict(req, deps=deps))
        d = outcome.to_dict()
        assert d["reason"] == "deferred_push_to_caller"
        # resolved_text contains the resolved_block embedded in the
        # surrounding file body (the merger overwrites the WHOLE file
        # on the workspace, not just the conflict region).
        assert "FINAL CONTENT" in d["resolved_text"]
        assert "<<<<<<<" not in d["resolved_text"]
        assert d["voted_score"] == 0
        assert d["push_sha"] == ""    # no push was attempted
