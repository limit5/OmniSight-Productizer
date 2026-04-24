"""Git Credential Registry — multi-account credential resolver.

Phase 5-2 (#multi-account-forge) refactor (2026-04-24)
──────────────────────────────────────────────────────
The registry now has two layered data sources:

1. **``git_accounts`` table** (pool-backed, per-tenant) — the canonical
   source going forward. Read via :func:`get_credential_registry_async`,
   :func:`pick_account_for_url`, :func:`pick_default`, :func:`pick_by_id`.
   These routines scope to ``db_context.current_tenant_id()`` (or the
   caller-supplied ``tenant_id`` kwarg) and decrypt Fernet ciphertext
   on read via :mod:`backend.secret_store`.

2. **Legacy ``Settings`` JSON maps / scalars** — the deprecated data
   model that Phase 5 replaces. Kept alive via the backward-compat
   shim :func:`_build_registry`, which synthesises virtual
   ``git_accounts``-shaped rows from the legacy fields so that every
   sync caller (``find_credential_for_url``, ``get_token_for_url``,
   ``get_ssh_key_for_url``, ``get_webhook_secret_for_host``) keeps
   working while rows 5-6/7/8 port call sites one by one. The shim
   emits a deprecation warning once per process on first read.

Precedence when the async path is used: if ``git_accounts`` has any
rows for the current tenant, return them. Otherwise fall back to the
legacy shim. This gives us the rollout ramp — Phase 5-5 (auto-
migration) moves legacy ``.env`` values into ``git_accounts``; once
that runs, the shim silently yields to the real table without the
call site noticing.

Module-global audit (SOP Step 1, qualified answer #1)
─────────────────────────────────────────────────────
Two module-level variables:

* ``_CREDENTIALS_CACHE`` / ``_CACHE_LOCK`` — legacy in-process cache
  of the shim output. Each uvicorn worker builds its own copy from
  the same ``Settings`` source, so the cache is identical across
  workers by construction (answer #1). Invalidated via
  :func:`clear_credential_cache` when ``PUT /runtime/settings`` edits
  the JSON maps.
* ``_LEGACY_WARN_EMITTED`` — one-shot flag to ensure the deprecation
  log fires once per process. Each worker emits once on its first
  shim call; that's the intended log volume (answer #1).

The async path holds no cache — every call round-trips to PG. The
``git_accounts`` table is O(<10 rows per tenant) in realistic
deployments; caching is not worth the staleness risk.

Read-after-write audit (SOP Step 1)
───────────────────────────────────
No write path is added or changed by this row. Row 5-4 will add the
CRUD endpoints that write to ``git_accounts``. Until then, the table
is empty in practice and the async read falls through to the shim
(same values every caller was already seeing).

Phase 5-3 (#multi-account-forge) additions (2026-04-24)
───────────────────────────────────────────────────────
Builds on 5-2 to lock the URL-pattern resolver contract:

* **Pattern syntax — glob, `fnmatch`-compatible.** Patterns are
  compared against a scheme-stripped lowercased URL form so the
  same pattern matches both ``https://github.com/acme-corp/app``
  and ``git@github.com:acme-corp/app`` — both normalise to
  ``github.com/acme-corp/app``. Glob metacharacters (``*``, ``?``,
  ``[seq]``) behave per :mod:`fnmatch`; everything else — including
  dots, dashes, underscores — is literal. See
  ``docs/phase-5-multi-account/01-design.md`` §3.4 + §3.10.

* **Deterministic first-match-wins.** The async registry is SELECTed
  with ``ORDER BY is_default DESC, last_used_at DESC NULLS LAST,
  platform, id``. The Python loop in :func:`pick_account_for_url`
  iterates in that same order; the first row whose ``url_patterns``
  entry glob-matches wins. Two accounts with overlapping patterns
  → the ``is_default=TRUE`` one wins; if neither is default, the
  more-recently-used one wins (LRU touched on every successful
  resolve).

* **Touch-on-resolve.** After :func:`pick_account_for_url`,
  :func:`pick_default`, or :func:`pick_by_id` returns a row, the
  resolver best-effort UPDATEs ``git_accounts.last_used_at =
  time.time()`` for that ``(id, tenant_id)``. Best-effort means:
  if the pool isn't initialised, if the id doesn't match a real
  row (shim fallback), or if the UPDATE raises, the touch silently
  no-ops and the resolve still returns the row. This lets the
  SELECT ordering above act as a true LRU without a cron job.

* **Explicit-raise variant.** :func:`require_account_for_url` is
  the same as :func:`pick_account_for_url` but raises
  :class:`MissingCredentialError` instead of returning ``None``
  when nothing matches AND no platform default exists. Call sites
  that cannot proceed without a credential (clone / push / webhook
  verify) should use this; call sites that have a fall-through
  path (e.g. anonymous public GitHub read) should use the
  returning-None variant.
"""

