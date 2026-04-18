"""Prompt Loader — assembles system prompts from model rules, role skills, and handoff context.

Directory layout::

    configs/models/*.md              Model-specific behavior rules
    configs/roles/{category}/*.skill.md   Role-specific skill definitions

Prompt assembly order:
    1. Model rules  (how to behave with this LLM)
    2. Role skill   (domain expertise for this agent role)
    3. Handoff      (context from previous agent, if any)

Falls back to built-in prompts when config files are missing.

B15 #350 — Skill Lazy Loading (Progressive Disclosure):
  * ``build_system_prompt(mode="eager")``  — legacy behaviour: the role skill's
    full markdown body is inlined (~50K chars, ~12.5K tokens).
  * ``build_system_prompt(mode="lazy")``   — Phase 1: only a compact metadata
    catalog (~500 chars) is injected; the agent consults it and emits
    ``[LOAD_SKILL: <name>]`` to request a full body.
  * ``build_skill_injection(domain_context, user_prompt, …)`` — Phase 2: given
    the CATC ``domain_context`` plus the user's prompt, keyword-match against
    the catalog and return the full bodies of the top-ranked skills so the
    ReAct loop can inject them on demand.

The default mode follows the ``OMNISIGHT_SKILL_LOADING`` env var (``eager`` if
unset) so the optimisation can be rolled out behind a feature flag without
changing caller code.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIGS_ROOT = _PROJECT_ROOT / "configs"
_MODELS_DIR = _CONFIGS_ROOT / "models"
_ROLES_DIR = _CONFIGS_ROOT / "roles"
_SKILLS_DIR = _CONFIGS_ROOT / "skills"
_CLAUDE_MD = _PROJECT_ROOT / "CLAUDE.md"

# Maximum prompt section lengths (rough char counts) to avoid blowing context
_MAX_CORE_RULES = 2000
_MAX_MODEL_RULES = 3000
_MAX_ROLE_SKILL = 8000
_MAX_TASK_SKILL = 4000
_MAX_HANDOFF = 4000

# L1 Core Rules cache (loaded once)
_core_rules_cache: str | None = None


def load_core_rules() -> str:
    """Load CLAUDE.md core rules (L1 Memory). Cached after first call."""
    global _core_rules_cache
    if _core_rules_cache is not None:
        return _core_rules_cache
    if _CLAUDE_MD.is_file():
        content = _read_md(_CLAUDE_MD, _MAX_CORE_RULES)
        _core_rules_cache = content
        logger.info("Loaded L1 core rules from CLAUDE.md (%d chars)", len(content))
        return content
    _core_rules_cache = ""
    return ""


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from markdown content."""
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL).strip()


def _read_md(path: Path, max_chars: int) -> str:
    """Read a markdown file, strip frontmatter, truncate if needed."""
    if not path.exists():
        return ""
    content = _strip_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    if len(content) > max_chars:
        content = content[:max_chars] + "\n... [truncated]"
    return content


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Model Rules
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fuzzy_match_model_file(model_name: str) -> Path | None:
    """Find the best matching model rule file using fuzzy prefix match.

    Examples::
        "claude-sonnet-4-20250514" → claude-sonnet.md
        "gpt-4o"                   → gpt.md
        "gemini-1.5-pro"           → gemini.md
        "unknown-model"            → _default.md
    """
    if not _MODELS_DIR.is_dir():
        return None

    name = model_name.lower()
    candidates = sorted(_MODELS_DIR.glob("*.md"), key=lambda p: len(p.stem), reverse=True)

    # Exact match first
    exact = _MODELS_DIR / f"{name}.md"
    if exact.exists():
        return exact

    # Longest prefix match
    for candidate in candidates:
        stem = candidate.stem.lower()
        if stem.startswith("_"):
            continue  # skip _default
        if name.startswith(stem):
            return candidate

    # Fallback to _default.md
    default = _MODELS_DIR / "_default.md"
    return default if default.exists() else None


