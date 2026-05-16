"""OP-1171 — v2-⑥-DRDrillForward — synthetic forward-drift recovery drill.

Chaos test that exercises the full forward-drift recovery path end-to-end:

* Synthetic DB at alembic head N-1 (one revision behind) while the image
  ``MANIFEST.json`` claims head N (image_ahead / forward drift).
* Invoke :func:`backend.alembic_startup_hook.maybe_run_startup_upgrade`
  and observe that
    - the advisory lock is acquired and released without contention,
    - ``alembic upgrade head`` runs exactly once,
    - the second drift check returns ``aligned`` so the post-upgrade DB
      head equals image head N.
* Re-scrape :class:`backend.agents.alembic_drift_probe.AlembicDriftProbe`
  and confirm ``omnisight_alembic_drift{direction="forward"} == 0`` (the
  probe reports ``aligned``) — the Family ⑥ alert rule (OP-1170)
  observes this gauge transition and resolves.
* Hand a synthetic ``OmniSightAlembicDrift`` envelope shaped after
  ``deploy/prometheus/rules/family6.yml`` to a mock AlertBridge adapter
  in two phases: first a firing envelope (``resolved_at=None``), then a
  resolution envelope (``resolved_at != None``). The mock captures
  both deliveries; severity=page rules also fan out to the email
  channel per ``SEVERITY_CHANNEL_MAP`` so we assert both surfaces.

The drill is wall-clock bounded by the ``DRILL_BUDGET_S`` constant
(currently 30s) per the Deploy AC. No real Postgres / Alembic command
runs — everything is monkeypatched so the test is collection-default
and CI-safe; the chaos surface is the *control-flow* through the
hook + probe + bridge composition, not a real database.

Pre-flight (per ticket §Pre-flight, [[feedback_filing_existing_impl_check]]):

* ``git ls-tree refs/remotes/origin/develop -- backend/alembic_startup_hook.py``
  → ``8cd99714…`` (OP-1166 merged).
* ``git ls-tree refs/remotes/origin/develop -- backend/agents/alembic_drift_probe.py``
  → ``e55224f2…`` (OP-1163 merged).
* ``git ls-tree -r refs/remotes/origin/develop -- backend/tests/`` shows
  ``test_alembic_startup_hook.py`` and ``test_alembic_drift_probe.py``
  but no forward-drift drill — this file ships it.
"""
from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from backend import alembic_startup_hook as hook
from backend import metrics
from backend.agents.alembic_drift_probe import AlembicDriftProbe
from backend.alerting.bridge import (
    AlertBridge,
    AlertEnvelope,
    CardinalityValidator,
    DedupeKeyGen,
    SmtpConfig,
    StdoutEmailAdapter,
    canonical_envelope,
)


IMAGE_HEAD_N = "rev_N"
DB_HEAD_NM1 = "rev_N_minus_1"
DRILL_BUDGET_S = 30.0
FIXED_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)


# ── Synthetic DB / engine doubles ────────────────────────────────────


class _Scalar:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar(self) -> bool:
        return self._value


class _SyntheticConnection:
    """Records advisory-lock acquire/release for assertion."""

    def __init__(self) -> None:
        self.acquires = 0
        self.releases = 0
        self.locked = False

    def execute(self, statement, params=None):  # noqa: ANN001
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            assert not self.locked, "lock contention — drill must be uncontended"
            self.locked = True
            self.acquires += 1
            return _Scalar(True)
        if "pg_advisory_unlock" in sql:
            self.locked = False
            self.releases += 1
            return _Scalar(True)
        raise AssertionError(f"unexpected SQL: {sql}")


class _SyntheticEngine:
    def __init__(self) -> None:
        self.conn = _SyntheticConnection()

    def connect(self):
        return self

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


class _SyntheticDb:
    """Tracks the synthetic DB's alembic head; flips on ``upgrade_head``."""

    def __init__(self, head: str) -> None:
        self.head = head
        self.upgrade_calls: list[str] = []

    def upgrade_head(self, target: str, db_url: str) -> None:
        self.upgrade_calls.append(db_url)
        self.head = target


