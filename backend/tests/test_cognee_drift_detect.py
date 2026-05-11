"""OP-907 F9 — Cognee ontology drift detection tests.

The 5 spec cases plus a small number of unit tests covering the
state machine + report writer so the fixture-driven cases stay
declarative.

Spec test plan mapping (per OP-907 description):

1. ``test_stale_entity_detected`` — entity in KG, absent from reality.
2. ``test_missing_entity_detected`` — entity in reality, absent from KG.
3. ``test_schema_violation_detected`` — KG class not in approved YAML.
4. ``test_allowlist_suppresses_false_positive`` — ignore.yaml drops the row.
5. ``test_escalation_triggers_after_7_days`` — 8-day-old drift pages
   the OP-721 notifier.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import cognee_drift_detect as cdd


# ── Fixtures ─────────────────────────────────────────────────────


def _write_classes_yaml(path: Path, classes: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ["version: 1", "classes:"]
    for c in classes:
        body.append(f"  - name: {c}")
        body.append("    source: python")
        body.append(f"    description: 'test class {c}'")
    path.write_text("\n".join(body) + "\n")


def _write_ignore_yaml(path: Path, entries: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ["version: 1", "ignore:"]
    if not entries:
        # Empty list serialised as []
        body = ["version: 1", "ignore: []"]
    else:
        for e in entries:
            body.append(f"  - kind: {e['kind']}")
            body.append(f"    class: {e['class']}")
            body.append(f"    key: {e['key']}")
            body.append("    rationale: 'test'")
    path.write_text("\n".join(body) + "\n")


def _init_repo(repo_root: Path, files: dict[str, str]) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
    for rel, body in files.items():
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo_root, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        {
            "backend/a.py": "value = 1\n",
            "backend/b.py": "value = 2\n",
            "scripts/runme.py": "print('hi')\n",
        },
    )
    return repo


@pytest.fixture
def fixture_config(tmp_path: Path, repo: Path) -> cdd.DriftDetectConfig:
    classes_yaml = tmp_path / "classes.yaml"
    _write_classes_yaml(classes_yaml, ["PythonModule", "JiraTicket"])

    ignore_yaml = tmp_path / "ignore.yaml"
    _write_ignore_yaml(ignore_yaml, [])

    out_dir = tmp_path / "audit"
    state_file = tmp_path / "state.json"

    return cdd.DriftDetectConfig(
        repo_root=repo,
        out_dir=out_dir,
        classes_yaml=classes_yaml,
        ignore_yaml=ignore_yaml,
        state_file=state_file,
        jira_window_days=30,
        use_jira=True,
        use_escalation=True,
    )


def _make_kg(
    *,
    python_modules: list[str] | None = None,
    jira_tickets: list[str] | None = None,
    extra_classes: list[str] | None = None,
) -> cdd.KGSnapshot:
    entities: dict[str, frozenset[str]] = {}
    classes: set[str] = set()
    if python_modules is not None:
        entities["PythonModule"] = frozenset(python_modules)
        classes.add("PythonModule")
    if jira_tickets is not None:
        entities["JiraTicket"] = frozenset(jira_tickets)
        classes.add("JiraTicket")
    for cls in extra_classes or []:
        classes.add(cls)
    return cdd.KGSnapshot(classes=frozenset(classes), entities=entities)


# ── 1. Stale entity detected ─────────────────────────────────────


def test_stale_entity_detected(fixture_config: cdd.DriftDetectConfig) -> None:
    """KG holds backend.agents.removed but the file no longer exists.

    The reality snapshot lists exactly the three git-tracked modules
    (backend.a, backend.b, scripts.runme). The KG also has
    backend.agents.removed, which should land in the stale-entity
    list and propagate to the rendered Markdown report.
    """
    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme", "backend.agents.removed"],
            jira_tickets=["OP-100"],
        )

    def jira_fetcher(_window_days: int) -> list[str]:
        return ["OP-100"]

    report, report_path = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=jira_fetcher,
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert report_path is not None
    stale_keys = [item.key for item in report.stale]
    assert "backend.agents.removed" in stale_keys
    assert not report.missing
    assert not report.schema_violations
    # Report contains the stale row in the Markdown table.
    body = report_path.read_text()
    assert "Stale entities" in body
    assert "backend.agents.removed" in body


# ── 2. Missing entity detected ───────────────────────────────────


def test_missing_entity_detected(fixture_config: cdd.DriftDetectConfig) -> None:
    """File exists on disk but the KG never ingested it.

    backend.b.py is git-tracked but the KG only has backend.a +
    scripts.runme — so backend.b lands as a missing entity.
    """
    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "scripts.runme"],
            jira_tickets=["OP-100"],
        )

    def jira_fetcher(_window_days: int) -> list[str]:
        return ["OP-100"]

    report, _ = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=jira_fetcher,
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    missing_keys = [item.key for item in report.missing]
    assert "backend.b" in missing_keys
    assert not report.stale


# ── 3. Schema violation detected ─────────────────────────────────


def test_schema_violation_detected(fixture_config: cdd.DriftDetectConfig) -> None:
    """KG carries a class label not present in the approved YAML.

    YAML approves {PythonModule, JiraTicket}. KG adds
    `RogueClass` — the detector must surface a schema mismatch.
    """
    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme"],
            jira_tickets=["OP-100"],
            extra_classes=["RogueClass"],
        )

    def jira_fetcher(_window_days: int) -> list[str]:
        return ["OP-100"]

    report, _ = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=jira_fetcher,
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    schema_keys = [item.key for item in report.schema_violations]
    assert "RogueClass" in schema_keys
    assert not report.stale
    assert not report.missing


# ── 4. Allowlist suppresses false-positive ───────────────────────


def test_allowlist_suppresses_false_positive(fixture_config: cdd.DriftDetectConfig) -> None:
    """The same stale entity from case 1, but with an allowlist entry.

    The drift item must be silently dropped — neither in the report's
    stale list nor in the rendered Markdown table.
    """
    _write_ignore_yaml(
        fixture_config.ignore_yaml,
        [{"kind": "stale", "class": "PythonModule", "key": "backend.agents.removed"}],
    )

    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme", "backend.agents.removed"],
            jira_tickets=["OP-100"],
        )

    def jira_fetcher(_window_days: int) -> list[str]:
        return ["OP-100"]

    report, report_path = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=jira_fetcher,
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert not report.stale  # suppressed
    body = report_path.read_text() if report_path else ""
    # Body lists the kind heading but the stale table reports "None".
    assert "backend.agents.removed" not in body


# ── 5. Escalation triggers after 7 days ──────────────────────────


def test_escalation_triggers_after_7_days(fixture_config: cdd.DriftDetectConfig) -> None:
    """A drift item that has been unresolved for >7 days pages the OP-721
    notifier with the spec-mandated DEGRADED severity + escalation code.

    We seed the state file with first_seen 8 days ago, then run the
    detect pipeline with the same drift item present. The notifier
    callable is a MagicMock — we assert it was called once with the
    expected code + age in context.
    """
    seed_state = cdd.DriftState(first_seen={
        "stale::PythonModule::backend.agents.removed":
            (datetime(2026, 5, 11, tzinfo=timezone.utc) - timedelta(days=8)).isoformat(),
    })
    cdd.save_drift_state(seed_state, fixture_config.state_file)

    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme", "backend.agents.removed"],
            jira_tickets=["OP-100"],
        )

    def jira_fetcher(_window_days: int) -> list[str]:
        return ["OP-100"]

    notifier = MagicMock()
    report, _ = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=jira_fetcher,
        notifier=notifier,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert report.stale  # drift still present
    notifier.assert_called_once()
    args, kwargs = notifier.call_args
    assert args[0] == "DEGRADED"
    assert kwargs["code"] == cdd.DRIFT_CODE_ESCALATION
    # The context must include enough to triage without opening the report.
    assert kwargs["drift_count"] >= 1
    assert kwargs["sample_class"] == "PythonModule"
    assert kwargs["sample_key"] == "backend.agents.removed"
    assert kwargs["sample_age_days"] >= 7


# ── Additional safety nets ───────────────────────────────────────


def test_jira_outage_does_not_create_stale_ticket_storm(
    fixture_config: cdd.DriftDetectConfig,
) -> None:
    """When the JIRA fetcher raises, the empty key set must NOT make
    every KG ticket look stale — that would be the OP-907 spec's worst
    operator-experience failure mode (paging on a JIRA outage)."""

    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme"],
            jira_tickets=["OP-100", "OP-101", "OP-102"],
        )

    def angry_fetcher(_window_days: int) -> list[str]:
        raise RuntimeError("JIRA REST 503")

    report, _ = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=angry_fetcher,
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    # No stale tickets surfaced — the detector skipped the JIRA dim.
    stale_jira = [item for item in report.stale if item.class_name == "JiraTicket"]
    assert stale_jira == []


def test_kg_unreachable_raises_typed_error(fixture_config: cdd.DriftDetectConfig) -> None:
    """The KG-collector failure surface is a typed error so the CLI
    can exit 2 (skip today, retry tomorrow) rather than 1 (fatal)."""

    def bad_collector() -> cdd.KGSnapshot:
        raise cdd.DriftDetectKGUnreachable("neo4j refused")

    with pytest.raises(cdd.DriftDetectKGUnreachable):
        cdd.run_drift_detect(fixture_config, kg_collector=bad_collector)


def test_audit_write_failure_falls_through_to_stdout(
    fixture_config: cdd.DriftDetectConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When write_report raises, the script must keep going and dump
    the rendered Markdown to stdout (degrade-not-die)."""

    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme"],
            jira_tickets=["OP-100"],
        )

    # Replace out_dir with a path that cannot be created (a file in
    # place of a directory).
    blocker = fixture_config.out_dir.parent / "blocker"
    blocker.write_text("not a directory")
    fixture_config = cdd.DriftDetectConfig(
        **{**vars(fixture_config), "out_dir": blocker / "child"},
    )

    report, report_path = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=lambda _d: ["OP-100"],
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert report_path is None
    captured = capsys.readouterr()
    assert "Cognee ontology drift" in captured.out