def load_model_rules(model_name: str) -> str:
    """Load model-specific behavior rules."""
    if not model_name:
        path = _MODELS_DIR / "_default.md"
    else:
        path = _fuzzy_match_model_file(model_name)

    if not path:
        return ""

    content = _read_md(path, _MAX_MODEL_RULES)
    if content:
        logger.debug("Loaded model rules from %s for model=%s", path.name, model_name)
    return content


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Role Skills
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_role_skill(category: str, role_id: str) -> str:
    """Load role-specific skill definition.

    Args:
        category: Agent type / category (firmware, software, etc.)
        role_id: Sub-type / role ID (bsp, isp, algorithm, etc.)
    """
    if not role_id:
        return ""

    path = _ROLES_DIR / category / f"{role_id}.skill.md"
    content = _read_md(path, _MAX_ROLE_SKILL)
    if content:
        logger.debug("Loaded role skill: %s/%s", category, role_id)
    return content


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task skill loading (Anthropic SKILL.md format)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_task_skills_cache: dict[str, dict] | None = None


def load_task_skill(task_type: str) -> str:
    """Load a task-specific skill definition (Anthropic format).

    Looks for ``configs/skills/{task_type}/SKILL.md``.

    Args:
        task_type: Task skill identifier (e.g. 'webapp-testing', 'pdf-generation').

    Returns:
        Skill content (markdown body without frontmatter), or "" if not found.
    """
    if not task_type:
        return ""
    path = (_SKILLS_DIR / task_type / "SKILL.md").resolve()
    if not str(path).startswith(str(_SKILLS_DIR.resolve())):
        logger.warning("Task skill path traversal blocked: %s", task_type)
        return ""
    content = _read_md(path, _MAX_TASK_SKILL)
    if content:
        logger.debug("Loaded task skill: %s", task_type)
    return content


def match_task_skill(task_title: str) -> str:
    """Match a task title against available task skills using keywords.

    Returns the task_type of the best-matching skill, or "" if no match.
    """
    if not task_title:
        return ""
    title_lower = task_title.lower()
    skills = list_available_task_skills()
    best_type = ""
    best_score = 0
    for skill in skills:
        score = sum(1 for kw in skill.get("keywords", []) if kw in title_lower)
        if score > best_score:
            best_score = score
            best_type = skill["name"]
    return best_type if best_score > 0 else ""


def list_available_task_skills() -> list[dict]:
    """Scan configs/skills/ and return all available task skill definitions. Cached."""
    global _task_skills_cache
    if _task_skills_cache is not None:
        return list(_task_skills_cache.values())
    _task_skills_cache = {}
    if not _SKILLS_DIR.is_dir():
        return []
    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if skill_dir.is_dir() and skill_file.exists():
            meta = _parse_frontmatter(skill_file)
            if meta.get("name"):
                _task_skills_cache[meta["name"]] = meta
    logger.info("Loaded %d task skill definitions", len(_task_skills_cache))
    return list(_task_skills_cache.values())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Role listing & keywords
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_roles_cache: list[dict] | None = None


def list_available_roles() -> list[dict]:
    """Scan configs/roles/ and return all available role definitions. Cached after first call."""
    global _roles_cache
    if _roles_cache is not None:
        return list(_roles_cache)  # Return copy to prevent mutation

    roles: list[dict] = []
    if not _ROLES_DIR.is_dir():
        return roles

    for category_dir in sorted(_ROLES_DIR.iterdir()):
        if not category_dir.is_dir():
            continue
        category = category_dir.name
        for skill_file in sorted(category_dir.glob("*.skill.md")):
            role_id = skill_file.stem.replace(".skill", "")
            meta = _parse_frontmatter(skill_file)
            roles.append({
                "role_id": role_id,
                "category": category,
                "label": meta.get("label", role_id),
                "label_en": meta.get("label_en", ""),
                "description": meta.get("description", ""),
                "keywords": meta.get("keywords", []),
                "tools": meta.get("tools", []),
            })
    _roles_cache = roles
    return list(roles)


def list_available_models() -> list[dict]:
    """Scan configs/models/ and return all available model rule definitions."""
    models: list[dict] = []
    if not _MODELS_DIR.is_dir():
        return models

    for md_file in sorted(_MODELS_DIR.glob("*.md")):
        meta = _parse_frontmatter(md_file)
        models.append({
            "file": md_file.stem,
            "model_id": meta.get("model_id", md_file.stem),
            "provider": meta.get("provider", ""),
            "family": meta.get("family", ""),
            "context_window": meta.get("context_window", 0),
            "strengths": meta.get("strengths", []),
        })
    return models


_role_keywords_cache: dict[str, list[str]] = {}


