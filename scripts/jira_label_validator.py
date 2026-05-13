#!/usr/bin/env python3
"""Shared JIRA ticket label validator for filing-time tooling."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPO_ROOT / "docs" / "sop" / "jira-label-schema.yaml"

IMPLEMENTATION_PATH_RE = re.compile(
    r"\b(?:scripts|backend|deploy|frontend|db)/[A-Za-z0-9_/.-]+"
    r"(?:\.(?:py|tsx?|sql|yaml|yml|json|toml|ini|sh|service|timer))?\b"
)
OP_LINK_LABEL_RE = re.compile(r"^(?:parent|blocked-by|blockedby|blocks):OP-\d+$")


@dataclass(frozen=True)
class Issue:
    severity: str
    rule: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True)
class LabelSchema:
    namespaces: dict[str, Any]
    bare_labels: set[str]
    retired_exact: set[str]
    retired_patterns: tuple[re.Pattern[str], ...]
    legacy_rewrites: set[str]
    promote_to_field: set[str]
    promote_to_issuelink: set[str]


def load_schema(path: Path = DEFAULT_SCHEMA) -> LabelSchema:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")

    retired = raw.get("retired") or {}
    bare = raw.get("bare_labels") or []
    bare_names = {
        item["name"] if isinstance(item, dict) else item
        for item in bare
    }

    return LabelSchema(
        namespaces=raw.get("namespaces") or {},
        bare_labels={str(name) for name in bare_names},
        retired_exact={str(label) for label in (retired.get("exact") or [])},
        retired_patterns=tuple(re.compile(str(pattern)) for pattern in (retired.get("patterns") or [])),
        legacy_rewrites={str(label) for label in (raw.get("legacy_rewrites") or {})},
        promote_to_field={str(label) for label in (raw.get("promote_to_field") or {})},
        promote_to_issuelink={str(label) for label in (raw.get("promote_to_issuelink") or {})},
    )


def validate(labels: list[str], description: str) -> list[Issue]:
    """Validate ``labels`` against the OP label schema and ticket text."""
    schema = load_schema()
    issues: list[Issue] = []

    for label in labels:
        if not _is_canonical_label(label, schema):
            issues.append(
                Issue("error", "unknown-label", f"unknown or retired label: {label}")
            )

    by_prefix: dict[str, list[str]] = {}
    for label in labels:
        if ":" not in label:
            continue
        prefix, _value = label.split(":", 1)
        by_prefix.setdefault(prefix, []).append(label)

    for prefix, values in sorted(by_prefix.items()):
        namespace = schema.namespaces.get(prefix)
        if isinstance(namespace, dict) and namespace.get("multi") is False and len(values) > 1:
            issues.append(
                Issue(
                    "error",
                    "single-valued-namespace-repeated",
                    f"more than one {prefix}: label: {', '.join(values)}",
                )
            )

    if "type:meta" in labels and IMPLEMENTATION_PATH_RE.search(description):
        issues.append(
            Issue(
                "error",
                "meta-type-with-implementation",
                "type:meta cannot be filed with implementation paths in the description",
            )
        )

    if any(label.startswith("class:") for label in labels):
        missing = [
            prefix
            for prefix in ("area", "tier")
            if not any(label.startswith(f"{prefix}:") for label in labels)
        ]
        if missing:
            issues.append(
                Issue(
                    "warning",
                    "class-requires-routing",
                    f"class:* label should be paired with {', '.join(f'{prefix}:*' for prefix in missing)}",
                )
            )

    return issues


def _is_canonical_label(label: str, schema: LabelSchema) -> bool:
    if label in schema.bare_labels:
        return True
    if label in schema.retired_exact or any(pattern.match(label) for pattern in schema.retired_patterns):
        return False
    if label in schema.legacy_rewrites or label in schema.promote_to_field:
        return False
    if label in schema.promote_to_issuelink or OP_LINK_LABEL_RE.match(label):
        return False
    if ":" not in label:
        return False

    prefix, value = label.split(":", 1)
    namespace = schema.namespaces.get(prefix)
    if not isinstance(namespace, dict) or not value:
        return False

    spec = namespace.get("value")
    if isinstance(spec, dict):
        enum = spec.get("enum")
        if enum is not None and value not in {str(item) for item in enum}:
            return False
        pattern = spec.get("pattern")
        if pattern is not None and re.match(str(pattern), value) is None:
            return False
    return True


def format_issues(issues: list[Issue]) -> list[str]:
    return [f"{issue.severity.upper()}: {issue.rule}: {issue.message}" for issue in issues]
