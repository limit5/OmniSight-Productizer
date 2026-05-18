"""WP.8 — Runbook primitive + Block→Runbook synthesizer + execution chain.

Mirrors :mod:`backend.agents.skills_loader` (WP.2) for parity:

  * 3-scope discovery (project / home / bundled) with shadowing
  * YAML frontmatter parsing (delegated to PyYAML for the runbook body
    because runbook steps + params are nested, unlike the flat skill
    frontmatter)
  * Lazy registry with shadowing semantics

A Runbook is a YAML document with the shape::

    name: my-bring-up
    description: SoC bring-up checklist for RK3588
    tags: [hd, bring-up]
    source_url: omnisight://block/blk-abc123
    params:
      - name: target_soc
        type: string
        default: rk3588
        description: SoC mark
    steps:
      - kind: command
        title: Probe USB
        command: lsusb | grep -i "{{ target_soc }}"
      - kind: prompt
        title: Confirm console
        prompt: Did the boot console reach login?
      - kind: skill
        title: Parse boot log
        skill: SKILL_HD_BRINGUP_LIVE_PARSE
        args: {log: "{{ boot_log }}"}

Block→Runbook synthesis (the "Save Block as Runbook" feature) derives
this shape from a WP.1 :class:`backend.models.Block`: free ``{{ var }}``
placeholders in the block's command / prompt become inferred params; the
block's ``kind`` becomes the step kind; the block's id becomes the
``source_url`` for lineage trace.

Runbook execution prompts the operator for params, applies them to the
step payloads via simple ``{{ var }}`` substitution, and materialises
one :class:`backend.models.Block` per step with ``parent_id`` chaining
back through the synthesised root → the WP.1 lineage path stays intact.

ADR anchor: ``docs/design/wp-warp-inspired-patterns.md`` §"WP.8".
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from backend.models import Block

logger = logging.getLogger(__name__)


SCOPE_ORDER: tuple[str, ...] = ("project", "home", "bundled")
_DEFAULT_PROVIDER_RANK = 0

# Step kinds we accept. Anything outside this set is normalised to
# ``"comment"`` so a malformed step doesn't crash the executor.
VALID_STEP_KINDS: frozenset[str] = frozenset(
    {"command", "prompt", "skill", "comment", "agent_turn"}
)

# Param types we accept. Anything outside is treated as ``"string"`` so
# runbooks remain forward-compatible with future type additions.
VALID_PARAM_TYPES: frozenset[str] = frozenset(
    {"string", "int", "float", "bool", "path"}
)

# Matches ``{{ name }}`` with optional whitespace; used to find inferred
# params in synthesised step payloads.
_PARAM_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


# ─── Dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RunbookParam:
    """One parameter the operator must (or may) fill in at run-time."""

    name: str
    type: str = "string"
    default: Any = None
    description: str = ""
    required: bool = True

    def coerce(self, value: Any) -> Any:
        """Apply best-effort type coercion before substitution.

        Coercion failures fall back to the raw string so the executor
        can still attempt substitution — runbooks should be permissive,
        not strict, at the param boundary.
        """
        if value is None:
            return self.default
        if self.type == "int":
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
        if self.type == "float":
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "on", "y"}:
                return True
            if text in {"0", "false", "no", "off", "n", ""}:
                return False
            return value
        return str(value)


@dataclass(frozen=True)
class RunbookStep:
    """One step in the runbook execution sequence."""

    kind: str
    title: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Runbook:
    """One loaded runbook — YAML body + metadata."""

    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    source_url: str = ""
    params: tuple[RunbookParam, ...] = ()
    steps: tuple[RunbookStep, ...] = ()
    source_path: Path | None = None
    scope: str = "bundled"

    def param(self, name: str) -> RunbookParam | None:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def to_catalog_entry(self) -> str:
        """One-line summary for the system-prompt catalog / dashboard list."""
        tag_part = f" [{', '.join(self.tags[:4])}]" if self.tags else ""
        return f"- **{self.name}** — {self.description}{tag_part}"


# ─── YAML parsing ────────────────────────────────────────────────


def _coerce_param(raw: Any) -> RunbookParam | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", "")).strip()
    if not name:
        return None
    ptype = str(raw.get("type", "string")).strip().lower()
    if ptype not in VALID_PARAM_TYPES:
        ptype = "string"
    default = raw.get("default")
    description = str(raw.get("description", "")).strip()
    # A param is required iff it has no default; matches the "prompt for
    # everything that wasn't pre-filled" UX from the design doc.
    required_raw = raw.get("required")
    if required_raw is None:
        required = default is None
    else:
        required = bool(required_raw)
    return RunbookParam(
        name=name,
        type=ptype,
        default=default,
        description=description,
        required=required,
    )


def _coerce_step(raw: Any) -> RunbookStep | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", "")).strip().lower()
    if not kind:
        return None
    if kind not in VALID_STEP_KINDS:
        kind = "comment"
    title = str(raw.get("title", "")).strip()
    payload = {
        k: v
        for k, v in raw.items()
        if k not in {"kind", "title"}
    }
    return RunbookStep(kind=kind, title=title, payload=payload)


def parse_runbook_text(text: str, scope: str, source_path: Path | None = None) -> Runbook | None:
    """Parse a YAML runbook body into a :class:`Runbook`.

    Returns None for empty / unparseable input. Logs a warning but does
    not raise — a malformed runbook should never kill the loader.
    """
    if not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("runbook_loader: YAML parse failed (%s): %s", source_path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning(
            "runbook_loader: top-level YAML is not a mapping (%s)",
            source_path,
        )
        return None

    name = str(data.get("name", "")).strip()
    if not name and source_path is not None:
        name = source_path.stem
    name = name.strip()
    if not name:
        return None

    description = str(data.get("description", "")).strip()
    tags_raw = data.get("tags") or ()
    if isinstance(tags_raw, str):
        tags = tuple(t.strip() for t in tags_raw.split(",") if t.strip())
    elif isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(t).strip() for t in tags_raw if str(t).strip())
    else:
        tags = ()
    source_url = str(data.get("source_url", "")).strip()

    params_raw = data.get("params") or ()
    params: list[RunbookParam] = []
    if isinstance(params_raw, list):
        for entry in params_raw:
            p = _coerce_param(entry)
            if p is not None:
                params.append(p)

    steps_raw = data.get("steps") or ()
    steps: list[RunbookStep] = []
    if isinstance(steps_raw, list):
        for entry in steps_raw:
            s = _coerce_step(entry)
            if s is not None:
                steps.append(s)

    return Runbook(
        name=name,
        description=description,
        tags=tags,
        source_url=source_url,
        params=tuple(params),
        steps=tuple(steps),
        source_path=source_path,
        scope=scope,
    )


def parse_runbook_file(path: Path, scope: str) -> Runbook | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("runbook_loader: read failed %s: %s", path, exc)
        return None
    return parse_runbook_text(text, scope=scope, source_path=path)


# ─── Registry ────────────────────────────────────────────────────


class RunbookRegistry:
    """In-memory runbook catalog with shadowing semantics.

    Same shape as :class:`backend.agents.skills_loader.SkillRegistry`:
    higher provider rank wins; equal/lower rank entries get logged as
    shadowed. This keeps operator override stories identical between
    WP.2 (skills) and WP.8 (runbooks).
    """

    def __init__(self) -> None:
        self._runbooks: dict[str, Runbook] = {}
        self._provider_ranks: dict[str, int] = {}

    def add(self, runbook: Runbook, *, provider_rank: int = _DEFAULT_PROVIDER_RANK) -> bool:
        previous = self._runbooks.get(runbook.name)
        if previous is not None:
            previous_rank = self._provider_ranks.get(runbook.name, _DEFAULT_PROVIDER_RANK)
            if provider_rank <= previous_rank:
                logger.warning(
                    "runbook_loader: runbook %r from %s (%s, rank=%d) "
                    "shadowed by %s (%s, rank=%d)",
                    runbook.name,
                    runbook.source_path,
                    runbook.scope,
                    provider_rank,
                    previous.source_path,
                    previous.scope,
                    previous_rank,
                )
                return False
            logger.warning(
                "runbook_loader: runbook %r from %s (%s, rank=%d) "
                "overrides %s (%s, rank=%d)",
                runbook.name,
                runbook.source_path,
                runbook.scope,
                provider_rank,
                previous.source_path,
                previous.scope,
                previous_rank,
            )
        self._runbooks[runbook.name] = runbook
        self._provider_ranks[runbook.name] = provider_rank
        return True

    def get(self, name: str) -> Runbook | None:
        return self._runbooks.get(name)

    def has(self, name: str) -> bool:
        return name in self._runbooks

    def names(self) -> list[str]:
        return sorted(self._runbooks)

    def list_all(self) -> list[Runbook]:
        return [self._runbooks[n] for n in self.names()]

    def provider_rank(self, name: str) -> int | None:
        if name not in self._runbooks:
            return None
        return self._provider_ranks.get(name, _DEFAULT_PROVIDER_RANK)

    def __len__(self) -> int:
        return len(self._runbooks)

    def __iter__(self) -> Iterator[Runbook]:
        return iter(self.list_all())


# ─── Scope walking ───────────────────────────────────────────────


def _project_scope_dirs(project_root: Path) -> list[tuple[Path, int]]:
    return [
        (project_root / ".claude" / "runbooks", 310),
        (project_root / ".warp" / "runbooks", 315),
        (project_root / ".omnisight" / "runbooks", 320),
    ]


def _home_scope_dirs(home: Path | None = None) -> list[tuple[Path, int]]:
    h = home or Path.home()
    return [
        (h / ".claude" / "runbooks", 210),
        (h / ".warp" / "runbooks", 215),
        (h / ".omnisight" / "runbooks", 220),
    ]


def _bundled_scope_dirs(project_root: Path) -> list[tuple[Path, int]]:
    return [
        (project_root / "configs" / "runbooks", 110),
        (project_root / "omnisight" / "agents" / "runbooks", 120),
        (project_root / "scripts" / "runbooks", 130),
    ]


def _scan_dir(root: Path, scope: str) -> list[Runbook]:
    if not root.exists() or not root.is_dir():
        return []
    out: list[Runbook] = []
    for path in sorted([*root.glob("*.yaml"), *root.glob("*.yml")]):
        rb = parse_runbook_file(path, scope)
        if rb is not None:
            out.append(rb)
    return out


def load_default_scopes(
    project_root: Path,
    *,
    home: Path | None = None,
    extra_dirs: Iterable[tuple[Path, str]] = (),
) -> RunbookRegistry:
    """Load runbooks with 3-scope precedence (project > home > bundled)."""
    registry = RunbookRegistry()

    def _add_all(dirs: list[tuple[Path, int]], scope: str) -> None:
        for d, rank in dirs:
            for rb in _scan_dir(d, scope):
                registry.add(rb, provider_rank=rank)

    _add_all(_project_scope_dirs(project_root), "project")
    _add_all(_home_scope_dirs(home), "home")
    _add_all(_bundled_scope_dirs(project_root), "bundled")
    for extra_dir, label in extra_dirs:
        for rb in _scan_dir(extra_dir, label):
            registry.add(rb, provider_rank=_DEFAULT_PROVIDER_RANK)

    if len(registry) > 0:
        scope_counts: dict[str, int] = {}
        for rb in registry.list_all():
            scope_counts[rb.scope] = scope_counts.get(rb.scope, 0) + 1
        logger.info(
            "runbook_loader: %d runbooks loaded — %s",
            len(registry),
            ", ".join(f"{k}={v}" for k, v in sorted(scope_counts.items())),
        )
    return registry


# ─── Block→Runbook synthesizer ───────────────────────────────────


def _slugify(text: str) -> str:
    """Lowercase + dash; non-alnum collapses to dash; bounded to 64 chars.

    Falls back to ``runbook`` so a Block with an empty title still
    produces a writable filename.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:64] or "runbook"


