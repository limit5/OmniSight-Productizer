"""Edge-case regressions for release dashboard projections."""
from __future__ import annotations

from pathlib import Path

from backend import deploy_audit
from backend import production_release as pr
from backend import release_dashboard as rd


def test_snapshot_skips_malformed_approval_file(tmp_path: Path) -> None:
    pr.save_approval(
        pr.ReleaseApproval(
            tag="v1.4.0",
            status="deploying",
            deploy_started_at="2026-05-17T10:00:00+00:00",
        ),
        tmp_path,
    )
    (tmp_path / "corrupt.json").write_text("{not-json", encoding="utf-8")

    body = rd.snapshot(store_dir=tmp_path, audit_query=lambda **_kwargs: [])

    assert body["current_prod_tag"] is None
    assert body["in_flight"] == [
        {
            "tag": "v1.4.0",
            "status": "deploying",
            "progress_percent": 50,
            "started_at": "2026-05-17T10:00:00+00:00",
            "approved_by": "",
        }
    ]
    assert body["milestones"] == [
        {"name": "staging_smoke", "state": "yellow"},
        {"name": "baseline_diff", "state": "green"},
        {"name": "operator_approval", "state": "yellow"},
    ]


def test_audit_summary_handles_missing_actor_tag_and_short_timestamp() -> None:
    rows = [
        {
            "id": 1,
            "ts": "bad",
            "kind": deploy_audit.KIND_DEPLOY,
            "tag": "",
            "actor": "",
            "status": deploy_audit.STATUS_FAILED,
        },
        {
            "id": 2,
            "ts": "",
            "kind": deploy_audit.KIND_ROLLBACK,
            "tag": None,
            "actor": None,
            "status": deploy_audit.STATUS_SUCCEEDED,
        },
    ]

    history = rd.release_history(audit_query=lambda **_kwargs: rows)
    audit_rows = rd.audit_rows(audit_query=lambda **_kwargs: rows)

    assert [row["summary"] for row in history] == [
        "system deploy unknown tag failed at bad",
        "system rolled back unknown tag at ",
    ]
    assert [row["message"] for row in audit_rows] == [
        "system deploy unknown tag failed at bad",
        "system rolled back unknown tag at ",
    ]
