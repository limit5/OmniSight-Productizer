#!/usr/bin/env python3
"""[OP-725] Hourly canary — exercises Gerrit -> bridge daemon end-to-end.

Why this exists
---------------
OP-708 burned us: a synthetic ``curl`` POST to the merger webhook
returned 200 even though Gerrit's ``webhooks`` plugin v3.13.5 cannot
deliver the same payload (no auth-header surface). The synthetic check
masked a real silent-failure path for ~2 days. A canary that runs the
*real* path — push -> stream-events -> daemon -> structured log — closes
that gap because it can only succeed if every link works.

Strict scope: this script is owned by ``area:devops``. It speaks only:
  * git CLI (push to ``refs/for/develop``)
  * SSH CLI (``gerrit review --abandon``)
  * filesystem reads of the bridge's structured-log file

It MUST NOT import from ``backend/`` — those are the components under
test. Importing them would void the canary (a backend regression could
break both the daemon AND the canary's import path, and we'd see a
PASS that's actually a NameError swallowed somewhere else).

Pollution controls (AC #3)
--------------------------
* Hashtags ``canary,wip,private`` are pushed with the change so it
  never appears in the human reviewer queue.
* Commit subject deliberately contains NO ``[OP-NNN]`` key, so the
  bridge's JIRA dispatch path skips it (event is logged as
  ``no_op_keys_in_subject`` and no comment/transition fires on any
  real ticket).
* The change is abandoned in a ``finally`` block — even if the log
  poll fails, the cleanup still runs.
* The temporary working tree under ``CANARY_WORKDIR`` is reused
  across runs (one branch per run, fast-forwarded from develop).

Exit codes
----------
``0``  full pipeline verified — daemon log shows the expected event
       within the timeout, change abandoned cleanly.
``2``  DEGRADED — push or daemon-log-poll failed. Exit code is
       non-zero so systemd's ``OnFailure=`` chain fires the T1
       alerter (``omnisight-canary-alert.service``).
``3``  configuration / environment error — refuse to run.

The script also writes a single structured JSON line to stdout
describing the run (level / event / detail), which is what the T1
alerter tails. Format mirrors ``backend.agents.gerrit_jira_bridge
.structured_log`` so existing journal parsers work unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── log/structured signal ───────────────────────────────────────────

CANARY_EVENT_OK = "canary_pipeline_check_ok"
CANARY_EVENT_DEGRADED = "canary_pipeline_check_failed"

# Bridge events the canary expects to observe within TIMEOUT seconds.
# ``proactive_merger_thread_spawned`` is the cheapest definitive proof
# that the stream-events SSH consumer received a Gerrit event and
# dispatched it to the proactive-merger code path — it fires for every
# patchset-created event the daemon ingests.
EXPECTED_BRIDGE_EVENT = "proactive_merger_thread_spawned"

# Default poll budget. The bridge typically fires the structured log
# within 1-3 s of a ``ref-updated``/``patchset-created`` stream event;
# 60 s gives a generous margin even on a loaded host.
DEFAULT_TIMEOUT_SECONDS = 60

# Default poll cadence — ~10 ticks per minute is enough granularity
# for a stream-events daemon and keeps fs reads cheap.
POLL_INTERVAL_SECONDS = 5


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_status(level: str, event: str, **extra: object) -> None:
    """Mirror ``backend.agents.gerrit_jira_bridge.structured_log``.

    The T1 alerter tails this stream by event name, so the field
    layout (``timestamp``, ``level``, ``event``) MUST stay identical.
    """
    record: dict[str, object] = {
        "timestamp": utc_now_iso(),
        "level": level,
        "event": event,
    }
    record.update(extra)
    print(json.dumps(record, sort_keys=True), flush=True)


# ─── pure helpers (testable without git / ssh / fs) ──────────────────


def build_canary_subject(now_iso: str) -> str:
    """Synthesize a unique commit subject for one canary run.

    The ``[CANARY]`` prefix is deliberately NOT a JIRA key so the
    bridge's ``extract_ticket_keys_from_subject`` returns empty and
    no JIRA mutation fires (AC #3 — no JIRA pollution).

    The ISO-second timestamp guarantees Gerrit treats each push as a
    new change (no Change-Id collision), even if a previous canary
    aborted before abandon ran.
    """
    return f"[CANARY] hourly synthetic event {now_iso}"


# ``remote: <https://host/c/project/+/12345 [stuff]>`` — Gerrit prints
# this on every ``git push gerrit HEAD:refs/for/...`` for a brand new
# change. The number is the only stable identifier we get back at push
# time (Change-Id requires extra parsing of stdout / commit-msg hook).
PUSH_NEW_CHANGE_RE = re.compile(r"remote:\s+\S+/\+/(\d+)\b")


def parse_change_number_from_push_stderr(stderr: str) -> str | None:
    """Extract the Gerrit change number from ``git push`` stderr.

    Returns ``None`` if the push didn't create a change (e.g. Gerrit
    rejected the push, or the push was a no-op fast-forward — which
    means our canary's commit was identical to a prior one, which
    means the timestamp seed broke).
    """
    m = PUSH_NEW_CHANGE_RE.search(stderr)
    return m.group(1) if m else None


def parse_log_for_event(
    lines: list[str], event: str, change_number: str
) -> dict | None:
    """Search structured-log lines for ``event`` matching ``change_number``.

    Each line is expected to be JSON (the bridge's ``structured_log``
    format). Lines that don't parse, lack the right ``event``, or
    have a different ``change_id`` are skipped without raising — we
    only care about the affirmative signal.

    Returns the matched record, or ``None``.
    """
    for raw in lines:
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("event") != event:
            continue
        # Bridge stores change_id as the numeric change number string.
        if str(rec.get("change_id") or "") == str(change_number):
            return rec
    return None


def build_abandon_argv(
    ssh_host: str, ssh_port: int, ssh_key: str, change_number: str
) -> list[str]:
    """Build the ``ssh ... gerrit review --abandon`` argv list.

    Kept as a pure function so tests can pin the exact flags without
    actually invoking SSH. ``--message`` is not strictly required by
    Gerrit but leaves a paper trail in the change history matching
    the canary's structured log so an operator grepping change
    history sees ``[CANARY]`` rather than a mute abandon.
    """
    return [
        "ssh",
        "-i",
        ssh_key,
        "-p",
        str(ssh_port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        ssh_host,
        "gerrit",
        "review",
        "--abandon",
        "--message",
        "[CANARY] auto-abandon after pipeline check",
        f"{change_number},1",
    ]


# ─── filesystem / subprocess (the side-effecting layer) ──────────────


def read_log_tail_since(log_path: Path, start_offset: int) -> list[str]:
    """Read lines appended to ``log_path`` after ``start_offset``.

    Uses byte offsets rather than line counts because the bridge can
    emit several events per Gerrit stream batch and we don't want to
    re-read the whole journal-style log on every poll tick.
    """
    if not log_path.exists():
        return []
    with log_path.open("rb") as fh:
        size = log_path.stat().st_size
        if size < start_offset:
            # Log was rotated out from under us — start over.
            start_offset = 0
        fh.seek(start_offset)
        chunk = fh.read()
    try:
        text = chunk.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return []
    return text.splitlines()


def snapshot_log_offset(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    return log_path.stat().st_size


def poll_for_event(
    log_path: Path,
    start_offset: int,
    event: str,
    change_number: str,
    timeout_seconds: int,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    sleep: object = time.sleep,
    monotonic: object = time.monotonic,
) -> dict | None:
    """Poll the bridge log for ``event`` referencing ``change_number``.

    ``sleep`` and ``monotonic`` are injected so unit tests can drive
    the loop without real-time delay.
    """
    deadline = monotonic() + timeout_seconds  # type: ignore[operator]
    while True:
        lines = read_log_tail_since(log_path, start_offset)
        hit = parse_log_for_event(lines, event, change_number)
        if hit is not None:
            return hit
        if monotonic() >= deadline:  # type: ignore[operator]
            return None
        sleep(poll_interval_seconds)  # type: ignore[misc]


def ensure_workdir(workdir: Path, gerrit_url: str) -> None:
    """Ensure ``workdir`` is a clone of the Gerrit project on develop.

    Performs a fresh clone the first time, then ``git fetch`` +
    ``git reset --hard origin/develop`` on subsequent runs so the
    canary always starts from a clean develop tip and never inherits
    cruft from a prior run.
    """
    if not (workdir / ".git").exists():
        workdir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", gerrit_url, str(workdir)],
            check=True,
            capture_output=True,
        )
        return
    subprocess.run(
        ["git", "-C", str(workdir), "fetch", "origin", "develop"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workdir), "reset", "--hard", "origin/develop"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workdir), "clean", "-fdx"],
        check=True,
        capture_output=True,
    )


def stage_canary_commit(workdir: Path, subject: str) -> None:
    """Touch ``canary/heartbeat.txt`` and commit it under ``subject``.

    The file lives at ``canary/`` rather than the repo root so a
    human glancing at ``ls`` sees the convention and doesn't get
    confused by a top-level ``heartbeat.txt``. The directory is
    created if missing.
    """
    canary_dir = workdir / "canary"
    canary_dir.mkdir(parents=True, exist_ok=True)
    (canary_dir / "heartbeat.txt").write_text(f"{subject}\n")
    subprocess.run(
        ["git", "-C", str(workdir), "add", "canary/heartbeat.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workdir),
            "commit",
            "-m",
            subject,
            "--allow-empty",
        ],
        check=True,
        capture_output=True,
    )


def push_canary_for_review(workdir: Path, target_branch: str) -> str:
    """Push HEAD to ``refs/for/<branch>%hashtag=canary,wip,private``.

    Returns the new Gerrit change number on success, raises
    ``RuntimeError`` otherwise. ``wip`` + ``private`` are Gerrit
    push-options that hide the change from the review queue (AC #3
    pollution control). ``hashtag=canary`` lets ops grep for stragglers
    if the abandon step ever fails.
    """
    refspec = (
        f"HEAD:refs/for/{target_branch}"
        "%hashtag=canary,wip,private,topic=canary"
    )
    proc = subprocess.run(
        ["git", "-C", str(workdir), "push", "origin", refspec],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git push failed: rc={proc.returncode} "
            f"stderr={proc.stderr[-500:]!r}"
        )
    change_number = parse_change_number_from_push_stderr(proc.stderr)
    if change_number is None:
        raise RuntimeError(
            "git push succeeded but no change number in stderr; "
            f"stderr tail: {proc.stderr[-500:]!r}"
        )
    return change_number


def abandon_change(
    ssh_host: str,
    ssh_port: int,
    ssh_key: str,
    change_number: str,
) -> bool:
    """Abandon ``change_number,1`` via Gerrit SSH. Best-effort.

    Returns ``True`` on a clean abandon, ``False`` otherwise. The
    canary deliberately does NOT raise on abandon failure: a stranded
    change is annoying but does not invalidate the upstream check, and
    we still want the structured-log status to reflect the actual
    pipeline result rather than the cleanup result.
    """
    argv = build_abandon_argv(ssh_host, ssh_port, ssh_key, change_number)
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode == 0


# ─── orchestrator ────────────────────────────────────────────────────


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="canary_pipeline",
        description=(
            "OP-725 hourly canary — pushes a no-op WIP+private change "
            "to Gerrit and verifies the bridge daemon processed it."
        ),
    )
    p.add_argument(
        "--bridge-log",
        type=Path,
        default=Path("/home/user/work/sora/logs/bridge/systemd.log"),
        help="path to the gerrit-jira-bridge structured-log file",
    )
    p.add_argument(
        "--workdir",
        type=Path,
        default=Path.home() / ".cache" / "omnisight" / "canary-workdir",
        help="local clone path the canary uses to build its commit",
    )
    p.add_argument(
        "--gerrit-url",
        default=os.environ.get(
            "OMNISIGHT_CANARY_GERRIT_URL",
            "ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer",
        ),
        help="Gerrit project clone URL",
    )
    p.add_argument(
        "--ssh-host",
        default=os.environ.get(
            "OMNISIGHT_GERRIT_SSH_HOST", "claude-bot@sora.services"
        ),
        help="ssh host:user for `gerrit review --abandon`",
    )
    p.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "29418")),
    )
    p.add_argument(
        "--ssh-key",
        default=os.environ.get(
            "OMNISIGHT_GIT_SSH_KEY_PATH",
            str(
                Path.home() / ".config" / "omnisight"
                / "gerrit-claude-bot-ed25519"
            ),
        ),
    )
    p.add_argument("--target-branch", default="develop")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument(
        "--no-cleanup",
        action="store_true",
        help="skip abandon (debugging only — leaves a stranded change)",
    )
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    subject = build_canary_subject(utc_now_iso())
    log_offset_before = snapshot_log_offset(args.bridge_log)

    try:
        ensure_workdir(args.workdir, args.gerrit_url)
        stage_canary_commit(args.workdir, subject)
        change_number = push_canary_for_review(
            args.workdir, args.target_branch
        )
    except Exception as exc:
        emit_status(
            "DEGRADED",
            CANARY_EVENT_DEGRADED,
            stage="push",
            err=f"{type(exc).__name__}: {exc}",
        )
        return 2

    matched = poll_for_event(
        args.bridge_log,
        log_offset_before,
        EXPECTED_BRIDGE_EVENT,
        change_number,
        timeout_seconds=args.timeout,
    )

    abandoned = True
    if not args.no_cleanup:
        try:
            abandoned = abandon_change(
                args.ssh_host,
                args.ssh_port,
                args.ssh_key,
                change_number,
            )
        except Exception:
            abandoned = False

    if matched is None:
        emit_status(
            "DEGRADED",
            CANARY_EVENT_DEGRADED,
            stage="poll",
            change_number=change_number,
            timeout_seconds=args.timeout,
            expected_event=EXPECTED_BRIDGE_EVENT,
            abandoned=abandoned,
        )
        return 2

    emit_status(
        "INFO",
        CANARY_EVENT_OK,
        change_number=change_number,
        bridge_event=EXPECTED_BRIDGE_EVENT,
        abandoned=abandoned,
    )
    return 0 if abandoned else 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.ssh_key or not Path(args.ssh_key).expanduser().exists():
        emit_status(
            "ERROR",
            CANARY_EVENT_DEGRADED,
            stage="config",
            err=f"ssh key not readable at {args.ssh_key!r}",
        )
        return 3
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
