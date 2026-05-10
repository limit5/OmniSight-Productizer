"""Feature-list AC parser for B6 submit-review checklist (OP-835).

Source-of-truth: ``docs/audit/2026-05-11-sprint-abc-master-plan.md`` §2.8.
Companion FSM: ``docs/architecture/sdk-runner-sprint-b-error-handling.md`` §B6.

Two AC formats are supported and detected from a ticket description:

* Feature-list JSON — a ``## Feature list (JSON)`` heading followed by a
  fenced ``json`` code block. Each item must be
  ``{"id": "FL<n>", "description": str, "verify": str}``.
* Legacy freeform — an ``## Acceptance criteria`` / ``Acceptance Criteria``
  section with markdown bullets. Each bullet becomes one feature-list item
  via :func:`up_convert_legacy`. If no bullets are found, the caller must
  escalate ``legacy_up_convert_fail`` per B6 AC #6.

The parser is intentionally side-effect free — file staging, model
injection, and JIRA comments live in :mod:`backend.agents.submit_checklist`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Iterable


FEATURE_LIST_HEADING_RE = re.compile(
    r"^##\s+Feature\s+list\s+\(JSON\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
LEGACY_AC_HEADING_RE = re.compile(
    r"^##\s+Acceptance\s+criteria\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(.+?)```",
    re.DOTALL | re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.+?)\s*$", re.MULTILINE)
_FL_ID_RE = re.compile(r"^FL\d+$")


class FeatureListParseError(Exception):
    """Raised when a feature-list section is present but unparseable.

    Surfaced to the FSM as ``format_unparseable`` per B6 error catalog —
    caller falls back to legacy mode.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LegacyUpConvertError(Exception):
    """Raised when legacy freeform AC contains no parseable bullets.

    Surfaced as ``legacy_up_convert_fail`` — submit must be blocked and
    operator review required.
    """


@dataclass(frozen=True)
class FeatureItem:
    """One row of the feature-list checklist.

    ``verify`` is free-text; the ``test_X passes`` vs ``manual:operator-check``
    distinction is advisory metadata for the model, not enforced here.
    """

    id: str
    description: str
    verify: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ACDetection:
    """Result of :func:`detect_and_parse`.

    ``mode`` is one of ``"feature_list_json"`` / ``"legacy_freeform"``.
    ``items`` is the (possibly up-converted) feature list. Empty only when
    ``mode == "legacy_freeform"`` and the caller has not yet up-converted —
    the parsed-from-JSON path always returns a non-empty list (a JSON
    block parseable to an empty list is rejected as
    ``format_unparseable``).
    """

    mode: str
    items: tuple[FeatureItem, ...]


def _extract_section_body(description: str, heading_re: re.Pattern[str]) -> str | None:
    """Return text from heading_re's match up to the next ``## `` heading."""
    m = heading_re.search(description)
    if not m:
        return None
    start = m.end()
    next_heading = re.search(r"^##\s+\S", description[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(description)
    return description[start:end]


def parse_feature_list_json(description: str) -> tuple[FeatureItem, ...]:
    """Parse the ``## Feature list (JSON)`` section.

    Raises :class:`FeatureListParseError` (mapped to ``format_unparseable``)
    when the section exists but the JSON, schema, or item count are bad.
    Returns ``()`` when the section is absent.
    """
    section = _extract_section_body(description, FEATURE_LIST_HEADING_RE)
    if section is None:
        return ()

    fence = _FENCED_JSON_RE.search(section)
    raw = (fence.group(1) if fence else section).strip()
    if not raw:
        raise FeatureListParseError("feature-list section is empty")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeatureListParseError(f"JSON decode failed: {exc}") from exc

    if not isinstance(data, list):
        raise FeatureListParseError("feature-list root must be a JSON array")
    if not data:
        raise FeatureListParseError("feature-list array is empty")

    items: list[FeatureItem] = []
    seen_ids: set[str] = set()
    for idx, raw_item in enumerate(data):
        if not isinstance(raw_item, dict):
            raise FeatureListParseError(f"item[{idx}] is not a JSON object")
        missing = {"id", "description", "verify"} - raw_item.keys()
        if missing:
            raise FeatureListParseError(
                f"item[{idx}] missing keys: {sorted(missing)}"
            )
        item_id = raw_item["id"]
        if not isinstance(item_id, str) or not _FL_ID_RE.match(item_id):
            raise FeatureListParseError(
                f"item[{idx}] id={item_id!r} must match FL<n>"
            )
        if item_id in seen_ids:
            raise FeatureListParseError(f"duplicate item id {item_id!r}")
        seen_ids.add(item_id)
        desc = raw_item["description"]
        verify = raw_item["verify"]
        if not isinstance(desc, str) or not desc.strip():
            raise FeatureListParseError(f"item[{idx}] description must be non-empty str")
        if not isinstance(verify, str) or not verify.strip():
            raise FeatureListParseError(f"item[{idx}] verify must be non-empty str")
        items.append(FeatureItem(id=item_id, description=desc.strip(), verify=verify.strip()))
    return tuple(items)


def up_convert_legacy(description: str) -> tuple[FeatureItem, ...]:
    """Convert legacy ``## Acceptance criteria`` bullets to feature-list items.

    Each bullet becomes ``FL<n>`` with ``verify="manual:operator-check"``
    (default for unannotated legacy AC). Raises :class:`LegacyUpConvertError`
    when no bullets are found (``legacy_up_convert_fail``).
    """
    section = _extract_section_body(description, LEGACY_AC_HEADING_RE)
    if section is None:
        raise LegacyUpConvertError("no '## Acceptance criteria' section found")
    bullets: list[str] = []
    for m in _BULLET_RE.finditer(section):
        text = _strip_checkbox(m.group(1)).strip()
        if text:
            bullets.append(text)
    if not bullets:
        raise LegacyUpConvertError("no bullets under '## Acceptance criteria'")
    return tuple(
        FeatureItem(
            id=f"FL{i + 1}",
            description=text,
            verify="manual:operator-check",
        )
        for i, text in enumerate(bullets)
    )


def _strip_checkbox(text: str) -> str:
    """Drop a leading ``[ ]`` / ``[x]`` markdown checkbox if present."""
    return re.sub(r"^\[[ xX]\]\s*", "", text)


def detect_and_parse(description: str) -> ACDetection:
    """Detect AC format and return parsed items.

    Behaviour matches B6 AC #1 / #6 — JSON mode wins when the heading is
    present; otherwise legacy is up-converted. JSON parse failures bubble
    up as :class:`FeatureListParseError` so the FSM can map them to
    ``format_unparseable`` and fall back to legacy mode.
    Legacy with no parseable bullets raises :class:`LegacyUpConvertError`
    (``legacy_up_convert_fail``).
    """
    if FEATURE_LIST_HEADING_RE.search(description):
        items = parse_feature_list_json(description)
        return ACDetection(mode="feature_list_json", items=items)
    items = up_convert_legacy(description)
    return ACDetection(mode="legacy_freeform", items=items)


def items_as_jsonable(items: Iterable[FeatureItem]) -> list[dict]:
    """Helper for serialising into checklist-injection payloads."""
    return [it.as_dict() for it in items]
