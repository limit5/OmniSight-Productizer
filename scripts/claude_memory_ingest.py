#!/usr/bin/env python3
"""γ-0 leg-3 — Claude-memory store ingest CLI (host-side, idempotent).

Walks ``~/.claude/projects/<project>/memory/``, parses BOTH frontmatter
variants, harvests the hand-curated rank+hook from MEMORY.md (wiring-audit
F5/F6 — the hook differs from the description; a year of curation is
preserved), and POSTs ≤32-item batches to ``/api/v1/claude-memories/ingest``
with an ``omni_`` bearer scoped to ``claude-memories``. Re-runs heal
partials (server-side upsert is idempotent by body sha).

Parser rules (audit-decided):
  * slug = filename stem, ALWAYS (``name`` frontmatter is never identity).
  * old flat variant: top-level ``type``/``originSessionId``; ``name`` = title.
  * new nested variant: ``metadata: {type, originSessionId}``.
  * mem_type fallback = filename prefix (one corpus file lacks type).
  * MEMORY.md line grammar ``- [Title](file.md) — hook`` (171/171 proven);
    rank = first occurrence, duplicates collapse; unindexed files rank=None.

Usage:
  OMNISIGHT_BACKEND_URL=... OMNISIGHT_CLAUDE_MEMORY_TOKEN=omni_... \
      python scripts/claude_memory_ingest.py [--store DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_DEFAULT_STORE = Path.home() / (
    ".claude/projects/-home-user-work-sora-OmniSight-Productizer/memory"
)
_INDEX_RE = re.compile(r"^- \[(?P<title>.+)\]\((?P<file>[^)]+\.md)\) — (?P<hook>.*)$")
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_TYPES = {"project", "feedback", "reference", "user"}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm: dict = {}
    meta: dict = {}
    in_meta = False
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if line.startswith("metadata:"):
            in_meta = True
            continue
        indented = line.startswith((" ", "\t"))
        key_line = line.strip()
        if ":" not in key_line:
            continue
        k, _, v = key_line.partition(":")
        k, v = k.strip(), v.strip().strip('"')
        if in_meta and indented:
            meta[k] = v
        else:
            in_meta = False
            fm[k] = v
    if meta:
        fm["metadata"] = meta
    return fm, text[m.end():]


def _mem_type(fm: dict, slug: str) -> str:
    t = (fm.get("metadata", {}) or {}).get("type") or fm.get("type") or ""
    if t in _TYPES:
        return t
    for prefix in _TYPES:
        if slug.startswith(prefix + "_"):
            return prefix
    return "project"


def harvest_index(store: Path) -> dict[str, tuple[int, str, str]]:
    """slug -> (rank, hook, title) from MEMORY.md; first occurrence wins."""
    out: dict[str, tuple[int, str, str]] = {}
    idx = store / "MEMORY.md"
    if not idx.exists():
        return out
    rank = 0
    for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _INDEX_RE.match(line.strip())
        if not m:
            continue
        slug = Path(m.group("file")).stem
        if slug in out:
            continue  # duplicates collapse to first occurrence
        rank += 1
        out[slug] = (rank, m.group("hook"), m.group("title"))
    return out


def collect_items(store: Path) -> list[dict]:
    index = harvest_index(store)
    items: list[dict] = []
    for path in sorted(store.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        slug = path.stem
        raw = path.read_text(encoding="utf-8", errors="replace")
        fm, body = _parse_frontmatter(raw)
        meta = fm.get("metadata", {}) or {}
        rank, hook, idx_title = index.get(slug, (None, None, None))
        title = (
            (fm.get("name") if "metadata" not in fm else None)  # old variant title
            or idx_title
            or (fm.get("description") or "").split("—")[0].strip()
            or slug.replace("_", " ")
        )
        items.append({
            "slug": slug,
            "title": title[:300],
            "description": fm.get("description", ""),
            "mem_type": _mem_type(fm, slug),
            "body": body if body.strip() else raw,
            "links": sorted({m.group(1).strip() for m in _LINK_RE.finditer(raw)}),
            "origin_session_id": meta.get("originSessionId") or fm.get("originSessionId"),
            "rank": rank,
            "hook": hook,
        })
    return items


def post_batches(items: list[dict], *, base: str, token: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for i in range(0, len(items), 32):
        payload = json.dumps({"items": items[i:i + 32]}).encode("utf-8")
        req = urllib.request.Request(
            f"{base.rstrip('/')}/api/v1/claude-memories/ingest",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for r in data.get("results", []):
            counts[r["action"]] = counts.get(r["action"], 0) + 1
            if r["action"] == "rejected":
                print(f"  REJECTED {r['slug']}: {r['reasons']}", file=sys.stderr)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", type=Path, default=_DEFAULT_STORE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = collect_items(args.store)
    ranked = sum(1 for x in items if x["rank"] is not None)
    print(f"collected {len(items)} items ({ranked} ranked from MEMORY.md)")
    if args.dry_run:
        for x in items[:5]:
            print(f"  {x['slug']} type={x['mem_type']} rank={x['rank']} "
                  f"body={len(x['body'])}B links={len(x['links'])}")
        return 0
    base = os.environ.get("OMNISIGHT_BACKEND_URL", "http://127.0.0.1:8000")
    token = os.environ.get("OMNISIGHT_CLAUDE_MEMORY_TOKEN", "")
    if not token.startswith("omni_"):
        print("OMNISIGHT_CLAUDE_MEMORY_TOKEN (omni_...) required", file=sys.stderr)
        return 2
    counts = post_batches(items, base=base, token=token)
    print(f"ingest result: {counts}")
    # Anti-hollow drift check: files vs accounted actions.
    accounted = sum(counts.values())
    if accounted != len(items):
        print(f"DRIFT: {len(items)} files vs {accounted} accounted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
