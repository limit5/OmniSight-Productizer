"""OP-855 — Runner capability matrix.

Maps `(ticket_type × area × tier)` → required runner capabilities. The
auto-runner reads ``config/capability_matrix.yaml`` at pickup time and
explicitly enables only the listed capabilities; the rest are blocked
via :exc:`CapabilityNotPermitted` at call sites (gerrit push, jira
update, migration runner, etc.).

Public API:

- :data:`CAPABILITIES`                  — canonical capability vocabulary
- :class:`CapabilityNotPermitted`       — runner attempted disabled cap
- :class:`CapabilityMatrixMissingEntry` — (type, area, tier) not mapped
- :func:`load_capability_matrix`        — parse YAML → :class:`CapabilityMatrix`
- :class:`CapabilityMatrix.resolve`     — single-area lookup
- :class:`CapabilityMatrix.resolve_for_areas` — multi-area union lookup
- :func:`canonical_issuetype`           — localized issuetype name → English key
- :func:`apply_label_overrides`         — operator escape hatch via labels
- :func:`require_capability`            — call-site enforcement
- :func:`parse_label_overrides`         — exposed for the dashboard script

The matrix YAML is the single source of truth. Rollback = revert the
config file (no DB state).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import yaml


log = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX_PATH = REPO_ROOT / "config" / "capability_matrix.yaml"

# Canonical capability vocabulary (AC#2). Updating this list also requires
# updating the `capabilities:` block in `config/capability_matrix.yaml`;
# drift is asserted by ``backend/tests/test_capability_matrix.py``.
CAPABILITIES: frozenset[str] = frozenset({
    "code_edit",
    "run_tests",
    "run_lint",
    "gerrit_push",
    "jira_update",
    "mcp_search",
    "memory_recall",
    "run_migration",
    "deploy_action",
    "run_outcomes_grader",
})

LABEL_ENABLE_PREFIX = "capability:enable="
LABEL_DISABLE_PREFIX = "capability:disable="


# ── Issuetype name localization (OP-986 / CapabilityMatrixLocaleDrift) ──
#
# JIRA returns ``issuetype.name`` in the instance's *display locale*, not a
# stable identifier: a Japanese-locale instance (soraapp.atlassian.net, the
# OP-986 incident on 2026-05-12) returns ``ストーリー`` where an English instance
# returns ``Story``. The matrix YAML keys stay English — one source of truth —
# and incoming localized names are normalized here before lookup. Without this,
# ``entries.get('ストーリー')`` returns ``None`` → the runner falls back to
# ``read_only_default`` → ``gerrit_push`` is silently denied for *every*
# Story-typed ticket the runner picks up.
#
# The fully locale-agnostic fix is to key the matrix on the numeric
# ``issuetype.id`` (``"10001"`` etc.) instead of ``.name``; that's deferred
# because it needs an ID-based YAML migration plus a per-JIRA-instance canary
# (the ID itself can differ between instances — ``IssueTypeIDDrift`` in OP-986's
# error catalog).
#
# Add a row below only for locales actually configured on a connected JIRA
# instance. JIRA's stock issuetype display names for the common locales, for the
# next operator's reference:
#   ja (Japanese): ストーリー=Story  バグ=Bug  タスク=Task  エピック=Epic  サブタスク=Sub-task
#   zh-TW (繁體中文): 故事=Story  錯誤=Bug  工作=Task  epic 史诗→史詩
#   ko (Korean): 스토리=Story  버그=Bug  작업=Task
#   de (German): Fehler=Bug  Aufgabe=Task   (Story stays "Story")
#   fr (French): Récit=Story  Tâche=Task
_ISSUETYPE_ALIASES: Mapping[str, str] = MappingProxyType({
    # Japanese — the display locale on soraapp.atlassian.net (OP-986).
    "ストーリー": "Story",
    "バグ": "Bug",
    "タスク": "Task",
    "エピック": "Epic",
    "サブタスク": "Sub-task",
})


def canonical_issuetype(ticket_type: str) -> str:
    """Map a (possibly localized) JIRA issuetype display name to the English
    canonical name used as a key in ``config/capability_matrix.yaml``.

    Names that are already canonical — or that we have no alias for — pass
    through unchanged, so a genuinely unmapped ticket type still surfaces as
    :exc:`CapabilityMatrixMissingEntry` rather than being silently rewritten.
    """
    return _ISSUETYPE_ALIASES.get(ticket_type, ticket_type)


class CapabilityMatrixError(ValueError):
    """Raised when the capability matrix YAML is malformed."""


class CapabilityMatrixMissingEntry(LookupError):
    """``(ticket_type, area, tier)`` not present in the matrix.

    The runner falls back to the YAML's ``read_only_default`` set and
    emits a warning so an operator can extend the matrix. Carrying the
    safe-default capabilities on the exception lets a caller log + degrade
    in one step.
    """

    def __init__(
        self,
        ticket_type: str,
        area: str,
        tier: str,
        default_capabilities: frozenset[str],
    ) -> None:
        self.ticket_type = ticket_type
        self.area = area
        self.tier = tier
        self.default_capabilities = default_capabilities
        super().__init__(
            f"capability_matrix: no entry for "
            f"(ticket_type={ticket_type!r}, area={area!r}, tier={tier!r}); "
            f"safe-default = {sorted(default_capabilities)}"
        )


class CapabilityNotPermitted(PermissionError):
    """Runner attempted a capability not enabled for this ticket."""

    def __init__(self, capability: str, enabled: Iterable[str]) -> None:
        self.capability = capability
        self.enabled = frozenset(enabled)
        super().__init__(
            f"capability {capability!r} not permitted for this ticket; "
            f"enabled: {sorted(self.enabled)}"
        )


@dataclass(frozen=True)
class CapabilityMatrix:
    """In-memory view of the capability matrix YAML."""

    schema_version: int
    capabilities: frozenset[str]
    read_only_default: frozenset[str]
    entries: Mapping[str, Mapping[str, Mapping[str, frozenset[str]]]]
    source_path: Path | None = None

    def resolve(
        self,
        ticket_type: str,
        area: str,
        tier: str,
        *,
        labels: Iterable[str] = (),
        strict: bool = False,
    ) -> frozenset[str]:
        """Return enabled capabilities for ``(ticket_type, area, tier)``.

        Applies operator label overrides (``capability:enable=<cap>`` /
        ``capability:disable=<cap>``) on top of the matrix lookup. If the
        triple is absent from the matrix:

        - ``strict=True``  → raise :exc:`CapabilityMatrixMissingEntry`
          (after applying label overrides to the safe-default).
        - ``strict=False`` → log a warning, then return the safe-default
          with label overrides applied.

        Label overrides apply in both cases so an operator can rescue a
        missing-entry ticket without first patching the YAML.
        """
        base = self._lookup(ticket_type, area, tier)
        if base is None:
            adjusted = apply_label_overrides(self.read_only_default, labels)
            if strict:
                raise CapabilityMatrixMissingEntry(
                    ticket_type, area, tier, frozenset(adjusted)
                )
            log.warning(
                "capability_matrix.missing_entry ticket_type=%s area=%s tier=%s "
                "safe_default=%s",
                ticket_type, area, tier, sorted(adjusted),
            )
            return adjusted
        return apply_label_overrides(base, labels)

    def resolve_for_areas(
        self,
        ticket_type: str,
        areas: Iterable[str],
        tier: str,
        *,
        labels: Iterable[str] = (),
        strict: bool = False,
    ) -> frozenset[str]:
        """Union of :meth:`resolve` across each area.

        A multi-area ticket (``area:backend, area:docs``) gets the union
        of capabilities for every declared area. Label overrides apply
        once at the end, so ``capability:disable=gerrit_push`` blocks the
        push regardless of which area enabled it.
        """
        area_list = [a.strip() for a in areas if a and a.strip()]
        if not area_list:
            raise CapabilityMatrixError(
                "resolve_for_areas: at least one area is required"
            )
        union: set[str] = set()
        missing: list[str] = []
        for area in area_list:
            base = self._lookup(ticket_type, area, tier)
            if base is None:
                missing.append(area)
            else:
                union |= base
        if missing and not union:
            adjusted = apply_label_overrides(self.read_only_default, labels)
            if strict:
                raise CapabilityMatrixMissingEntry(
                    ticket_type, ",".join(missing), tier, frozenset(adjusted)
                )
            log.warning(
                "capability_matrix.missing_entry ticket_type=%s areas=%s tier=%s "
                "safe_default=%s",
                ticket_type, missing, tier, sorted(adjusted),
            )
            return adjusted
        if missing:
            log.warning(
                "capability_matrix.partial_missing ticket_type=%s missing_areas=%s "
                "tier=%s — using union of mapped areas",
                ticket_type, missing, tier,
            )
        return apply_label_overrides(frozenset(union), labels)

    def known_ticket_types(self) -> tuple[str, ...]:
        return tuple(sorted(self.entries))

    def known_areas(self, ticket_type: str) -> tuple[str, ...]:
        return tuple(sorted(self.entries.get(ticket_type, {})))

    def known_tiers(self, ticket_type: str, area: str) -> tuple[str, ...]:
        return tuple(sorted(self.entries.get(ticket_type, {}).get(area, {})))

    def _lookup(
        self, ticket_type: str, area: str, tier: str
    ) -> frozenset[str] | None:
        # Normalize a (possibly localized) JIRA issuetype name to the English
        # key used in the YAML — see ``canonical_issuetype`` / OP-986.
        by_area = self.entries.get(canonical_issuetype(ticket_type))
        if by_area is None:
            return None
        by_tier = by_area.get(area)
        if by_tier is None:
            return None
        return by_tier.get(tier)


def load_capability_matrix(
    path: Path | str = DEFAULT_MATRIX_PATH,
) -> CapabilityMatrix:
    """Parse the capability matrix YAML at ``path``."""
    matrix_path = Path(path)
    raw = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CapabilityMatrixError("capability_matrix YAML must be a mapping")
    if raw.get("schema_version") != 1:
        raise CapabilityMatrixError(
            f"capability_matrix schema_version must be 1, got {raw.get('schema_version')!r}"
        )

    capabilities_raw = raw.get("capabilities")
    if not isinstance(capabilities_raw, list) or not capabilities_raw:
        raise CapabilityMatrixError(
            "capability_matrix YAML must list 'capabilities'"
        )
    declared_caps = frozenset(_clean_capability(c) for c in capabilities_raw)
    unknown_declared = declared_caps - CAPABILITIES
    if unknown_declared:
        raise CapabilityMatrixError(
            f"capability_matrix declares unknown capabilities: {sorted(unknown_declared)} "
            f"(canonical set: {sorted(CAPABILITIES)})"
        )

    default_raw = raw.get("read_only_default", [])
    if not isinstance(default_raw, list):
        raise CapabilityMatrixError(
            "capability_matrix.read_only_default must be a list"
        )
    read_only_default = frozenset(_clean_capability(c) for c in default_raw)
    _assert_subset(read_only_default, declared_caps, "read_only_default")

    matrix_raw = raw.get("matrix")
    if not isinstance(matrix_raw, dict) or not matrix_raw:
        raise CapabilityMatrixError(
            "capability_matrix YAML must contain a non-empty 'matrix' mapping"
        )

    entries: dict[str, dict[str, dict[str, frozenset[str]]]] = {}
    for ticket_type, by_area in matrix_raw.items():
        if not isinstance(by_area, dict) or not by_area:
            raise CapabilityMatrixError(
                f"capability_matrix.matrix[{ticket_type!r}] must be a non-empty mapping"
            )
        per_area: dict[str, dict[str, frozenset[str]]] = {}
        for area, by_tier in by_area.items():
            if not isinstance(by_tier, dict) or not by_tier:
                raise CapabilityMatrixError(
                    f"capability_matrix.matrix[{ticket_type!r}][{area!r}] "
                    "must be a non-empty mapping"
                )
            per_tier: dict[str, frozenset[str]] = {}
            for tier, caps in by_tier.items():
                if not isinstance(caps, list):
                    raise CapabilityMatrixError(
                        f"capability_matrix.matrix[{ticket_type!r}][{area!r}][{tier!r}] "
                        "must be a list of capability strings"
                    )
                clean = frozenset(_clean_capability(c) for c in caps)
                _assert_subset(
                    clean, declared_caps,
                    f"matrix[{ticket_type!r}][{area!r}][{tier!r}]",
                )
                per_tier[str(tier)] = clean
            per_area[str(area)] = MappingProxyType(per_tier)  # type: ignore[assignment]
        entries[str(ticket_type)] = MappingProxyType(per_area)  # type: ignore[assignment]

    return CapabilityMatrix(
        schema_version=1,
        capabilities=declared_caps,
        read_only_default=read_only_default,
        entries=MappingProxyType(entries),  # type: ignore[arg-type]
        source_path=matrix_path,
    )


def parse_label_overrides(
    labels: Iterable[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(enable_set, disable_set)`` extracted from ticket labels.

    Unknown capabilities in label overrides raise — silently ignoring them
    would defeat the operator escape hatch by hiding typos. Validated
    against :data:`CAPABILITIES` so the runner refuses to start with a
    malformed override.
    """
    enable: set[str] = set()
    disable: set[str] = set()
    for label in labels:
        if not isinstance(label, str):
            continue
        if label.startswith(LABEL_ENABLE_PREFIX):
            cap = label[len(LABEL_ENABLE_PREFIX):]
            _assert_known_capability(cap, label)
            enable.add(cap)
        elif label.startswith(LABEL_DISABLE_PREFIX):
            cap = label[len(LABEL_DISABLE_PREFIX):]
            _assert_known_capability(cap, label)
            disable.add(cap)
    return frozenset(enable), frozenset(disable)


