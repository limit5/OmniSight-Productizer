"""Phase 2 — Skills loader (3-scope walk + lazy `Skill` tool dispatch).

Mirrors Claude Code's skill-pack convention so the runner *and* backend
specialist agents can discover, load, and execute markdown-based skills
from three precedence layers:

  1. **Project** — ``<project>/.claude/skills/`` and
     ``<project>/.warp/skills/`` and ``<project>/.omnisight/skills/``
     (highest)
  2. **Home** — ``~/.claude/skills/`` and ``~/.warp/skills/`` and
     ``~/.omnisight/skills/``
  3. **Bundled** — ``<project>/omnisight/agents/skills/`` (lowest,
     ships with repo). ``<project>/configs/skills/`` remains a legacy
     bundled fallback while existing packs are migrated.

Same skill name in a higher scope shadows lower scopes — operators can
override a bundled skill by dropping a same-named ``SKILL.md`` into
their project ``.claude/skills/``, ``.warp/skills/``, or
``.omnisight/skills/``. Within the same scope, ``.omnisight/skills`` has
higher provider rank than ``.warp/skills``, which has higher provider
rank than ``.claude/skills``.

Format support:

  * **YAML frontmatter** (canonical, Claude Code style)::

        ---
        name: mcp-builder
        description: Build MCP servers …
        keywords: [mcp, server, integration]
        ---
        <markdown body>

  * **Legacy header-only** (older OmniSight convention)::

        # SKILL-NEXTJS — W6 #280 (pilot)
        First sentence becomes the description.
        <markdown body>

Both formats are loaded; the registry exposes a uniform :class:`Skill`
dataclass.

Injection strategy: **lazy via the `Skill` tool**. Only the catalog
(name + 1-line description, ~80 chars per entry) goes into the LLM's
system prompt — bodies are fetched on demand via a tool handler. This
keeps prompt-cache prefix small even with 30+ skills.

ADR: TODO row WP.2 freezes this contract.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from backend import feature_flags

logger = logging.getLogger(__name__)


# Where to look for skills, in precedence order. Each entry is a
# (resolver, scope_label) pair — resolver takes the project_root and
# returns a list of directories to scan. Lower index = higher priority.
SCOPE_ORDER: tuple[str, ...] = ("project", "home", "bundled")
_DEFAULT_PROVIDER_RANK = 0
SKILLS_LOADER_ENABLED_ENV = "OMNISIGHT_WP_SKILLS_LOADER_ENABLED"

# ─── Executable-skill containment (U6-0 P-SKILL) ─────────────────
#
# Trust boundary for `*.skill` subprocess execution. The boundary is the
# CURRENT process's imported constants below — the model editing this file
# (or the manifest) on disk does NOT affect the already-imported module.
# Corollary: a dirty, model-modified checkout must never be RESTARTED into
# a runner without review/reset; the manifest-lockstep test is maintenance
# protection, NOT the security boundary.
#
# Update procedure: edit the trusted skill AND its pinned sha256 here in
# ONE reviewed change.

REPO_ROOT = Path(__file__).resolve().parents[2]
"""The singular module-owned repo root — both launchers resolve to it."""

TRUSTED_EXECUTABLE_SKILLS: dict[str, str] = {
    "scripts/skills/run_tests.skill": (
        "ef0116ced93971eb07a31b1fc6b90ceab94b7b4c3f82956464e743f9af43a936"
    ),
    "scripts/skills/fmt.skill": (
        "5c3e3359797bbdd754013277b238ba29045133863c2907d9d665b6e9fea6e9c3"
    ),
    "scripts/skills/lint_changed.skill": (
        "8e31ae6d8f2d80195eeb428e5f650771339a3d616663b4ed23396fc608585099"
    ),
}
"""Pinned manifest: repo-relative path → sha256 of the committed bytes."""

# Kill-switch: truthy ⇒ refuse ALL executable-skill exec (markdown skills
# unaffected). Deliberately a plain env check — a security kill-switch
# must not depend on backend state (feature_flags / DB registries).
EXECUTABLE_SKILLS_DISABLED_ENV = "OMNISIGHT_EXECUTABLE_SKILLS_DISABLED"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}

# WP.2.7 rollback registry: mirrors the pre-WP.2.5 hard-coded
# SKILL_HD_* table from backend.agents.tool_schemas.
_HARD_CODED_SKILL_REGISTRY: tuple[tuple[str, str], ...] = (
    ("SKILL_HD_PARSE", "[HD.1] Parse an EDA file into HDIR."),
    ("SKILL_HD_DIFF_REFERENCE", "[HD.4] Reference vs customer design diff."),
    ("SKILL_HD_SENSOR_SWAP_FEASIBILITY", "[HD.5] Sensor substitution feasibility."),
    ("SKILL_HD_FW_SYNC_PATCH", "[HD.7] HW change -> FW patch list."),
    ("SKILL_HD_PCB_SI_ANALYZE", "[HD.2] PCB signal integrity analysis."),
    ("SKILL_HD_HIL_RUN", "[HD.8] Hardware-in-the-loop session execution."),
    ("SKILL_HD_RAG_QUERY", "[HD.9] Datasheet RAG retrieval."),
    ("SKILL_HD_CERT_RETEST_PLAN", "[HD.10] EMC / safety retest plan generator."),
    ("SKILL_HD_PLATFORM_RESOLVE", "[HD.16] SoC mark -> platform spec lookup."),
    ("SKILL_HD_VENDOR_SYNC", "[HD.16] Vendor SDK upstream sync pipeline."),
    ("SKILL_HD_VENDOR_REBASE", "[HD.16] Patch rebase conflict auto-attempt."),
    ("SKILL_HD_NDA_GATE", "[HD.16] NDA boundary enforcement check."),
    ("SKILL_HD_CUSTOMER_OVERLAY", "[HD.17] Per-customer overlay manifest resolver."),
    ("SKILL_HD_LIFECYCLE_AUDIT", "[HD.18] Annual reproducibility audit."),
    ("SKILL_HD_CVE_IMPACT", "[HD.18] CVE feed -> SBOM impact analysis."),
    (
        "SKILL_HD_CVE_AUTO_BACKPORT",
        "[HD.18] Vendor patch -> customer-overlay backport proposal.",
    ),
    ("SKILL_HD_BRINGUP_CHECKLIST", "[HD.19] SoC-specific bring-up checklist generator."),
    ("SKILL_HD_BRINGUP_LIVE_PARSE", "[HD.19] Live boot console -> AI parse blockers."),
    (
        "SKILL_HD_PORT_ADVISOR",
        "[HD.19] Cross-SoC port required-changes + effort estimate.",
    ),
    ("SKILL_HD_DEVKIT_FORK", "[HD.19] DevKit reference -> customer fork starting point."),
    ("SKILL_HD_ISP_TUNING_DIFF", "[HD.20] ISP tuning binary before/after compare."),
    ("SKILL_HD_BLOB_COMPAT", "[HD.20] (BSP-version, blob-version) compatibility matrix."),
    ("SKILL_HD_PRODUCTION_BUNDLE", "[HD.21] EMS production access bundle generator."),
    ("SKILL_HD_OTA_PACKAGE_GEN", "[HD.21] OTA bundle generation (SWUpdate / RAUC / A-B)."),
    ("SKILL_HD_SBOM_GENERATE", "[HD.21] SBOM CycloneDX + SPDX generation."),
    ("SKILL_HD_LICENSE_AUDIT", "[HD.21] Ship-time license conflict check."),
    ("SKILL_HD_AUTHENTICITY_VERIFY", "[HD.21] Chip authenticity challenge / verification."),
    ("SKILL_HD_AI_COMPANION", "[HD.21] Unified chat surface skill router."),
)


@dataclass(frozen=True)
class Skill:
    """One loaded skill — markdown body + metadata."""

    name: str
    """Unique within registry (after shadowing). Matches the dir name or
    the frontmatter ``name:`` field; frontmatter wins."""

    description: str
    """One-line description used in the system-prompt catalog."""

    keywords: tuple[str, ...] = ()
    """Topical tags from frontmatter; used by future fuzzy lookup."""

    body: str = ""
    """The markdown body BELOW the frontmatter (or the whole file when
    no frontmatter is present). What the LLM actually reads."""

    source_path: Path | None = None
    """Where on disk this skill was loaded from."""

    scope: str = "bundled"
    """Which scope won shadowing — one of SCOPE_ORDER."""

    def to_catalog_entry(self) -> str:
        """One-line summary for the system-prompt catalog."""
        kw = (
            f" (keywords: {', '.join(self.keywords[:5])})"
            if self.keywords
            else ""
        )
        return f"- **{self.name}** — {self.description}{kw}"


# ─── Parser ──────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_KV_RE = re.compile(r"^([\w-]+)\s*:\s*(.*?)\s*$")
_LIST_INLINE_RE = re.compile(r"^\[(.*)\]$")


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    """Tiny YAML-ish parser for skill frontmatter.

    Supports just the shapes we actually use: scalar ``key: value`` and
    inline lists ``key: [a, b, c]``. Multi-line blocks, anchors, and
    nested structures are rejected — those would mean an over-engineered
    skill metadata schema.
    """
    out: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        # Strip wrapping quotes
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        # Inline list?
        lm = _LIST_INLINE_RE.match(value)
        if lm:
            inner = lm.group(1)
            items = [s.strip().strip("'\"") for s in inner.split(",") if s.strip()]
            out[key] = items
        else:
            out[key] = value
    return out


def _legacy_header_description(body: str) -> tuple[str, str]:
    """Pull (description, name_hint) from a legacy-format SKILL.md.

    Convention used by older OmniSight skills:
      ``# SKILL-NEXTJS — W6 #280 (pilot)``
    becomes name_hint=``skill-nextjs``, description=`first prose paragraph`.
    """
    name_hint = ""
    description = ""
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        # Take everything up to the first whitespace as the name token —
        # hyphens stay (so `SKILL-NEXTJS` survives). E.g.:
        #   "SKILL-NEXTJS — W6 #280 (pilot)" → "skill-nextjs"
        #   "MCP Server Development"        → "mcp"
        head = title.split(maxsplit=1)[0] if title else ""
        name_hint = head.lower()
    # First non-empty line after the title block becomes description.
    for line in lines[1:]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        description = s
        break
    return description, name_hint


def parse_skill_file(path: Path, scope: str) -> Skill | None:
    """Read one ``SKILL.md`` (or flat skill file) and return a :class:`Skill`.

    Returns None if the file is empty / unreadable. Logs a warning but
    does not raise — a malformed skill file shouldn't kill the loader.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skill load failed: %s — %s", path, e)
        return None
    if not text.strip():
        return None

    if path.suffix == ".skill":
        description = "(no description)"
        for line in text.splitlines()[:20]:
            match = re.match(r"#\s*description\s*:\s*(.+)", line.strip())
            if match:
                description = match.group(1).strip()
                break
        return Skill(
            name=path.stem,
            description=description,
            body=text,
            source_path=path,
            scope=scope,
        )

    name = ""
    description = ""
    keywords: tuple[str, ...] = ()
    body = text

    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        meta = _parse_yaml_subset(fm_match.group("fm"))
        body = fm_match.group("body").lstrip("\n")
        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()
        kw_raw = meta.get("keywords", ())
        if isinstance(kw_raw, list):
            keywords = tuple(str(k).strip() for k in kw_raw if str(k).strip())
        elif isinstance(kw_raw, str) and kw_raw:
            keywords = tuple(s.strip() for s in kw_raw.split(",") if s.strip())

    if not name or not description:
        legacy_desc, legacy_name = _legacy_header_description(text)
        if not name:
            name = legacy_name
        if not description:
            description = legacy_desc

    if not name:
        # Fall back to parent directory name.
        if path.parent.name and path.parent.name not in {".", "/"}:
            name = path.parent.name
        else:
            name = path.stem
    name = name.strip()
    if not name:
        return None

    return Skill(
        name=name,
        description=description or "(no description)",
        keywords=keywords,
        body=body,
        source_path=path,
        scope=scope,
    )


