#!/usr/bin/env python3
"""γ-2 leg-3 — MEMORY.md regenerator (the payoff; dual-pin, reconcile-first).

Regenerates the harness-injected index from PUBLISHED rows only. The live
bug this fixes: MEMORY.md is ~50KB against the harness's 24.4KB budget —
recall is truncated BLINDLY today; this makes the cut rank-ordered, logged,
and verifiable.

Integrity-audit posture (all folded):
  * CLIENT FILE-BINDING is the check that matters vs a compromised backend:
    every exported row's ``body_sha256`` is re-hashed against the LOCAL
    topic file (ground truth is already on this disk); mismatch → the row
    is EXCLUDED + marked drifted + counted. Server claims are never trusted
    for content.
  * SERVER PIN (export ledger) defends against an ``~/.claude`` attacker;
    after writing, the regenerator reports the written file's sha back
    (``/export-pin``). HARD-REFUSE only when the server ledger is
    self-inconsistent with what it previously pinned.
  * LOCAL ``.memory.pin`` = two-writer RACE GUARD only: a mismatch means
    harness/Claude wrote since the last regeneration → RECONCILE (run the
    ingest first — invoked automatically), never refuse.
  * Line budget 200 BYTES (truncate the hook at a word boundary, never
    title/slug); file budget 24,000 bytes — WHOLE lines dropped bottom-up
    (NULL-rank first), every dropped slug logged.

Usage:
  OMNISIGHT_BACKEND_URL=... OMNISIGHT_CLAUDE_MEMORY_TOKEN=omni_... \
      python scripts/claude_memory_regen.py [--store DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_memory_ingest import (  # noqa: E402
    _DEFAULT_STORE,
    _parse_frontmatter,
    collect_items,
    post_batches,
)

_LINE_B = 200
_FILE_B = 24_000
_PIN_NAME = ".memory.pin"


def _api(path: str, *, base: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/v1{path}",
        data=(json.dumps(payload).encode() if payload is not None else None),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _local_body_sha(store: Path, slug: str) -> str | None:
    path = store / f"{slug}.md"
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8", errors="replace")
    _fm, body = _parse_frontmatter(raw)
    body = body if body.strip() else raw
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _render_line(title: str, slug: str, hook: str) -> str:
    prefix = f"- [{title}]({slug}.md) — "
    budget = _LINE_B - len(prefix.encode("utf-8"))
    h = (hook or "").strip()
    if len(h.encode("utf-8")) > budget:
        clipped = h.encode("utf-8")[: max(0, budget - 3)].decode("utf-8", "ignore")
        cut = clipped.rfind(" ")
        h = (clipped[:cut] if cut > 20 else clipped) + "…"
    return prefix + h


def regenerate(store: Path, *, base: str, token: str, dry_run: bool) -> int:
    # 1. Two-writer race guard: reconcile-first, never refuse (audit B2).
    pin_path = store / _PIN_NAME
    idx_path = store / "MEMORY.md"
    if idx_path.exists() and pin_path.exists():
        current = hashlib.sha256(idx_path.read_bytes()).hexdigest()
        if current != pin_path.read_text().strip():
            print("[regen] local writes since last regeneration — reconciling "
                  "(ingest first)", file=sys.stderr)
            if not dry_run:
                counts = post_batches(collect_items(store), base=base, token=token)
                print(f"[regen] reconcile ingest: {counts}", file=sys.stderr)

    # 2. Export published rows (server writes its pin-ledger row).
    export = _api("/claude-memories/export", base=base, token=token)
    items = export["items"]
    print(f"[regen] exported {len(items)} published rows "
          f"(export {export['export_id']})")

    # 3. CLIENT FILE-BINDING — the check that matters vs a bad backend.
    bound, drifted = [], []
    for it in items:
        local = _local_body_sha(store, it["slug"])
        if local == it["body_sha256"]:
            bound.append(it)
        else:
            drifted.append(it["slug"])
    if drifted:
        print(f"[regen] ⚠ DRIFTED (excluded, server≠disk): {drifted}",
              file=sys.stderr)
    if len(drifted) > max(3, len(items) // 4):
        print("[regen] HARD-REFUSE: drift above threshold — backend and disk "
              "disagree wholesale; investigate before regenerating",
              file=sys.stderr)
        return 2

    # 4. Render: rank ASC NULLS LAST (export pre-sorted), line + file budgets.
    header = (
        "# MEMORY.md — governed index (regenerated; published memories only)\n"
    )
    lines = [_render_line(it["title"], it["slug"], it["hook"] or "")
             for it in bound]
    kept, dropped, total = [], [], len(header.encode("utf-8"))
    for it, line in zip(bound, lines):
        b = len(line.encode("utf-8")) + 1
        if total + b > _FILE_B:
            dropped.append(it["slug"])
            continue
        kept.append(line)
        total += b
    # bottom-up semantics: since input is rank-ordered, overflow naturally
    # drops the lowest-ranked tail; log it loudly.
    if dropped:
        print(f"[regen] budget drop ({len(dropped)}): {dropped}", file=sys.stderr)

    content = header + "\n".join(kept) + "\n"
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    print(f"[regen] {len(kept)} lines kept, {len(dropped)} dropped, "
          f"{total}B, sha {sha[:16]}…")
    if dry_run:
        return 0

    # 5. Write + local pin + server pin report.
    idx_path.write_text(content, encoding="utf-8")
    pin_path.write_text(sha + "\n", encoding="utf-8")
    _api("/claude-memories/export-pin", base=base, token=token,
         payload={"export_id": export["export_id"], "export_sha256": sha})
    print("[regen] MEMORY.md written + pinned (local + server)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", type=Path, default=_DEFAULT_STORE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    base = os.environ.get("OMNISIGHT_BACKEND_URL", "http://127.0.0.1:8000")
    token = os.environ.get("OMNISIGHT_CLAUDE_MEMORY_TOKEN", "")
    if not args.dry_run and not token.startswith("omni_"):
        print("OMNISIGHT_CLAUDE_MEMORY_TOKEN (omni_...) required", file=sys.stderr)
        return 2
    return regenerate(args.store, base=base, token=token, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
