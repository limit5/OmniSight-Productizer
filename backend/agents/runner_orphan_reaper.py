"""C1 orphan reaper — recover tickets stranded by dead runner hosts (OP-1059).

The AUDIT-24/OP-977 fencing-token mutex prevents two same-instance
runners from claiming the same ticket concurrently, but does NOT recover
a ticket whose claiming runner crashed mid-pickup. The stale-claim
sweep only fires on the *next* claim attempt for the same instance, so a
crashed runner with no successor strands its ticket In Progress forever.

This module is a standalone sweeper, invoked by a 10-minute systemd timer
(``deploy/systemd/runner-orphan-reaper.{service,timer}``):

1. Pull every In-Progress JIRA ticket carrying a ``claim:*`` label.
2. For each fenced ``claim:{instance}:{epoch_us}-{uuid8}`` label, compute
   ``age = now - epoch_us``; skip when ``age < OMNISIGHT_RUNNER_CLAIM_TTL_SEC``
   (default 3600s — twice the standard runner CLI hard timeout).
3. Cross-check ``/tmp/runner-pickup/{INSTANCE}/{EPOCH_US}-{TICKET}/heartbeat``.
   Treat missing file / malformed JSON / missing pid / dead pid as orphan.
4. On orphan: post ``[reaper-detected-orphan]`` comment, call
   :func:`jira_dispatch.release_ticket_claim` to strip the labels, clear
   the assignee, transition In Progress → To Do, and emit a
   ``Severity.WARN`` ``runner_orphan_reaped`` operator alert.

Kill-switch: ``OMNISIGHT_RUNNER_REAPER_ENABLED=0`` exits 0 without
scanning. See ``docs/sop/runbooks/orphan-reaper.md`` for the operator
playbook.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from backend.agents.jira_dispatch import (
    DispatchClient,
    IN_PROGRESS_STATUS_NAMES,
    TRANSITION_IDS,
    _claim_token_epoch_us,
    _parse_claim_label,
    _request,
    _request_idempotent,
    add_comment,
    clear_assignee,
    make_client,
    release_ticket_claim,
)
from backend.agents.operator_notifier import Severity, notify

log = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────

# Claim labels younger than this are presumed live; older claims are
# candidates for the heartbeat cross-check. Defaults to 1h per the SP-B-X-002
# design-doc §C1 — twice the standard CLI hard-timeout window.
_CLAIM_TTL_ENV = "OMNISIGHT_RUNNER_CLAIM_TTL_SEC"
_DEFAULT_CLAIM_TTL_S = 3600

# Kill-switch — when set to ``0`` / ``false`` / ``no``, ``main`` exits 0
# without touching JIRA. Lets operators disable the reaper from the unit
# env file without ``systemctl disable``.
_ENABLED_ENV = "OMNISIGHT_RUNNER_REAPER_ENABLED"

# Heartbeat directory root. Matches the path the runner pickup script
# writes to (``/tmp/runner-pickup/{INSTANCE}/{EPOCH}-{TICKET}/heartbeat``).
# Override-able for tests.
_HEARTBEAT_ROOT = Path("/tmp/runner-pickup")


def _read_claim_ttl_s(env: dict[str, str] | None = None) -> int:
    e = env if env is not None else os.environ
    raw = e.get(_CLAIM_TTL_ENV, "").strip()
    if not raw:
        return _DEFAULT_CLAIM_TTL_S
    try:
        v = int(raw)
    except ValueError:
        log.warning("reaper: %s=%r not an int; using default %ds", _CLAIM_TTL_ENV, raw, _DEFAULT_CLAIM_TTL_S)
        return _DEFAULT_CLAIM_TTL_S
    return v if v > 0 else _DEFAULT_CLAIM_TTL_S


def _reaper_enabled(env: dict[str, str] | None = None) -> bool:
    e = env if env is not None else os.environ
    raw = e.get(_ENABLED_ENV, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


# ── Heartbeat check ───────────────────────────────────────────────────


@dataclass(frozen=True)
class HeartbeatStatus:
    """Outcome of one heartbeat probe. ``alive`` is True iff the file
    exists, parses, and its PID is currently a live process on this host
    (OP-783 single-host invariant — cross-host probes are out of scope)."""

    alive: bool
    reason: str
    pid: int | None = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists, owned by another user — still alive.
        return True
    except OSError:
        return False
    return True


def heartbeat_path(instance_id: str, epoch_us: int, ticket_key: str,
                   root: Path | None = None) -> Path:
    """Construct the well-known heartbeat path for a claim."""
    return (root or _HEARTBEAT_ROOT) / instance_id / f"{epoch_us}-{ticket_key}" / "heartbeat"


def probe_heartbeat(instance_id: str, epoch_us: int, ticket_key: str,
                    root: Path | None = None) -> HeartbeatStatus:
    """Resolve the runner-liveness signal for one claim.

    The heartbeat file is expected to contain JSON of the form
    ``{"pid": <int>, ...}`` written by the runner at pickup. Any
    failure path (missing file, malformed JSON, missing/non-int pid,
    dead pid) returns ``alive=False`` with a descriptive ``reason``.
    """
    path = heartbeat_path(instance_id, epoch_us, ticket_key, root=root)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return HeartbeatStatus(alive=False, reason="heartbeat-file-absent")
    except OSError as exc:
        return HeartbeatStatus(alive=False, reason=f"heartbeat-read-error:{type(exc).__name__}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return HeartbeatStatus(alive=False, reason="heartbeat-malformed-json")

    pid = data.get("pid") if isinstance(data, dict) else None
    if not isinstance(pid, int):
        return HeartbeatStatus(alive=False, reason="heartbeat-missing-pid")

    if not _pid_alive(pid):
        return HeartbeatStatus(alive=False, reason="heartbeat-pid-dead", pid=pid)

    return HeartbeatStatus(alive=True, reason="heartbeat-pid-alive", pid=pid)


# ── Classification ────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrphanCandidate:
    """One TTL-exceeded fenced claim label on an In-Progress ticket. A
    single ticket can stack multiple claim labels (winner + loser tokens
    from same-instance peers); the reaper picks the oldest to drive the
    heartbeat probe, and ``release_ticket_claim`` strips all of that
    instance's labels in one pass."""

    ticket_key: str
    instance_id: str
    token: str
    epoch_us: int
    age_seconds: float