# ─── Registry ────────────────────────────────────────────────────


class SkillRegistry:
    """In-memory skill catalog with shadowing semantics.

    Add skills with a provider rank; higher rank wins, equal/lower rank
    conflicts are shadowed. Iteration order is alphabetical.

    Module-global state audit: registry instances are caller-local. Every
    worker rebuilds rank decisions from the same filesystem sources.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._provider_ranks: dict[str, int] = {}

    def add(self, skill: Skill, *, provider_rank: int = _DEFAULT_PROVIDER_RANK) -> bool:
        """Add ``skill`` unless a higher/equal-priority entry already won.

        Returns True if accepted, False if shadowed. Duplicate names always
        WARN so operators can see which on-disk source became effective.
        """
        previous = self._skills.get(skill.name)
        if previous is not None:
            previous_rank = self._provider_ranks.get(
                skill.name, _DEFAULT_PROVIDER_RANK
            )
            if provider_rank <= previous_rank:
                logger.warning(
                    "skills_loader: skill %r from %s (%s, rank=%d) "
                    "shadowed by %s (%s, rank=%d)",
                    skill.name,
                    skill.source_path,
                    skill.scope,
                    provider_rank,
                    previous.source_path,
                    previous.scope,
                    previous_rank,
                )
                return False
            logger.warning(
                "skills_loader: skill %r from %s (%s, rank=%d) "
                "overrides %s (%s, rank=%d)",
                skill.name,
                skill.source_path,
                skill.scope,
                provider_rank,
                previous.source_path,
                previous.scope,
                previous_rank,
            )
        self._skills[skill.name] = skill
        self._provider_ranks[skill.name] = provider_rank
        return True

    def provider_rank(self, name: str) -> int | None:
        if name not in self._skills:
            return None
        return self._provider_ranks.get(name, _DEFAULT_PROVIDER_RANK)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def has(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        return sorted(self._skills)

    def list_all(self) -> list[Skill]:
        return [self._skills[n] for n in self.names()]

    def __len__(self) -> int:
        return len(self._skills)


class ProjectWatchedSkillRegistry:
    """Long-lived registry proxy that reloads when project skills change.

    Module-global state audit: each instance keeps a per-worker cache
    derived from the project skill files on disk. Cross-worker consistency
    is guaranteed because workers independently rebuild from the same
    shared filesystem when the project-scope signature changes.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        home: Path | None = None,
        extra_dirs: Iterable[tuple[Path, str]] = (),
    ) -> None:
        self._project_root = project_root
        self._home = home
        self._extra_dirs = tuple(extra_dirs)
        self._signature: tuple[tuple[str, int, int, int], ...] | None = None
        self._registry = SkillRegistry()

    def reload(self) -> None:
        self._signature = _project_scope_signature(self._project_root)
        self._registry = load_default_scopes(
            self._project_root,
            home=self._home,
            extra_dirs=self._extra_dirs,
        )

    def _refresh_if_needed(self) -> None:
        signature = _project_scope_signature(self._project_root)
        if self._signature == signature:
            return
        self._signature = signature
        self._registry = load_default_scopes(
            self._project_root,
            home=self._home,
            extra_dirs=self._extra_dirs,
        )

    def provider_rank(self, name: str) -> int | None:
        self._refresh_if_needed()
        return self._registry.provider_rank(name)

    def get(self, name: str) -> Skill | None:
        self._refresh_if_needed()
        return self._registry.get(name)

    def has(self, name: str) -> bool:
        self._refresh_if_needed()
        return self._registry.has(name)

    def names(self) -> list[str]:
        self._refresh_if_needed()
        return self._registry.names()

    def list_all(self) -> list[Skill]:
        self._refresh_if_needed()
        return self._registry.list_all()

    def __len__(self) -> int:
        self._refresh_if_needed()
        return len(self._registry)


