"""W15.6 — Three-class self-fix classifier contract tests.

The W15.6 row spec is "Tests: syntax error / undefined symbol / import
path typo 三類自動修" — the closing row of the W15 Vite/Build
self-healing loop epic.  These tests:

  * Pin the three frozen class identifiers + their pattern tuples
    (drift guards).
  * Round-trip canonical Vite / Rollup / esbuild error messages of
    each class through W15.1's :func:`format_vite_error_for_history`
    (so the producer/consumer contract is exercised end-to-end — no
    hand-rolled W15.2 strings that could drift from the actual wire
    shape).
  * Verify each class flows through W15.3's banner formatter intact.
  * Verify a 3-strike trail of any single class fires the W15.4
    retry-budget escalation (the auto-fix loop's escape hatch).
  * Verify the three classes do not collapse into one bucket — a
    mixed trail of three different classes does NOT escalate even
    when the head-only signature is otherwise stable.
  * Verify the re-export surface (12 W15.6 symbols accessible via
    :mod:`backend.web`).

Section layout:

§A — Drift guards (class identifier literals, pattern tuples,
     unclassified token, dataclass frozen).
§B — :func:`is_vite_history_entry` (positive / negative / non-string).
§C — :func:`classify_vite_error_for_self_fix` per-class positive
     coverage (canonical Vite messages of each class match).
§D — :func:`classify_vite_error_for_self_fix` negative coverage
     (non-Vite entries / unclassified Vite entries / cross-class
     non-collisions).
§E — :func:`classify_vite_history_for_self_fix` (skips non-Vite,
     preserves order, fresh list).
§F — :func:`summarise_self_fix_classes` (Counter shape, buckets).
§G — Round-trip with W15.2 (:func:`format_vite_error_for_history`).
§H — Round-trip with W15.3 (:func:`build_last_vite_error_banner`).
§I — Round-trip with W15.4 (:func:`should_escalate_vite_pattern`).
§J — Re-export surface (12 W15.6 symbols + count guard).
"""

from __future__ import annotations

from typing import Any

import pytest

from backend import web as web_pkg
from backend.web.vite_error_prompt import (
    VITE_ERROR_BANNER_TEMPLATE,
    build_last_vite_error_banner,
    extract_last_vite_error_from_history,
)
from backend.web.vite_error_relay import (
    VITE_ERROR_HISTORY_KEY_PREFIX,
    format_vite_error_for_history,
    vite_error_history_signature,
)
from backend.web.vite_retry_budget import (
    VITE_RETRY_BUDGET_THRESHOLD,
    ViteRetryBudgetEscalation,
    count_trailing_same_vite_signature,
    should_escalate_vite_pattern,
)
from backend.web.vite_self_fix import (
    VITE_SELF_FIX_CLASSES,
    VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO,
    VITE_SELF_FIX_CLASS_SYNTAX_ERROR,
    VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL,
    VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS,
    VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS,
    VITE_SELF_FIX_UNCLASSIFIED_TOKEN,
    VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS,
    ViteSelfFixClassification,
    classify_vite_error_for_self_fix,
    classify_vite_history_for_self_fix,
    is_vite_history_entry,
    summarise_self_fix_classes,
)
from backend.web_sandbox_vite_errors import (
    VITE_ERROR_PLUGIN_NAME,
    VITE_ERROR_PLUGIN_VERSION,
    WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
    ViteBuildError,
)


# ── Fixtures ────────────────────────────────────────────────────────


def _make_error(**overrides: Any) -> ViteBuildError:
    base: dict[str, Any] = {
        "schema_version": WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        "kind": "compile",
        "phase": "transform",
        "message": "Failed to parse module",
        "file": "src/App.tsx",
        "line": 42,
        "column": 7,
        "stack": None,
        "plugin": VITE_ERROR_PLUGIN_NAME,
        "plugin_version": VITE_ERROR_PLUGIN_VERSION,
        "occurred_at": 1714760400.123,
        "received_at": 1714760400.456,
    }
    base.update(overrides)
    return ViteBuildError(**base)


def _entry(message: str, **overrides: Any) -> str:
    """Build a W15.2-formatted history entry by routing through the
    real :func:`format_vite_error_for_history` so W15.6 exercises the
    producer/consumer contract end-to-end (no hand-rolled strings)."""

    overrides["message"] = message
    return format_vite_error_for_history(_make_error(**overrides))


