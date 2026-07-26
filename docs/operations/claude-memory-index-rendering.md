# MEMORY.md index rendering — why `_render_line` sanitises

**Ticket:** OP-2729 · **Scope label:** `scope:failed-units-2026-07-25`

## The loop that makes a line break dangerous

`MEMORY.md` is **unconditional system context** for every Claude Code session in this project, and
`_INDEX_RE` in `scripts/claude_memory_ingest.py` re-parses each of its lines as an index entry:

```
^- \[(?P<title>.+)\]\((?P<file>[^)]+\.md)\) — (?P<hook>.*)$
```

So a line break inside an interpolated field does not merely look untidy. It **manufactures a
second, well-formed index entry**, that entry lands in the system context of every subsequent
session, and the next reconcile ingest reads it back as if a human had written it. Reproduced
before the fix: a `\n` in `hook` produced a second line that `_INDEX_RE` matched.

Both interpolated fields are reachable:

- **`hook`** comes straight from the API, and `now_touch` (`backend/routers/claude_memories.py`)
  rewrites it on an **already-published** row with no lint, no new version row and no transition
  event — and the regenerator's file-binding check compares `body_sha256`, which does not cover
  `hook`, so that check is structurally blind to it.
- **`title`** is interpolated on the same line. Publish-gated, so weaker, but the same shape.

## What the renderer guarantees

`_render_line` is the choke point every rendered field passes through, so the guard lives there
rather than at the ingest boundary. Its postconditions:

1. **Single line.** `_one_line()` collapses line breaks using `str.splitlines()` as the authority
   rather than a hand-written character class — it splits on exactly what Python, and therefore the
   `re.M` ingest parser and every text reader in this pipeline, treats as a boundary: `\n`, `\r`,
   `\r\n`, `\v`, `\f`, `\x1c`-`\x1e`, `\x85`, and the unicode separators U+2028 / U+2029.
2. **Within `_LINE_B` (200) bytes.** The title is clipped first, against the space left once the
   fixed markup, the slug and a minimum hook are accounted for. The previous version clipped only
   `hook`, so a long title blew the cap outright — a 250-character title rendered **268 bytes**.
3. **Never a split multibyte character** — clipping is on a character boundary, preferring a word
   break, with an ellipsis when anything was removed.

An `assert` re-checks (1) on the way out: it is the property the function exists to provide.

## Verification of record — 2026-07-26

All three defects reproduced first, then fixed, then regression-checked against the real store:
rendering the live prod export through the old and new renderer produced a **byte-identical**
MEMORY.md (`sha f34f63f726931519`, 37 lines, 7240 B). Legitimate content is untouched; only the
pathological cases change. 25 tests pin the behaviour, including every line-break form above and
the multibyte clipping.

## What this does NOT fix

The write path that lets `hook` be rewritten on a published row without review is a separate
defect, tracked as part of the leg-3 ingest hardening (SP-3b). This change makes the *rendering*
safe regardless of what reaches it — defence at the last mile, not a substitute for the gate.
