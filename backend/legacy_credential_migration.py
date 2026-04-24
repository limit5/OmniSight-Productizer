"""Phase 5-5 (#multi-account-forge) — legacy ``Settings`` → ``git_accounts``
auto-migration.

Lifespan startup hook that detects legacy forge credentials living in
``Settings`` (``github_token`` / ``github_token_map`` / ``gitlab_token``
/ ``gitlab_token_map`` / ``gitlab_url`` / ``gerrit_instances`` / scalar
gerrit / ``notification_jira_*``) and one-shot migrates each one into
a ``git_accounts`` row so the post-Phase-5 resolver
(``backend.git_credentials.pick_account_for_url``) reads the canonical
table instead of falling through to the deprecated shim. Runs at most
once per database (idempotent guard); operator can disable entirely
via ``OMNISIGHT_CREDENTIAL_MIGRATE=skip``.

Idempotency contract
─────────────────────
A non-empty ``git_accounts`` table is the cue that migration already
happened (or that the operator already created accounts via the
Phase-5-4 CRUD). Re-running the hook in that state is a no-op — we
never resurrect rows the operator deliberately deleted, and we never
overwrite credentials that may have been rotated since the legacy
``.env`` snapshot. Operators who want to force a re-migration can
either ``DELETE FROM git_accounts`` first or build their own
purpose-specific INSERT script.

Multi-worker safety (SOP Step 1, qualified answer #2)
─────────────────────────────────────────────────────
Production runs ``uvicorn --workers N`` so the lifespan hook fires
on every worker process at boot. Without coordination, all N workers
would race on a single empty-table observation, each conclude
"empty → migrate", and emit duplicate inserts.

Two layers of defence:

1. **Deterministic primary keys.** Each migrated row's id is a stable
   function of its source (``ga-legacy-{platform}-{slug}``), not a
   ``uuid.uuid4()``. That means worker A and worker B both compute
   the SAME id for the SAME source — so even if they both observe
   the empty table, the second INSERT collides on the PK rather
   than producing a duplicate.

2. **``INSERT ... ON CONFLICT (id) DO NOTHING``.** PG resolves the
   collision at row-write time without raising; the loser silently
   skips. We use ``RETURNING id`` to detect winners; only the winner
   emits the audit row + the per-row "[CRED-MIGRATE]" log line, so
   N workers don't N× the audit chain or the warning volume.

Pattern mirrors :func:`backend.api_keys.migrate_legacy_bearer` (task
#106) — the same shape that closed the duplicated-bearer-row class
of bug.

Read-after-write audit (SOP Step 1)
───────────────────────────────────
The hook executes inside the lifespan ``startup`` block before
uvicorn opens the listening socket — no request handler can observe
the partial-write state. Concurrent worker startups are bounded by
the deterministic-id + ON CONFLICT guard above; a worker that loses
the race observes the "table is non-empty" branch on its NEXT call
(which never comes — the hook only runs once per process).

Module-global audit (SOP Step 1)
────────────────────────────────
No new module-globals introduced. The migration function is pure
async; everything mutable lives in ``git_accounts`` (PG-backed,
cross-worker visible) and the audit chain (also PG-backed).

What is NOT migrated (and why)
───────────────────────────────
* ``git_credentials_file`` (YAML on disk) — operators who have
  carefully curated this file should keep using it via the legacy
  shim until the call-site sweep (rows 5-6/7/8) ports each consumer
  to the async resolver. Migrating YAML in here would create a
  silent "now there are two sources" duality.
* ``git_ssh_key_map`` — SSH paths can't be migrated to encrypted
  bytes without inlining the file contents (which we DON'T want to
  do silently). Operators who depend on per-host SSH keys will need
  to either copy the key payload in via the CRUD endpoint or keep
  using the legacy shim.
* GitHub / GitLab / JIRA *webhook* secrets (``github_webhook_secret``
  / ``gitlab_webhook_secret`` / ``jira_webhook_secret``) — these are
  global per-platform, not per-account, and don't fit the
  ``git_accounts`` row shape cleanly. Stay scalar in ``Settings``
  for now.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from backend.config import settings

logger = logging.getLogger(__name__)


# Action name for audit_log entries this module produces.
_AUDIT_ACTION = "credential_auto_migrate"

# Default tenant for legacy migrations — matches the bootstrap admin's
# tenant. Multi-tenant deployments that already split by tenant will
# have created their accounts via the Phase-5-4 CRUD instead.
_DEFAULT_TENANT = "t-default"

# Kill-switch env var. Operator escape hatch documented in the Phase-5-5
# row spec. Setting it to ``skip`` is the only reason we'd want to
# bypass the migration without also clearing the legacy ``.env`` keys.
_KILL_SWITCH_ENV = "OMNISIGHT_CREDENTIAL_MIGRATE"
_KILL_SWITCH_SKIP = "skip"


def _slug(host: str) -> str:
    """Slug a host into a deterministic id fragment.

    ``"github.com"`` → ``"github-com"``; any non-alphanumeric character
    becomes ``-`` and runs of ``-`` are collapsed. Lowercased + stripped
    so two operators who entered the same host with different casings
    still hash to the same id.
    """
    s = (host or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unknown"


def _parse_json_map(blob: str) -> dict[str, str]:
    if not blob:
        return {}
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "[CRED-MIGRATE] failed to parse JSON map blob (%d chars); "
            "skipping. Check the .env value is valid JSON.",
            len(blob),
        )
    return {}


def _parse_gerrit_instances(blob: str) -> list[dict[str, Any]]:
    if not blob:
        return []
    try:
        data = json.loads(blob)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "[CRED-MIGRATE] failed to parse gerrit_instances JSON; skipping."
        )
    return []


def _host_from_url(url: str) -> str:
    """Extract a lowercased host from an http(s)://… URL, or return
    the input unchanged if it doesn't parse. Used to derive labels +
    deterministic ids from ``gitlab_url`` / ``notification_jira_url``.
    """
    if not url:
        return ""
    if "://" not in url:
        return url.strip().lower().rstrip("/")
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return url.strip().lower().rstrip("/")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Plan-then-write: build the candidate row list, then atomically
#  insert each via ON CONFLICT (id) DO NOTHING.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _plan_rows() -> list[dict[str, Any]]:
    """Return the list of ``git_accounts`` rows to insert from the
    current ``Settings`` snapshot.

    Each dict has the in-memory shape ``create_account`` would consume,
    plus a deterministic ``id`` and a one-line ``source`` tag for the
    audit / log line. Empty input → empty list (caller no-ops).

    Precedence rules (matches the legacy
    ``backend.git_credentials._build_registry`` shim so callers see the
    same end state pre/post migration):

    * ``*_token_map`` entries are migrated as non-default rows (the
      legacy shim never marked map entries default).
    * Scalar ``github_token`` / ``gitlab_token`` are only migrated if
      the corresponding ``*_token_map`` does NOT already cover their
      host (avoids two rows for the same host with the same secret).
      When migrated, they become the platform default.
    * Scalar gerrit (``gerrit_enabled=True`` + ``gerrit_ssh_host``)
      is only migrated if ``gerrit_instances`` is empty / does not
      already cover that ``ssh_host``.
    * JIRA is single-account per ``Settings`` schema today; migrate
      iff URL+token both present.
    """
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    ssh_key_path = (settings.git_ssh_key_path or "").strip()

    def _push(row: dict[str, Any]) -> None:
        rid = row["id"]
        if rid in seen_ids:
            return
        seen_ids.add(rid)
        rows.append(row)

    # ── github_token_map (per-host PATs) ──
    gh_map = _parse_json_map(settings.github_token_map)
    gh_map_hosts: set[str] = set()
    for host, token in gh_map.items():
        host_l = (host or "").strip().lower()
        if not host_l or not token:
            continue
        gh_map_hosts.add(host_l)
        _push({
            "id": f"ga-legacy-github-{_slug(host_l)}",
            "platform": "github",
            "instance_url": f"https://{host_l}",
            "label": f"{host_l} (legacy)",
            "token": token,
            "ssh_key": "",
            "ssh_host": "",
            "ssh_port": 0,
            "project": "",
            "webhook_secret": "",
            "url_patterns": [],
            "is_default": False,
            "enabled": True,
            "source": f"github_token_map[{host_l}]",
        })

    # ── github_token (single scalar fallback → default for github.com) ──
    if settings.github_token and "github.com" not in gh_map_hosts:
        _push({
            "id": "ga-legacy-github-github-com",
            "platform": "github",
            "instance_url": "https://github.com",
            "label": "github.com (legacy)",
            "token": settings.github_token,
            "ssh_key": "",
            "ssh_host": "",
            "ssh_port": 0,
            "project": "",
            "webhook_secret": "",
            "url_patterns": [],
            "is_default": True,
            "enabled": True,
            "source": "github_token",
        })

    # ── gitlab_token_map (per-host PATs) ──
    gl_map = _parse_json_map(settings.gitlab_token_map)
    gl_map_hosts: set[str] = set()
    for host, token in gl_map.items():
        host_l = (host or "").strip().lower()
        if not host_l or not token:
            continue
        gl_map_hosts.add(host_l)
        _push({
            "id": f"ga-legacy-gitlab-{_slug(host_l)}",
            "platform": "gitlab",
            "instance_url": f"https://{host_l}",
            "label": f"{host_l} (legacy)",
            "token": token,
            "ssh_key": "",
            "ssh_host": "",
            "ssh_port": 0,
            "project": "",
            "webhook_secret": "",
            "url_patterns": [],
            "is_default": False,
            "enabled": True,
            "source": f"gitlab_token_map[{host_l}]",
        })

    # ── gitlab_token + gitlab_url (single scalar fallback → default) ──
    if settings.gitlab_token:
        gl_url = (settings.gitlab_url or "https://gitlab.com").strip()
        gl_host = _host_from_url(gl_url) or "gitlab.com"
        if gl_host not in gl_map_hosts:
            _push({
                "id": f"ga-legacy-gitlab-{_slug(gl_host)}",
                "platform": "gitlab",
                "instance_url": gl_url if "://" in gl_url else f"https://{gl_host}",
                "label": f"{gl_host} (legacy)",
                "token": settings.gitlab_token,
                "ssh_key": "",
                "ssh_host": "",
                "ssh_port": 0,
                "project": "",
                "webhook_secret": "",
                "url_patterns": [],
                "is_default": True,
                "enabled": True,
                "source": "gitlab_token",
            })

    # ── gerrit_instances (multi-instance JSON list) ──
    gerrit_instances = _parse_gerrit_instances(settings.gerrit_instances)
    gerrit_instance_hosts: set[str] = set()
    for inst in gerrit_instances:
        ssh_host = (inst.get("ssh_host") or "").strip().lower()
        if not ssh_host:
            continue
        gerrit_instance_hosts.add(ssh_host)
        _push({
            "id": f"ga-legacy-gerrit-{_slug(ssh_host)}",
            "platform": "gerrit",
            "instance_url": (inst.get("url") or "").strip(),
            "label": (
                inst.get("label")
                or inst.get("id")
                or f"{ssh_host} (legacy)"
            ),
            "token": "",
            # Inline SSH key payload only if the inst dict explicitly
            # carries it (rare); never read from disk during migration —
            # operator must paste the key via Phase-5-9 UI.
            "ssh_key": inst.get("ssh_key", "") or "",
            "ssh_host": ssh_host,
            "ssh_port": int(inst.get("ssh_port") or 29418),
            "project": (inst.get("project") or "").strip(),
            "webhook_secret": (inst.get("webhook_secret") or "").strip(),
            "url_patterns": [],
            "is_default": False,
            "enabled": True,
            "source": f"gerrit_instances[{ssh_host}]",
        })

    # ── scalar gerrit (only if enabled + ssh_host + not in instances) ──
    if (
        settings.gerrit_enabled
        and (settings.gerrit_ssh_host or "").strip()
    ):
        ssh_host_l = settings.gerrit_ssh_host.strip().lower()
        if ssh_host_l not in gerrit_instance_hosts:
            _push({
                "id": f"ga-legacy-gerrit-{_slug(ssh_host_l)}",
                "platform": "gerrit",
                "instance_url": (settings.gerrit_url or "").strip(),
                "label": f"{ssh_host_l} (legacy)",
                "token": "",
                "ssh_key": "",
                "ssh_host": ssh_host_l,
                "ssh_port": int(settings.gerrit_ssh_port or 29418),
                "project": (settings.gerrit_project or "").strip(),
                "webhook_secret": (settings.gerrit_webhook_secret or "").strip(),
                "url_patterns": [],
                "is_default": True,
                "enabled": True,
                "source": "gerrit_scalar",
            })

    # ── JIRA (single-account per Settings shape) ──
    jira_url = (settings.notification_jira_url or "").strip()
    jira_token = (settings.notification_jira_token or "").strip()
    if jira_url and jira_token:
        jira_host = _host_from_url(jira_url) or "jira"
        _push({
            "id": f"ga-legacy-jira-{_slug(jira_host)}",
            "platform": "jira",
            "instance_url": jira_url,
            "label": f"{jira_host} (legacy)",
            "token": jira_token,
            "ssh_key": "",
            "ssh_host": "",
            "ssh_port": 0,
            "project": (settings.notification_jira_project or "").strip(),
            "webhook_secret": (settings.jira_webhook_secret or "").strip(),
            "url_patterns": [],
            "is_default": True,
            "enabled": True,
            "source": "notification_jira",
        })

    # Defensive: ssh_key_path scalar is a path, not a key payload — we
    # don't migrate the path into the encrypted_ssh_key column. Tracked
    # in the module-level "What is NOT migrated" docstring; the variable
    # is named here so future maintainers don't accidentally inline-read
    # the file in this hook.
    _ = ssh_key_path

    return rows


async def _table_has_any_row(conn) -> bool:
    """Return True if ``git_accounts`` has at least one row anywhere
    (any tenant, any platform). Cheaper than COUNT(*) on a non-trivial
    table — ``LIMIT 1`` aborts after first match.
    """
    row = await conn.fetchrow("SELECT 1 FROM git_accounts LIMIT 1")
    return row is not None


async def _insert_one(conn, row: dict[str, Any]) -> bool:
    """Insert a single planned row via ``ON CONFLICT (id) DO NOTHING``.

    Returns True iff this call inserted (i.e. won the race against any
    concurrent worker that may have proposed the same deterministic id).
    Returns False on conflict — silently — so the audit + log emission
    stays one-per-row across the whole worker pool.
    """
    from backend.secret_store import encrypt

    enc_token = encrypt(row["token"]) if row.get("token") else ""
    enc_ssh = encrypt(row["ssh_key"]) if row.get("ssh_key") else ""
    enc_whs = (
        encrypt(row["webhook_secret"]) if row.get("webhook_secret") else ""
    )
    now = time.time()
    inserted = await conn.fetchrow(
        "INSERT INTO git_accounts ("
        " id, tenant_id, platform, instance_url, label, username,"
        " encrypted_token, encrypted_ssh_key, ssh_host, ssh_port, project,"
        " encrypted_webhook_secret, url_patterns, auth_type, is_default,"
        " enabled, metadata, created_at, updated_at, version"
        ") VALUES ("
        " $1, $2, $3, $4, $5, $6,"
        " $7, $8, $9, $10, $11,"
        " $12, $13, 'pat', $14,"
        " $15, '{}', $16, $17, 0"
        ") ON CONFLICT (id) DO NOTHING "
        "RETURNING id",
        row["id"], _DEFAULT_TENANT, row["platform"],
        row.get("instance_url", ""),
        row.get("label", ""), "",
        enc_token, enc_ssh,
        row.get("ssh_host", ""), int(row.get("ssh_port") or 0),
        row.get("project", ""),
        enc_whs, json.dumps(list(row.get("url_patterns") or [])),
        bool(row.get("is_default", False)),
        bool(row.get("enabled", True)),
        now, now,
    )
    return inserted is not None


async def migrate_legacy_credentials_once() -> dict[str, Any]:
    """Lifespan hook entry point.

    Returns a structured summary the caller can log:

    ``{
        "migrated": int,                 # rows actually inserted
        "candidates": int,               # rows planned (incl. losers)
        "skipped_reason": str | None,    # set iff migration was no-op
        "sources": list[str],            # source tags of inserted rows
    }``

    Never raises on pool / DB errors — those are logged at warning and
    surface as ``skipped_reason="error: ..."`` so a transient PG hiccup
    on boot doesn't crash the lifespan. (The Phase-5 resolver will fall
    back to the shim if migration didn't run, so the rest of the app
    keeps working.)
    """
    # ── Kill switch (operator escape hatch) ──
    kill = (os.environ.get(_KILL_SWITCH_ENV) or "").strip().lower()
    if kill == _KILL_SWITCH_SKIP:
        logger.info(
            "[CRED-MIGRATE] %s=skip — bypassing legacy → git_accounts "
            "migration entirely.",
            _KILL_SWITCH_ENV,
        )
        return {
            "migrated": 0,
            "candidates": 0,
            "skipped_reason": f"env:{_KILL_SWITCH_ENV}=skip",
            "sources": [],
        }

    # ── Pool gate (SQLite dev mode has no pool yet — skip) ──
    try:
        from backend.db_pool import get_pool
        pool = get_pool()
    except RuntimeError:
        logger.info(
            "[CRED-MIGRATE] db_pool not initialised (SQLite dev mode); "
            "skipping legacy credential migration."
        )
        return {
            "migrated": 0,
            "candidates": 0,
            "skipped_reason": "no_pool",
            "sources": [],
        }

    # ── Plan rows from current Settings snapshot ──
    planned = _plan_rows()
    if not planned:
        logger.info(
            "[CRED-MIGRATE] no legacy credentials present in Settings; "
            "nothing to migrate."
        )
        return {
            "migrated": 0,
            "candidates": 0,
            "skipped_reason": "no_legacy_credentials",
            "sources": [],
        }

    # ── Idempotency: if any row already exists, defer to operator. ──
    try:
        async with pool.acquire() as conn:
            if await _table_has_any_row(conn):
                logger.info(
                    "[CRED-MIGRATE] git_accounts already has rows; "
                    "skipping legacy migration (operator-managed table)."
                )
                return {
                    "migrated": 0,
                    "candidates": len(planned),
                    "skipped_reason": "git_accounts_non_empty",
                    "sources": [],
                }
    except Exception as exc:
        logger.warning(
            "[CRED-MIGRATE] idempotency check failed (%s); skipping "
            "migration this boot.",
            type(exc).__name__,
        )
        return {
            "migrated": 0,
            "candidates": len(planned),
            "skipped_reason": f"error:{type(exc).__name__}",
            "sources": [],
        }

    # ── Set tenant context for audit chain ──
    from backend.db_context import current_tenant_id, set_tenant_id
    prev_tid = current_tenant_id()
    set_tenant_id(_DEFAULT_TENANT)

    inserted_sources: list[str] = []
    try:
        for row in planned:
            try:
                won = False
                async with pool.acquire() as conn:
                    won = await _insert_one(conn, row)
            except Exception as exc:
                logger.warning(
                    "[CRED-MIGRATE] insert failed for source=%s id=%s (%s); "
                    "continuing with remaining rows.",
                    row.get("source"), row.get("id"), type(exc).__name__,
                )
                continue

            if not won:
                # Lost the race against a sibling worker; the winner
                # already emitted the audit + log line for this id.
                logger.debug(
                    "[CRED-MIGRATE] id=%s already inserted by sibling "
                    "worker; skipping audit emit.",
                    row.get("id"),
                )
                continue

            inserted_sources.append(row.get("source", row["id"]))
            # Fire audit row. Don't pass the plaintext token — only
            # metadata (id / platform / source) so a leaked audit
            # snapshot can't be replayed.
            try:
                from backend import audit as _audit
                await _audit.log(
                    action=_AUDIT_ACTION,
                    entity_kind="git_account",
                    entity_id=row["id"],
                    before=None,
                    after={
                        "platform": row["platform"],
                        "instance_url": row.get("instance_url", ""),
                        "label": row.get("label", ""),
                        "ssh_host": row.get("ssh_host", ""),
                        "is_default": bool(row.get("is_default", False)),
                        "source": row.get("source", ""),
                    },
                    actor="system/migration",
                )
            except Exception as exc:  # pragma: no cover — audit best-effort
                logger.warning(
                    "[CRED-MIGRATE] audit.log raised %s for id=%s — "
                    "row already inserted, proceeding.",
                    type(exc).__name__, row.get("id"),
                )
            logger.warning(
                "[CRED-MIGRATE] migrated source=%s → git_accounts row %s "
                "(platform=%s, label=%r, default=%s). "
                "Update operator runbook: legacy %s knob is now "
                "shadowed by the git_accounts table.",
                row.get("source"), row["id"], row["platform"],
                row.get("label", ""), row.get("is_default", False),
                row.get("source"),
            )
    finally:
        set_tenant_id(prev_tid)

    return {
        "migrated": len(inserted_sources),
        "candidates": len(planned),
        "skipped_reason": None,
        "sources": inserted_sources,
    }
