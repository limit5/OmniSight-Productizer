"""Prompt Loader — assembles system prompts from model rules, role skills, and handoff context.

Directory layout::

    configs/models/*.md              Model-specific behavior rules
    configs/roles/{category}/*.skill.md   Role-specific skill definitions

Prompt assembly order:
    1. Model rules  (how to behave with this LLM)
    2. Role skill   (domain expertise for this agent role)
    3. Handoff      (context from previous agent, if any)

Falls back to built-in prompts when config files are missing.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIGS_ROOT = Path(__file__).resolve().parent.parent / "configs"
_MODELS_DIR = _CONFIGS_ROOT / "models"
_ROLES_DIR = _CONFIGS_ROOT / "roles"

# Maximum prompt section lengths (rough char counts) to avoid blowing context
_MAX_MODEL_RULES = 3000
_MAX_ROLE_SKILL = 6000
_MAX_HANDOFF = 4000


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
    except Exception:
        return {}


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
) -> str:
    """Assemble the full system prompt from model rules + role skill + handoff.

    Falls back to built-in prompts if config files are missing.
    """
    sections: list[str] = []

    # 1. Model rules
    model_rules = load_model_rules(model_name)
    if model_rules:
        sections.append(f"# Model Behavior Rules\n\n{model_rules}")

    # 2. Role skill
    role_skill = load_role_skill(agent_type, sub_type)
    if role_skill:
        sections.append(f"# Role: {sub_type or agent_type}\n\n{role_skill}")
    elif agent_type in _BUILTIN_PROMPTS:
        # Fallback to built-in prompt
        sections.append(_BUILTIN_PROMPTS[agent_type])
    else:
        sections.append(
            f"You are an AI agent of type '{agent_type}'. "
            "You have access to file, git, and bash tools. "
            "Use them to complete the user's request."
        )

    # 3. Handoff context (truncated if too long)
    if handoff_context:
        if len(handoff_context) > _MAX_HANDOFF:
            handoff_context = handoff_context[:_MAX_HANDOFF] + "\n... [handoff truncated]"
        sections.append(f"# Previous Task Handoff\n\n{handoff_context}")

    return "\n\n---\n\n".join(sections)
