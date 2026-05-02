"""W14.9 — `backend/web_sandbox_resource_limits.py` contract tests.

Pins the cgroup-limits module's structural + behavioural promises:

* Module surface (``__all__`` membership, schema version, default
  constants, error hierarchy, row-spec literal pinning).
* :func:`parse_memory_bytes` / :func:`parse_cpu_limit` /
  :func:`parse_storage_bytes` accept docker-style sizes
  (``2g``, ``512m``, raw bytes) and reject malformed inputs.
* :func:`format_cpu_arg` renders ``--cpus`` deterministically — int
  → "1" not "1.0", fractional → "0.5".
* :class:`WebPreviewResourceLimits` validates floor / ceiling on
  every field, accepts ``storage_limit_bytes=None`` for the
  overlay2-on-ext4 fallback, freezes via :func:`dataclasses.replace`.
* :func:`build_docker_resource_args` lays out the docker-run argv
  extension (``--memory`` / ``--memory-swap`` / ``--cpus`` /
  ``--storage-opt size=``) deterministically.
* :meth:`WebPreviewResourceLimits.from_settings` reads the three
  ``OMNISIGHT_WEB_SANDBOX_*`` env knobs with empty-fallback +
  rejects malformed input.
* Cross-worker contract: same-input → byte-equal output across
  multiple constructions (SOP §1 type-1 answer).
"""

from __future__ import annotations

from typing import Any

import pytest

from backend import web_sandbox_resource_limits as rlmod
from backend.web_sandbox_resource_limits import (
    CGROUP_OOM_REASON,
    DEFAULT_CPU_LIMIT,
    DEFAULT_CPU_LIMIT_TEXT,
    DEFAULT_MEMORY_LIMIT_BYTES,
    DEFAULT_MEMORY_LIMIT_TEXT,
    DEFAULT_STORAGE_LIMIT_BYTES,
    DEFAULT_STORAGE_LIMIT_TEXT,
    MAX_CPU_LIMIT,
    MAX_MEMORY_LIMIT_BYTES,
    MAX_STORAGE_LIMIT_BYTES,
    MIN_CPU_LIMIT,
    MIN_MEMORY_LIMIT_BYTES,
    MIN_STORAGE_LIMIT_BYTES,
    RESOURCE_LIMITS_SCHEMA_VERSION,
    STORAGE_LIMIT_DISABLED_TOKENS,
    ResourceLimitsError,
    WebPreviewResourceLimits,
    build_docker_resource_args,
    format_cpu_arg,
    parse_cpu_limit,
    parse_memory_bytes,
    parse_storage_bytes,
)


# ── Module surface ────────────────────────────────────────────────


EXPECTED_ALL = {
    "RESOURCE_LIMITS_SCHEMA_VERSION",
    "DEFAULT_MEMORY_LIMIT_TEXT",
    "DEFAULT_CPU_LIMIT_TEXT",
    "DEFAULT_STORAGE_LIMIT_TEXT",
    "DEFAULT_MEMORY_LIMIT_BYTES",
    "DEFAULT_CPU_LIMIT",
    "DEFAULT_STORAGE_LIMIT_BYTES",
    "MIN_MEMORY_LIMIT_BYTES",
    "MAX_MEMORY_LIMIT_BYTES",
    "MIN_CPU_LIMIT",
    "MAX_CPU_LIMIT",
    "MIN_STORAGE_LIMIT_BYTES",
    "MAX_STORAGE_LIMIT_BYTES",
    "STORAGE_LIMIT_DISABLED_TOKENS",
    "CGROUP_OOM_REASON",
    "ResourceLimitsError",
    "WebPreviewResourceLimits",
    "parse_memory_bytes",
    "parse_cpu_limit",
    "parse_storage_bytes",
    "build_docker_resource_args",
    "format_cpu_arg",
}


def test_all_exports_match_expected() -> None:
    assert set(rlmod.__all__) == EXPECTED_ALL


def test_all_exports_unique() -> None:
    assert len(rlmod.__all__) == len(set(rlmod.__all__))


def test_schema_version_is_semver() -> None:
    parts = RESOURCE_LIMITS_SCHEMA_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_error_hierarchy() -> None:
    assert issubclass(ResourceLimitsError, ValueError)


# ── Drift guards: row-spec literals ───────────────────────────────


def test_default_memory_pinned_to_2_gib() -> None:
    """W14.9 row spec literal: 2 GB RAM."""

    assert DEFAULT_MEMORY_LIMIT_BYTES == 2 * 1024 * 1024 * 1024
    assert DEFAULT_MEMORY_LIMIT_TEXT == "2g"


def test_default_cpu_pinned_to_one() -> None:
    """W14.9 row spec literal: 1 CPU."""

    assert DEFAULT_CPU_LIMIT == 1.0
    assert DEFAULT_CPU_LIMIT_TEXT == "1"