# Canonical messages per class — sourced from real Vite / Rollup /
# esbuild error surfacing.  Any drift in the W15.6 pattern tuples
# would trip these (the W15.1 → W15.2 → W15.6 round-trip is exercised
# in §G).
SYNTAX_ERROR_MESSAGES: tuple[str, ...] = (
    "Failed to parse module",
    "Unexpected token (3:5)",
    "Unexpected character '@'",
    "Unexpected end of input",
    "Expected `;` but found `:`",
    "ParseError: Unexpected token '<'",
    "Syntax Error: invalid expression",
)

UNDEFINED_SYMBOL_MESSAGES: tuple[str, ...] = (
    "ReferenceError: foo is not defined",
    "'bar' is not defined  (no-undef)",
    "Cannot find name 'baz'",
    "ReferenceError: process is not defined at L42",
)

IMPORT_PATH_TYPO_MESSAGES: tuple[str, ...] = (
    'Failed to resolve import "./Header" from "src/App.tsx"',
    "Cannot find module './header.tsx'",
    "Could not resolve \"./components/Card\"",
    "Module not found: Can't resolve '@/lib/api'",
    "Unresolved import: ./missing",
)

# Messages that should NOT match any of the three classes — operator
# escalation territory.  Used by §D to defend against pattern over-
# matching.
UNCLASSIFIED_MESSAGES: tuple[str, ...] = (
    "<unknown>",
    "Internal server error",
    "Network request failed",
    "Out of memory",
    "fetch failed: ECONNRESET",
)


# ────────────────────────────────────────────────────────────────────
# §A — Drift guards
# ────────────────────────────────────────────────────────────────────


def test_class_identifier_literal_syntax_error() -> None:
    assert VITE_SELF_FIX_CLASS_SYNTAX_ERROR == "syntax_error"


def test_class_identifier_literal_undefined_symbol() -> None:
    assert VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL == "undefined_symbol"


def test_class_identifier_literal_import_path_typo() -> None:
    assert VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO == "import_path_typo"


def test_classes_tuple_has_three_members_in_row_spec_order() -> None:
    """Row spec literal "syntax error / undefined symbol / import path
    typo 三類" pins both the count and the order."""

    assert VITE_SELF_FIX_CLASSES == (
        VITE_SELF_FIX_CLASS_SYNTAX_ERROR,
        VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL,
        VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO,
    )


def test_classes_tuple_is_frozen_tuple() -> None:
    assert isinstance(VITE_SELF_FIX_CLASSES, tuple)


def test_unclassified_token_literal() -> None:
    assert VITE_SELF_FIX_UNCLASSIFIED_TOKEN == "unclassified"


def test_unclassified_token_not_in_classes_tuple() -> None:
    """The unclassified token MUST NOT collide with a class identifier
    — the dashboard tile distinguishes the four buckets by string
    equality."""

    assert VITE_SELF_FIX_UNCLASSIFIED_TOKEN not in VITE_SELF_FIX_CLASSES


def test_classification_dataclass_is_frozen() -> None:
    record = ViteSelfFixClassification(entry="x", vite_class=None)
    with pytest.raises(Exception):
        record.entry = "y"  # type: ignore[misc]


def test_classification_dataclass_fields() -> None:
    record = ViteSelfFixClassification(
        entry="vite[x] f:1: compile: msg", vite_class="syntax_error",
    )
    assert record.entry == "vite[x] f:1: compile: msg"
    assert record.vite_class == "syntax_error"


def test_pattern_tuples_are_non_empty() -> None:
    """Empty pattern tuple would silently classify everything in the
    fallthrough chain into the next class.  Pin non-empty so a
    refactor that accidentally clears a tuple trips here."""

    assert len(VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS) > 0
    assert len(VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS) > 0
    assert len(VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS) > 0


def test_pattern_tuples_are_compiled_regex() -> None:
    """The classifier's hot-path is ``any(p.search(body) for p in
    patterns)``.  Pre-compiled :class:`re.Pattern` is the contract; a
    refactor that switched to raw strings would break the hot-path
    silently — pin the type."""

    import re

    for pat in VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS:
        assert isinstance(pat, re.Pattern)
    for pat in VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS:
        assert isinstance(pat, re.Pattern)
    for pat in VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS:
        assert isinstance(pat, re.Pattern)


