"""OP-962 (AUDIT-13b) + OP-1534 — Gerrit conditional submit-requirements
for the release-cut promotion to main (ADR-0016 path C / ADR-0020).

These tests pin the *policy contract* encoded in
``.gerrit/project.config``:

  * a ``MainFastForwardMergerPlus2`` submit-requirement that lets a
    ``merger-agent-bot`` **or** a ``non-ai-reviewer`` Code-Review +2
    satisfy the gate, and
  * a narrowed ``Human-Plus-2`` requirement that carves the release-cut
    change shape out of the human hard gate — and **only** that shape, and
  * the co-exist ``release-cut-promote`` requirement (merger +2 AND human
    +2) that re-imposes the dual sign-off for a genuine cut.

OP-1534 re-keys all three onto a SINGLE consistent four-key release-cut
signature so the carve-out cannot over-scope::

    branch:main
    AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward)
    AND intopic:release-cut          # substring — topic: is exact-match
    AND owner:sora

The most dangerous failure mode (``SubmitRuleOverScopes`` in the OP-962
error catalog) is the carve-out leaking to arbitrary ``refs/for/main``
changes — e.g. a branch:main change that has the hashtag but lacks the
release-cut topic/owner.  ``test_op1534_bare_hashtag_on_main_no_longer_
carved_out``, ``test_regression_arbitrary_main_change_still_needs_human``
and the quad-key knockouts are the mandatory negative checks against it.

Like ``test_gerrit_verified_gate.py`` these tests stay static: there is
no live Gerrit in CI, so they parse the declarative config and exercise a
small evaluator for the (deliberately tiny) subset of the Gerrit
submit-requirement expression language that this project's config uses.
The merger-bot's *decision to cast* the +2 (owner == sora? fast-forward?
hashtag present?) lives in the merger-bot poller (``area:backend`` — out
of scope for this ``area:devops``/``area:tests`` ticket); here we only pin
what the Gerrit-side rule does once a vote exists.
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

# OP-1536 / rcsr C3a — the corrected ``applicableIf`` for the three keyed
# submit-requirements, pinned verbatim.  These tests DEFINE the desired
# config: C3a satisfies these strings statically (a file parse), so the
# suite is green ahead of — and is NOT blocked by — the C3b
# refs/meta/config deploy.  Notes on the exact form (per ADR-0020):
#   * The original topic:^release-v...$ regex + author:^...bot$ keys were
#     dropped (2026-05-20, OP-1531): the Gerrit lexer rejects `[`, real
#     release-cut topics are vX.Y.Z-release-cut (matched by intopic:, since
#     topic: is exact-match), and real cuts are owner:sora (O10 lockdown
#     forces sora to push; bots can't pushMerge). Corrected 4-key =
#     branch:main + (canonical hashtag OR transition form) + intopic:
#     release-cut + owner:sora. See project_release_cut_sr_gating memory.
#   * Human-Plus-2's carve-out negates the same four keys (De Morgan), so a
#     real cut is NOT_APPLICABLE to the generic human gate and is governed by
#     release-cut-promote's co-exist (merger +2 AND human +2) instead.
C3A_APPLICABLE_IF: dict[str, str] = {
    "Human-Plus-2": (
        '-branch:main OR (-hashtag:"milestone:R3-fastforward" AND '
        '-hashtag:R3-fastforward) OR -intopic:release-cut OR -owner:sora'
    ),
    "MainFastForwardMergerPlus2": (
        'branch:main AND (hashtag:"milestone:R3-fastforward" OR '
        'hashtag:R3-fastforward) AND intopic:release-cut AND owner:sora'
    ),
    "release-cut-promote": (
        'branch:main AND (hashtag:"milestone:R3-fastforward" OR '
        'hashtag:R3-fastforward) AND intopic:release-cut AND owner:sora'
    ),
}


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
    # OP-982 (AUDIT-26c / ADR-0020) + OP-1534: the release-cut SRs are
    # keyed on topic + owner too, so the model carries them.  Empty by
    # default — a change with no release-cut topic / no recorded owner is
    # NOT a release cut, so the carve-out SRs are NOT_APPLICABLE to it
    # (and Human-Plus-2 still applies), exactly as in production.
    #
    # OP-1534 re-key: applicability matches a topic CONTAINING
    # "release-cut" (intopic:, since topic: is exact-match) and owner ==
    # sora — NOT the old ^release-v...$ topic regex / author-identity
    # regex.  ``author`` is retained only so older fixtures still
    # construct; the live SRs no longer read it.
    topic: str = ""
    owner: str = ""
    author: str = ""

    def has_vote(self, *, group: str, min_score: int) -> bool:
        return any(group in v.groups and v.score >= min_score for v in self.votes)

    def has_score(self, score: int) -> bool:
        return any(v.score == score for v in self.votes)


# ──────────────────────────────────────────────────────────────────────
#  OP-1534 — the single canonical release-cut signature that all three
#  carve-out SRs (Human-Plus-2 / MainFastForwardMergerPlus2 /
#  release-cut-promote) now key on, consistently:
#
#     branch:main
#     AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward)
#     AND intopic:release-cut          # substring — topic: is exact-match
#     AND owner:sora
# ──────────────────────────────────────────────────────────────────────

# A real release-cut topic CONTAINS "release-cut" (intopic: substring).
RELEASE_CUT_TOPIC = "release-cut-v0.5.0-rc1"
RELEASE_CUT_OWNER = "sora"
# The bare transition spelling accepted alongside milestone:R3-fastforward.
R3_FASTFORWARD_HASHTAG_BARE = "R3-fastforward"


def _real_cut_change(*, votes: tuple[Vote, ...] = (), **overrides) -> "Change":
    """A change matching all four release-cut keys, unless an override
    knocks one out."""
    kwargs: dict = dict(
        branch="main",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
        topic=RELEASE_CUT_TOPIC,
        owner=RELEASE_CUT_OWNER,
        votes=votes,
    )
    kwargs.update(overrides)
    return Change(**kwargs)


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
    if op == "intopic":
        # OP-1534: intopic:release-cut — SUBSTRING match on the change
        # topic (Gerrit `intopic:` is the substring operator; plain
        # `topic:` is exact-match and would never fire on a
        # release-cut-vX.Y.Z topic).
        return raw.strip('"') in change.topic
    if op == "owner":
        # OP-1534: owner:sora — the change owner (account) identity.
        return change.owner == raw.strip('"')
    if op == "topic":
        # Legacy (pre-OP-1534) exact/regex topic predicate. No live SR
        # uses it after the OP-1534 re-key; retained so the evaluator
        # stays a faithful superset of the query language.
        return re.search(raw.strip('"'), change.topic) is not None
    if op == "author":
        # Legacy (pre-OP-1534) author-identity regex predicate, replaced
        # by owner:sora. Retained for the same reason as topic: above.
        return re.search(raw.strip('"'), change.author) is not None
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


@pytest.fixture(scope="module")
def example_submit_requirements() -> dict[str, dict[str, str]]:
    """The refs/meta/config mirror (.example).  Code AC (OP-1536) requires
    the corrected applicableIf to be asserted in BOTH config files, so the
    keyed-SR tests parse this one too rather than relying solely on the
    project.config↔.example byte-sync check."""
    return _parse_submit_requirements(
        _PROJECT_CONFIG_EXAMPLE.read_text(encoding="utf-8")
    )


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


def _applicable_requirements(change: Change, srs: dict[str, dict[str, str]]) -> set[str]:
    """Names of the submit-requirements whose ``applicableIf`` matches
    this change (mirrors Gerrit's APPLICABLE vs NOT_APPLICABLE split)."""
    return {
        name
        for name, sr in srs.items()
        if _eval_expr(sr.get("applicableIf"), change, default=True)
    }


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

    # OP-1534: applicableIf is now keyed on the FULL release-cut signature
    # (identical to release-cut-promote), not the old doubly-keyed form.
    # Compare with quoting stripped so the contract is robust to
    # "..."-vs-bare tuning.
    applicable = sr["applicableIf"].replace('"', "")
    assert applicable == (
        f"branch:main AND (hashtag:{AUTO_PROMOTE_HASHTAG} OR "
        f"hashtag:{R3_FASTFORWARD_HASHTAG_BARE}) "
        f"AND intopic:release-cut AND owner:{RELEASE_CUT_OWNER}"
    ), applicable

    # submittableIf includes a clause counting merger-bot +2 toward the
    # submit gate (analogous to O6 OP-269), and keeps an operator (path A)
    # +2 as the fallback.
    submittable = sr["submittableIf"]
    assert "label:Code-Review=+2,group=merger-agent-bot" in submittable
    assert "label:Code-Review=+2,group=non-ai-reviewer" in submittable
    assert " OR " in submittable

    assert sr.get("canOverrideInChildProjects") == "false"


def test_human_plus_two_is_carved_out_only_for_autopromote(submit_requirements):
    """AC #1 mechanism (OP-1534): Human-Plus-2 stays unconditional
    everywhere except a genuine release cut — its applicableIf is the
    exact De Morgan negation of the four-key release-cut signature, so
    the carve-out and release-cut-promote cover the SAME set of changes."""
    sr = submit_requirements["Human-Plus-2"]
    applicable = sr["applicableIf"].replace('"', "")
    # NOT (branch:main AND (h_milestone OR h_bare) AND intopic AND owner)
    #   ⇔ -branch:main OR (-h_milestone AND -h_bare) OR -intopic OR -owner
    assert applicable == (
        f"-branch:main OR (-hashtag:{AUTO_PROMOTE_HASHTAG} AND "
        f"-hashtag:{R3_FASTFORWARD_HASHTAG_BARE}) "
        f"OR -intopic:release-cut OR -owner:{RELEASE_CUT_OWNER}"
    ), applicable
    # The actual approval clause is untouched — still a non-ai-reviewer +2.
    assert sr["submittableIf"] == "label:Code-Review=+2,group=non-ai-reviewer"


# ──────────────────────────────────────────────────────────────────────
#  Whole-SR-set consistency (OP-1534) — exercised through the parsed
#  config.  After the re-key, the carve-out region of Human-Plus-2, the
#  applicable region of MainFastForwardMergerPlus2, and the applicable
#  region of release-cut-promote are the SAME set of changes (the four-
#  key release-cut signature).  release-cut-promote (merger +2 AND human
#  +2) is the strictest of the co-applying rules, so for a real cut a
#  human +2 is STILL required — CLAUDE.md L1 stays intact.
# ──────────────────────────────────────────────────────────────────────


def test_carveout_applicability_set_is_consistent(submit_requirements):
    """OP-1534 core invariant — the three SRs partition changes
    consistently: on a full real cut, Human-Plus-2 is NOT applicable
    while MainFastForwardMergerPlus2 AND release-cut-promote ARE; on the
    old bare-hashtag-on-main shape, that flips."""
    real = _real_cut_change(votes=(merger_plus2(), human_plus2()))
    applicable = _applicable_requirements(real, submit_requirements)
    assert "Human-Plus-2" not in applicable
    assert "MainFastForwardMergerPlus2" in applicable
    assert "release-cut-promote" in applicable

    bare_hashtag = Change(
        branch="main",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),  # hashtag but no topic/owner
        votes=(merger_plus2(), human_plus2()),
    )
    applicable_bare = _applicable_requirements(bare_hashtag, submit_requirements)
    assert "Human-Plus-2" in applicable_bare
    assert "MainFastForwardMergerPlus2" not in applicable_bare
    assert "release-cut-promote" not in applicable_bare