def get_role_keywords(category: str, role_id: str) -> list[str]:
    """Get keywords for a role from its skill file frontmatter. Cached."""
    cache_key = f"{category}/{role_id}"
    if cache_key in _role_keywords_cache:
        return _role_keywords_cache[cache_key]

    path = _ROLES_DIR / category / f"{role_id}.skill.md"
    meta = _parse_frontmatter(path)
    keywords = meta.get("keywords", [])
    _role_keywords_cache[cache_key] = keywords
    return keywords


def _parse_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter as a dict. Returns {} if none found."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}

    import yaml
    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception as exc:
        logger.warning("Invalid YAML frontmatter in %s: %s", path, exc)
        return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  B15 #350 — Skill catalog (Phase 1) + on-demand matching (Phase 2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Total chars allowed for the skill metadata catalog in lazy mode.
# ~500 chars/skill × ~10 skills advertised = ~5K chars (~1.2K tokens) versus
# ~50K chars if we inlined full bodies. The catalog is truncated if a
# deployment ships a huge skill library.
_MAX_SKILL_CATALOG = 6000

# How many skills Phase 2 may inject at once. Guardrails against a runaway
# ReAct loop that would otherwise pull in every skill in the repo.
_MAX_PHASE2_SKILLS = 3
_MAX_PHASE2_CHARS = _MAX_ROLE_SKILL  # reuse role-skill budget for parity

_SKILL_CATALOG_PREAMBLE = (
    "The following skills are available **on demand** — their full bodies "
    "are NOT pre-loaded to save tokens. To consult a skill's full content, "
    "emit the marker `[LOAD_SKILL: <skill_name>]` on its own line and the "
    "system will inject that skill's body into the next turn."
)


def _resolve_skill_loading_mode(requested: str | None) -> str:
    """Return ``"eager"`` or ``"lazy"``. Explicit caller arg wins; otherwise
    fall back to ``OMNISIGHT_SKILL_LOADING`` env var; otherwise ``"eager"``
    for backward compatibility."""
    if requested in ("eager", "lazy"):
        return requested
    env = (os.environ.get("OMNISIGHT_SKILL_LOADING") or "").strip().lower()
    return env if env in ("eager", "lazy") else "eager"


def list_all_skills_metadata() -> list[dict]:
    """Enumerate every skill under ``configs/roles/**/*.skill.md`` and
    ``configs/skills/*/SKILL.md`` and return its metadata card
    (name, description, trigger_condition, token_cost, path, …).

    Skills with no parseable content are skipped. Used by Phase 1 to build
    the catalog and by Phase 2 to score matches."""
    # Deferred import avoids a circular edge: prompt_registry also imports
    # from the same project tree during bootstrap.
    from backend.prompt_registry import get_skill_metadata

    out: list[dict] = []
    seen_paths: set[str] = set()

    # Role skills — configs/roles/{category}/{role}.skill.md
    if _ROLES_DIR.is_dir():
        for category_dir in sorted(_ROLES_DIR.iterdir()):
            if not category_dir.is_dir():
                continue
            for skill_file in sorted(category_dir.glob("*.skill.md")):
                meta = get_skill_metadata(str(skill_file))
                if not meta:
                    continue
                meta["category"] = category_dir.name
                meta["role_id"] = skill_file.stem.replace(".skill", "")
                meta["kind"] = "role"
                seen_paths.add(meta.get("path", ""))
                out.append(meta)

    # Task skills — configs/skills/{name}/SKILL.md
    if _SKILLS_DIR.is_dir():
        for skill_dir in sorted(_SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            meta = get_skill_metadata(str(skill_file))
            if not meta:
                continue
            if meta.get("path") in seen_paths:
                continue
            meta.setdefault("name", skill_dir.name)
            meta["kind"] = "task"
            out.append(meta)

    return out


def _format_skill_card(meta: dict) -> str:
    """Render one metadata entry as a catalog bullet. Keeps each card under
    ~250 chars so the full catalog stays near the ~500-char-per-skill budget
    set out in the B15 design note."""
    name = meta.get("name") or "?"
    kind = meta.get("kind", "")
    category = meta.get("category", "")
    tok = meta.get("token_cost") or 0
    header = f"- **{name}**"
    if kind == "role" and category:
        header += f" (role/{category})"
    elif kind == "task":
        header += " (task skill)"
    if tok:
        header += f" [~{tok} tok]"

    desc = (meta.get("description") or "").strip()
    if len(desc) > 180:
        desc = desc[:177] + "…"

    trig = (meta.get("trigger_condition") or "").strip()
    if len(trig) > 160:
        trig = trig[:157] + "…"

    lines = [header]
    if desc:
        lines.append(f"  · {desc}")
    if trig:
        lines.append(f"  · Trigger: {trig}")
    return "\n".join(lines)


def build_skill_catalog(skills: list[dict] | None = None) -> str:
    """Phase 1 — format the metadata catalog that lazy-mode system prompts
    inject in place of a full role skill body.

    Deterministic ordering (by kind then name) so identical prompts hash
    identically across restarts — important for prompt-registry canary
    replay."""
    if skills is None:
        skills = list_all_skills_metadata()
    if not skills:
        return ""

    # Stable sort: role skills first (more load-bearing), then task skills.
    def _key(m: dict) -> tuple[int, str, str]:
        kind_rank = 0 if m.get("kind") == "role" else 1
        return (kind_rank, m.get("category", ""), m.get("name", ""))

    ordered = sorted(skills, key=_key)

    body_parts = [_SKILL_CATALOG_PREAMBLE, ""]
    total = len(body_parts[0]) + 1
    for meta in ordered:
        card = _format_skill_card(meta)
        if total + len(card) + 1 > _MAX_SKILL_CATALOG:
            body_parts.append("… [catalog truncated; ask operator to "
                              "split by agent_type]")
            break
        body_parts.append(card)
        total += len(card) + 1

    return "\n".join(body_parts)


def _skill_tokens(text: str) -> set[str]:
    """Lowercase word-bag of a skill's searchable fields — used for scoring.
    Keeps tokens ≥3 chars to drop stop-words cheaply without a full NLP pass."""
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())}


