"""OP-851 (Sprint C / C1) — Anthropic Memory Tool standalone integration.

Per ticket DoD: 8 test cases covering AC #1–#7. Each test is documented
with the AC reference it satisfies so retrospective verification can
trace evidence cleanly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.agents.memory_tool_handler import (
    DEFAULT_CAP_MB,
    DEFAULT_TIER,
    ERR_BAD_INPUT,
    ERR_STORAGE_FULL,
    ERR_TIER_VIOLATION,
    MEMORY_TOOL_BETA_HEADER,
    MEMORY_TOOL_NAME,
    MEMORY_TOOL_SPEC,
    MEMORY_TOOL_TYPE,
    MemoryStorageFull,
    MemoryToolConfig,
    MemoryToolHandler,
    MemoryToolNotStandalone,
    TierViolationUnauthorizedRecall,
    build_memory_tool_handler,
    classify_tier,
    list_seeded_lessons,
    seed_lessons_from,
    tier_is_recallable,
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    """Per-fleet storage root."""
    root = tmp_path / "memory" / "fleet-test"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def progress_path(tmp_path: Path) -> Path:
    return tmp_path / "progress.txt"


@pytest.fixture
def handler(tmp_storage: Path, progress_path: Path) -> MemoryToolHandler:
    return MemoryToolHandler(MemoryToolConfig(
        fleet_id="fleet-test",
        storage_root=tmp_storage,
        cap_mb=1,  # Small cap so eviction is exercisable in tests.
        tier_l_optin=False,
        progress_path=progress_path,
        ticket_key="OP-851",
    ))


def _audit_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── Test 1: AC #1 + #2 spike — standalone use confirmed ────────────────


def test_standalone_use_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_storage: Path
) -> None:
    """AC #2 spike: Memory Tool is usable without Managed Agents runtime.

    Standalone semantics: build the handler, register the spec, run
    a write/read roundtrip against the local filesystem — no managed
    agents session involved.
    """
    monkeypatch.delenv("OMNISIGHT_MEMORY_TOOL_REQUIRES_MA", raising=False)
    handler = MemoryToolHandler(MemoryToolConfig(
        fleet_id="standalone-test",
        storage_root=tmp_storage,
        cap_mb=5,
    ))
    # Spike: explicit verification call doesn't raise.
    handler.verify_standalone()

    # AC #1 — tool spec is the conference-announced shape.
    assert MEMORY_TOOL_SPEC == {
        "type": "memory_20260120",
        "name": "memory",
    }
    assert MEMORY_TOOL_TYPE == "memory_20260120"
    assert MEMORY_TOOL_NAME == "memory"
    assert MEMORY_TOOL_BETA_HEADER == "managed-agents-2026-04-01"

    # Spike abort path — when operator pins the kill-switch, the
    # handler refuses to claim standalone status and the orchestrator
    # falls back to B10.
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_REQUIRES_MA", "1")
    with pytest.raises(MemoryToolNotStandalone):
        handler.verify_standalone()


# ── Test 2: AC #1/#3 — write/read roundtrip on per-fleet filesystem ───


def test_write_read_roundtrip(handler: MemoryToolHandler) -> None:
    """AC #3: per-fleet filesystem storage, write then read returns body."""
    # create
    create_result = handler.handle({
        "command": "create",
        "path": "/memories/hello.md",
        "file_text": "# Hello\nworld\n",
    })
    assert create_result == {
        "ok": True,
        "path": "/memories/hello.md",
        "tier": DEFAULT_TIER,
    }
    # view (single file)
    view_result = handler.handle({
        "command": "view",
        "path": "/memories/hello.md",
    })
    assert view_result["content"] == "# Hello\nworld\n"
    assert view_result["tier"] == DEFAULT_TIER

    # str_replace
    sr = handler.handle({
        "command": "str_replace",
        "path": "/memories/hello.md",
        "old_str": "world",
        "new_str": "fleet",
    })
    assert sr["ok"] is True
    view2 = handler.handle({
        "command": "view",
        "path": "/memories/hello.md",
    })
    assert "fleet" in view2["content"]

    # rename + delete
    rn = handler.handle({
        "command": "rename",
        "path": "/memories/hello.md",
        "new_path": "/memories/hello2.md",
    })
    assert rn["ok"] is True
    assert (handler.config.storage_root / "hello2.md").exists()
    dl = handler.handle({
        "command": "delete",
        "path": "/memories/hello2.md",
    })
    assert dl["ok"] is True
    assert not (handler.config.storage_root / "hello2.md").exists()


# ── Test 3: AC #4 — eviction at cap (oldest-first) ─────────────────────


