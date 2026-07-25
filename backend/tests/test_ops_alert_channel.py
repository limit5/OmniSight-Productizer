"""OP-2728 — ops alert channel (systemd OnFailure -> JIRA) + host disk floor.

Pure-logic tests only: nothing here touches JIRA, Prometheus, systemd or the
network. The two properties that actually matter operationally are:

  1. the handler NEVER raises and NEVER returns non-zero (an OnFailure handler
     that fails gives you restart loops and lost signal), and
  2. one open issue per source — enforced by the throttle stamp plus the
     "find open issue first" lookup.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOTIFY_PATH = PROJECT_ROOT / "scripts" / "omnisight-alert-notify.py"
FLOOR_PATH = PROJECT_ROOT / "scripts" / "omnisight-disk-floor-check.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


notify_mod = _load("omnisight_alert_notify", NOTIFY_PATH)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(notify_mod, "SPOOL", tmp_path / "spool.jsonl")
    monkeypatch.setattr(notify_mod, "RUNLOG", tmp_path / "run.log")
    return tmp_path


def test_slug_is_label_safe():
    # JIRA labels must not contain spaces; the .service suffix is noise.
    assert notify_mod._slug("omnisight-prod-backup.service") == "omnisight-prod-backup"
    assert notify_mod._slug("host disk at 91%") == "host-disk-at-91-"
    assert " " not in notify_mod._slug("a b c")


def test_throttle_window_then_clear(state, monkeypatch):
    monkeypatch.setattr(notify_mod, "THROTTLE_S", 3600)
    assert notify_mod._throttled("svc") is False  # no stamp yet -> alert
    notify_mod._touch_stamp("svc")
    assert notify_mod._throttled("svc") is True  # inside the window -> stay quiet
    notify_mod.clear_source("svc")
    assert notify_mod._throttled("svc") is False  # cleared -> next breach alerts at once


def test_notify_source_never_raises_and_spools_without_credentials(state, monkeypatch):
    """The whole point of the handler: no config, no network, still exit 0."""
    monkeypatch.setenv("OMNISIGHT_ALERT_JIRA_ENV", str(state / "does-not-exist.env"))
    rc = notify_mod.notify_source("some-source", "title", "detail")
    assert rc == 0
    spooled = [json.loads(line) for line in (state / "spool.jsonl").read_text().splitlines()]
    assert spooled and spooled[0]["unit"] == "some-source"


def test_adf_document_shape():
    doc = notify_mod._adf(["one", "two"], code_block="log tail")
    assert doc["type"] == "doc" and doc["version"] == 1
    kinds = [node["type"] for node in doc["content"]]
    assert kinds == ["paragraph", "paragraph", "codeBlock"]


def test_adf_truncates_oversized_code_block():
    doc = notify_mod._adf(["x"], code_block="y" * 50_000)
    assert len(doc["content"][-1]["content"][0]["text"]) == 30_000


# --------------------------------------------------------------- disk floor


floor_mod = _load("omnisight_disk_floor_check", FLOOR_PATH)


@pytest.fixture()
def calls(monkeypatch):
    seen: list[tuple[str, str]] = []

    def _record(src, title, detail, priority="High"):
        seen.append((src, priority))
        return 0

    monkeypatch.setattr(floor_mod.alert, "notify_source", _record)
    monkeypatch.setattr(floor_mod.alert, "clear_source", lambda src: seen.append((src, "CLEARED")))
    monkeypatch.setattr(floor_mod, "evidence", lambda: "<evidence>")
    return seen


def test_below_floor_alerts_nothing(monkeypatch, calls):
    monkeypatch.setattr(floor_mod, "current_pct", lambda: 75.0)
    assert floor_mod.main() == 0
    assert ("host-disk", "CLEARED") in calls
    assert not [c for c in calls if c[1] in ("High", "Highest")]


def test_above_warn_alerts_high(monkeypatch, calls):
    monkeypatch.setattr(floor_mod, "current_pct", lambda: 87.5)  # the 2026-07-20 nine-days-out value
    assert floor_mod.main() == 0
    assert ("host-disk", "High") in calls


def test_above_critical_escalates(monkeypatch, calls):
    monkeypatch.setattr(floor_mod, "current_pct", lambda: 99.6)  # the value the day it filled
    assert floor_mod.main() == 0
    assert ("host-disk", "Highest") in calls


def test_unreachable_prometheus_is_itself_an_alert(monkeypatch, calls):
    def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(floor_mod, "current_pct", boom)
    assert floor_mod.main() == 0
    assert ("host-disk-probe", "Medium") in calls


def test_metric_with_no_series_is_an_alert(monkeypatch, calls):
    monkeypatch.setattr(floor_mod, "current_pct", lambda: None)
    assert floor_mod.main() == 0
    assert ("host-disk-probe", "Medium") in calls