def _infer_params_from_text(text: str, seen: set[str]) -> list[RunbookParam]:
    """Pull ``{{ name }}`` placeholders out of payload text fields.

    Returns RunbookParam stubs (type=string, no default) for each
    placeholder not already in ``seen``. ``seen`` is mutated in-place so
    a placeholder appearing in multiple steps is emitted only once.
    """
    out: list[RunbookParam] = []
    for match in _PARAM_PLACEHOLDER_RE.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(
            RunbookParam(
                name=name,
                type="string",
                default=None,
                description=f"(inferred from {{{{ {name} }}}} placeholder)",
                required=True,
            )
        )
    return out


def synthesize_runbook_from_block(
    block: Block,
    *,
    name: str | None = None,
    description: str | None = None,
    tags: Iterable[str] = (),
) -> Runbook:
    """Derive a Runbook stub from a single :class:`Block`.

    The resulting runbook is a single-step pipeline whose ``source_url``
    pins the originating block id (so future executions can be traced
    back to the WP.1 lineage). Free ``{{ var }}`` placeholders in the
    block's payload (``command`` / ``prompt`` / ``text``) get promoted
    to inferred params with type=string and no default — the parameter-
    prompt UI will surface them to the operator at execution time.
    """
    rb_name = (name or block.title or block.kind or "runbook").strip()
    rb_name = _slugify(rb_name)
    rb_desc = (description or block.title or f"Saved from block {block.block_id}").strip()

    # Step kind defaults to the block's kind when recognised. Anything
    # the runbook executor doesn't know how to dispatch is captured as a
    # plain ``comment`` step so the operator can edit it later.
    step_kind = block.kind if block.kind in VALID_STEP_KINDS else "comment"

    payload_keys = ("command", "prompt", "text", "body")
    step_payload: dict[str, Any] = {}
    text_blob_parts: list[str] = []
    for key in payload_keys:
        value = block.payload.get(key)
        if isinstance(value, str) and value.strip():
            step_payload[key] = value
            text_blob_parts.append(value)
    # Preserve any non-text payload fields (args dicts, etc.) verbatim so
    # the synthesised runbook can still re-execute the original block.
    for key, value in block.payload.items():
        if key in step_payload:
            continue
        if isinstance(value, (dict, list, int, float, bool)) or value is None:
            step_payload[key] = value

    seen: set[str] = set()
    params = _infer_params_from_text("\n".join(text_blob_parts), seen)

    step = RunbookStep(
        kind=step_kind,
        title=block.title or block.kind or rb_name,
        payload=step_payload,
    )

    tag_tuple = tuple(t.strip() for t in tags if str(t).strip())
    if not tag_tuple and block.kind:
        tag_tuple = (block.kind,)

    return Runbook(
        name=rb_name,
        description=rb_desc,
        tags=tag_tuple,
        source_url=f"omnisight://block/{block.block_id}",
        params=tuple(params),
        steps=(step,),
        scope="project",
    )


