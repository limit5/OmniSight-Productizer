"""MkDocs hook — auto-link JIRA ticket references in page markdown.

Registered via ``hooks:`` in ``mkdocs.yml``. Rewrites bare ``OP-NNN``
tokens into links to ``https://soraapp.atlassian.net/browse/OP-NNN``.

Skips:
- tokens already inside an ``[label](url)`` link target
- tokens inside fenced code blocks or inline ``code`` spans
- the leading ``L-OP-NNN-...`` lesson-id pattern (the lesson filename
  prefix carries OP-NNN but should not auto-link to the ticket from
  inside its own page slug)
"""
from __future__ import annotations

import re

JIRA_BASE_URL = "https://soraapp.atlassian.net/browse/"
PROJECT_KEYS = ("OP",)

_TICKET_PATTERN = re.compile(
    r"(?<![A-Za-z0-9-])(" + "|".join(PROJECT_KEYS) + r")-(\d+)(?![A-Za-z0-9-])"
)


def _replace_outside_code(text: str) -> str:
    parts = re.split(r"(```.*?```|`[^`\n]+`)", text, flags=re.DOTALL)
    for index, part in enumerate(parts):
        if index % 2 == 1:
            continue
        parts[index] = _replace_outside_links(part)
    return "".join(parts)


def _replace_outside_links(text: str) -> str:
    parts = re.split(r"(\[[^\]]*\]\([^)]*\))", text)
    for index, part in enumerate(parts):
        if index % 2 == 1:
            continue
        parts[index] = _TICKET_PATTERN.sub(
            lambda m: f"[{m.group(0)}]({JIRA_BASE_URL}{m.group(0)})",
            part,
        )
    return "".join(parts)


def on_page_markdown(markdown, *, page, config, files):  # noqa: ARG001  (MkDocs hook signature)
    return _replace_outside_code(markdown)