# ─── Scope walking ───────────────────────────────────────────────


def _scan_dir_for_skills(
    root: Path,
    scope: str,
) -> list[Skill]:
    """Find every ``SKILL.md`` (or top-level skill file) under ``root``.

    Convention 1: ``<root>/<skill_name>/SKILL.md`` — preferred shape, also
    the format Claude Code's bundled skills use.

    Convention 2: ``<root>/<skill_name>.md`` — flat layout fallback.
    Convention 3: ``<root>/<skill_name>.skill`` — executable JSON skill.
    """
    if not root.exists() or not root.is_dir():
        return []
    out: list[Skill] = []
    seen_paths: set[Path] = set()
    # Subdir SKILL.md
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        candidate = skill_dir / "SKILL.md"
        if candidate.is_file() and candidate not in seen_paths:
            sk = parse_skill_file(candidate, scope)
            if sk is not None:
                out.append(sk)
            seen_paths.add(candidate)
    # Flat *.md / *.skill (skip README to avoid noise)
    for md in sorted([*root.glob("*.md"), *root.glob("*.skill")]):
        if md.name.lower() in {"readme.md", "index.md"}:
            continue
        if md in seen_paths:
            continue
        sk = parse_skill_file(md, scope)
        if sk is not None:
            out.append(sk)
        seen_paths.add(md)
    return out


