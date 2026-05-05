"""WP.1.5 -- Block share redaction-mask helpers.

The ``blocks.redaction_mask`` JSONB column marks the exact sub-regions
that must be masked before a Block is shared through WP.9
``shareable_objects``.  This module keeps the masking logic pure so the
future WP.9 router can call it without coupling share creation to a DB
writer.

Module-global / cross-worker state audit: constants and regex objects are
immutable policy data.  Helpers allocate request-local copies only, do not
cache Presidio engines, and never decrypt KS.1 envelopes.  Every worker
derives the same redaction result from the same Block payload.

Read-after-write timing audit: pure in-memory transform; no DB writes,
locks, queues, or caches are introduced.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from backend.models import Block
from backend.security.pii_auto_mask import mask_log_text
from backend.security.pii_presidio import AnalyzerLike, AnonymizerLike, PresidioConfig
from backend.security.secret_filter import redact as redact_secrets


BLOCK_SHARE_REGIONS: tuple[str, ...] = (
    "command",
    "output",
    "metadata",
    "screenshots",
)
REDACTION_SECRET = "secret"
REDACTION_PII = "pii"
REDACTION_CUSTOMER_IP = "customer_ip"
REDACTION_KS_ENVELOPE = "ks_envelope"
REDACTION_REASONS: tuple[str, ...] = (
    REDACTION_SECRET,
    REDACTION_PII,
    REDACTION_CUSTOMER_IP,
    REDACTION_KS_ENVELOPE,
)

_PAYLOAD_REGION_KEYS: Mapping[str, tuple[str, ...]] = {
    "command": ("command", "commands", "argv"),
    "output": ("output", "stdout", "stderr"),
    "screenshots": ("screenshot", "screenshots", "image", "images"),
}
_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)
_KS_ENVELOPE_KEYS = frozenset({
    "fmt",
    "alg",
    "dek",
    "tid",
    "nonce_b64",
    "ciphertext_b64",
})
_KS_BOUNDARY_KEYS = frozenset({"ciphertext", "dek_ref"})


@dataclass(frozen=True)
class BlockRedactionResult:
    """Redacted share projection plus the JSONB mask that explains it."""

    block: dict[str, Any]
    redaction_mask: dict[str, Any]
    regions: tuple[str, ...]
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "redaction_mask": self.redaction_mask,
            "regions": list(self.regions),
            "changed": self.changed,
        }


def normalise_share_regions(regions: Iterable[str] | None) -> tuple[str, ...]:
    """Return supported share regions, deduped in canonical order."""

    if regions is None:
        return BLOCK_SHARE_REGIONS
    requested = {str(item).strip() for item in regions if str(item).strip()}
    return tuple(region for region in BLOCK_SHARE_REGIONS if region in requested)


def redact_block_for_share(
    block: Block,
    *,
    regions: Iterable[str] | None = None,
    pii_config: PresidioConfig | None = None,
    pii_analyzer: AnalyzerLike | None = None,
    pii_anonymizer: AnonymizerLike | None = None,
) -> BlockRedactionResult:
    """Return a share-safe Block projection and JSONB redaction mask.

    ``block.redaction_mask`` remains authoritative for operator-marked
    redactions.  Automatic detectors add secret, PII, customer-IP, and
    KS.1 envelope-boundary markers for the selected share regions only.
    """

    selected = normalise_share_regions(regions)
    source = block.model_dump()
    redacted = copy.deepcopy(source)
    mask: dict[str, Any] = {}
    changed = False

    for path, value in _walk_selected_region_values(source, selected):
        explicit_reason = _explicit_reason_for_path(block.redaction_mask, path)
        next_value, reasons = _redact_value(
            value,
            explicit_reason=explicit_reason,
            pii_config=pii_config,
            pii_analyzer=pii_analyzer,
            pii_anonymizer=pii_anonymizer,
        )
        if reasons:
            _set_path(redacted, path, next_value)
            mask[path] = _mask_reason_value(reasons)
            changed = True

    return BlockRedactionResult(
        block=redacted,
        redaction_mask=mask,
        regions=selected,
        changed=changed,
    )


def _walk_selected_region_values(
    block: Mapping[str, Any],
    selected: tuple[str, ...],
) -> Iterable[tuple[str, Any]]:
    payload = block.get("payload")
    if isinstance(payload, Mapping):
        for region in ("command", "output", "screenshots"):
            if region not in selected:
                continue
            for key in _PAYLOAD_REGION_KEYS[region]:
                if key in payload:
                    yield from _walk_json(f"payload.{key}", payload[key])

    metadata = block.get("metadata")
    if "metadata" in selected and isinstance(metadata, Mapping):
        yield from _walk_json("metadata", metadata)


def _walk_json(path: str, value: Any) -> Iterable[tuple[str, Any]]:
    if _is_ks_envelope(value):
        yield path, value
        return
    if isinstance(value, Mapping):
        if _KS_BOUNDARY_KEYS.intersection(str(key) for key in value):
            yield path, value
            return
        for key, item in value.items():
            yield from _walk_json(f"{path}.{key}", item)
        return
    if isinstance(value, list):
        for idx, item in enumerate(value):
            yield from _walk_json(f"{path}.{idx}", item)
        return
    if isinstance(value, tuple):
        for idx, item in enumerate(value):
            yield from _walk_json(f"{path}.{idx}", item)
        return
    yield path, value


def _redact_value(
    value: Any,
    *,
    explicit_reason: Any,
    pii_config: PresidioConfig | None,
    pii_analyzer: AnalyzerLike | None,
    pii_anonymizer: AnonymizerLike | None,
) -> tuple[Any, tuple[str, ...]]:
    if explicit_reason:
        reasons = _normalise_reasons(explicit_reason)
        if not reasons:
            return value, ()
        return _placeholder_for_reasons(reasons), reasons

    if _is_ks_envelope(value) or _is_ks_boundary_mapping(value):
        return {"redacted": True, "reason": REDACTION_KS_ENVELOPE}, (
            REDACTION_KS_ENVELOPE,
        )

    if not isinstance(value, str):
        return value, ()

    reasons: list[str] = []
    out, secret_hits = redact_secrets(value)
    if secret_hits:
        reasons.append(REDACTION_SECRET)

    out_after_ip = _IP_RE.sub("[REDACTED:customer_ip]", out)
    if out_after_ip != out:
        out = out_after_ip
        reasons.append(REDACTION_CUSTOMER_IP)

    pii = mask_log_text(
        out,
        config=pii_config,
        analyzer=pii_analyzer,
        anonymizer=pii_anonymizer,
    )
    if pii.changed:
        out = str(pii.value)
        reasons.append(REDACTION_PII)

    return out, _dedupe_reasons(reasons)


def _is_ks_envelope(value: Any) -> bool:
    return isinstance(value, Mapping) and _KS_ENVELOPE_KEYS.issubset(
        {str(key) for key in value}
    )


def _is_ks_boundary_mapping(value: Any) -> bool:
    return isinstance(value, Mapping) and bool(
        _KS_BOUNDARY_KEYS.intersection(str(key) for key in value)
    )


def _explicit_reason_for_path(mask: Mapping[str, Any], path: str) -> Any:
    if path in mask:
        return mask[path]
    parts = path.split(".")
    for idx in range(len(parts) - 1, 0, -1):
        ancestor = ".".join(parts[:idx])
        if ancestor in mask:
            return mask[ancestor]
    return None


def _normalise_reasons(value: Any) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple, set)) else (value,)
    reasons = [str(item).strip() for item in raw if str(item).strip()]
    if "none" in reasons:
        return ()
    allowed = [item for item in reasons if item in REDACTION_REASONS]
    return _dedupe_reasons(allowed or [REDACTION_SECRET])


def _dedupe_reasons(reasons: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in reasons if item))


def _mask_reason_value(reasons: tuple[str, ...]) -> str | list[str]:
    if len(reasons) == 1:
        return reasons[0]
    return list(reasons)


def _placeholder_for_reasons(reasons: tuple[str, ...]) -> str:
    label = "+".join(reasons) if reasons else REDACTION_SECRET
    return f"[REDACTED:{label}]"


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor: Any = target
    for part in parts[:-1]:
        if isinstance(cursor, list):
            cursor = cursor[int(part)]
        else:
            cursor = cursor[part]
    leaf = parts[-1]
    if isinstance(cursor, list):
        cursor[int(leaf)] = value
    else:
        cursor[leaf] = value


__all__ = [
    "BLOCK_SHARE_REGIONS",
    "REDACTION_CUSTOMER_IP",
    "REDACTION_KS_ENVELOPE",
    "REDACTION_PII",
    "REDACTION_REASONS",
    "REDACTION_SECRET",
    "BlockRedactionResult",
    "normalise_share_regions",
    "redact_block_for_share",
]
