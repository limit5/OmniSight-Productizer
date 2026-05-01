#!/usr/bin/env python3
"""B2 pilot — Anthropic Batch API for `backend/agents/*.py` architecture notes.

This is intentionally NOT a generic batch-runner (per Track B's B3 decision:
do not invest in batch tooling for TODO until natural homogeneous batches
emerge). It exists for one task: generate one-paragraph architecture notes
for each module under ``backend/agents/`` and persist them to
``docs/architecture/agents/``.

Usage:
    # Show what would be submitted, total payload, no API call
    python scripts/batch_arch_notes.py submit --dry-run

    # Submit (real API call — money spent here, ~50% off batch discount)
    python scripts/batch_arch_notes.py submit

    # Collect once Anthropic finishes (re-run safely; idempotent on retrieve)
    python scripts/batch_arch_notes.py collect <batch_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from anthropic import Anthropic


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_ROOT / "backend" / "agents"
OUTPUT_DIR = PROJECT_ROOT / "docs" / "architecture" / "agents"
MANIFEST_DIR = PROJECT_ROOT / "out" / "batch-arch-notes"

MODEL = "claude-opus-4-7"
MAX_TOKENS_PER_NOTE = 1000

PROMPT_TEMPLATE = """You are reading a Python module from the OmniSight-Productizer backend.
Write a concise architecture note for it in Markdown, structured as:

# {module_name}

**Purpose**: 1-2 sentences — what role does this module play in the system?

**Key types / public surface**: 3-5 bullet points naming the top-level
classes, functions, or constants that callers actually use. One short line
each.

**Key invariants**: 2-4 bullet points — non-obvious assumptions, contracts
the code depends on, or gotchas a future maintainer would want to know.
Pull these from comments, docstrings, or visible patterns. Skip the
obvious; favour the surprising.

**Cross-module touchpoints**: 1-3 bullet points — which other backend
modules does this one import from or get called by? (Use the imports +
visible patterns.)

Plain English. Don't speculate beyond the source. If something is unclear
from the code, say so explicitly. Cap the whole note at ~250 words.