def runbook_to_yaml(runbook: Runbook) -> str:
    """Serialise a :class:`Runbook` to the canonical YAML on-disk shape.

    Used by the ``POST /runbooks`` save endpoint and by the synthesizer
    preview so the operator sees the exact bytes that will land on disk.
    """
    payload: dict[str, Any] = {
        "name": runbook.name,
        "description": runbook.description,
    }
    if runbook.tags:
        payload["tags"] = list(runbook.tags)
    if runbook.source_url:
        payload["source_url"] = runbook.source_url
    if runbook.params:
        payload["params"] = [
            {
                "name": p.name,
                "type": p.type,
                "default": p.default,
                "description": p.description,
                "required": p.required,
            }
            for p in runbook.params
        ]
    if runbook.steps:
        payload["steps"] = [
            {"kind": s.kind, "title": s.title, **s.payload}
            for s in runbook.steps
        ]
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


# ─── Execution ───────────────────────────────────────────────────


def _substitute(value: Any, resolved: dict[str, Any]) -> Any:
    """Recursively substitute ``{{ name }}`` placeholders in payload values."""
    if isinstance(value, str):
        def _repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in resolved and resolved[key] is not None:
                return str(resolved[key])
            return match.group(0)
        return _PARAM_PLACEHOLDER_RE.sub(_repl, value)
    if isinstance(value, list):
        return [_substitute(v, resolved) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, resolved) for k, v in value.items()}
    return value