def test_real_release_cut_still_needs_human_plus_two(submit_requirements):
    """A genuine release cut is carved out of Human-Plus-2, but
    release-cut-promote co-applies and demands BOTH a merger +2 AND a
    human +2 — so a merger-bot +2 alone can NOT auto-submit it (this is
    the consistency fix: the old MainFastForwardMergerPlus2 merger-only
    path is subsumed by the stricter dual gate)."""
    assert _is_submittable(
        _real_cut_change(votes=(merger_plus2(),)), submit_requirements
    ) is False
    assert _is_submittable(
        _real_cut_change(votes=(human_plus2(),)), submit_requirements
    ) is False
    assert _is_submittable(
        _real_cut_change(votes=(merger_plus2(), human_plus2())), submit_requirements
    ) is True


def test_op1534_bare_hashtag_on_main_no_longer_carved_out(submit_requirements):
    """OP-1534 over-scope closure — THE reason for this ticket.  A
    branch:main change carrying the R3-fastforward hashtag but LACKING the
    release-cut topic/owner used to fall into the doubly-keyed carve-out
    (merger-bot +2 alone could submit).  After the re-key it is no longer
    carved out: Human-Plus-2 applies and a human +2 is required; a merger
    (or any pile of AI) +2 alone does NOT submit it."""
    over_scope = Change(
        branch="main",
        hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
        votes=(merger_plus2(), lint_bot_plus2()),
    )
    assert _is_submittable(over_scope, submit_requirements) is False
    # …and it becomes submittable only once a human signs off.
    assert _is_submittable(
        Change(
            branch="main",
            hashtags=frozenset({AUTO_PROMOTE_HASHTAG}),
            votes=(merger_plus2(), human_plus2()),
        ),
        submit_requirements,
    ) is True