—— BEGIN MODULE SOURCE ({module_path}) ——
{module_src}
—— END MODULE SOURCE ——
"""


def _list_modules() -> list[Path]:
    """Modules to process: every non-empty *.py under backend/agents/."""
    return sorted(p for p in AGENTS_DIR.glob("*.py") if p.stat().st_size > 0)


def _build_requests() -> tuple[list[dict], list[dict]]:
    """Build (anthropic-batch-payload, manifest-entries) for every module."""
    requests: list[dict] = []
    manifest_entries: list[dict] = []
    for path in _list_modules():
        custom_id = f"arch_{path.stem}"
        src = path.read_text(encoding="utf-8")
        prompt = PROMPT_TEMPLATE.format(
            module_name=path.stem,
            module_path=path.relative_to(PROJECT_ROOT),
            module_src=src,
        )
        params = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS_PER_NOTE,
            "messages": [{"role": "user", "content": prompt}],
        }
        requests.append({"custom_id": custom_id, "params": params})
        manifest_entries.append(
            {
                "custom_id": custom_id,
                "module_path": str(path.relative_to(PROJECT_ROOT)),
                "output_path": str(
                    (OUTPUT_DIR / f"{path.stem}.md").relative_to(PROJECT_ROOT)
                ),
                "src_chars": len(src),
            }
        )
    return requests, manifest_entries


def _check_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY 未設定", file=sys.stderr)
        sys.exit(1)


def cmd_submit(args: argparse.Namespace) -> None:
    requests, manifest_entries = _build_requests()
    payload_chars = sum(len(json.dumps(r, ensure_ascii=False)) for r in requests)
    print(f"📦 modules: {len(requests)}")
    print(f"📏 payload: {payload_chars / 1024:.1f} KB (~{payload_chars // 4} input tokens)")
    print()
    for e in manifest_entries:
        print(f"  - {e['custom_id']:30s} ← {e['module_path']:45s} ({e['src_chars']:>6} chars)")

    if args.dry_run:
        print("\n--dry-run set; not submitting.")
        return

    _check_api_key()
    client = Anthropic()
    print("\n⏳ submitting to Anthropic Batch API...")
    batch = client.messages.batches.create(requests=requests)
    batch_id = batch.id
    print(f"✅ submitted: batch_id={batch_id}")
    print(f"   processing_status={batch.processing_status}")

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_DIR / f"{batch_id}.json"
    manifest_path.write_text(
        json.dumps(
            {
                "batch_id": batch_id,
                "submitted_at_iso": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "model": MODEL,
                "entries": manifest_entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"   manifest: {manifest_path.relative_to(PROJECT_ROOT)}")
    print(f"\n👉 Collect later with:")
    print(f"   python scripts/batch_arch_notes.py collect {batch_id}")


def _result_text(result_obj: object) -> tuple[str, str]:
    """Extract (status, text) from one Anthropic batch result entry."""
    inner = getattr(result_obj, "result", None) or {}
    rtype = (
        getattr(inner, "type", None)
        if not isinstance(inner, dict)
        else inner.get("type")
    )
    if rtype != "succeeded":
        return rtype or "unknown", ""
    msg = (
        getattr(inner, "message", None)
        if not isinstance(inner, dict)
        else inner.get("message")
    )
    if msg is None:
        return rtype, ""
    content = (
        getattr(msg, "content", None)
        if not isinstance(msg, dict)
        else msg.get("content", [])
    ) or []
    parts: list[str] = []
    for block in content:
        btype = (
            getattr(block, "type", None)
            if not isinstance(block, dict)
            else block.get("type")
        )
        if btype == "text":
            text = (
                getattr(block, "text", "")
                if not isinstance(block, dict)
                else block.get("text", "")
            )
            if text:
                parts.append(text)
    return rtype, "".join(parts)


def cmd_collect(args: argparse.Namespace) -> None:
    _check_api_key()
    batch_id = args.batch_id
    manifest_path = MANIFEST_DIR / f"{batch_id}.json"
    if not manifest_path.exists():
        print(f"❌ manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {e["custom_id"]: e for e in manifest["entries"]}

    client = Anthropic()
    batch = client.messages.batches.retrieve(batch_id)
    print(f"📊 batch {batch_id}: {batch.processing_status}")
    rc = batch.request_counts
    succeeded = getattr(rc, "succeeded", 0) or 0
    errored = getattr(rc, "errored", 0) or 0
    canceled = getattr(rc, "canceled", 0) or 0
    expired = getattr(rc, "expired", 0) or 0
    processing = getattr(rc, "processing", 0) or 0
    print(
        f"   succeeded={succeeded} errored={errored} "
        f"canceled={canceled} expired={expired} processing={processing}"
    )

    if batch.processing_status != "ended":
        print(f"\n⏳ 尚未完成 — 等到 ended 後再 collect。")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    failed: list[tuple[str, str]] = []
    for result_obj in client.messages.batches.results(batch_id):
        custom_id = result_obj.custom_id
        entry = by_id.get(custom_id)
        if entry is None:
            print(f"⚠️ result for unknown custom_id: {custom_id}")
            continue
        status, text = _result_text(result_obj)
        if status != "succeeded" or not text.strip():
            failed.append((custom_id, status))
            continue
        out_path = PROJECT_ROOT / entry["output_path"]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        written += 1
    print(f"\n✅ wrote {written} notes")
    if failed:
        print(f"❌ {len(failed)} failed:")
        for cid, st in failed:
            print(f"   - {cid}: {st}")
    print(f"   output under: {OUTPUT_DIR.relative_to(PROJECT_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="B2 pilot — submit/collect batch arch notes."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_submit = sub.add_parser("submit", help="Build + submit batch")
    p_submit.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be submitted; no API call",
    )
    p_submit.set_defaults(func=cmd_submit)

    p_collect = sub.add_parser("collect", help="Collect results when ready")
    p_collect.add_argument("batch_id", help="batch_id printed by submit")
    p_collect.set_defaults(func=cmd_collect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