def test_default_storage_pinned_to_5_gib() -> None:
    """W14.9 row spec literal: 5 GB disk."""

    assert DEFAULT_STORAGE_LIMIT_BYTES == 5 * 1024 * 1024 * 1024
    assert DEFAULT_STORAGE_LIMIT_TEXT == "5g"


def test_cgroup_oom_reason_string() -> None:
    """``cgroup_oom`` literal aligns with W14.2's reserved
    ``killed_reason`` string."""

    assert CGROUP_OOM_REASON == "cgroup_oom"


def test_floor_and_ceiling_sane() -> None:
    """Floor/ceiling pins protect against typos and host-overrun."""

    assert MIN_MEMORY_LIMIT_BYTES == 64 * 1024 * 1024
    assert MAX_MEMORY_LIMIT_BYTES == 64 * 1024 * 1024 * 1024
    assert MIN_CPU_LIMIT == 0.05
    assert MAX_CPU_LIMIT == 64.0
    assert MIN_STORAGE_LIMIT_BYTES == 256 * 1024 * 1024
    assert MAX_STORAGE_LIMIT_BYTES == 256 * 1024 * 1024 * 1024


def test_disabled_tokens_set() -> None:
    """Operators on overlay2-on-ext4 disable the disk cap with one
    of these tokens."""

    assert "off" in STORAGE_LIMIT_DISABLED_TOKENS
    assert "0" in STORAGE_LIMIT_DISABLED_TOKENS
    assert "none" in STORAGE_LIMIT_DISABLED_TOKENS
    assert "disabled" in STORAGE_LIMIT_DISABLED_TOKENS


# ── parse_memory_bytes ────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2g", 2 * 1024**3),
        ("2G", 2 * 1024**3),
        ("2gb", 2 * 1024**3),
        ("2GB", 2 * 1024**3),
        ("512m", 512 * 1024**2),
        ("512M", 512 * 1024**2),
        ("64k", 64 * 1024),
        ("1t", 1024**4),
        ("1024", 1024),
        ("1.5g", int(1.5 * 1024**3)),
        ("  2g  ", 2 * 1024**3),
    ],
)
def test_parse_memory_bytes_valid(raw: str, expected: int) -> None:
    assert parse_memory_bytes(raw) == expected


def test_parse_memory_bytes_int_passthrough() -> None:
    assert parse_memory_bytes(2147483648) == 2147483648
    assert parse_memory_bytes(1.5e9) == 1_500_000_000


@pytest.mark.parametrize("bad", ["", "abc", "2x", "-2g", "2.0.0", "g"])
def test_parse_memory_bytes_rejects_malformed(bad: str) -> None:
    with pytest.raises(ResourceLimitsError):
        parse_memory_bytes(bad)


def test_parse_memory_bytes_rejects_zero_and_negative() -> None:
    with pytest.raises(ResourceLimitsError):
        parse_memory_bytes(0)
    with pytest.raises(ResourceLimitsError):
        parse_memory_bytes(-1)
    with pytest.raises(ResourceLimitsError):
        parse_memory_bytes("0g")


def test_parse_memory_bytes_rejects_bool() -> None:
    with pytest.raises(ResourceLimitsError):
        parse_memory_bytes(True)  # type: ignore[arg-type]


def test_parse_memory_bytes_rejects_non_numeric_type() -> None:
    with pytest.raises(ResourceLimitsError):
        parse_memory_bytes(None)  # type: ignore[arg-type]
    with pytest.raises(ResourceLimitsError):
        parse_memory_bytes([2, "g"])  # type: ignore[arg-type]


# ── parse_cpu_limit ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", 1.0),
        ("1.0", 1.0),
        ("0.5", 0.5),
        ("2", 2.0),
        ("0.25", 0.25),
        (1, 1.0),
        (0.5, 0.5),
        (2.5, 2.5),
        ("  1.5  ", 1.5),
    ],
)
def test_parse_cpu_limit_valid(raw: Any, expected: float) -> None:
    assert parse_cpu_limit(raw) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1cpu", "  "])
def test_parse_cpu_limit_rejects_malformed(bad: str) -> None:
    with pytest.raises(ResourceLimitsError):
        parse_cpu_limit(bad)


def test_parse_cpu_limit_rejects_bool() -> None:
    with pytest.raises(ResourceLimitsError):
        parse_cpu_limit(True)  # type: ignore[arg-type]


def test_parse_cpu_limit_rejects_non_numeric_type() -> None:
    with pytest.raises(ResourceLimitsError):
        parse_cpu_limit(None)  # type: ignore[arg-type]


# ── parse_storage_bytes ───────────────────────────────────────────