def test_state_resolved_items_cleared(fixture_config: cdd.DriftDetectConfig) -> None:
    """Once a drift item disappears from the report, its first_seen
    entry must be evicted from state — otherwise re-occurrences would
    inherit an ancient first_seen and prematurely escalate."""

    seed_state = cdd.DriftState(first_seen={
        "stale::PythonModule::backend.agents.removed":
            (datetime(2026, 5, 11, tzinfo=timezone.utc) - timedelta(days=20)).isoformat(),
    })
    cdd.save_drift_state(seed_state, fixture_config.state_file)

    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme"],
            jira_tickets=["OP-100"],
        )

    cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=lambda _d: ["OP-100"],
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    final = cdd.load_drift_state(fixture_config.state_file)
    assert "stale::PythonModule::backend.agents.removed" not in final.first_seen


def test_report_render_contains_counts_and_actions() -> None:
    """The Markdown report's `Counts` section + suggested-action lines
    are operator-visible contracts; assert their literal shape."""
    report = cdd.DriftReport(
        stale=[cdd.DriftItem(kind="stale", class_name="PythonModule", key="x")],
        missing=[cdd.DriftItem(kind="missing", class_name="JiraTicket", key="OP-1")],
        schema_violations=[cdd.DriftItem(kind="schema", class_name="Rogue", key="Rogue")],
    )
    md = cdd.render_report_markdown(report, generated_for="2026-05-11")
    assert "Cognee ontology drift — 2026-05-11" in md
    assert "Stale entities: 1" in md
    assert "Missing entities: 1" in md
    assert "Schema mismatches: 1" in md
    assert "Total drift items: 3" in md
    assert "Suggested action" in md