def test_bare_r3_fastforward_hashtag_spelling_is_accepted(submit_requirements):
    """OP-1534 OR transition form — a real cut that carries the bare
    `R3-fastforward` hashtag spelling (instead of the
    `milestone:R3-fastforward` form) still matches the carve-out set and
    still needs both a merger +2 and a human +2."""
    cut_bare = _real_cut_change(
        hashtags=frozenset({R3_FASTFORWARD_HASHTAG_BARE}),
        votes=(merger_plus2(), human_plus2()),
    )
    assert "release-cut-promote" in _applicable_requirements(cut_bare, submit_requirements)
    assert "Human-Plus-2" not in _applicable_requirements(cut_bare, submit_requirements)
    assert _is_submittable(cut_bare, submit_requirements) is True
    # merger alone is still not enough even with the bare spelling.
    assert _is_submittable(
        _real_cut_change(
            hashtags=frozenset({R3_FASTFORWARD_HASHTAG_BARE}),
            votes=(merger_plus2(),),
        ),
        submit_requirements,
    ) is False


def test_regression_arbitrary_main_change_still_needs_human(submit_requirements):
    """`SubmitRuleOverScopes` guard — a change pushed to refs/for/main
    WITHOUT the R3-fastforward hashtag must still require a human +2; a
    merger-bot (or any pile of AI) +2 must NOT submit it."""
    only_ai = Change(
        branch="main",
        hashtags=frozenset(),
        votes=(merger_plus2(), lint_bot_plus2()),
    )
    assert _is_submittable(only_ai, submit_requirements) is False
    with_human = Change(
        branch="main",
        hashtags=frozenset(),
        votes=(merger_plus2(), human_plus2()),
    )
    assert _is_submittable(with_human, submit_requirements) is True


