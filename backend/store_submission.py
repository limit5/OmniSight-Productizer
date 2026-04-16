"""P5 (#290) — Store-submission O7 dual-+2 coordinator.

Every binary that leaves the build pipeline for App Store Connect
(``submit_for_review``) or Google Play Production (``submit_to_production``)
passes through **two** independent sign-offs, mirroring the merge
arbiter's dual-+2 rule:

  1. **Merger Agent +2** — verifies the submission artifact is the
     bit-for-bit output of the reviewed Gerrit change (artifact sha256
     matches the codesign audit entry), that mandatory release-note
     fields are set, and that no unreleased dependency slipped in.
  2. **Human +2** — the release manager confirms store-guideline
     compliance (App Store Review Guidelines / Play Policy — handled
     by P6 compliance gates before the human signs).

This module is the *coordinator*: it evaluates the vote bundle against
:mod:`backend.submit_rule` (so the authoritative logic lives in one
place), writes a tamper-evident audit entry into
:class:`backend.codesign_store.CodeSignAuditChain`, and hands the
caller a :class:`StoreSubmissionContext` that the store clients
(:class:`backend.app_store_connect.AppStoreConnectClient`,
:class:`backend.google_play_developer.GooglePlayClient`) require before
they flip any production bit.

Why not just reuse ``evaluate_submit_rule`` directly?
-----------------------------------------------------
The Gerrit evaluator speaks *code-review* votes; store submissions
attach additional metadata (store target, artifact sha256, release
notes, guideline scope).  Wrapping it lets us:

* Enrich the audit record with store-specific context.
* Track submissions by ``submission_id`` for revocation / rollback.
* Reject edge cases Gerrit doesn't — e.g. an artifact the codesign
  chain has never signed (``unknown_artifact``).
* Persist a hash-chained approval log the compliance harness (P6)
  audits without re-deriving votes from Gerrit state.

Public API
----------
``StoreSubmissionContext``
    Immutable handle returned by :func:`approve_submission`.  Carries
    ``allow``, ``reason``, the vote tally, and the audit entry.  This
    is the object the store clients' ``dual_sign_context=`` parameter
    accepts.
``approve_submission(...)``
    Main entry point.  Evaluates the vote list + verifies the artifact
    exists in the codesign audit chain + appends a store-submission
    chain entry.
``get_submission_chain()``
    The global hash-chained audit log of every approve_submission call.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from backend import submit_rule as _sr

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  0. Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class StoreSubmissionError(Exception):
    pass


class StoreTarget(str, enum.Enum):
    app_store_version = "app_store_version"
    app_store_review = "app_store_review"
    play_track_update = "play_track_update"
    play_production = "play_production"
    testflight_internal = "testflight_internal"
    firebase_internal = "firebase_internal"


# Which targets demand the full dual-+2 gate (human AND merger +2).
# Internal distribution targets only need the Merger +2 — the goal is
# to let the app get into QA hands without the release manager chasing
# every nightly build.  Any target shipping to real end users requires
# the full gate.
TARGETS_REQUIRING_HUMAN: frozenset[StoreTarget] = frozenset({
    StoreTarget.app_store_review,
    StoreTarget.play_production,
    StoreTarget.play_track_update,  # beta/alpha rollouts also face users
    StoreTarget.app_store_version,
})


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Context object
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class StoreSubmissionContext:
    """Opaque handle the store clients consume.

    ``allow`` is the single boolean that gates the ASC / Play call.
    Everything else is observability / audit.
    """

    submission_id: str
    target: StoreTarget
    allow: bool
    reason: str
    detail: str
    artifact_sha256: str
    audit_entry: dict[str, Any]
    human_plus_twos: int
    merger_plus_twos: int
    ai_plus_twos: int
    negative_voters: tuple[str, ...]
    release_notes_langs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "target": self.target.value,
            "allow": self.allow,
            "reason": self.reason,
            "detail": self.detail,
            "artifact_sha256": self.artifact_sha256,
            "audit_entry": dict(self.audit_entry),
            "human_plus_twos": self.human_plus_twos,
            "merger_plus_twos": self.merger_plus_twos,
            "ai_plus_twos": self.ai_plus_twos,
            "negative_voters": list(self.negative_voters),
            "release_notes_langs": list(self.release_notes_langs),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Submission audit chain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _chain_hash(prev: str, record: dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canonical(record)).encode("utf-8")).hexdigest()


@dataclass
class StoreSubmissionAuditChain:
    """Hash-chained log of every ``approve_submission`` outcome.

    Mirrors :class:`backend.codesign_store.CodeSignAuditChain` and the
    merger-vote chain — a tamper on row *i* invalidates every
    subsequent ``curr_hash``.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    persist: bool = True

    def append(
        self,
        *,
        submission_id: str,
        target: str,
        artifact_sha256: str,
        allow: bool,
        reason: str,
        voters: Iterable[str],
        ts: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ts_v = float(ts) if ts is not None else time.time()
        record = {
            "submission_id": submission_id,
            "target": target,
            "artifact_sha256": artifact_sha256,
            "allow": allow,
            "reason": reason,
            "voters": list(voters),
            "ts": round(ts_v, 6),
            "extra": dict(extra or {}),
        }
        prev = self.entries[-1]["curr_hash"] if self.entries else ""
        record["prev_hash"] = prev
        record["curr_hash"] = _chain_hash(prev, record)
        self.entries.append(record)
        if self.persist:
            self._fire_audit(record)
        return record

    def verify(self) -> tuple[bool, int | None]:
        prev = ""
        for i, rec in enumerate(self.entries):
            saved_curr = rec.get("curr_hash")
            saved_prev = rec.get("prev_hash")
            payload = {k: v for k, v in rec.items() if k != "curr_hash"}
            payload["prev_hash"] = prev
            recomputed = _chain_hash(prev, payload)
            if saved_prev != prev or saved_curr != recomputed:
                return (False, i)
            prev = saved_curr
        return (True, None)

    def head(self) -> str:
        return self.entries[-1]["curr_hash"] if self.entries else ""

    def for_submission(self, submission_id: str) -> list[dict[str, Any]]:
        return [r for r in self.entries if r["submission_id"] == submission_id]

    def for_artifact(self, artifact_sha256: str) -> list[dict[str, Any]]:
        return [r for r in self.entries if r["artifact_sha256"] == artifact_sha256]

    @staticmethod
    def _fire_audit(record: dict[str, Any]) -> None:
        try:
            from backend import audit
            audit.log_sync(
                action=f"store_submission.{record['reason']}",
                entity_kind="store_submission",
                entity_id=record["submission_id"],
                after=record,
                actor="store-submission-coordinator",
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("store submission audit fire-and-forget failed: %s", exc)


_submission_chain_singleton: StoreSubmissionAuditChain | None = None


def get_submission_chain() -> StoreSubmissionAuditChain:
    global _submission_chain_singleton
    if _submission_chain_singleton is None:
        _submission_chain_singleton = StoreSubmissionAuditChain()
    return _submission_chain_singleton


def reset_submission_chain_for_tests() -> None:
    global _submission_chain_singleton
    _submission_chain_singleton = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Approve entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def approve_submission(
    *,
    target: StoreTarget | str,
    artifact_sha256: str,
    votes: Iterable[_sr.ReviewerVote | dict[str, Any]],
    release_notes: dict[str, str] | None = None,
    artifact_cert_id: str | None = None,
    require_artifact_in_codesign_chain: bool = True,
    codesign_chain: Any | None = None,
    submission_chain: StoreSubmissionAuditChain | None = None,
    ts: float | None = None,
    extra: dict[str, Any] | None = None,
) -> StoreSubmissionContext:
    """Evaluate the dual-+2 gate for a store submission.

    Returns a :class:`StoreSubmissionContext` either way; the caller
    must check ``ctx.allow`` before proceeding.  The store clients
    re-check ``allow`` as a defence-in-depth measure.

    Parameters
    ----------
    target:
        Which store surface is being gated (one of :class:`StoreTarget`
        or its ``.value``).
    artifact_sha256:
        SHA-256 of the ``.ipa`` / ``.aab`` that will be uploaded.
        Must be a 64-hex string.  If
        ``require_artifact_in_codesign_chain`` is True we verify the
        codesign audit chain has at least one entry with this sha — the
        only way to guarantee the submission matches a signed build.
    votes:
        The Code-Review vote list from Gerrit for the change that built
        the artifact (or a synthetic equivalent for GitHub-only flows).
    release_notes:
        ``{lang_tag: text}`` for the store-facing release notes.  Empty
        is a hard fail for human-visible targets.
    artifact_cert_id:
        Optional; when supplied the codesign-chain lookup filters on
        ``cert_id`` too (useful for multi-cert apps).
    require_artifact_in_codesign_chain:
        Set False in tests where the codesign chain isn't wired.
    codesign_chain:
        Inject a specific chain (tests).  Default uses the global one.
    submission_chain:
        Inject a specific submission audit chain (tests).
    """
    target_v = _coerce_target(target)
    if not artifact_sha256 or not _SHA256_HEX.match(artifact_sha256):
        raise StoreSubmissionError(
            f"artifact_sha256 must be 64 lowercase hex chars: {artifact_sha256!r}",
        )

    notes = {k: v for k, v in (release_notes or {}).items() if k and v}
    if target_v in TARGETS_REQUIRING_HUMAN and not notes:
        return _build_and_log(
            target=target_v,
            artifact_sha256=artifact_sha256,
            allow=False,
            reason="reject_missing_release_notes",
            detail=(
                "Release notes required for any human-facing store "
                "target; got none."
            ),
            votes=[],
            notes=notes,
            submission_chain=submission_chain,
            ts=ts,
            extra=extra,
        )

    # ── 1. Codesign-chain artifact existence check ────────────────
    if require_artifact_in_codesign_chain:
        ok, lookup_detail = _artifact_in_codesign_chain(
            artifact_sha256=artifact_sha256,
            cert_id=artifact_cert_id,
            chain=codesign_chain,
        )
        if not ok:
            return _build_and_log(
                target=target_v,
                artifact_sha256=artifact_sha256,
                allow=False,
                reason="reject_unknown_artifact",
                detail=lookup_detail,
                votes=[],
                notes=notes,
                submission_chain=submission_chain,
                ts=ts,
                extra=extra,
            )

    # ── 2. Vote evaluation ────────────────────────────────────────
    decision = _sr.evaluate_submit_rule(votes)

    # ── 3. Target-specific human-gate relaxation ──────────────────
    # For internal distribution targets (TestFlight / Firebase), the
    # human +2 is optional — only the Merger +2 matters.  We reinstate
    # the submit rule's tally but override ``allow`` accordingly.
    effective_allow = decision.allow
    effective_reason = decision.reason.value
    effective_detail = decision.detail

    if (
        not effective_allow
        and decision.negative_votes == 0
        and target_v not in TARGETS_REQUIRING_HUMAN
        and decision.merger_plus_twos >= 1
    ):
        effective_allow = True
        effective_reason = "allow_internal_merger_only"
        effective_detail = (
            f"Internal distribution ({target_v.value}) permitted with "
            f"Merger +2 alone; human gate deferred until store-facing "
            f"submission."
        )

    # ── 4. Log and build context ──────────────────────────────────
    voter_ids: list[str] = []
    for raw in votes:
        if isinstance(raw, _sr.ReviewerVote):
            voter_ids.append(raw.voter)
        elif isinstance(raw, dict):
            voter_ids.append(str(raw.get("voter", "")))

    ctx = _build_and_log(
        target=target_v,
        artifact_sha256=artifact_sha256,
        allow=effective_allow,
        reason=effective_reason,
        detail=effective_detail,
        votes=voter_ids,
        notes=notes,
        submission_chain=submission_chain,
        ts=ts,
        extra=extra,
        human_plus_twos=decision.human_plus_twos,
        merger_plus_twos=decision.merger_plus_twos,
        ai_plus_twos=decision.ai_plus_twos,
        negative_voters=tuple(decision.negative_voters),
    )
    return ctx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _coerce_target(target: StoreTarget | str) -> StoreTarget:
    if isinstance(target, StoreTarget):
        return target
    try:
        return StoreTarget(target)
    except ValueError as exc:
        raise StoreSubmissionError(
            f"unknown target {target!r}; expected one of "
            f"{[t.value for t in StoreTarget]}",
        ) from exc


def _artifact_in_codesign_chain(
    *,
    artifact_sha256: str,
    cert_id: str | None,
    chain: Any | None,
) -> tuple[bool, str]:
    """Return (ok, detail) from the codesign audit chain."""
    if chain is None:
        try:
            from backend import codesign_store
            chain = codesign_store.get_global_audit_chain()
        except Exception as exc:  # pragma: no cover
            return (False, f"codesign chain unavailable: {exc}")
    hits = list(chain.for_artifact(artifact_sha256))
    if cert_id:
        hits = [h for h in hits if h.get("cert_id") == cert_id]
    if not hits:
        return (
            False,
            (
                f"artifact sha256={artifact_sha256[:12]}… has no entry in "
                f"the codesign audit chain — refuse to submit an unsigned "
                f"or un-attested build."
            ),
        )
    return (True, f"{len(hits)} codesign entries vouch for the artifact")


def _build_and_log(
    *,
    target: StoreTarget,
    artifact_sha256: str,
    allow: bool,
    reason: str,
    detail: str,
    votes: list[str],
    notes: dict[str, str],
    submission_chain: StoreSubmissionAuditChain | None,
    ts: float | None,
    extra: dict[str, Any] | None,
    human_plus_twos: int = 0,
    merger_plus_twos: int = 0,
    ai_plus_twos: int = 0,
    negative_voters: tuple[str, ...] = (),
) -> StoreSubmissionContext:
    chain = submission_chain or get_submission_chain()
    submission_id = f"sub-{uuid.uuid4().hex[:16]}"
    audit_entry = chain.append(
        submission_id=submission_id,
        target=target.value,
        artifact_sha256=artifact_sha256,
        allow=allow,
        reason=reason,
        voters=votes,
        ts=ts,
        extra={
            "release_notes_langs": sorted(notes.keys()),
            **(extra or {}),
        },
    )
    return StoreSubmissionContext(
        submission_id=submission_id,
        target=target,
        allow=allow,
        reason=reason,
        detail=detail,
        artifact_sha256=artifact_sha256,
        audit_entry=audit_entry,
        human_plus_twos=human_plus_twos,
        merger_plus_twos=merger_plus_twos,
        ai_plus_twos=ai_plus_twos,
        negative_voters=tuple(negative_voters),
        release_notes_langs=tuple(sorted(notes.keys())),
    )


__all__ = [
    "StoreSubmissionAuditChain",
    "StoreSubmissionContext",
    "StoreSubmissionError",
    "StoreTarget",
    "TARGETS_REQUIRING_HUMAN",
    "approve_submission",
    "get_submission_chain",
    "reset_submission_chain_for_tests",
]
