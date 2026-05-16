#!/usr/bin/env python3
"""Auto-inject ``mutex_with`` entries for hot-file edits (OP-1124).

When two tickets both touch a hot file (e.g. ``backend/agents/jira_dispatch.py``)
in the same window, their patchsets routinely conflict in Gerrit —
typically in the import block alone. The runner already serialises
sibling pickups via the ``mutex_with`` Prerequisites declarations
consumed by :func:`backend.agents.jira_dispatch.find_mutex_holders`
(OP-687, table-backed in OP-1108). What was missing was the *discipline*
of declaring ``mutex:<path>`` on every ticket that edits a hot file.

This module supplies that discipline. Given a ticket description text:

1. :func:`extract_files_touched` scans for repo-relative path tokens
   (``backend/...``, ``scripts/...``, ``auto-runner-*.py``, etc.).
2. :func:`required_mutex_labels` intersects the touched set with the
   hot-file allow-list loaded from
   ``config/hot_files_mutex_allowlist.yaml``.
3. :func:`inject_mutex_with` rewrites (or creates) the Prerequisites
   YAML block to include ``mutex:<path>`` entries — idempotent on
   re-filing.

The single public entry point used by filing tooling is
:func:`auto_inject_for_filing` which returns ``(new_description, added)``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HOT_FILES_ALLOWLIST_PATH = REPO_ROOT / "config" / "hot_files_mutex_allowlist.yaml"

# Path-token recognisers. The first matches any
# repo-relative-looking ``dir/sub/file.ext`` token rooted at a known
# top-level directory; the second catches the bare ``auto-runner-*.py``
# entry points that sit at the repo root.
_DIR_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:backend|scripts|deploy|frontend|db|app|components|hooks|tests|"
    r"governance_engine|infra|config|tools|omnisight|installer|packages)"
    r"/[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}"
    r"(?![A-Za-z0-9])"
)
_BARE_RUNNER_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])auto-runner-[A-Za-z0-9_-]+\.py(?![A-Za-z0-9])"
)

# Locates the existing Prerequisites YAML fence so we can rewrite it.
# Mirrors PREREQS_RE in backend/agents/jira_dispatch.py but captures the
# fence wrappers separately so we can splice in a new YAML body.
_PREREQS_BLOCK_RE = re.compile(
    r"(?P<header>##\s+Prerequisites\s*\n+)"
    r"(?P<fence_open>```yaml\s*\n)"
    r"(?P<body>.*?)"
    r"(?P<fence_close>\n```)",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class InjectionResult:
    """Outcome of an :func:`auto_inject_for_filing` pass."""

    description: str
    added_mutexes: tuple[str, ...]
    already_present: tuple[str, ...]
    hot_files_touched: tuple[str, ...]


def load_hot_files(path: Path = HOT_FILES_ALLOWLIST_PATH) -> set[str]:
    """Load the operator-editable hot-file allow-list."""
    if not path.exists():
        return set()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("hot_files") or []
    return {str(item).strip() for item in items if str(item).strip()}


def extract_files_touched(description: str) -> set[str]:
    """Return every repo-relative path token mentioned in the description.

    Permissive on purpose: any path-shaped token in any section
    (``## Files / Paths``, ``## Scope``, AC bullets, inline prose) is
    considered "touched". The downstream intersection with the
    hot-file allow-list keeps the false-positive surface bounded.
    """
    touched: set[str] = set()
    for match in _DIR_PATH_RE.finditer(description):
        touched.add(match.group(0))
    for match in _BARE_RUNNER_RE.finditer(description):
        touched.add(match.group(0))
    return touched


def required_mutex_labels(
    touched: Iterable[str], hot_files: Iterable[str]
) -> list[str]:
    """Compute the ``mutex:<path>`` labels required for ``touched``.

    Returns a deterministically sorted list so the rewrite is stable
    across re-filings (idempotency contract).
    """
    hot_set = set(hot_files)
    hits = sorted({f"mutex:{path}" for path in touched if path in hot_set})
    return hits


def _parse_yaml_block(body: str) -> dict[str, list]:
    data = yaml.safe_load(body) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _render_yaml_block(data: dict) -> str:
    # Preserve insertion order, emit empty lists as ``key: []`` to match
    # the seed-ticket house style (see scripts/jira_seed_example_tickets.py).
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


_DEFAULT_PREREQ_KEYS = (
    "blocks_on",
    "soft_prereqs",
    "mutex_with",
    "schema_locks",
    "live_state_requires",
    "external_blockers",
)


def inject_mutex_with(
    description: str, mutex_labels: list[str]
) -> tuple[str, list[str], list[str]]:
    """Splice ``mutex_labels`` into the Prerequisites YAML block.

    Returns ``(new_description, added, already_present)``. Idempotent:
    re-running with the same ``mutex_labels`` returns ``added == []``.

    If no Prerequisites block exists, a minimal one is appended.
    """
    if not mutex_labels:
        return description, [], []

    match = _PREREQS_BLOCK_RE.search(description)
    if match is None:
        # Build a fresh block. Keep formatting identical to the seed
        # examples so downstream parsers (parse_prerequisites) see a
        # familiar shape.
        new_block_data: dict[str, list] = {key: [] for key in _DEFAULT_PREREQ_KEYS}
        new_block_data["mutex_with"] = list(mutex_labels)
        rendered = _render_yaml_block(new_block_data)
        suffix = "" if description.endswith("\n") else "\n"
        appended = (
            f"{suffix}\n## Prerequisites\n\n```yaml\n{rendered}\n```\n"
        )
        return description + appended, list(mutex_labels), []

    body = match.group("body")
    data = _parse_yaml_block(body)
    # Ensure all canonical keys are present so output shape stays stable
    for key in _DEFAULT_PREREQ_KEYS:
        data.setdefault(key, [])

    existing = data.get("mutex_with") or []
    if not isinstance(existing, list):
        existing = []
    existing_set = {str(x) for x in existing}

    added: list[str] = []
    already_present: list[str] = []
    for label in mutex_labels:
        if label in existing_set:
            already_present.append(label)
        else:
            existing.append(label)
            existing_set.add(label)
            added.append(label)

    if not added:
        return description, [], already_present

    data["mutex_with"] = existing
    new_body = _render_yaml_block(data)
    new_block = (
        match.group("header")
        + match.group("fence_open")
        + new_body
        + match.group("fence_close")
    )
    new_description = description[: match.start()] + new_block + description[match.end():]
    return new_description, added, already_present


def auto_inject_for_filing(
    description: str,
    *,
    hot_files: set[str] | None = None,
) -> InjectionResult:
    """Top-level entry point used by filing tooling.

    Loads the allow-list (override via ``hot_files`` for tests),
    derives required mutex labels from the description, and returns
    the rewritten description plus the diff. Idempotent.
    """
    files_touched = extract_files_touched(description)
    if hot_files is None:
        hot_files = load_hot_files()
    hot_hits = sorted(files_touched & hot_files)
    required = required_mutex_labels(hot_hits, hot_files)
    new_description, added, already_present = inject_mutex_with(description, required)
    return InjectionResult(
        description=new_description,
        added_mutexes=tuple(added),
        already_present=tuple(already_present),
        hot_files_touched=tuple(hot_hits),
    )