def _project_scope_signature(
    project_root: Path,
) -> tuple[tuple[str, int, int, int], ...]:
    """Return a deterministic signature for project-scope skill files."""
    entries: list[tuple[str, int, int, int]] = []
    for root, _provider_rank in _project_scope_dirs(project_root):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            try:
                st = path.stat()
            except OSError:
                continue
            if path.is_dir() or path.name == "SKILL.md" or path.suffix == ".md":
                content_sig = st.st_mtime_ns
                if path.is_file():
                    try:
                        content_sig = int.from_bytes(
                            sha256(path.read_bytes()).digest()[:8],
                            "big",
                        )
                    except OSError:
                        continue
                entries.append(
                    (
                        str(path.relative_to(project_root)),
                        int(path.is_dir()),
                        st.st_size,
                        content_sig,
                    )
                )
    return tuple(entries)


def _project_scope_dirs(project_root: Path) -> list[tuple[Path, int]]:
    return [
        (project_root / ".claude" / "skills", 310),
        (project_root / ".warp" / "skills", 315),
        (project_root / ".omnisight" / "skills", 320),
    ]


def _home_scope_dirs(home: Path | None = None) -> list[tuple[Path, int]]:
    h = home or Path.home()
    return [
        (h / ".claude" / "skills", 210),
        (h / ".warp" / "skills", 215),
        (h / ".omnisight" / "skills", 220),
    ]


