"""C13 — L4-CORE-13 Connectivity sub-skill library (#227).

Sub-skill library for connectivity protocols:
  BLE        — GATT + pairing + OTA DFU profile
  WiFi       — STA/AP + provisioning + enterprise auth
  5G         — modem AT / QMI + dual-SIM
  Ethernet   — basic + VLAN + PoE detection
  CAN        — SocketCAN + diagnostics (UDS/OBD-II)
  Modbus     — RTU/TCP master/slave
  OPC-UA     — server/client for industrial automation

Provides:
  - Protocol definition lookup from connectivity_standards.yaml
  - Per-protocol test recipe management
  - Connectivity test stub runners
  - Sub-skill registry with composition rules
  - Checklist validation (spec → required tests + artifacts)
  - get_connectivity_certs() for doc_suite_generator integration

Public API:
    protocols = list_protocols()
    proto     = get_protocol("ble")
    recipes   = get_test_recipes("wifi")
    result    = run_connectivity_test(protocol, recipe_id, target, work_dir)
    check     = validate_connectivity_checklist(spec)
    compose   = resolve_composition(product_type)
    certs     = get_connectivity_certs()
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONNECTIVITY_STANDARDS_PATH = _PROJECT_ROOT / "configs" / "connectivity_standards.yaml"


# -- Enums --

class ConnectivityProtocol(str, Enum):
    """Canonical identifiers for protocols handled by this sub-skill library.

    Values match the keys used in ``configs/connectivity_standards.yaml``
    and are the strings accepted by :func:`get_protocol` and friends.
    """

    ble = "ble"
    wifi = "wifi"
    fiveg = "fiveg"
    ethernet = "ethernet"
    can = "can"
    modbus = "modbus"
    opcua = "opcua"


class TestCategory(str, Enum):
    """Buckets that group recipes by what aspect of a protocol they exercise."""

    functional = "functional"
    security = "security"
    performance = "performance"
    provisioning = "provisioning"
    monitoring = "monitoring"
    resilience = "resilience"
    diagnostics = "diagnostics"
    ota = "ota"


class TestStatus(str, Enum):
    """Outcome states for an individual recipe or checklist item.

    ``pending`` means awaiting execution; ``skipped`` means deliberately
    excluded; ``error`` is reserved for runner/tooling failures distinct
    from a genuine test ``failed``.
    """

    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class TransportType(str, Enum):
    """Physical-medium classification used when filtering protocols."""

    wireless = "wireless"
    wired = "wired"
    mixed = "mixed"


class ProtocolLayer(str, Enum):
    """OSI-style layer the protocol occupies, used for composition logic."""

    link = "link"
    network = "network"
    application = "application"


# -- Data models --

@dataclass
class ConnTestRecipe:
    """One executable test recipe for a protocol.

    Recipes are loaded from the ``test_recipes`` list under each protocol
    in ``connectivity_standards.yaml``. ``category`` is expected to be a
    :class:`TestCategory` value but is kept as ``str`` to tolerate
    future categories added in YAML without a code change.
    """

    recipe_id: str
    name: str
    category: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict mirror of this recipe."""
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "tools": self.tools,
            "reference": self.reference,
        }


@dataclass
class ConnArtifactDef:
    """Definition of a certification artifact required by one or more protocols.

    Artifacts are evidence files (logs, captures, signed reports) that
    must accompany a protocol submission. ``file_pattern`` is a glob
    hint shown to operators — it is not enforced here.
    """

    artifact_id: str
    name: str
    description: str = ""
    file_pattern: str = ""


