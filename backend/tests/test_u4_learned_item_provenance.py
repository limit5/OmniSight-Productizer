"""OP-2569 U4-E — server-derived provenance tests (fully offline).

The module under test is the PURE seam: raw Gerrit query dicts and JIRA
label lists are injected as fixtures — no network, no DB, no clock (the
module never reads the clock; the caller supplies ``now``). Fixture
dicts mirror the actual SSH ``gerrit query --format=JSON
--current-patch-set`` payload shape from ``backend/gerrit.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.learned_item_provenance import (
    GROUND_TRUTH_KINDS,
    GroundTruth,
    ProvenanceUnconfirmable,
    ReverifyResult,
    derive_ground_truths,
    normalize_change_ref,
    reverify_for_promotion,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PURE_MODULE = BACKEND_ROOT / "learned_item_provenance.py"

NOW = "2026-07-10T12:00:00+00:00"
CHANGE_ID = "I" + "0123456789abcdef" * 2 + "01234567"  # I + 40 lowercase hex


def _change(status: str = "NEW", approvals: list[dict] | None = None) -> dict:
    """Shape mirrors the raw SSH query payload used across the repo."""
    return {
        "project": "omnisight",
        "branch": "develop",
        "id": CHANGE_ID,
        "number": "2046",
        "subject": "[OP-2046] some change",
        "status": status,
        "currentPatchSet": {
            "number": "3",
            "revision": "d" * 40,
            "approvals": approvals or [],
        },
    }


def _approval(type_: str, value: str, username: str | None) -> dict:
    approval: dict = {"type": type_, "value": value}
    if username is not None:
        approval["by"] = {"name": username.title(), "username": username}
    return approval


def _derive(gerrit_change=None, jira_labels=None, change_ref="#2046"):
    return derive_ground_truths(
        change_ref=change_ref,
        gerrit_change=gerrit_change,
        jira_labels=jira_labels,
        now=NOW,
    )


# ━━ (a) normalize_change_ref ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNormalizeChangeRef:
    def test_hash_number(self) -> None:
        assert normalize_change_ref("#2046") == "gerrit:2046"

    def test_bare_number(self) -> None:
        assert normalize_change_ref("2046") == "gerrit:2046"

    def test_leading_zeros_preserved(self) -> None:
        assert normalize_change_ref("#0046") == "gerrit:0046"

    def test_change_id_verbatim(self) -> None:
        assert len(CHANGE_ID) == 41
        assert normalize_change_ref(CHANGE_ID) == CHANGE_ID

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not a ref",
            "I123",  # short Change-Id
            " 2046",  # no whitespace stripping
            "OP-2472",  # JIRA key (A2↔E gap — fail-closed)
            "https://gerrit.example/c/2046",
            "I" + "ABCDEF0123" * 4,  # uppercase hex rejected
            "٢٠٤٦",  # unicode digits — not ASCII
        ],
    )
    def test_rejected_with_bad_change_ref_reason(self, bad: str) -> None:
        with pytest.raises(ProvenanceUnconfirmable) as excinfo:
            normalize_change_ref(bad)
        assert excinfo.value.reason == "bad_change_ref"


# ━━ (b) merged ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMerged:
    def test_merged_status_yields_exactly_one_truth(self) -> None:
        truths = _derive(gerrit_change=_change("MERGED"))
        assert len(truths) == 1
        (truth,) = truths
        assert truth.kind == "merged"
        assert truth.revert_state == "none"
        assert truth.evidence_span == {"status": "MERGED"}
        assert truth.source_change_id == "gerrit:2046"
        assert truth.verified_at == NOW

    def test_new_status_alone_raises(self) -> None:
        with pytest.raises(ProvenanceUnconfirmable) as excinfo:
            _derive(gerrit_change=_change("NEW"))
        assert excinfo.value.reason == "no_confirmable_signal"


# ━━ (c) review_plus2 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReviewPlus2:
    def test_human_plus2_counts(self) -> None:
        change = _change("NEW", [_approval("Code-Review", "2", "sora")])
        truths = _derive(gerrit_change=change)
        assert [t.kind for t in truths] == ["review_plus2"]
        assert truths[0].evidence_span == {
            "label": "Code-Review",
            "value": 2,
            "by": "sora",
        }

    def test_plus_prefixed_string_value_parses(self) -> None:
        change = _change("NEW", [_approval("Code-Review", "+2", "sora")])
        truths = _derive(gerrit_change=change)
        assert [t.kind for t in truths] == ["review_plus2"]
        assert truths[0].evidence_span["value"] == 2

    @pytest.mark.parametrize(
        "bot",
        [
            "claude-bot-claude-2",  # mid-string -bot (plain substring)
            "ai-reviewer",
            "ci-worker",
        ],
    )
    def test_bot_shaped_plus2_does_not_count(self, bot: str) -> None:
        change = _change("NEW", [_approval("Code-Review", "2", bot)])
        with pytest.raises(ProvenanceUnconfirmable):
            _derive(gerrit_change=change)

    def test_missing_by_does_not_count(self) -> None:
        change = _change("NEW", [_approval("Code-Review", "2", None)])
        with pytest.raises(ProvenanceUnconfirmable):
            _derive(gerrit_change=change)

    def test_human_plus1_does_not_count(self) -> None:
        change = _change("NEW", [_approval("Code-Review", "1", "sora")])
        with pytest.raises(ProvenanceUnconfirmable):
            _derive(gerrit_change=change)


# ━━ (d) ci_pass ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCiPass:
    def test_verified_plus1_by_bot_counts(self) -> None:
        change = _change("NEW", [_approval("Verified", "+1", "ci-worker")])
        truths = _derive(gerrit_change=change)
        assert [t.kind for t in truths] == ["ci_pass"]
        assert truths[0].evidence_span == {
            "label": "Verified",
            "value": 1,
            "by": "ci-worker",
        }

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_verified_zero_or_negative_does_not_count(self, value: str) -> None:
        change = _change("NEW", [_approval("Verified", value, "ci-worker")])
        with pytest.raises(ProvenanceUnconfirmable):
            _derive(gerrit_change=change)


# ━━ (e) stoploss ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStoploss:
    @pytest.mark.parametrize(
        "label",
        [
            "runner-stoploss:revert-OP-2046",
            "runner-stoploss:circuit-tripped-claude-1",
        ],
    )
    def test_each_prefix_fires_with_reverted_state(self, label: str) -> None:
        truths = _derive(jira_labels=[label, "unrelated-label"])
        assert [t.kind for t in truths] == ["stoploss"]
        assert truths[0].revert_state == "reverted"
        assert truths[0].evidence_span == {"jira_labels": [label]}

    def test_combines_with_merged(self) -> None:
        truths = _derive(
            gerrit_change=_change("MERGED"),
            jira_labels=["runner-stoploss:revert-OP-2046"],
        )
        kinds = {t.kind for t in truths}
        assert kinds == {"merged", "stoploss"}
        by_kind = {t.kind: t for t in truths}
        assert by_kind["merged"].revert_state == "none"
        assert by_kind["stoploss"].revert_state == "reverted"

    def test_non_stoploss_labels_alone_raise(self) -> None:
        with pytest.raises(ProvenanceUnconfirmable):
            _derive(jira_labels=["runner-pushed-to-gerrit", "meta:retrospective"])


# ━━ (f) multi-truth ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMultiTruth:
    def test_merged_with_human_plus2_and_verified(self) -> None:
        change = _change(
            "MERGED",
            [
                _approval("Code-Review", "2", "sora"),
                _approval("Verified", "1", "ci-worker"),
            ],
        )
        truths = _derive(gerrit_change=change)
        assert {t.kind for t in truths} == {"merged", "review_plus2", "ci_pass"}
        assert len(truths) == 3
        for truth in truths:
            assert truth.source_change_id == "gerrit:2046"
            assert truth.verified_at == NOW
            assert truth.revert_state == "none"


# ━━ (g) fail-closed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFailClosed:
    def test_both_inputs_none_raises(self) -> None:
        with pytest.raises(ProvenanceUnconfirmable) as excinfo:
            _derive(gerrit_change=None, jira_labels=None)
        assert excinfo.value.reason == "no_confirmable_signal"

    def test_abandoned_only_raises(self) -> None:
        with pytest.raises(ProvenanceUnconfirmable):
            _derive(gerrit_change=_change("ABANDONED"))


# ━━ (h) reverify matrix ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _reverify(original_kind="merged", gerrit_change=None, jira_labels=None):
    return reverify_for_promotion(
        original_kind=original_kind,
        change_ref="#2046",
        gerrit_change=gerrit_change,
        jira_labels=jira_labels,
        now=NOW,
    )


class TestReverifyForPromotion:
    def test_fresh_stoploss_blocks(self) -> None:
        result = _reverify(
            gerrit_change=_change("MERGED"),
            jira_labels=["runner-stoploss:revert-OP-2046"],
        )
        assert result == ReverifyResult(
            ok=False,
            revert_state="reverted",
            reasons=("stoploss_since_evidence",),
        )

    def test_fresh_lookup_none_is_unconfirmable_not_raised(self) -> None:
        result = _reverify(gerrit_change=None, jira_labels=None)
        assert result.ok is False
        assert result.revert_state == "none"
        assert result.reasons == ("unconfirmable_at_promotion",)

    def test_original_kind_unconfirmed(self) -> None:
        # Fresh lookup confirms review_plus2 only — original merged gone.
        change = _change("NEW", [_approval("Code-Review", "2", "sora")])
        result = _reverify(original_kind="merged", gerrit_change=change)
        assert result.ok is False
        assert result.reasons == ("original_kind_unconfirmed",)

    def test_reasons_accumulate(self) -> None:
        # Stoploss labels on a ref whose derivation raises: the label
        # check runs on the RAW labels independently of derivation.
        result = reverify_for_promotion(
            original_kind="merged",
            change_ref="not a ref",
            gerrit_change=_change("MERGED"),
            jira_labels=["runner-stoploss:circuit-tripped-claude-1"],
            now=NOW,
        )
        assert result.ok is False
        assert result.revert_state == "reverted"
        assert result.reasons == (
            "stoploss_since_evidence",
            "unconfirmable_at_promotion",
        )

    def test_healthy_merged_to_merged(self) -> None:
        result = _reverify(gerrit_change=_change("MERGED"))
        assert result == ReverifyResult(ok=True, revert_state="none", reasons=())

    def test_bad_original_kind_raises_plain_valueerror(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _reverify(original_kind="not-a-kind", gerrit_change=_change("MERGED"))
        # ProvenanceUnconfirmable IS a ValueError subclass — the guard
        # must raise the PLAIN one (programmer error, not an outcome).
        assert not isinstance(excinfo.value, ProvenanceUnconfirmable)


# ━━ (i) determinism ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeterminism:
    def test_same_inputs_same_now_identical_tuples(self) -> None:
        change = _change(
            "MERGED",
            [
                _approval("Code-Review", "2", "sora"),
                _approval("Verified", "1", "ci-worker"),
            ],
        )
        labels = ["runner-stoploss:revert-OP-2046"]
        first = _derive(gerrit_change=change, jira_labels=labels)
        second = _derive(gerrit_change=change, jira_labels=labels)
        assert first == second

    def test_module_source_has_no_clock_reads(self) -> None:
        src = PURE_MODULE.read_text()
        assert "datetime.now" not in src
        assert "time.time" not in src
        # Belt-and-braces: the module must not import the clock modules
        # at all (comments included — the substring check above matches
        # across dots and inside comments).
        assert "import datetime" not in src
        assert "import time" not in src


# ━━ (j) dormant ship (A2's rglob pattern, verbatim) ━━━━━━━━━━━━━━━━━━━


class TestDormantShip:
    def test_no_nontest_module_references_the_new_module(self) -> None:
        own = {"learned_item_provenance.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if len(parts) == 1 and parts[0] in own:
                continue
            text = py.read_text(errors="ignore")
            if "learned_item_provenance" in text:
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"


# ━━ (k) string bans ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


LEDGER_TABLE_NAMES = (
    "learned_item_versions",
    "learned_item_evidence",
    "memory_eval_runs",
    "memory_eval_cases",
    "memory_approvals",
    "memory_publications",
    "memory_transition_events",
    "learned_item_snapshots",
)

SIBLING_MODULE_NAMES = (
    "learned_item_record",
    "learned_item_renderer",
    "learned_item_publisher",
    "learned_item_publication",
    "_SKILLS_LIVE",
)


class TestPureModuleStringBans:
    def test_no_ledger_table_name_in_pure_module(self) -> None:
        # The module is NOT on the A1 dormant-sweep allowlist — it must
        # not name any ledger table.
        source = PURE_MODULE.read_text()
        for table in LEDGER_TABLE_NAMES:
            assert table not in source, table

    def test_no_sibling_module_name_in_pure_module(self) -> None:
        source = PURE_MODULE.read_text()
        for name in SIBLING_MODULE_NAMES:
            assert name not in source, name


# ━━ Contract shape ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestContractShape:
    def test_kinds_mirror_the_0258_check(self) -> None:
        assert GROUND_TRUTH_KINDS == (
            "merged",
            "ci_pass",
            "review_plus2",
            "reverted",
            "stoploss",
        )

    def test_ground_truth_is_frozen(self) -> None:
        (truth,) = _derive(gerrit_change=_change("MERGED"))
        assert isinstance(truth, GroundTruth)
        with pytest.raises(AttributeError):
            truth.kind = "stoploss"  # type: ignore[misc]
