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

ProjectClass = Literal[
    "embedded_product", "algo_sim", "optical_sim",
    "iso_standard", "test_tool", "factory_tool",
    "enterprise_web", "unknown",
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
    project_class: Field = field(default_factory=lambda: Field("unknown", 0.0))
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
            "project_type", "project_class", "runtime_model", "target_arch",
            "target_os", "framework", "persistence", "deploy_target",
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
            "project_class":      fv(self.project_class),
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


_PROJECT_CLASS_PATTERNS: dict[str, re.Pattern[str]] = {
    "embedded_product": re.compile(
        f"{_NW}(?:firmware|driver|bsp|uvc|ipcam|camera|dashcam|doorbell|router|gateway|earbuds|display|kiosk|scanner|printer|barcode|drone|watch|glasses|payment.?terminal|pos){_Nw}",
        re.IGNORECASE,
    ),
    "optical_sim": re.compile(
        f"{_NW}(?:zemax|code.?v|lighttools|optical|lens|ray.?tracing|mtf|spot.?diagram|wavefront|aberration){_Nw}",
        re.IGNORECASE,
    ),
    "algo_sim": re.compile(
        f"{_NW}(?:algorithm|simulation|matlab|pytorch|tensorflow|training|inference|model|dataset|neural|deep.?learning|machine.?learning|cv|computer.?vision){_Nw}",
        re.IGNORECASE,
    ),
    "iso_standard": re.compile(
        f"{_NW}(?:iso\\s*\\d|iec\\s*\\d|compliance|certification|conformance|standard|do-?178|asil|sil\\s*[1-4]){_Nw}",
        re.IGNORECASE,
    ),
    "test_tool": re.compile(
        f"{_NW}(?:test.?tool|test.?harness|test.?framework|test.?fixture|qa.?tool|regression.?tool|benchmark.?tool|validation.?tool){_Nw}",
        re.IGNORECASE,
    ),
    "factory_tool": re.compile(
        f"{_NW}(?:factory|production.?line|jig|mes|spc|yield|station|manufacturing|assembly.?line){_Nw}",
        re.IGNORECASE,
    ),
    "enterprise_web": re.compile(
        f"{_NW}(?:erp|crm|hrm|wms|warehouse|inventory|e-?commerce|portal|dashboard|admin.?panel|saas|multi.?tenant|rbac|sso|ldap){_Nw}",
        re.IGNORECASE,
    ),
}


def _infer_project_class(text: str, project_type: str, framework: str) -> tuple[str, float]:
    pc_v, pc_c = _regex_first(_PROJECT_CLASS_PATTERNS, text)
    if pc_v != "unknown":
        return pc_v, pc_c
    if project_type == "embedded_firmware":
        return "embedded_product", 0.5
    if project_type == "web_app" and framework in ("nextjs", "react", "vue", "svelte", "django", "flask", "fastapi"):
        return "enterprise_web", 0.4
    if project_type == "research":
        return "algo_sim", 0.4
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

    pc_v, pc_c = _infer_project_class(text, project_type, fw_v)

    return ParsedSpec(
        project_type=Field(project_type, pt_conf),
        project_class=Field(pc_v, pc_c),
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
  "project_class":  { "value": "embedded_product|algo_sim|optical_sim|iso_standard|test_tool|factory_tool|enterprise_web|unknown", "confidence": 0.0..1.0 },
  "runtime_model":  { "value": "ssg|ssr|isr|spa|cli|batch|unknown", "confidence": 0.0..1.0 },
  "target_arch":    { "value": "x86_64|arm64|arm32|riscv64|unknown", "confidence": 0.0..1.0 },
  "target_os":      { "value": "linux|darwin|windows|rtos|unknown", "confidence": 0.0..1.0 },
  "framework":      { "value": "<framework name or unknown>", "confidence": 0.0..1.0 },
  "persistence":    { "value": "sqlite|postgres|mysql|redis|flat_file|none|unknown", "confidence": 0.0..1.0 },
  "deploy_target":  { "value": "local|ssh|edge_device|cloud|unknown", "confidence": 0.0..1.0 },
  "hardware_required": { "value": "yes|no|unknown", "confidence": 0.0..1.0 }
}

project_class meanings:
- embedded_product: firmware/drivers for cameras, IoT, wearables, \
  POS terminals, scanners, printers, drones, etc.
- algo_sim: academic algorithm simulation, ML training, data science
- optical_sim: Zemax/CodeV/LightTools optical design simulation
- iso_standard: ISO/IEC/DO-178/ASIL compliance implementation
- test_tool: test harnesses, QA automation, validation tools
- factory_tool: production line jigs, MES integration, SPC
- enterprise_web: ERP/CRM/HRM/WMS/e-commerce/SaaS web applications

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
        project_class=pick("project_class"),
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
#  Conflict detector — YAML-driven (Phase 68-B)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from pathlib import Path as _Path

_CONFLICTS_PATH = _Path(__file__).resolve().parents[1] / "configs" / "spec_conflicts.yaml"
_CONFLICTS_CACHE: list[dict] | None = None


def _load_conflicts_yaml() -> list[dict]:
    """Parse + cache the rulebook. `reload_conflicts()` is the
    supported way to bust the cache; tests call it after swapping
    the file via monkeypatch."""
    global _CONFLICTS_CACHE
    if _CONFLICTS_CACHE is None:
        try:
            import yaml  # lazy — yaml isn't on the cold-start path
            data = yaml.safe_load(_CONFLICTS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("spec_conflicts.yaml load failed: %s", exc)
            data = {}
        rules = (data or {}).get("rules") or []
        _CONFLICTS_CACHE = rules if isinstance(rules, list) else []
    return _CONFLICTS_CACHE


def reload_conflicts() -> None:
    """Drop the YAML cache. Call after editing spec_conflicts.yaml
    in-flight or from a test that monkey-patches the path."""
    global _CONFLICTS_CACHE
    _CONFLICTS_CACHE = None


def _eval_when_clause(condition: Any, parsed: ParsedSpec, field_name: str) -> bool:
    """Evaluate a single `when:<field>: <condition>` clause.

    Supported condition shapes:
      * scalar string     → exact field.value equality
      * list[str]         → field.value in list
      * {re: "..."}       → regex on parsed.raw_text (field_name ignored)
      * {not: X}          → negate X (recursive)
    """
    raw = parsed.raw_text

    if isinstance(condition, dict):
        if "not" in condition:
            return not _eval_when_clause(condition["not"], parsed, field_name)
        if "re" in condition:
            try:
                return bool(re.search(str(condition["re"]), raw))
            except re.error as exc:
                logger.debug("spec_conflicts regex bad: %s", exc)
                return False
        return False

    if field_name == "raw_text":
        return raw == condition if isinstance(condition, str) else False

    try:
        field_val = getattr(parsed, field_name).value
    except AttributeError:
        return False
    if isinstance(condition, list):
        return field_val in condition
    if isinstance(condition, str):
        return field_val == condition
    if isinstance(condition, bool):
        # YAML `yes` parses as Python True; treat as string "yes"
        # which is the canonical ParsedSpec value for hardware_required.
        return field_val == ("yes" if condition else "no")
    return False


def _rule_matches(rule: dict, parsed: ParsedSpec) -> bool:
    """All `when:` clauses AND together. Missing / empty `when`
    means the rule would fire unconditionally — treat as disabled
    to avoid a YAML typo nuking every parse."""
    when = rule.get("when") or {}
    if not isinstance(when, dict) or not when:
        return False
    return all(
        _eval_when_clause(cond, parsed, field_name)
        for field_name, cond in when.items()
    )


def _rule_to_conflict(rule: dict) -> SpecConflict:
    """Translate a YAML rule into a SpecConflict instance. Silently
    coerces missing fields to defaults so a partial entry in YAML
    doesn't crash the parser."""
    options = []
    for opt in (rule.get("options") or []):
        if not isinstance(opt, dict):
            continue
        entry: dict[str, str] = {}
        for k in ("id", "label", "desc"):
            if k in opt:
                entry[k] = str(opt[k])
        # `apply` survives as a JSON-serialised string so SpecConflict
        # stays a frozen-tuple-compatible dataclass; apply_clarification
        # reads it back via the rule lookup, not from the option here.
        options.append(entry)
    return SpecConflict(
        id=str(rule.get("id") or "unnamed"),
        message=str(rule.get("message") or "").strip(),
        fields=tuple(rule.get("fields") or ()),
        options=tuple(options),
        severity=str(rule.get("severity") or "routine"),  # type: ignore[arg-type]
    )


def detect_conflicts(parsed: ParsedSpec) -> list[SpecConflict]:
    """Run every YAML rule whose `when:` matches against `parsed`.

    Rule eval errors are swallowed at debug — one bad YAML entry
    must not disable the whole detector."""
    out: list[SpecConflict] = []
    for rule in _load_conflicts_yaml():
        try:
            if _rule_matches(rule, parsed):
                out.append(_rule_to_conflict(rule))
        except Exception as exc:
            logger.debug("rule %r eval failed: %s", rule.get("id"), exc)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Iterative clarification (Phase 68-B)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Pattern: operator sees a conflict proposal, picks one of its
# options, caller invokes `apply_clarification()` which returns a
# new ParsedSpec with the option's `apply:` field overrides baked
# in at confidence 1.0 (operator's choice is authoritative). Caller
# then re-runs detect_conflicts on the updated spec; if more
# conflicts remain the cycle repeats, capped at MAX_CLARIFY_ROUNDS.
# Round bookkeeping is the caller's responsibility, mirroring how
# Phase 56-DAG-C's mutation loop manages its own retry budget.

MAX_CLARIFY_ROUNDS = 3


def apply_clarification(
    parsed: ParsedSpec,
    conflict_id: str,
    option_id: str,
) -> ParsedSpec:
    """Apply the chosen option's `apply:` overrides to `parsed` and
    return a new ParsedSpec with conflicts re-detected.

    Unknown conflict_id / option_id → returns the input unchanged
    (logged at warning) so an operator racing tabs can't corrupt
    state by clicking a stale clarification button."""
    rule = next(
        (r for r in _load_conflicts_yaml() if r.get("id") == conflict_id),
        None,
    )
    if rule is None:
        logger.warning("apply_clarification: unknown conflict %r", conflict_id)
        return parsed
    opt = next(
        (o for o in (rule.get("options") or [])
         if isinstance(o, dict) and o.get("id") == option_id),
        None,
    )
    if opt is None:
        logger.warning(
            "apply_clarification: unknown option %r for %r",
            option_id, conflict_id,
        )
        return parsed

    applies: dict[str, str] = opt.get("apply") or {}
    if not isinstance(applies, dict):
        return parsed

    updates: dict[str, Field] = {}
    for name in (
        "project_type", "project_class", "runtime_model", "target_arch",
        "target_os", "framework", "persistence", "deploy_target",
        "hardware_required",
    ):
        if name in applies:
            updates[name] = Field(str(applies[name]), 1.0)

    new = ParsedSpec(
        project_type=updates.get("project_type", parsed.project_type),
        project_class=updates.get("project_class", parsed.project_class),
        runtime_model=updates.get("runtime_model", parsed.runtime_model),
        target_arch=updates.get("target_arch", parsed.target_arch),
        target_os=updates.get("target_os", parsed.target_os),
        framework=updates.get("framework", parsed.framework),
        persistence=updates.get("persistence", parsed.persistence),
        deploy_target=updates.get("deploy_target", parsed.deploy_target),
        hardware_required=updates.get("hardware_required", parsed.hardware_required),
        raw_text=parsed.raw_text,
    )
    new.conflicts = detect_conflicts(new)
    return new


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
