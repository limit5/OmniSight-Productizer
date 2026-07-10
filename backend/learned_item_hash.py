"""OP-2565 U4-A1 — canonical content hash for learned-item versions.

Pure helper, no I/O, no DB. This hash is what the U4 ledger's dedup and
immutability key on: the per-audience partial unique indexes of the
version ledger (migration 0258) treat two payloads with equal hashes as
the same learned item within a scope.

The hash covers ONLY the content fields: provenance and timestamp keys
(``created_at``, ``created_by``, ``source_change_id``, ``verified_at``)
and entire ``provenance`` / ``_meta`` sub-objects are stripped at every
nesting level before hashing, so re-observing the same content with
different provenance yields the SAME hash, while any content edit yields
a NEW hash (= a new version row; versions are never mutated in place).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_EXCLUDED_KEYS = frozenset(
    {
        "created_at",
        "created_by",
        "source_change_id",
        "verified_at",
        "provenance",
        "_meta",
    }
)


def _strip_provenance(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_provenance(item)
            for key, item in value.items()
            if key not in _EXCLUDED_KEYS
        }
    if isinstance(value, list):
        return [_strip_provenance(item) for item in value]
    return value


def canonical_content_hash(payload: dict) -> str:
    """sha256 hex of the canonical JSON of the content fields of *payload*.

    Deterministic: sorted keys, compact separators, provenance/timestamp
    keys excluded (see ``_EXCLUDED_KEYS``) at every nesting depth.
    """
    canonical = json.dumps(
        _strip_provenance(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
