"""OP-1168 shadow writes for JIRA claim labels + runner_coordination."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd
from backend.agents import runner_coordination as rc

SCRIPT = REPO_ROOT / "scripts" / "runner-claim-shadow-diff.py"
spec = importlib.util.spec_from_file_location("runner_claim_shadow_diff", SCRIPT)
shadow_diff = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = shadow_diff
spec.loader.exec_module(shadow_diff)


CREATE_RUNNER_CLAIMS = """
CREATE TABLE runner_claims (
    lease_id TEXT PRIMARY KEY,
    ticket_key TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    owner_agent_class TEXT NOT NULL,
    owner_instance_id TEXT NOT NULL,
    fencing_token TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    phase TEXT NOT NULL DEFAULT 'pickup',
    heartbeat_at TEXT,
    acquired_at TEXT,
    released_at TEXT,
    release_reason TEXT,
    external_refs TEXT
);
CREATE UNIQUE INDEX runner_claims_active_resource_idx
ON runner_claims(resource_key)
WHERE state = 'active';
"""


class FakeJira:
    def __init__(self, *, initial_labels: tuple[str, ...] = ()) -> None:
        self._lock = threading.Lock()
        self.assignee: str | None = None
        self.labels: set[str] = set(initial_labels)
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(
        self,
        client: jd.DispatchClient,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> dict:
        with self._lock:
            self.calls.append((method, path, body))
            if method == "GET" and path.startswith("/issue/"):
                return {
                    "fields": {
                        "assignee": (
                            {"accountId": self.assignee} if self.assignee else None
                        ),
                        "labels": sorted(self.labels),
                    }
                }
            if method == "PUT" and path.startswith("/issue/"):
                fields = (body or {}).get("fields") or {}
                if "assignee" in fields:
                    assignee = fields["assignee"]
                    self.assignee = (
                        (assignee or {}).get("accountId") if assignee else None
                    )
                for op in ((body or {}).get("update") or {}).get("labels", []):
                    if "add" in op:
                        self.labels.add(op["add"])
                    if "remove" in op:
                        self.labels.discard(op["remove"])
                return {}
        raise AssertionError(f"unexpected request {method} {path}")


def _fake_client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-codex",
        bot_email="bot@example.invalid",
    )


def _bootstrap_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "omnisight.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(CREATE_RUNNER_CLAIMS)
    conn.close()
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db))
    return db


def _install_fake(monkeypatch, fake: FakeJira) -> None:
    monkeypatch.setattr(jd, "_request", fake.request)
    monkeypatch.setattr(jd, "_CLAIM_READBACK_DELAY_S", 0.0)


def test_shadow_write_creates_table_row_alongside_label(monkeypatch, tmp_path) -> None:
    _bootstrap_db(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNISIGHT_RUNNER_CLAIM_SHADOW", "on")
    fake = FakeJira()
    _install_fake(monkeypatch, fake)

    result = jd.claim_ticket_atomic(_fake_client(), "OP-1168", "default")

    assert result.ok is True
    assert any(label.startswith("claim:default:") for label in fake.labels)
    holders = rc.find_active_holders(resource_keys=["ticket:OP-1168"])
    assert len(holders) == 1
    assert holders[0].phase == "label-claimed"
    assert holders[0].external_refs["label_fencing_token"] == (
        result.claim_token or ""
    ).split(":", 1)[1]


def test_shadow_write_failure_does_not_block_label_write(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_CLAIM_SHADOW", "on")
    fake = FakeJira()
    _install_fake(monkeypatch, fake)

    def fail_acquire(**kwargs):
        raise RuntimeError("table down")

    monkeypatch.setattr(jd.runner_coordination, "acquire_claim", fail_acquire)

    result = jd.claim_ticket_atomic(_fake_client(), "OP-1168", "default")

    assert result.ok is True
    assert result.coordination_lease_id is None
    assert any(label.startswith("claim:default:") for label in fake.labels)


def test_shadow_release_clears_both_label_and_table_row(monkeypatch, tmp_path) -> None:
    _bootstrap_db(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNISIGHT_RUNNER_CLAIM_SHADOW", "on")
    fake = FakeJira()
    _install_fake(monkeypatch, fake)
    client = _fake_client()

    result = jd.claim_ticket_atomic(client, "OP-1168", "default")
    token = (result.claim_token or "").split(":", 1)[1]

    jd.release_ticket_claim(
        client,
        "OP-1168",
        "default",
        token=token,
        coordination_lease_id=result.coordination_lease_id,
        coordination_fencing_token=result.coordination_fencing_token,
    )

    assert not any(label.startswith("claim:default") for label in fake.labels)
    assert rc.find_active_holders(resource_keys=["ticket:OP-1168"]) == []


def test_shadow_diff_script_detects_label_without_table_row() -> None:
    report = shadow_diff.build_report(
        [
            shadow_diff.JiraClaim(
                ticket_key="OP-1168",
                instance_id="default",
                fencing_token="0000000000000001-aaaaaaaa",
                label="claim:default:0000000000000001-aaaaaaaa",
            )
        ],
        [],
        generated_at="2026-05-16T00:00:00+00:00",
    )

    assert report["label_without_table"] == [
        {
            "ticket_key": "OP-1168",
            "label": "claim:default:0000000000000001-aaaaaaaa",
        }
    ]
    assert report["summary"]["label_without_table_count"] == 1
