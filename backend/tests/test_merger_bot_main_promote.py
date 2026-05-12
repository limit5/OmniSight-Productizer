"""OP-962 (AUDIT-13b) — Gerrit conditional submit-requirement for the
develop→main auto-promote fast-forward change (ADR-0016 path C).

These tests pin the *policy contract* encoded in
``.gerrit/project.config``:

  * a new ``MainFastForwardMergerPlus2`` submit-requirement that lets a
    ``merger-agent-bot`` (path C, zero operator click) **or** a
    ``non-ai-reviewer`` (path A, operator fallback) Code-Review +2
    satisfy the gate for the develop→main auto-promote change, and
  * a narrowed ``Human-Plus-2`` requirement that carves that single
    change out of the human hard gate — and **only** that change.

The most dangerous failure mode (``SubmitRuleOverScopes`` in the OP-962
error catalog) is the carve-out leaking to arbitrary ``refs/for/main``
changes.  ``test_regression_arbitrary_main_change_still_needs_human`` and
friends are the mandatory negative checks against it.

Like ``test_gerrit_verified_gate.py`` these tests stay static: there is
no live Gerrit in CI, so they parse the declarative config and exercise a
small evaluator for the (deliberately tiny) subset of the Gerrit
submit-requirement expression language that this project's config uses.
The merger-bot's *decision to cast* the +2 (author == auto-promote-bot?
fast-forward? hashtag present?) lives in the merger-bot poller
(``area:backend`` — out of scope for this ``area:devops``/``area:tests``
ticket); here we only pin what the Gerrit-side rule does once a vote
exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_CONFIG = _REPO_ROOT / ".gerrit" / "project.config"
_PROJECT_CONFIG_EXAMPLE = _REPO_ROOT / ".gerrit" / "project.config.example"

# The hashtag the develop→main auto-promote push attaches
# (backend/agents/auto_promote_main.PROMOTE_HASHTAGS) — the path-C
# forward-compat hook per ADR-0016.
AUTO_PROMOTE_HASHTAG = "milestone:R3-fastforward"

_MERGER_DESCRIPTION = (
    "auto-promote-bot or merger-bot +2 allowed only for "
    "milestone:R3-fastforward auto-promote changes on main"
)


# ──────────────────────────────────────────────────────────────────────
#  Minimal model of a Gerrit change + a Code-Review vote
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Vote:
    groups: frozenset[str]
    score: int


def merger_plus2() -> Vote:
    # merger-agent-bot is also a member of the ai-reviewer-bots umbrella.
    return Vote(groups=frozenset({"merger-agent-bot", "ai-reviewer-bots"}), score=2)


def human_plus2() -> Vote:
    return Vote(groups=frozenset({"non-ai-reviewer"}), score=2)


def lint_bot_plus2() -> Vote:
    # A generic AI reviewer that is NOT the merger.
    return Vote(groups=frozenset({"ai-reviewer-bots"}), score=2)


def minus1(group: str = "non-ai-reviewer") -> Vote:
    return Vote(groups=frozenset({group}), score=-1)


@dataclass
class Change:
    branch: str
    hashtags: frozenset[str] = field(default_factory=frozenset)
    votes: tuple[Vote, ...] = ()

    def has_vote(self, *, group: str, min_score: int) -> bool:
        return any(group in v.groups and v.score >= min_score for v in self.votes)

    def has_score(self, score: int) -> bool:
        return any(v.score == score for v in self.votes)


# ──────────────────────────────────────────────────────────────────────
#  Tiny evaluator for the subset of the Gerrit query language used by
#  .gerrit/project.config submit-requirements:
#     PRED            hashtag:X | hashtag:"X" | branch:X
#                     label:Code-Review=+2,group=G | label:Code-Review=-1
#                     label:Code-Review=-2 | label:Verified=+1
#                     is:true | is:false
#     -PRED           negation (the leading '-' on a token)
#     E AND E         conjunction
#     E OR E          disjunction
#     ( E )           grouping
#  AND binds tighter than OR; '-' binds tightest.  This mirrors Gerrit's
#  precedence for these operators.
# ──────────────────────────────────────────────────────────────────────


def _tokenize(expr: str) -> list[str]:
    # Put whitespace around parens so a plain split() separates them; no
    # quoted value in this config contains whitespace, so split() is safe.
    return expr.replace("(", " ( ").replace(")", " ) ").split()


def _eval_predicate(pred: str, change: Change) -> bool:
    if pred == "is:true":
        return True
    if pred == "is:false":
        return False
    op, _, raw = pred.partition(":")
    if op == "hashtag":
        value = raw.strip('"')
        return value in change.hashtags
    if op == "branch":
        return change.branch == raw.strip('"')
    if op == "label":
        # raw looks like 'Code-Review=+2,group=merger-agent-bot' or
        # 'Code-Review=-1' or 'Verified=+1'
        label_part, _, group_part = raw.partition(",")
        name, _, score_str = label_part.partition("=")
        if name != "Code-Review":
            # Verified (or any other label) — no such votes in this model.
            return False
        score = int(score_str)
        if group_part:
            assert group_part.startswith("group=")
            group = group_part[len("group="):]
            return change.has_vote(group=group, min_score=score)
        return change.has_score(score)
    raise AssertionError(f"unsupported predicate in test evaluator: {pred!r}")


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self._toks = tokens
        self._i = 0

    def _peek(self) -> str | None:
        return self._toks[self._i] if self._i < len(self._toks) else None

    def _next(self) -> str:
        tok = self._toks[self._i]
        self._i += 1
        return tok

    def parse(self, change: Change) -> bool:
        result = self._or(change)
        assert self._peek() is None, f"trailing tokens: {self._toks[self._i:]}"
        return result

    def _or(self, change: Change) -> bool:
        value = self._and(change)
        while self._peek() == "OR":
            self._next()
            value = self._and(change) or value
        return value

    def _and(self, change: Change) -> bool:
        value = self._primary(change)
        while self._peek() == "AND":
            self._next()
            value = self._primary(change) and value
        return value

    def _primary(self, change: Change) -> bool:
        tok = self._next()
        if tok == "(":
            value = self._or(change)
            assert self._next() == ")", "unbalanced parenthesis"
            return value
        if tok.startswith("-"):
            return not _eval_predicate(tok[1:], change)
        return _eval_predicate(tok, change)


def _eval_expr(expr: str | None, change: Change, *, default: bool) -> bool:
    if expr is None:
        return default
    return _Parser(_tokenize(expr)).parse(change)


# ──────────────────────────────────────────────────────────────────────
#  Parse .gerrit/project.config submit-requirements
# ──────────────────────────────────────────────────────────────────────


_SR_HEADER = re.compile(r'^\[submit-requirement "(?P<name>[^"]+)"\]$')


def _parse_submit_requirements(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m = _SR_HEADER.match(stripped)
        if m:
            current = {}
            out[m.group("name")] = current
            continue
        if stripped.startswith("["):
            current = None
            continue
        if current is None or not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            current[key.strip()] = value.strip()
    return out


@pytest.fixture(scope="module")
def submit_requirements() -> dict[str, dict[str, str]]:
    return _parse_submit_requirements(_PROJECT_CONFIG.read_text(encoding="utf-8"))


def _is_submittable(change: Change, srs: dict[str, dict[str, str]]) -> bool:
    """A change is submittable iff every *applicable* submit-requirement
    is satisfied (mirrors Gerrit's behaviour: NOT_APPLICABLE rules don't
    block; APPLICABLE ones must be SATISFIED)."""
    for sr in srs.values():
        applicable = _eval_expr(sr.get("applicableIf"), change, default=True)
        if not applicable:
            continue
        if not _eval_expr(sr.get("submittableIf"), change, default=True):
            return False
    return True


# ──────────────────────────────────────────────────────────────────────
#  AC #1 — the MainFastForwardMergerPlus2 submit-requirement is present
#  and well-formed
# ──────────────────────────────────────────────────────────────────────


def test_main_fastforward_submit_requirement_present_and_wellformed(submit_requirements):
    assert "MainFastForwardMergerPlus2" in submit_requirements, (
        "OP-962: .gerrit/project.config must define the "
        "MainFastForwardMergerPlus2 submit-requirement"
    )
    sr = submit_requirements["MainFastForwardMergerPlus2"]

    # description verbatim per AC #1.
    assert sr["description"] == _MERGER_DESCRIPTION

    # applicableIf is doubly-keyed: hashtag AND branch:main.  Compare with
    # quoting stripped so the contract is robust to "..."-vs-bare tuning.
    applicable = sr["applicableIf"].replace('"', "")
    assert applicable == f"hashtag:{AUTO_PROMOTE_HASHTAG} AND branch:main", applicable

    # submittableIf includes a clause counting merger-bot +2 toward the
    # submit gate (analogous to O6 OP-269), and keeps an operator (path A)
    # +2 as the fallback.
    submittable = sr["submittableIf"]
    assert "label:Code-Review=+2,group=merger-agent-bot" in submittable
    assert "label:Code-Review=+2,group=non-ai-reviewer" in submittable
    assert " OR " in submittable

    assert sr.get("canOverrideInChildProjects") == "false"


def test_human_plus_two_is_carved_out_only_for_autopromote(submit_requirements):
    """AC #1 mechanism: Human-Plus-2 stays unconditional everywhere
    except the doubly-keyed develop→main auto-promote change."""
    sr = submit_requirements["Human-Plus-2"]
    applicable = sr["applicableIf"].replace('"', "")
    # NOT (hashtag AND branch:main)  ⇔  (-hashtag) OR (-branch:main)
    assert applicable == f"-hashtag:{AUTO_PROMOTE_HASHTAG} OR -branch:main", applicable
    # The actual approval clause is untouched — still a non-ai-reviewer +2.
    assert sr["submittableIf"] == "label:Code-Review=+2,group=non-ai-reviewer"


# ──────────────────────────────────────────────────────────────────────
#  Test plan (OP-962) — exercised through the parsed config
# ──────────────────────────────────────────────────────────────────────


def test_happy_path_merger_bot_plus_two_submits_autopromote(submit_requirements):
    """Test plan #1 — auto-promote change (hashtag + branch:main) with a
    merger-agent-bot +2 → submittable, no human click."""
    change = Change(
        branch="main",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
        votes=(merger_plus2(),),
    )
    assert _is_submittable(change, submit_requirements) is True


def test_path_a_operator_plus_two_still_submits_autopromote(submit_requirements):
    """ADR-0016 path A stays available: an operator (non-ai-reviewer) +2
    on the auto-promote change is also sufficient — removing the rule is
    not the only way back to manual submit."""
    change = Change(
        branch="main",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
        votes=(human_plus2(),),
    )
    assert _is_submittable(change, submit_requirements) is True


def test_regression_arbitrary_main_change_still_needs_human(submit_requirements):
    """Test plan #2 / AC #4 — THE CRITICAL `SubmitRuleOverScopes` guard.

    A change pushed to refs/for/main WITHOUT the milestone:R3-fastforward
    hashtag must still require a human +2 — a merger-bot (or any pile of
    AI) +2 must NOT submit it."""
    only_ai = Change(
        branch="main",
        hashtags=frozenset(),
        votes=(merger_plus2(), lint_bot_plus2()),
    )
    assert _is_submittable(only_ai, submit_requirements) is False

    # …and it becomes submittable only once a human signs off.
    with_human = Change(
        branch="main",
        hashtags=frozenset(),
        votes=(merger_plus2(), human_plus2()),
    )
    assert _is_submittable(with_human, submit_requirements) is True


def test_wrong_hashtag_on_main_still_needs_human(submit_requirements):
    """Test plan #3 — a change carrying a *different* milestone:* hashtag
    does not match the carve-out; the human gate still applies."""
    change = Change(
        branch="main",
        hashtags=frozenset({"milestone:something-else"}),
        votes=(merger_plus2(),),
    )
    assert _is_submittable(change, submit_requirements) is False
    assert _is_submittable(
        Change(
            branch="main",
            hashtags=frozenset({"milestone:something-else"}),
            votes=(merger_plus2(), human_plus2()),
        ),
        submit_requirements,
    ) is True


def test_non_merger_ai_plus_two_does_not_submit_autopromote(submit_requirements):
    """Test plan #4 (config half) — even on a correctly-hashtagged main
    change, the carve-out only honours a *merger-agent-bot* (or human)
    +2.  A generic ai-reviewer-bot +2 (e.g. lint-bot) does not submit
    it.  The author-identity check (auto-promote-bot vs. anyone else)
    lives in the merger-bot poller, which is what actually decides
    whether to cast the merger +2 in the first place — out of scope for
    this devops/tests ticket."""
    change = Change(
        branch="main",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
        votes=(lint_bot_plus2(),),
    )
    assert _is_submittable(change, submit_requirements) is False


def test_negative_vote_blocks_even_autopromote(submit_requirements):
    """Test plan #5 (config half) — No-Veto is unconditional, so a -1
    (which the merger-bot itself casts when its pre-vote validation
    fails — non-fast-forward, or author ≠ auto-promote-bot — and which
    an operator can always cast) blocks submission even with a merger
    +2 and the hashtag present.  Fast-forward-ness is enforced upstream
    (auto_promote_main never creates a non-FF change); this is the
    in-config backstop."""
    change = Change(
        branch="main",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
        votes=(merger_plus2(), minus1()),
    )
    assert _is_submittable(change, submit_requirements) is False


def test_carveout_does_not_leak_to_develop(submit_requirements):
    """Sanity: the carve-out is keyed on branch:main, so changes on
    develop (even the absurd hypothetical of one carrying the
    milestone:R3-fastforward hashtag) still need a human +2."""
    only_merger = Change(
        branch="develop",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
        votes=(merger_plus2(),),
    )
    assert _is_submittable(only_merger, submit_requirements) is False
    assert _is_submittable(
        Change(
            branch="develop",
            hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
            votes=(merger_plus2(), human_plus2()),
        ),
        submit_requirements,
    ) is True


# ──────────────────────────────────────────────────────────────────────
#  AC #2 — merger-bot's Gerrit account can cast Code-Review +2 on main
# ──────────────────────────────────────────────────────────────────────


def test_ai_reviewer_bots_may_vote_code_review_on_all_branches_incl_main():
    """AC #2 — merger-agent-bot (a member of ai-reviewer-bots) must be
    able to cast Code-Review +2 on refs/heads/main.  This project's ACL
    is branch-agnostic — the ``[access "refs/heads/*"]`` block grants the
    -2..+2 Code-Review range to ai-reviewer-bots, which covers main — so
    no per-branch ``refs/for/main`` grant is needed (and the file says
    so, referencing OP-962)."""
    text = _PROJECT_CONFIG.read_text(encoding="utf-8")
    # The grant lives in a `refs/heads/*` access section.
    assert re.search(
        r'\[access "refs/heads/\*"\][^\[]*?'
        r'label-Code-Review = -2\.\.\+2 group ai-reviewer-bots',
        text,
        re.DOTALL,
    ), "ai-reviewer-bots must keep the -2..+2 Code-Review grant on refs/heads/*"
    # Nothing narrows it back below +2 for main, and the rationale is
    # recorded against this ticket.
    assert "OP-962" in text


# ──────────────────────────────────────────────────────────────────────
#  Keep .example in sync with the live config (mirrors the OP-740 check
#  in test_gerrit_verified_gate.py)
# ──────────────────────────────────────────────────────────────────────


def test_project_config_example_keeps_op962_block_in_sync():
    example_srs = _parse_submit_requirements(
        _PROJECT_CONFIG_EXAMPLE.read_text(encoding="utf-8")
    )
    live_srs = _parse_submit_requirements(_PROJECT_CONFIG.read_text(encoding="utf-8"))
    for name in ("Human-Plus-2", "MainFastForwardMergerPlus2"):
        assert name in example_srs, f"{name} missing from project.config.example"
        assert example_srs[name] == live_srs[name], (
            f"{name} drifted between project.config and project.config.example"
        )