def test_parse_storage_bytes_aliases_memory() -> None:
    assert parse_storage_bytes("5g") == parse_memory_bytes("5g")
    assert parse_storage_bytes("256m") == 256 * 1024 * 1024


# ── format_cpu_arg ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cpu,expected",
    [
        (1.0, "1"),
        (1, "1"),
        (2.0, "2"),
        (0.5, "0.5"),
        (1.5, "1.5"),
        (0.25, "0.25"),
    ],
)
def test_format_cpu_arg_renders_expected(cpu: float, expected: str) -> None:
    assert format_cpu_arg(cpu) == expected


def test_format_cpu_arg_rejects_bool() -> None:
    with pytest.raises(ResourceLimitsError):
        format_cpu_arg(True)  # type: ignore[arg-type]


def test_format_cpu_arg_rejects_non_number() -> None:
    with pytest.raises(ResourceLimitsError):
        format_cpu_arg("1.0")  # type: ignore[arg-type]


# ── WebPreviewResourceLimits validation ───────────────────────────


def test_default_factory_returns_row_spec() -> None:
    limits = WebPreviewResourceLimits.default()
    assert limits.memory_limit_bytes == DEFAULT_MEMORY_LIMIT_BYTES
    assert limits.cpu_limit == DEFAULT_CPU_LIMIT
    assert limits.storage_limit_bytes == DEFAULT_STORAGE_LIMIT_BYTES
    assert limits.memory_swap_disabled is True


def test_explicit_construction() -> None:
    limits = WebPreviewResourceLimits(
        memory_limit_bytes=4 * 1024**3,
        cpu_limit=2.0,
        storage_limit_bytes=10 * 1024**3,
    )
    assert limits.memory_limit_bytes == 4 * 1024**3
    assert limits.cpu_limit == 2.0
    assert limits.storage_limit_bytes == 10 * 1024**3


def test_storage_limit_none_means_disabled() -> None:
    limits = WebPreviewResourceLimits(storage_limit_bytes=None)
    assert limits.storage_limit_bytes is None


def test_construction_rejects_negative_memory() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(memory_limit_bytes=-1)


def test_construction_rejects_zero_memory() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(memory_limit_bytes=0)


def test_construction_rejects_below_min_memory() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(memory_limit_bytes=1024)  # 1 KB


def test_construction_rejects_above_max_memory() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(memory_limit_bytes=MAX_MEMORY_LIMIT_BYTES + 1)


def test_construction_rejects_bool_memory() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(memory_limit_bytes=True)  # type: ignore[arg-type]


def test_construction_rejects_below_min_cpu() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(cpu_limit=0.001)


def test_construction_rejects_above_max_cpu() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(cpu_limit=MAX_CPU_LIMIT + 1)


def test_construction_rejects_bool_cpu() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(cpu_limit=True)  # type: ignore[arg-type]


def test_construction_rejects_non_numeric_cpu() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(cpu_limit="1.0")  # type: ignore[arg-type]


def test_construction_rejects_negative_storage() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(storage_limit_bytes=-1)


def test_construction_rejects_zero_storage() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(storage_limit_bytes=0)


def test_construction_rejects_below_min_storage() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(storage_limit_bytes=1024)


def test_construction_rejects_above_max_storage() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(storage_limit_bytes=MAX_STORAGE_LIMIT_BYTES + 1)


def test_construction_rejects_non_bool_swap_flag() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits(memory_swap_disabled="yes")  # type: ignore[arg-type]


def test_cpu_int_normalised_to_float() -> None:
    limits = WebPreviewResourceLimits(cpu_limit=2)
    assert isinstance(limits.cpu_limit, float)
    assert limits.cpu_limit == 2.0


def test_frozen_dataclass_rejects_mutation() -> None:
    limits = WebPreviewResourceLimits.default()
    with pytest.raises(Exception):
        limits.memory_limit_bytes = 999  # type: ignore[misc]


def test_to_dict_round_trip_shape() -> None:
    d = WebPreviewResourceLimits.default().to_dict()
    assert d == {
        "schema_version": RESOURCE_LIMITS_SCHEMA_VERSION,
        "memory_limit_bytes": DEFAULT_MEMORY_LIMIT_BYTES,
        "cpu_limit": DEFAULT_CPU_LIMIT,
        "storage_limit_bytes": DEFAULT_STORAGE_LIMIT_BYTES,
        "memory_swap_disabled": True,
    }


def test_to_dict_disabled_storage_serialises_none() -> None:
    d = WebPreviewResourceLimits(storage_limit_bytes=None).to_dict()
    assert d["storage_limit_bytes"] is None


# ── build_docker_resource_args ────────────────────────────────────


