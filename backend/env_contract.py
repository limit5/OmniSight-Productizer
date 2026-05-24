"""[OP-1643] Boreas-B A2 — dev/prod env-contract guard.

Fail-closed when a process's declared environment (``OMNISIGHT_ENV``) does not
match the environment its resolved Postgres DSN points at. This is the
*env-declaration* layer that complements A1's *DB-credential* layer (the
``omnisight_dev`` role is ``REVOKE``'d ``CONNECT`` on the prod ``omnisight`` DB):
A1 catches a dev process by its credential; A2 catches a dev process that
resolves a prod-looking DSN regardless of credential.

Design + codex review: ``docs/sprint-boreas/boreas-b-A2-env-contract-guard-design.md``.

This module is intentionally dependency-light (stdlib only) and must NOT import
``backend.config`` / ``backend.db_url`` — it is called *from* the connection
primitives (``db_pool.init_pool``, ``db._resolve_pg_dsn``,
``db_url.DatabaseURL.asyncpg_connect_kwargs``, ``alembic/env.py``), so importing
those back would create a cycle. It ships its own tolerant DSN parser.
"""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)

# Exit 78 = EX_CONFIG (sysexits.h) — signals a config error to orchestrators,
# matching backend.config.ConfigValidationError.
_EXIT_CONFIG = 78

# Known non-prod Postgres ports published on the co-tenanted host.
_DEV_PORTS = {58432}
_STAGING_PORTS = {55432, 55433}

# Canonical env aliases (mirrors backend.config._resolve_yaml_path + the
# startup validator, which only special-cased the literal "production").
_ENV_ALIASES = {
    "production": "prod",
    "prod": "prod",
    "develop": "dev",
    "development": "dev",
    "dev": "dev",
    "staging": "staging",
}


class EnvContractViolation(SystemExit):
    """Raised (exits 78) when env declaration and DB DSN disagree."""

    def __init__(self, message: str) -> None:
        super().__init__(_EXIT_CONFIG)
        self.message = message
        _logger.critical("EnvContractViolation: %s", message)


def canonical_env(raw: str | None) -> str | None:
    """Canonicalize ``OMNISIGHT_ENV`` → ``dev`` / ``staging`` / ``prod`` / None.

    Unknown non-empty values are returned lower-cased as-is (so an unexpected
    marker is treated as its own class and will fail the match, fail-closed).
    """
    if raw is None:
        return None
    v = raw.strip().lower()
    if not v:
        return None
    return _ENV_ALIASES.get(v, v)


def _split_dsn(dsn: str) -> tuple[str | None, str | None, int | None]:
    """Tolerant parse → (user, db-name, port).

    Avoids ``urllib.parse`` pitfalls with passwords containing ``/`` or ``@``:
    the userinfo is everything up to the LAST ``@`` (libpq convention), and the
    db-name is taken only from the path AFTER userinfo is stripped, so a ``/``
    inside the password cannot be mistaken for the path separator.
    """
    from urllib.parse import unquote

    rest = dsn.split("://", 1)[1] if "://" in dsn else dsn
    at = rest.rfind("@")
    userinfo = rest[:at] if at >= 0 else ""
    authority = rest[at + 1:] if at >= 0 else rest

    user = None
    if userinfo:
        user = unquote(userinfo.split(":", 1)[0]) or None

    hostport, _, pathq = authority.partition("/")
    db = pathq.split("?", 1)[0].strip("/")
    db = unquote(db) if db else None

    port: int | None = None
    if ":" in hostport:
        tail = hostport.rsplit(":", 1)[1]
        if tail.isdigit():
            port = int(tail)
    return user, db, port