from __future__ import annotations

import json
import logging
import threading as _threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yaml

from backend.config import settings
from backend.db_context import current_tenant_id

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CREDENTIALS_CACHE: list[dict] | None = None
_CACHE_LOCK = _threading.Lock()
_LEGACY_WARN_EMITTED: bool = False


# Columns pulled from ``git_accounts`` when the async path is in use.
# Matches alembic 0027 + docs/phase-5-multi-account/01-design.md §2.
_GIT_ACCOUNTS_COLS = (
    "id, tenant_id, platform, instance_url, label, username, "
    "encrypted_token, encrypted_ssh_key, ssh_host, ssh_port, project, "
    "encrypted_webhook_secret, url_patterns, auth_type, is_default, "
    "enabled, metadata, last_used_at, created_at, updated_at, version"
)


def _allowed_credential_roots() -> list[Path]:
    """Directories from which the credentials file may be loaded."""
    roots = [(_PROJECT_ROOT / "configs").resolve()]
    home_ssh = (Path("~/.config/omnisight").expanduser()).resolve()
    roots.append(home_ssh)
    return roots


def _load_yaml_credentials() -> list[dict]:
    """Load credentials from git_credentials.yaml.

    The configured path must resolve under one of the allowed roots
    (configs/ or ~/.config/omnisight/) to prevent path-traversal abuse
    via OMNISIGHT_GIT_CREDENTIALS_FILE.
    """
    if settings.git_credentials_file:
        candidate = Path(settings.git_credentials_file).expanduser()
    else:
        candidate = _PROJECT_ROOT / "configs" / "git_credentials.yaml"

    try:
        resolved = candidate.resolve(strict=False)
    except Exception:
        return []

    allowed = _allowed_credential_roots()
    if not any(_is_within(resolved, root) for root in allowed):
        logger.warning(
            "Refusing to load credentials from %s (outside allowed roots)",
            resolved,
        )
        return []

    if not resolved.exists():
        return []

    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        repos = data.get("repositories", [])
        if not isinstance(repos, list):
            logger.warning(
                "git_credentials.yaml: 'repositories' must be a list (got %s) — ignoring",
                type(repos).__name__,
            )
            return []
        # H6: schema validation — drop entries that fail required-field check
        validated: list[dict] = []
        required_any = ("token", "ssh_key", "webhook_secret")
        for i, entry in enumerate(repos):
            if not isinstance(entry, dict):
                logger.warning("repo[%d]: not a dict — skipping", i)
                continue
            if not entry.get("id") and not entry.get("url") and not entry.get("ssh_host"):
                logger.warning("repo[%d]: needs at least one of id/url/ssh_host", i)
                continue
            if not any(entry.get(k) for k in required_any):
                logger.warning(
                    "repo[%d] (%s): no token/ssh_key/webhook_secret — skipping",
                    i, entry.get("id") or entry.get("url") or "<unknown>",
                )
                continue
            validated.append(entry)
        logger.info("Loaded %d/%d repo credentials from %s", len(validated), len(repos), resolved)
        return validated
    except yaml.YAMLError as exc:
        # Avoid logging credential-bearing snippets — only the error type/line.
        logger.warning(
            "Failed to parse git_credentials.yaml (%s)", type(exc).__name__
        )
    except Exception as exc:  # pragma: no cover — unexpected I/O
        logger.warning("Credential load I/O error: %s", type(exc).__name__)

    return []


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_json_map(json_str: str) -> dict[str, str]:
    """Parse a JSON map string from config, return empty dict on failure."""
    if not json_str:
        return {}
    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def _load_gerrit_instances() -> list[dict]:
    """Parse gerrit_instances JSON from config."""
    if not settings.gerrit_instances:
        return []
    try:
        data = json.loads(settings.gerrit_instances)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Virtual-row shape helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _virtual_account_row(
    *,
    entry_id: str,
    platform: str,
    instance_url: str = "",
    token: str = "",
    ssh_key: str = "",
    ssh_host: str = "",
    ssh_port: int = 0,
    project: str = "",
    webhook_secret: str = "",
    label: str = "",
    username: str = "",
    url_patterns: Optional[list[str]] = None,
    is_default: bool = False,
    enabled: bool = True,
    metadata: Optional[dict] = None,
    tenant_id: str = "t-default",
) -> dict:
    """Shape a legacy ``Settings``-sourced entry as a virtual ``git_accounts`` row.

    The output shape mirrors what :func:`_row_to_dict` returns for a real
    ``git_accounts`` row — same keys, same types — so downstream callers
    cannot tell the two apart. Two extra plaintext compat fields (``token``
    / ``ssh_key`` / ``webhook_secret`` / ``url``) live alongside the
    canonical ``encrypted_*`` columns for sync callers that still reach
    into the dict directly; the async path populates both sides too so
    there's a single contract.
    """
    now = time.time()
    return {
        "id": entry_id,
        "tenant_id": tenant_id,
        "platform": platform,
        # Legacy compat: many sync callers read ``url``. Keep it aliased
        # to ``instance_url`` so the field is present under both names.
        "url": instance_url,
        "instance_url": instance_url,
        "label": label,
        "username": username,
        "token": token,
        "ssh_key": ssh_key,
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "project": project,
        "webhook_secret": webhook_secret,
        # Canonical encrypted-at-rest fields: the shim never has real
        # ciphertext (Settings holds plaintext), so these stay empty
        # strings — the ``token`` / ``ssh_key`` / ``webhook_secret``
        # above carry the plaintext for legacy callers.
        "encrypted_token": "",
        "encrypted_ssh_key": "",
        "encrypted_webhook_secret": "",
        "url_patterns": list(url_patterns) if url_patterns else [],
        "auth_type": "pat",
        "is_default": is_default,
        "enabled": enabled,
        "metadata": dict(metadata) if metadata else {},
        "last_used_at": None,
        "created_at": now,
        "updated_at": now,
        "version": 0,
    }


