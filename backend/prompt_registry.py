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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_ROOT = _PROJECT_ROOT / "backend" / "agents" / "prompts"
SKILLS_ROOT = _PROJECT_ROOT / "configs" / "skills"
ROLES_ROOT = _PROJECT_ROOT / "configs" / "roles"
CANARY_RATE_PCT = 5  # design-locked: 5%

# B15 #350: Anthropic rule-of-thumb for token estimation (≈4 chars / token).
# Used by get_skill_metadata() so operators can budget skill load-outs
# without running the tokenizer.
_CHARS_PER_TOKEN = 4


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
#  B15 #350 — Skill Lazy Loading (metadata-only lookup)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _resolve_skill_path(path: str | Path) -> Optional[Path]:
    """Resolve a skill identifier to its on-disk markdown file.

    Accepts any of:
      * bare skill name          → ``configs/skills/<name>/SKILL.md``
      * relative/absolute path   → used directly if it exists
      * directory path           → ``<dir>/SKILL.md`` if present

    Returns ``None`` when nothing resolves (caller decides whether that's
    an error — `get_skill_metadata` degrades gracefully, returns ``{}``).

    Read-only: unlike `_normalise_path`, this does not enforce the
    prompt-registry whitelist because metadata can be safely exposed for
    any skill/role markdown shipped in the repo.
    """
    if not path:
        return None
    raw = str(path)
    p = Path(raw)

    # Absolute or relative to project root.
    if p.is_absolute():
        candidate = p
    else:
        # Bare name (single path segment, no extension) → try
        # `configs/skills/<name>/SKILL.md` first.
        if "/" not in raw and "\\" not in raw and p.suffix == "":
            bare = SKILLS_ROOT / raw / "SKILL.md"
            if bare.is_file():
                return bare.resolve()
        candidate = (_PROJECT_ROOT / p).resolve()

    if candidate.is_dir():
        nested = candidate / "SKILL.md"
        if nested.is_file():
            return nested.resolve()
        return None
    if candidate.is_file():
        return candidate.resolve()
    return None