def classify_dsn(dsn: str | None) -> str:
    """Classify a DSN → ``"prod"`` / ``"dev"`` / ``"staging"`` / ``"sqlite"`` /
    ``"skip"`` / ``"unknown"``.

    - ``skip``    — empty, or a non-Postgres non-SQLite scheme (not our concern).
    - ``sqlite``  — a SQLite URL (legacy / tests).
    - ``unknown`` — a Postgres URL we cannot positively place (fail-closed: the
      caller hard-fails on this, so a typo / renamed-prod / url-encoding bug
      can't fail-open).
    """
    if not dsn or not dsn.strip():
        return "skip"
    s = dsn.strip()
    scheme = s.split("://", 1)[0].lower() if "://" in s else s.lower()
    if scheme.startswith("sqlite"):
        return "sqlite"
    if not scheme.startswith(("postgresql", "postgres", "asyncpg")):
        return "skip"  # mysql/etc — not the omnisight PG we guard

    user, db, port = _split_dsn(s)
    db_l = (db or "").lower()
    user_l = (user or "").lower()

    if db_l.endswith(("_dev", "-dev")) or user_l == "omnisight_dev" or (port in _DEV_PORTS):
        return "dev"
    if db_l.endswith(("_staging", "-staging")) or (port in _STAGING_PORTS):
        return "staging"
    if db_l == "omnisight" and user_l == "omnisight":
        return "prod"
    return "unknown"


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def enforce_env_db_contract(
    dsn: str | None,
    *,
    source: str,
    env: str | None = None,
) -> None:
    """Fail closed (exit 78) when ``OMNISIGHT_ENV`` disagrees with ``dsn``.

    ``source`` is a short label for the call site (for the error/log message).
    ``env`` overrides ``OMNISIGHT_ENV`` (used by tests). See the design doc for
    the full truth table; summary:

    ===============  ==========  =======
    OMNISIGHT_ENV    DSN class   verdict
    ===============  ==========  =======
    dev              dev          OK
    dev              prod/stg     FAIL  (core protection)
    prod             prod         OK
    prod             dev/stg      FAIL  (symmetric)
    unset            prod         OK    (legacy prod-by-default: runners/coord)
    unset            dev/stg      FAIL  (non-prod DB without declaring non-prod)
    any              unknown-pg   FAIL  (fail-closed on unrecognised PG)
    any              sqlite/skip  OK    (legacy/tests/non-PG)
    ===============  ==========  =======
    """
    cls = classify_dsn(dsn)
    if cls in ("sqlite", "skip"):
        return

    canon_env = canonical_env(env if env is not None else os.environ.get("OMNISIGHT_ENV"))
    redacted = _redact(dsn or "")

    # Break-glass: refused in prod (env OR DSN) so it can never silence a real
    # prod mismatch; valid prod states (unset+prod / prod+prod) never need it.
    if _truthy(os.environ.get("OMNISIGHT_ENV_CONTRACT_DISABLE")):
        if canon_env == "prod" or cls == "prod":
            raise EnvContractViolation(
                f"OMNISIGHT_ENV_CONTRACT_DISABLE=1 is refused when env or DSN is prod "
                f"(env={canon_env!r}, dsn-class={cls!r}, source={source}, dsn={redacted})."
            )
        _logger.warning(
            "env-contract DISABLED via OMNISIGHT_ENV_CONTRACT_DISABLE=1 "
            "(env=%r, dsn-class=%r, source=%s) — break-glass, non-prod only.",
            canon_env, cls, source,
        )
        return

    # Test/CI carveout: tests call init_pool/connect directly with assorted
    # DSNs. Bypass ONLY when not pointed at prod — a prod DSN inside a test is
    # still a real violation worth surfacing.
    #
    # Tightened (OP-1698, finding #17): the in-test carveout must NOT silently
    # un-enforce the dev<->staging contract. A 'dev' process resolving a staging
    # DSN (or a 'staging' process resolving a dev DSN) is genuine cross-
    # contamination, not a test artifact, so it stays enforced even under
    # PYTEST_CURRENT_TEST/OMNISIGHT_CI_MODE. Every other non-prod case — notably
    # env-unset test-DB setup that connects to a dev/staging DB, or env==DSN —
    # remains exempt so the suite's own fixtures and direct init_pool/connect
    # calls aren't poisoned.
    in_test = bool(os.environ.get("PYTEST_CURRENT_TEST")) or _truthy(os.environ.get("OMNISIGHT_CI_MODE"))
    if in_test and cls != "prod":
        _dev_staging = {"dev", "staging"}
        dev_staging_mismatch = (
            canon_env in _dev_staging and cls in _dev_staging and cls != canon_env
        )
        if not dev_staging_mismatch:
            return
        # else: fall through to the real enforcement below, which raises the
        # env-contract violation with the canonical message.

    # Unrecognised Postgres → hard fail (never fail-open).
    if cls == "unknown":
        raise EnvContractViolation(
            f"DSN resolves to an unrecognised Postgres database — refusing to connect "
            f"(env={canon_env!r}, source={source}, dsn={redacted}). Expected a db/user "
            f"matching dev (omnisight_dev), staging (*_staging) or prod (omnisight)."
        )

    # cls is one of dev/staging/prod from here.
    expected = "prod" if canon_env is None else canon_env
    if cls != expected:
        env_desc = "unset (legacy prod-by-default)" if canon_env is None else repr(canon_env)
        raise EnvContractViolation(
            f"env-contract violation: OMNISIGHT_ENV={env_desc} but the DSN looks like "
            f"'{cls}' (source={source}, dsn={redacted}). A '{expected}' process must use a "
            f"'{expected}' database. Set OMNISIGHT_ENV to match, or point at the correct DB."
        )


def _redact(dsn: str) -> str:
    """Mask the password in a DSN for safe logging."""
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    at = rest.rfind("@")
    userinfo, tail = rest[:at], rest[at + 1:]
    if ":" in userinfo:
        userinfo = userinfo.split(":", 1)[0] + ":***"
    return f"{scheme}://{userinfo}@{tail}"
