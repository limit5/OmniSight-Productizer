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
from dataclasses import dataclass, field
from typing import Any

import pytest

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


def test_multi_file_abstain():
    llm = _FakeLLM({
        "resolved_block": "ok\n", "confidence": 0.99,
        "rationale": "trivial", "new_logic_detected": False,
    })
    deps = ma.MergerDeps(
        llm=llm, pusher=_FakePusher(), reviewer=_FakeReviewer(),
        test_runner=_test_runner(True),
    )
    req = _base_request(additional_files=["backend/utils.py"])
    outcome = _run(ma.resolve_conflict(req, deps=deps))
    assert outcome.reason is ma.MergerReason.abstained_multi_file
    assert llm.calls == []


def test_oversized_conflict_abstain():
    deps = ma.MergerDeps(
        llm=_FakeLLM({"resolved_block": "x", "confidence": 0.99,
                      "rationale": "", "new_logic_detected": False}),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
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
        test_runner=_test_runner(True),
    )
    req = _base_request(conflict="no markers here\n")
    outcome = _run(ma.resolve_conflict(req, deps=deps))
    assert outcome.reason is ma.MergerReason.refused_no_conflict


def test_push_fail_no_vote_and_escalates():
    llm = _FakeLLM({
        "resolved_block": "ok\n", "confidence": 0.95,
        "rationale": "trivial", "new_logic_detected": False,
    })
    pusher = _FakePusher(ok=False, reason="SSH key rejected")
    reviewer = _FakeReviewer()
    deps = ma.MergerDeps(
        llm=llm, pusher=pusher, reviewer=reviewer,
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
        test_runner=_test_runner(True),
    )
    outcome = _run(ma.resolve_conflict(_base_request(), deps=deps))
    assert outcome.reason is ma.MergerReason.refused_llm_unavailable
    assert ma.get_failure_count("Iabc123") == 1


def test_llm_invalid_json_abstain_and_counts():
    deps = ma.MergerDeps(
        llm=_FakeLLM("this is not JSON"),
        pusher=_FakePusher(), reviewer=_FakeReviewer(),
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
    assert "HEAD" in prompt
    assert "feature/greeting" in prompt
    assert "backend/greetings.py" in prompt


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