def test_build_args_default_layout() -> None:
    args = build_docker_resource_args(WebPreviewResourceLimits.default())
    # --memory + --memory-swap + --cpus + --storage-opt
    assert args == [
        "--memory",
        str(DEFAULT_MEMORY_LIMIT_BYTES),
        "--memory-swap",
        str(DEFAULT_MEMORY_LIMIT_BYTES),
        "--cpus",
        "1",
        "--storage-opt",
        f"size={DEFAULT_STORAGE_LIMIT_BYTES}",
    ]


def test_build_args_swap_flag_off_omits_memory_swap() -> None:
    limits = WebPreviewResourceLimits(memory_swap_disabled=False)
    args = build_docker_resource_args(limits)
    assert "--memory" in args
    assert "--memory-swap" not in args


def test_build_args_storage_none_omits_storage_opt() -> None:
    limits = WebPreviewResourceLimits(storage_limit_bytes=None)
    args = build_docker_resource_args(limits)
    assert "--storage-opt" not in args


def test_build_args_fractional_cpu() -> None:
    limits = WebPreviewResourceLimits(cpu_limit=0.5)
    args = build_docker_resource_args(limits)
    cpu_idx = args.index("--cpus")
    assert args[cpu_idx + 1] == "0.5"


def test_build_args_none_returns_empty() -> None:
    assert build_docker_resource_args(None) == []


def test_build_args_rejects_non_limits_type() -> None:
    with pytest.raises(TypeError):
        build_docker_resource_args({"memory": 1})  # type: ignore[arg-type]


def test_build_args_deterministic() -> None:
    a = build_docker_resource_args(WebPreviewResourceLimits.default())
    b = build_docker_resource_args(WebPreviewResourceLimits.default())
    assert a == b


# ── from_settings (env-knob round-trip) ───────────────────────────


class _FakeSettings:
    """Minimal duck-typed Settings for tests."""

    def __init__(
        self,
        memory: str = "",
        cpu: str = "",
        storage: str = "",
    ) -> None:
        self.web_sandbox_memory_limit = memory
        self.web_sandbox_cpu_limit = cpu
        self.web_sandbox_storage_limit = storage


def test_from_settings_empty_falls_back_to_defaults() -> None:
    limits = WebPreviewResourceLimits.from_settings(_FakeSettings())
    assert limits == WebPreviewResourceLimits.default()


def test_from_settings_parses_memory_override() -> None:
    limits = WebPreviewResourceLimits.from_settings(
        _FakeSettings(memory="4g")
    )
    assert limits.memory_limit_bytes == 4 * 1024**3
    assert limits.cpu_limit == DEFAULT_CPU_LIMIT
    assert limits.storage_limit_bytes == DEFAULT_STORAGE_LIMIT_BYTES


def test_from_settings_parses_cpu_override() -> None:
    limits = WebPreviewResourceLimits.from_settings(
        _FakeSettings(cpu="2")
    )
    assert limits.cpu_limit == 2.0


def test_from_settings_parses_storage_override() -> None:
    limits = WebPreviewResourceLimits.from_settings(
        _FakeSettings(storage="10g")
    )
    assert limits.storage_limit_bytes == 10 * 1024**3


@pytest.mark.parametrize("token", ["off", "0", "none", "disabled", "OFF"])
def test_from_settings_storage_disabled_token(token: str) -> None:
    limits = WebPreviewResourceLimits.from_settings(
        _FakeSettings(storage=token)
    )
    assert limits.storage_limit_bytes is None


def test_from_settings_rejects_malformed_memory() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits.from_settings(_FakeSettings(memory="2x"))


def test_from_settings_rejects_malformed_cpu() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits.from_settings(_FakeSettings(cpu="abc"))


def test_from_settings_rejects_below_floor_memory() -> None:
    with pytest.raises(ResourceLimitsError):
        WebPreviewResourceLimits.from_settings(_FakeSettings(memory="1k"))


def test_from_settings_strips_whitespace() -> None:
    limits = WebPreviewResourceLimits.from_settings(
        _FakeSettings(memory="  3g  ", cpu="  0.5  ")
    )
    assert limits.memory_limit_bytes == 3 * 1024**3
    assert limits.cpu_limit == 0.5


# ── Cross-worker contract (SOP §1 type-1 answer) ──────────────────


def test_cross_worker_default_byte_equal() -> None:
    """Two workers constructing the default produce byte-equal
    output; the dataclass is a value object."""

    a = WebPreviewResourceLimits.default()
    b = WebPreviewResourceLimits.default()
    assert a == b
    assert a.to_dict() == b.to_dict()


def test_cross_worker_argv_byte_equal() -> None:
    """Same input across 8 workers ⇒ identical argv (the docker-run
    argv is part of the cross-worker recovery contract — a worker
    that did not launch the sandbox can still recompute the argv to
    triage)."""

    args = [
        build_docker_resource_args(WebPreviewResourceLimits.default())
        for _ in range(8)
    ]
    assert all(a == args[0] for a in args)
