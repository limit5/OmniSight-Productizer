"""OP-1163 — Alembic drift probe metrics."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend import metrics
from backend.agents.alembic_drift_probe import AlembicDriftProbe


pytestmark = pytest.mark.skipif(
    not metrics.is_available(), reason="prometheus_client not installed"
)


def _probe_for(payload: dict, *, code: int = 0) -> AlembicDriftProbe:
    return AlembicDriftProbe(
        comparator=lambda _db_url, _script_dir: (code, payload),
        db_url_resolver=lambda: "sqlite:///probe.db",
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


def test_probe_emits_aligned_when_image_eq_db() -> None:
    metrics.reset_for_tests()
    probe = _probe_for({"reason": "heads_match"})

    assert probe.probe_once() == "aligned"

    assert _sample_value("omnisight_alembic_drift", direction="aligned") == 0.0
    assert _sample_value("omnisight_alembic_drift", direction="forward") == 0.0
    assert _sample_value("omnisight_alembic_drift", direction="backward") == 0.0


def test_probe_emits_forward_when_image_gt_db() -> None:
    metrics.reset_for_tests()
    probe = _probe_for(
        {"reason": "alembic_head_drift", "drift_direction": "image_ahead"},
        code=1,
    )

    assert probe.probe_once() == "forward"

    assert _sample_value("omnisight_alembic_drift", direction="forward") == 1.0
    assert _sample_value("omnisight_alembic_drift", direction="backward") == 0.0


def test_probe_emits_backward_when_image_lt_db() -> None:
    metrics.reset_for_tests()
    probe = _probe_for(
        {"reason": "alembic_head_drift", "drift_direction": "db_ahead"},
        code=1,
    )

    assert probe.probe_once() == "backward"

    assert _sample_value("omnisight_alembic_drift", direction="backward") == 1.0
    assert _sample_value("omnisight_alembic_drift", direction="forward") == 0.0


def test_probe_db_query_failure_emits_error_counter() -> None:
    metrics.reset_for_tests()
    probe = _probe_for({"reason": "db_unreachable"}, code=2)

    assert probe.probe_once() == "error"

    assert _sample_value("omnisight_alembic_drift_probe_errors_total") == 1.0


def test_probe_clears_unknown_and_stamps_freshness_on_success() -> None:
    # §4.2 unknown=0 + §4.4 last_collection_ts advances on a computed result.
    metrics.reset_for_tests()
    before = time.time()
    probe = _probe_for({"reason": "heads_match"})

    assert probe.probe_once() == "aligned"

    assert _sample_value("omnisight_alembic_drift_unknown") == 0.0
    assert _sample_value("omnisight_alembic_drift_last_collection_ts") >= before


def test_probe_sets_unknown_on_db_unreachable() -> None:
    # §4.2 — a code=2 (db_unreachable) collection cannot be computed.
    metrics.reset_for_tests()
    probe = _probe_for({"reason": "db_unreachable"}, code=2)

    assert probe.probe_once() == "error"

    assert _sample_value("omnisight_alembic_drift_unknown") == 1.0


def test_probe_sets_unknown_on_comparator_exception() -> None:
    # §4.2 — a raising comparator is the "cannot compute" case too.
    metrics.reset_for_tests()

    def _boom(_db_url: str, _script_dir: Path) -> tuple[int, dict]:
        raise RuntimeError("alembic CLI absent")

    probe = AlembicDriftProbe(
        comparator=_boom,
        db_url_resolver=lambda: "sqlite:///probe.db",
        script_dir_resolver=lambda: Path("/tmp/alembic"),
    )

    assert probe.probe_once() == "error"

    assert _sample_value("omnisight_alembic_drift_unknown") == 1.0


def test_probe_does_not_stamp_freshness_on_failed_collection() -> None:
    # §4.4 — freshness only records *successful* collections so the
    # AlertBridge staleness rule can fire while the probe keeps erroring.
    metrics.reset_for_tests()
    probe = _probe_for({"reason": "db_unreachable"}, code=2)

    assert probe.probe_once() == "error"

    assert _sample_value("omnisight_alembic_drift_last_collection_ts") == 0.0


def test_probe_recovery_clears_unknown_after_error() -> None:
    # An erroring probe sets unknown=1; the next good collection clears it.
    metrics.reset_for_tests()
    err_probe = _probe_for({"reason": "db_unreachable"}, code=2)
    assert err_probe.probe_once() == "error"
    assert _sample_value("omnisight_alembic_drift_unknown") == 1.0

    ok_probe = _probe_for(
        {"reason": "alembic_head_drift", "drift_direction": "image_ahead"},
        code=1,
    )
    assert ok_probe.probe_once() == "forward"
    assert _sample_value("omnisight_alembic_drift_unknown") == 0.0
    assert _sample_value("omnisight_alembic_drift_last_collection_ts") > 0.0


def test_probe_does_not_call_readyz(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_for_tests()
    readyz_get = Mock()
    requests_get = Mock()
    monkeypatch.setattr("httpx.Client.get", readyz_get, raising=False)
    monkeypatch.setattr("requests.get", requests_get, raising=False)
    probe = _probe_for({"reason": "heads_match"})

    assert probe.probe_once() == "aligned"

    readyz_get.assert_not_called()
    requests_get.assert_not_called()
