"""OP-1732 -- commit_promotion_ledger helper tests.

Covers the requirement that the promotion LEDGER (audit row +
predicates), which promote writes into an ephemeral /tmp worktree, can be
merged from a stable path into the canonical committable ``audit/`` paths:

* the audit row is appended and predicates copied from a --from-dir;
* the merge is idempotent (re-running appends nothing, copies nothing);
* an audit row already present is not duplicated;
* a predicate name collision with DIFFERENT content is a hard error
  (never a silent overwrite);
* --dry-run writes/stages nothing;
* touched files are staged (git add) but never committed/pushed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_SCRIPT = REPO_ROOT / "scripts" / "commit_promotion_ledger.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ledger():
    return _load_module("commit_promotion_ledger", LEDGER_SCRIPT)


def _row(bundle_id: str = "develop+20260525.shaecde478", version: str = "v0.6.2") -> dict:
    return {
        "event": "image_bundle_promoted",
        "bundle_id": bundle_id,
        "version": version,
        "from_env": "staging",
        "actor": "sora",
        "approval_refs": ["OP-1729"],
        "final_digest_equality": True,
    }


def _source_dir(tmp_path: Path, *, rows=None, predicates=None) -> Path:
    src = tmp_path / "preserved"
    src.mkdir()
    rows = rows if rows is not None else [_row()]
    (src / "audit-rows.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    for name, content in (predicates or {}).items():
        (src / name).write_text(content, encoding="utf-8")
    return src


def test_merge_appends_row_and_copies_predicates(ledger, tmp_path):
    src = _source_dir(
        tmp_path,
        predicates={"develop-v0.6.2-backend.promotion.json": '{"image_ref": "be"}\n'},
    )
    audit_log = tmp_path / "audit" / "image_promotion_audit.jsonl"
    pred_dir = tmp_path / "audit" / "promotion-predicates"

    source_audit, source_predicates = ledger.gather_sources(
        from_dir=src, audit_file=None, predicate_files=[]
    )
    summary = ledger.commit_ledger(
        audit_log=audit_log,
        predicate_dir=pred_dir,
        source_audit=source_audit,
        source_predicates=source_predicates,
        stage=False,
    )

    assert len(summary["new_rows"]) == 1
    assert audit_log.read_text(encoding="utf-8").splitlines() == [
        json.dumps(_row(), sort_keys=True)
    ]
    assert (pred_dir / "develop-v0.6.2-backend.promotion.json").exists()
    assert len(summary["copied_predicates"]) == 1


def test_idempotent_rerun_is_a_noop(ledger, tmp_path):
    src = _source_dir(
        tmp_path,
        predicates={"p.promotion.json": '{"image_ref": "be"}\n'},
    )
    audit_log = tmp_path / "audit" / "image_promotion_audit.jsonl"
    pred_dir = tmp_path / "audit" / "promotion-predicates"

    common = dict(audit_log=audit_log, predicate_dir=pred_dir, stage=False)
    source_audit, source_predicates = ledger.gather_sources(
        from_dir=src, audit_file=None, predicate_files=[]
    )
    ledger.commit_ledger(source_audit=source_audit, source_predicates=source_predicates, **common)

    # Second run: a fresh discovery over the same source.
    source_audit2, source_predicates2 = ledger.gather_sources(
        from_dir=src, audit_file=None, predicate_files=[]
    )
    summary2 = ledger.commit_ledger(
        source_audit=source_audit2, source_predicates=source_predicates2, **common
    )

    assert summary2["new_rows"] == []
    assert summary2["copied_predicates"] == []
    # Row was not duplicated.
    assert len(audit_log.read_text(encoding="utf-8").splitlines()) == 1


def test_existing_row_not_duplicated(ledger, tmp_path):
    audit_log = tmp_path / "image_promotion_audit.jsonl"
    audit_log.write_text(json.dumps(_row(), sort_keys=True) + "\n", encoding="utf-8")
    src = _source_dir(tmp_path)  # same row

    source_audit, _ = ledger.gather_sources(from_dir=src, audit_file=None, predicate_files=[])
    new = ledger.merge_audit_rows(
        source_rows=ledger._read_rows(source_audit), audit_log=audit_log
    )
    assert new == []
    assert len(audit_log.read_text(encoding="utf-8").splitlines()) == 1


def test_predicate_collision_with_different_content_is_error(ledger, tmp_path):
    pred_dir = tmp_path / "predicates"
    pred_dir.mkdir()
    (pred_dir / "p.promotion.json").write_text('{"image_ref": "old"}\n', encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "p.promotion.json").write_text('{"image_ref": "NEW"}\n', encoding="utf-8")

    with pytest.raises(ledger.LedgerError, match="refusing to overwrite"):
        ledger.copy_predicates(
            source_files=[src / "p.promotion.json"], predicate_dir=pred_dir
        )


def test_predicate_collision_identical_content_is_skipped(ledger, tmp_path):
    pred_dir = tmp_path / "predicates"
    pred_dir.mkdir()
    content = '{"image_ref": "same"}\n'
    (pred_dir / "p.promotion.json").write_text(content, encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "p.promotion.json").write_text(content, encoding="utf-8")

    copied = ledger.copy_predicates(
        source_files=[src / "p.promotion.json"], predicate_dir=pred_dir
    )
    assert copied == []


def test_dry_run_writes_nothing(ledger, tmp_path):
    src = _source_dir(
        tmp_path,
        predicates={"p.promotion.json": '{"image_ref": "be"}\n'},
    )
    audit_log = tmp_path / "audit" / "image_promotion_audit.jsonl"
    pred_dir = tmp_path / "audit" / "promotion-predicates"

    source_audit, source_predicates = ledger.gather_sources(
        from_dir=src, audit_file=None, predicate_files=[]
    )
    summary = ledger.commit_ledger(
        audit_log=audit_log,
        predicate_dir=pred_dir,
        source_audit=source_audit,
        source_predicates=source_predicates,
        dry_run=True,
        stage=False,
    )

    assert len(summary["new_rows"]) == 1  # reports what WOULD change
    assert not audit_log.exists()
    assert not pred_dir.exists()


def test_stage_calls_git_add_never_commit_or_push(ledger, tmp_path):
    src = _source_dir(
        tmp_path,
        predicates={"p.promotion.json": '{"image_ref": "be"}\n'},
    )
    audit_log = tmp_path / "audit" / "image_promotion_audit.jsonl"
    pred_dir = tmp_path / "audit" / "promotion-predicates"
    calls: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        calls.append(list(cmd))
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, "", "")

    source_audit, source_predicates = ledger.gather_sources(
        from_dir=src, audit_file=None, predicate_files=[]
    )
    ledger.commit_ledger(
        audit_log=audit_log,
        predicate_dir=pred_dir,
        source_audit=source_audit,
        source_predicates=source_predicates,
        stage=True,
        runner=fake_runner,
    )

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == ["git", "add"]
    assert str(audit_log) in cmd
    # The helper must never commit or push the ledger itself.
    for c in calls:
        assert c[:2] != ["git", "commit"]
        assert "push" not in c


def test_gather_sources_requires_some_input(ledger, tmp_path):
    with pytest.raises(ledger.LedgerError, match="no ledger found"):
        ledger.gather_sources(from_dir=None, audit_file=None, predicate_files=[])


def test_cli_dry_run_against_real_backup(ledger, tmp_path):
    """Smoke the CLI end-to-end against the preserved v0.6.2 record if present
    (the first application documented in the runbook)."""
    backup = Path("/home/user/backups/promote-records/v0.6.2")
    if not backup.is_dir():
        pytest.skip("v0.6.2 backup record not present")
    rc = ledger.main(["--from-dir", str(backup), "--audit-log",
                       str(tmp_path / "a.jsonl"), "--predicate-out-dir",
                       str(tmp_path / "p"), "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "a.jsonl").exists()