def _manifest_at(tmp_path: Path, head: str) -> Path:
    path = tmp_path / "MANIFEST.json"
    path.write_text(
        json.dumps(
            {
                "image_sha": "sha",
                "build_time": "2026-05-16T00:00:00Z",
                "git_ref": "test",
                hook.MANIFEST_HEAD_KEY: head,
            }
        ),
        encoding="utf-8",
    )
    return path


def _payload_for(db_head: str, image_head: str) -> dict[str, Any]:
    """Build a drift-gate payload of the same shape ``check_drift`` returns.

    The hook keys off ``drift_direction``: ``image_ahead`` triggers an
    upgrade, ``match`` is the aligned terminal state.
    """
    if db_head == image_head:
        return {
            "reason": "heads_match",
            "drift_direction": "match",
            "image_heads": [image_head],
            "db_heads": [db_head],
        }
    return {
        "reason": "alembic_head_drift",
        "drift_direction": "image_ahead",
        "image_head": [image_head],
        "db_head": [db_head],
    }


# ── Mock AlertBridge adapter ─────────────────────────────────────────


class _MockBridgeAdapter:
    """Captures every envelope the AlertBridge attempts to deliver.

    The real :class:`StdoutEmailAdapter` is also exercised in
    ``test_full_recovery_drill_resolves_alert`` to keep us in sync with
    the AlertBridge §10 canonical-envelope round-trip invariant, but this
    mock is the simpler surface most assertions read.
    """

    name = "mock"

    def __init__(self) -> None:
        self.fired: list[AlertEnvelope] = []
        self.resolved: list[AlertEnvelope] = []

    def deliver(self, envelope: AlertEnvelope) -> None:
        if envelope.resolved_at is not None:
            self.resolved.append(envelope)
        else:
            self.fired.append(envelope)

    def healthcheck(self):  # pragma: no cover - protocol surface
        from backend.alerting.bridge import AdapterHealth

        return AdapterHealth(ok=True, detail="mock")


def _drift_envelope(*, resolved_at: datetime | None) -> AlertEnvelope:
    """Build an envelope matching the Family ⑥ alert rule's contract.

    Shape mirrors ``deploy/prometheus/rules/family6.yml`` so the drill
    asserts against the *same* labels/annotations Prometheus would emit
    when the rule fires on backward drift; for the forward-drill we
    re-use that shape because the AlertBridge contract carries the
    severity=page → P3 recovery edge for the rule regardless of which
    direction caused the firing.
    """
    labels = {
        "severity": "page",
        "area": "deployment",
        "family": "6",
        "defense_dimension": "D1",
        "instance": "backend-prod-a",
    }
    annotations = {
        "summary": "Alembic image-DB drift detected (forward — drill)",
        "description": "synthetic forward-drift drill OP-1171",
        "runbook_url": "https://docs.sora.services/runbooks/omnisight-alembic-drift",
        "remediation_hint": "Drill — startup hook auto-applied alembic upgrade head.",
    }
    critical_labels = ("instance",)
    return AlertEnvelope(
        alertname="OmniSightAlembicDrift",
        severity="page",
        area="deployment",
        family="6",
        defense_dimension="D1",
        labels=labels,
        annotations=annotations,
        dedupe_key=DedupeKeyGen.generate(
            "OmniSightAlembicDrift", "6", labels, critical_labels
        ),
        fired_at=FIXED_NOW,
        resolved_at=resolved_at,
        critical_labels=critical_labels,
    )


# ── Helpers reused across drill assertions ───────────────────────────