@dataclass
class ProtocolDef:
    """Parsed view of a single protocol entry in connectivity_standards.yaml.

    Holds the recipes, required artifacts, and SoC compatibility list for
    the protocol; consumers typically obtain instances via
    :func:`get_protocol` or :func:`list_protocols` rather than
    constructing one directly.
    """

    protocol_id: str
    name: str
    standard: str
    authority: str
    description: str = ""
    transport: str = "wireless"
    layer: str = "link"
    features: list[str] = field(default_factory=list)
    test_recipes: list[ConnTestRecipe] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    compatible_socs: list[str] = field(default_factory=list)

    def get_recipe(self, recipe_id: str) -> ConnTestRecipe | None:
        """Return the recipe with the given id, or ``None`` if not defined."""
        for r in self.test_recipes:
            if r.recipe_id == recipe_id:
                return r
        return None

    @property
    def recipe_ids(self) -> list[str]:
        """Ids of every recipe attached to this protocol, in YAML order."""
        return [r.recipe_id for r in self.test_recipes]

    def recipes_by_category(self, category: str) -> list[ConnTestRecipe]:
        """Return recipes whose ``category`` matches the given string exactly."""
        return [r for r in self.test_recipes if r.category == category]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict mirror of this protocol."""
        return {
            "protocol_id": self.protocol_id,
            "name": self.name,
            "standard": self.standard,
            "authority": self.authority,
            "description": self.description,
            "transport": self.transport,
            "layer": self.layer,
            "features": self.features,
            "test_recipes": [r.to_dict() for r in self.test_recipes],
            "required_artifacts": self.required_artifacts,
            "compatible_socs": self.compatible_socs,
        }


@dataclass
class ConnTestResult:
    """Outcome of a single :func:`run_connectivity_test` invocation.

    ``measurements`` is a free-form bag for per-recipe data
    (latencies, RSSI, error counters, etc.). ``raw_log_path`` points at
    the binary's captured output when an external runner was used;
    ``message`` is a short human-readable summary suitable for
    surfacing in dashboards and audit log entries.
    """

    recipe_id: str
    protocol: str
    status: TestStatus
    target_device: str = ""
    timestamp: float = field(default_factory=time.time)
    measurements: dict[str, Any] = field(default_factory=dict)
    raw_log_path: str = ""
    message: str = ""

    @property
    def passed(self) -> bool:
        """``True`` iff the status is exactly ``TestStatus.passed``."""
        return self.status == TestStatus.passed

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict mirror of this result."""
        return {
            "recipe_id": self.recipe_id,
            "protocol": self.protocol,
            "status": self.status.value,
            "target_device": self.target_device,
            "timestamp": self.timestamp,
            "measurements": self.measurements,
            "raw_log_path": self.raw_log_path,
            "message": self.message,
        }


@dataclass
class ChecklistItem:
    """One row in a :class:`ConnChecklist` — either a recipe or an artifact.

    The ``category`` field is reused to distinguish kinds: recipe items
    carry the recipe's :class:`TestCategory` value, while artifact items
    use the literal string ``"artifact"``.
    """

    item_id: str
    description: str
    category: str
    status: TestStatus = TestStatus.pending
    details: str = ""


@dataclass
class ConnChecklist:
    """Per-protocol roll-up of recipes + required artifacts and their states.

    Produced by :func:`validate_connectivity_checklist`. The
    :attr:`complete` flag is the single source of truth for "everything
    needed for this protocol has been satisfied" — it requires every
    item to be ``passed`` or explicitly ``skipped`` and the list to be
    non-empty.
    """

    protocol: str
    protocol_name: str
    items: list[ChecklistItem] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        """Total number of checklist items (recipes + artifact rows)."""
        return len(self.items)

    @property
    def passed_count(self) -> int:
        """Number of items currently in :attr:`TestStatus.passed`."""
        return sum(1 for i in self.items if i.status == TestStatus.passed)

    @property
    def pending_count(self) -> int:
        """Number of items still awaiting execution / evidence."""
        return sum(1 for i in self.items if i.status == TestStatus.pending)

    @property
    def failed_count(self) -> int:
        """Number of items whose latest result is :attr:`TestStatus.failed`."""
        return sum(1 for i in self.items if i.status == TestStatus.failed)

    @property
    def complete(self) -> bool:
        """``True`` iff every item is ``passed`` or ``skipped`` and the list is non-empty."""
        return all(
            i.status in (TestStatus.passed, TestStatus.skipped) for i in self.items
        ) and len(self.items) > 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict including pre-computed counters."""
        return {
            "protocol": self.protocol,
            "protocol_name": self.protocol_name,
            "total": self.total,
            "passed": self.passed_count,
            "pending": self.pending_count,
            "failed": self.failed_count,
            "complete": self.complete,
            "timestamp": self.timestamp,
            "items": [
                {
                    "item_id": i.item_id,
                    "description": i.description,
                    "category": i.category,
                    "status": i.status.value,
                    "details": i.details,
                }
                for i in self.items
            ],
        }


@dataclass
class ConnCertArtifact:
    """Concrete artifact instance attached to a protocol submission.

    Distinct from :class:`ConnArtifactDef` — that is the catalogue entry
    describing what an artifact *is*, while this records the per-protocol
    state of a given artifact (provided vs. pending) for cert packaging.
    """

    artifact_id: str
    name: str
    protocol: str
    status: str = "pending"
    file_path: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict mirror of this artifact."""
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "protocol": self.protocol,
            "status": self.status,
            "file_path": self.file_path,
            "description": self.description,
        }


