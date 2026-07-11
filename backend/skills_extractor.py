"""OP-2576 U4-I — workflow_run → quarantined learned item.

Redirect of the legacy Phase 62 skills extractor: the
``configs/skills/_pending/*.md`` file-writer is REPLACED by the
substrate producer (:mod:`backend.learned_item_producer`). Every
eligible workflow_run now lands as a quarantined substrate version row
(inert downstream — no eval/approval/publication until U4-J).

``propose_promotion(...)`` is RETIRED: the ``skill/promote`` card's
promote endpoint has been HTTP 410 since U4-0b, so a decision card here
would only accumulate dead cards. The function stays as a no-op with a
deprecation log so existing workflow hooks / tests keep their call
shape; U4-J will land ranked digest cards via
``propose_memory_promotion_digest``.

Scrub/redaction ``hits`` logic UNCHANGED — it runs BEFORE record
building (still needed as the security backstop for step outputs that
contain secrets).
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from backend.learned_item_producer import submit_quarantined_version
from backend.skills_scrubber import is_safe_to_promote, scrub

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Preserved for backwards compat: routers/skills.py imports PENDING_DIR
# to serve the frozen archive of previously-written _pending files. No
# NEW files land here — the extractor stops writing after this ticket.
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
    """Render the scrubber-input markdown body. Pure function — no IO.

    The rendered text no longer lands as a file; it feeds the scrubber
    (so step outputs containing secrets are still redacted before
    landing in the record) and the ``_draft_to_record`` parser.
    """
    kind = getattr(run, "kind", "unknown")
    metadata = getattr(run, "metadata", {}) or {}
    platform = metadata.get("platform") or metadata.get("target_platform") or ""

    error_steps = [s for s in steps if getattr(s, "error", None)]
    success_steps = [s for s in steps if not getattr(s, "error", None)]
    retry_count = len(error_steps)

    duration_s = 0.0
    if steps and getattr(steps[-1], "completed_at", None) and getattr(steps[0], "started_at", None):
        duration_s = max(0.0, float(steps[-1].completed_at) - float(steps[0].started_at))

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
#  Template → A2 typed record parser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SYMPTOMS_HEADING = "## Symptoms (what triggered this run)"
_FAILURES_HEADING = "## Failure modes encountered"
_RESOLUTION_HEADING = "## Resolution path (steps that succeeded)"
_NEXT_HEADING_PREFIX = "## "

_BULLET_RE = re.compile(r"^-\s+(.*)$")


def _section_bullets(markdown: str, heading: str) -> list[str]:
    lines = markdown.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        return []
    out: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith(_NEXT_HEADING_PREFIX):
            break
        stripped = line.strip()
        if not stripped:
            continue
        m = _BULLET_RE.match(stripped)
        if m:
            out.append(m.group(1).strip())
    return out


def _draft_to_record(
    markdown: str, run: Any, steps: list[Any]
) -> dict[str, Any] | None:
    """Parse the extractor's OWN fixed markdown template into an A2
    typed-record payload. Returns ``None`` when the template didn't
    match (caller skips + logs).

    Grammar bans backticks in ``evidence_references``, so this helper
    NEVER puts backticked bullets there.
    """
    symptoms = _section_bullets(markdown, _SYMPTOMS_HEADING)
    failures = _section_bullets(markdown, _FAILURES_HEADING)
    resolution = _section_bullets(markdown, _RESOLUTION_HEADING)

    kind = getattr(run, "kind", "workflow")
    retry_count = sum(1 for s in steps if getattr(s, "error", None))
    scope = (
        f"Applies to a workflow_run of kind {kind!r} that reached success "
        f"after {retry_count} retried step(s) across {len(steps)} total "
        f"steps."
    )

    if not resolution:
        return None

    return {
        "scope": scope,
        "preconditions": symptoms[:16],
        "procedure_steps": resolution[:16],
        "verification": "",
        "known_failures": failures[:16],
        "prohibited_actions": [],
        "evidence_references": [],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public extract entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SkillExtractionResult:
    """Returned from extract(). The ``path`` field is retained for
    backwards-compat but is always ``None`` since U4-I — no files are
    written; the ``version_id`` field carries the quarantined row id
    on success."""
    def __init__(self, *, written: bool, path: Path | None,
                 hits: Counter[str], skipped_reason: str = "",
                 version_id: str | None = None):
        self.written = written
        self.path = path
        self.hits = hits
        self.skipped_reason = skipped_reason
        self.version_id = version_id


async def _resolve_conn(conn: Any | None) -> tuple[Any, Any]:
    if conn is not None:
        async def _noop():  # pragma: no cover — trivial
            return None
        return conn, _noop
    from backend.db_pool import get_pool
    pool = get_pool()
    acquired = await pool.acquire()

    async def _release():
        await pool.release(acquired)

    return acquired, _release


def _tenant_id_for(run: Any) -> str:
    metadata = getattr(run, "metadata", {}) or {}
    if isinstance(metadata, dict) and metadata.get("tenant_id"):
        return str(metadata["tenant_id"])
    from backend.db_context import tenant_insert_value
    return tenant_insert_value()


def _evidence_from_run(run: Any) -> tuple:
    """Best-effort raw lookup data from run metadata. Live Gerrit/JIRA
    fetch wiring is U4-J's — the extractor passes whatever change ref
    its run context already holds through unchanged."""
    metadata = getattr(run, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return ()
    change_ref = metadata.get("change_ref") or metadata.get("gerrit_change_ref")
    if not change_ref:
        return ()
    return (
        {
            "change_ref": change_ref,
            "gerrit_change": metadata.get("gerrit_change") or {},
            "jira_labels": metadata.get("jira_labels") or [],
        },
    )


async def extract(
    run: Any,
    steps: list[Any],
    *,
    pending_dir: Path | None = None,
    conn: Any | None = None,
) -> SkillExtractionResult:
    """Extract → scrub → submit as a quarantined version row. NO file
    is written (the ``configs/skills/_pending`` writer is retired since
    U4-I); ``pending_dir`` is accepted but IGNORED for backwards-compat
    with test fixtures.

    Returns a :class:`SkillExtractionResult`; failures stay best-effort
    (logged, never crashes the workflow hook)."""
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
            "refusing to submit %s",
            sum(hits.values()), getattr(run, "id", "?"),
        )
        return SkillExtractionResult(
            written=False, path=None, hits=hits,
            skipped_reason=f"too many secret hits ({sum(hits.values())})",
        )

    payload = _draft_to_record(scrubbed, run, steps)
    if payload is None:
        logger.info(
            "skills_extractor: template did not parse — skipping submit "
            "for run=%s",
            getattr(run, "id", "?"),
        )
        return SkillExtractionResult(
            written=False, path=None, hits=hits,
            skipped_reason="template_unparseable",
        )

    kind = getattr(run, "kind", "workflow")
    slug = _slugify(kind)
    name = f"skill-{slug}"
    tid = _tenant_id_for(run)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    evidence = _evidence_from_run(run)

    owned_conn, release = await _resolve_conn(conn)
    try:
        try:
            result = await submit_quarantined_version(
                owned_conn,
                payload=payload,
                kind="skill",
                audience="tenant",
                tenant_id=tid,
                created_by="skills_extractor",
                name=name,
                description=f"Auto-extracted from workflow_run kind {kind!r}.",
                trigger_condition="",
                keywords=[],
                delivery_mode="retrieved",
                evidence=evidence,
                now=now,
            )
        except Exception as exc:
            logger.warning(
                "skills_extractor submit failed for run=%s: %s",
                getattr(run, "id", "?"),
                exc,
            )
            return SkillExtractionResult(
                written=False, path=None, hits=hits,
                skipped_reason=f"submit_failed: {exc}",
            )
    finally:
        await release()

    logger.info(
        "skill extracted → quarantined version %s (hits=%s)",
        result.version_id, dict(hits),
    )
    try:
        from backend import metrics as _m
        _m.skill_extracted_total.labels(status="written").inc()
    except Exception as exc:
        logger.debug("skill_extracted metric bump failed: %s", exc)

    return SkillExtractionResult(
        written=True, path=None, hits=hits, version_id=result.version_id,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Retired decision-engine hook (no-op deprecation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def propose_promotion(result: SkillExtractionResult, run: Any) -> None:
    """RETIRED since U4-I. The legacy ``skill/promote`` card's promote
    endpoint has been HTTP 410 since U4-0b, so filing new cards here
    would only accumulate dead work. Ranked digest cards land in U4-J
    via ``learned_item_approval.propose_memory_promotion_digest``.

    Kept as a no-op returning ``None`` so existing workflow hooks +
    tests calling it don't break their shape.
    """
    logger.debug(
        "propose_promotion is retired since U4-I; ranked digest cards "
        "land in U4-J. call for run=%s ignored.",
        getattr(run, "id", "?"),
    )
    return None


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