def classify_in_progress_issue(issue: dict, *, now_us: int, ttl_s: int) -> OrphanCandidate | None:
    """Return an :class:`OrphanCandidate` if this ticket has a TTL-exceeded
    fenced claim label, else ``None``. Pre-AUDIT-24 bare ``claim:{instance}``
    labels carry no epoch and are deferred to the next claimer's stale sweep."""
    key = issue.get("key") or "?"
    fields = issue.get("fields") or {}
    labels = list(fields.get("labels") or [])

    oldest: OrphanCandidate | None = None
    for label in labels:
        parsed = _parse_claim_label(label)
        if parsed is None:
            continue
        instance_id, token = parsed
        if token is None:
            # Legacy bare claim — no token, no age signal. Defer to the
            # next claimer's stale sweep.
            continue
        epoch_us = _claim_token_epoch_us(token)
        if epoch_us is None:
            continue
        age_s = (now_us - epoch_us) / 1_000_000
        if age_s <= ttl_s:
            continue
        if oldest is None or epoch_us < oldest.epoch_us:
            oldest = OrphanCandidate(
                ticket_key=key,
                instance_id=instance_id,
                token=token,
                epoch_us=epoch_us,
                age_seconds=age_s,
            )
    return oldest


# ── JQL sweep ─────────────────────────────────────────────────────────

# We pull both English and Japanese-locale status names because JIRA's
# JQL parser matches workflow status by *localised* name (per
# jira-ticket-conventions §10 + the
# ``ストーリー`` issuetype-locale incident handled by AUDIT-27/OP-986).
_IN_PROGRESS_JQL_CLAUSE = " OR ".join(
    f'status = "{name}"' for name in sorted(IN_PROGRESS_STATUS_NAMES)
)


def fetch_in_progress_with_claims(client: DispatchClient, max_results: int = 100) -> list[dict]:
    """Return every In-Progress issue in ``client.project_key`` that
    carries at least one ``claim:*`` label.

    JIRA does not support label wildcards, so we cannot filter
    server-side on ``claim:*``. Instead we pull every In-Progress
    issue and filter locally — volume is bounded by In-Progress
    cardinality, which is small in practice (<20 per project)."""
    jql = f'project = "{client.project_key}" AND ({_IN_PROGRESS_JQL_CLAUSE})'
    resp = _request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["status", "labels", "assignee"],
        "maxResults": max_results,
    })
    out: list[dict] = []
    for issue in resp.get("issues", []):
        labels = ((issue.get("fields") or {}).get("labels")) or []
        if any(isinstance(l, str) and l.startswith("claim:") for l in labels):
            out.append(issue)
    return out


# ── Reap action ───────────────────────────────────────────────────────


def _transition_to_todo_no_assignee(client: DispatchClient, key: str) -> None:
    """Post the transition POST for In Progress → To Do.

    Mirrors :func:`jira_dispatch.transition_back_to_todo`'s transition
    POST without its incident-recording / reason-comment plumbing —
    those are the runner's responsibility on its own revert path. The
    reaper writes its own ``[reaper-detected-orphan]`` comment via
    :func:`add_comment` before calling this.
    """
    _request_idempotent(
        client, "POST", f"/issue/{key}/transitions",
        {"transition": {"id": TRANSITION_IDS["back_to_todo"]}},
        f"reaper-{key}-back-to-todo-{int(time.time())}",
    )