def _bundled_scope_dirs(project_root: Path) -> list[tuple[Path, int]]:
    return [
        (project_root / "scripts" / "skills", 130),
        (project_root / "omnisight" / "agents" / "skills", 120),
        (project_root / "configs" / "skills", 110),
    ]


def _skills_loader_enabled() -> bool:
    """Master switch for WP.2 filesystem skill loading.

    Default ON; set ``OMNISIGHT_WP_SKILLS_LOADER_ENABLED=false`` to use
    the pre-WP.2.5 hard-coded SKILL_HD registry. Module-global state audit:
    WP.7.7 resolves registry first, then env fallback, so workers share
    registry invalidation or derive the same process-env decision.
    """
    return feature_flags.resolve_env_backed_feature_flag(
        SKILLS_LOADER_ENABLED_ENV,
        env_mode="true_values",
    )


def _load_hard_coded_registry() -> SkillRegistry:
    """Return the pre-WP.2.5 hard-coded SKILL_HD registry."""
    registry = SkillRegistry()
    for name, description in _HARD_CODED_SKILL_REGISTRY:
        registry.add(
            Skill(
                name=name,
                description=description,
                body=(
                    f"# {name}\n\n"
                    "Hard-coded WP.2 rollback placeholder. Re-enable "
                    f"filesystem skills by unsetting {SKILLS_LOADER_ENABLED_ENV}."
                ),
                scope="hardcoded",
            )
        )
    logger.warning(
        "skills_loader: %s=false; using hard-coded SKILL registry (%d skills)",
        SKILLS_LOADER_ENABLED_ENV,
        len(registry),
    )
    return registry


def load_default_scopes(
    project_root: Path,
    *,
    home: Path | None = None,
    extra_dirs: Iterable[tuple[Path, str]] = (),
) -> SkillRegistry:
    """Load skills with 3-scope precedence.

    Args:
      project_root: Repository root. Project and bundled skill roots are
        scanned relative to it.
      home: Override ``~`` for tests. Defaults to :func:`Path.home`.
      extra_dirs: Optional ``(path, scope_label)`` pairs. Iterated in
        order, treated as the **lowest** priority — useful for embedded
        skill bundles distributed alongside a customer install.
    """
    if not _skills_loader_enabled():
        return _load_hard_coded_registry()

    registry = SkillRegistry()

    def _add_all(dirs: list[tuple[Path, int]], scope: str) -> None:
        for d, provider_rank in dirs:
            for sk in _scan_dir_for_skills(d, scope):
                registry.add(sk, provider_rank=provider_rank)

    _add_all(_project_scope_dirs(project_root), "project")
    _add_all(_home_scope_dirs(home), "home")
    _add_all(_bundled_scope_dirs(project_root), "bundled")
    for extra_dir, label in extra_dirs:
        for sk in _scan_dir_for_skills(extra_dir, label):
            registry.add(sk, provider_rank=_DEFAULT_PROVIDER_RANK)

    if len(registry) > 0:
        scope_counts: dict[str, int] = {}
        for sk in registry.list_all():
            scope_counts[sk.scope] = scope_counts.get(sk.scope, 0) + 1
        logger.info(
            "skills_loader: %d skills loaded — %s",
            len(registry),
            ", ".join(f"{k}={v}" for k, v in sorted(scope_counts.items())),
        )
    return registry