def test_empty_jira_does_not_promote_modules_to_missing(
    fixture_config: cdd.DriftDetectConfig,
) -> None:
    """Reality includes Python modules even when JIRA returns no
    tickets — the JIRA-empty guard only affects JiraTicket drift."""

    def kg_collector() -> cdd.KGSnapshot:
        return _make_kg(
            python_modules=["backend.a"],  # backend.b + scripts.runme missing
            jira_tickets=[],
        )

    report, _ = cdd.run_drift_detect(
        fixture_config,
        kg_collector=kg_collector,
        jira_fetcher=lambda _d: [],  # empty JIRA reality
        notifier=lambda *a, **k: None,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    missing_modules = {item.key for item in report.missing if item.class_name == "PythonModule"}
    assert "backend.b" in missing_modules
    assert "scripts.runme" in missing_modules


def test_escalation_skipped_when_use_escalation_false(
    fixture_config: cdd.DriftDetectConfig,
) -> None:
    """The --no-escalation flag short-circuits the notifier even when
    the drift is well over 7 days old — useful for forensic replays."""
    seed_state = cdd.DriftState(first_seen={
        "stale::PythonModule::backend.agents.removed":
            (datetime(2026, 5, 11, tzinfo=timezone.utc) - timedelta(days=30)).isoformat(),
    })
    cdd.save_drift_state(seed_state, fixture_config.state_file)
    cfg = cdd.DriftDetectConfig(**{**vars(fixture_config), "use_escalation": False})

    notifier = MagicMock()
    cdd.run_drift_detect(
        cfg,
        kg_collector=lambda: _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme", "backend.agents.removed"],
            jira_tickets=["OP-100"],
        ),
        jira_fetcher=lambda _d: ["OP-100"],
        notifier=notifier,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    notifier.assert_not_called()


def test_escalation_bridge_down_falls_back(
    fixture_config: cdd.DriftDetectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the notifier raises, the detector must swallow the failure
    (logged as escalation_bridge_down) and degrade to the email
    fallback path. The audit pipeline overall stays exit-0."""
    seed_state = cdd.DriftState(first_seen={
        "stale::PythonModule::backend.agents.removed":
            (datetime(2026, 5, 11, tzinfo=timezone.utc) - timedelta(days=10)).isoformat(),
    })
    cdd.save_drift_state(seed_state, fixture_config.state_file)

    def angry_notifier(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("smtp dead")

    email_called: list[bool] = []

    def fake_email(items: list[Any], *, report_path: Path | None) -> None:
        email_called.append(True)

    monkeypatch.setattr(cdd, "_email_fallback", fake_email)

    # Should not raise — the bridge-down path is internally caught.
    cdd.run_drift_detect(
        fixture_config,
        kg_collector=lambda: _make_kg(
            python_modules=["backend.a", "backend.b", "scripts.runme", "backend.agents.removed"],
            jira_tickets=["OP-100"],
        ),
        jira_fetcher=lambda _d: ["OP-100"],
        notifier=angry_notifier,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert email_called == [True]
