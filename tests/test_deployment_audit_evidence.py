"""OP-1761 — family ⑤ §6 deployment-audit evidence-file writer.

Exercises the ADDITIVE evidence emitter bolted onto the OP-1753 image-SHA state
machine in ``scripts/deployment-audit.sh``: the §6.2-schema JSON
(``docs/audit/AUDIT-deployment/YYYY-MM-DD.json``), the ``latest.json`` symlink,
and the ``index.json`` of non-OK runs (contract
``docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md`` §6), plus
the §8.4 Discord gate. None of these touch live deployment state — every case
drives the script's documented test-override env into a throwaway evidence dir.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "deployment-audit.sh"
EVIDENCE_UNIT = REPO_ROOT / "deploy" / "systemd" / "deployment-audit-evidence.service"
EVIDENCE_TIMER = REPO_ROOT / "deploy" / "systemd" / "deployment-audit-evidence.timer"

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
NOW = "2026-05-27T12:00:00Z"
TODAY_FILE = "2026-05-27.json"


@pytest.fixture()
def audit_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REAL_SCRIPT, repo / "scripts" / "deployment-audit.sh")
    os.chmod(repo / "scripts" / "deployment-audit.sh", 0o755)
    return repo


def _version_json(image_sha: str) -> str:
    return json.dumps(
        {
            "image_sha": image_sha,
            "build_time": "2026-05-26T00:00:00Z",
            "git_ref": "1" * 40,
            "alembic_head_in_image": "0204",
            "manifest_path": "/app/MANIFEST.json",
        }
    )


def _run(repo: Path, evidence_dir: Path, env_extra: dict[str, str] | None = None):
    manifest = repo / "manifest.tsv"
    manifest.write_text("image-sha\tbackend\tyes\tOP-1753\tfixture\n", encoding="utf-8")
    env = {
        **os.environ,
        "USER": os.environ.get("USER", "tester"),
        "DEPLOYMENT_AUDIT_USER_SYSTEMD": "0",
        "OMNISIGHT_AUDIT_NOW": NOW,
        "OMNISIGHT_AUDIT_EVIDENCE_DIR": str(evidence_dir),
        "OMNISIGHT_AUDIT_T1_VERSION_JSON": _version_json(SHA_A),
        "OMNISIGHT_AUDIT_T2_DIGEST": SHA_A,
        "OMNISIGHT_AUDIT_T2_PUSHED_AT": "2026-05-27T10:00:00Z",
        "OMNISIGHT_AUDIT_T4_IMAGE_SHA": SHA_A,
    }
    env.update(env_extra or {})
    res = subprocess.run(
        [str(repo / "scripts" / "deployment-audit.sh"), str(manifest)],
        capture_output=True,
        text=True,
        env=env,
    )
    return res


def _evidence(evidence_dir: Path) -> dict:
    return json.loads((evidence_dir / TODAY_FILE).read_text(encoding="utf-8"))


# ── Code AC: an audit run emits a §6.2-schema evidence JSON + latest.json ──────
def test_ok_run_emits_schema_v62_evidence_and_latest_symlink(
    audit_repo: Path, tmp_path: Path
) -> None:
    ev = tmp_path / "ev"
    res = _run(audit_repo, ev)
    assert res.returncode == 0, res.stdout

    doc = _evidence(ev)
    # §6.2 top-level keys.
    assert doc["schema_version"] == 1
    assert doc["result_state"] == "OK"
    assert doc["audit_run_at"] == NOW
    assert doc["audit_host"]
    assert doc["audit_script_version"].startswith("scripts/deployment-audit.sh@")
    assert set(doc) >= {
        "truth_sources",
        "comparisons",
        "ghcr_query_evidence",
        "alerts_emitted",
    }
    # truth_sources carries all four axes (T3 is a documented out-of-area stub).
    ts = doc["truth_sources"]
    assert set(ts) == {
        "T1_running_version",
        "T2_ghcr_latest",
        "T3_db_alembic_version",
        "T4_image_manifest",
    }
    assert ts["T1_running_version"]["image_sha"] == SHA_A
    assert ts["T1_running_version"]["fetch_latency_ms"] is not None
    assert doc["comparisons"]["T1_vs_T2_image_sha_match"] is True
    assert doc["comparisons"]["T1_image_sha_vs_T4_integrity"] is True
    assert doc["alerts_emitted"] == []

    # latest.json symlink resolves to today's file (§6.5).
    latest = ev / "latest.json"
    assert latest.is_symlink() or latest.exists()
    assert json.loads(latest.read_text(encoding="utf-8"))["result_state"] == "OK"
    # OK runs are not listed in the non-OK incident index (§6.5).
    index = json.loads((ev / "index.json").read_text(encoding="utf-8"))
    assert index["non_ok_runs"] == []


@pytest.mark.parametrize(
    ("pushed_at", "state", "alert", "rc"),
    [
        ("2026-05-27T11:00:00Z", "WARN_STALE_IMAGE", "OmniSightStaleImage", 0),
        ("2026-05-24T11:00:00Z", "PAGE_STALE_IMAGE", "OmniSightStaleImage", 1),
    ],
)
def test_drift_run_records_state_and_alert_and_indexes_it(
    audit_repo: Path, tmp_path: Path, pushed_at: str, state: str, alert: str, rc: int
) -> None:
    ev = tmp_path / "ev"
    res = _run(
        audit_repo,
        ev,
        {"OMNISIGHT_AUDIT_T2_DIGEST": SHA_B, "OMNISIGHT_AUDIT_T2_PUSHED_AT": pushed_at},
    )
    assert res.returncode == rc, res.stdout
    doc = _evidence(ev)
    assert doc["result_state"] == state
    assert doc["comparisons"]["T1_vs_T2_image_sha_match"] is False
    assert any(a["alert"] == alert for a in doc["alerts_emitted"])
    # Non-OK runs are recorded in the incident index (§6.5).
    index = json.loads((ev / "index.json").read_text(encoding="utf-8"))
    assert index["non_ok_runs"] == [
        {"date": "2026-05-27", "result_state": state, "alerts": [alert]}
    ]


def test_integrity_finding_is_the_headline_when_combined_with_stale(
    audit_repo: Path, tmp_path: Path
) -> None:
    ev = tmp_path / "ev"
    res = _run(
        audit_repo,
        ev,
        {
            "OMNISIGHT_AUDIT_T2_DIGEST": SHA_B,
            "OMNISIGHT_AUDIT_T2_PUSHED_AT": "2026-05-24T11:00:00Z",
            "OMNISIGHT_AUDIT_T4_IMAGE_SHA": SHA_C,
        },
    )
    assert res.returncode == 1, res.stdout
    doc = _evidence(ev)
    # PAGE_INTEGRITY outranks PAGE_STALE_IMAGE for the headline result_state.
    assert doc["result_state"] == "PAGE_INTEGRITY"
    assert doc["comparisons"]["T1_image_sha_vs_T4_integrity"] is False
    names = {a["alert"] for a in doc["alerts_emitted"]}
    assert names == {"OmniSightImageIntegrity", "OmniSightStaleImage"}


# ── Integration AC: INCOMPLETE writes evidence but fires NO stale-image alert ──
def test_incomplete_run_writes_evidence_without_stale_alert(
    audit_repo: Path, tmp_path: Path
) -> None:
    ev = tmp_path / "ev"
    res = _run(
        audit_repo,
        ev,
        {"OMNISIGHT_AUDIT_T1_VERSION_JSON": "{}", "OMNISIGHT_AUDIT_T2_DIGEST": SHA_B},
    )
    assert res.returncode == 0, res.stdout
    doc = _evidence(ev)
    assert doc["result_state"] == "INCOMPLETE"
    names = {a["alert"] for a in doc["alerts_emitted"]}
    assert "OmniSightStaleImage" not in names  # §6.4 false-positive avoidance
    assert "OmniSightAuditIncomplete" in names
    # The failing source carries its error sub-field (§6.3).
    assert doc["truth_sources"]["T1_running_version"].get("error")


# ── §8.4 Discord gate ─────────────────────────────────────────────────────────
def test_discord_gate_is_off_by_default(audit_repo: Path, tmp_path: Path) -> None:
    res = _run(audit_repo, tmp_path / "ev")
    assert "OMNISIGHT_FAMILY5_DISCORD_ENABLED=0" in res.stderr
    assert "not posting" in res.stderr


def test_discord_enabled_consumes_the_31c_client_on_drift(
    audit_repo: Path, tmp_path: Path
) -> None:
    sentinel = tmp_path / "discord_called.txt"
    client = tmp_path / "discord_client.sh"
    client.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > " + f'"{sentinel}"\n',
        encoding="utf-8",
    )
    os.chmod(client, 0o755)
    ev = tmp_path / "ev"
    res = _run(
        audit_repo,
        ev,
        {
            "OMNISIGHT_FAMILY5_DISCORD_ENABLED": "1",
            "OMNISIGHT_FAMILY5_DISCORD_CLIENT": str(client),
            "OMNISIGHT_AUDIT_T2_DIGEST": SHA_B,
            "OMNISIGHT_AUDIT_T2_PUSHED_AT": "2026-05-24T11:00:00Z",
        },
    )
    assert res.returncode == 1, res.stdout  # PAGE_STALE_IMAGE
    assert sentinel.exists(), res.stderr
    args = sentinel.read_text(encoding="utf-8")
    assert "--state PAGE_STALE_IMAGE" in args
    assert str(ev / TODAY_FILE) in args


def test_discord_enabled_does_not_post_on_ok(audit_repo: Path, tmp_path: Path) -> None:
    sentinel = tmp_path / "discord_called.txt"
    client = tmp_path / "discord_client.sh"
    client.write_text(
        "#!/usr/bin/env bash\ntouch " + f'"{sentinel}"\n', encoding="utf-8"
    )
    os.chmod(client, 0o755)
    res = _run(
        audit_repo,
        tmp_path / "ev",
        {
            "OMNISIGHT_FAMILY5_DISCORD_ENABLED": "1",
            "OMNISIGHT_FAMILY5_DISCORD_CLIENT": str(client),
        },
    )
    assert res.returncode == 0, res.stdout  # OK
    assert not sentinel.exists()  # §5.4: OK never posts


# ── Deploy AC: the new timer/service units are well-formed ────────────────────
def test_evidence_units_exist_and_are_distinct_from_op1016_baseline() -> None:
    svc = EVIDENCE_UNIT.read_text(encoding="utf-8")
    timer = EVIDENCE_TIMER.read_text(encoding="utf-8")
    assert "deployment-audit.sh" in svc
    assert "OMNISIGHT_FAMILY5_DISCORD_ENABLED=0" in svc  # gated off by default
    assert "Unit=deployment-audit-evidence.service" in timer
    assert "02:00:00" in timer  # distinct from the OP-1016 07:00 baseline


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze not available"
)
def test_evidence_units_pass_systemd_analyze_verify() -> None:
    for unit in (EVIDENCE_UNIT, EVIDENCE_TIMER):
        res = subprocess.run(
            ["systemd-analyze", "verify", str(unit)],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"{unit.name}: {res.stderr}"
