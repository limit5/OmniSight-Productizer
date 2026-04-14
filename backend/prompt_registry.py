"""Phase 63-C — Prompt Registry + Canary.

Lifts agent system prompts out of source code into versioned rows that
can be rolled back without a deploy. Each `path` (e.g.
``backend/agents/prompts/firmware.md``) has at most one ``active``
row, optionally one ``canary`` row, plus N ``archive`` rows.

Routing model:
  * `pick_for_request(path, agent_id)` — deterministic 5% canary
    routing via stable hash of agent_id. Returns
    ``(version, body, role)``. 95% of agents always see the active
    version; 5% always see the canary while it's open. No request-time
    randomness — operators investigating a regression can replay.

Outcome feedback:
  * `record_outcome(path, version, success: bool)` — bumps
    success_count/failure_count on the row. Phase 63-A's IIS already
    knows whether a response was "good" (code_pass / compliance);
    this is its persistent shadow.

Auto-rollback policy:
  * `evaluate_canary(path, *, min_samples=20, regression_pp=5)`:
    if the canary has ≥ min_samples and its pass rate is more than
    `regression_pp` percentage points BELOW the active baseline,
    automatically retire the canary (role='archive') and write
    rollback_reason. Caller decides cadence (Phase 63-A loop or
    nightly cron — we leave that to consumers).

Path whitelist:
  * Only `backend/agents/prompts/**.md` is acceptable. Refuses
    `CLAUDE.md` and anything outside the prompt tree even if the
    caller passes an absolute path (resolved + containment check).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_ROOT = _PROJECT_ROOT / "backend" / "agents" / "prompts"
CANARY_RATE_PCT = 5  # design-locked: 5%


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Path whitelist
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PathRejected(ValueError):
    """Caller tried to register/touch a path outside PROMPTS_ROOT."""


def _normalise_path(path: str) -> str:
    """Return the canonical relative-to-project path string, raising
    `PathRejected` if it escapes PROMPTS_ROOT or names CLAUDE.md."""
    p = Path(path)
    if not p.is_absolute():
        p = (_PROJECT_ROOT / p).resolve()
    else:
        p = p.resolve()
    root = PROMPTS_ROOT.resolve()
    if root not in p.parents and p != root:
        raise PathRejected(f"path {path!r} outside {root}")
    if p.name == "CLAUDE.md" or "CLAUDE.md" in str(p):
        raise PathRejected("CLAUDE.md is L1-immutable; refusing to manage it")
    if p.suffix != ".md":
        raise PathRejected(f"prompt path must end .md, got {p.suffix!r}")
    return str(p.relative_to(_PROJECT_ROOT))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data classes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PromptVersion:
    id: int
    path: str
    version: int
    role: str  # active | canary | archive
    body: str
    body_sha256: str
    success_count: int
    failure_count: int
    created_at: float
    promoted_at: Optional[float]
    rolled_back_at: Optional[float]
    rollback_reason: Optional[str]

    @property
    def total_samples(self) -> int:
        return self.success_count + self.failure_count

    @property
    def pass_rate(self) -> Optional[float]:
        n = self.total_samples
        return None if n == 0 else self.success_count / n


def _row_to_version(row) -> PromptVersion:
    return PromptVersion(
        id=row["id"], path=row["path"], version=row["version"],
        role=row["role"], body=row["body"], body_sha256=row["body_sha256"],
        success_count=row["success_count"], failure_count=row["failure_count"],
        created_at=row["created_at"],
        promoted_at=row["promoted_at"], rolled_back_at=row["rolled_back_at"],
        rollback_reason=row["rollback_reason"],
    )


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API — register / lookup / outcome
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def register_active(path: str, body: str) -> PromptVersion:
    """Register or replace the ``active`` row for `path`. If a prior
    active version had different content, demote it to ``archive``.
    Idempotent on identical body (returns the existing row)."""
    rel = _normalise_path(path)
    sha = _sha(body)
    from backend import db
    conn = db._conn()

    async with conn.execute(
        "SELECT * FROM prompt_versions WHERE path=? ORDER BY version DESC LIMIT 1",
        (rel,),
    ) as cur:
        last = await cur.fetchone()
    next_v = (last["version"] + 1) if last else 1

    # Same body as current active → no-op, return existing row.
    async with conn.execute(
        "SELECT * FROM prompt_versions WHERE path=? AND role='active'",
        (rel,),
    ) as cur:
        active = await cur.fetchone()
    if active and active["body_sha256"] == sha:
        return _row_to_version(active)

    if active:
        await conn.execute(
            "UPDATE prompt_versions SET role='archive' WHERE id=?",
            (active["id"],),
        )

    now = time.time()
    cur = await conn.execute(
        "INSERT INTO prompt_versions "
        "(path, version, role, body, body_sha256, created_at, promoted_at) "
        "VALUES (?,?, 'active', ?,?,?,?)",
        (rel, next_v, body, sha, now, now),
    )
    new_id = cur.lastrowid
    await conn.commit()
    return await get_by_id(new_id)


async def register_canary(path: str, body: str) -> PromptVersion:
    """Register a canary candidate for `path`. Replaces any prior open
    canary on the same path (demoted to archive with rollback reason)."""
    rel = _normalise_path(path)
    sha = _sha(body)
    from backend import db
    conn = db._conn()

    async with conn.execute(
        "SELECT * FROM prompt_versions WHERE path=? AND role='canary'",
        (rel,),
    ) as cur:
        prior = await cur.fetchone()
    if prior:
        await conn.execute(
            "UPDATE prompt_versions SET role='archive', "
            "rolled_back_at=?, rollback_reason='superseded by new canary' "
            "WHERE id=?",
            (time.time(), prior["id"]),
        )

    async with conn.execute(
        "SELECT MAX(version) AS m FROM prompt_versions WHERE path=?",
        (rel,),
    ) as cur:
        row = await cur.fetchone()
    next_v = ((row["m"] or 0) + 1)

    now = time.time()
    cur = await conn.execute(
        "INSERT INTO prompt_versions "
        "(path, version, role, body, body_sha256, created_at) "
        "VALUES (?,?, 'canary', ?,?,?)",
        (rel, next_v, body, sha, now),
    )
    new_id = cur.lastrowid
    await conn.commit()
    return await get_by_id(new_id)


async def get_by_id(vid: int) -> PromptVersion:
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM prompt_versions WHERE id=?", (vid,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise LookupError(f"no prompt_version id={vid}")
    return _row_to_version(row)


async def get_active(path: str) -> Optional[PromptVersion]:
    rel = _normalise_path(path)
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM prompt_versions WHERE path=? AND role='active'",
        (rel,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_version(row) if row else None


async def get_canary(path: str) -> Optional[PromptVersion]:
    rel = _normalise_path(path)
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM prompt_versions WHERE path=? AND role='canary'",
        (rel,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_version(row) if row else None


async def list_all(path: str) -> list[PromptVersion]:
    rel = _normalise_path(path)
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM prompt_versions WHERE path=? ORDER BY version DESC",
        (rel,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_version(r) for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Canary routing — deterministic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _pick_canary_bucket(agent_id: str) -> bool:
    """Stable hash → 0..99 bucket. True iff bucket < CANARY_RATE_PCT.
    Same agent_id always maps to the same bucket → repeatable replay."""
    h = hashlib.blake2b(agent_id.encode("utf-8"), digest_size=2).digest()
    bucket = int.from_bytes(h, "big") % 100
    return bucket < CANARY_RATE_PCT


async def pick_for_request(path: str,
                           agent_id: str) -> Optional[tuple[PromptVersion, str]]:
    """Resolve which prompt version to actually serve. Returns
    ``(version, role)`` where role is ``"active"`` or ``"canary"``,
    or ``None`` if no active prompt is registered for this path.

    Deterministic: pure function of (path, agent_id) at the moment
    of call. If `path` has no canary, always returns active.
    """
    active = await get_active(path)
    if active is None:
        return None
    if not _pick_canary_bucket(agent_id):
        return (active, "active")
    canary = await get_canary(path)
    if canary is None:
        return (active, "active")
    return (canary, "canary")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome feedback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def record_outcome(version_id: int, success: bool) -> None:
    """Bump success/failure counter on a version row."""
    col = "success_count" if success else "failure_count"
    from backend import db
    await db._conn().execute(
        f"UPDATE prompt_versions SET {col} = {col} + 1 WHERE id=?",
        (version_id,),
    )
    await db._conn().commit()
    try:
        from backend import metrics as _m
        _m.prompt_outcome_total.labels(
            role="active" if success else "active",  # placeholder (filled by caller)
            outcome="success" if success else "failure",
        ).inc()
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-rollback evaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class CanaryEvaluation:
    decision: str  # "no_canary" | "insufficient_samples" | "promote_canary" | "rollback" | "keep_running"
    canary: Optional[PromptVersion]
    active: Optional[PromptVersion]
    reason: str = ""


async def evaluate_canary(path: str, *,
                          min_samples: int = 20,
                          regression_pp: float = 5.0,
                          window_s: float = 7 * 86400) -> CanaryEvaluation:
    """Decide what to do with the open canary on `path`. Pure read +
    optional rollback action. Returns one of:

      * no_canary             — nothing open
      * insufficient_samples  — wait for more data
      * rollback              — canary regressed; demoted to archive
      * keep_running          — canary holding its own; window not elapsed
      * promote_canary        — canary >= active; window elapsed (caller
                                may now manually call promote_canary())
    """
    canary = await get_canary(path)
    active = await get_active(path)
    if canary is None:
        return CanaryEvaluation("no_canary", None, active)
    if canary.total_samples < min_samples:
        return CanaryEvaluation(
            "insufficient_samples", canary, active,
            f"canary has {canary.total_samples}/{min_samples} samples",
        )
    canary_rate = canary.pass_rate or 0.0
    active_rate = (active.pass_rate or 0.0) if active else 0.0
    pp_delta = (canary_rate - active_rate) * 100

    if pp_delta < -regression_pp:
        # Auto-rollback.
        from backend import db
        reason = (f"canary {canary_rate:.2%} vs active {active_rate:.2%} "
                  f"(Δ={pp_delta:+.1f}pp < -{regression_pp}pp)")
        await db._conn().execute(
            "UPDATE prompt_versions SET role='archive', rolled_back_at=?, "
            "rollback_reason=? WHERE id=?",
            (time.time(), reason, canary.id),
        )
        await db._conn().commit()
        try:
            from backend import metrics as _m
            _m.prompt_rolled_back_total.labels(path=path).inc()
        except Exception:
            pass
        logger.warning(
            "[prompt-canary] auto-rollback: %s (%s)", path, reason,
        )
        return CanaryEvaluation("rollback", canary, active, reason)

    age = time.time() - canary.created_at
    if age >= window_s:
        return CanaryEvaluation(
            "promote_canary", canary, active,
            f"canary held {pp_delta:+.1f}pp over {int(age)}s",
        )
    return CanaryEvaluation(
        "keep_running", canary, active,
        f"canary {canary_rate:.2%} vs active {active_rate:.2%} "
        f"(Δ={pp_delta:+.1f}pp, {int(age)}/{int(window_s)}s)",
    )


async def bootstrap_from_disk(*, paths: list[Path] | None = None) -> list[tuple[str, str]]:
    """Phase 56-DAG-C S3: sync on-disk prompt markdown files into
    ``prompt_versions`` as the active row.

    Idempotent — `register_active` is a no-op when the body hash
    already matches. Called from the app lifespan so a fresh DB
    always has an active version for every shipped prompt file,
    even if no operator has registered anything yet.

    Returns a list of ``(path, action)`` where action is
    ``"registered"`` or ``"unchanged"``. Failures per-file are
    caught + logged so one malformed prompt can't block startup.

    `paths` override is for tests; default scans PROMPTS_ROOT.
    """
    targets = paths if paths is not None else sorted(PROMPTS_ROOT.glob("*.md"))
    outcomes: list[tuple[str, str]] = []
    for p in targets:
        try:
            rel = _normalise_path(str(p))
        except PathRejected as exc:
            logger.warning("bootstrap: skip %s (%s)", p, exc)
            continue
        try:
            body = p.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("bootstrap: read %s failed: %s", p, exc)
            continue
        try:
            prior = await get_active(rel)
            new = await register_active(rel, body)
            if prior and prior.id == new.id:
                outcomes.append((rel, "unchanged"))
            else:
                outcomes.append((rel, "registered"))
                logger.info(
                    "prompt_registry: bootstrapped %s (v%d)", rel, new.version,
                )
        except Exception as exc:
            logger.warning("bootstrap: register %s failed: %s", rel, exc)
    return outcomes


async def promote_canary(path: str) -> Optional[PromptVersion]:
    """Operator action: replace the active prompt with the open canary.
    Old active goes to archive; canary becomes active."""
    canary = await get_canary(path)
    if canary is None:
        return None
    active = await get_active(path)
    from backend import db
    now = time.time()
    if active:
        await db._conn().execute(
            "UPDATE prompt_versions SET role='archive' WHERE id=?",
            (active.id,),
        )
    await db._conn().execute(
        "UPDATE prompt_versions SET role='active', promoted_at=? WHERE id=?",
        (now, canary.id),
    )
    await db._conn().commit()
    return await get_by_id(canary.id)