def match_skills_for_context(
    domain_context: str = "",
    user_prompt: str = "",
    *,
    skills: list[dict] | None = None,
    top_k: int = _MAX_PHASE2_SKILLS,
) -> list[dict]:
    """Phase 2 — keyword-score every skill against the CATC ``domain_context``
    + the user's current prompt and return the top ``top_k`` matches.

    Scoring: each skill's ``keywords`` / ``description`` / ``trigger_condition``
    / ``name`` is tokenised; overlap with the query token-bag gives the score.
    Keywords are weighted 2× (explicit signal) and name matches 3×.

    Returns [] when nothing scores above 0 — caller should treat this as
    "no skill is clearly relevant, proceed with base prompt"."""
    if skills is None:
        skills = list_all_skills_metadata()
    if not skills:
        return []
    query = f"{domain_context}\n{user_prompt}".strip()
    if not query:
        return []

    query_tokens = _skill_tokens(query)
    if not query_tokens:
        return []

    scored: list[tuple[int, dict]] = []
    for meta in skills:
        kw_list = meta.get("keywords") or []
        if isinstance(kw_list, str):
            kw_list = [kw_list]
        kw_tokens = _skill_tokens(" ".join(str(k) for k in kw_list))
        desc_tokens = _skill_tokens(
            f"{meta.get('description','')} {meta.get('trigger_condition','')}"
        )
        name_tokens = _skill_tokens(
            f"{meta.get('name','')} {meta.get('role_id','')} "
            f"{meta.get('category','')}"
        )

        score = (
            3 * len(query_tokens & name_tokens)
            + 2 * len(query_tokens & kw_tokens)
            + 1 * len(query_tokens & desc_tokens)
        )
        if score > 0:
            scored.append((score, meta))

    scored.sort(key=lambda t: (-t[0], t[1].get("name", "")))
    return [m for _, m in scored[:top_k]]


