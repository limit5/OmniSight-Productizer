#!/usr/bin/env python3
"""One-shot cleanup for OP-690: delete the duplicate ``[runner-pushed-to-gerrit]``
comment posted by the runner on 2026-05-07 ~01:41:51.

Background: per OP-691, codex called ``transition_to_under_review()`` itself
during work on OP-690, then the runner's Phase 1.5 post-CLI block ran the
same helper again. Two ``[runner-pushed-to-gerrit]`` comments landed on
OP-690 — one with codex's edited text (~01:41:07, KEEP) and one with the
runner's stale develop-tip text (~01:41:51, DELETE).

This script identifies and deletes the runner's stale duplicate. Idempotent:
re-running after the cleanup finds zero stale duplicates and exits 0.

Usage:
    python3 scripts/op690_dedupe_runner_comment.py            # dry-run (default)
    python3 scripts/op690_dedupe_runner_comment.py --apply    # actually delete

Auth: reuses jira_dispatch.make_client() with subscription-claude
credentials (claude-bot has comment-delete permission on its own posts and
on bot-posted comments per the OP project ACL).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch  # noqa: E402

TICKET = "OP-690"
TAG = "[runner-pushed-to-gerrit]"
KEEP_AUTHOR_HINT = "codex"  # codex-bot's edited comment is the keeper
TARGET_DATE = "2026-05-07"  # safety guard: only act on comments from the incident day


def _comment_text(comment: dict) -> str:
    """Flatten a JIRA comment ADF body to plain text for substring matching."""
    body = comment.get("body")
    if isinstance(body, str):
        return body
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                chunks.append(node.get("text", ""))
            for c in node.get("content", []) or []:
                walk(c)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return "".join(chunks)


def _author_display(comment: dict) -> str:
    return str((comment.get("author") or {}).get("displayName", ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete (default: dry-run, just print).")
    args = parser.parse_args()

    client = jira_dispatch.make_client("subscription-claude")
    print(f"[op690-cleanup] authenticated as {client.bot_email}")

    resp = jira_dispatch._request(
        client, "GET", f"/issue/{TICKET}/comment?maxResults=200"
    )
    comments = resp.get("comments", [])
    print(f"[op690-cleanup] fetched {len(comments)} comments on {TICKET}")

    matches = []
    for c in comments:
        text = _comment_text(c)
        if TAG not in text:
            continue
        created = str(c.get("created", ""))
        if not created.startswith(TARGET_DATE):
            continue
        matches.append({
            "id": c.get("id"),
            "created": created,
            "author": _author_display(c),
            "text_preview": text[:120].replace("\n", " "),
        })

    if not matches:
        print(f"[op690-cleanup] no {TAG} comments from {TARGET_DATE} found — nothing to do (idempotent re-run?)")
        return 0

    matches.sort(key=lambda m: m["created"])
    print(f"[op690-cleanup] found {len(matches)} {TAG} comments on {TARGET_DATE}:")
    for m in matches:
        print(f"  - id={m['id']} created={m['created']} author={m['author']}")
        print(f"    preview: {m['text_preview']}")

    if len(matches) <= 1:
        print(f"[op690-cleanup] only {len(matches)} match — no duplicates to remove")
        return 0

    # Per OP-691: KEEP the earliest one (codex's edited new-text), DELETE the later one(s).
    keeper = matches[0]
    deletes = matches[1:]
    print(f"\n[op690-cleanup] KEEPER: id={keeper['id']} (created={keeper['created']})")
    for d in deletes:
        print(f"[op690-cleanup] DELETE:  id={d['id']} (created={d['created']})")

    if not args.apply:
        print("\n[op690-cleanup] DRY-RUN — pass --apply to actually delete")
        return 0

    for d in deletes:
        try:
            jira_dispatch._request(
                client, "DELETE", f"/issue/{TICKET}/comment/{d['id']}"
            )
            print(f"[op690-cleanup] deleted comment id={d['id']}")
        except Exception as e:  # noqa: BLE001 — informational, do not abort entire cleanup
            print(f"[op690-cleanup] FAILED to delete id={d['id']}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 1

    print("[op690-cleanup] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