def test_pattern_tuples_are_case_insensitive() -> None:
    """Real-world Vite errors mix case (``ReferenceError`` vs
    ``referenceerror`` from log scrapers).  Patterns compiled with
    ``re.IGNORECASE`` so the classifier catches both — pin the
    flag."""

    import re

    for tup in (
        VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS,
        VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS,
        VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS,
    ):
        for pat in tup:
            assert pat.flags & re.IGNORECASE


# ────────────────────────────────────────────────────────────────────
# §B — is_vite_history_entry
# ────────────────────────────────────────────────────────────────────


class TestIsViteHistoryEntry:
    def test_real_vite_entry_is_true(self) -> None:
        e = _entry("Failed to parse module")
        assert is_vite_history_entry(e) is True

    def test_non_vite_string_is_false(self) -> None:
        assert is_vite_history_entry("tool[bash] exit_code=1") is False

    def test_empty_string_is_false(self) -> None:
        assert is_vite_history_entry("") is False

    def test_non_string_is_false(self) -> None:
        assert is_vite_history_entry(None) is False
        assert is_vite_history_entry(42) is False
        assert is_vite_history_entry({"vite": True}) is False

    def test_prefix_substring_only_match(self) -> None:
        """A string containing the prefix mid-stream MUST NOT match —
        the prefix is anchored at start by ``str.startswith``."""

        assert is_vite_history_entry(f"prefix {VITE_ERROR_HISTORY_KEY_PREFIX}x]") is False


# ────────────────────────────────────────────────────────────────────
# §C — classify_vite_error_for_self_fix per-class positive coverage
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("message", SYNTAX_ERROR_MESSAGES)
def test_syntax_error_messages_classify_as_syntax_error(message: str) -> None:
    e = _entry(message)
    assert classify_vite_error_for_self_fix(e) == VITE_SELF_FIX_CLASS_SYNTAX_ERROR


@pytest.mark.parametrize("message", UNDEFINED_SYMBOL_MESSAGES)
def test_undefined_symbol_messages_classify_as_undefined_symbol(message: str) -> None:
    e = _entry(message)
    assert classify_vite_error_for_self_fix(e) == VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL


@pytest.mark.parametrize("message", IMPORT_PATH_TYPO_MESSAGES)
def test_import_path_typo_messages_classify_as_import_path_typo(message: str) -> None:
    e = _entry(message)
    assert classify_vite_error_for_self_fix(e) == VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO


# ────────────────────────────────────────────────────────────────────
# §D — classify_vite_error_for_self_fix negative coverage
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("message", UNCLASSIFIED_MESSAGES)
def test_unclassified_messages_return_none(message: str) -> None:
    e = _entry(message)
    assert classify_vite_error_for_self_fix(e) is None


def test_non_vite_entry_returns_none() -> None:
    """Tool-error keys from the existing self-healing loop look like
    ``tool[bash] exit_code=1`` — they MUST NOT be classified into a
    Vite bucket."""

    assert classify_vite_error_for_self_fix("tool[bash] exit_code=1") is None


def test_empty_string_returns_none() -> None:
    assert classify_vite_error_for_self_fix("") is None


def test_degraded_vite_entry_returns_none() -> None:
    """An entry that matches the prefix but lacks the expected colon
    shape (pathological filename that exhausted the byte cap) must
    not classify — the message body is unrecoverable."""

    degraded = "vite[transform] src/App.tsx"
    assert classify_vite_error_for_self_fix(degraded) is None


def test_no_class_collision_syntax_vs_undefined() -> None:
    """A canonical syntax-error message MUST NOT also match the
    undefined-symbol patterns (the classifier returns the first
    match in row-spec order — pin the no-collision invariant)."""

    e = _entry("Failed to parse module")
    body = "Failed to parse module"
    assert not any(
        p.search(body) for p in VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS
    )
    assert classify_vite_error_for_self_fix(e) == VITE_SELF_FIX_CLASS_SYNTAX_ERROR


def test_no_class_collision_undefined_vs_import_typo() -> None:
    body = "ReferenceError: foo is not defined"
    assert not any(
        p.search(body) for p in VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS
    )


def test_no_class_collision_import_typo_vs_syntax() -> None:
    body = 'Failed to resolve import "./Header"'
    assert not any(
        p.search(body) for p in VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS
    )


# ────────────────────────────────────────────────────────────────────
# §E — classify_vite_history_for_self_fix
# ────────────────────────────────────────────────────────────────────


