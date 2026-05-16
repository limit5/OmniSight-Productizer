#!/usr/bin/env python3
"""Lint v2-AlertBridge Prometheus rule files before promotion.

Usage:
    python3 scripts/promote-alert-rule.py deploy/prometheus/rules/runner_atlas.yml
    python3 scripts/promote-alert-rule.py --file deploy/prometheus/rules/runner_atlas.yml
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.alerting.bridge import (
    AlertEnvelope,
    CardinalityLimitExceeded,
    CardinalityValidator,
    DedupeKeyGen,
    REQUIRED_ANNOTATIONS,
    SEVERITY_CHANNEL_MAP,
)


REQUIRED_LABELS = ("severity", "area", "family", "defense_dimension")
CRITICAL_LABEL_PLACEHOLDER = "__promote_alert_rule_lint__"


def lint_rule_file(path: Path) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    rules: list[dict[str, Any]] = []

    try:
        doc = _load_yaml(path)
    except OSError as exc:
        return _result(path, [], [{"rule": "<file>", "message": str(exc)}])
    except yaml.YAMLError as exc:
        return _result(path, [], [{"rule": "<file>", "message": f"yaml parse error: {exc}"}])

    for group_index, group in enumerate(_as_list(doc.get("groups")) if isinstance(doc, dict) else []):
        group_name = group.get("name", f"group[{group_index}]") if isinstance(group, dict) else f"group[{group_index}]"
        if not isinstance(group, dict):
            errors.append({"rule": group_name, "message": "group entry must be a mapping"})
            continue
        for rule_index, rule in enumerate(_as_list(group.get("rules"))):
            rule_name = _rule_name(group_name, rule_index, rule)
            rule_errors = _lint_rule(rule_name, rule)
            errors.extend({"rule": rule_name, "message": message} for message in rule_errors)
            rules.append({"alert": rule_name, "ok": not rule_errors})

    if not rules and not errors:
        errors.append({"rule": "<file>", "message": "no Prometheus alert rules found"})

    return _result(path, rules, errors)


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _lint_rule(rule_name: str, rule: Any) -> list[str]:
    if not isinstance(rule, dict):
        return ["rule entry must be a mapping"]

    errors: list[str] = []
    labels = _string_map(rule.get("labels"))
    annotations = _string_map(rule.get("annotations"))
    bridge = rule.get("__bridge")
    expr = str(rule.get("expr", ""))

    if not labels:
        errors.append("labels must be a mapping")
    if not annotations:
        errors.append("annotations must be a mapping")
    if not isinstance(bridge, dict):
        errors.append("__bridge must be a mapping")
        bridge = {}

    for label in REQUIRED_LABELS:
        if not labels.get(label):
            errors.append(f"missing required label: {label}")

    missing_annotations = [key for key in REQUIRED_ANNOTATIONS if not annotations.get(key)]
    for annotation in missing_annotations:
        errors.append(f"missing required annotation: {annotation}")

    severity = labels.get("severity", "")
    if severity and severity not in SEVERITY_CHANNEL_MAP:
        errors.append(f"unknown severity: {severity}")

    defense_dimension = labels.get("defense_dimension", "")
    if defense_dimension and defense_dimension not in {"D1", "D2", "D3", "D4", "D5"}:
        errors.append(f"unknown defense_dimension: {defense_dimension}")

    critical_labels = _string_list(bridge.get("critical_labels"))
    if not critical_labels:
        errors.append("__bridge.critical_labels must declare at least one label")

    caps = _int_map(bridge.get("cardinality_caps"))
    if bridge.get("cardinality_caps") is not None and caps is None:
        errors.append("__bridge.cardinality_caps must map labels to integer caps")
        caps = {}

    for critical_label in critical_labels:
        if critical_label not in labels and critical_label not in expr:
            errors.append(f"critical label is not declared by labels or expr: {critical_label}")

    if errors:
        return errors

    envelope_labels = {
        **labels,
        **{label: labels.get(label, CRITICAL_LABEL_PLACEHOLDER) for label in critical_labels},
    }

    try:
        dedupe_key = DedupeKeyGen.generate(
            str(rule.get("alert", rule_name)),
            labels["family"],
            envelope_labels,
            critical_labels,
        )
        envelope = AlertEnvelope(
            alertname=str(rule.get("alert", rule_name)),
            severity=severity,  # type: ignore[arg-type]
            area=labels["area"],
            family=labels["family"],
            defense_dimension=defense_dimension,  # type: ignore[arg-type]
            labels=envelope_labels,
            annotations=annotations,
            dedupe_key=dedupe_key,
            fired_at=datetime.now(UTC),
            resolved_at=None,
            critical_labels=tuple(critical_labels),
        )
        CardinalityValidator().validate(envelope, caps=caps)
    except CardinalityLimitExceeded as exc:
        errors.append(str(exc))
    except ValueError as exc:
        errors.append(str(exc))

    return errors


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _int_map(value: Any) -> dict[str, int] | None:
    if value is None:
        return {}
    if not isinstance(value, dict):
        return None
    caps: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(item, int):
            return None
        caps[str(key)] = item
    return caps


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _rule_name(group_name: str, rule_index: int, rule: Any) -> str:
    if isinstance(rule, dict) and rule.get("alert"):
        return str(rule["alert"])
    return f"{group_name}.rules[{rule_index}]"


def _result(path: Path, rules: list[dict[str, Any]], errors: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "ok": not errors,
        "file": str(path),
        "rules": rules,
        "errors": errors,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lint a v2-AlertBridge Prometheus rule YAML file.")
    parser.add_argument("path", nargs="?", help="Prometheus rule YAML file to lint")
    parser.add_argument("--file", dest="file_path", help="Prometheus rule YAML file to lint")
    args = parser.parse_args(argv)
    if not args.path and not args.file_path:
        parser.error("provide a rule YAML path or --file")
    if args.path and args.file_path:
        parser.error("provide either positional path or --file, not both")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    path = Path(args.file_path or args.path)
    payload = lint_rule_file(path)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
