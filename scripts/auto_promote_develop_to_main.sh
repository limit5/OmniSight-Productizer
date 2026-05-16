#!/usr/bin/env bash
# OP-877 D5 / OP-961 — daily develop -> main auto-promote cron wrapper.
#
# This script is a thin wrapper around the OP-960 (AUDIT-13) rewrite of
# ``backend.agents.auto_promote_main``. It no longer does its own
# ``git push`` or its own ``release_audit`` write — both now live in the
# Python module, which advances ``main`` *through Gerrit Code Review*
# (``develop`` -> ``refs/for/main`` with the ``auto-promote`` +
# ``milestone:R3-fastforward`` hashtags and ``develop-to-main`` topic),
# never a direct push to ``refs/heads/main``.
#
# Behaviour:
#   * Read the most recent ``release_milestone_checker.py`` record from
#     the D1 / OP-868 event log (the acceptance signal for the current
#     fixVersion).
#   * If the latest record is ``milestone_ready`` -- or ``milestone_force_promoted``
#     (ADR-0019 / OP-967 AUDIT-18b operator emergency override; treated as
#     green-equivalent here, the bypassed-gate reasons ride in the record's
#     ``reasons``): delegate to ``backend.agents.auto_promote_main`` which
#     pushes ``develop`` to ``refs/for/main`` (creating one review change
#     per intervening commit) and writes the release audit row.
#   * Otherwise: log + no-op (cron exits 0 — non-fatal).
#
# PromotionResult.status -> cron exit code (the R3 / develop->main step;
# see docs/operations/release-conductor-runbook.md):
#   change_created          -> 0   review change(s) created on refs/for/main
#   noop                    -> 0   main already contains develop
#   milestone_not_accepted  -> 0   latest event is not milestone_ready
#   ignored                 -> 0   no milestone record in the log yet
#   push_rejected           -> 2   Gerrit refused the refs/for/main push
#   non_ff_refused          -> 3   main has commits absent from develop
#   batch_too_large         -> 4   develop too far ahead for one push
#                                  (> receive.maxBatchChanges)
#   (anything else)         -> 1   unexpected — operator alert
#
# Environment overrides (test seam — production cron uses defaults from
# deploy/systemd/auto-promote-develop.service):
#   OP877_REPO              target git repo path     (default: /home/user/sora-bridge)
#   OP877_REMOTE            gerrit remote name        (default: gerrit)
#   OP877_SOURCE_BRANCH     source branch             (default: develop)
#   OP877_TARGET_BRANCH     target branch             (default: main)
#   OP877_EVENT_LOG         release-milestone event log
#   OP877_MAX_PROMOTE_BATCH receive.maxBatchChanges guard (default: 10)
#   OP877_NOW               ISO-8601 UTC timestamp override (logging only)
#   OP961_PKG_ROOT          dir holding the ``backend`` package
#                           (default: this script's repo root)
#
# Forwarded verbatim to the Python process (consumed by the module's
# audit sink / release-conductor hooks): OP877_AUDIT_DB_KIND and every
# RELEASE_CONDUCTOR_* variable. ``python3`` inherits the process
# environment so these propagate automatically; we re-export them so the
# contract is visible at the call site.
set -euo pipefail

REPO="${OP877_REPO:-/home/user/sora-bridge}"
REMOTE="${OP877_REMOTE:-gerrit}"
SOURCE_BRANCH="${OP877_SOURCE_BRANCH:-develop}"
TARGET_BRANCH="${OP877_TARGET_BRANCH:-main}"
EVENT_LOG="${OP877_EVENT_LOG:-/home/user/work/sora/logs/release-milestone/systemd.log}"
MAX_PROMOTE_BATCH="${OP877_MAX_PROMOTE_BATCH:-10}"
NOW="${OP877_NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

# Dir containing the ``backend`` package (this checkout's root). NOT the
# same as OP877_REPO — that is the *target* repo the promotion operates
# on (sora-bridge), which need not contain the agent code.
PKG_ROOT="${OP961_PKG_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

log() { printf '[OP-877] %s %s\n' "${NOW}" "$*"; }

# Re-export the variables the Python side consumes so they survive into
# the child process even if a caller set them as plain shell variables.
export OP877_AUDIT_DB_KIND="${OP877_AUDIT_DB_KIND:-}"
while IFS='=' read -r _name _; do
    case "${_name}" in RELEASE_CONDUCTOR_*) export "${_name}" ;; esac
done < <(env)

# ── Delegate to backend.agents.auto_promote_main ────────────────────
# The inline driver picks the latest milestone record from the event log
# (same selection rule the pre-OP-961 shell used), then calls
# ``promote_on_milestone_ready`` with the module defaults so the module
# owns the refs/for/main push, the operator notification, the telemetry
# event, AND the release audit row. We only read back PromotionResult to
# choose the cron exit code; the line we add is prefixed with the
# ``__OP961_RESULT__`` sentinel so it is unambiguous among the module's
# own JSON event / notify lines.
set +e
out="$(
    cd "${PKG_ROOT}" && PYTHONPATH="${PKG_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 - "${REPO}" "${REMOTE}" "${SOURCE_BRANCH}" "${TARGET_BRANCH}" "${EVENT_LOG}" "${MAX_PROMOTE_BATCH}" <<'PYEOF'