def build_skill_injection(
    domain_context: str = "",
    user_prompt: str = "",
    *,
    explicit_skills: list[str] | None = None,
    top_k: int = _MAX_PHASE2_SKILLS,
) -> str:
    """Phase 2 entry point — return a ready-to-inject system message chunk
    containing the full bodies of skills relevant to this ReAct turn.

    Two ways to select skills:
      * ``explicit_skills`` — agent emitted ``[LOAD_SKILL: <name>]`` markers.
        Caller extracts the names and passes them here directly.
      * ``domain_context`` + ``user_prompt`` — auto-match via keyword score.

    The returned string is ``""`` when no skill is selected or bodies fail
    to load. Otherwise, headers separate each skill with ``---`` and the
    whole chunk is truncated to ``_MAX_PHASE2_CHARS`` to bound injection
    size.
    """
    from backend.prompt_registry import get_skill_full, get_skill_metadata

    selected: list[dict] = []
    if explicit_skills:
        # Build a by-name index across role + task skills so the agent
        # can emit `[LOAD_SKILL: android-kotlin]` for role skills that
        # don't live under configs/skills/ (prompt_registry's path
        # resolver only looks there).
        catalog_index: dict[str, dict] | None = None
        for name in explicit_skills:
            meta = get_skill_metadata(name)
            if not meta:
                if catalog_index is None:
                    catalog_index = {
                        m.get("name", ""): m
                        for m in list_all_skills_metadata()
                        if m.get("name")
                    }
                meta = catalog_index.get(name)
            if meta:
                selected.append(meta)
    if not selected:
        selected = match_skills_for_context(
            domain_context=domain_context,
            user_prompt=user_prompt,
            top_k=top_k,
        )
    if not selected:
        return ""

    chunks: list[str] = []
    total = 0
    for meta in selected:
        body = get_skill_full(meta.get("path") or meta.get("name") or "")
        if not body:
            continue
        header = f"## Skill: {meta.get('name','?')}"
        block = f"{header}\n\n{body.strip()}"
        if total + len(block) > _MAX_PHASE2_CHARS:
            remaining = _MAX_PHASE2_CHARS - total
            if remaining < 200:
                break
            block = block[:remaining] + "\n... [skill body truncated]"
            chunks.append(block)
            total = _MAX_PHASE2_CHARS
            break
        chunks.append(block)
        total += len(block)

    return "\n\n---\n\n".join(chunks)


# Regex for parsing [LOAD_SKILL: <name>] markers an agent emits during ReAct.
_LOAD_SKILL_PATTERN = re.compile(r"\[LOAD_SKILL:\s*([^\]\n]+)\]")


def extract_load_skill_requests(agent_output: str) -> list[str]:
    """Parse an agent response for ``[LOAD_SKILL: <name>]`` markers.

    Returns unique skill names in the order they first appeared.
    Pairs with `build_skill_injection(explicit_skills=...)` so the ReAct
    loop can echo "here's the body you asked for" into the next turn.
    """
    if not agent_output:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _LOAD_SKILL_PATTERN.finditer(agent_output):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prompt Assembly
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Built-in fallback prompts (from original _SPECIALIST_PROMPTS)
_BUILTIN_PROMPTS = {
    "firmware": (
        "You are the Firmware Agent for embedded AI cameras. "
        "You handle UVC/RTSP drivers, Linux kernel modules, I2C/SPI sensor "
        "initialization, ISP pipeline configuration, Makefile cross-compilation, "
        "and flash operations.\n\n"
        "You have access to tools for reading/writing files, running git commands, "
        "and executing bash commands. Use them when the user's request requires "
        "inspecting or modifying the project. Always check existing files before "
        "writing new ones."
    ),
    "software": (
        "You are the Software Agent. You handle algorithm implementation, "
        "SDK/API development, C/C++ library integration, code refactoring, "
        "and build system maintenance.\n\n"
        "You have access to tools for reading/writing files, running git commands, "
        "and executing bash commands. Use them to inspect and modify code."
    ),
    "validator": (
        "You are the Validator Agent. You design and run test suites, "
        "coverage analysis, regression checks, benchmarks, and QA processes "
        "for embedded camera systems.\n\n"
        "You have access to tools for reading files, checking git status, "
        "and running test commands via bash."
    ),
    "reporter": (
        "You are the Reporter Agent. You generate compliance documentation "
        "(FCC/CE), test summaries, project reports, and exportable artifacts.\n\n"
        "You have access to tools for reading files and checking git history."
    ),
    "reviewer": (
        "You are the Code Reviewer Agent. You review Gerrit patchsets for "
        "embedded AI camera code quality. You check for memory safety issues, "
        "pointer errors, thread safety, and coding style.\n\n"
        "You have access to tools for reading diffs, posting inline comments, "
        "and submitting Code-Review scores (+1 or -1 only).\n"
        "You must NEVER give +2 or Submit — those are reserved for human maintainers."
    ),
}