@dataclass
class SubSkillDef:
    """Entry from the ``sub_skill_registry.available_sub_skills`` YAML block.

    Each sub-skill bundles a set of protocols a given device class
    typically needs. ``typical_products`` is consulted as a fallback by
    :func:`resolve_composition` when no named composition rule matches.
    """

    sub_skill_id: str
    skill_id: str
    protocols: list[str] = field(default_factory=list)
    typical_products: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict mirror of this sub-skill entry."""
        return {
            "sub_skill_id": self.sub_skill_id,
            "skill_id": self.skill_id,
            "protocols": self.protocols,
            "typical_products": self.typical_products,
        }


@dataclass
class CompositionRule:
    """Named composition rule from ``sub_skill_registry.composition_rules``.

    Maps a product-type name (e.g. ``"industrial gateway"``) to its
    ``required`` and ``optional`` sub-skills. Matched case-insensitively
    by :func:`resolve_composition` after underscore/hyphen normalisation.
    """

    name: str
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict mirror of this rule."""
        return {
            "name": self.name,
            "required": self.required,
            "optional": self.optional,
        }


@dataclass
class CompositionResult:
    """Result of resolving a product type against the composition registry.

    ``matched_rule`` is the name of the named rule that matched, or
    ``None`` when the resolution fell back to ``typical_products`` on a
    sub-skill (or when nothing matched at all, in which case all the
    list fields are empty).
    """

    product_type: str
    matched_rule: str | None = None
    required_sub_skills: list[str] = field(default_factory=list)
    optional_sub_skills: list[str] = field(default_factory=list)
    all_protocols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict mirror of this resolution."""
        return {
            "product_type": self.product_type,
            "matched_rule": self.matched_rule,
            "required_sub_skills": self.required_sub_skills,
            "optional_sub_skills": self.optional_sub_skills,
            "all_protocols": self.all_protocols,
        }


# -- Config loading (cached) --

_CONN_CACHE: dict | None = None


def _load_connectivity_standards() -> dict:
    global _CONN_CACHE
    if _CONN_CACHE is None:
        try:
            _CONN_CACHE = yaml.safe_load(
                _CONNECTIVITY_STANDARDS_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "connectivity_standards.yaml load failed: %s — using empty config", exc
            )
            _CONN_CACHE = {
                "protocols": {},
                "sub_skill_registry": {},
                "artifact_definitions": {},
            }
    return _CONN_CACHE


def reload_connectivity_standards_for_tests() -> None:
    """Drop the cached YAML so the next query re-reads the file from disk.

    Test helper for cases where a fixture rewrites
    ``configs/connectivity_standards.yaml`` between assertions. Not
    intended for production callers — the cache is otherwise process-wide.
    """
    global _CONN_CACHE
    _CONN_CACHE = None


def _parse_recipe(data: dict) -> ConnTestRecipe:
    return ConnTestRecipe(
        recipe_id=data["id"],
        name=data.get("name", data["id"]),
        category=data.get("category", ""),
        description=data.get("description", ""),
        tools=data.get("tools", []),
        reference=data.get("reference", ""),
    )


def _parse_protocol(protocol_id: str, data: dict) -> ProtocolDef:
    recipes = [_parse_recipe(r) for r in data.get("test_recipes", [])]
    return ProtocolDef(
        protocol_id=protocol_id,
        name=data.get("name", protocol_id),
        standard=data.get("standard", ""),
        authority=data.get("authority", ""),
        description=data.get("description", ""),
        transport=data.get("transport", "wireless"),
        layer=data.get("layer", "link"),
        features=data.get("features", []),
        test_recipes=recipes,
        required_artifacts=data.get("required_artifacts", []),
        compatible_socs=data.get("compatible_socs", []),
    )


# -- Protocol queries --

def get_protocol(protocol_id: str) -> ProtocolDef | None:
    """Return the :class:`ProtocolDef` for ``protocol_id``, or ``None`` if unknown.

    ``protocol_id`` must match a top-level key under ``protocols:`` in
    ``connectivity_standards.yaml`` exactly (case-sensitive).
    """
    raw = _load_connectivity_standards().get("protocols", {})
    if protocol_id not in raw:
        return None
    return _parse_protocol(protocol_id, raw[protocol_id])


def list_protocols() -> list[ProtocolDef]:
    """Return every protocol defined in the standards YAML, in file order."""
    raw = _load_connectivity_standards().get("protocols", {})
    return [_parse_protocol(k, v) for k, v in raw.items()]


def get_test_recipes(protocol_id: str) -> list[ConnTestRecipe]:
    """Return the test recipes for a protocol, or ``[]`` if the protocol is unknown.

    Unknown protocols return an empty list rather than raising so
    callers can iterate without pre-checking existence.
    """
    proto = get_protocol(protocol_id)
    if proto is None:
        return []
    return proto.test_recipes


def get_protocol_features(protocol_id: str) -> list[str]:
    """Return the declared feature tags for a protocol, or ``[]`` if unknown."""
    proto = get_protocol(protocol_id)
    if proto is None:
        return []
    return proto.features


def get_compatible_socs(protocol_id: str) -> list[str]:
    """Return SoC ids declared compatible with a protocol, or ``[]`` if unknown.

    An empty list on a *known* protocol means "no SoC restriction"
    (universal support); see :func:`check_soc_compatibility` for the
    matching semantics.
    """
    proto = get_protocol(protocol_id)
    if proto is None:
        return []
    return proto.compatible_socs


# -- Artifact definitions --

def get_artifact_definition(artifact_id: str) -> ConnArtifactDef | None:
    """Return the catalogue entry for ``artifact_id``, or ``None`` if undefined.

    Looks up the ``artifact_definitions`` block of the standards YAML.
    """
    raw = _load_connectivity_standards().get("artifact_definitions", {})
    if artifact_id not in raw:
        return None
    d = raw[artifact_id]
    return ConnArtifactDef(
        artifact_id=artifact_id,
        name=d.get("name", artifact_id),
        description=d.get("description", ""),
        file_pattern=d.get("file_pattern", ""),
    )


def list_artifact_definitions() -> list[ConnArtifactDef]:
    """Return every catalogue entry under ``artifact_definitions:``."""
    raw = _load_connectivity_standards().get("artifact_definitions", {})
    return [
        ConnArtifactDef(
            artifact_id=k,
            name=v.get("name", k),
            description=v.get("description", ""),
            file_pattern=v.get("file_pattern", ""),
        )
        for k, v in raw.items()
    ]


# -- Test stub runners --

def run_connectivity_test(
    protocol_id: str,
    recipe_id: str,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> ConnTestResult:
    """Stub runner for connectivity tests.

    In production this dispatches to the appropriate tool (bluetoothctl,
    wpa_supplicant, mmcli, etc.). Currently returns a pending/stub result.

    Args:
        protocol_id: Canonical protocol key (e.g. ``"ble"`` / ``"wifi"``);
            must be a value present under ``protocols:`` in the standards
            YAML. An unknown id yields a ``TestStatus.error`` result.
        recipe_id: Recipe id within the protocol. An unknown recipe also
            yields a ``TestStatus.error`` result rather than raising.
        target_device: Free-form identifier for the device under test
            (path like ``/dev/ttyUSB0``, hostname, MAC address, …).
        work_dir: Optional cwd passed through to the external binary
            when one is available.
        timeout_s: Maximum wall-clock seconds before the external binary
            is terminated and a ``TestStatus.error`` result is returned.
        **kwargs: Reserved for forward-compatible options. ``binary``
            (if present and resolvable on ``PATH``) triggers external
            execution; ``output_file`` (if present) is forwarded as
            ``--output`` to that binary.

    Returns:
        A :class:`ConnTestResult` whose ``status`` is one of
        ``passed``/``failed`` when a binary actually ran,
        ``pending`` when this returned the stub measurement bundle, or
        ``error`` on unknown protocol/recipe, missing binary or timeout.

    The function never raises for routine input problems — it always
    returns a ``ConnTestResult`` so callers can persist it through the
    audit log even on failure.
    """
    proto = get_protocol(protocol_id)
    if proto is None:
        return ConnTestResult(
            recipe_id=recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Unknown protocol: {protocol_id!r}. "
                    f"Available: {[p.protocol_id for p in list_protocols()]}",
        )

    recipe = proto.get_recipe(recipe_id)
    if recipe is None:
        return ConnTestResult(
            recipe_id=recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Unknown recipe: {recipe_id!r}. Available: {proto.recipe_ids}",
        )

    binary = kwargs.pop("binary", "")
    if binary and shutil.which(binary):
        return _exec_connectivity_binary(
            binary, protocol_id, recipe, target_device,
            work_dir=work_dir, timeout_s=timeout_s, **kwargs,
        )

    return ConnTestResult(
        recipe_id=recipe_id,
        protocol=protocol_id,
        status=TestStatus.pending,
        target_device=target_device,
        measurements={
            "category": recipe.category,
            "tools": recipe.tools,
            "features": proto.features,
        },
        message=f"Stub: {recipe.name} — awaiting hardware execution. "
                f"Tools needed: {recipe.tools}. Ref: {recipe.reference}",
    )


def _exec_connectivity_binary(
    binary: str,
    protocol_id: str,
    recipe: ConnTestRecipe,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> ConnTestResult:
    cmd = [
        binary,
        "--protocol", protocol_id,
        "--recipe", recipe.recipe_id,
        "--device", target_device,
    ]
    output_file = kwargs.get("output_file", "")
    if output_file:
        cmd += ["--output", output_file]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_s, cwd=work_dir,
        )
        passed = proc.returncode == 0
        return ConnTestResult(
            recipe_id=recipe.recipe_id,
            protocol=protocol_id,
            status=TestStatus.passed if passed else TestStatus.failed,
            target_device=target_device,
            raw_log_path=output_file,
            message=proc.stdout[:500] if proc.stdout else proc.stderr[:500],
        )
    except subprocess.TimeoutExpired:
        return ConnTestResult(
            recipe_id=recipe.recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Timeout after {timeout_s}s",
        )
    except FileNotFoundError:
        return ConnTestResult(
            recipe_id=recipe.recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Binary not found: {binary}",
        )


# -- Sub-skill registry --

def list_sub_skills() -> list[SubSkillDef]:
    """Return every entry under ``sub_skill_registry.available_sub_skills``."""
    raw = _load_connectivity_standards().get("sub_skill_registry", {})
    available = raw.get("available_sub_skills", [])
    return [
        SubSkillDef(
            sub_skill_id=s["id"],
            skill_id=s.get("skill_id", ""),
            protocols=s.get("protocols", []),
            typical_products=s.get("typical_products", []),
        )
        for s in available
    ]


def get_sub_skill(sub_skill_id: str) -> SubSkillDef | None:
    """Return the sub-skill with the given id, or ``None`` if not in the registry."""
    for s in list_sub_skills():
        if s.sub_skill_id == sub_skill_id:
            return s
    return None


def list_composition_rules() -> list[CompositionRule]:
    """Return every named rule under ``sub_skill_registry.composition_rules``."""
    raw = _load_connectivity_standards().get("sub_skill_registry", {})
    rules = raw.get("composition_rules", [])
    return [
        CompositionRule(
            name=r["name"],
            required=r.get("required", []),
            optional=r.get("optional", []),
        )
        for r in rules
    ]


def resolve_composition(product_type: str) -> CompositionResult:
    """Resolve which sub-skills a product type needs.

    Matches ``product_type`` against composition rules by name
    (case-insensitive, with underscores/hyphens/spaces normalised to a
    single space). On a hit, returns the rule's required + optional
    sub-skills with ``matched_rule`` set to the rule name.

    If no named rule matches, falls back to scanning each sub-skill's
    ``typical_products`` list (case-insensitive substring-exact match):
    the first match returns that sub-skill's protocols as ``required``
    with ``matched_rule=None``.

    When nothing matches at all, the result has empty lists and
    ``matched_rule=None`` — callers should treat that as "no
    composition known" rather than as an error.
    """
    normalized = product_type.lower().replace("-", " ").replace("_", " ")
    rules = list_composition_rules()

    for rule in rules:
        rule_normalized = rule.name.lower().replace("-", " ").replace("_", " ")
        if rule_normalized == normalized:
            all_protos: list[str] = list(rule.required) + list(rule.optional)
            return CompositionResult(
                product_type=product_type,
                matched_rule=rule.name,
                required_sub_skills=list(rule.required),
                optional_sub_skills=list(rule.optional),
                all_protocols=all_protos,
            )

    sub_skills = list_sub_skills()
    for ss in sub_skills:
        if product_type.lower() in [p.lower() for p in ss.typical_products]:
            return CompositionResult(
                product_type=product_type,
                matched_rule=None,
                required_sub_skills=ss.protocols,
                optional_sub_skills=[],
                all_protocols=ss.protocols,
            )

    return CompositionResult(
        product_type=product_type,
        matched_rule=None,
        required_sub_skills=[],
        optional_sub_skills=[],
        all_protocols=[],
    )


# -- Cert artifact generator --

def generate_cert_artifacts(
    protocol_id: str,
    spec: dict[str, Any] | None = None,
    test_results: list[ConnTestResult] | None = None,
) -> list[ConnCertArtifact]:
    """Produce the per-protocol artifact roll-up consumed by cert packaging.

    For each artifact id listed under the protocol's ``required_artifacts``
    a :class:`ConnCertArtifact` is emitted with ``status="provided"``
    when the id appears in ``spec["provided_artifacts"]`` and
    ``status="pending"`` otherwise. Unknown protocols yield ``[]``.

    ``test_results`` is accepted for future use (correlating artifacts
    to the run that produced them) but does not currently influence
    output beyond what ``spec`` declares.
    """
    proto = get_protocol(protocol_id)
    if proto is None:
        return []

    spec = spec or {}
    test_results = test_results or []
    provided_artifacts = set(spec.get("provided_artifacts", []))

    {
        r.recipe_id: r for r in test_results
    }

    art_defs = {a.artifact_id: a for a in list_artifact_definitions()}
    artifacts: list[ConnCertArtifact] = []

    for art_id in proto.required_artifacts:
        art_def = art_defs.get(art_id)
        name = art_def.name if art_def else art_id
        desc = art_def.description if art_def else ""

        if art_id in provided_artifacts:
            status = "provided"
        else:
            status = "pending"

        artifacts.append(ConnCertArtifact(
            artifact_id=art_id,
            name=name,
            protocol=protocol_id,
            status=status,
            description=desc,
        ))

    return artifacts


# -- Checklist validation --

def validate_connectivity_checklist(
    spec: dict[str, Any],
    test_results: list[ConnTestResult] | None = None,
) -> list[ConnChecklist]:
    """Build a per-protocol checklist of recipes and required artifacts.

    Reads ``spec["target_protocols"]`` for the list of protocols to roll
    up; unknown protocols are silently skipped (the YAML is the source
    of truth and stale spec entries should not produce noise). For each
    target protocol the resulting :class:`ConnChecklist` contains:

    - one row per recipe — status is set from ``test_results``
      (matched by ``recipe_id``) when available; ``passed`` and
      ``failed`` survive as-is, anything else (including ``skipped``
      or ``error``) collapses to ``pending`` so the checklist treats
      it as outstanding work;
    - one ``"artifact:<id>"`` row per required artifact — ``passed``
      iff the id is in ``spec["provided_artifacts"]``, otherwise
      ``pending``.

    Returns an empty list when ``target_protocols`` is missing or empty.
    """
    target_protocols = spec.get("target_protocols", [])
    if not target_protocols:
        return []

    test_results = test_results or []
    result_map: dict[str, ConnTestResult] = {
        r.recipe_id: r for r in test_results
    }

    checklists: list[ConnChecklist] = []

    for protocol_id in target_protocols:
        proto = get_protocol(protocol_id)
        if proto is None:
            continue

        items: list[ChecklistItem] = []

        for recipe in proto.test_recipes:
            existing = result_map.get(recipe.recipe_id)
            if existing:
                if existing.status == TestStatus.passed:
                    status = TestStatus.passed
                elif existing.status == TestStatus.failed:
                    status = TestStatus.failed
                else:
                    status = TestStatus.pending
            else:
                status = TestStatus.pending

            items.append(ChecklistItem(
                item_id=recipe.recipe_id,
                description=recipe.name,
                category=recipe.category,
                status=status,
                details=recipe.reference,
            ))

        art_defs = {a.artifact_id: a for a in list_artifact_definitions()}
        provided = set(spec.get("provided_artifacts", []))
        for art_id in proto.required_artifacts:
            art_def = art_defs.get(art_id)
            items.append(ChecklistItem(
                item_id=f"artifact:{art_id}",
                description=f"Artifact: {art_def.name if art_def else art_id}",
                category="artifact",
                status=TestStatus.passed if art_id in provided else TestStatus.pending,
            ))

        checklists.append(ConnChecklist(
            protocol=protocol_id,
            protocol_name=proto.name,
            items=items,
        ))

    return checklists


# -- SoC compatibility check --

def check_soc_compatibility(
    soc_id: str,
    protocol_ids: list[str] | None = None,
) -> dict[str, bool]:
    """Check which protocols support a given SoC.

    Args:
        soc_id: SoC identifier; matched case-insensitively against each
            protocol's ``compatible_socs`` entries.
        protocol_ids: Subset of protocol ids to evaluate; defaults to
            every value in :class:`ConnectivityProtocol`.

    Returns:
        A dict mapping each requested ``protocol_id`` to a boolean. An
        unknown protocol id maps to ``False``. A *known* protocol with
        an empty ``compatible_socs`` list maps to ``True`` — empty means
        universal support, not "no SoCs declared".
    """
    protocol_ids = protocol_ids or [p.value for p in ConnectivityProtocol]
    result: dict[str, bool] = {}
    for pid in protocol_ids:
        proto = get_protocol(pid)
        if proto is None:
            result[pid] = False
            continue
        if not proto.compatible_socs:
            result[pid] = True
        else:
            result[pid] = soc_id.lower() in [s.lower() for s in proto.compatible_socs]
    return result


# -- Doc suite generator integration --

_ACTIVE_CONN_CERTS: list[dict[str, Any]] = []


def register_connectivity_cert(
    standard: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    """Record a cert entry in the process-local registry.

    Entries are surfaced to the doc-suite generator via
    :func:`get_connectivity_certs`. State lives only in this process —
    callers responsible for cross-process aggregation must persist
    elsewhere. Use :func:`clear_connectivity_certs` to reset between
    runs.
    """
    _ACTIVE_CONN_CERTS.append({
        "standard": standard,
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_connectivity_certs() -> list[dict[str, Any]]:
    """Return a shallow copy of registered connectivity certs.

    Used by ``doc_suite_generator`` to embed cert summaries in
    generated documentation. The returned list is a copy, so callers
    cannot mutate the internal registry through it.
    """
    return list(_ACTIVE_CONN_CERTS)


def clear_connectivity_certs() -> None:
    """Drop every entry from the process-local cert registry.

    Intended for test isolation and runner-loop resets; production code
    should treat the registry as append-only across a workload.
    """
    _ACTIVE_CONN_CERTS.clear()


# -- Audit log integration --

async def log_connectivity_test_result(result: ConnTestResult) -> Optional[int]:
    """Append a connectivity test result to the audit log.

    Returns the new audit row id on success, or ``None`` if the audit
    subsystem could not be reached. Any exception from the audit module
    is caught and logged at WARNING — connectivity tests must never
    fail because the audit pipeline is unavailable.

    The ``entity_id`` is composed as ``"{protocol}:{recipe_id}"`` so
    queries can trivially slice by protocol or by individual recipe.
    """
    try:
        from backend import audit
        entity_id = f"{result.protocol}:{result.recipe_id}"
        return await audit.log(
            action="connectivity_test",
            entity_kind="connectivity_test_result",
            entity_id=entity_id,
            before=None,
            after=result.to_dict(),
            actor="connectivity",
        )
    except Exception as exc:
        logger.warning("Failed to log connectivity test result to audit: %s", exc)
        return None


def log_connectivity_test_result_sync(result: ConnTestResult) -> None:
    """Fire-and-forget variant for callers outside an ``await`` context.

    Schedules :func:`log_connectivity_test_result` on the currently
    running asyncio loop. When no loop is running (e.g. CLI / sync
    test contexts), the call is silently skipped — there is no
    sensible fallback that doesn't risk blocking the caller, and audit
    logging is best-effort.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("log_connectivity_test_result_sync skipped (no running loop)")
        return
    loop.create_task(log_connectivity_test_result(result))
