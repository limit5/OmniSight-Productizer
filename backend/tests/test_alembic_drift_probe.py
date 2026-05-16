"""OP-1163 — Alembic drift probe metrics."""

from __future__ import annotations

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