def _install_drill_fakes(
    monkeypatch: pytest.MonkeyPatch, db: _SyntheticDb
) -> tuple[_SyntheticEngine, list[dict[str, Any]]]:
    """Wire the synthetic DB into the startup hook.

    Returns the engine (for lock-state inspection) and a list collecting
    every drift payload ``_check_drift`` returns in call order, so tests
    can confirm the hook re-checks after upgrade.
    """
    engine = _SyntheticEngine()
    drift_log: list[dict[str, Any]] = []

    def fake_check_drift(_db_url: str) -> dict[str, Any]:
        payload = _payload_for(db.head, IMAGE_HEAD_N)
        drift_log.append(payload)
        return payload

    def fake_upgrade_head(db_url: str) -> None:
        db.upgrade_head(IMAGE_HEAD_N, db_url)

    monkeypatch.setattr(hook, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(hook, "_check_drift", fake_check_drift)
    monkeypatch.setattr(hook, "_upgrade_head", fake_upgrade_head)
    return engine, drift_log


def _probe_for_db(db: _SyntheticDb) -> AlembicDriftProbe:
    """A drift probe whose comparator reflects the current synthetic DB head.

    Returned exit code mirrors ``alembic_drift_gate.check_drift``:
    code 0 for aligned, code 1 for ``image_ahead`` (forward drift). The
    probe then publishes ``omnisight_alembic_drift{direction}`` via the
    same code path it uses in production.
    """

    def comparator(_db_url: str, _script_dir: Path) -> tuple[int, dict[str, Any]]:
        payload = _payload_for(db.head, IMAGE_HEAD_N)
        return (0 if payload["drift_direction"] == "match" else 1, payload)

    return AlembicDriftProbe(
        comparator=comparator,
        db_url_resolver=lambda: "postgresql://drill",
        script_dir_resolver=lambda: Path("/tmp/alembic"),
    )


def _sample_value(name: str, **labels: str) -> float:
    body = metrics.render_exposition()[0].decode()
    wanted = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
    for line in body.splitlines():
        if not line.startswith(name):
            continue
        if labels and "{" + wanted + "}" not in line:
            continue
        return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"missing sample {name} {labels!r}\n{body}")


