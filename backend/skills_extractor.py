"""Phase 62 — Knowledge Generation: workflow_run → skill markdown.

The flow:
  1. A workflow_run reaches `status=completed`.
  2. `should_extract(run, steps)` decides if it's worth distilling
     (≥ 5 steps OR ≥ 3 retries — the spec's threshold for "this was
     hard enough that the lesson is worth keeping").
  3. `extract(run, steps)` builds a deterministic markdown file with
     YAML frontmatter, scrubs it, and writes to
     `configs/skills/_pending/skill-<slug>.md`.
  4. `propose_promotion(...)` files a Decision Engine `skill/promote`
     proposal so an operator can review + move into the live
     `configs/skills/<slug>/SKILL.md`.

We deliberately use a template — not an LLM call — for v1. Determinism
makes the pipeline testable and the output reviewable; an LLM-rewriter
can be layered on later as Phase 62.5 once the gating + audit story is
proven.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from backend.skills_scrubber import is_safe_to_promote, scrub

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = _PROJECT_ROOT / "configs" / "skills" / "_pending"

# Spec thresholds — keep in sync with HANDOFF Phase 62 doc.
MIN_STEPS = 5
MIN_RETRIES = 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Trigger gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def should_extract(run: Any, steps: list[Any]) -> bool:
    """True when this run is hard enough that the lesson is worth
    keeping. Either a long step chain OR significant retry pressure."""
    if getattr(run, "status", "") != "completed":
        return False
    if len(steps) >= MIN_STEPS:
        return True
    retries = sum(1 for s in steps if getattr(s, "error", None))
    return retries >= MIN_RETRIES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slug + extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:max_len] or "skill"


def _build_markdown(run: Any, steps: list[Any]) -> str:
    """Render the canonical skill markdown body. Pure function — no IO."""
    kind = getattr(run, "kind", "unknown")
    metadata = getattr(run, "metadata", {}) or {}
    platform = metadata.get("platform") or metadata.get("target_platform") or ""

    error_steps = [s for s in steps if getattr(s, "error", None)]
    success_steps = [s for s in steps if not getattr(s, "error", None)]
    retry_count = len(error_steps)
    final_step = success_steps[-1] if success_steps else (steps[-1] if steps else None)

    duration_s = 0.0
    if steps and getattr(steps[-1], "completed_at", None) and getattr(steps[0], "started_at", None):
        duration_s = max(0.0, float(steps[-1].completed_at) - float(steps[0].started_at))

    # Frontmatter (operator can edit before promotion).
    fm_lines = [
        "---",
        f"name: skill-{_slugify(kind)}-{int(time.time())}",
        f'description: "Auto-extracted from successful workflow_run \'{kind}\' '
        f'after {retry_count} retries across {len(steps)} steps."',
        f"trigger_kinds: [{kind!r}]",
        f"platform: {platform!r}" if platform else "platform: ''",
        f"retry_count: {retry_count}",
        f"step_count: {len(steps)}",
        f"duration_s: {round(duration_s, 1)}",
        "confidence: 0.5  # operator should review and adjust",
        "source_run_id: " + (getattr(run, "id", "") or ""),
        "extracted_at: " + str(int(time.time())),
        "---",
        "",
    ]

    body = [
        f"# Skill: {kind}",
        "",
        "## Symptoms (what triggered this run)",
        "",
        f"- workflow kind: `{kind}`",
        f"- {retry_count} retried step(s) before success",
        f"- {len(steps)} total steps, {round(duration_s)}s elapsed",
        "",
    ]

    if error_steps:
        body.append("## Failure modes encountered")
        body.append("")
        for s in error_steps[:5]:  # cap at 5 to keep output readable
            err = (getattr(s, "error", "") or "").strip().splitlines()[:2]
            body.append(f"- `{s.idempotency_key}`: {' '.join(err)[:160]}")
        body.append("")

    body.append("## Resolution path (steps that succeeded)")
    body.append("")
    for s in success_steps[:10]:
        out = getattr(s, "output", None)
        summary = ""
        if isinstance(out, dict):
            summary = str(out.get("summary") or out.get("status") or "")[:120]
        body.append(f"- `{s.idempotency_key}`{(' — ' + summary) if summary else ''}")
    body.append("")

    body.append("## Operator notes")
    body.append("")
    body.append("Review the trigger conditions above. If this skill should")
    body.append("apply to additional `kind` patterns, edit `trigger_kinds:`")
    body.append("in the frontmatter before approving the promotion.")
    body.append("")

    return "\n".join(fm_lines) + "\n".join(body) + "\n"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public extract entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SkillExtractionResult:
    """Returned from extract() so the caller (workflow finish hook) can
    decide whether to file a Decision Engine proposal."""
    def __init__(self, *, written: bool, path: Path | None,
                 hits: Counter[str], skipped_reason: str = ""):
        self.written = written
        self.path = path
        self.hits = hits
        self.skipped_reason = skipped_reason


def extract(run: Any, steps: list[Any], *,
            pending_dir: Path | None = None) -> SkillExtractionResult:
    """Extract → scrub → write to _pending/. Returns a result object;
    does NOT propose to the Decision Engine (the workflow hook does)."""
    if not should_extract(run, steps):
        return SkillExtractionResult(
            written=False, path=None, hits=Counter(),
            skipped_reason=f"below threshold (steps={len(steps)} < {MIN_STEPS} "
                           f"and retries < {MIN_RETRIES})",
        )

    raw = _build_markdown(run, steps)
    scrubbed, hits = scrub(raw)

    if not is_safe_to_promote(hits):
        logger.warning(
            "skill extract: %d redactions exceed safety threshold; "
            "refusing to write %s",
            sum(hits.values()), getattr(run, "id", "?"),
        )
        return SkillExtractionResult(
            written=False, path=None, hits=hits,
            skipped_reason=f"too many secret hits ({sum(hits.values())})",
        )

    target_dir = pending_dir or PENDING_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(getattr(run, "kind", "skill"))
    filename = f"skill-{slug}-{getattr(run, 'id', 'x')[:8]}.md"
    path = target_dir / filename
    path.write_text(scrubbed, encoding="utf-8")
    logger.info("skill extracted: %s (hits=%s)", path, dict(hits))

    try:
        from backend import metrics as _m
        _m.skill_extracted_total.labels(status="written").inc()
    except Exception:
        pass

    return SkillExtractionResult(written=True, path=path, hits=hits)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision Engine integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def propose_promotion(result: SkillExtractionResult, run: Any) -> str | None:
    """Open a Decision Engine `skill/promote` proposal. Operator
    `approve` → moves _pending/<file>.md into live skills dir.
    Returns the decision id, or None if not applicable."""
    if not result.written or not result.path:
        return None
    try:
        from backend import decision_engine as de
    except Exception as exc:
        logger.warning("decision_engine import failed: %s", exc)
        return None

    options = [
        {"id": "promote", "label": "Promote to configs/skills/",
         "description": f"Move {result.path.name} from _pending into the live skills tree."},
        {"id": "discard", "label": "Discard",
         "description": "Delete the _pending file; lesson not retained."},
    ]
    dec = de.propose(
        kind="skill/promote",
        title=f"Skill candidate: {result.path.name}",
        detail=f"Auto-extracted from workflow_run {getattr(run, 'id', '?')}. "
               f"Scrub hits: {dict(result.hits)}.",
        options=options,
        default_option_id="discard",  # safer default
        severity=de.DecisionSeverity.routine,
        timeout_s=86400.0,  # 24h to review
        source={
            "extractor": "skills_extractor",
            "path": str(result.path),
            "run_id": getattr(run, "id", ""),
            "scrub_hits": dict(result.hits),
        },
    )
    return dec.id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Opt-in gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_enabled() -> bool:
    """Phase 62 lives behind the OMNISIGHT_SELF_IMPROVE_LEVEL env knob.
    Active when the level includes L1 (knowledge generation):
        off | l1 | l1+l3 | all   → enabled iff "l1" appears."""
    level = (os.environ.get("OMNISIGHT_SELF_IMPROVE_LEVEL") or "off").strip().lower()
    if level in {"off", ""}:
        return False
    return "l1" in level or level == "all"
