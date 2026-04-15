"""Phase 68-A — Intent Parser + ParsedSpec.

Sits between the operator's free-form command and the DAG drafter.
The problem we're solving: Phase 47C ambiguity detection only catches
a hardcoded list of known templates; free-form conflicts like "static
site + runtime DB query" slip straight into the planner, which then
picks a default the operator never saw.

This module produces a structured intermediate: each spec field
carries its own (value, confidence) pair plus an explicit
`conflicts[]` list. Low-confidence fields feed Decision-Engine
clarification proposals before any DAG is drafted. Full declarative
conflict rulebook lands in Phase 68-B (`configs/spec_conflicts.yaml`);
this phase ships ParsedSpec, the parser entry point, a minimal
handwritten conflict detector as a smoke, and the LLM-backed parse
path with a deterministic heuristic fallback for unit tests / LLM
outages.

The parser NEVER calls DAG drafting itself; callers should:

    parsed = await parse_intent(user_command)
    if parsed.needs_clarification():
        await emit_decision_engine_proposal(parsed)
        return   # wait for operator
    # ... proceed to DAG drafter

This module is pure data + pure IO. No global state.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data classes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Operator-visible enums. Keep tight so UI dropdowns in Phase 68-C
# match. `unknown` carries confidence 0 by convention.
RuntimeModel = Literal["ssg", "ssr", "isr", "spa", "cli", "batch", "unknown"]
Persistence = Literal["sqlite", "postgres", "mysql", "redis", "flat_file", "none", "unknown"]
DeployTarget = Literal["local", "ssh", "edge_device", "cloud", "unknown"]
ProjectType = Literal[
    "embedded_firmware", "web_app", "data_pipeline",
    "research", "cli_tool", "unknown",
]


@dataclass(frozen=True)
class Field:
    """(value, confidence) tuple. Confidence is 0.0 — 1.0; 0 means
    the parser didn't extract anything, 1.0 means structured UI path
    (Phase 68-C form) filled it in directly."""
    value: str
    confidence: float = 0.0

    def known(self) -> bool:
        """True iff the parser returned a value other than the
        per-enum sentinel `"unknown"`."""
        return self.value != "unknown" and self.value != ""


@dataclass(frozen=True)
class SpecConflict:
    """Something in the spec is mutually inconsistent. The `options`
    list becomes the choices in a Decision-Engine proposal; the
    operator picks one, the resolver records their choice into L3
    (Phase 68-D), and we re-parse."""
    id: str
    message: str
    fields: tuple[str, ...]          # field names involved
    options: tuple[dict[str, str], ...]
    severity: Literal["info", "routine", "risky", "destructive"] = "routine"


@dataclass
class ParsedSpec:
    """Structured extraction of the operator's command.

    Every field is a `Field(value, confidence)` so the UI can colour
    low-confidence entries and the ambiguity detector can ask about
    them explicitly. `conflicts[]` is populated by the detector pass
    that runs after parse — an empty list means the parser believes
    the spec is internally consistent (doesn't mean the DAG will
    validate; just that there's nothing obvious to clarify first).
    """
    project_type:  Field = field(default_factory=lambda: Field("unknown", 0.0))
    runtime_model: Field = field(default_factory=lambda: Field("unknown", 0.0))
    target_arch:   Field = field(default_factory=lambda: Field("unknown", 0.0))
    target_os:     Field = field(default_factory=lambda: Field("linux", 0.3))
    framework:     Field = field(default_factory=lambda: Field("unknown", 0.0))
    persistence:   Field = field(default_factory=lambda: Field("unknown", 0.0))
    deploy_target: Field = field(default_factory=lambda: Field("unknown", 0.0))
    hardware_required: Field = field(default_factory=lambda: Field("no", 0.3))
    # Free-text note captured verbatim from the prompt — useful for
    # downstream agents that want the operator's phrasing (e.g. the
    # orchestrator's system prompt).
    raw_text: str = ""
    conflicts: list[SpecConflict] = field(default_factory=list)

    def low_confidence(self, threshold: float = 0.7) -> list[str]:
        """Field names whose confidence sits below `threshold`. Phase
        68-C surfaces these as a clarification form."""
        out: list[str] = []
        for name in (
            "project_type", "runtime_model", "target_arch", "target_os",
            "framework", "persistence", "deploy_target",
        ):
            v: Field = getattr(self, name)
            if v.confidence < threshold:
                out.append(name)
        return out

    def needs_clarification(self, threshold: float = 0.7) -> bool:
        """True iff any conflict fires OR any structural field is
        below the confidence floor. Callers use this to decide
        whether to open a Decision-Engine clarification proposal
        before drafting a DAG."""
        return bool(self.conflicts) or bool(self.low_confidence(threshold))

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for audit log + Decision-Engine detail."""
        def fv(f: Field) -> dict[str, Any]:
            return {"value": f.value, "confidence": round(f.confidence, 2)}
        return {
            "project_type":       fv(self.project_type),
            "runtime_model":      fv(self.runtime_model),
            "target_arch":        fv(self.target_arch),
            "target_os":          fv(self.target_os),
            "framework":          fv(self.framework),
            "persistence":        fv(self.persistence),
            "deploy_target":      fv(self.deploy_target),
            "hardware_required":  fv(self.hardware_required),
            "raw_text":           self.raw_text,
            "conflicts":          [
                {
                    "id": c.id,
                    "message": c.message,
                    "fields": list(c.fields),
                    "options": list(c.options),
                    "severity": c.severity,
                }
                for c in self.conflicts
            ],
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Heuristic fallback (LLM-free)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Regex-and-keyword extraction used when `ask_fn` is missing or the
# LLM call fails. Deliberately narrow; its job is to never silently
# succeed on ambiguous input — low confidence is the right answer
# more often than a wrong confident value.

# Patterns are CJK-safe: Python's `\b` doesn't fire at a CJK↔Latin
# boundary (Chinese characters aren't word characters), so prompts
# like "使用Next.js開發" would miss the framework. Patterns here
# bound with `(?<![a-z0-9])` / `(?![a-z0-9])` — ASCII-word-only
# lookarounds that work regardless of surrounding Unicode.
_NW = r"(?<![a-z0-9])"   # not preceded by a word char
_Nw = r"(?![a-z0-9])"    # not followed by a word char

_FRAMEWORK_PATTERNS: dict[str, re.Pattern[str]] = {
    "nextjs":    re.compile(f"{_NW}next\\.?js{_Nw}", re.IGNORECASE),
    "react":     re.compile(f"{_NW}react{_Nw}", re.IGNORECASE),
    "vue":       re.compile(f"{_NW}vue(?:\\.js)?{_Nw}", re.IGNORECASE),
    "svelte":    re.compile(f"{_NW}svelte(?:kit)?{_Nw}", re.IGNORECASE),
    "django":    re.compile(f"{_NW}django{_Nw}", re.IGNORECASE),
    "flask":     re.compile(f"{_NW}flask{_Nw}", re.IGNORECASE),
    "fastapi":   re.compile(f"{_NW}fastapi{_Nw}", re.IGNORECASE),
    "rust":      re.compile(f"{_NW}(?:rust|cargo|axum){_Nw}", re.IGNORECASE),
    "embedded":  re.compile(f"{_NW}(?:firmware|driver|mcu|rtos|zephyr|freertos){_Nw}", re.IGNORECASE),
}

_ARCH_PATTERNS: dict[str, re.Pattern[str]] = {
    "x86_64":   re.compile(f"{_NW}(?:x86[_-]?64|amd64|intel){_Nw}", re.IGNORECASE),
    "arm64":    re.compile(f"{_NW}(?:aarch64|arm64|m1|m2|apple silicon|raspberry pi ?[45]){_Nw}", re.IGNORECASE),
    "arm32":    re.compile(f"{_NW}(?:armv7|armhf|raspberry pi (?:2|3|zero)){_Nw}", re.IGNORECASE),
    "riscv64":  re.compile(f"{_NW}risc[- ]?v(?:64)?{_Nw}", re.IGNORECASE),
}

_PERSISTENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "sqlite":    re.compile(f"{_NW}sqlite{_Nw}", re.IGNORECASE),
    "postgres":  re.compile(f"{_NW}(?:postgres(?:ql)?|pg){_Nw}", re.IGNORECASE),
    "mysql":     re.compile(f"{_NW}(?:mysql|mariadb){_Nw}", re.IGNORECASE),
    "redis":     re.compile(f"{_NW}redis{_Nw}", re.IGNORECASE),
    "flat_file": re.compile(f"{_NW}(?:json|yaml|csv|markdown|flat[-_ ]?file){_Nw}", re.IGNORECASE),
}

# Runtime patterns include CJK variants (「靜態網頁」, 「靜態展示」)
# so Chinese specs don't silently miss the SSG signal.
_RUNTIME_PATTERNS: dict[str, re.Pattern[str]] = {
    # Match "static site", "static next.js site", "static page", etc.
    # Allow up to 20 chars between `static` and the head-noun so
    # "static Next.js site" triggers without swallowing paragraphs.
    "ssg":   re.compile(f"{_NW}(?:static(?:\\s+\\S{{1,20}}){{0,2}}\\s+(?:site|page|export|html)|ssg|next\\s*export){_Nw}|靜態(?:網頁|頁面|展示|站)|静态(?:网页|页面|展示|站)", re.IGNORECASE),
    "ssr":   re.compile(f"{_NW}(?:ssr|server[- ]?side[- ]?render(?:ing)?){_Nw}", re.IGNORECASE),
    "isr":   re.compile(f"{_NW}(?:isr|incremental[- ]?static[- ]?regen){_Nw}", re.IGNORECASE),
    "spa":   re.compile(f"{_NW}(?:spa|single[- ]?page\\s+app){_Nw}", re.IGNORECASE),
    "batch": re.compile(f"{_NW}batch\\s+(?:job|processing|pipeline){_Nw}", re.IGNORECASE),
    "cli":   re.compile(f"{_NW}(?:cli|command[- ]?line)\\s+(?:tool|utility){_Nw}", re.IGNORECASE),
}

# "runtime DB" / "本機資料庫" / "query at request time" language —
# used by the minimal conflict detector below. Matches either side
# of the static/runtime tension: explicit "at request time" /
# "runtime" near any persistence word, OR a Chinese
# 「從/本機...資料庫」phrase, OR "reads from <persistence> at request
# time" English (the SQLite-without-explicit-'database' case).
_RUNTIME_DB_HINT = re.compile(
    r"(?:runtime|request\s*time|on each request|per request)"
    r".{0,40}(?:db|database|sqlite|postgres|mysql|redis|read)"
    r"|(?:read|fetch|query|load)s?\s+from\s+.{0,40}"
    r"(?:database|db|sqlite|postgres|mysql|redis)"
    r"|(?:本機|本地|本地端|local).{0,10}(?:資料庫|database|db)"
    r"|(?:從|from).{0,10}(?:資料庫|database|db|sqlite).{0,10}(?:拉|fetch|query)",
    re.IGNORECASE,
)


def _regex_first(patterns: dict[str, re.Pattern[str]], text: str) -> tuple[str, float]:
    """Return (first match, 0.6) or ("unknown", 0.0).

    Confidence 0.6: regex matches are better than nothing, still
    below the 0.7 clarification threshold so the operator gets asked
    to confirm. LLM-backed parses can bump past 0.7 themselves.
    """
    for name, pat in patterns.items():
        if pat.search(text):
            return name, 0.6
    return "unknown", 0.0


def _heuristic_parse(text: str) -> ParsedSpec:
    """Fallback parser when no LLM is available."""
    fw_v, fw_c = _regex_first(_FRAMEWORK_PATTERNS, text)
    arch_v, arch_c = _regex_first(_ARCH_PATTERNS, text)
    pers_v, pers_c = _regex_first(_PERSISTENCE_PATTERNS, text)
    rt_v, rt_c = _regex_first(_RUNTIME_PATTERNS, text)

    # Inferred fields from strong hints.
    project_type = "unknown"
    pt_conf = 0.0
    if fw_v in ("embedded",):
        project_type, pt_conf = "embedded_firmware", 0.7
    elif fw_v in ("nextjs", "react", "vue", "svelte"):
        project_type, pt_conf = "web_app", 0.7
    elif fw_v in ("django", "flask", "fastapi", "rust"):
        project_type, pt_conf = "web_app", 0.5

    # Hardware required only when project_type=embedded_firmware or
    # explicit hardware nouns appear.
    hw = "yes" if (
        project_type == "embedded_firmware"
        or re.search(r"\b(?:sensor|GPIO|i2c|spi|uart|camera|CSI|MIPI)\b", text, re.IGNORECASE)
    ) else "no"
    hw_c = 0.7 if project_type == "embedded_firmware" else 0.5

    return ParsedSpec(
        project_type=Field(project_type, pt_conf),
        runtime_model=Field(rt_v, rt_c),
        target_arch=Field(arch_v, arch_c),
        target_os=Field("linux", 0.3),
        framework=Field(fw_v, fw_c),
        persistence=Field(pers_v, pers_c),
        deploy_target=Field("unknown", 0.0),
        hardware_required=Field(hw, hw_c),
        raw_text=text,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM-backed parse (structured JSON output)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_LLM_SYSTEM_PROMPT = """You extract a structured project-intent spec \
from a user's free-form command. Return ONLY a JSON object (no \
markdown, no prose) matching this schema exactly:

{
  "project_type":   { "value": "embedded_firmware|web_app|data_pipeline|research|cli_tool|unknown", "confidence": 0.0..1.0 },
  "runtime_model":  { "value": "ssg|ssr|isr|spa|cli|batch|unknown", "confidence": 0.0..1.0 },
  "target_arch":    { "value": "x86_64|arm64|arm32|riscv64|unknown", "confidence": 0.0..1.0 },
  "target_os":      { "value": "linux|darwin|windows|rtos|unknown", "confidence": 0.0..1.0 },
  "framework":      { "value": "<framework name or unknown>", "confidence": 0.0..1.0 },
  "persistence":    { "value": "sqlite|postgres|mysql|redis|flat_file|none|unknown", "confidence": 0.0..1.0 },
  "deploy_target":  { "value": "local|ssh|edge_device|cloud|unknown", "confidence": 0.0..1.0 },
  "hardware_required": { "value": "yes|no|unknown", "confidence": 0.0..1.0 }
}

Rules:
- Use "unknown" with confidence 0.0 for any field the user didn't \
  mention, rather than guessing.
- Confidence ≤ 0.65 when you're interpreting implicit language, \
  ≥ 0.85 when the user stated the value verbatim.
- NEVER emit a field not listed above. No free text."""


async def _llm_parse(
    text: str,
    ask_fn: Callable[[str, str], Awaitable[tuple[str, int]]],
    model: str,
) -> Optional[ParsedSpec]:
    """Call ask_fn with the structured-JSON prompt, parse the response
    into ParsedSpec, return None on any failure so the caller can
    degrade to the heuristic parser."""
    try:
        combined = f"{_LLM_SYSTEM_PROMPT}\n\n---\n\nUSER COMMAND:\n{text}"
        raw, _tokens = await ask_fn(model, combined)
        if not raw:
            return None
        # Tolerate accidental markdown fences.
        s = raw.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        data = json.loads(s)
        if not isinstance(data, dict):
            return None
    except Exception as exc:
        logger.debug("_llm_parse failed: %s", exc)
        return None

    def pick(name: str, default: str = "unknown", default_conf: float = 0.0) -> Field:
        entry = data.get(name)
        if not isinstance(entry, dict):
            return Field(default, default_conf)
        v = str(entry.get("value") or default).strip() or default
        try:
            c = float(entry.get("confidence") or 0.0)
            c = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            c = default_conf
        return Field(v, c)

    return ParsedSpec(
        project_type=pick("project_type"),
        runtime_model=pick("runtime_model"),
        target_arch=pick("target_arch"),
        target_os=pick("target_os", default="linux", default_conf=0.3),
        framework=pick("framework"),
        persistence=pick("persistence"),
        deploy_target=pick("deploy_target"),
        hardware_required=pick("hardware_required", default="no", default_conf=0.3),
        raw_text=text,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Conflict detector (smoke — full library is Phase 68-B)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def detect_conflicts(parsed: ParsedSpec) -> list[SpecConflict]:
    """Smoke rules — Phase 68-B will replace this with a YAML-driven
    table. Kept minimal here so 68-A ships with at least one real
    conflict demo.
    """
    out: list[SpecConflict] = []

    # SSG + runtime DB query — the motivating example from the
    # operator's 2026-04-15 design review.
    if (
        parsed.runtime_model.value == "ssg"
        and _RUNTIME_DB_HINT.search(parsed.raw_text)
    ):
        out.append(SpecConflict(
            id="static_with_runtime_db",
            message=(
                "Spec mentions a static/SSG site but also talks about "
                "reading from a local database at runtime. Pick one:"
            ),
            fields=("runtime_model", "persistence"),
            options=(
                {"id": "ssg_build_time",
                 "label": "SSG — read DB at build time",
                 "desc": "Next.js SSG, `next build` queries the DB, deploy only `out/` + static server."},
                {"id": "ssr_runtime",
                 "label": "SSR — query at request time",
                 "desc": "Node runtime on the target, DB query per request."},
                {"id": "isr_hybrid",
                 "label": "ISR — hybrid revalidate",
                 "desc": "Best of both; needs Node runtime on target."},
            ),
            severity="routine",
        ))

    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AskFn = Callable[[str, str], Awaitable[tuple[str, int]]]


async def parse_intent(
    text: str,
    *,
    ask_fn: Optional[AskFn] = None,
    model: str = "",
) -> ParsedSpec:
    """Extract a ParsedSpec from a free-form command. When `ask_fn`
    is provided we try an LLM parse first (structured JSON); on any
    failure we fall back to `_heuristic_parse` so the caller always
    gets a usable object. Conflict detection runs unconditionally on
    whatever ParsedSpec came out.

    `ask_fn` signature matches `iq_runner.live_ask_fn`:
        async (model: str, prompt: str) -> (response_text, tokens_used)

    Empty / whitespace-only input short-circuits to an all-unknown
    spec — no point invoking an LLM on "" and getting a hallucination.
    """
    text = (text or "").strip()
    parsed: Optional[ParsedSpec] = None
    if text and ask_fn is not None and model:
        parsed = await _llm_parse(text, ask_fn, model)
    if parsed is None:
        parsed = _heuristic_parse(text)
    parsed.conflicts = detect_conflicts(parsed)
    return parsed
