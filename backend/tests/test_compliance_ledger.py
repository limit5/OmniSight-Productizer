"""OP-952 H7 — release compliance ledger contract tests."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from backend.release_conductor import compliance_ledger, state_machine
from backend.release_conductor.event_handlers import hotfix_label


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
MIGRATION_0232 = BACKEND_ROOT / "alembic" / "versions" / "0232_release_events.py"
MIGRATION_0233 = BACKEND_ROOT / "alembic" / "versions" / "0233_release_state.py"
MIGRATION_0234 = BACKEND_ROOT / "alembic" / "versions" / "0234_release_compliance_ledger.py"
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_compliance_ledger.py"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_compliance_chain.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def ledger_engine():
    """In-memory sqlite engine with H3/H2/H7 schema applied."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy.pool import StaticPool

    engine = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mig233 = _load_module(MIGRATION_0233, "_alembic_test_0233_for_op952")
    mig232 = _load_module(MIGRATION_0232, "_alembic_test_0232_for_op952")
    mig234 = _load_module(MIGRATION_0234, "_alembic_test_0234_for_op952")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            mig233.upgrade()
            mig232.upgrade()
            mig234.upgrade()
        conn.commit()

    state_machine.set_engine_for_tests(engine)
    compliance_ledger.set_engine_for_tests(engine)
    try:
        yield engine
    finally:
        state_machine.set_engine_for_tests(None)
        compliance_ledger.set_engine_for_tests(None)
        engine.dispose()


def test_write_happy_path_records_required_columns(ledger_engine) -> None:
    row = compliance_ledger.record(
        actor="alice@example.com",
        action="state.transition",
        release_id="OP-952",
        before_state="pending",
        after_state="building",
        reason="build started",
        evidence={"source": "test"},
    )

    assert row is not None
    stored = compliance_ledger.list_rows()
    assert len(stored) == 1
    assert stored[0]["actor"] == "alice@example.com"
    assert stored[0]["action"] == "state.transition"
    assert stored[0]["release_id"] == "OP-952"
    assert stored[0]["before_state"] == "pending"
    assert stored[0]["after_state"] == "building"
    assert len(stored[0]["evidence_hash"]) == 64


def test_update_blocked_by_append_only_trigger(ledger_engine) -> None:
    compliance_ledger.record(
        actor="alice",
        action="state.transition",
        release_id="OP-952",
        before_state="pending",
        after_state="building",
        reason="r",
        evidence={},
    )

    with pytest.raises(sa.exc.DBAPIError, match="LedgerTriggerBypassed"):
        with ledger_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE release_compliance_ledger SET reason = 'edited' WHERE id = 1"
                )
            )


def test_delete_blocked_by_append_only_trigger(ledger_engine) -> None:
    compliance_ledger.record(
        actor="alice",
        action="state.transition",
        release_id="OP-952",
        before_state="pending",
        after_state="building",
        reason="r",
        evidence={},
    )

    with pytest.raises(sa.exc.DBAPIError, match="LedgerTriggerBypassed"):
        with ledger_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM release_compliance_ledger WHERE id = 1"))


def test_state_transition_operator_approval_and_hotfix_write_rows(ledger_engine) -> None:
    state_machine.create(release_id="OP-1000", version="v1.0.0")
    state_machine.transition(
        release_id="OP-1000",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="build started",
    )
    state_machine.request_approval(release_id="OP-1000", canary_percent=5)
    state_machine.record_decision(
        release_id="OP-1000",
        decision="approve",
        operator="operator@example.com",
        reason="ship it",
    )

    comments: list[str] = []
    hotfix_label.on_hotfix_label_added(
        {
            "change": {"number": 95001},
            "approval": {
                "type": "hotfix:cherry-pick-to=release/v1.2.4+1"
            },
        },
        cherry_pick=lambda _change, _target: {"cherry_picked_change": "95002"},
        gerrit_comment=lambda _change, message: comments.append(message),
        meta_instantiator=lambda target, _picked: {
            "meta_key": f"HOTFIX-{target.removeprefix('release/')}"
        },
    )

    rows = compliance_ledger.list_rows()
    assert [row["action"] for row in rows] == [
        "state.transition",
        "operator.approval_granted",
        "hotfix.trigger",
    ]
    assert rows[0]["before_state"] == "pending"
    assert rows[0]["after_state"] == "building"
    assert rows[1]["actor"] == "operator@example.com"
    assert rows[2]["after_state"] == "hotfix_triggered"


def test_chain_hash_verifies(ledger_engine) -> None:
    first = compliance_ledger.record(
        actor="a",
        action="one",
        release_id="OP-1",
        before_state=None,
        after_state="pending",
        reason="created",
        evidence={"n": 1},
    )
    second = compliance_ledger.record(
        actor="a",
        action="two",
        release_id="OP-1",
        before_state="pending",
        after_state="building",
        reason="advanced",
        evidence={"n": 2},
    )

    assert first is not None and second is not None
    assert second["prev_row_hash"] == first["row_hash"]
    assert compliance_ledger.verify_chain() is True


def test_broken_chain_detected(ledger_engine) -> None:
    row = compliance_ledger.record(
        actor="a",
        action="one",
        release_id="OP-1",
        before_state=None,
        after_state="pending",
        reason="created",
        evidence={"n": 1},
    )
    assert row is not None
    broken = [dict(row)]
    broken[0]["reason"] = "tampered"

    with pytest.raises(compliance_ledger.ChainBroken, match="row_hash mismatch"):
        compliance_ledger.verify_chain(broken)


def test_daily_export_script_writes_pdf_and_signed_csv(
    ledger_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    compliance_ledger.record(
        actor="a",
        action="one",
        release_id="OP-1",
        before_state=None,
        after_state="pending",
        reason="created",
        evidence={"n": 1},
    )
    monkeypatch.setenv("OMNISIGHT_COMPLIANCE_LEDGER_SIGNING_KEY", "test-secret")
    script = _load_module(EXPORT_SCRIPT, "_export_compliance_ledger_under_test")

    rc = script.main(["--since=2000-01-01", f"--output-dir={tmp_path}"])

    assert rc == 0
    csv_path = tmp_path / "release-compliance-ledger-2000-01-01.csv"
    sig_path = tmp_path / "release-compliance-ledger-2000-01-01.csv.sig"
    pdf_path = tmp_path / "release-compliance-ledger-2000-01-01.pdf"
    assert csv_path.read_text(encoding="utf-8").startswith("id,ts,actor,action")
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    signature = json.loads(sig_path.read_text(encoding="utf-8"))
    assert compliance_ledger.verify_csv_signature(
        csv_path,
        signature,
        compliance_ledger.LedgerSigningKey(key_id="local-dev", secret=b"test-secret"),
    )


def test_verify_script_reports_chain_ok(ledger_engine, capsys) -> None:
    compliance_ledger.record(
        actor="a",
        action="one",
        release_id="OP-1",
        before_state=None,
        after_state="pending",
        reason="created",
        evidence={"n": 1},
    )
    script = _load_module(VERIFY_SCRIPT, "_verify_compliance_chain_under_test")

    rc = script.main(["--since=2000-01-01"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["verified_rows"] == 1