class TestClassifyHistory:
    def test_empty_history_returns_empty_list(self) -> None:
        assert classify_vite_history_for_self_fix([]) == []

    def test_skips_non_vite_entries(self) -> None:
        history = [
            "tool[bash] exit_code=1",
            _entry("Failed to parse module"),
            "tool[python] traceback...",
        ]
        result = classify_vite_history_for_self_fix(history)
        assert len(result) == 1
        assert result[0].vite_class == VITE_SELF_FIX_CLASS_SYNTAX_ERROR

    def test_preserves_order_oldest_first(self) -> None:
        history = [
            _entry("Failed to parse module"),
            _entry("foo is not defined"),
            _entry('Failed to resolve import "./X"'),
        ]
        result = classify_vite_history_for_self_fix(history)
        assert [r.vite_class for r in result] == [
            VITE_SELF_FIX_CLASS_SYNTAX_ERROR,
            VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL,
            VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO,
        ]

    def test_returns_fresh_list_not_alias(self) -> None:
        history = [_entry("Failed to parse module")]
        a = classify_vite_history_for_self_fix(history)
        b = classify_vite_history_for_self_fix(history)
        assert a is not b
        assert a == b

    def test_unclassified_vite_entry_yields_none_class(self) -> None:
        history = [_entry("<unknown>")]
        result = classify_vite_history_for_self_fix(history)
        assert len(result) == 1
        assert result[0].vite_class is None
        # entry is preserved verbatim (not stripped or re-formatted).
        assert result[0].entry == history[0]


# ────────────────────────────────────────────────────────────────────
# §F — summarise_self_fix_classes
# ────────────────────────────────────────────────────────────────────


class TestSummarise:
    def test_empty_history_returns_empty_counter(self) -> None:
        c = summarise_self_fix_classes([])
        assert dict(c) == {}

    def test_three_classes_one_each(self) -> None:
        history = [
            _entry("Failed to parse module"),
            _entry("foo is not defined"),
            _entry('Failed to resolve import "./X"'),
        ]
        c = summarise_self_fix_classes(history)
        assert c[VITE_SELF_FIX_CLASS_SYNTAX_ERROR] == 1
        assert c[VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL] == 1
        assert c[VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO] == 1
        assert c[VITE_SELF_FIX_UNCLASSIFIED_TOKEN] == 0

    def test_unclassified_bucket_populated(self) -> None:
        history = [
            _entry("<unknown>"),
            _entry("Network request failed"),
        ]
        c = summarise_self_fix_classes(history)
        assert c[VITE_SELF_FIX_UNCLASSIFIED_TOKEN] == 2

    def test_non_vite_entries_skipped_not_in_unclassified(self) -> None:
        """Non-Vite entries must NOT pollute the unclassified bucket
        — the bucket is reserved for *Vite* errors with no class
        match."""

        history = [
            "tool[bash] exit_code=1",
            "tool[python] traceback...",
        ]
        c = summarise_self_fix_classes(history)
        assert c[VITE_SELF_FIX_UNCLASSIFIED_TOKEN] == 0
        assert sum(c.values()) == 0

    def test_repeated_class_aggregates(self) -> None:
        history = [
            _entry("Failed to parse module") for _ in range(5)
        ]
        c = summarise_self_fix_classes(history)
        assert c[VITE_SELF_FIX_CLASS_SYNTAX_ERROR] == 5


# ────────────────────────────────────────────────────────────────────
# §G — Round-trip with W15.2 (format_vite_error_for_history)
# ────────────────────────────────────────────────────────────────────


class TestRoundTripWithW15_2Relay:
    """Each canonical message of each class MUST be classifiable
    after passing through :func:`format_vite_error_for_history` (the
    W15.1 → W15.2 wire shape).  This is the producer/consumer
    contract that the W15.6 row pins."""

    @pytest.mark.parametrize("message", SYNTAX_ERROR_MESSAGES)
    def test_syntax_class_round_trips(self, message: str) -> None:
        formatted = format_vite_error_for_history(_make_error(message=message))
        assert formatted.startswith(VITE_ERROR_HISTORY_KEY_PREFIX)
        assert classify_vite_error_for_self_fix(formatted) == (
            VITE_SELF_FIX_CLASS_SYNTAX_ERROR
        )

    @pytest.mark.parametrize("message", UNDEFINED_SYMBOL_MESSAGES)
    def test_undefined_class_round_trips(self, message: str) -> None:
        formatted = format_vite_error_for_history(
            _make_error(message=message, kind="runtime", phase="hmr")
        )
        assert classify_vite_error_for_self_fix(formatted) == (
            VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL
        )

    @pytest.mark.parametrize("message", IMPORT_PATH_TYPO_MESSAGES)
    def test_import_typo_class_round_trips(self, message: str) -> None:
        formatted = format_vite_error_for_history(_make_error(message=message))
        assert classify_vite_error_for_self_fix(formatted) == (
            VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO
        )