def _parse_frontmatter_text(text: str) -> tuple[dict, str]:
    """Split ``text`` into (frontmatter_dict, body). Frontmatter is the
    leading ``---\\n…\\n---`` YAML block; everything after is body.
    Returns ``({}, text)`` when no frontmatter is present or yaml fails."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}, text
    try:
        import yaml
        fm = yaml.safe_load(match.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception as exc:
        logger.warning("skill frontmatter parse failed: %s", exc)
        fm = {}
    body = text[match.end():]
    return fm, body


def _derive_trigger_condition(fm: dict, body: str) -> str:
    """Pull the skill's trigger hint out of (a) frontmatter keys or
    (b) the ``## When to use`` markdown section — whichever is present.
    Empty string if neither exists (the skill hasn't declared one yet —
    B15 TODO row 286 will backfill these).
    """
    for key in ("trigger_condition", "trigger", "when_to_use"):
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (list, tuple)) and val:
            return " / ".join(str(x).strip() for x in val if str(x).strip())

    # Fallback: scan markdown body for a "When to use" heading.
    m = re.search(
        r"(?im)^#{1,6}\s*when\s+to\s+use\b[^\n]*\n(.+?)(?=\n#{1,6}\s|\Z)",
        body,
        re.DOTALL,
    )
    if m:
        section = m.group(1).strip()
        if section:
            # Truncate — trigger hint is not the whole section.
            return section[:500] + ("…" if len(section) > 500 else "")
    return ""


def get_skill_metadata(path: str | Path) -> dict:
    """B15 (#350) — return a skill's *advertising card* without paying
    the token cost of loading its full body.

    Keys returned (always present, may be empty):
      * ``name``               — frontmatter ``name`` or file/dir stem
      * ``description``        — frontmatter ``description`` (one-line)
      * ``trigger_condition``  — when the agent should load this skill
                                 (frontmatter ``trigger_condition`` /
                                 ``trigger`` / ``when_to_use`` or the
                                 ``## When to use`` markdown section)
      * ``token_cost``         — rough token count of the *full* body
                                 (chars ÷ 4, Anthropic rule of thumb)

    Extra keys passed through when present: ``keywords``, ``version``,
    ``path`` (absolute on-disk path as string).

    Missing or unreadable files return ``{}`` — callers can treat this
    as "skill unknown" without a try/except.
    """
    resolved = _resolve_skill_path(path)
    if resolved is None:
        return {}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("get_skill_metadata: read %s failed: %s", resolved, exc)
        return {}

    fm, body = _parse_frontmatter_text(text)

    # OmniSight skill packs (configs/skills/<name>/) also ship a sibling
    # `skill.yaml` manifest — merge it in as a fallback so metadata keys
    # missing from SKILL.md frontmatter are filled from the manifest.
    sibling_yaml = resolved.parent / "skill.yaml"
    if sibling_yaml.is_file():
        try:
            import yaml
            ydata = yaml.safe_load(sibling_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(ydata, dict):
                for k, v in ydata.items():
                    fm.setdefault(k, v)
        except Exception as exc:
            logger.warning("get_skill_metadata: skill.yaml parse failed for %s: %s",
                           sibling_yaml, exc)

    # Prefer explicit frontmatter name; fall back to parent dir name for
    # configs/skills/<name>/SKILL.md, else the file stem.
    name = fm.get("name") or ""
    if not name:
        if resolved.name.lower() == "skill.md":
            name = resolved.parent.name
        else:
            name = resolved.stem.replace(".skill", "")

    description = fm.get("description") or ""
    if isinstance(description, str):
        description = " ".join(description.split())  # collapse YAML folding

    meta: dict = {
        "name": str(name),
        "description": str(description),
        "trigger_condition": _derive_trigger_condition(fm, body),
        "token_cost": max(1, len(body) // _CHARS_PER_TOKEN) if body else 0,
        "path": str(resolved),
    }
    for passthrough in ("keywords", "version", "label", "label_en"):
        if passthrough in fm:
            meta[passthrough] = fm[passthrough]
    return meta


def get_skill_full(path: str | Path) -> str:
    """B15 (#350) — return a skill's *complete* markdown content
    (frontmatter + body) for on-demand injection after the agent has
    consulted `get_skill_metadata` and decided to load it.

    Pairs with `get_skill_metadata`:
      * `get_skill_metadata` — advertising card only (~150 chars).
      * `get_skill_full`     — full skill body for the ReAct loop to
                               inject on `[LOAD_SKILL: …]` signal.

    Accepts the same identifiers as `get_skill_metadata` (bare name,
    relative path, absolute path, or directory containing SKILL.md).

    Missing or unreadable files return an empty string — the lazy-loader
    treats "" as "skill body unavailable" and logs, so a malformed skill
    never crashes the ReAct loop.
    """
    resolved = _resolve_skill_path(path)
    if resolved is None:
        return ""
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("get_skill_full: read %s failed: %s", resolved, exc)
        return ""


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

async def _register_active_impl(conn, rel: str, body: str) -> int:
    # Advisory lock keyed on (role, path) — serialises concurrent
    # same-path writers across workers. Without it, two workers both
    # see no existing row, both compute next_v=1, both INSERT, and
    # the loser's UNIQUE(path, version) violation poisons the
    # transaction. The lock is tx-scoped → released on COMMIT.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"prompt-register-active-{rel}",
    )
    sha = _sha(body)

    # Same body as current active → no-op, return existing row.
    active = await conn.fetchrow(
        "SELECT id, body_sha256 FROM prompt_versions "
        "WHERE path = $1 AND role = 'active'",
        rel,
    )
    if active and active["body_sha256"] == sha:
        return active["id"]

    last = await conn.fetchrow(
        "SELECT version FROM prompt_versions WHERE path = $1 "
        "ORDER BY version DESC LIMIT 1",
        rel,
    )
    next_v = (last["version"] + 1) if last else 1

    if active:
        await conn.execute(
            "UPDATE prompt_versions SET role = 'archive' WHERE id = $1",
            active["id"],
        )

    now = time.time()
    row = await conn.fetchrow(
        "INSERT INTO prompt_versions "
        "(path, version, role, body, body_sha256, created_at, promoted_at) "
        "VALUES ($1, $2, 'active', $3, $4, $5, $6) RETURNING id",
        rel, next_v, body, sha, now, now,
    )
    return row["id"]


async def register_active(
    path: str, body: str, conn=None,
) -> PromptVersion:
    """Register or replace the ``active`` row for `path`. If a prior
    active version had different content, demote it to ``archive``.
    Idempotent on identical body (returns the existing row).

    Task #105 (2026-04-21): ported from the shared compat ``_conn()``
    to a per-call pool acquire + tx wrap + ``pg_advisory_xact_lock``
    on ``hashtext(prompt-register-active-<path>)``. Two regressions
    this closes:

    1. Check-then-insert race under multi-worker — two workers running
       bootstrap_from_disk in parallel could both see an empty table,
       both compute ``next_v=1``, and both INSERT, one failing on
       UNIQUE(path, version). The advisory lock serialises them on
       path, so the loser sees the winner's committed row and
       short-circuits.
    2. Failed INSERT poisons the shared compat connection's tx,
       turning every subsequent ``db._conn().execute(...)`` from ANY
       caller (/readyz's db_ping, alembic_probe, etc.) into
       ``current transaction is aborted``. Moving to per-call pool
       acquire + ``async with conn.transaction()`` means the conn
       auto-rolls-back on exception and returns to the pool healthy.
    """
    rel = _normalise_path(path)
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                new_id = await _register_active_impl(owned, rel, body)
    else:
        async with conn.transaction():
            new_id = await _register_active_impl(conn, rel, body)
    return await get_by_id(new_id)


async def _register_canary_impl(conn, rel: str, body: str) -> int:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"prompt-register-canary-{rel}",
    )
    sha = _sha(body)

    prior = await conn.fetchrow(
        "SELECT id FROM prompt_versions "
        "WHERE path = $1 AND role = 'canary'",
        rel,
    )
    if prior:
        await conn.execute(
            "UPDATE prompt_versions SET role = 'archive', "
            "rolled_back_at = $1, "
            "rollback_reason = 'superseded by new canary' "
            "WHERE id = $2",
            time.time(), prior["id"],
        )

    row = await conn.fetchrow(
        "SELECT MAX(version) AS m FROM prompt_versions WHERE path = $1",
        rel,
    )
    next_v = ((row["m"] or 0) + 1)

    now = time.time()
    row = await conn.fetchrow(
        "INSERT INTO prompt_versions "
        "(path, version, role, body, body_sha256, created_at) "
        "VALUES ($1, $2, 'canary', $3, $4, $5) RETURNING id",
        rel, next_v, body, sha, now,
    )
    return row["id"]


async def register_canary(
    path: str, body: str, conn=None,
) -> PromptVersion:
    """Register a canary candidate for `path`. Replaces any prior open
    canary on the same path (demoted to archive with rollback reason).

    Task #105 (2026-04-21): same port recipe as ``register_active``
    above — pool + tx + advisory lock on
    ``hashtext(prompt-register-canary-<path>)``. Uses a DIFFERENT
    lock key than register_active so a canary registration isn't
    blocked by an in-flight active registration on the same path
    (the two operations share a table but touch disjoint rows:
    canary never hits the active row's version-increment path).
    """
    rel = _normalise_path(path)
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                new_id = await _register_canary_impl(owned, rel, body)
    else:
        async with conn.transaction():
            new_id = await _register_canary_impl(conn, rel, body)
    return await get_by_id(new_id)


_VERSION_COLS = (
    "id, path, version, role, body, body_sha256, "
    "success_count, failure_count, created_at, "
    "promoted_at, rolled_back_at, rollback_reason"
)


async def get_by_id(vid: int, conn=None) -> PromptVersion:
    sql = f"SELECT {_VERSION_COLS} FROM prompt_versions WHERE id = $1"
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, vid)
    else:
        row = await conn.fetchrow(sql, vid)
    if not row:
        raise LookupError(f"no prompt_version id={vid}")
    return _row_to_version(row)


async def get_active(
    path: str, conn=None,
) -> Optional[PromptVersion]:
    rel = _normalise_path(path)
    sql = (
        f"SELECT {_VERSION_COLS} FROM prompt_versions "
        "WHERE path = $1 AND role = 'active'"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, rel)
    else:
        row = await conn.fetchrow(sql, rel)
    return _row_to_version(row) if row else None


async def get_canary(
    path: str, conn=None,
) -> Optional[PromptVersion]:
    rel = _normalise_path(path)
    sql = (
        f"SELECT {_VERSION_COLS} FROM prompt_versions "
        "WHERE path = $1 AND role = 'canary'"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, rel)
    else:
        row = await conn.fetchrow(sql, rel)
    return _row_to_version(row) if row else None


async def list_all(
    path: str, conn=None,
) -> list[PromptVersion]:
    rel = _normalise_path(path)
    sql = (
        f"SELECT {_VERSION_COLS} FROM prompt_versions "
        "WHERE path = $1 ORDER BY version DESC"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            rows = await owned.fetch(sql, rel)
    else:
        rows = await conn.fetch(sql, rel)
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


async def pick_for_request(
    path: str, agent_id: str, conn=None,
) -> Optional[tuple[PromptVersion, str]]:
    """Resolve which prompt version to actually serve. Returns
    ``(version, role)`` where role is ``"active"`` or ``"canary"``,
    or ``None`` if no active prompt is registered for this path.

    Deterministic: pure function of (path, agent_id) at the moment
    of call. If `path` has no canary, always returns active.
    """
    active = await get_active(path, conn=conn)
    if active is None:
        return None
    if not _pick_canary_bucket(agent_id):
        return (active, "active")
    canary = await get_canary(path, conn=conn)
    if canary is None:
        return (active, "active")
    return (canary, "canary")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome feedback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def record_outcome(
    version_id: int, success: bool, conn=None,
) -> None:
    """Bump success/failure counter on a version row.

    SP-5.2: atomic ``col = col + 1`` UPDATE — safe under concurrent
    workers without an advisory lock because PG serialises writes
    to the same row at the kernel level. Same shape as SP-4.4's
    failed_login_count fix.
    """
    col = "success_count" if success else "failure_count"
    sql = (
        f"UPDATE prompt_versions SET {col} = {col} + 1 WHERE id = $1"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            await owned.execute(sql, version_id)
    else:
        await conn.execute(sql, version_id)
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


async def evaluate_canary(
    path: str, *,
    min_samples: int = 20,
    regression_pp: float = 5.0,
    window_s: float = 7 * 86400,
    conn=None,
) -> CanaryEvaluation:
    """Decide what to do with the open canary on `path`. Pure read +
    optional rollback action. Returns one of:

      * no_canary             — nothing open
      * insufficient_samples  — wait for more data
      * rollback              — canary regressed; demoted to archive
      * keep_running          — canary holding its own; window not elapsed
      * promote_canary        — canary >= active; window elapsed (caller
                                may now manually call promote_canary())

    SP-5.2: the rollback-UPDATE path runs inside its own pool acquire
    with an advisory lock keyed on the path, so two concurrent cron
    evaluations on the same path can't both decide "rollback" and
    both UPDATE (the second would be a no-op since role is already
    'archive', but the lock makes the decision deterministic).
    """
    canary = await get_canary(path, conn=conn)
    active = await get_active(path, conn=conn)
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
        # Auto-rollback. Wrap in tx + advisory lock on path so two
        # concurrent evaluators can't both decide-and-update.
        reason = (f"canary {canary_rate:.2%} vs active {active_rate:.2%} "
                  f"(Δ={pp_delta:+.1f}pp < -{regression_pp}pp)")

        async def _do_rollback(c):
            await c.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"prompt-rollback-{path}",
            )
            await c.execute(
                "UPDATE prompt_versions SET role = 'archive', "
                "rolled_back_at = $1, rollback_reason = $2 "
                "WHERE id = $3 AND role = 'canary'",
                time.time(), reason, canary.id,
            )

        if conn is None:
            from backend.db_pool import get_pool
            async with get_pool().acquire() as owned:
                async with owned.transaction():
                    await _do_rollback(owned)
        else:
            async with conn.transaction():
                await _do_rollback(conn)
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ZZ.C1 #305-1 checkbox 2 — runtime snapshot capture
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Role label for rows written by :func:`capture_prompt_snapshot`. Kept
# distinct from ``active`` / ``canary`` / ``archive`` so the auto-
# captured runtime trail does not interfere with active-row promotion
# or canary routing — the read API (GET /runtime/prompts) does not
# filter by role, so snapshots still surface in the timeline.
SNAPSHOT_ROLE = "snapshot"


async def _capture_snapshot_impl(
    conn, rel: str, body: str,
) -> Optional[int]:
    sha = _sha(body)
    # Fast path (no lock): if the hash is already known for this path,
    # nothing to do. Hits the ``idx_prompt_versions_path_role`` prefix
    # scan + body_sha256 filter — cheap compared to acquiring the
    # advisory lock on every prompt build.
    existing = await conn.fetchrow(
        "SELECT id FROM prompt_versions "
        "WHERE path = $1 AND body_sha256 = $2 LIMIT 1",
        rel, sha,
    )
    if existing:
        return None

    # Advisory lock shares the same key as ``register_active`` /
    # ``promote_canary`` so a concurrent deploy and a runtime snapshot
    # cannot both pick the same ``version`` (UNIQUE(path, version)).
    # Tx-scoped → released on COMMIT / ROLLBACK.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"prompt-register-active-{rel}",
    )

    # Re-check under the lock — another worker may have inserted the
    # same hash between our fast-path SELECT and the lock grant.
    existing = await conn.fetchrow(
        "SELECT id FROM prompt_versions "
        "WHERE path = $1 AND body_sha256 = $2 LIMIT 1",
        rel, sha,
    )
    if existing:
        return None

    last = await conn.fetchrow(
        "SELECT MAX(version) AS m FROM prompt_versions WHERE path = $1",
        rel,
    )
    next_v = (last["m"] or 0) + 1

    row = await conn.fetchrow(
        "INSERT INTO prompt_versions "
        "(path, version, role, body, body_sha256, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
        rel, next_v, SNAPSHOT_ROLE, body, sha, time.time(),
    )
    return int(row["id"])


async def capture_prompt_snapshot(
    path: str, body: str, conn=None,
) -> Optional[int]:
    """Best-effort auto-capture of an assembled system prompt.

    ZZ.C1 #305-1 checkbox 2: ``build_system_prompt()`` calls this after
    composing the per-turn prompt so the version-timeline API
    (``GET /runtime/prompts``) surfaces the exact strings agents saw at
    runtime — no operator deploy action required.

    Semantics:
      * Deduped by ``body_sha256`` — identical content is never written
        twice for the same ``path``. Returns ``None`` when the hash is
        already present.
      * Advisory lock (shared key with ``register_active``) serialises
        concurrent workers so two INSERTs cannot collide on
        ``UNIQUE(path, version)``.
      * Writes ``role='snapshot'`` — a new role reserved for this path.
        Does NOT touch the existing active/canary rows. Timeline reads
        (no role filter) still surface snapshots alongside deploys.

    Returns the new row id, or ``None`` when the hash already existed.
    """
    rel = _normalise_path(path)
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                return await _capture_snapshot_impl(owned, rel, body)
    async with conn.transaction():
        return await _capture_snapshot_impl(conn, rel, body)


async def bootstrap_from_disk(
    *, paths: list[Path] | None = None, conn=None,
) -> list[tuple[str, str]]:
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
            prior = await get_active(rel, conn=conn)
            new = await register_active(rel, body, conn=conn)
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


async def _promote_canary_impl(
    conn, path: str,
) -> Optional[PromptVersion]:
    # Advisory lock keyed on (path) so a concurrent register_active
    # or evaluate_canary rollback can't interleave with the two
    # UPDATEs. The lock key is shared with register_active's key
    # scheme to guarantee no same-path writer races promote.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"prompt-register-active-{path}",
    )
    canary = await get_canary(path, conn=conn)
    if canary is None:
        return None
    active = await get_active(path, conn=conn)
    now = time.time()
    if active:
        await conn.execute(
            "UPDATE prompt_versions SET role = 'archive' WHERE id = $1",
            active.id,
        )
    await conn.execute(
        "UPDATE prompt_versions SET role = 'active', "
        "promoted_at = $1 WHERE id = $2",
        now, canary.id,
    )
    return await get_by_id(canary.id, conn=conn)


async def promote_canary(
    path: str, conn=None,
) -> Optional[PromptVersion]:
    """Operator action: replace the active prompt with the open canary.
    Old active goes to archive; canary becomes active.

    SP-5.2: two UPDATEs are now inside a single tx + advisory lock.
    Previously the compat-path crash window between the two statements
    left the old active row archived while the canary was still
    'canary' — the prompt path had NO active version briefly. The tx
    makes the promotion atomic; the advisory lock (shared key with
    register_active) prevents interleaving with a concurrent
    register_active that would land a new pending 'active' row on
    top of this operation.
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                return await _promote_canary_impl(owned, path)
    async with conn.transaction():
        return await _promote_canary_impl(conn, path)
    return await get_by_id(canary.id)
