#!/usr/bin/env python3
"""[OP-1066] Hourly Anthropic-API surface-drift canary (SP-B-X-008 / C8).

The runner depends on Anthropic's built-in tools (``text_editor_20250728``
et al.) — both the request payload AND response block shape. A
non-backwards-compatible API change would silently break the
dispatcher. This canary asserts the response shape matches the pinned
fixture at ``tests/fixtures/anthropic-api-tool-schema-pinned.json``; on
drift it fires ``Severity.DEGRADED`` via ``operator_notifier.notify``.

Re-pinning is gated on an explicit operator commit — see
``docs/sop/runbooks/anthropic-api-drift-response.md``.

Exit codes: 0 = no drift, 2 = DRIFT (alert fired), 3 = env error.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("omnisight.anthropic_api_canary")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PINNED_SCHEMA_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "anthropic-api-tool-schema-pinned.json"
)
DEFAULT_MODEL = "claude-sonnet-4-6"
CANARY_FIXTURE_PATH = "/tmp/omnisight-anthropic-canary-fixture.txt"
DRIFT_NOTIFICATION_CODE = "anthropic_api_schema_drift"

EXIT_OK = 0
EXIT_DRIFT = 2
EXIT_ENV_ERROR = 3


def _is_mapping(x: Any) -> bool:
    return isinstance(x, dict)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if _is_mapping(obj):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _has(obj: Any, key: str) -> bool:
    if _is_mapping(obj):
        return key in obj
    return getattr(obj, key, None) is not None


def _find_tool_use_block(response: Any) -> Any | None:
    content = _get(response, "content", []) or []
    if not isinstance(content, list):
        return None
    for block in content:
        if _get(block, "type") == "tool_use":
            return block
    return None


def diff_observed_against_pinned(
    observed: Any, pinned: dict[str, Any]
) -> list[str]:
    """Pure diff. Empty list = no drift; otherwise human-readable strings
    that surface in the alert context."""

    inv = pinned.get("response_invariants", {})
    diffs: list[str] = []

    for key in inv.get("top_level_required_keys", []):
        if not _has(observed, key):
            diffs.append(f"missing top-level key: {key}")
    for key, expected in inv.get("top_level_fixed_values", {}).items():
        actual = _get(observed, key)
        if actual != expected:
            diffs.append(f"top-level {key}={actual!r} (expected {expected!r})")

    allowed_stops = inv.get("stop_reason_allowed", [])
    if allowed_stops:
        stop_reason = _get(observed, "stop_reason")
        if stop_reason not in allowed_stops:
            diffs.append(
                f"stop_reason={stop_reason!r} not in allowed set {sorted(allowed_stops)}"
            )

    # tool_use shape only enforced when a tool_use block actually exists.
    # ``end_turn`` without a tool call is a model decision, not API drift.
    tool_block = _find_tool_use_block(observed)
    if tool_block is not None:
        spec = inv.get("tool_use_block", {})
        for key in spec.get("required_keys", []):
            if not _has(tool_block, key):
                diffs.append(f"tool_use missing key: {key}")
        expected_name = spec.get("name_must_equal")
        if expected_name and _get(tool_block, "name") != expected_name:
            diffs.append(
                f"tool_use.name={_get(tool_block, 'name')!r} (expected {expected_name!r})"
            )
        tool_input = _get(tool_block, "input")
        if not _is_mapping(tool_input):
            diffs.append(
                f"tool_use.input is not a mapping: type={type(tool_input).__name__}"
            )
        else:
            for key in spec.get("input_required_keys_for_view", []):
                if key not in tool_input:
                    diffs.append(f"tool_use.input missing key: {key}")
            allowed = spec.get("input_allowed_command_values", [])
            if allowed and tool_input.get("command") not in allowed:
                diffs.append(
                    f"tool_use.input.command={tool_input.get('command')!r} "
                    f"not in allowed set {sorted(allowed)}"
                )

    usage = _get(observed, "usage")
    for key in inv.get("usage_required_keys", []):
        if usage is None or _get(usage, key) is None:
            diffs.append(f"usage missing key: {key}")

    return diffs


def load_pinned_schema(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ("schema_version", "tool_type", "tool_name",
                "request_tool_payload", "response_invariants")
    for key in required:
        if key not in data:
            raise ValueError(f"pinned schema missing required key: {key}")
    return data


def _build_messages_request(pinned: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": os.environ.get("OMNISIGHT_ANTHROPIC_CANARY_MODEL", DEFAULT_MODEL),
        "max_tokens": 512,
        "tools": [pinned["request_tool_payload"]],
        "messages": [{
            "role": "user",
            "content": (
                f"Use the {pinned['tool_name']} tool with the 'view' command on the "
                f"file at path {CANARY_FIXTURE_PATH!r}. Reply with only the tool call."
            ),
        }],
    }


def call_anthropic_messages(pinned: dict[str, Any]) -> Any:
    """Live API call. Lazy-imports the SDK so unit tests can avoid it."""
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("anthropic SDK not installed") from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    return client.messages.create(**_build_messages_request(pinned))


def _response_to_dict(response: Any) -> dict[str, Any]:
    if _is_mapping(response):
        return dict(response)
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(response, method_name, None)
        if callable(method):
            try:
                result = method()
                if _is_mapping(result):
                    return result
            except Exception:  # noqa: BLE001
                pass
    return {"_repr": str(response)}


def _fire_drift_alert(
    *,
    pinned: dict[str, Any],
    diffs: list[str],
    observed: dict[str, Any],
    notifier: Any,
) -> None:
    # Import Severity unconditionally so the test spy can compare against
    # the real enum. The notify callable itself is injectable for tests.
    from backend.agents.operator_notifier import Severity
    if notifier is None:
        from backend.agents.operator_notifier import notify as _notify
    else:
        _notify = notifier

    diff_summary = "; ".join(diffs[:6]) + ("…" if len(diffs) > 6 else "")
    tool = pinned.get("tool_name", "unknown")
    _notify(
        Severity.DEGRADED,
        DRIFT_NOTIFICATION_CODE,
        message=f"Tool {tool} schema diff: {diff_summary}",
        context={
            "tool": tool,
            "tool_type": pinned.get("tool_type"),
            "schema_version": pinned.get("schema_version"),
            "pinned_at": pinned.get("pinned_at"),
            "diff_count": len(diffs),
            "expected_schema": pinned.get("response_invariants"),
            "actual_schema": observed,
            "runbook": "docs/sop/runbooks/anthropic-api-drift-response.md",
        },
    )


def run_canary(
    pinned_path: Path = DEFAULT_PINNED_SCHEMA_PATH,
    dry_run_response_path: Path | None = None,
    api_caller: Any = None,
    notifier: Any = None,
) -> int:
    try:
        pinned = load_pinned_schema(pinned_path)
    except FileNotFoundError:
        logger.error("pinned schema file not found at %s", pinned_path)
        return EXIT_ENV_ERROR
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("pinned schema invalid: %s", exc)
        return EXIT_ENV_ERROR

    if dry_run_response_path is not None:
        try:
            response = json.loads(dry_run_response_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("dry-run response file unreadable: %s", exc)
            return EXIT_ENV_ERROR
    else:
        caller = api_caller or call_anthropic_messages
        try:
            response = caller(pinned)
        except RuntimeError as exc:
            logger.error("anthropic API call refused (env): %s", exc)
            return EXIT_ENV_ERROR
        except Exception as exc:  # noqa: BLE001 — surface as drift
            logger.exception("anthropic API call raised; treating as drift")
            _fire_drift_alert(
                pinned=pinned,
                diffs=[f"api_call_raised: {type(exc).__name__}: {exc}"],
                observed={"_exception": f"{type(exc).__name__}: {exc}"},
                notifier=notifier,
            )
            return EXIT_DRIFT

    diffs = diff_observed_against_pinned(response, pinned)
    if not diffs:
        logger.info("anthropic-api-canary OK — pinned schema matches observed response")
        return EXIT_OK

    _fire_drift_alert(
        pinned=pinned,
        diffs=diffs,
        observed=_response_to_dict(response),
        notifier=notifier,
    )
    return EXIT_DRIFT


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("OMNISIGHT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description="OmniSight Anthropic-API drift canary (OP-1066).")
    p.add_argument("--pinned-schema", type=Path, default=DEFAULT_PINNED_SCHEMA_PATH)
    p.add_argument("--dry-run-response", type=Path, default=None)
    p.add_argument("--check-fixture", action="store_true",
                   help="Validate the pinned-schema fixture parses, then exit.")
    args = p.parse_args(argv)

    if args.check_fixture:
        try:
            load_pinned_schema(args.pinned_schema)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"pinned schema invalid: {exc}", file=sys.stderr)
            return EXIT_ENV_ERROR
        print(f"pinned schema OK: {args.pinned_schema}")
        return EXIT_OK

    return run_canary(
        pinned_path=args.pinned_schema,
        dry_run_response_path=args.dry_run_response,
    )


if __name__ == "__main__":
    sys.exit(main())
