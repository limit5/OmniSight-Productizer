"""[OP-939] Release conductor cron skeleton tests.

The shell script is the deliverable, so these tests execute it with
env-injected command hooks instead of live JIRA, Gerrit, systemd, or G6.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_conductor_cron.sh"
SERVICE = REPO_ROOT / "deploy" / "systemd" / "release-conductor-cron.service"
TIMER = REPO_ROOT / "deploy" / "systemd" / "release-conductor-cron.timer"


def _run_cron(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "RELEASE_CONDUCTOR_STATE_DIR": str(tmp_path / "state"),
            "RELEASE_CONDUCTOR_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
            "RELEASE_CONDUCTOR_REPO_ROOT": str(REPO_ROOT),
            "RELEASE_CONDUCTOR_META_EXISTS_CMD": "exit 1",
            "RELEASE_CONDUCTOR_ACCEPTANCE_CMD": "exit 0",
            "RELEASE_CONDUCTOR_INSTANTIATE_CMD": "true",
            # OP-941: keep the hotfix scan deterministic + offline by
            # default (tests opt into a payload via this override).
            "RELEASE_CONDUCTOR_HOTFIX_CHANGES_CMD": "true",
            "RELEASE_CONDUCTOR_INSTANTIATE_HOTFIX_CMD": "true",
        }
    )
    env.update(overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def _audit_rows(path: Path) -> list[dict[str, object]]:
    audit = path / "audit.jsonl"
    if not audit.exists():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines()]


def test_happy_path_instantiates_meta_and_notifies_operator(tmp_path: Path) -> None:
    created = tmp_path / "created.txt"
    notified = tmp_path / "notified.txt"

    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="printf '%s\n' v9.99.0",
        CREATED_FILE=str(created),
        NOTIFIED_FILE=str(notified),
        RELEASE_CONDUCTOR_INSTANTIATE_CMD=(
            'printf "%s\n" "$RELEASE_CONDUCTOR_VERSION" >> "$CREATED_FILE"'
        ),
        RELEASE_CONDUCTOR_NOTIFY_CMD=(
            'printf "%s %s\n" "$RELEASE_CONDUCTOR_EVENT" '
            '"$RELEASE_CONDUCTOR_VERSION" >> "$NOTIFIED_FILE"'
        ),
    )

    assert created.read_text().splitlines() == ["v9.99.0"]
    assert "ReleaseMetaInstantiated v9.99.0" in notified.read_text()
    assert "ExecStart=/bin/bash /home/user/sora-bridge/scripts/release_conductor_cron.sh" in SERVICE.read_text()
    timer_text = TIMER.read_text()
    assert "Unit=release-conductor-cron.service" in timer_text
    assert "OnCalendar=*-*-* 03:00:00 UTC" in timer_text
    assert "Persistent=true" in timer_text


def test_no_fixversions_skip_without_instantiation(tmp_path: Path) -> None:
    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="true",
        RELEASE_CONDUCTOR_INSTANTIATE_CMD="exit 42",
    )

    assert _audit_rows(tmp_path)[0]["event"] == "NoFixVersions"


def test_acceptance_not_ready_skips_and_increments_backoff(tmp_path: Path) -> None:
    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="printf '%s\n' v9.99.0",
        RELEASE_CONDUCTOR_ACCEPTANCE_CMD="exit 1",
        RELEASE_CONDUCTOR_INSTANTIATE_CMD="exit 42",
    )

    rows = _audit_rows(tmp_path)
    assert rows[-1]["event"] == "MilestoneAcceptanceCheckFailed"
    assert rows[-1]["fixVersion"] == "v9.99.0"
    assert (tmp_path / "state" / "v9.99.0.failures").read_text().strip() == "1"


def test_three_consecutive_acceptance_failures_alert_operator(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "v9.99.0.failures").write_text("2\n")
    notified = tmp_path / "notified.txt"

    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="printf '%s\n' v9.99.0",
        RELEASE_CONDUCTOR_ACCEPTANCE_CMD="exit 1",
        RELEASE_CONDUCTOR_INSTANTIATE_CMD="exit 42",
        NOTIFIED_FILE=str(notified),
        RELEASE_CONDUCTOR_NOTIFY_CMD=(
            'printf "%s %s %s\n" "$RELEASE_CONDUCTOR_EVENT" '
            '"$RELEASE_CONDUCTOR_VERSION" "$RELEASE_CONDUCTOR_DETAIL" '
            '>> "$NOTIFIED_FILE"'
        ),
    )

    assert (state / "v9.99.0.failures").read_text().strip() == "3"
    assert "Backoff3Consecutive v9.99.0 suppressed after 3" in notified.read_text()
    assert [row["event"] for row in _audit_rows(tmp_path)][-2:] == [
        "MilestoneAcceptanceCheckFailed",
        "Backoff3Consecutive",
    ]


def test_existing_meta_suppresses_acceptance_and_instantiation(tmp_path: Path) -> None:
    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="printf '%s\n' v9.99.0",
        RELEASE_CONDUCTOR_META_EXISTS_CMD="exit 0",
        RELEASE_CONDUCTOR_ACCEPTANCE_CMD="exit 42",
        RELEASE_CONDUCTOR_INSTANTIATE_CMD="exit 42",
    )

    rows = _audit_rows(tmp_path)
    assert rows[-1]["event"] == "ExistingMetaSuppress"
    assert rows[-1]["fixVersion"] == "v9.99.0"


# ── OP-941 (G5) — hotfix trigger ─────────────────────────────────────


def test_hotfix_labelled_change_instantiates_hotfix_meta(tmp_path: Path) -> None:
    invoked = tmp_path / "hotfix_invoked.txt"
    notified = tmp_path / "notified.txt"

    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="true",
        RELEASE_CONDUCTOR_HOTFIX_CHANGES_CMD="printf '%s\n' '95001 v1.2.4+1'",
        INVOKED_FILE=str(invoked),
        NOTIFIED_FILE=str(notified),
        RELEASE_CONDUCTOR_INSTANTIATE_HOTFIX_CMD=(
            'printf "%s %s\n" "$RELEASE_CONDUCTOR_HOTFIX_CHANGE" '
            '"$RELEASE_CONDUCTOR_HOTFIX_TARGET" >> "$INVOKED_FILE"'
        ),
        RELEASE_CONDUCTOR_NOTIFY_CMD=(
            'printf "%s %s\n" "$RELEASE_CONDUCTOR_EVENT" '
            '"$RELEASE_CONDUCTOR_VERSION" >> "$NOTIFIED_FILE"'
        ),
    )

    assert invoked.read_text().splitlines() == ["95001 v1.2.4+1"]
    assert "HotfixMetaInstantiated v1.2.4+1" in notified.read_text()
    rows = _audit_rows(tmp_path)
    assert rows[-1]["event"] == "HotfixMetaInstantiated"
    assert rows[-1]["fixVersion"] == "v1.2.4+1"


def test_hotfix_malformed_target_is_skipped(tmp_path: Path) -> None:
    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="true",
        RELEASE_CONDUCTOR_HOTFIX_CHANGES_CMD="printf '%s\n' '95001 release/not-semver'",
        RELEASE_CONDUCTOR_INSTANTIATE_HOTFIX_CMD="exit 42",
    )

    rows = _audit_rows(tmp_path)
    assert rows[-1]["event"] == "HotfixTriggerSkipped"


def test_hotfix_instantiate_failure_audited_not_fatal(tmp_path: Path) -> None:
    _run_cron(
        tmp_path,
        RELEASE_CONDUCTOR_FIXVERSIONS_CMD="true",
        RELEASE_CONDUCTOR_HOTFIX_CHANGES_CMD="printf '%s\n' '95001 v1.2.4+1'",
        RELEASE_CONDUCTOR_INSTANTIATE_HOTFIX_CMD="exit 7",
    )

    rows = _audit_rows(tmp_path)
    assert rows[-1]["event"] == "InstantiateHotfixMetaFailed"
    assert rows[-1]["fixVersion"] == "v1.2.4+1"