def test_eviction_oldest_first_at_cap(handler: MemoryToolHandler) -> None:
    """AC #4: when cap reached, oldest-first eviction makes room."""
    # cap_mb=1 from the fixture → ~1MB cap. Use 300KB chunks.
    chunk = "x" * (300 * 1024)
    # Place three files with manually-distinguishable mtimes.
    for i in range(3):
        handler.handle({
            "command": "create",
            "path": f"/memories/file-{i}.md",
            "file_text": chunk,
        })
        path = handler.config.storage_root / f"file-{i}.md"
        # Force oldest first: file-0 has the smallest mtime, file-2 newest.
        os.utime(path, (1000.0 + i, 1000.0 + i))

    # Fourth file pushes us past the cap → file-0 must be evicted first.
    handler.handle({
        "command": "create",
        "path": "/memories/file-3.md",
        "file_text": chunk,
    })
    remaining = sorted(
        p.name for p in handler.config.storage_root.iterdir()
        if p.is_file()
    )
    assert "file-0.md" not in remaining, f"file-0 should have been evicted; got {remaining}"
    assert "file-3.md" in remaining

    # AC #5: at least one evict audit row exists.
    rows = _audit_rows(handler.config.progress_path)
    evicts = [r for r in rows if r.get("op") == "evict"]
    assert evicts, f"expected eviction audit row, got: {rows[-5:]}"


def test_eviction_refuses_oversize_write(handler: MemoryToolHandler) -> None:
    """AC #4 edge: a single write larger than the cap surfaces MemoryStorageFull."""
    huge = "y" * (2 * 1024 * 1024)  # 2MB > 1MB cap
    result = handler.handle({
        "command": "create",
        "path": "/memories/huge.md",
        "file_text": huge,
    })
    assert result["error"] == ERR_STORAGE_FULL
    assert "cap" in result["message"].lower()


# ── Test 4–7: AC #6 — tier filter for S/M/L/X ──────────────────────────


def test_tier_filter_S_and_M_recallable(handler: MemoryToolHandler) -> None:
    """AC #6: tier:S and tier:M auto-recallable; appear in directory view."""
    handler.handle({
        "command": "create",
        "path": "/memories/tier-S-known.md",
        "file_text": "---\ntier: S\n---\nbody\n",
    })
    handler.handle({
        "command": "create",
        "path": "/memories/tier-M-known.md",
        "file_text": "---\ntier: M\n---\nbody\n",
    })
    view = handler.handle({"command": "view", "path": "/memories"})
    names = sorted(e["name"] for e in view["entries"])
    assert names == ["tier-M-known.md", "tier-S-known.md"]


def test_tier_filter_L_requires_optin(
    tmp_storage: Path, progress_path: Path
) -> None:
    """AC #6: tier:L hidden without opt-in; visible with opt-in.

    Without opt-in:
      - directory view omits the L file (one tier_refuse audit row)
      - single-file view raises TierViolationUnauthorizedRecall code
    """
    h_no = MemoryToolHandler(MemoryToolConfig(
        fleet_id="fleet-tier-L",
        storage_root=tmp_storage,
        cap_mb=5,
        tier_l_optin=False,
        progress_path=progress_path,
        ticket_key="OP-851",
    ))
    h_no.handle({
        "command": "create",
        "path": "/memories/L-secret.md",
        "file_text": "---\ntier: L\n---\nsecret\n",
    })
    listing = h_no.handle({"command": "view", "path": "/memories"})
    names = [e["name"] for e in listing["entries"]]
    assert "L-secret.md" not in names, names

    view = h_no.handle({"command": "view", "path": "/memories/L-secret.md"})
    assert view["error"] == ERR_TIER_VIOLATION

    rows = _audit_rows(progress_path)
    assert any(r.get("op") == "tier_refuse" for r in rows)

    # With opt-in: visible.
    h_yes = MemoryToolHandler(MemoryToolConfig(
        fleet_id="fleet-tier-L",
        storage_root=tmp_storage,
        cap_mb=5,
        tier_l_optin=True,
        progress_path=progress_path,
        ticket_key="OP-851",
    ))
    listing2 = h_yes.handle({"command": "view", "path": "/memories"})
    names2 = [e["name"] for e in listing2["entries"]]
    assert "L-secret.md" in names2


def test_tier_filter_X_refused_even_with_optin(
    tmp_storage: Path, progress_path: Path
) -> None:
    """AC #6: tier:X never auto-recallable — operator approval required."""
    h = MemoryToolHandler(MemoryToolConfig(
        fleet_id="fleet-tier-X",
        storage_root=tmp_storage,
        cap_mb=5,
        tier_l_optin=True,  # even with L opt-in, X still refused
        progress_path=progress_path,
        ticket_key="OP-851",
    ))
    h.handle({
        "command": "create",
        "path": "/memories/tier-X-credentials.md",
        "file_text": "---\ntier: X\n---\nrotation key\n",
    })
    listing = h.handle({"command": "view", "path": "/memories"})
    names = [e["name"] for e in listing["entries"]]
    assert "tier-X-credentials.md" not in names

    view = h.handle({"command": "view", "path": "/memories/tier-X-credentials.md"})
    assert view["error"] == ERR_TIER_VIOLATION