def _row_to_dict(row: Any) -> dict:
    """Normalise an ``asyncpg.Record`` from ``git_accounts`` into the
    same shape :func:`_virtual_account_row` produces.

    Decrypts ``encrypted_token`` / ``encrypted_ssh_key`` /
    ``encrypted_webhook_secret`` via :mod:`backend.secret_store` and
    exposes the plaintext under the legacy keys ``token`` / ``ssh_key``
    / ``webhook_secret``. Decryption errors fall back to empty strings
    (logged) rather than raising, so a single bad row can't take down
    the whole registry read.
    """
    from backend.secret_store import decrypt

    def _safe_decrypt(ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return decrypt(ciphertext)
        except Exception as exc:
            logger.warning(
                "git_accounts row %s: decrypt failed (%s) — "
                "treating secret as empty",
                row["id"] if "id" in row else "?",
                type(exc).__name__,
            )
            return ""

    # ``url_patterns`` / ``metadata`` are JSONB on PG (arrive as list/
    # dict) but TEXT-of-JSON on SQLite — parse defensively.
    url_patterns_raw = row["url_patterns"]
    if isinstance(url_patterns_raw, str):
        try:
            url_patterns = json.loads(url_patterns_raw) or []
        except (json.JSONDecodeError, TypeError):
            url_patterns = []
    elif isinstance(url_patterns_raw, list):
        url_patterns = url_patterns_raw
    else:
        url_patterns = []

    metadata_raw = row["metadata"]
    if isinstance(metadata_raw, str):
        try:
            metadata = json.loads(metadata_raw) or {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    elif isinstance(metadata_raw, dict):
        metadata = metadata_raw
    else:
        metadata = {}

    plain_token = _safe_decrypt(row["encrypted_token"] or "")
    plain_ssh_key = _safe_decrypt(row["encrypted_ssh_key"] or "")
    plain_whs = _safe_decrypt(row["encrypted_webhook_secret"] or "")

    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "platform": row["platform"],
        "url": row["instance_url"] or "",
        "instance_url": row["instance_url"] or "",
        "label": row["label"] or "",
        "username": row["username"] or "",
        "token": plain_token,
        "ssh_key": plain_ssh_key,
        "ssh_host": row["ssh_host"] or "",
        "ssh_port": int(row["ssh_port"] or 0),
        "project": row["project"] or "",
        "webhook_secret": plain_whs,
        "encrypted_token": row["encrypted_token"] or "",
        "encrypted_ssh_key": row["encrypted_ssh_key"] or "",
        "encrypted_webhook_secret": row["encrypted_webhook_secret"] or "",
        "url_patterns": list(url_patterns) if isinstance(url_patterns, list) else [],
        "auth_type": row["auth_type"] or "pat",
        "is_default": bool(row["is_default"]),
        "enabled": bool(row["enabled"]),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": int(row["version"] or 0),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Legacy sync registry (backward-compat shim)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def get_credential_registry() -> list[dict]:
    """Return the legacy-Settings-sourced registry, cached per-process.

    **Phase 5-2 note**: this sync function remains for backward
    compatibility with call sites that have not yet been ported to
    the async pool-backed API (rows 5-6/7/8). New code should prefer
    :func:`get_credential_registry_async` which reads the
    ``git_accounts`` table and respects tenant scope. The dict shape
    returned here matches the async path's output (virtual
    ``git_accounts`` rows), so callers can swap between them without
    renaming keys.

    Each worker rebuilds its own cache from the same ``Settings``
    source, so cross-worker consistency is by construction — SOP
    Step 1 qualified answer #1. Lock prevents two concurrent first-
    callers from racing on ``_CREDENTIALS_CACHE`` assignment.
    """
    global _CREDENTIALS_CACHE
    # Fast-path read without lock — dict-pointer assignment is atomic in CPython.
    cached = _CREDENTIALS_CACHE
    if cached is not None:
        return list(cached)

    with _CACHE_LOCK:
        # Double-check inside lock
        if _CREDENTIALS_CACHE is not None:
            return list(_CREDENTIALS_CACHE)
        registry = _build_registry()
        _CREDENTIALS_CACHE = registry
        logger.info("Credential registry (legacy shim): %d entries", len(registry))
        return list(registry)


def _build_registry() -> list[dict]:
    """Backward-compat shim — synthesise virtual ``git_accounts`` rows
    from legacy ``Settings`` JSON maps / scalars.

    Emits a one-shot deprecation warning per process; the intent is
    that every ``uvicorn --workers N`` replica logs exactly one line
    if anything still reaches this code path, so ops can tell at a
    glance how much legacy traffic remains during the Phase-5 rollout.

    Removed after rows 5-6/7/8 port every sync call site to the
    async pool-backed API and row 5-5's auto-migration moves the
    legacy ``Settings`` payload into ``git_accounts``.
    """
    global _LEGACY_WARN_EMITTED
    if not _LEGACY_WARN_EMITTED:
        logger.warning(
            "git_credentials._build_registry: reading legacy Settings "
            "credentials (github_token / gitlab_token / gerrit_instances "
            "/ git_ssh_key_map / github_token_map / gitlab_token_map / "
            "git_credentials.yaml). This is the Phase 5-2 backward-compat "
            "shim — migrate to the `git_accounts` table via "
            "`pick_account_for_url` / `pick_default` / `pick_by_id`. "
            "See docs/phase-5-multi-account/01-design.md."
        )
        _LEGACY_WARN_EMITTED = True

    registry: list[dict] = []

    # 1. Load from YAML file
    yaml_creds = _load_yaml_credentials()
    for entry in yaml_creds:
        registry.append(_virtual_account_row(
            entry_id=entry.get("id", "") or f"yaml-{len(registry)}",
            platform=entry.get("platform", "unknown"),
            instance_url=entry.get("url", ""),
            token=entry.get("token", ""),
            ssh_key=entry.get("ssh_key", ""),
            ssh_host=entry.get("ssh_host", ""),
            ssh_port=int(entry.get("ssh_port", 22) or 22),
            project=entry.get("project", ""),
            webhook_secret=entry.get("webhook_secret", ""),
            label=entry.get("label", "") or entry.get("id", ""),
        ))

    # 2. Build entries from JSON maps (env var overrides)
    ssh_map = _load_json_map(settings.git_ssh_key_map)
    gh_map = _load_json_map(settings.github_token_map)
    gl_map = _load_json_map(settings.gitlab_token_map)

    # GitHub token map entries
    for host, token in gh_map.items():
        if not any(host in (r.get("instance_url", "") or "") for r in registry):
            registry.append(_virtual_account_row(
                entry_id=f"github-{host.replace('.', '-')}",
                platform="github",
                instance_url=f"https://{host}",
                token=token,
                ssh_key=ssh_map.get(host, ""),
                label=f"{host} (legacy map)",
            ))

    # GitLab token map entries
    for host, token in gl_map.items():
        if not any(host in (r.get("instance_url", "") or "") for r in registry):
            registry.append(_virtual_account_row(
                entry_id=f"gitlab-{host.replace('.', '-')}",
                platform="gitlab",
                instance_url=f"https://{host}",
                token=token,
                ssh_key=ssh_map.get(host, ""),
                label=f"{host} (legacy map)",
            ))

    # Gerrit instances from JSON
    for inst in _load_gerrit_instances():
        inst_id = inst.get("id", f"gerrit-{inst.get('ssh_host', 'unknown')}")
        if not any(r.get("id") == inst_id for r in registry):
            registry.append(_virtual_account_row(
                entry_id=inst_id,
                platform="gerrit",
                instance_url=inst.get("url", ""),
                ssh_host=inst.get("ssh_host", ""),
                ssh_port=int(inst.get("ssh_port", 29418) or 29418),
                project=inst.get("project", ""),
                webhook_secret=inst.get("webhook_secret", ""),
                ssh_key=ssh_map.get(inst.get("ssh_host", ""), ""),
                label=inst.get("label", "") or inst_id,
            ))

    # 3. Build fallback entries from scalar config (backward compat)
    if settings.github_token and not any(r["platform"] == "github" for r in registry):
        registry.append(_virtual_account_row(
            entry_id="default-github",
            platform="github",
            instance_url="https://github.com",
            token=settings.github_token,
            ssh_key=settings.git_ssh_key_path,
            label="default-github (legacy scalar)",
            is_default=True,
        ))

    if settings.gitlab_token and not any(r["platform"] == "gitlab" for r in registry):
        registry.append(_virtual_account_row(
            entry_id="default-gitlab",
            platform="gitlab",
            instance_url=settings.gitlab_url or "https://gitlab.com",
            token=settings.gitlab_token,
            ssh_key=settings.git_ssh_key_path,
            label="default-gitlab (legacy scalar)",
            is_default=True,
        ))

    if (
        settings.gerrit_enabled
        and settings.gerrit_ssh_host
        and not any(r["platform"] == "gerrit" for r in registry)
    ):
        registry.append(_virtual_account_row(
            entry_id="default-gerrit",
            platform="gerrit",
            instance_url=settings.gerrit_url,
            ssh_host=settings.gerrit_ssh_host,
            ssh_port=int(settings.gerrit_ssh_port or 29418),
            project=settings.gerrit_project,
            webhook_secret=settings.gerrit_webhook_secret,
            ssh_key=settings.git_ssh_key_path,
            label="default-gerrit (legacy scalar)",
            is_default=True,
        ))

    return registry


def clear_credential_cache() -> None:
    """Clear the cached shim registry (call after settings change).

    Does NOT reset the deprecation-warn flag — the warn is meant to
    fire once per process, and ``PUT /runtime/settings`` / tests
    invalidating the cache shouldn't re-spam the log line.
    """
    global _CREDENTIALS_CACHE
    with _CACHE_LOCK:
        _CREDENTIALS_CACHE = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  URL → host extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_host(url: str) -> str:
    """Extract a lower-cased hostname from a git URL."""
    if not url:
        return ""
    url_lower = url.lower()
    if url_lower.startswith("git@"):
        return url_lower.split("@", 1)[1].split(":")[0]
    if url_lower.startswith("ssh://"):
        return urlparse(url_lower).hostname or ""
    if "://" in url_lower:
        return urlparse(url_lower).hostname or ""
    # Bare host form (e.g. ``github.com``) — return as-is lowercased.
    return url_lower.rstrip("/")


def find_credential_for_url(url: str) -> dict | None:
    """Find the best matching credential entry for a git URL (SYNC).

    Reads from the legacy shim registry — preserved for sync callers
    pending rows 5-6/7/8 call-site sweep. New async callers should use
    :func:`pick_account_for_url`.
    """
    host = _extract_host(url)
    if not host:
        return None

    registry = get_credential_registry()

    # Exact host match
    for entry in registry:
        entry_host = _extract_host(entry.get("instance_url", "") or entry.get("url", ""))
        ssh_host = (entry.get("ssh_host") or "").lower()
        if host == entry_host or host == ssh_host:
            return entry

    # Partial match (e.g., "gitlab" in host matches a gitlab entry)
    for entry in registry:
        entry_url = (entry.get("instance_url") or entry.get("url") or "").lower()
        if host in entry_url or entry_url.rstrip("/").endswith(host):
            return entry

    return None


def get_token_for_url(url: str) -> str:
    """Get the authentication token for a URL from the registry (SYNC).

    Phase 5-6 (#multi-account-forge): the legacy scalar fallback is
    gone — :func:`_build_registry` already synthesises ``default-
    github`` / ``default-gitlab`` virtual rows from
    ``settings.github_token`` / ``settings.gitlab_token``, so a
    platform-agnostic duplicate fallback here only hid the real
    "no credential for this host" signal (and, worse, silently
    leaked the github.com token to a ghe.mycompany.com clone).
    Returning ``""`` when :func:`find_credential_for_url` can't match
    surfaces the missing-credential state to callers cleanly.
    """
    cred = find_credential_for_url(url)
    if cred and cred.get("token"):
        return cred["token"]
    return ""


def get_ssh_key_for_url(url: str) -> str:
    """Get the SSH key path for a URL from the registry (SYNC).

    Falls back to scalar config if no registry match.
    """
    cred = find_credential_for_url(url)
    if cred and cred.get("ssh_key"):
        return cred["ssh_key"]
    return settings.git_ssh_key_path


def get_webhook_secret_for_host(host: str, platform: str = "") -> str:
    """Get the webhook secret for a specific host (SYNC).

    H5 fix: exact host equality only. The previous ``host.lower() in (...)``
    check used set-membership which was correct, but the broader registry
    matching elsewhere used substring; lock both to exact match here so
    ``github.com`` cannot match ``github.company.com`` and route the wrong
    secret.
    """
    if not host:
        return ""
    needle = host.strip().lower()
    registry = get_credential_registry()
    for entry in registry:
        entry_host = (entry.get("ssh_host") or "").strip().lower()
        entry_url_host = ""
        entry_url = entry.get("instance_url") or entry.get("url") or ""
        if entry_url:
            entry_url_host = (urlparse(entry_url).hostname or "").lower()
        if needle == entry_host or needle == entry_url_host:
            secret = entry.get("webhook_secret", "")
            if secret:
                return secret

    # Fallback to scalar secrets
    if platform == "gerrit":
        return settings.gerrit_webhook_secret
    if platform == "github":
        return settings.github_webhook_secret
    if platform == "gitlab":
        return settings.gitlab_webhook_secret
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async pool-backed API (Phase 5-2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _resolve_tenant(tenant_id: str | None) -> str:
    """Resolve effective tenant id: explicit → contextvar → ``t-default``."""
    if tenant_id:
        return tenant_id
    ctx = current_tenant_id()
    if ctx:
        return ctx
    return "t-default"


async def _fetch_git_accounts_rows(
    tenant_id: str,
    *,
    enabled_only: bool = True,
) -> list[Any]:
    """Pool-backed read of ``git_accounts`` rows for ``tenant_id``.

    Returns an empty list (NOT the legacy shim) if the PG pool is not
    initialised — it's the caller's job to decide whether to fall back.
    ``enabled_only`` filters out rows the operator disabled without
    deleting (useful for CRUD list views; internal resolvers always
    want only enabled rows).

    Ordered so that default rows come first, then most-recently-used
    first (touch-on-resolve fills in ``last_used_at`` in row 5-3).
    """
    try:
        from backend.db_pool import get_pool
        pool = get_pool()
    except RuntimeError:
        # Pool not initialised — dev / unit-test path without PG.
        return []

    where = ["tenant_id = $1"]
    params: list[Any] = [tenant_id]
    if enabled_only:
        where.append("enabled = TRUE")

    sql = (
        f"SELECT {_GIT_ACCOUNTS_COLS} FROM git_accounts "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY is_default DESC, last_used_at DESC NULLS LAST, "
        "platform, id"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return list(rows)


async def get_credential_registry_async(
    tenant_id: str | None = None,
    *,
    enabled_only: bool = True,
) -> list[dict]:
    """Return the canonical credential registry for a tenant.

    Reads the ``git_accounts`` table via the asyncpg pool. If the pool
    isn't initialised OR the table has no rows for this tenant yet,
    falls back to the legacy shim :func:`_build_registry` — preserving
    current behaviour until row 5-5's auto-migration lands. The dict
    shape is identical either way (``_virtual_account_row`` mirrors
    :func:`_row_to_dict`), so callers can't observe which path they
    got.

    Tenant scope: explicit ``tenant_id`` overrides the contextvar;
    otherwise uses ``db_context.current_tenant_id()`` with a
    ``t-default`` fallback.
    """
    tid = _resolve_tenant(tenant_id)
    rows = await _fetch_git_accounts_rows(tid, enabled_only=enabled_only)
    if rows:
        return [_row_to_dict(r) for r in rows]
    # Empty table → fall back to the legacy shim. Once row 5-5 runs,
    # this branch stops firing for migrated tenants.
    return _build_registry()


class MissingCredentialError(LookupError):
    """No credential could be resolved for the requested URL.

    Raised by :func:`require_account_for_url` when neither a
    ``url_patterns`` glob match nor a platform default is found for
    the given URL. Distinct from a generic ``LookupError`` so call
    sites can catch this specifically (e.g. surface a "configure a
    GitHub account first" toast in the UI) without swallowing
    unrelated key errors.
    """


def _normalize_url_for_pattern_match(url: str) -> str:
    """Strip scheme + ``git@`` form so a single glob pattern matches
    both HTTPS and SSH URLs for the same repo.

    Examples
    --------
    >>> _normalize_url_for_pattern_match("https://github.com/acme/app")
    'github.com/acme/app'
    >>> _normalize_url_for_pattern_match("git@github.com:acme/app.git")
    'github.com/acme/app.git'
    >>> _normalize_url_for_pattern_match("ssh://git@github.com/acme/app")
    'github.com/acme/app'

    Lowercased so case-insensitive comparison against
    lowercased patterns Just Works (URLs and forge org/repo names
    are conventionally case-insensitive on the wire even when
    GitHub displays mixed case).
    """
    s = url.lower().lstrip()
    if s.startswith("git@"):
        after_at = s.split("@", 1)[1]
        if ":" in after_at:
            h, _, rest = after_at.partition(":")
            return f"{h}/{rest}"
        return after_at
    if "://" in s:
        _, _, tail = s.partition("://")
        # ``ssh://git@github.com/...`` — strip embedded user@ if present.
        if "@" in tail and "/" in tail and tail.index("@") < tail.index("/"):
            tail = tail.split("@", 1)[1]
        return tail
    return s


def _matches_pattern(scheme_stripped_url: str, pattern: str) -> bool:
    """Glob match, anchored to the full normalised URL.

    :mod:`fnmatch` is anchored by construction (``fnmatch.fnmatch``
    requires the entire string to match the pattern, not a substring).
    Pattern is lowercased for case-insensitive comparison; everything
    that isn't ``*`` / ``?`` / ``[seq]`` / ``[!seq]`` is a literal
    character — including dots, dashes, underscores, slashes — which
    matches operator intuition for glob patterns like
    ``github.com/acme-corp/*``.

    Returns False on any non-string / empty input rather than raising
    so a malformed ``url_patterns`` entry doesn't break the resolver
    for the rest of the registry.
    """
    import fnmatch
    if not isinstance(pattern, str) or not pattern:
        return False
    if not scheme_stripped_url:
        return False
    return fnmatch.fnmatch(scheme_stripped_url, pattern.lower())


async def _touch_last_used_at(
    account_id: str | None,
    tenant_id: str,
) -> None:
    """Best-effort UPDATE of ``git_accounts.last_used_at`` for the
    just-resolved account.

    Why best-effort
    ───────────────
    The resolver should never fail a clone / push / webhook verify
    just because the LRU bookkeeping write hit a transient PG hiccup.
    Three silent-skip cases:

    1. ``account_id`` is empty / ``None`` (defensive — pick_* never
       returns a row without an id, but we don't want to crash if a
       future caller passes one).
    2. The pool isn't initialised yet (dev / unit-test path with no
       PG, or pre-lifespan startup).
    3. The UPDATE raises (network blip, statement_timeout, etc.) —
       we ``logger.debug`` it and move on; the resolve call still
       returns the row.

    A no-op UPDATE (zero rows affected, e.g. when a shim virtual
    row's id like ``default-github`` doesn't exist in the real
    ``git_accounts`` table) is also fine — PG just reports 0 rows
    and we return. This is the expected behaviour during the Phase 5
    rollout ramp before row 5-5's auto-migration moves legacy
    Settings into the table.

    Module-global / read-after-write audit
    ──────────────────────────────────────
    No new module-globals introduced. The UPDATE is a single-statement
    auto-commit (no explicit transaction needed) so each call is
    atomic; concurrent touches against the same row are PG-serialised
    via the row lock and the LWW semantic is correct (most-recent
    timestamp wins, which matches LRU intent).
    """
    if not account_id:
        return
    try:
        from backend.db_pool import get_pool
        pool = get_pool()
    except RuntimeError:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE git_accounts SET last_used_at = $1 "
                "WHERE id = $2 AND tenant_id = $3",
                time.time(), account_id, tenant_id,
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug(
            "git_credentials._touch_last_used_at(%s/%s): %s — "
            "best-effort, ignoring",
            tenant_id, account_id, type(exc).__name__,
        )


async def pick_account_for_url(
    url: str,
    *,
    tenant_id: str | None = None,
    touch: bool = True,
) -> dict | None:
    """Resolve the best ``git_accounts`` row for *url* (async).

    Strategy
    ────────
    1. Glob-match each row's ``url_patterns`` list via :mod:`fnmatch`
       against the scheme-stripped lowercased URL (so
       ``github.com/acme-corp/*`` matches both
       ``https://github.com/acme-corp/x`` and
       ``git@github.com:acme-corp/x``). First match wins; rows are
       SELECTed with ``ORDER BY is_default DESC, last_used_at DESC
       NULLS LAST, platform, id`` so multi-match resolves
       deterministically — the default row beats non-defaults, and
       among non-defaults the most-recently-used wins.
    2. Exact host match against ``instance_url`` / ``ssh_host``.
    3. Substring host match (legacy fallback for shim rows whose
       ``url_patterns`` is empty).
    4. Platform default via :func:`pick_default` (e.g. any
       ``github.com/*`` URL without a specific pattern match gets
       the platform's ``is_default=TRUE`` account).

    Returns ``None`` when nothing matches. Use
    :func:`require_account_for_url` instead at call sites that
    cannot proceed without a credential — it raises
    :class:`MissingCredentialError`.

    On a successful resolve, ``last_used_at`` is best-effort
    updated for the matched ``git_accounts`` row (no-op if the
    pool isn't up or the id doesn't correspond to a real row).
    Pass ``touch=False`` to suppress the LRU update — useful for
    debug / introspection endpoints that shouldn't disturb the
    LRU ordering.
    """
    if not url:
        return None
    registry = await get_credential_registry_async(tenant_id)
    if not registry:
        return None

    host = _extract_host(url)
    scheme_stripped = _normalize_url_for_pattern_match(url)
    tid_for_touch = _resolve_tenant(tenant_id)

    # 1. url_patterns match (glob via fnmatch, anchored)
    for entry in registry:
        patterns = entry.get("url_patterns") or []
        if not isinstance(patterns, list):
            continue
        for pat in patterns:
            if _matches_pattern(scheme_stripped, pat):
                if touch:
                    await _touch_last_used_at(entry.get("id"), tid_for_touch)
                return entry

    if not host:
        return None

    # 2. Exact host match
    for entry in registry:
        entry_host = _extract_host(
            entry.get("instance_url", "") or entry.get("url", "")
        )
        ssh_host = (entry.get("ssh_host") or "").lower()
        if host == entry_host or host == ssh_host:
            if touch:
                await _touch_last_used_at(entry.get("id"), tid_for_touch)
            return entry

    # 3. Substring match (legacy fallback)
    for entry in registry:
        entry_url = (entry.get("instance_url") or entry.get("url") or "").lower()
        if entry_url and (host in entry_url or entry_url.rstrip("/").endswith(host)):
            if touch:
                await _touch_last_used_at(entry.get("id"), tid_for_touch)
            return entry

    # 4. Platform default fallback (pick_default touches its own row).
    from backend.git_auth import detect_platform
    platform = detect_platform(url)
    if platform and platform != "unknown":
        return await pick_default(platform, tenant_id=tenant_id, touch=touch)
    return None


async def require_account_for_url(
    url: str,
    *,
    tenant_id: str | None = None,
    touch: bool = True,
) -> dict:
    """Same contract as :func:`pick_account_for_url` but raises
    :class:`MissingCredentialError` instead of returning ``None``
    when no row matches AND no platform default exists.

    Intended for call sites that cannot proceed without a credential
    (clone / fetch / push / webhook verify). The raised exception
    carries the requested URL in its message so operators reading
    logs can immediately tell which call was unsatisfied.
    """
    entry = await pick_account_for_url(
        url, tenant_id=tenant_id, touch=touch,
    )
    if entry is None:
        tid = _resolve_tenant(tenant_id)
        raise MissingCredentialError(
            f"No git_accounts row matches url={url!r} for tenant={tid!r} "
            "and no platform default is configured. Configure a "
            "matching account (with url_patterns) or mark a default "
            "account for this platform."
        )
    return entry


async def pick_default(
    platform: str,
    *,
    tenant_id: str | None = None,
    touch: bool = True,
) -> dict | None:
    """Return the default ``git_accounts`` row for *platform* (async).

    First preference: the row with ``is_default=TRUE`` (at most one
    per tenant+platform, enforced by the partial unique index). If no
    default is flagged, returns the first enabled row of that platform
    (LRU order) so a single-account deploy Just Works without the
    operator having to mark it default.

    Touches ``last_used_at`` on the returned row by default; pass
    ``touch=False`` to suppress (debug / introspection callers).
    """
    if not platform:
        return None
    registry = await get_credential_registry_async(tenant_id)
    matched: dict | None = None
    for entry in registry:
        if entry.get("platform") == platform and entry.get("is_default"):
            matched = entry
            break
    if matched is None:
        for entry in registry:
            if entry.get("platform") == platform:
                matched = entry
                break
    if matched is None:
        return None
    if touch:
        tid = _resolve_tenant(tenant_id)
        await _touch_last_used_at(matched.get("id"), tid)
    return matched


async def pick_by_id(
    account_id: str,
    *,
    tenant_id: str | None = None,
    touch: bool = True,
) -> dict | None:
    """Return the ``git_accounts`` row with primary key *account_id*
    (async), scoped to the current tenant.

    Returns None if the id doesn't exist in this tenant's scope, even
    if it exists in a different tenant's — that's the tenant-isolation
    guarantee (pool query ``WHERE tenant_id = $1 AND id = $2``).

    Touches ``last_used_at`` on the returned row by default; pass
    ``touch=False`` to suppress.
    """
    if not account_id:
        return None
    tid = _resolve_tenant(tenant_id)
    try:
        from backend.db_pool import get_pool
        pool = get_pool()
    except RuntimeError:
        # No pool → search the legacy shim registry for a matching id.
        # Don't touch (no real row to update).
        for entry in _build_registry():
            if entry.get("id") == account_id:
                return entry
        return None

    sql = (
        f"SELECT {_GIT_ACCOUNTS_COLS} FROM git_accounts "
        "WHERE tenant_id = $1 AND id = $2"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, tid, account_id)
    if row is None:
        return None
    out = _row_to_dict(row)
    if touch:
        await _touch_last_used_at(out.get("id"), tid)
    return out


def _reset_deprecation_warn_for_tests() -> None:
    """Reset the one-shot deprecation-warn flag.

    Intended for unit tests that want to assert the log line fires
    on a fresh process. Never call from production code — the flag
    exists to prevent log spam and the reset breaks that guarantee.
    """
    global _LEGACY_WARN_EMITTED
    _LEGACY_WARN_EMITTED = False
