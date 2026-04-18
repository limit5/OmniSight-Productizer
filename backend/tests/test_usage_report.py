"""M4 — tests for scripts/usage_report.py.

Pure unit tests on the rendering + live-mode data extraction. HTTP
mode is exercised via a stubbed urlopen so the test doesn't depend on
a running backend.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "usage_report.py"


def _import_script():
    spec = importlib.util.spec_from_file_location("usage_report_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ur():
    return _import_script()


@pytest.fixture(autouse=True)
def _reset_metrics():
    from backend import host_metrics as hm
    hm._reset_for_tests()
    yield
    hm._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Renderers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRenderers:
    def test_text_empty_is_friendly(self, ur):
        out = ur.render([], "text")
        assert "no tenants" in out.lower()

    def test_text_has_header_and_one_row(self, ur):
        rows = [{
            "tenant_id": "tA", "cpu_seconds_total": 12.5,
            "mem_gb_seconds_total": 2.0, "cpu_percent_now": 50.0,
            "mem_used_gb_now": 1.5, "disk_used_gb": 0.5,
            "sandbox_count": 2, "last_updated": 123.0,
        }]
        text = ur.render(rows, "text")
        assert "Tenant" in text
        assert "tA" in text
        assert "12.50" in text

    def test_json_round_trips(self, ur):
        rows = [{"tenant_id": "tA", "cpu_seconds_total": 1.0,
                 "mem_gb_seconds_total": 2.0, "cpu_percent_now": 3.0,
                 "mem_used_gb_now": 4.0, "disk_used_gb": 5.0,
                 "sandbox_count": 6, "last_updated": 7.0}]
        out = ur.render(rows, "json")
        assert json.loads(out) == rows

    def test_csv_has_header_and_body(self, ur):
        rows = [{"tenant_id": "tA", "cpu_seconds_total": 1.0,
                 "mem_gb_seconds_total": 2.0, "cpu_percent_now": 3.0,
                 "mem_used_gb_now": 4.0, "disk_used_gb": 5.0,
                 "sandbox_count": 6, "last_updated": 7.0}]
        out = ur.render(rows, "csv")
        lines = out.strip().splitlines()
        assert lines[0].startswith("tenant_id,")
        assert "tA" in lines[1]

    def test_csv_empty_is_empty(self, ur):
        assert ur.render([], "csv") == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Live mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLiveMode:
    def test_live_rows_pull_accounting_plus_live(self, ur, monkeypatch):
        from backend import host_metrics as hm
        with hm._lock:
            hm._latest_by_tenant["tA"] = hm.TenantUsage(
                tenant_id="tA", cpu_percent=120.0, mem_used_gb=2.0,
                disk_used_gb=1.5, sandbox_count=2,
            )
        hm.accumulate_usage(
            {"tA": hm.TenantUsage(tenant_id="tA", cpu_percent=100.0,
                                   mem_used_gb=2.0)},
            interval_s=10.0,
        )
        rows = ur._rows_live()
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == "tA"
        assert rows[0]["cpu_seconds_total"] > 0
        assert rows[0]["cpu_percent_now"] == 120.0
        assert rows[0]["sandbox_count"] == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP mode (stubbed urlopen)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload
    def read(self): return self._payload
    def __enter__(self): return self
    def __exit__(self, *_): return False


class TestHttpMode:
    def test_http_merges_accounting_plus_live(self, ur, monkeypatch):
        responses = {
            "/api/v1/host/accounting": {
                "tenants": [
                    {"tenant_id": "tA", "cpu_seconds_total": 1000.0,
                     "mem_gb_seconds_total": 200.0, "last_updated": 123.0},
                ],
            },
            "/api/v1/host/metrics": {
                "tenants": [
                    {"tenant_id": "tA", "cpu_percent": 50.0, "mem_used_gb": 1.0,
                     "disk_used_gb": 0.5, "sandbox_count": 1},
                ],
            },
        }

        def fake_urlopen(req, timeout=10):
            path = req.full_url.split("localhost:8000", 1)[-1]
            return _FakeResp(json.dumps(responses[path]).encode())

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        rows = ur._rows_http("http://localhost:8000", token="secret")
        assert len(rows) == 1
        assert rows[0]["cpu_seconds_total"] == 1000.0
        assert rows[0]["disk_used_gb"] == 0.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI entry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCli:
    def test_cli_live_text_no_data(self, ur, capsys):
        rc = ur.main(["--live", "--format", "text"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no tenants" in captured.out.lower()

    def test_cli_live_json_shape(self, ur, capsys):
        from backend import host_metrics as hm
        hm.accumulate_usage(
            {"tA": hm.TenantUsage(tenant_id="tA", cpu_percent=100.0,
                                   mem_used_gb=1.0)},
            interval_s=1.0,
        )
        rc = ur.main(["--live", "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        assert data[0]["tenant_id"] == "tA"