def watch_project_scopes(
    project_root: Path,
    *,
    home: Path | None = None,
    extra_dirs: Iterable[tuple[Path, str]] = (),
) -> ProjectWatchedSkillRegistry:
    """Return a registry proxy that reloads on project skill file changes."""
    return ProjectWatchedSkillRegistry(
        project_root,
        home=home,
        extra_dirs=extra_dirs,
    )


# ─── Tool handler ────────────────────────────────────────────────


def make_skill_handler(registry: SkillRegistry):
    """Build a sync handler for the ``Skill`` tool schema.

    Returns a callable suitable for
    ``ToolDispatcher.register("Skill", handler)``. The handler reads
    ``payload["skill"]`` and returns the markdown body of that skill;
    unknown names produce a structured error so the LLM can recover.
    """

    def _handler(payload: dict[str, Any]) -> Any:
        name = str(payload.get("skill", "")).strip()
        if not name:
            raise ValueError("Skill tool requires non-empty 'skill' field")
        skill = registry.get(name)
        if skill is None:
            top = ", ".join(registry.names()[:20])
            more = (
                f" (showing 20 of {len(registry)})"
                if len(registry) > 20
                else ""
            )
            raise KeyError(
                f"Unknown skill {name!r}. Available{more}: {top}"
            )
        if skill.source_path is not None and skill.source_path.suffix == ".skill":
            return _run_executable_skill(skill, payload)
        args = str(payload.get("args", "") or "").strip()
        body = skill.body
        if args:
            body = f"_(invoked with args: {args})_\n\n{body}"
        return body

    return _handler


