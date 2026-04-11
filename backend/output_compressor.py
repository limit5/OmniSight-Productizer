"""Unified output compressor for AI agent tool results.

Reduces token consumption by:
1. Removing duplicate consecutive lines (deduplication)
2. Stripping progress bars, spinners, and ANSI escape codes
3. Collapsing repeated warning/info patterns
4. Optionally piping through RTK binary (if available)

Applied at the tool executor level to cover ALL tools (shell and Python alike).
"""

from __future__ import annotations

import asyncio
import logging
import re

from backend.config import settings

logger = logging.getLogger(__name__)

# Patterns for strippable noise
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PROGRESS_BAR = re.compile(r"^.*[\[=>#\-]{5,}.*\d+%.*$", re.MULTILINE)
_SPINNER_LINE = re.compile(r"^.*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏|/\-\\]{1,3}\s.*$", re.MULTILINE)
_BLANK_RUNS = re.compile(r"\n{3,}", re.MULTILINE)

# Track compression metrics (module-level accumulator)
_compression_stats = {
    "total_original_bytes": 0,
    "total_compressed_bytes": 0,
    "compression_count": 0,
    "total_lines_removed": 0,
}


def get_compression_stats() -> dict:
    """Return current compression statistics."""
    stats = dict(_compression_stats)
    if stats["total_original_bytes"] > 0:
        stats["avg_ratio"] = round(1 - stats["total_compressed_bytes"] / stats["total_original_bytes"], 4)
    else:
        stats["avg_ratio"] = 0
    return stats


def reset_compression_stats() -> None:
    """Reset compression statistics."""
    for k in _compression_stats:
        _compression_stats[k] = 0


async def compress_output(text: str, tool_name: str = "") -> tuple[str, int]:
    """Compress tool output to reduce token consumption.

    Returns ``(compressed_text, bytes_saved)``.
    Falls back gracefully if compression fails.
    """
    if not isinstance(text, str) or not text:
        return str(text) if text else "", 0

    if not settings.rtk_enabled:
        return text, 0

    original_len = len(text)
    if original_len < settings.rtk_compression_threshold:
        return text, 0

    # Skip binary-looking content
    if _looks_binary(text):
        return text, 0

    try:
        compressed = _python_compress(text)

        bytes_saved = original_len - len(compressed)
        if bytes_saved > 0 and settings.rtk_track_savings:
            _compression_stats["total_original_bytes"] += original_len
            _compression_stats["total_compressed_bytes"] += len(compressed)
            _compression_stats["compression_count"] += 1

        return compressed, max(bytes_saved, 0)

    except Exception as exc:
        logger.warning("Output compression failed for %s: %s", tool_name, exc)
        return text, 0


def _looks_binary(text: str) -> bool:
    """Heuristic: if >10% of first 200 chars are non-printable, treat as binary."""
    sample = text[:200]
    non_printable = sum(1 for c in sample if ord(c) < 32 and c not in "\n\r\t")
    return non_printable > len(sample) * 0.1


def _python_compress(text: str) -> str:
    """Pure Python output compression (no external binary needed).

    Strategies applied in order:
    1. Strip ANSI escape codes
    2. Remove progress bars and spinner lines
    3. Deduplicate consecutive identical lines
    4. Collapse repeated warning patterns
    5. Remove excessive blank lines
    """
    # 1. Strip ANSI escapes
    text = _ANSI_ESCAPE.sub("", text)

    # 2. Remove progress bars and spinners
    if settings.rtk_strip_progress:
        text = _PROGRESS_BAR.sub("", text)
        text = _SPINNER_LINE.sub("", text)

    # 3. Deduplicate consecutive identical lines
    if settings.rtk_dedup_lines:
        lines = text.split("\n")
        deduped: list[str] = []
        prev = None
        dup_count = 0
        for line in lines:
            stripped = line.rstrip()
            if stripped == prev:
                dup_count += 1
            else:
                if dup_count > 0:
                    deduped.append(f"  ... ({dup_count} identical line(s) removed)")
                    _compression_stats["total_lines_removed"] += dup_count
                deduped.append(line)
                prev = stripped
                dup_count = 0
        if dup_count > 0:
            deduped.append(f"  ... ({dup_count} identical line(s) removed)")
            _compression_stats["total_lines_removed"] += dup_count
        text = "\n".join(deduped)

    # 4. Collapse repeated warning patterns (e.g., "warning: unused variable" x20)
    text = _collapse_repeated_patterns(text)

    # 5. Remove excessive blank lines
    text = _BLANK_RUNS.sub("\n\n", text)

    return text.strip()


def _collapse_repeated_patterns(text: str) -> str:
    """Collapse blocks where the same warning/note pattern repeats."""
    lines = text.split("\n")
    if len(lines) < 10:
        return text

    # Count pattern frequency (strip file paths and numbers)
    pattern_key = re.compile(r"[/\\][\w._\-/\\]+(?::\d+)+")  # file:line:col
    counts: dict[str, int] = {}
    for line in lines:
        key = pattern_key.sub("<FILE>", line.strip())
        if len(key) > 20:  # Only meaningful lines
            counts[key] = counts.get(key, 0) + 1

    # Find patterns that repeat > 3 times
    frequent = {k for k, v in counts.items() if v > 3}
    if not frequent:
        return text

    result: list[str] = []
    seen_frequent: dict[str, int] = {}
    for line in lines:
        key = pattern_key.sub("<FILE>", line.strip())
        if key in frequent:
            seen_frequent[key] = seen_frequent.get(key, 0) + 1
            if seen_frequent[key] <= 2:
                result.append(line)  # Keep first 2 occurrences
            elif seen_frequent[key] == 3:
                result.append(f"  ... (pattern repeated {counts[key]} times, showing first 2)")
            # Skip rest
        else:
            result.append(line)

    return "\n".join(result)