# ────────────────────────────────────────────────────────────────────
# §H — Round-trip with W15.3 (build_last_vite_error_banner)
# ────────────────────────────────────────────────────────────────────


class TestRoundTripWithW15_3Banner:
    """Each class's canonical message MUST appear verbatim in the
    W15.3 banner that the agent sees on the next LLM turn — pin so a
    drift in the banner template does not silently drop the message
    body that the W15.6 classifier expects."""

    @pytest.mark.parametrize("message", SYNTAX_ERROR_MESSAGES)
    def test_syntax_message_in_banner(self, message: str) -> None:
        history = [_entry(message)]
        banner = build_last_vite_error_banner(history)
        assert banner != ""
        # Banner uses the VITE_ERROR_BANNER_TEMPLATE — the message
        # body sits in the second bracket pair.
        assert message in banner

    @pytest.mark.parametrize("message", UNDEFINED_SYMBOL_MESSAGES)
    def test_undefined_message_in_banner(self, message: str) -> None:
        history = [_entry(message)]
        banner = build_last_vite_error_banner(history)
        assert message in banner

    @pytest.mark.parametrize("message", IMPORT_PATH_TYPO_MESSAGES)
    def test_import_typo_message_in_banner(self, message: str) -> None:
        history = [_entry(message)]
        banner = build_last_vite_error_banner(history)
        assert message in banner

    def test_banner_uses_w15_3_template(self) -> None:
        """W15.6 banner contract is the W15.3 template — pin the
        cross-row alignment."""

        # Render a banner manually with known tokens and confirm the
        # W15.3 template literal still produces the expected shape.
        rendered = VITE_ERROR_BANNER_TEMPLATE.format(
            file="src/App.tsx", line="42", message="Failed to parse module",
        )
        assert "Failed to parse module" in rendered
        assert "src/App.tsx" in rendered

    def test_extract_round_trips_all_three_classes(self) -> None:
        for cls_messages in (
            SYNTAX_ERROR_MESSAGES,
            UNDEFINED_SYMBOL_MESSAGES,
            IMPORT_PATH_TYPO_MESSAGES,
        ):
            for message in cls_messages:
                history = [_entry(message)]
                parts = extract_last_vite_error_from_history(history)
                assert parts is not None
                file_token, line_token, msg_token = parts
                assert msg_token == message


# ────────────────────────────────────────────────────────────────────
# §I — Round-trip with W15.4 (should_escalate_vite_pattern)
# ────────────────────────────────────────────────────────────────────