def build_system_prompt(
    model_name: str = "",
    agent_type: str = "general",
    sub_type: str = "",
    handoff_context: str = "",
    task_skill_context: str = "",
    *,
    mode: str | None = None,
    domain_context: str = "",
) -> str:
    """Assemble the full system prompt from model rules + role skill + task skill + handoff.

    Two-phase skill loading (B15 #350):

      * ``mode="eager"`` (default for back-compat) — the role skill's full
        markdown body is inlined (legacy behaviour). ~50K chars / ~12.5K tok.
      * ``mode="lazy"`` — Phase 1: only the metadata catalog is injected,
        plus a small "pre-load hint" section naming the skills most
        relevant to ``domain_context`` so the agent can load them
        immediately without waiting for a ReAct round-trip.
      * ``mode=None`` — resolved from ``OMNISIGHT_SKILL_LOADING`` env var
        (default ``eager``), so operators can flip modes without patching
        callers.

    ``domain_context`` is the CATC ``domain_context`` string (see
    ``backend/catc.py``). It is only consulted in lazy mode to pick the
    Phase-2 pre-load hint. Falls back to built-in prompts if config files
    are missing.
    """
    resolved_mode = _resolve_skill_loading_mode(mode)
    sections: list[str] = []

    # 0. L1 Core Rules (CLAUDE.md — immutable, always first)
    core_rules = load_core_rules()
    if core_rules:
        sections.append(f"# Core Rules (Immutable)\n\n{core_rules}")

    # 1. Model rules
    model_rules = load_model_rules(model_name)
    if model_rules:
        sections.append(f"# Model Behavior Rules\n\n{model_rules}")

    # 2. Role skill (defines agent behavior).
    #    eager → inline full body (legacy).
    #    lazy  → inline a compact metadata catalog + Phase-2 pre-load hint.
    if resolved_mode == "lazy":
        catalog = build_skill_catalog()
        # Always carry a minimal identity header even in lazy mode — the
        # agent still needs to know who it is.
        identity = _BUILTIN_PROMPTS.get(
            agent_type,
            f"You are an AI agent of type '{agent_type}'.",
        )
        lazy_parts = [identity]
        if catalog:
            lazy_parts.append("\n## Available Skills (on-demand)\n\n" + catalog)
        # Phase-2 pre-load hint: if we already know the CATC domain_context,
        # surface the top matching skill names so the agent can emit
        # [LOAD_SKILL: …] on its very first turn rather than guessing.
        if domain_context:
            hints = match_skills_for_context(
                domain_context=domain_context,
                user_prompt="",
                top_k=_MAX_PHASE2_SKILLS,
            )
            if hints:
                hint_names = ", ".join(h.get("name", "?") for h in hints)
                lazy_parts.append(
                    "\n## Relevant skills for this task\n\n"
                    f"Based on the task's domain_context, consider loading: "
                    f"**{hint_names}**. Emit `[LOAD_SKILL: <name>]` to pull "
                    "the full body into the next turn."
                )
        sections.append(
            f"# Role: {sub_type or agent_type} (lazy-loaded skills)\n\n"
            + "\n".join(lazy_parts)
        )
    else:
        role_skill = load_role_skill(agent_type, sub_type)
        if role_skill:
            sections.append(f"# Role: {sub_type or agent_type}\n\n{role_skill}")
        elif agent_type in _BUILTIN_PROMPTS:
            sections.append(_BUILTIN_PROMPTS[agent_type])
        else:
            sections.append(
                f"You are an AI agent of type '{agent_type}'. "
                "You have access to file, git, and bash tools. "
                "Use them to complete the user's request."
            )

    # 3. Task skill (defines task execution steps — Anthropic SKILL.md format)
    if task_skill_context:
        if len(task_skill_context) > _MAX_TASK_SKILL:
            task_skill_context = task_skill_context[:_MAX_TASK_SKILL] + "\n... [task skill truncated]"
        sections.append(f"# Task Skill\n\n{task_skill_context}")

    # 4. Handoff context (truncated if too long)
    if handoff_context:
        if len(handoff_context) > _MAX_HANDOFF:
            handoff_context = handoff_context[:_MAX_HANDOFF] + "\n... [handoff truncated]"
        sections.append(f"# Previous Task Handoff\n\n{handoff_context}")

    return "\n\n---\n\n".join(sections)