def reap_one(client: DispatchClient, cand: OrphanCandidate,
             hb_status: HeartbeatStatus) -> None:
    """Five-step reap: comment, strip labels, clear assignee, transition
    to To Do, notify. Each step is best-effort — a partial reap leaves
    the ticket in a strictly better state and re-runs next cycle, which
    mirrors :func:`release_ticket_claim`'s own best-effort posture."""
    comment = (
        "[reaper-detected-orphan] Runner claim aged past TTL with no live "
        f"heartbeat — reaping. instance={cand.instance_id} "
        f"token={cand.token} age={cand.age_seconds:.0f}s "
        f"reason={hb_status.reason}"
        + (f" pid={hb_status.pid}" if hb_status.pid is not None else "")
    )
    try:
        add_comment(client, cand.ticket_key, comment)
    except Exception as exc:  # noqa: BLE001 — partial reap > no reap
        log.warning("reaper.add_comment failed key=%s: %s", cand.ticket_key, exc)

    try:
        release_ticket_claim(client, cand.ticket_key, cand.instance_id, token=cand.token)
    except Exception as exc:  # noqa: BLE001 — release_ticket_claim is itself best-effort
        log.warning("reaper.release_ticket_claim failed key=%s: %s", cand.ticket_key, exc)

    try:
        clear_assignee(client, cand.ticket_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("reaper.clear_assignee failed key=%s: %s", cand.ticket_key, exc)

    try:
        _transition_to_todo_no_assignee(client, cand.ticket_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("reaper.transition failed key=%s: %s", cand.ticket_key, exc)

    try:
        notify(
            Severity.WARN,
            "runner_orphan_reaped",
            message=f"Reaped orphan claim on {cand.ticket_key} (TTL exceeded)",
            context={
                "ticket": cand.ticket_key,
                "claim_age_sec": int(cand.age_seconds),
                "instance_id": cand.instance_id,
                "heartbeat_reason": hb_status.reason,
            },
        )
    except Exception as exc:  # noqa: BLE001 — notifier failure must not block reap
        log.warning("reaper.notify failed key=%s: %s", cand.ticket_key, exc)


# ── Top-level cycle ───────────────────────────────────────────────────


@dataclass
class CycleResult:
    """Counts surfaced by :func:`run_cycle` for tests + the unit's log line."""

    scanned: int = 0
    skipped_live: int = 0
    reaped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"scanned": self.scanned, "skipped_live": self.skipped_live, "reaped": self.reaped}


def run_cycle(
    client: DispatchClient,
    *,
    ttl_s: int | None = None,
    now_us: int | None = None,
    heartbeat_root: Path | None = None,
    dry_run: bool = False,
) -> CycleResult:
    """One reaper pass. ``dry_run=True`` classifies + logs but skips the
    JIRA-write side of :func:`reap_one`."""
    ttl = ttl_s if ttl_s is not None else _read_claim_ttl_s()
    now = now_us if now_us is not None else time.time_ns() // 1000
    result = CycleResult()

    issues = fetch_in_progress_with_claims(client)
    result.scanned = len(issues)

    for issue in issues:
        cand = classify_in_progress_issue(issue, now_us=now, ttl_s=ttl)
        if cand is None:
            result.skipped_live += 1
            continue
        hb = probe_heartbeat(
            cand.instance_id, cand.epoch_us, cand.ticket_key, root=heartbeat_root,
        )
        if hb.alive:
            log.info(
                "reaper.skip ticket=%s instance=%s age=%.0fs pid=%s",
                cand.ticket_key, cand.instance_id, cand.age_seconds, hb.pid,
            )
            result.skipped_live += 1
            continue
        log.info(
            "reaper.%s ticket=%s instance=%s age=%.0fs reason=%s",
            "dry_run" if dry_run else "reap",
            cand.ticket_key, cand.instance_id, cand.age_seconds, hb.reason,
        )
        if not dry_run:
            reap_one(client, cand, hb)
        result.reaped += 1

    return result


# ── CLI entrypoint (systemd) ──────────────────────────────────────────


def _agent_class_from_env(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    return e.get("OMNISIGHT_REAPER_AGENT_CLASS", "subscription-claude")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniSight C1 orphan reaper")
    parser.add_argument(
        "--agent-class",
        default=_agent_class_from_env(),
        help="agent_class used to authenticate the JIRA REST client (default: subscription-claude)",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help=f"override {_CLAIM_TTL_ENV} for this run (default: env or {_DEFAULT_CLAIM_TTL_S})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="classify + log but do not write JIRA (no comment, no transition, no notify)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not _reaper_enabled():
        log.info("reaper.disabled %s=0 — exiting without scanning", _ENABLED_ENV)
        return 0

    try:
        client = make_client(args.agent_class)
    except Exception as exc:  # noqa: BLE001 — env / cred misconfig surfaces here
        log.error("reaper.make_client_failed agent_class=%s: %s", args.agent_class, exc)
        return 2

    try:
        result = run_cycle(client, ttl_s=args.ttl_seconds, dry_run=args.dry_run)
    except Exception:  # noqa: BLE001 — surface in logs, exit non-zero for systemd
        log.exception("reaper.cycle_failed")
        return 1

    log.info("reaper.cycle_done %s", json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover — systemd entrypoint
    sys.exit(main())