def test_tier_classification_helpers() -> None:
    """AC #6 underlying invariant: front-matter > filename > default."""
    assert classify_tier(content="---\ntier: L\n---\n", filename="x.md") == "L"
    assert classify_tier(content=None, filename="tier-X-foo.md") == "X"
    assert classify_tier(content="hello", filename="random.md") == DEFAULT_TIER
    assert classify_tier(content="tier: bogus", filename="foo.md") == DEFAULT_TIER
    assert tier_is_recallable("S", tier_l_optin=False) is True
    assert tier_is_recallable("M", tier_l_optin=False) is True
    assert tier_is_recallable("L", tier_l_optin=False) is False
    assert tier_is_recallable("L", tier_l_optin=True) is True
    assert tier_is_recallable("X", tier_l_optin=True) is False


# ── Test 8: AC #5 — audit log shape matches B9 progress.txt schema ────


def test_audit_log_jsonl_shape(handler: MemoryToolHandler) -> None:
    """AC #5: every op writes one JSONL row with the documented fields."""
    handler.handle({
        "command": "create",
        "path": "/memories/audit-me.md",
        "file_text": "tiny\n",
    })
    handler.handle({"command": "view", "path": "/memories/audit-me.md"})
    rows = _audit_rows(handler.config.progress_path)
    # At minimum one write + one read row.
    ops = [r["op"] for r in rows]
    assert "write" in ops
    assert "read" in ops
    for r in rows:
        assert r["type"] == "memory_tool"
        assert r["tool"] == "memory"
        assert "key" in r
        assert "timestamp" in r
        assert r["ticket_key"] == "OP-851"


# ── Test 9: AC #7 — seeding + DoD memory.list("lesson:*") ──────────────


def test_seeding_and_lesson_list(
    tmp_path: Path, tmp_storage: Path, progress_path: Path
) -> None:
    """AC #7 + DoD: lessons seeded on first run; listable via lesson: prefix."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "L-OP-001-foo.md").write_text("# Foo\nbody\n")
    (lessons_dir / "L-OP-002-bar.md").write_text("# Bar\nbody\n")
    (lessons_dir / "ignored.txt").write_text("not a lesson\n")

    h = MemoryToolHandler(MemoryToolConfig(
        fleet_id="fleet-seed",
        storage_root=tmp_storage,
        cap_mb=5,
        progress_path=progress_path,
    ))
    count = seed_lessons_from(h, lessons_dir)
    assert count == 2

    seeded = list_seeded_lessons(h)
    assert sorted(seeded) == [
        "lesson:L-OP-001-foo.md",
        "lesson:L-OP-002-bar.md",
    ]

    # Re-seeding without overwrite is a no-op (operator hand-edit safety).
    again = seed_lessons_from(h, lessons_dir)
    assert again == 0


# ── Test 10: AC #2 error catalog — fallback unavailability surface ────


def test_fallback_path_unavailable_returns_error_code(
    handler: MemoryToolHandler,
) -> None:
    """AC error catalog: bad input → memory_bad_input; missing file → not_found."""
    bad = handler.handle({"command": "view", "path": "no-prefix.md"})
    assert bad["error"] == ERR_BAD_INPUT

    missing = handler.handle({
        "command": "view",
        "path": "/memories/never-existed.md",
    })
    assert missing["error"] == "not_found"

    unknown = handler.handle({"command": "delete-everything", "path": "/memories"})
    assert unknown["error"] == ERR_BAD_INPUT


# ── Test 11: AC #2 corruption recovery surface ─────────────────────────


def test_corruption_surfaces_as_error_code(
    tmp_storage: Path, progress_path: Path
) -> None:
    """AC error catalog: unreadable file → memory_corrupted code."""
    h = MemoryToolHandler(MemoryToolConfig(
        fleet_id="fleet-corrupt",
        storage_root=tmp_storage,
        cap_mb=5,
        progress_path=progress_path,
    ))
    # Plant a binary file under the storage root and try to view it.
    bad = tmp_storage / "bad.bin"
    bad.write_bytes(b"\xff\xfe\xfd not utf-8")
    result = h.handle({"command": "view", "path": "/memories/bad.bin"})
    assert result["error"] == "memory_corrupted"


# ── Test 12: build_memory_tool_handler integration helper ──────────────


def test_build_memory_tool_handler_returns_none_on_killswitch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC #2 abort: kill-switch flips build helper to None → caller falls back."""
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_REQUIRES_MA", "1")
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_ROOT", str(tmp_path))
    result = build_memory_tool_handler(fleet_id="killswitch-fleet")
    assert result is None


def test_build_memory_tool_handler_seeds_when_dir_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC #7: build helper seeds lessons when seed_dir is supplied."""
    monkeypatch.delenv("OMNISIGHT_MEMORY_TOOL_REQUIRES_MA", raising=False)
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_ROOT", str(tmp_path / "mem"))
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    (lessons / "L-OP-999-test.md").write_text("# Test\n")
    handler = build_memory_tool_handler(
        fleet_id="seed-fleet",
        seed_dir=lessons,
    )
    assert handler is not None
    seeded = list_seeded_lessons(handler)
    assert any(n.startswith("lesson:L-OP-999") for n in seeded)