import json
import sys
from pathlib import Path

from backend.agents.auto_promote_main import (
    EVENT_MILESTONE_READY,
    PROMOTE_HASHTAGS,
    PROMOTE_TOPIC,
    promote_on_milestone_ready,
)

repo, remote, source_branch, target_branch, event_log, max_batch = sys.argv[1:7]

# ADR-0019 / OP-967 AUDIT-18b: the operator emergency-override event is a
# green-equivalent for the promote/no-op decision.
EVENT_MILESTONE_FORCE_PROMOTED = "milestone_force_promoted"
GREEN_MILESTONE_EVENTS = (EVENT_MILESTONE_READY, EVENT_MILESTONE_FORCE_PROMOTED)


def _emit(payload: dict) -> None:
    print("__OP961_RESULT__ " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _latest_record(path: Path) -> dict:
    record = {"event": "missing"}
    if not path.is_file():
        return record
    recognized = (*GREEN_MILESTONE_EVENTS, "milestone_blocked")
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        idx = raw.find("{")
        if idx < 0:
            continue
        try:
            parsed = json.loads(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") in recognized:
            record = parsed
    return record


try:
    record = _latest_record(Path(event_log))
    forced = record.get("event") == EVENT_MILESTONE_FORCE_PROMOTED
    if record.get("event") not in GREEN_MILESTONE_EVENTS:
        _emit({
            "status": "milestone_not_accepted",
            "version": str(record.get("fixVersion") or ""),
            "event": record.get("event", "missing"),
        })
        sys.exit(0)

    if forced:
        # ``promote_on_milestone_ready`` only acts on ``milestone_ready``;
        # present the force-promoted record as its green equivalent. The
        # original blocked reasons stay in the record for the audit detail.
        record = {**record, "event": EVENT_MILESTONE_READY}

    result = promote_on_milestone_ready(
        record,
        repo=Path(repo),
        remote=remote,
        source_branch=source_branch,
        target_branch=target_branch,
        max_promote_batch=int(max_batch),
    )
    status = result.status
    if status == "blocked":
        # The module folds two refusals into status="blocked"; split them
        # back out for the exit-code contract.
        status = "non_ff_refused" if result.main_only else "batch_too_large"
    _emit({
        "status": status,
        "version": result.version,
        "detail": result.detail,
        "created_changes": list(result.created_changes),
        "hashtags": list(PROMOTE_HASHTAGS),
        "topic": PROMOTE_TOPIC,
        "operator_override": forced,
    })
except SystemExit:
    raise
except BaseException as exc:  # surface as exit 1 below — never crash the cron silently
    _emit({"status": "error", "detail": f"{type(exc).__name__}: {exc}"})
PYEOF
)"
rc=$?
set -e

# Forward everything the module printed to the journal, then pull our
# decision line back out.
if [ -n "${out}" ]; then printf '%s\n' "${out}"; fi

decision="$(
    printf '%s\n' "${out}" | sed -n 's/^__OP961_RESULT__ //p' | tail -n1 \
        | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); print(json.loads(raw)["status"] if raw else "error")' 2>/dev/null
)" || decision="error"
override="$(
    printf '%s\n' "${out}" | sed -n 's/^__OP961_RESULT__ //p' | tail -n1 \
        | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); print("1" if (raw and json.loads(raw).get("operator_override")) else "0")' 2>/dev/null
)" || override="0"
if [ "${override}" = "1" ]; then
    # ADR-0019 / OP-967: this tick acted on a release:force-promote operator
    # override — the milestone gates were red. Make that loud in the journal.
    log "OPERATOR FORCE-PROMOTE: promotion decided on a milestone_force_promoted event (gates were red); see ADR-0019 / release:force-promote runbook"
fi
if [ "${rc}" -ne 0 ]; then
    case "${decision}" in
        # Python aborted *after* reporting a benign decision — treat as error.
        change_created|noop|milestone_not_accepted|ignored) decision="error" ;;
    esac
fi

case "${decision}" in
    change_created)
        log "Promoted: ${SOURCE_BRANCH} -> refs/for/${TARGET_BRANCH} review change(s) created (operator/merger-bot submits)"
        exit 0 ;;
    noop)
        log "Noop: ${TARGET_BRANCH} already contains ${SOURCE_BRANCH}"
        exit 0 ;;
    milestone_not_accepted)
        log "MilestoneNotAccepted: latest milestone event is not milestone_ready; no-op"
        exit 0 ;;
    ignored)
        log "Ignored: no milestone_ready record in the event log yet; no-op"
        exit 0 ;;
    push_rejected)
        log "MainPushRejected: Gerrit refused the develop -> refs/for/${TARGET_BRANCH} push"
        exit 2 ;;
    non_ff_refused)
        log "GitFFNotPossible: ${TARGET_BRANCH} has commits absent from ${SOURCE_BRANCH}"
        exit 3 ;;
    batch_too_large)
        log "BatchTooLarge: ${SOURCE_BRANCH} is too far ahead of ${TARGET_BRANCH} for one push (> receive.maxBatchChanges)"
        exit 4 ;;
    *)
        log "UnexpectedOutcome: decision='${decision}' rc=${rc}; operator alert"
        exit 1 ;;
esac
