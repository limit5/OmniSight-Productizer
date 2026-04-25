"""R20 Phase 0 — Doc corpus loader with audience classification.

Walks ``docs/**/*.md``, parses optional ``audience:`` frontmatter, and
exposes a queryable list of ``Doc`` records.

Audience classification (the entire point of this module):

  - ``public``   — visible to anyone, including anonymous chat sessions
  - ``operator`` — visible to logged-in operators (the default role)
  - ``admin``    — visible to admin-role users only
  - ``internal`` — NEVER exposed via chat regardless of role; this is
                   how we keep ``docs/design/*`` (architecture
                   decisions, security model, threat docs) out of the
                   RAG retrieval path

Default classification when frontmatter is absent uses the doc's
directory (see ``_DIR_DEFAULTS``). A doc without explicit frontmatter
and in an unrecognised directory falls to ``internal`` — fail-closed,
so adding a new doc to a new dir won't accidentally leak it.

Operators tagging docs explicitly should add to the front of the file:

  ---
  audience: operator
  ---
  # My doc
  ...

Frontmatter format is the standard YAML-ish ``---`` fence; we only
parse the ``audience`` line so we don't pull a YAML dependency.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Locating the docs root. Defaults to ``docs`` relative to repo root
# but can be overridden in tests / containerised deployments where the
# repo isn't laid out at $PWD.
DOC_ROOT = Path(os.environ.get("OMNISIGHT_DOCS_ROOT", "docs"))

VALID_AUDIENCES = ("public", "operator", "admin", "internal")

# Directory-based defaults when a doc has no explicit ``audience:``
# frontmatter. First match wins; anything not matched falls to
# ``internal`` (fail-closed). Update this list when adding a new
# top-level docs directory whose audience is non-internal — and add
# explicit frontmatter to individual files when they should override.
_DIR_DEFAULTS: list[tuple[str, str]] = [
    ("docs/operator/", "operator"),
    ("docs/operations/", "operator"),
    ("docs/integrations/", "operator"),
    ("docs/sop/", "operator"),
    ("docs/ops/", "operator"),
    ("docs/design/", "internal"),
    ("docs/spec/", "admin"),
    ("docs/phase-", "internal"),
    ("docs/priority-y/", "internal"),
    ("docs/security/", "internal"),
    ("README.md", "public"),
]


@dataclass(frozen=True)
class Doc:
    """One classified doc: path / title / audience / body."""
    path: str       # repo-relative path, e.g. "docs/operator/getting-started.md"
    title: str      # H1 heading, or filename stem if no H1
    audience: str   # one of VALID_AUDIENCES
    body: str       # full body with frontmatter stripped


# Matches the YAML-ish frontmatter block at the very top of a file.
_FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
# Single-line ``audience: <value>`` extractor inside the frontmatter.
_AUDIENCE_LINE = re.compile(r"^audience:\s*(\w+)\s*$", re.MULTILINE)
# H1 extractor for the title fallback.
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _classify_default(rel_path: str) -> str:
    """Map a repo-relative path → default audience by directory."""
    for prefix, audience in _DIR_DEFAULTS:
        if rel_path.startswith(prefix):
            return audience
    # Fail-closed: unrecognised directory → internal. Adding new dirs
    # requires an explicit ``_DIR_DEFAULTS`` entry, otherwise the doc
    # silently disappears from RAG (which is the desired behaviour —
    # better than silently being public).
    return "internal"


def _parse_doc(path: Path, root: Path) -> Doc:
    """Read one .md file, parse frontmatter + title, return a Doc."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Compute path relative to the repo root (not docs root) so the
    # citation path matches what an operator would type to open the
    # file: ``docs/operator/foo.md`` rather than ``operator/foo.md``.
    try:
        rel = str(path.relative_to(root.parent))
    except ValueError:
        rel = str(path)

    fm_match = _FM_RE.match(raw)
    if fm_match:
        fm_body, body = fm_match.group(1), fm_match.group(2)
        am = _AUDIENCE_LINE.search(fm_body)
        if am and am.group(1) in VALID_AUDIENCES:
            audience = am.group(1)
        else:
            audience = _classify_default(rel)
    else:
        body = raw
        audience = _classify_default(rel)

    title_match = _TITLE_RE.search(body)
    title = title_match.group(1) if title_match else path.stem
    return Doc(path=rel, title=title, audience=audience, body=body)


def load_corpus(root: Path | None = None) -> list[Doc]:
    """Load every .md doc under ``root`` (default ``DOC_ROOT``).

    Returns docs sorted by path so retrieval is deterministic — useful
    for unit tests that snapshot retrieval results.
    """
    root = root or DOC_ROOT
    if not root.exists():
        return []
    return [_parse_doc(p, root) for p in sorted(root.rglob("*.md"))]


def visible_audiences_for(role: str) -> frozenset[str]:
    """Return the set of audiences a user with this role can see.

    Permission is cumulative — admin sees admin + operator + public,
    operator sees operator + public, anonymous sees public only.
    ``internal`` is **never** in any returned set: it's only readable
    by humans clicking through ``docs/`` directly, never via chat.
    """
    role = (role or "anonymous").lower()
    if role == "admin":
        return frozenset({"public", "operator", "admin"})
    if role == "operator":
        return frozenset({"public", "operator"})
    return frozenset({"public"})
