"""MP.W10.1 -- provider cap detection from Z.1 rate-limit headers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from backend.agents.ratelimit_headers import (
    _PROVIDER_RATELIMIT_HEADERS,
    provider_ratelimit_family,
)


def ratelimit_cap_signal(
    provider_id: str,
    *chunks: str,
    kind: str,
) -> dict[str, int | str] | None:
    """Return a cap signal when Z.1-mapped headers show quota exhaustion.

    Subscription CLIs can surface upstream HTTP headers either as JSON fields or
    stderr text. This helper intentionally only understands providers present in
    ``_PROVIDER_RATELIMIT_HEADERS`` so MP cap detection stays aligned with the
    Z.1 header registry.
    """
    provider = provider_ratelimit_family(provider_id)
    if provider is None:
        return None
    mapping = _PROVIDER_RATELIMIT_HEADERS[provider]
    header_names = {key: value.lower() for key, value in mapping.items()}

    for headers in _candidate_header_dicts(chunks):
        signal = _signal_from_headers(headers, header_names, kind)
        if signal is not None:
            return signal
    text_signal = _signal_from_text("\n".join(chunks), header_names, kind)
    if text_signal is not None:
        return text_signal
    return None


def _candidate_header_dicts(chunks: tuple[str, ...]) -> Iterator[dict[str, Any]]:
    for payload in _json_payloads(*chunks):
        yield from _json_header_dicts(payload)


def _json_header_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if _looks_like_header_dict(value):
            yield value
        for key, child in value.items():
            if str(key).lower() in {"headers", "response_headers", "rate_limit"}:
                if isinstance(child, dict):
                    yield child
            yield from _json_header_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _json_header_dicts(item)


def _looks_like_header_dict(value: dict[str, Any]) -> bool:
    lower_keys = {str(key).lower() for key in value}
    for mapping in _PROVIDER_RATELIMIT_HEADERS.values():
        if any(header.lower() in lower_keys for header in mapping.values()):
            return True
    return False


def _signal_from_headers(
    headers: dict[str, Any],
    header_names: dict[str, str],
    kind: str,
) -> dict[str, int | str] | None:
    lower = {str(key).lower(): value for key, value in headers.items()}
    remaining = [
        _parse_int_or_none(lower.get(header_names["remaining_requests"])),
        _parse_int_or_none(lower.get(header_names["remaining_tokens"])),
    ]
    if not any(value is not None and value <= 0 for value in remaining):
        return None
    return _signal_payload(
        kind,
        retry_after=lower.get(header_names["retry_after"]),
        reset_at=lower.get(header_names["reset_at"]),
    )


def _signal_from_text(
    text: str,
    header_names: dict[str, str],
    kind: str,
) -> dict[str, int | str] | None:
    remaining = [
        _header_value_from_text(text, header_names["remaining_requests"]),
        _header_value_from_text(text, header_names["remaining_tokens"]),
    ]
    parsed_remaining = [_parse_int_or_none(value) for value in remaining]
    if not any(
        value is not None and value <= 0 for value in parsed_remaining
    ):
        return None
    return _signal_payload(
        kind,
        retry_after=_header_value_from_text(text, header_names["retry_after"]),
        reset_at=_header_value_from_text(text, header_names["reset_at"]),
    )


def _signal_payload(
    kind: str,
    *,
    retry_after: Any,
    reset_at: Any,
) -> dict[str, int | str]:
    out: dict[str, int | str] = {"kind": kind}
    retry_after_s = _parse_int_or_none(retry_after)
    if retry_after_s is not None:
        out["retry_after_s"] = max(retry_after_s, 0)
    reset_at_s = _parse_int_or_none(reset_at)
    if reset_at_s is not None:
        out["reset_at"] = max(reset_at_s, 0)
    return out


def _header_value_from_text(text: str, header_name: str) -> str | None:
    pattern = re.compile(
        rf"\b{re.escape(header_name)}\b[\"':=\s-]*(?P<value>[^\s,;}}]+)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match is None:
        return None
    return match.group("value").strip()


def _parse_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _json_payloads(*chunks: str) -> Iterator[Any]:
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        try:
            yield json.loads(text)
            continue
        except json.JSONDecodeError:
            pass
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


__all__ = ["ratelimit_cap_signal"]