class UntrustedExecutableSkillError(ValueError):
    """Refusal to execute a ``*.skill`` file outside the pinned manifest.

    Raised OUT of the Skill tool handler on purpose: ``ToolDispatcher.execute``
    is the layer that catches handler exceptions and converts them into
    ``is_error=True`` tool results. Returning a structured error instead would
    be serialized as a SUCCESSFUL tool result — wrong.

    ``reason`` is one of the bounded values: ``executable_skills_disabled`` /
    ``untrusted_path`` / ``symlink_at_trusted_path`` / ``content_hash_mismatch``.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"executable skill refused: {reason} — only pinned repo-committed "
            "skills may run (TRUSTED_EXECUTABLE_SKILLS)"
        )


def _verify_trusted_executable_skill(script_path: Path) -> bytes:
    """Verify ``script_path`` against the pinned manifest; return its bytes.

    ``script_path`` must be the UNRESOLVED ``skill.source_path``: path
    identity is compared with directories canonicalized but the LEAF never
    resolved (``candidate.parent.resolve() / candidate.name``). A naive
    ``resolve()`` would follow a symlink first and make the symlink check
    dead code — a symlink whose target hashes correctly is still refused,
    because hash equality does not preserve execution context.

    Expected paths are built from the module constants at CALL time.
    Checks, in order:

      1. kill-switch env ⇒ ``executable_skills_disabled``
      2. leaf-unresolved identity against one manifest key ⇒ else
         ``untrusted_path`` (note: a project/home-scope ``run_tests.skill``
         SHADOWS the bundled one at scan time and is then REFUSED here —
         intended fail-closed, not a regression)
      3. ``lstat`` on the matched pinned path: regular file, not a symlink
         ⇒ else ``symlink_at_trusted_path``
      4. single read + sha256 == pinned digest ⇒ else
         ``content_hash_mismatch``

    On success returns the VERIFIED BYTES read in step 4 — callers must
    execute THOSE bytes (never re-open the workspace pathname; see
    :func:`_run_executable_skill`).
    """
    disabled = os.environ.get(EXECUTABLE_SKILLS_DISABLED_ENV, "")
    if disabled.strip().lower() in _TRUTHY_ENV_VALUES:
        raise UntrustedExecutableSkillError("executable_skills_disabled")

    candidate_identity = script_path.parent.resolve() / script_path.name
    matched_path: Path | None = None
    expected_digest: str | None = None
    for key, digest in TRUSTED_EXECUTABLE_SKILLS.items():
        pinned = REPO_ROOT / key
        if candidate_identity == pinned.parent.resolve() / Path(key).name:
            matched_path = pinned.parent.resolve() / Path(key).name
            expected_digest = digest
            break
    if matched_path is None or expected_digest is None:
        raise UntrustedExecutableSkillError("untrusted_path")

    if matched_path.is_symlink() or not stat.S_ISREG(
        matched_path.lstat().st_mode
    ):
        raise UntrustedExecutableSkillError("symlink_at_trusted_path")

    data = matched_path.read_bytes()
    if sha256(data).hexdigest() != expected_digest:
        raise UntrustedExecutableSkillError("content_hash_mismatch")
    return data


def _run_executable_skill(skill: Skill, payload: dict[str, Any]) -> Any:
    """Execute a verified ``*.skill`` file using JSON stdin/stdout.

    TOCTOU containment: verify-then-execute-the-pathname is racy (a
    previously-launched background process can swap the file between the
    hash check and Python re-opening it; hardlinks defeat naive symlink
    prohibition). So the workspace pathname is NEVER re-opened for
    execution — the verified bytes are written to a fresh private snapshot
    (``mkstemp`` inside a 0700 ``mkdtemp``, unpredictable name, created by
    this process) and THAT snapshot is executed, then deleted.

    Residual risk: same-uid processes can theoretically still race the
    private snapshot; the unpredictable name plus the open-write-close-exec
    window makes this impractical — noted rather than claiming perfection.
    """
    if skill.source_path is None:
        raise ValueError(f"Executable skill {skill.name!r} has no source path")
    verified_bytes = _verify_trusted_executable_skill(skill.source_path)
    args = payload.get("args")
    if isinstance(args, dict):
        skill_input = dict(args)
    elif isinstance(args, str) and args.strip():
        try:
            decoded = json.loads(args)
        except json.JSONDecodeError:
            decoded = {"args": args}
        skill_input = decoded if isinstance(decoded, dict) else {"args": decoded}
    else:
        skill_input = {}
    for key, value in payload.items():
        if key not in {"skill", "args"}:
            skill_input[key] = value

    snapshot_dir = tempfile.mkdtemp(prefix="omnisight-skill-")
    try:
        fd, snapshot_path = tempfile.mkstemp(
            dir=snapshot_dir, suffix=".skill"
        )
        try:
            os.write(fd, verified_bytes)
        finally:
            os.close(fd)
        result = subprocess.run(
            [sys.executable, snapshot_path],
            input=json.dumps(skill_input),
            text=True,
            capture_output=True,
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            timeout=600,
            check=False,
        )
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Executable skill {skill.name!r} failed with exit "
            f"{result.returncode}: {detail[:1000]}"
        )
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Executable skill {skill.name!r} returned non-JSON output"
        ) from e


# ─── Catalog rendering for system prompt ────────────────────────


def render_catalog_for_prompt(
    registry: SkillRegistry,
    *,
    max_entries: int = 60,
) -> str:
    """Render a system-prompt-friendly catalog of available skills.

    Caps at ``max_entries`` to avoid blowing the cached prefix when the
    operator has 100+ skills installed. Always includes a hint about how
    to invoke via the ``Skill`` tool.
    """
    if len(registry) == 0:
        return ""
    lines = [
        "# 可用 Skills（lazy load — 用 Skill tool 載入完整內容）",
        f"共 {len(registry)} 個 skill；下面是 catalog（name + 1-line desc）。",
        "需要某 skill 詳細內容時，呼叫 Skill tool 並傳 `skill: <name>`。",
        "",
    ]
    for sk in registry.list_all()[:max_entries]:
        lines.append(sk.to_catalog_entry())
    if len(registry) > max_entries:
        remaining = len(registry) - max_entries
        lines.append(f"… 還有 {remaining} 個未列出（呼叫 Skill 直接載入即可）")
    lines.append("")
    return "\n".join(lines)