def apply_label_overrides(
    base: Iterable[str],
    labels: Iterable[str],
) -> frozenset[str]:
    """Apply ``capability:enable=`` / ``capability:disable=`` labels.

    Disable wins over enable when the same capability appears in both
    (defensive: a copy-paste error shouldn't accidentally grant a
    capability the operator also tried to remove).
    """
    enable, disable = parse_label_overrides(labels)
    return (frozenset(base) | enable) - disable


def require_capability(enabled: Iterable[str], requested: str) -> None:
    """Raise :exc:`CapabilityNotPermitted` if ``requested`` isn't enabled."""
    enabled_set = frozenset(enabled)
    if requested not in enabled_set:
        raise CapabilityNotPermitted(requested, enabled_set)


def resolve_with_profile(
    matrix: CapabilityMatrix,
    ticket_type: str,
    area: str,
    tier: str,
    *,
    profile: Any,
    labels: Iterable[str] = (),
) -> frozenset[str]:
    """Resolve through a ``capability_profile`` overlay.

    The profile is allowed to narrow the OP-855 matrix, not widen it. Legacy
    ``capability:enable=`` / ``capability:disable=`` labels still apply last
    during the transition sprint.
    """
    from backend.agents.capability_registry import _enforce_profile

    _enforce_profile(profile, tier)
    base = matrix.resolve(ticket_type, area, tier, labels=())
    return apply_label_overrides(base & profile.tools, labels)


