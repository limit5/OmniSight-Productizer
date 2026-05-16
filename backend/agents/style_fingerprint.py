"""RPG.W3.2 -- deterministic style fingerprint generator.

ADR-0008 defines the Character Card ``style_fingerprint`` as a hash over
the last N tasks' ``commit-style / test-pattern / refactor-tendency`` tuple.
This module owns only that pure generator. Storage, scheduling, and Character
Card writes are intentionally left to callers so W3.3 can wire the daily cron
without mixing persistence into this helper.

Module-global state audit (SOP Step 1): this module defines immutable
constants, dataclasses, and pure helper functions only. It has no mutable
module state, no clock reads, and no filesystem or database access.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping


DEFAULT_STYLE_WINDOW = 20
STYLE_FINGERPRINT_VERSION = "rpg-style-v1"
STYLE_FINGERPRINT_HEX_LENGTH = 64

_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class TaskStyleSignals:
    """Style signals extracted from one completed task."""

    commit_style: str
    test_pattern: str
    refactor_tendency: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "commit_style",
            _required("commit_style", self.commit_style),
        )
        object.__setattr__(
            self,
            "test_pattern",
            _required("test_pattern", self.test_pattern),
        )
        object.__setattr__(
            self,
            "refactor_tendency",
            _required("refactor_tendency", self.refactor_tendency),
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "TaskStyleSignals":
        """Build a sample from a dict-like task-history row."""
        return cls(
            commit_style=str(data.get("commit_style", "")),
            test_pattern=str(data.get("test_pattern", "")),
            refactor_tendency=str(data.get("refactor_tendency", "")),
        )

    def canonical(self) -> dict[str, str]:
        """Return the normalized tuple used by the fingerprint hash."""
        return {
            "commit_style": _canonical_axis(self.commit_style),
            "test_pattern": _canonical_axis(self.test_pattern),
            "refactor_tendency": _canonical_axis(self.refactor_tendency),
        }


def compute_style_fingerprint(
    tasks: list[TaskStyleSignals] | tuple[TaskStyleSignals, ...],
    *,
    last_n: int = DEFAULT_STYLE_WINDOW,
) -> str:
    """Return the SHA-256 style fingerprint for the newest ``last_n`` tasks.

    ``tasks`` is ordered oldest -> newest. Only the selected task triples are
    hashed; task ids, timestamps, and agent ids are deliberately excluded so the
    result reflects style rather than identity metadata.
    """
    _validate_window(last_n)
    if not tasks:
        return ""

    selected = tuple(tasks[-last_n:])
    payload = {
        "version": STYLE_FINGERPRINT_VERSION,
        "samples": [sample.canonical() for sample in selected],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def compute_style_fingerprint_from_rows(
    rows: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
    *,
    last_n: int = DEFAULT_STYLE_WINDOW,
) -> str:
    """Return a style fingerprint from dict-like task-history rows."""
    return compute_style_fingerprint(
        tuple(TaskStyleSignals.from_mapping(row) for row in rows),
        last_n=last_n,
    )


def _required(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} is required")
    return stripped


def _canonical_axis(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip().lower())


def _validate_window(last_n: int) -> None:
    if isinstance(last_n, bool) or not isinstance(last_n, int):
        raise TypeError("last_n must be an int")
    if last_n < 1:
        raise ValueError("last_n must be >= 1")
