"""γ-2 (leg-3) — regenerator: line budget, file-binding drift, thresholds."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from claude_memory_regen import _render_line, regenerate  # noqa: E402


def test_render_line_budget_truncates_hook_not_title():
    line = _render_line("T" * 50, "slug_x", "word " * 100)
    assert len(line.encode("utf-8")) <= 200
    assert line.startswith("- [" + "T" * 50 + "](slug_x.md) — ")
    assert line.endswith("…")
    short = _render_line("T", "s", "small hook")
    assert short == "- [T](s.md) — small hook"


def _mk_store(tmp_path, bodies):
    for slug, body in bodies.items():
        (tmp_path / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: d\nmetadata:\n  type: project\n---\n{body}",
            encoding="utf-8",
        )
    return tmp_path


def _sha(body):
    return hashlib.sha256(body.encode()).hexdigest()


def test_regenerate_binds_drifts_and_writes(tmp_path, monkeypatch):
    store = _mk_store(tmp_path, {"a_ok": "body A", "b_drift": "body B"})
    calls = {}

    def _fake_api(path, *, base, token, payload=None):
        calls.setdefault(path, []).append(payload)
        if path == "/claude-memories/export":
            return {"export_id": "e1", "previous_export_sha256": None,
                    "items": [
                        {"slug": "a_ok", "title": "A", "hook": "h", "rank": 1,
                         "revision": 1, "body_sha256": _sha("body A"),
                         "mem_type": "project"},
                        {"slug": "b_drift", "title": "B", "hook": "h", "rank": 2,
                         "revision": 1, "body_sha256": "0" * 64,
                         "mem_type": "project"},
                    ]}
        return {"pinned": payload["export_sha256"]}

    import claude_memory_regen as cr

    monkeypatch.setattr(cr, "_api", _fake_api)
    rc = regenerate(store, base="http://x", token="omni_t", dry_run=False)
    assert rc == 0
    content = (store / "MEMORY.md").read_text()
    assert "a_ok.md" in content
    assert "b_drift" not in content  # drifted row excluded (file-binding)
    pin = (store / ".memory.pin").read_text().strip()
    assert pin == hashlib.sha256(content.encode()).hexdigest()
    assert calls["/claude-memories/export-pin"][0]["export_sha256"] == pin


def test_regenerate_hard_refuses_on_wholesale_drift(tmp_path, monkeypatch):
    store = _mk_store(tmp_path, {})
    import claude_memory_regen as cr

    items = [{"slug": f"s{i}", "title": "T", "hook": "", "rank": i,
              "revision": 1, "body_sha256": "0" * 64, "mem_type": "project"}
             for i in range(8)]
    monkeypatch.setattr(cr, "_api", lambda path, **kw: {
        "export_id": "e", "previous_export_sha256": None, "items": items,
    })
    rc = regenerate(store, base="http://x", token="omni_t", dry_run=True)
    assert rc == 2  # backend and disk disagree wholesale


def test_regenerate_reconciles_on_pin_mismatch(tmp_path, monkeypatch):
    store = _mk_store(tmp_path, {"a_ok": "body A"})
    (store / "MEMORY.md").write_text("- [old](a_ok.md) — old\n")
    (store / ".memory.pin").write_text("f" * 64 + "\n")  # mismatch
    import claude_memory_regen as cr

    ingested = {"n": 0}
    monkeypatch.setattr(cr, "collect_items", lambda s: [{"slug": "a_ok"}])
    monkeypatch.setattr(cr, "post_batches",
                        lambda items, base, token: ingested.update(n=len(items)) or {"unchanged": 1})
    monkeypatch.setattr(cr, "_api", lambda path, **kw: {
        "export_id": "e", "previous_export_sha256": None,
        "items": [{"slug": "a_ok", "title": "A", "hook": "h", "rank": 1,
                   "revision": 1, "body_sha256": _sha("body A"),
                   "mem_type": "project"}],
    } if path == "/claude-memories/export" else {"pinned": "x"})
    rc = regenerate(store, base="http://x", token="omni_t", dry_run=False)
    assert rc == 0
    assert ingested["n"] == 1  # reconcile ingest ran BEFORE regeneration