def resolve_for_areas_with_profile(
    matrix: CapabilityMatrix,
    ticket_type: str,
    areas: Iterable[str],
    tier: str,
    *,
    profile: Any,
    labels: Iterable[str] = (),
) -> frozenset[str]:
    """Multi-area variant of :func:`resolve_with_profile`."""
    from backend.agents.capability_registry import _enforce_profile

    _enforce_profile(profile, tier)
    base = matrix.resolve_for_areas(ticket_type, areas, tier, labels=())
    return apply_label_overrides(base & profile.tools, labels)


def _clean_capability(value: Any) -> str:
    if not isinstance(value, str):
        raise CapabilityMatrixError(f"capability must be a string, got {value!r}")
    cap = value.strip()
    if not cap:
        raise CapabilityMatrixError("capability must be non-empty")
    return cap


def _assert_known_capability(cap: str, label: str) -> None:
    if cap not in CAPABILITIES:
        raise CapabilityMatrixError(
            f"label override {label!r} names unknown capability {cap!r}; "
            f"canonical set: {sorted(CAPABILITIES)}"
        )


def _assert_subset(
    subset: frozenset[str], superset: frozenset[str], where: str
) -> None:
    extra = subset - superset
    if extra:
        raise CapabilityMatrixError(
            f"{where} references undeclared capabilities: {sorted(extra)}"
        )


__all__ = [
    "CAPABILITIES",
    "CapabilityMatrix",
    "CapabilityMatrixError",
    "CapabilityMatrixMissingEntry",
    "CapabilityNotPermitted",
    "DEFAULT_MATRIX_PATH",
    "LABEL_DISABLE_PREFIX",
    "LABEL_ENABLE_PREFIX",
    "apply_label_overrides",
    "canonical_issuetype",
    "load_capability_matrix",
    "parse_label_overrides",
    "require_capability",
    "resolve_for_areas_with_profile",
    "resolve_with_profile",
]
