"""P5 (#290) — Unit tests for backend.store_submission (O7 dual-+2 gate)."""

from __future__ import annotations

import pytest

from backend import codesign_store
from backend import store_submission as sub
from backend import submit_rule as sr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _fresh_chains():
    sub.reset_submission_chain_for_tests()
    codesign_store.reset_global_audit_chain_for_tests()
    yield
    sub.reset_submission_chain_for_tests()
    codesign_store.reset_global_audit_chain_for_tests()


@pytest.fixture()
def signed_artifact_sha():
    sha = "a" * 64
    chain = codesign_store.get_global_audit_chain()
    chain.persist = False
    chain.append(
        cert_id="apple.dev.acme",
        cert_fingerprint="fp",
        artifact_path="/tmp/app.ipa",
        artifact_sha256=sha,
        actor="merger-agent-bot",
        hsm_vendor="none",
    )
    return sha


def _vote_bundle(*, human=True, merger=True, negative=False):
    votes = []
    if human:
        votes.append(sr.human_vote("alice@acme"))
    if merger:
        votes.append(sr.merger_vote())
    if negative:
        votes.append(sr.human_vote("bob@acme", score=-1))
    return votes


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Happy path — store-facing target requires both +2s
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHappyPath:
    def test_allow_when_both_plus_two(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.app_store_review,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(),
            release_notes={"en-US": "First release"},
        )
        assert ctx.allow is True
        assert ctx.reason == "allow"
        assert ctx.human_plus_twos == 1
        assert ctx.merger_plus_twos == 1
        assert ctx.release_notes_langs == ("en-US",)

    def test_allow_includes_audit_chain_entry(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.play_production,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(),
            release_notes={"en-US": "ship", "zh-TW": "上架"},
        )
        head = sub.get_submission_chain().head()
        assert head == ctx.audit_entry["curr_hash"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rejection paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRejections:
    def test_missing_human_plus_two(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.app_store_review,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(human=False),
            release_notes={"en-US": "ship"},
        )
        assert ctx.allow is False
        assert "human" in ctx.reason

    def test_missing_merger_plus_two(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.app_store_review,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(merger=False),
            release_notes={"en-US": "ship"},
        )
        assert ctx.allow is False
        assert "merger" in ctx.reason

    def test_negative_vote_blocks_even_with_both(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.app_store_review,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(negative=True),
            release_notes={"en-US": "ship"},
        )
        assert ctx.allow is False
        assert ctx.reason == "reject_negative_vote"

    def test_missing_release_notes_blocks_store_target(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.play_production,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(),
            release_notes=None,
        )
        assert ctx.allow is False
        assert ctx.reason == "reject_missing_release_notes"

    def test_unknown_artifact_blocks(self):
        # No codesign entry for this sha — must be rejected.
        ctx = sub.approve_submission(
            target=sub.StoreTarget.app_store_review,
            artifact_sha256="c" * 64,
            votes=_vote_bundle(),
            release_notes={"en-US": "ship"},
        )
        assert ctx.allow is False
        assert ctx.reason == "reject_unknown_artifact"

    def test_bad_sha_raises(self):
        with pytest.raises(sub.StoreSubmissionError):
            sub.approve_submission(
                target=sub.StoreTarget.app_store_review,
                artifact_sha256="notahash",
                votes=[],
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal targets — merger-only path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInternalTargetMergerOnly:
    def test_testflight_allows_with_merger_only(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.testflight_internal,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(human=False),
            release_notes={"en-US": "nightly"},
        )
        assert ctx.allow is True
        assert ctx.reason == "allow_internal_merger_only"
        assert ctx.merger_plus_twos == 1
        assert ctx.human_plus_twos == 0

    def test_firebase_internal_skips_release_notes_check(self, signed_artifact_sha):
        # Internal targets aren't in TARGETS_REQUIRING_HUMAN so missing
        # release notes isn't an automatic reject here (they're still
        # validated at the distribute_internal layer if desired).
        ctx = sub.approve_submission(
            target=sub.StoreTarget.firebase_internal,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(human=False),
            release_notes=None,
        )
        assert ctx.allow is True

    def test_internal_still_blocked_without_merger(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.testflight_internal,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(human=False, merger=False),
        )
        assert ctx.allow is False

    def test_internal_still_blocked_with_negative(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.testflight_internal,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(negative=True),
        )
        assert ctx.allow is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit chain integrity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAuditChain:
    def test_chain_verifies_clean(self, signed_artifact_sha):
        for _ in range(3):
            sub.approve_submission(
                target=sub.StoreTarget.app_store_review,
                artifact_sha256=signed_artifact_sha,
                votes=_vote_bundle(),
                release_notes={"en-US": "ship"},
            )
        ok, idx = sub.get_submission_chain().verify()
        assert ok is True
        assert idx is None

    def test_tamper_detected(self, signed_artifact_sha):
        sub.approve_submission(
            target=sub.StoreTarget.app_store_review,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(),
            release_notes={"en-US": "ship"},
        )
        sub.approve_submission(
            target=sub.StoreTarget.app_store_review,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(),
            release_notes={"en-US": "ship2"},
        )
        chain = sub.get_submission_chain()
        # Tamper with row 0 — any later row's verify should flag.
        chain.entries[0]["target"] = "play_production"
        ok, idx = chain.verify()
        assert ok is False
        assert idx == 0

    def test_for_submission_indexing(self, signed_artifact_sha):
        ctx = sub.approve_submission(
            target=sub.StoreTarget.play_production,
            artifact_sha256=signed_artifact_sha,
            votes=_vote_bundle(),
            release_notes={"en-US": "ship"},
        )
        hits = sub.get_submission_chain().for_submission(ctx.submission_id)
        assert len(hits) == 1
        assert hits[0]["target"] == "play_production"
