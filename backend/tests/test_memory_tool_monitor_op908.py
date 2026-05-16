"""OP-908 F10 -- Memory Tool eviction monitoring + cap scaling."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.agents.memory_tool_handler import MemoryToolConfig, MemoryToolHandler


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "memory_tool_monitor.py"


def _load_monitor() -> Any:
    spec = importlib.util.spec_from_file_location("memory_tool_monitor_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_tool_monitor_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


monitor = _load_monitor()


def _write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _metric_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_thresholds_emit_warn_page_and_evict_actions(tmp_path: Path) -> None:
    """AC #1/#2/#5: hourly scan exports per-fleet size, count, and actions."""
    root = tmp_path / "memory"
    cap_mb = 10
    _write_bytes(root / "claude" / "warn.bin", 7 * 1024 * 1024)
    _write_bytes(root / "codex" / "page.bin", 9 * 1024 * 1024)
    _write_bytes(root / "merger" / "evict.bin", 10 * 1024 * 1024)

    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    metrics = {
        fleet: monitor.scan_fleet(
            root=root,
            fleet=fleet,
            cap_mb=cap_mb,
            now=now,
            stale_allowlist=set(),
        )
        for fleet in ("claude", "codex", "merger")
    }

    assert metrics["claude"].action == "warn"
    assert metrics["claude"].file_count == 1
    assert metrics["codex"].action == "page_operator"
    assert metrics["merger"].action == "evict_required"
    assert metrics["merger"].pct_cap == 100.0


def test_main_writes_f15_metrics_jsonl(tmp_path: Path) -> None:
    """AC #5: monitor writes per-fleet JSONL for operator dashboard ingestion."""
    root = tmp_path / "memory"
    _write_bytes(root / "codex" / "memory.md", 1024)
    metrics_path = tmp_path / "metrics.jsonl"

    rc = monitor.main([
        "--root", str(root),
        "--fleets", "codex",
        "--cap-mb", "1",
        "--metrics", str(metrics_path),
        "--proposal-file", str(tmp_path / "memory-cap-proposals.md"),
    ])

    assert rc == 0
    rows = _metric_rows(metrics_path)
    assert rows == [{
        "action": "ok",
        "bytes": 1024,
        "cap_bytes": 1048576,
        "error": None,
        "file_count": 1,
        "fleet": "codex",
        "op": "scan",
        "path": str(root / "codex"),
        "pct_cap": 0.098,
        "stale_files": [],
        "timestamp": rows[0]["timestamp"],
        "type": "memory_tool_monitor",
    }]


def test_cap_scale_proposal_after_three_day_eviction_flood(tmp_path: Path) -> None:
    """AC #3: >5 evictions on each of three days appends a 10 MB proposal."""
    audit = tmp_path / "progress.txt"
    now = datetime(2026, 5, 12, 12, tzinfo=timezone.utc)
    rows = []
    for days_ago in (1, 2, 3):
        ts = (now - timedelta(days=days_ago)).isoformat()
        rows.extend(
            {"op": "evict", "fleet": "codex", "timestamp": ts, "key": f"/memories/{days_ago}-{i}.md"}
            for i in range(6)
        )
    audit.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    proposal = tmp_path / "memory-cap-proposals.md"

    counts = monitor.eviction_counts_by_day([audit], now)
    proposed = monitor.append_cap_proposals(
        path=proposal,
        counts=counts,
        cap_mb=100,
        now=now,
    )
    duplicate = monitor.append_cap_proposals(
        path=proposal,
        counts=counts,
        cap_mb=100,
        now=now + timedelta(hours=1),
    )

    text = proposal.read_text(encoding="utf-8")
    assert proposed == ["codex"]
    assert duplicate == []
    assert "fleet=codex" in text
    assert "current_cap_mb=100 proposed_cap_mb=110" in text
    assert text.count("fleet=codex") == 1


def test_stale_file_detection_honors_operator_allowlist(tmp_path: Path) -> None:
    """AC #4 and error catalog: unread >30d files are flagged unless allowlisted."""
    root = tmp_path / "memory"
    stale = root / "codex" / "old.md"
    kept = root / "codex" / "keep.md"
    fresh = root / "codex" / "fresh.md"
    for path in (stale, kept, fresh):
        _write_bytes(path, 10)

    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    old = (now - timedelta(days=31)).timestamp()
    os.utime(stale, (old, old))
    os.utime(kept, (old, old))

    metric = monitor.scan_fleet(
        root=root,
        fleet="codex",
        cap_mb=1,
        now=now,
        stale_allowlist={"/memories/keep.md"},
    )

    assert [item.path for item in metric.stale_files] == ["/memories/old.md"]
    assert metric.stale_files[0].unread_days == 31


def test_c1_eviction_audit_rows_include_fleet_for_monitor(tmp_path: Path) -> None:
    """AC #3: C1 eviction rows carry fleet so daily cap scaling is per fleet."""
    progress = tmp_path / "progress.txt"
    handler = MemoryToolHandler(MemoryToolConfig(
        fleet_id="codex",
        storage_root=tmp_path / "memory" / "codex",
        cap_mb=1,
        progress_path=progress,
        ticket_key="OP-908",
    ))
    chunk = "x" * (300 * 1024)
    for i in range(3):
        handler.handle({"command": "create", "path": f"/memories/file-{i}.md", "file_text": chunk})
        os.utime(handler.config.storage_root / f"file-{i}.md", (1000 + i, 1000 + i))

    handler.handle({"command": "create", "path": "/memories/file-3.md", "file_text": chunk})

    rows = _metric_rows(progress)
    evict_rows = [row for row in rows if row.get("op") == "evict"]
    exceeded = [row for row in rows if row.get("op") == "MemoryCapExceeded"]
    assert evict_rows and evict_rows[0]["fleet"] == "codex"
    assert exceeded and exceeded[0]["fleet"] == "codex"


def test_systemd_timer_is_hourly_and_uses_monitor_script() -> None:
    """AC #1/#5: deployment unit schedules hourly monitor metrics export."""
    service = (REPO_ROOT / "deploy/systemd/memory-tool-monitor.service").read_text(encoding="utf-8")
    timer = (REPO_ROOT / "deploy/systemd/memory-tool-monitor.timer").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "docs/operations/memory-tool-monitoring-runbook.md").read_text(encoding="utf-8")

    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "scripts/memory_tool_monitor.py" in service
    assert "OMNISIGHT_MEMORY_TOOL_METRICS=/var/omnisight/memory/metrics.jsonl" in service
    assert "F15 should ingest this file directly" in runbook
