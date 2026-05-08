#!/usr/bin/env python3
"""OP-764 D3 — one-shot ``.env`` → vault + ``config/<env>.yaml`` migrator.

Reads a legacy ``.env`` file, splits each ``OMNISIGHT_<KEY>=value``
line into "secret" or "non-secret" using
:data:`backend.secrets_provider.SECRET_FIELDS`, and writes:

  * Secrets → the configured secrets provider (file-vault or hvac).
  * Non-secrets → patched into ``config/<env>.yaml`` (preserving
    existing entries; refuses to overwrite a key whose value
    differs unless ``--force`` is set).

Idempotent: running twice with the same input does not create
duplicate vault rows or YAML keys. The script reports a tally at
exit and exits non-zero on any encountered error so it can be
chained from a deploy hook.

Usage::

    OMNISIGHT_SECRETS_BACKEND=file \\
    OMNISIGHT_SECRETS_FILE=data/secrets.vault \\
        python scripts/migrate_dotenv_to_vault.py \\
            --dotenv .env \\
            --env staging \\
            [--force] [--dry-run]

The script never deletes the source ``.env`` — operators verify
the round-trip first, then remove the file manually. That
deliberate safety belt is documented in
``docs/operations/secrets-management.md``.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("migrate_dotenv_to_vault")

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Imported after sys.path tweak so the script works when invoked
# from any cwd.
from backend.secrets_provider import (  # noqa: E402
    SECRET_FIELDS,
    SecretsProviderError,
    is_secret_field,
    make_secrets_provider,
)

ENV_PREFIX = "OMNISIGHT_"
_DOTENV_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$",
)


def _strip_quotes(raw: str) -> str:
    """Trim matching outer quotes from a ``.env`` value, the same
    way pydantic-settings' parser does."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Return a dict of all key=value entries in ``path`` (later
    entries override earlier duplicates, mimicking dotenv loaders)."""
    entries: dict[str, str] = {}
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _DOTENV_LINE_RE.match(stripped)
        if not m:
            logger.warning("skipping unparseable line: %s", stripped)
            continue
        key, raw_value = m.group(1), m.group(2)
        # Trailing inline comments — ``KEY=value # comment`` — are
        # stripped only for unquoted values, matching pydantic's
        # behaviour. Quoted values keep ``#`` as-is.
        if not (raw_value.startswith('"') or raw_value.startswith("'")):
            comment_at = raw_value.find(" #")
            if comment_at >= 0:
                raw_value = raw_value[:comment_at].rstrip()
        entries[key] = _strip_quotes(raw_value)
    return entries


def _strip_prefix(env_var: str) -> str | None:
    if not env_var.startswith(ENV_PREFIX):
        return None
    return env_var[len(ENV_PREFIX):].lower()


def _resolve_yaml_path(env_name: str) -> Path:
    aliases = {"production": "prod", "develop": "dev", "development": "dev"}
    canonical = aliases.get(env_name, env_name)
    return _REPO_ROOT / "config" / f"{canonical}.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    import yaml  # type: ignore[import-not-found]
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: top-level must be a mapping")
    return raw


def _dump_yaml(path: Path, data: dict) -> None:
    import yaml  # type: ignore[import-not-found]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Migrate a legacy .env into vault + config/<env>.yaml.",
    )
    p.add_argument("--dotenv", required=True, type=Path, help="Path to source .env file")
    p.add_argument("--env", required=True, choices=("dev", "staging", "prod", "production"),
                   help="Target environment (selects config/<env>.yaml)")
    p.add_argument("--force", action="store_true",
                   help="Overwrite YAML / vault values whose existing value differs")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        entries = _parse_dotenv(args.dotenv)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2

    yaml_path = _resolve_yaml_path(args.env)
    yaml_data = _load_yaml(yaml_path)

    try:
        provider = make_secrets_provider()
    except SecretsProviderError as exc:
        logger.error("secrets backend init failed: %s", exc)
        return 3

    secret_writes: dict[str, str] = {}
    yaml_writes: dict[str, str] = {}
    skipped_unprefixed: list[str] = []
    skipped_unknown: list[str] = []
    conflicts: list[str] = []

    # Lazily fetch Settings field names so the script doesn't pull
    # the full Settings instantiation cost (env overlay, validators)
    # — we only need the *declared* fields for the membership check.
    from backend.config import Settings as _Settings
    field_names = set(_Settings.model_fields)

    for env_var, value in entries.items():
        if not value:
            # Empty assignments mean "fall through to default" — no
            # need to write either side.
            continue
        bare = _strip_prefix(env_var)
        if bare is None:
            skipped_unprefixed.append(env_var)
            continue
        if bare not in field_names:
            skipped_unknown.append(env_var)
            continue
        if is_secret_field(bare):
            existing = provider.get(bare)
            if existing == value:
                continue
            if existing and not args.force:
                conflicts.append(f"vault[{bare}]: existing differs (use --force)")
                continue
            secret_writes[bare] = value
        else:
            existing = yaml_data.get(bare)
            if existing == value:
                continue
            if existing not in (None, "") and not args.force:
                conflicts.append(
                    f"yaml[{bare}]: existing={existing!r} new={value!r} "
                    "(use --force)",
                )
                continue
            yaml_writes[bare] = value

    if conflicts:
        for c in conflicts:
            logger.error("%s", c)
        logger.error("Refusing to migrate due to %d conflict(s).", len(conflicts))
        return 4

    if args.dry_run:
        logger.info("[dry-run] would write %d secret(s) to vault", len(secret_writes))
        for k in sorted(secret_writes):
            logger.info("[dry-run]   secret %s", k)
        logger.info(
            "[dry-run] would write %d non-secret(s) to %s",
            len(yaml_writes), yaml_path,
        )
        for k in sorted(yaml_writes):
            logger.info("[dry-run]   yaml   %s = %s", k, yaml_writes[k])
        if skipped_unprefixed:
            logger.info(
                "[dry-run] skipped %d non-OMNISIGHT_ env vars",
                len(skipped_unprefixed),
            )
        if skipped_unknown:
            logger.info(
                "[dry-run] skipped %d unknown OMNISIGHT_ vars (not declared on Settings)",
                len(skipped_unknown),
            )
        return 0

    for k, v in secret_writes.items():
        provider.put(k, v)
        logger.info("vault wrote %s", k)

    if yaml_writes:
        merged = dict(yaml_data)
        merged.update(yaml_writes)
        _dump_yaml(yaml_path, merged)
        logger.info("yaml updated: %s (%d new entries)", yaml_path, len(yaml_writes))

    logger.info(
        "Migration done — secrets=%d non-secrets=%d skipped_unprefixed=%d "
        "skipped_unknown=%d",
        len(secret_writes), len(yaml_writes),
        len(skipped_unprefixed), len(skipped_unknown),
    )

    # Sanity round-trip: reload the provider and confirm every
    # written secret is readable. Catches a misconfigured Fernet
    # key or a Vault permission error before the deploy proceeds.
    failed_roundtrip: list[str] = []
    for k, v in secret_writes.items():
        if provider.get(k) != v:
            failed_roundtrip.append(k)
    if failed_roundtrip:
        logger.error(
            "Round-trip verification failed for: %s",
            ", ".join(failed_roundtrip),
        )
        return 5

    # Hint to operator that SECRET_FIELDS is the canonical split
    # registry; running --dry-run with --verbose prints it for sanity.
    if args.verbose:
        logger.debug(
            "SECRET_FIELDS registry contains %d entries", len(SECRET_FIELDS),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