def resolve_params(
    runbook: Runbook,
    supplied: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Resolve user-supplied params against the runbook declaration.

    Returns (resolved_values, missing_required). Coerces types per the
    param declaration. Missing required params are returned for the UI
    layer to prompt for; the executor will refuse to dispatch until the
    list is empty so a half-filled run can't quietly corrupt state.
    """
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    declared_names = {p.name for p in runbook.params}

    for p in runbook.params:
        if p.name in supplied and supplied[p.name] not in (None, ""):
            resolved[p.name] = p.coerce(supplied[p.name])
        elif p.default is not None:
            resolved[p.name] = p.coerce(p.default)
        else:
            if p.required:
                missing.append(p.name)
            resolved[p.name] = None

    for k, v in supplied.items():
        if k not in declared_names and v is not None:
            resolved[k] = v

    return resolved, missing


class RunbookExecutionError(RuntimeError):
    """Raised when execution can't proceed (e.g. missing required params)."""


def execute_runbook(
    runbook: Runbook,
    supplied_params: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    parent_block_id: str | None = None,
) -> list[Block]:
    """Materialise a runbook into a chain of :class:`Block` instances.

    Each step becomes one Block whose ``parent_id`` points at the
    previous step's block (or at ``parent_block_id`` for the first
    step). The first block's payload pins the runbook name + resolved
    params under ``runbook.*`` so a downstream operator can re-derive
    "this Block was produced by runbook X with params Y" without going
    back to the lineage table.

    The Block ``kind`` is set to ``runbook_step`` per the WP.1 kind
    enumeration in the design doc, and the step's original kind is
    stashed under ``payload['step_kind']`` so executors / UI surfaces
    can dispatch to the right renderer.
    """
    resolved, missing = resolve_params(runbook, supplied_params)
    if missing:
        raise RunbookExecutionError(
            f"runbook {runbook.name!r} missing required params: {sorted(missing)}"
        )

    chain: list[Block] = []
    prev_id = parent_block_id
    for index, step in enumerate(runbook.steps):
        substituted = _substitute(dict(step.payload), resolved)
        block_payload: dict[str, Any] = {
            "step_index": index,
            "step_kind": step.kind,
            **substituted,
        }
        if index == 0:
            block_payload["runbook"] = {
                "name": runbook.name,
                "source_url": runbook.source_url,
                "params": dict(resolved),
            }
        block = Block(
            block_id=f"blk-{uuid.uuid4().hex[:12]}",
            parent_id=prev_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            kind="runbook_step",
            status="completed",
            title=step.title or f"{runbook.name} step {index + 1}",
            payload=block_payload,
            metadata={
                "runbook_name": runbook.name,
                "runbook_step_index": index,
                "runbook_source_url": runbook.source_url,
            },
        )
        chain.append(block)
        prev_id = block.block_id
    return chain


# ─── Save-to-disk helper ─────────────────────────────────────────


def write_project_runbook(
    runbook: Runbook,
    project_root: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Write a synthesised runbook to the project-scope directory.

    Directory: ``<project_root>/.omnisight/runbooks/<name>.yaml`` —
    matches the highest-priority project scope so the freshly-saved
    runbook is immediately discoverable by the next ``load_default_scopes``
    call. Refuses to overwrite an existing file unless ``overwrite=True``
    so an accidental same-name save can't silently clobber an operator-
    curated runbook.
    """
    target_dir = project_root / ".omnisight" / "runbooks"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{runbook.name}.yaml"
    if target_path.exists() and not overwrite:
        raise FileExistsError(
            f"runbook {runbook.name!r} already exists at {target_path}; "
            "pass overwrite=True or rename the runbook"
        )
    target_path.write_text(runbook_to_yaml(runbook), encoding="utf-8")
    return target_path
