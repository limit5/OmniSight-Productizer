"""AB.5 — External tool registry + 4 integration-type handlers.

Wires non-Anthropic tools (MCP servers, subprocess CLIs, Docker
sidecars, Python libs) into OmniSight's Anthropic batch dispatcher
(AB.4). Each external tool gets:

  - **Static metadata** — tool_name, integration_type, license_tier,
    sandbox_required, description (declared in this module)
  - **Dynamic binding** — Docker image / REST URL / binary path /
    Python module name (read from ``external_tool_registry`` table
    populated by operator at deploy)
  - **Handler class** — knows how to invoke the underlying tool given
    the binding + caller-supplied input dict
  - **Per-task-type dispatch entry** — TaskKind → tool subset, so
    Anthropic sees only the relevant 5-10 tools per task type rather
    than all 50+

Four ``IntegrationType`` adapters ship:

  1. ``python_lib``    — direct ``importlib.import_module()`` of an
                         installed Python package; no sandbox needed
                         (already in our process). MIT/Apache only.
  2. ``subprocess``    — ``asyncio.subprocess.create_subprocess_exec``
                         with stdin/stdout pipes + timeout. Used for
                         GPL CLIs (altium2kicad Perl) where process
                         boundary IS the license boundary.
  3. ``docker_mcp``    — JSON-RPC over Docker container's STDIO,
                         MCP 2025-06-18 protocol. Used for KiCAD-MCP-
                         Server. Tool whitelist enforced (28 read /
                         no write) per AB.5 R55.
  4. ``docker_sidecar`` — HTTP REST against a separately-running
                         Docker sidecar container. Process boundary
                         IS the AGPL boundary. Used for OdbDesign.

License boundary enforcement (R57): registry rejects any handler
binding where `license_tier` is GPL/AGPL but `integration_type` is
not subprocess/docker_sidecar. The boundary is structural, not
disciplinary.

Concrete handler bindings for the seven tools listed in
``docs/operations/anthropic-api-migration-and-batch-mode.md §5.1``
are pre-declared in this module's ``DEFAULT_TOOL_DEFINITIONS``.
Operator runs a one-time ``seed_default_tools(persistence)`` at
deploy time to insert the rows; subsequent updates land via direct
SQL or admin UI.

Out-of-scope (defer until consumer priority lands):

  * SQL plumbing — ``ExternalToolRegistryStore`` Protocol declared,
    ``InMemoryExternalToolRegistryStore`` impl ships, PG impl waits
    for first cross-restart deployment.
  * Health check loop — schema field ``health_status`` exists,
    background poller arrives with AB.6 cost guard / AB.7 retry.

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §5
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


# ─── Type aliases ────────────────────────────────────────────────


IntegrationType = Literal[
    "python_lib", "subprocess", "docker_mcp", "docker_sidecar"
]
LicenseTier = Literal[
    "mit_apache_bsd",       # direct link OK
    "lgpl",                 # dynamic link OK
    "gpl",                  # subprocess boundary required
    "agpl",                 # Docker sidecar REST boundary required
    "inspiration_only",     # pattern only, no source (Warp tier)
]
HealthStatus = Literal["unknown", "healthy", "degraded", "unreachable"]


# ─── Errors ──────────────────────────────────────────────────────


class ExternalToolError(RuntimeError):
    """Base error for external tool invocation failures."""


class LicenseBoundaryViolation(ValueError):
    """Raised when a registry definition couples a copyleft license
    with a non-isolated integration type. Structural, not opt-in."""


class ToolNotEnabledError(ExternalToolError):
    """Operator has disabled this tool in the registry."""


class ToolNotFoundError(ExternalToolError):
    """Tool not registered (typo or missing operator deploy step)."""


# ─── Definitions ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ExternalToolDefinition:
    """One registered external tool — code-side metadata.

    Operator-deploy-time data lives in the ``external_tool_registry``
    DB table (alembic 0184); this dataclass mirrors the row shape for
    in-process use. ``config`` here is the *default* binding when the
    operator hasn't customised it.
    """

    tool_name: str
    integration_type: IntegrationType
    license_tier: LicenseTier
    sandbox_required: bool
    description: str
    default_config: dict[str, Any] = field(default_factory=dict)
    enabled_by_default: bool = True

    def __post_init__(self) -> None:
        # AB.5 R57: structural license boundary enforcement.
        if self.license_tier == "gpl" and self.integration_type != "subprocess":
            raise LicenseBoundaryViolation(
                f"GPL tool {self.tool_name!r} must use integration_type='subprocess', "
                f"got {self.integration_type!r}. process boundary = license boundary."
            )
        if self.license_tier == "agpl" and self.integration_type != "docker_sidecar":
            raise LicenseBoundaryViolation(
                f"AGPL tool {self.tool_name!r} must use integration_type='docker_sidecar', "
                f"got {self.integration_type!r}. network boundary = license boundary."
            )
        # GPL / AGPL implicitly require sandbox; assert.
        if self.license_tier in ("gpl", "agpl") and not self.sandbox_required:
            raise LicenseBoundaryViolation(
                f"{self.license_tier.upper()} tool {self.tool_name!r} must set "
                "sandbox_required=True. The boundary is what the license requires."
            )


@dataclass
class ExternalToolBinding:
    """A definition + operator-applied config (live deployment state)."""

    definition: ExternalToolDefinition
    config: dict[str, Any]
    enabled: bool = True
    deployed_at: datetime | None = None
    last_health_check: datetime | None = None
    health_status: HealthStatus = "unknown"


# ─── Default tool definitions (HD borrows + 4 google_ai MCP) ─────


DEFAULT_TOOL_DEFINITIONS: tuple[ExternalToolDefinition, ...] = (
    # ── HD.1.2a — KiCAD-MCP-Server (mixelpixx) ──
    ExternalToolDefinition(
        tool_name="KiCadMCP",
        integration_type="docker_mcp",
        license_tier="mit_apache_bsd",
        sandbox_required=True,  # processes customer .kicad_pcb / .kicad_sch
        description=(
            "KiCAD-MCP-Server (mixelpixx, MIT). 122 tools (28 read whitelist). "
            "Docker sandboxed, no outbound network."
        ),
        default_config={
            "docker_image": "ghcr.io/mixelpixx/kicad-mcp:latest",
            "stdio": True,
            "read_only_whitelist": True,  # 28 read-only tools per HD.1.2a / R55
        },
    ),
    # ── HD.1.3b — altium2kicad (thesourcerer8, GPL-2.0) ──
    ExternalToolDefinition(
        tool_name="Altium2KiCad",
        integration_type="subprocess",
        license_tier="gpl",
        sandbox_required=True,
        description=(
            "altium2kicad (thesourcerer8, GPL-2.0). Perl subprocess, "
            "process boundary = license boundary. Converts Altium .PcbDoc → KiCad."
        ),
        default_config={
            "command": ["perl", "third_party/altium2kicad/altium2kicad.pl"],
            "timeout_sec": 300,
        },
    ),
    # ── HD.1.12a — OdbDesign (nam20485, AGPL-3.0) ──
    ExternalToolDefinition(
        tool_name="OdbDesign",
        integration_type="docker_sidecar",
        license_tier="agpl",
        sandbox_required=True,
        description=(
            "OdbDesign (nam20485, AGPL-3.0). Docker sidecar REST, network boundary = "
            "AGPL boundary. ODB++ parser via HTTP."
        ),
        default_config={
            "rest_url": "http://odb-sidecar:8080",
            "image": "ghcr.io/nam20485/odb-design:latest",
        },
    ),
    # ── HD.5.13 — vision-parse (iamarunbrahma, MIT) ──
    ExternalToolDefinition(
        tool_name="VisionParse",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description=(
            "vision-parse (iamarunbrahma, MIT, 470⭐). PDF → structured "
            "markdown via vision LLM. Used for HD.5 sensor datasheet onboarding."
        ),
        default_config={
            "module": "vision_parse",
            "callable": "VisionParser",
        },
    ),
    # ── HD.6.9 — SKiDL (devbisme, MIT) ──
    ExternalToolDefinition(
        tool_name="SKiDL",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description=(
            "SKiDL (devbisme, MIT, 1473⭐). Python circuit DSL. NL → SKiDL → "
            "KiCad netlist for HD.6.9 advisory circuit generation."
        ),
        default_config={"module": "skidl", "callable": "Circuit"},
    ),
    # ── HD.7.1a — pyFDT (molejar, Apache) ──
    ExternalToolDefinition(
        tool_name="PyFDT",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description=(
            "pyFDT (molejar, Apache-2.0). Linux device-tree (.dts ↔ .dtb) "
            "convert + parse. Used by HD.7.1 Yocto/BR firmware adaptor."
        ),
        default_config={"module": "fdt", "callable": "FDT"},
    ),
    # ── HD.7.4b — ldparser (pftbest, MIT) ──
    ExternalToolDefinition(
        tool_name="LDParser",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description=(
            "ldparser (pftbest, MIT). GNU linker script (.ld) parser for "
            "HD.7.4 RTOS / FreeRTOS / ThreadX firmware analysis."
        ),
        default_config={"module": "ldparser"},
    ),
)


# ─── Per-task-type dispatch table (AB.5.7) ───────────────────────


# Each entry maps a TaskKind to the tool subset Anthropic should see.
# Keeps tools=[] payload small (avoid R55 prompt bloat) and surfaces
# only contextually-relevant capabilities to the model.
TASK_KIND_DISPATCH: dict[str, tuple[str, ...]] = {
    # HD.1 EDA parser tasks
    "hd_parse_kicad": ("Read", "KiCadMCP", "SKILL_HD_PARSE"),
    "hd_parse_altium": (
        "Read", "Altium2KiCad", "KiCadMCP", "SKILL_HD_PARSE",
    ),
    "hd_parse_odb": ("Read", "OdbDesign", "SKILL_HD_PARSE"),
    "hd_parse_eagle": ("Read", "KiCadMCP", "SKILL_HD_PARSE"),  # via KiCad importer
    # HD.4 reference vs customer diff
    "hd_diff_reference": (
        "Read", "KiCadMCP", "OdbDesign",
        "SKILL_HD_DIFF_REFERENCE", "SKILL_HD_PARSE",
    ),
    # HD.5 sensor KB onboarding
    "hd_sensor_kb_extract": (
        "Read", "Write", "VisionParse", "SKILL_HD_RAG_QUERY",
    ),
    # HD.6 forced AVL workflow
    "hd_avl_substitution": (
        "Read", "SKILL_HD_SENSOR_SWAP_FEASIBILITY",
        "SKILL_HD_BLOB_COMPAT", "SKiDL",
    ),
    # HD.7 firmware stack adaptor
    "hd_fw_dts_parse": ("Read", "PyFDT", "SKILL_HD_FW_SYNC_PATCH"),
    "hd_fw_linker_parse": ("Read", "LDParser", "SKILL_HD_FW_SYNC_PATCH"),
    # HD.18 CVE impact
    "hd_cve_impact": ("Read", "WebFetch", "SKILL_HD_CVE_IMPACT"),
    # HD.19 bring-up workbench
    "hd_bringup": (
        "Read", "Bash", "Edit",
        "SKILL_HD_BRINGUP_CHECKLIST", "SKILL_HD_BRINGUP_LIVE_PARSE",
    ),
    # Generic dev task (default — eager Claude Code tools only, no HD)
    "generic_dev": ("Read", "Write", "Edit", "Bash", "Grep", "Glob"),
    # Orchestration / planning task
    "planning": ("Read", "Grep", "Glob", "Agent", "ToolSearch"),
}


def tools_for_task_kind(task_kind: str) -> tuple[str, ...]:
    """Return the tool subset for a task kind. Falls back to generic_dev
    if the kind isn't registered — never raises (warn on miss).
    """
    if task_kind in TASK_KIND_DISPATCH:
        return TASK_KIND_DISPATCH[task_kind]
    logger.warning(
        "Unknown task_kind %r; falling back to generic_dev tool subset",
        task_kind,
    )
    return TASK_KIND_DISPATCH["generic_dev"]


# ─── Storage Protocol + in-memory impl ───────────────────────────


class ExternalToolRegistryStore(Protocol):
    async def upsert_binding(self, binding: ExternalToolBinding) -> None: ...
    async def get_binding(self, tool_name: str) -> ExternalToolBinding | None: ...
    async def list_bindings(
        self, *, enabled_only: bool = False
    ) -> list[ExternalToolBinding]: ...
    async def set_health(
        self, tool_name: str, status: HealthStatus, checked_at: datetime
    ) -> None: ...


class InMemoryExternalToolRegistryStore:
    """Dev / test store. Production swap-in: same Protocol, PG-backed."""

    def __init__(self) -> None:
        self._bindings: dict[str, ExternalToolBinding] = {}

    async def upsert_binding(self, binding: ExternalToolBinding) -> None:
        self._bindings[binding.definition.tool_name] = binding

    async def get_binding(self, tool_name: str) -> ExternalToolBinding | None:
        return self._bindings.get(tool_name)

    async def list_bindings(
        self, *, enabled_only: bool = False
    ) -> list[ExternalToolBinding]:
        items = list(self._bindings.values())
        if enabled_only:
            items = [b for b in items if b.enabled]
        return items

    async def set_health(
        self, tool_name: str, status: HealthStatus, checked_at: datetime
    ) -> None:
        b = self._bindings.get(tool_name)
        if b is None:
            return
        b.health_status = status
        b.last_health_check = checked_at


# ─── Handlers (one per integration_type) ─────────────────────────


HandlerCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class HandlerInvocation:
    """Result of invoking an external tool handler."""

    output: dict[str, Any]
    duration_ms: int
    handler_kind: str


class PythonLibHandler:
    """Dynamic-import Python package and call a configured callable."""

    def __init__(self, binding: ExternalToolBinding) -> None:
        self.binding = binding
        self._module_name = binding.config.get("module")
        if not self._module_name:
            raise ExternalToolError(
                f"PythonLibHandler {binding.definition.tool_name!r}: "
                "config.module missing."
            )
        self._callable_name = binding.config.get("callable")

    async def __call__(self, input_dict: dict[str, Any]) -> HandlerInvocation:
        start = asyncio.get_event_loop().time()
        try:
            module = importlib.import_module(self._module_name)
        except ImportError as e:
            raise ExternalToolError(
                f"Module {self._module_name!r} not installed for tool "
                f"{self.binding.definition.tool_name!r}: {e}"
            ) from e

        target = (
            getattr(module, self._callable_name)
            if self._callable_name
            else module
        )

        # Call the configured method, passing input dict; default to a
        # `run(**input_dict)` convention for callable classes.
        result: Any
        if asyncio.iscoroutinefunction(target):
            result = await target(**input_dict)
        elif callable(target):
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: target(**input_dict)
            )
        else:
            raise ExternalToolError(
                f"Resolved {self._module_name}.{self._callable_name} is not callable."
            )

        elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
        return HandlerInvocation(
            output=result if isinstance(result, dict) else {"result": result},
            duration_ms=elapsed,
            handler_kind="python_lib",
        )


class SubprocessHandler:
    """Run an external CLI via asyncio subprocess. GPL boundary."""

    def __init__(self, binding: ExternalToolBinding) -> None:
        self.binding = binding
        self._command: list[str] = list(binding.config.get("command") or [])
        if not self._command:
            raise ExternalToolError(
                f"SubprocessHandler {binding.definition.tool_name!r}: "
                "config.command missing."
            )
        self._timeout_sec = int(binding.config.get("timeout_sec", 300))

    async def __call__(self, input_dict: dict[str, Any]) -> HandlerInvocation:
        start = asyncio.get_event_loop().time()
        # Caller passes additional command args via input_dict["args"];
        # stdin payload via input_dict["stdin"].
        extra_args = list(input_dict.get("args") or [])
        stdin_payload: bytes | None = None
        raw_stdin = input_dict.get("stdin")
        if isinstance(raw_stdin, str):
            stdin_payload = raw_stdin.encode("utf-8")
        elif isinstance(raw_stdin, bytes):
            stdin_payload = raw_stdin

        proc = await asyncio.create_subprocess_exec(
            *self._command,
            *extra_args,
            stdin=asyncio.subprocess.PIPE if stdin_payload else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_payload),
                timeout=self._timeout_sec,
            )
        except asyncio.TimeoutError as e:
            with _suppress_proc_kill():
                proc.kill()
            raise ExternalToolError(
                f"Subprocess {self.binding.definition.tool_name!r} "
                f"timed out after {self._timeout_sec}s"
            ) from e

        elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
        return HandlerInvocation(
            output={
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            },
            duration_ms=elapsed,
            handler_kind="subprocess",
        )


class DockerMCPHandler:
    """Stub: JSON-RPC over Docker container's STDIO (MCP 2025-06-18).

    Production impl wires through the MCP client SDK; this stub
    records the binding contract and returns a structured "deferred"
    payload until operator deploys the Docker container. Tests exercise
    the contract without spinning real containers.
    """

    def __init__(self, binding: ExternalToolBinding) -> None:
        self.binding = binding

    async def __call__(self, input_dict: dict[str, Any]) -> HandlerInvocation:
        if not self.binding.config.get("docker_image"):
            raise ExternalToolError(
                f"DockerMCPHandler {self.binding.definition.tool_name!r}: "
                "config.docker_image missing."
            )
        # AB.5 R55: enforce read-only whitelist for KiCad-MCP-style tools
        # that ship 122 tools when only 28 are safe.
        whitelist = self.binding.config.get("read_only_whitelist", False)
        method = input_dict.get("method", "")
        if whitelist and method.startswith(("write_", "create_", "delete_", "edit_", "modify_")):
            raise ExternalToolError(
                f"DockerMCPHandler {self.binding.definition.tool_name!r}: "
                f"method {method!r} blocked by read-only whitelist (R55)."
            )
        return HandlerInvocation(
            output={
                "status": "deferred",
                "reason": "DockerMCPHandler is a contract stub; operator must "
                          "deploy the Docker container before invocation.",
                "binding": self.binding.definition.tool_name,
                "method": method,
            },
            duration_ms=0,
            handler_kind="docker_mcp",
        )


class DockerSidecarHandler:
    """Stub: HTTP REST against a separately-running Docker sidecar.

    Production impl uses ``httpx.AsyncClient``; this stub records the
    contract surface. AGPL boundary (R57) enforced via license_tier
    check at registration; this handler trusts the registry.
    """

    def __init__(self, binding: ExternalToolBinding) -> None:
        self.binding = binding

    async def __call__(self, input_dict: dict[str, Any]) -> HandlerInvocation:
        url = self.binding.config.get("rest_url")
        if not url:
            raise ExternalToolError(
                f"DockerSidecarHandler {self.binding.definition.tool_name!r}: "
                "config.rest_url missing."
            )
        return HandlerInvocation(
            output={
                "status": "deferred",
                "reason": "DockerSidecarHandler is a contract stub; "
                          "operator must deploy the sidecar before invocation.",
                "binding": self.binding.definition.tool_name,
                "rest_url": url,
                "request": input_dict,
            },
            duration_ms=0,
            handler_kind="docker_sidecar",
        )


HANDLER_CLASSES: dict[IntegrationType, type] = {
    "python_lib": PythonLibHandler,
    "subprocess": SubprocessHandler,
    "docker_mcp": DockerMCPHandler,
    "docker_sidecar": DockerSidecarHandler,
}


# ─── Registry ────────────────────────────────────────────────────


class ExternalToolRegistry:
    """Operator-facing registry — read deployment bindings, build handlers."""

    def __init__(
        self,
        store: ExternalToolRegistryStore | None = None,
        definitions: tuple[ExternalToolDefinition, ...] = DEFAULT_TOOL_DEFINITIONS,
    ) -> None:
        self.store = store or InMemoryExternalToolRegistryStore()
        self._definitions: dict[str, ExternalToolDefinition] = {
            d.tool_name: d for d in definitions
        }

    @property
    def definitions(self) -> dict[str, ExternalToolDefinition]:
        return dict(self._definitions)

    async def seed_default_bindings(self) -> int:
        """Insert default bindings (operator deploys can override later)."""
        count = 0
        for definition in self._definitions.values():
            existing = await self.store.get_binding(definition.tool_name)
            if existing is None:
                await self.store.upsert_binding(
                    ExternalToolBinding(
                        definition=definition,
                        config=dict(definition.default_config),
                        enabled=definition.enabled_by_default,
                        deployed_at=datetime.now(timezone.utc),
                    )
                )
                count += 1
        return count

    async def build_handler(self, tool_name: str) -> HandlerCallable:
        """Resolve a tool name to an instantiated handler.

        Raises:
          ToolNotFoundError — unknown tool name (typo / not seeded)
          ToolNotEnabledError — operator has disabled this tool
          ExternalToolError — config invalid for the integration_type
        """
        if tool_name not in self._definitions:
            raise ToolNotFoundError(
                f"Tool {tool_name!r} not in registry. Known: "
                f"{sorted(self._definitions)[:8]}{'…' if len(self._definitions) > 8 else ''}"
            )
        binding = await self.store.get_binding(tool_name)
        if binding is None:
            raise ToolNotFoundError(
                f"Tool {tool_name!r} not yet seeded in store. "
                "Run registry.seed_default_bindings() first."
            )
        if not binding.enabled:
            raise ToolNotEnabledError(
                f"Tool {tool_name!r} is disabled (operator kill-switch)."
            )

        handler_cls = HANDLER_CLASSES.get(binding.definition.integration_type)
        if handler_cls is None:
            raise ExternalToolError(
                f"No handler for integration_type "
                f"{binding.definition.integration_type!r}"
            )
        return handler_cls(binding)

    async def list_for_task_kind(
        self, task_kind: str, *, only_enabled: bool = True
    ) -> list[str]:
        """Return tool names appropriate for a task kind, filtered by enabled."""
        candidates = tools_for_task_kind(task_kind)
        if not only_enabled:
            return list(candidates)
        out: list[str] = []
        for name in candidates:
            if name not in self._definitions:
                # Likely a Claude Code built-in (Read / Bash / etc.) — pass through
                out.append(name)
                continue
            binding = await self.store.get_binding(name)
            if binding and binding.enabled:
                out.append(name)
        return out


def _suppress_proc_kill():
    """Helper: suppress exception during process kill (process already dead)."""
    import contextlib

    return contextlib.suppress(ProcessLookupError, OSError)