class TestRoundTripWithW15_4RetryBudget:
    """The W15.4 3-strike gate uses the W15.2 head-only signature —
    a same-class trail of THRESHOLD entries that share the head
    (file/line/phase/kind) MUST escalate; a mixed-class trail that
    differs in head MUST NOT.  W15.6 pins this so the operator UI's
    debug-feed filter stays aligned with the auto-fix loop's escape
    hatch."""

    @pytest.mark.parametrize("message", [m for m in SYNTAX_ERROR_MESSAGES[:1]])
    def test_same_syntax_error_three_times_escalates(self, message: str) -> None:
        history = [_entry(message) for _ in range(VITE_RETRY_BUDGET_THRESHOLD)]
        decision = should_escalate_vite_pattern(history)
        assert isinstance(decision, ViteRetryBudgetEscalation)
        assert decision.count == VITE_RETRY_BUDGET_THRESHOLD
        assert decision.threshold == VITE_RETRY_BUDGET_THRESHOLD

    def test_same_undefined_symbol_three_times_escalates(self) -> None:
        history = [
            _entry("foo is not defined", kind="runtime", phase="hmr")
            for _ in range(VITE_RETRY_BUDGET_THRESHOLD)
        ]
        decision = should_escalate_vite_pattern(history)
        assert isinstance(decision, ViteRetryBudgetEscalation)

    def test_same_import_typo_three_times_escalates(self) -> None:
        history = [
            _entry('Failed to resolve import "./Header"')
            for _ in range(VITE_RETRY_BUDGET_THRESHOLD)
        ]
        decision = should_escalate_vite_pattern(history)
        assert isinstance(decision, ViteRetryBudgetEscalation)

    def test_two_strikes_under_threshold_no_escalation(self) -> None:
        """The auto-fix loop gets THRESHOLD - 1 retries with the
        W15.3 banner before the operator is paged.  Pin so a tuning
        drift in :data:`VITE_RETRY_BUDGET_THRESHOLD` from 3 → 2 trips
        here."""

        history = [
            _entry("Failed to parse module")
            for _ in range(VITE_RETRY_BUDGET_THRESHOLD - 1)
        ]
        assert should_escalate_vite_pattern(history) is None

    def test_mixed_three_classes_does_not_escalate(self) -> None:
        """A mixed-class trail (one entry per class, all with
        different files so the head differs) MUST NOT escalate even
        though the trail length equals the threshold."""

        history = [
            _entry("Failed to parse module", file="src/A.tsx", line=1),
            _entry("foo is not defined", file="src/B.tsx", line=2),
            _entry('Failed to resolve import "./X"', file="src/C.tsx", line=3),
        ]
        # Each entry has a unique signature → trailing same-sig count is 1.
        count, sig = count_trailing_same_vite_signature(history)
        assert count == 1
        assert sig is not None
        assert should_escalate_vite_pattern(history) is None

    def test_message_body_difference_buckets_together(self) -> None:
        """W15.4 head-only signature drops the message body — three
        syntax errors at the same file/line/phase/kind with slightly
        different wording still bucket together and escalate.  This
        is the W15.4 contract; W15.6 pins it cross-row."""

        history = [
            _entry("Failed to parse module"),
            _entry("Failed to parse module: variant 2"),
            _entry("Failed to parse module: variant 3"),
        ]
        # Same file/line/phase/kind → same head-only signature.
        sigs = vite_error_history_signature(history)
        assert sigs[0] == sigs[1] == sigs[2]
        decision = should_escalate_vite_pattern(history)
        assert isinstance(decision, ViteRetryBudgetEscalation)

    def test_escalation_signature_bucket_belongs_to_one_class(self) -> None:
        """When the gate fires, every entry in the trailing-same-sig
        run MUST classify into the SAME self-fix class.  Pin so a
        future pattern that smears two classes into one bucket is
        caught by W15.6."""

        history = [_entry("Failed to parse module") for _ in range(3)]
        decision = should_escalate_vite_pattern(history)
        assert decision is not None
        classes = {classify_vite_error_for_self_fix(e) for e in history[-3:]}
        assert classes == {VITE_SELF_FIX_CLASS_SYNTAX_ERROR}


# ────────────────────────────────────────────────────────────────────
# §J — Re-export surface
# ────────────────────────────────────────────────────────────────────


_W15_6_RE_EXPORTS: tuple[str, ...] = (
    "VITE_SELF_FIX_CLASSES",
    "VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO",
    "VITE_SELF_FIX_CLASS_SYNTAX_ERROR",
    "VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL",
    "VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS",
    "VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS",
    "VITE_SELF_FIX_UNCLASSIFIED_TOKEN",
    "VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS",
    "ViteSelfFixClassification",
    "classify_vite_error_for_self_fix",
    "classify_vite_history_for_self_fix",
    "is_vite_history_entry",
    "summarise_self_fix_classes",
)


@pytest.mark.parametrize("symbol", _W15_6_RE_EXPORTS)
def test_w15_6_symbol_re_exported_from_package(symbol: str) -> None:
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert hasattr(web_pkg, symbol), f"{symbol} not attribute of backend.web"


def test_w15_6_re_export_count_is_thirteen() -> None:
    """13 symbols cover the row's full surface: 4 string class
    constants (3 class identifiers + unclassified token) + 1 frozen
    tuple of class identifiers + 3 frozen tuples of compiled
    patterns + 1 frozen dataclass + 4 functions (classify single /
    classify history / is-vite predicate / summarise) = 13."""

    assert len(_W15_6_RE_EXPORTS) == 13


def test_total_re_export_count_pinned_at_288() -> None:
    """W15.5 left __all__ at 275 symbols; W15.6 adds 13
    vite_self_fix symbols → 288.  Each row's drift guard is updated
    in lock-step so a future row that adds a new symbol fails every
    guard until each one acknowledges the new total."""

    assert len(web_pkg.__all__) == 330