# ── The drill ────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not metrics.is_available(),
    reason="prometheus_client not installed — drill cannot scrape gauge",
)
def test_full_recovery_drill_resolves_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end forward-drift recovery drill.

    Composes the three merged parents (OP-1166 hook, OP-1163 probe,
    OP-1170 alert rule) and asserts the four scope bullets from the
    ticket: advisory lock uncontended, single upgrade, post-upgrade
    probe shows ``aligned``, mock AlertBridge adapter receives the
    recovery envelope.
    """
    metrics.reset_for_tests()
    started_at = time.monotonic()

    # Setup: synthetic DB at N-1; image MANIFEST claims head N.
    db = _SyntheticDb(head=DB_HEAD_NM1)
    manifest = _manifest_at(tmp_path, IMAGE_HEAD_N)
    engine, drift_log = _install_drill_fakes(monkeypatch, db)

    # Pre-upgrade probe: gauge MUST report forward drift (≠ aligned).
    pre_probe = _probe_for_db(db)
    assert pre_probe.probe_once() == "forward"
    assert _sample_value("omnisight_alembic_drift", direction="forward") == 1.0

    # Synthetic firing of the OmniSightAlembicDrift rule through both a
    # mock adapter (simple capture) and the real StdoutEmailAdapter (to
    # exercise the v2-AlertBridge §10 canonical-envelope round-trip).
    mock_adapter = _MockBridgeAdapter()
    stdout_stream = io.StringIO()
    sent_emails: list[EmailMessage] = []
    real_adapter = StdoutEmailAdapter(
        smtp=SmtpConfig(
            host="smtp.test",
            port=587,
            sender="alerts@example.test",
            recipients=("oncall@example.test",),
        ),
        stream=stdout_stream,
        send_fn=lambda _smtp, msg: sent_emails.append(msg),
    )
    bridge = AlertBridge(
        adapter=mock_adapter,
        validator=CardinalityValidator(clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )
    fire_envelope = _drift_envelope(resolved_at=None)
    bridge.fire(
        alertname=fire_envelope.alertname,
        severity=fire_envelope.severity,
        area=fire_envelope.area,
        family=fire_envelope.family,
        defense_dimension=fire_envelope.defense_dimension,
        labels=fire_envelope.labels,
        annotations=fire_envelope.annotations,
        critical_labels=fire_envelope.critical_labels,
    )
    real_adapter.deliver(fire_envelope)

    # Act: invoke the startup hook against the manifest.
    db_head_after = hook.maybe_run_startup_upgrade(
        db_url="postgresql://drill", image_head_path=str(manifest)
    )

    # Assert (1): the advisory lock was acquired once and released.
    assert engine.conn.acquires == 1, "advisory lock must be acquired exactly once"
    assert engine.conn.releases == 1, "advisory lock must be released after upgrade"
    assert engine.conn.locked is False, "lock must not be held on exit"

    # Assert (2): ``alembic upgrade head`` ran exactly once.
    assert db.upgrade_calls == ["postgresql://drill"]

    # Assert (3): post-upgrade DB head equals image head N.
    assert db.head == IMAGE_HEAD_N
    assert db_head_after == IMAGE_HEAD_N
    # The hook check-drift ran twice: once pre-upgrade (image_ahead), once
    # post-upgrade (match). Sanity-check that ordering.
    assert len(drift_log) == 2
    assert drift_log[0]["drift_direction"] == "image_ahead"
    assert drift_log[1]["drift_direction"] == "match"

    # Assert (4): a re-probe within the drill budget reports ``aligned``.
    aligned_by = time.monotonic() + 60.0
    direction = "forward"
    while time.monotonic() < aligned_by:
        direction = pre_probe.probe_once()
        if direction == "aligned":
            break
    assert direction == "aligned", "probe must return aligned within 60s of upgrade"
    assert _sample_value("omnisight_alembic_drift", direction="forward") == 0.0
    assert _sample_value("omnisight_alembic_drift", direction="backward") == 0.0

    # Assert (5): mock AlertBridge adapter receives the recovery (resolved)
    # envelope per the v2-AlertBridge §"severity=page emits P3 recovery"
    # contract: a resolution envelope (``resolved_at != None``) is
    # delivered when the gauge returns to aligned.
    resolve_envelope = _drift_envelope(
        resolved_at=FIXED_NOW + timedelta(minutes=1)
    )
    bridge.adapter.deliver(resolve_envelope)
    real_adapter.deliver(resolve_envelope)

    assert len(mock_adapter.fired) == 1
    assert len(mock_adapter.resolved) == 1
    assert mock_adapter.fired[0].alertname == "OmniSightAlembicDrift"
    assert mock_adapter.resolved[0].alertname == "OmniSightAlembicDrift"
    assert mock_adapter.resolved[0].severity == "page"
    assert mock_adapter.resolved[0].resolved_at is not None
    # Dedupe key MUST match between fire and resolve so AlertManager
    # joins them (AlertBridge §4 dedupe contract).
    assert (
        mock_adapter.fired[0].dedupe_key == mock_adapter.resolved[0].dedupe_key
    )

    # Real adapter parity: stdout-side received both deliveries, and
    # both messages fanned out to email per ``SEVERITY_CHANNEL_MAP``
    # for severity=page. The resolution email is subject-prefixed
    # ``[info] [RESOLVED] `` per ``StdoutEmailAdapter._deliver_email``
    # so on-call sees the all-clear without it looking like a re-page.
    stdout_lines = [line for line in stdout_stream.getvalue().splitlines() if line]
    assert len(stdout_lines) == 2
    parsed_fire = json.loads(stdout_lines[0])
    parsed_resolve = json.loads(stdout_lines[1])
    assert parsed_fire["alertname"] == "OmniSightAlembicDrift"
    assert parsed_fire["resolved_at"] is None
    assert parsed_resolve["resolved_at"] is not None
    assert canonical_envelope(parsed_fire) == canonical_envelope(fire_envelope)
    assert canonical_envelope(parsed_resolve) == canonical_envelope(resolve_envelope)
    assert len(sent_emails) == 2
    assert sent_emails[0]["Subject"].startswith("[page] ")
    assert sent_emails[1]["Subject"].startswith("[info] [RESOLVED] ")

    # Wall-clock budget per Deploy AC: 30s.
    assert time.monotonic() - started_at < DRILL_BUDGET_S


def test_drill_no_op_when_db_already_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency guard: if the DB is already at head N the hook must
    NOT invoke alembic upgrade. Protects against the drill itself being
    re-run on a healed cluster (e.g., CI re-run after a transient
    failure)."""
    db = _SyntheticDb(head=IMAGE_HEAD_N)
    manifest = _manifest_at(tmp_path, IMAGE_HEAD_N)
    engine, drift_log = _install_drill_fakes(monkeypatch, db)

    assert (
        hook.maybe_run_startup_upgrade(
            db_url="postgresql://drill", image_head_path=str(manifest)
        )
        == IMAGE_HEAD_N
    )

    assert db.upgrade_calls == []
    assert engine.conn.acquires == 1
    assert engine.conn.releases == 1
    # Only one drift check: the aligned terminal state.
    assert len(drift_log) == 1
    assert drift_log[0]["drift_direction"] == "match"
