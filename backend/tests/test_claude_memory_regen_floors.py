"""OP-2729 — regeneration must REFUSE a bad export, not shrink the index silently.

`claude_memory_regen.py` rewrites `MEMORY.md`, which is unconditional system
context for every Claude Code session in this project. Two ways it could destroy
that index while reporting success:

* an **empty export** rendered `header + "" + "\\n"` — a bare-header MEMORY.md —
  and returned 0;
* the only existing guard, HARD-REFUSE, measures **drift** (server rows vs disk
  bodies) and its threshold `max(3, len(items) // 4)` is computed over the export
  itself, so a shrinking export shrinks its own guard. A server that simply
  returns fewer rows produces no drift at all and sails through.

Note what is NOT a bug: regeneration emitting far fewer lines than the current
hand-maintained index. It publishes PUBLISHED rows only, by design. That is
exactly why the shrink floor is a ratchet with an explicit opt-out rather than a
hard error — it encodes the operator's standing "hold the regen switch until
coverage is decent" decision instead of relying on someone remembering it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude_memory_regen.py"
_spec = importlib.util.spec_from_file_location("claude_memory_regen_floors", SCRIPT)
assert _spec and _spec.loader
regen = importlib.util.module_from_spec(_spec)
sys.modules["claude_memory_regen_floors"] = regen
_spec.loader.exec_module(regen)

HEADER_ONLY = "# MEMORY.md — governed index (regenerated; published memories only)\n"


def _index_lines(n: int) -> str:
    return "".join(f"- [T{i}](slug{i}.md) — hook {i}\n" for i in range(n))


def _fake_export(items: list[dict]) -> dict:
    return {"export_id": "exp-test", "items": items}


def _item(slug: str, body: str) -> dict:
    return {
        "slug": slug,
        "title": f"Title {slug}",
        "hook": f"hook for {slug}",
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    return tmp_path


def _wire(monkeypatch, items: list[dict], bodies: dict[str, str]) -> None:
    monkeypatch.setattr(regen, "_api", lambda path, **kw: _fake_export(items))
    monkeypatch.setattr(
        regen,
        "_local_body_sha",
        lambda store, slug: hashlib.sha256(bodies[slug].encode()).hexdigest()
        if slug in bodies
        else "",
    )


def test_empty_export_refuses_and_writes_nothing(store: Path, monkeypatch) -> None:
    idx = store / "MEMORY.md"
    idx.write_text(HEADER_ONLY + _index_lines(10), encoding="utf-8")
    before = idx.read_text(encoding="utf-8")
    _wire(monkeypatch, [], {})

    rc = regen.regenerate(store, base="http://x", token="t", dry_run=False)

    assert rc == 3
    assert idx.read_text(encoding="utf-8") == before, "MEMORY.md must be untouched"


def test_shrink_below_the_floor_refuses_and_writes_nothing(store: Path, monkeypatch) -> None:
    idx = store / "MEMORY.md"
    idx.write_text(HEADER_ONLY + _index_lines(20), encoding="utf-8")
    before = idx.read_text(encoding="utf-8")
    bodies = {f"s{i}": f"body {i}" for i in range(3)}
    _wire(monkeypatch, [_item(s, b) for s, b in bodies.items()], bodies)

    rc = regen.regenerate(store, base="http://x", token="t", dry_run=False)

    assert rc == 3, "3 entries against a 20-entry index must refuse"
    assert idx.read_text(encoding="utf-8") == before


def test_shrink_is_permitted_with_the_explicit_flag(store: Path, monkeypatch) -> None:
    idx = store / "MEMORY.md"
    idx.write_text(HEADER_ONLY + _index_lines(20), encoding="utf-8")
    bodies = {f"s{i}": f"body {i}" for i in range(3)}
    _wire(monkeypatch, [_item(s, b) for s, b in bodies.items()], bodies)
    monkeypatch.setattr(regen, "_api", lambda path, **kw: _fake_export(
        [_item(s, b) for s, b in bodies.items()]) if "export" in path and "pin" not in path else {})

    rc = regen.regenerate(store, base="http://x", token="t", dry_run=False, allow_shrink=True)

    assert rc == 0
    assert len(regen._INDEX_LINE_RE.findall(idx.read_text(encoding="utf-8"))) == 3


def test_first_run_has_no_baseline_so_the_ratchet_does_not_fire(store: Path, monkeypatch) -> None:
    """No MEMORY.md yet — a ratchet against nothing must not block bootstrapping."""
    bodies = {f"s{i}": f"body {i}" for i in range(2)}
    monkeypatch.setattr(regen, "_api", lambda path, **kw: _fake_export(
        [_item(s, b) for s, b in bodies.items()]) if "export" in path and "pin" not in path else {})
    monkeypatch.setattr(
        regen, "_local_body_sha",
        lambda st, slug: hashlib.sha256(bodies[slug].encode()).hexdigest(),
    )

    rc = regen.regenerate(store, base="http://x", token="t", dry_run=False)

    assert rc == 0
    assert (store / "MEMORY.md").exists()


def test_healthy_regeneration_still_writes(store: Path, monkeypatch) -> None:
    idx = store / "MEMORY.md"
    idx.write_text(HEADER_ONLY + _index_lines(4), encoding="utf-8")
    bodies = {f"s{i}": f"body {i}" for i in range(6)}
    monkeypatch.setattr(regen, "_api", lambda path, **kw: _fake_export(
        [_item(s, b) for s, b in bodies.items()]) if "export" in path and "pin" not in path else {})
    monkeypatch.setattr(
        regen, "_local_body_sha",
        lambda st, slug: hashlib.sha256(bodies[slug].encode()).hexdigest(),
    )

    rc = regen.regenerate(store, base="http://x", token="t", dry_run=False)

    assert rc == 0
    assert len(regen._INDEX_LINE_RE.findall(idx.read_text(encoding="utf-8"))) == 6


def test_dry_run_reports_the_refusal_rather_than_hiding_it(store: Path, monkeypatch) -> None:
    """The floors run BEFORE the dry-run return, so an operator sees the verdict
    before committing to a real run."""
    idx = store / "MEMORY.md"
    idx.write_text(HEADER_ONLY + _index_lines(20), encoding="utf-8")
    bodies = {"s0": "body 0"}
    _wire(monkeypatch, [_item("s0", "body 0")], bodies)

    assert regen.regenerate(store, base="http://x", token="t", dry_run=True) == 3


def test_count_index_lines_ignores_header_and_prose(tmp_path: Path) -> None:
    f = tmp_path / "MEMORY.md"
    f.write_text(HEADER_ONLY + "some prose\n" + _index_lines(5) + "\ntrailing note\n")
    assert regen._count_index_lines(f) == 5
    assert regen._count_index_lines(tmp_path / "absent.md") == 0
