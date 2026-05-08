---
title: Lessons Learned — index
---

# Lessons Learned

Each engineering lesson lives as a single file under `docs/sop/lessons/L-*.md`. This page is the human-friendly index that the OP-787 dynamic indexer will populate as part of the full migration. The spike below shows one entry rendered.

## Index (spike)

| ID | Date | Ticket | Lesson |
|---|---|---|---|
| L-OP-785 | 2026-05-08 | OP-785 | [MkDocs spike — framework decision](L-OP-785-mkdocs-spike.md) |

## How JIRA cross-linking works on this site

The hook at `docs-site/hooks/jira_links.py` rewrites bare ticket references in page markdown into Atlassian browse links. So a sentence that mentions OP-785 in prose lands on the rendered page as a clickable link to the ticket — no manual `[OP-785](https://...)` markup required at write-time.

Inside fenced code blocks (`OP-785` here) and inline `code` spans, the rewrite is suppressed so example output stays verbatim:

```text
OP-785  ← stays bare inside code blocks
```