def test_wrong_hashtag_on_main_still_needs_human(submit_requirements):
    """A change carrying a *different* milestone:* hashtag does not match
    the carve-out; the human gate still applies."""
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


def test_non_merger_ai_plus_two_does_not_submit_real_cut(submit_requirements):
    """Even on a fully-keyed real cut, the merger half of
    release-cut-promote only honours a *merger-agent-bot* +2.  A generic
    ai-reviewer-bot +2 (e.g. lint-bot) plus a human +2 does not satisfy
    the merger half, so the cut is not submittable."""
    change = _real_cut_change(votes=(lint_bot_plus2(), human_plus2()))
    assert _is_submittable(change, submit_requirements) is False


def test_negative_vote_blocks_even_real_cut(submit_requirements):
    """No-Veto is unconditional, so a -1 (which the merger-bot itself
    casts when its pre-vote validation fails, and which an operator can
    always cast) blocks submission even on a fully-keyed, fully-+2'd real
    cut."""
    change = _real_cut_change(votes=(merger_plus2(), human_plus2(), minus1()))
    assert _is_submittable(change, submit_requirements) is False


def test_carveout_does_not_leak_to_develop(submit_requirements):
    """Sanity: the carve-out is keyed on branch:main, so an otherwise
    release-cut-shaped change on develop (topic/hashtag/owner all present)
    still needs a human +2 and is governed by Human-Plus-2, not the
    carve-out SRs."""
    on_develop = _real_cut_change(branch="develop", votes=(merger_plus2(),))
    applicable = _applicable_requirements(on_develop, submit_requirements)
    assert "Human-Plus-2" in applicable
    assert "release-cut-promote" not in applicable
    assert "MainFastForwardMergerPlus2" not in applicable
    assert _is_submittable(on_develop, submit_requirements) is False
    assert _is_submittable(
        _real_cut_change(branch="develop", votes=(merger_plus2(), human_plus2())),
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
    # OP-982 adds release-cut-promote to the synced set.
    for name in ("Human-Plus-2", "MainFastForwardMergerPlus2", "release-cut-promote"):
        assert name in example_srs, f"{name} missing from project.config.example"
        assert example_srs[name] == live_srs[name], (
            f"{name} drifted between project.config and project.config.example"
        )


# ──────────────────────────────────────────────────────────────────────
#  OP-982 / AUDIT-26c — the `release-cut-promote` submit-requirement and
#  the MERGE_ALWAYS submit-type on refs/heads/main (ADR-0020): a release
#  cut is ONE merge change on main, quadruply-keyed.  OP-1534 re-keyed it
#  to (branch:main + canonical R3-fastforward hashtag [OR transition form]
#  + intopic:release-cut + owner:sora); it co-exists with the human +2
#  (merger-bot +2 is ADDITIVE, not substitutive — CLAUDE.md L1 stays
#  un-amended).  The shared four-key signature + helper now live at the
#  top of this module (``_real_cut_change``).
# ──────────────────────────────────────────────────────────────────────

_RELEASE_CUT_DESCRIPTION = (
    "Release-cut promotion to main; conditional merger-bot +2 acceptable "
    "when branch + canonical R3-fastforward hashtag + release-cut topic + "
    "owner match the release-cut pattern."
)


def test_release_cut_promote_present_and_wellformed(submit_requirements):
    """AC #2 (OP-982 + OP-1534) — the release-cut-promote block exists, is
    keyed on all four of {branch:main, R3-fastforward hashtag (OR form),
    intopic:release-cut, owner:sora}, and is co-exist (BOTH
    merger-agent-bot +2 AND non-ai-reviewer +2)."""
    assert "release-cut-promote" in submit_requirements, (
        "OP-982: .gerrit/project.config must define the release-cut-promote "
        "submit-requirement"
    )
    sr = submit_requirements["release-cut-promote"]
    assert sr["description"] == _RELEASE_CUT_DESCRIPTION

    # C3a (OP-1536): pin the EXACT corrected applicableIf, not loose
    # substrings — this string is the spec C3a must satisfy and C3b
    # deploys to refs/meta/config.
    applicable = sr["applicableIf"]
    assert applicable == C3A_APPLICABLE_IF["release-cut-promote"], applicable
    # Quadruply-keyed = three top-level ANDs joining the four predicates
    # (branch:main + the parenthesised hashtag-OR + intopic:release-cut +
    # owner:sora). The OR inside the hashtag group is not a top-level join.
    assert applicable.count(" AND ") == 3, applicable

    submittable = sr["submittableIf"]
    assert "label:Code-Review=+2,group=merger-agent-bot" in submittable
    assert "label:Code-Review=+2,group=non-ai-reviewer" in submittable
    # Co-exist (AND), NOT substitute (OR) — this is the distinction from
    # MainFastForwardMergerPlus2.
    assert " AND " in submittable
    assert " OR " not in submittable

    assert sr.get("canOverrideInChildProjects") == "false"


def test_release_cut_promote_applicable_if_corrected_in_both_files(
    submit_requirements, example_submit_requirements
):
    """Code AC (OP-1536) — the C3a corrected ``applicableIf`` for ALL THREE
    keyed submit-requirements (Human-Plus-2, MainFastForwardMergerPlus2,
    release-cut-promote) is pinned byte-for-byte in BOTH
    .gerrit/project.config AND .gerrit/project.config.example.

    These tests DEFINE the desired config: they assert the strings the C3a
    patch produces, statically (a file parse), so the suite stays green
    ahead of — and is never blocked by — the C3b refs/meta/config deploy."""
    for name, expected in C3A_APPLICABLE_IF.items():
        assert submit_requirements[name]["applicableIf"] == expected, (
            f"{name}: project.config applicableIf is not the C3a-corrected form"
        )
        assert example_submit_requirements[name]["applicableIf"] == expected, (
            f"{name}: project.config.example applicableIf is not the C3a-corrected form"
        )


def test_release_cut_needs_both_merger_and_human_plus_two(submit_requirements):
    """The co-exist contract: a real release cut needs a merger-agent-bot
    +2 AND a non-ai-reviewer +2 — neither alone submits it."""
    assert _is_submittable(
        _real_cut_change(votes=(merger_plus2(),)), submit_requirements
    ) is False
    assert _is_submittable(
        _real_cut_change(votes=(human_plus2(),)), submit_requirements
    ) is False
    assert _is_submittable(
        _real_cut_change(votes=(merger_plus2(), human_plus2())), submit_requirements
    ) is True


def test_release_cut_promote_quad_key_negatives(submit_requirements):
    """`SubmitRuleOverScopes` guard, tightened from OP-962's two keys to
    four: knock out ANY one of {intopic:release-cut, owner, hashtag,
    branch:main} and release-cut-promote stops being applicable — the
    change then falls through to the standard gates, never to a
    merger-only path."""
    real = _real_cut_change(votes=(merger_plus2(), human_plus2()))
    assert "release-cut-promote" in _applicable_requirements(real, submit_requirements)

    knockouts = {
        # topic no longer CONTAINS "release-cut" → intopic: stops matching.
        "no topic": _real_cut_change(topic="", votes=(merger_plus2(), human_plus2())),
        "wrong topic": _real_cut_change(
            topic="develop-to-main", votes=(merger_plus2(), human_plus2())
        ),
        # a release-vX.Y.Z topic (the OLD shape) lacks the "release-cut"
        # substring, so the re-keyed intopic: predicate correctly rejects it.
        "old release-v topic": _real_cut_change(
            topic="release-v0.5.0-rc1", votes=(merger_plus2(), human_plus2())
        ),
        "wrong owner": _real_cut_change(
            owner="lint-bot", votes=(merger_plus2(), human_plus2())
        ),
        "no owner": _real_cut_change(
            owner="", votes=(merger_plus2(), human_plus2())
        ),
        "no hashtag": _real_cut_change(
            hashtags=frozenset(), votes=(merger_plus2(), human_plus2())
        ),
        "wrong hashtag": _real_cut_change(
            hashtags=frozenset({"milestone:something-else"}),
            votes=(merger_plus2(), human_plus2()),
        ),
        "on develop": _real_cut_change(
            branch="develop", votes=(merger_plus2(), human_plus2())
        ),
    }
    for label, change in knockouts.items():
        assert "release-cut-promote" not in _applicable_requirements(
            change, submit_requirements
        ), f"release-cut-promote must not be applicable when: {label}"


def test_release_cut_promote_negative_vote_still_blocks(submit_requirements):
    """No-Veto is unconditional, so a -1 (incl. the one merger-bot casts
    when its pre-vote validation fails) blocks even a fully-keyed,
    fully-+2'd release cut."""
    change = _real_cut_change(votes=(merger_plus2(), human_plus2(), minus1()))
    assert _is_submittable(change, submit_requirements) is False


def test_non_merger_ai_plus_two_does_not_satisfy_release_cut(submit_requirements):
    """Even on a fully-keyed release cut, only a *merger-agent-bot* +2
    (not a generic ai-reviewer-bot like lint-bot) counts toward the
    merger half of release-cut-promote."""
    change = _real_cut_change(votes=(lint_bot_plus2(), human_plus2()))
    assert _is_submittable(change, submit_requirements) is False


def test_main_submit_type_is_merge_always_in_both_config_files():
    """AC #2 (OP-982) — refs/heads/main carries submitType = MERGE_ALWAYS
    in both the live config and its refs/meta/config mirror, and the
    project-global submitType is left alone."""
    for path in (_PROJECT_CONFIG, _PROJECT_CONFIG_EXAMPLE):
        text = path.read_text(encoding="utf-8")
        assert re.search(
            r'\[submit "refs/heads/main"\]\s*\n\s*submitType\s*=\s*MERGE_ALWAYS',
            text,
        ), f'{path.name}: missing [submit "refs/heads/main"] submitType = MERGE_ALWAYS'
        # The project-global default is unchanged (still REBASE_IF_NECESSARY).
        assert re.search(r'\[project\][^\[]*?submitType\s*=\s*REBASE_IF_NECESSARY', text, re.DOTALL), (
            f"{path.name}: project-global submitType should stay REBASE_IF_NECESSARY"
        )
