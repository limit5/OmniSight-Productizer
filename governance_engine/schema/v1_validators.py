"""Pure field-level predicates for the v1 ticket contract schema."""

from __future__ import annotations

import re


EXTERNAL_ARTIFACT_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
L1_EXCLUSIVE_REASON_VALUES = frozenset(
    {
        "key-material",
        "meta-closure",
        "release-tag-governance",
        "override",
        "roster-mutation",
        "governance-engine-foundation",
    }
)


def validate_required_path_shape(path: str) -> bool:
    """Return True for forward-slash POSIX-style relative paths."""
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    return all(part and part != ".." for part in parts)


def validate_external_artifact_name(name: str) -> bool:
    """Return True for RFC-1123-ish artifact registry names."""
    return re.fullmatch(EXTERNAL_ARTIFACT_NAME_PATTERN, name) is not None


def validate_l1_exclusive_reason_value(reason: str) -> bool:
    """Return True for ADR-0033 known reasons or custom free-form values."""
    return isinstance(reason, str)


__all__ = [
    "EXTERNAL_ARTIFACT_NAME_PATTERN",
    "L1_EXCLUSIVE_REASON_VALUES",
    "validate_external_artifact_name",
    "validate_l1_exclusive_reason_value",
    "validate_required_path_shape",
]
