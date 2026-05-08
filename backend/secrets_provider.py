"""OP-764 D3 — pluggable secret provider for OmniSight backend startup.

Single source-of-truth for secret resolution at process boot. The
backend used to read API keys, webhook secrets, and admin
passwords directly from ``.env`` (via pydantic-settings'
:mod:`dotenv` loader). That setup made every deploy a manual
``scp .env`` operation and stored plaintext secrets on disk.

This module replaces that with a small abstraction:

  * :class:`SecretsProvider` — abstract interface (``get`` / ``list_keys``).
  * :class:`EnvSecretsProvider` — legacy fallback; reads ``os.environ``.
  * :class:`FileSecretsProvider` — Fernet-encrypted JSON file at
    ``data/secrets.vault`` (reuses :mod:`backend.secret_store`'s
    machine-local Fernet key, so any host that already has the
    OmniSight key file can decrypt the vault).
  * :class:`HashiCorpVaultProvider` — thin shim over the ``hvac``
    SDK; reads ``secret/data/omnisight/<env>`` by default. The
    SDK is imported lazily so deployments that don't use Vault
    don't need the dependency installed.

The :data:`SECRET_FIELDS` registry is the authoritative list of
``Settings`` field names that must NEVER appear in ``config/*.yaml``.
:mod:`scripts.migrate_dotenv_to_vault` enforces the split and
:mod:`backend.config` reads from the provider only for keys in this
set; everything else stays on the env-var / yaml path.

Module-global state audit (per ``docs/sop/implement_phase_step.md``
Step 1, type-1 answer): the singleton :data:`_provider` is derived
once at process boot from ``OMNISIGHT_SECRETS_BACKEND`` — every
uvicorn worker resolves the same backend from the same env, so
cross-worker secret values match without coordination. Per-request
secret rotation is intentionally OUT OF SCOPE — operators rotate
secrets via the vault and bounce the process.
"""

from __future__ import annotations

import abc
import json
import logging
import os
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  SECRET_FIELDS — the registry the YAML / vault split is anchored on
# ─────────────────────────────────────────────────────────────────
#
# Every name here is a snake_case ``backend.config.Settings`` field.
# A field belongs in this set when leaking it would compromise an
# external system: API keys, OAuth client secrets, webhook HMAC
# secrets, bearer tokens, admin passwords, encrypted-store keys,
# SMTP passwords, the Cloudflare API token, the bootstrap admin
# password, etc.
#
# Anything NOT in this set is a non-secret config knob (feature
# flags, hostnames, timeouts, sandbox limits) and may live in
# ``config/*.yaml`` checked into the repo.
#
# Adding a field: when you introduce a new Settings field that
# carries a secret, add the snake_case name here. The migration
# script and the startup overlay both consult this set; missing
# entries silently leak the secret into ``config/*.yaml`` on the
# next migration run.

SECRET_FIELDS: frozenset[str] = frozenset({
    # LLM provider keys
    "anthropic_api_key",
    "google_api_key",
    "openai_api_key",
    "xai_api_key",
    "groq_api_key",
    "deepseek_api_key",
    "together_api_key",
    "openrouter_api_key",
    # Git forge tokens / SSH key path — not the path itself, but the
    # legacy single-token fallbacks count as secrets in transit.
    "github_token",
    "gitlab_token",
    "github_token_map",
    "gitlab_token_map",
    "git_ssh_key_map",
    # Webhook HMAC / bearer secrets
    "gerrit_webhook_secret",
    "github_webhook_secret",
    "gitlab_webhook_secret",
    "jira_webhook_secret",
    "email_webhook_secret",
    # JIRA notification token
    "notification_jira_token",
    # PagerDuty + SMTP password + SMS webhook
    "notification_pagerduty_key",
    "notification_email_smtp_password",
    "notification_sms_webhook",
    # ChatOps secrets
    "chatops_discord_public_key",
    "chatops_teams_secret",
    "chatops_line_channel_token",
    "chatops_line_channel_secret",
    # Stripe
    "stripe_secret_key",
    "stripe_webhook_secret",
    # CI / Jenkins
    "ci_jenkins_api_token",
    # Cloudflare tunnel + Access
    "cf_api_token",
    "cloudflare_tunnel_token",
    # Firecrawl SaaS
    "firecrawl_api_key",
    # Slack incoming webhook is technically a URL with a secret path
    "notification_slack_webhook",
    "chatops_discord_webhook",
    "chatops_teams_webhook",
    # Bootstrap admin + API bearer + metrics token
    "admin_password",
    "decision_bearer",
    "metrics_token",
    # OAuth client secrets (leave client IDs in YAML — they are public)
    "oauth_google_client_secret",
    "oauth_github_client_secret",
    "oauth_microsoft_client_secret",
    "oauth_apple_client_secret",
    "oauth_discord_client_secret",
    "oauth_gitlab_client_secret",
    "oauth_bitbucket_client_secret",
    "oauth_slack_client_secret",
    "oauth_notion_client_secret",
    "oauth_salesforce_client_secret",
    "oauth_hubspot_client_secret",
    "oauth_flow_signing_key",
})


def is_secret_field(name: str) -> bool:
    """Return True when ``name`` is a Settings field that must be
    sourced from the vault / env, never from ``config/*.yaml``.

    Case-sensitive on the snake_case form; callers that hold the
    upper-case ``OMNISIGHT_FOO_BAR`` env-var spelling should strip
    the prefix and lower() before consulting this helper.
    """
    return name in SECRET_FIELDS


# ─────────────────────────────────────────────────────────────────
#  Provider interface + implementations
# ─────────────────────────────────────────────────────────────────


class SecretsProviderError(RuntimeError):
    """Raised on a configuration error in the chosen backend (bad
    path, missing SDK, malformed vault payload, …)."""


class SecretsProvider(abc.ABC):
    """Abstract: minimal read-side surface used by ``backend.config``.

    Write side (``put``) is implemented by file + vault backends so
    :mod:`scripts.migrate_dotenv_to_vault` can populate them; the
    env backend treats ``put`` as a no-op (env vars are set by the
    operator's shell, not by the migration script).
    """

    @abc.abstractmethod
    def get(self, key: str) -> str | None:  # pragma: no cover - interface
        """Return the secret value for ``key`` (snake_case Settings
        field name) or ``None`` if not present. Implementations
        MUST NOT raise on a missing key — that path is "use the
        env / class default fallback"."""

    @abc.abstractmethod
    def list_keys(self) -> Iterable[str]:  # pragma: no cover - interface
        """Return every key the provider currently has a value for.
        Used by the startup overlay to bulk-populate env vars and by
        the migration script to verify a round-trip."""

    def put(self, key: str, value: str) -> None:  # pragma: no cover
        raise SecretsProviderError(
            f"{type(self).__name__} is read-only; use a writable "
            "backend (file / hvac) for the migration script.",
        )


class EnvSecretsProvider(SecretsProvider):
    """Legacy fallback — reads from the process environment.

    For each registered SECRET_FIELDS entry, looks up
    ``OMNISIGHT_<UPPER>`` in ``os.environ``. Empty values are
    returned as ``None`` so the overlay treats them as "not set"
    rather than overwriting a yaml/class default with empty string.
    """

    def get(self, key: str) -> str | None:
        env_name = f"OMNISIGHT_{key.upper()}"
        value = os.environ.get(env_name, "")
        return value or None

    def list_keys(self) -> Iterable[str]:
        return [k for k in SECRET_FIELDS if self.get(k) is not None]


class FileSecretsProvider(SecretsProvider):
    """Fernet-encrypted JSON vault on local disk.

    File format: a single ciphertext blob produced by
    :func:`backend.secret_store.encrypt`; the plaintext is a JSON
    object mapping snake_case Settings field names to string
    values. Keys not present in :data:`SECRET_FIELDS` are tolerated
    on read (forward-compat) but the migration script refuses to
    write them.

    The file path defaults to ``data/secrets.vault`` and the Fernet
    key is the same machine-local key used elsewhere in OmniSight
    (:mod:`backend.secret_store`). Operators rotating that key
    must re-encrypt the vault payload — same drill as the existing
    ``data/.secret_key`` rotation.
    """

    DEFAULT_PATH = Path("data") / "secrets.vault"

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else self.DEFAULT_PATH
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        from backend import secret_store
        try:
            ciphertext = self._path.read_text("utf-8").strip()
            if not ciphertext:
                self._cache = {}
                return self._cache
            plaintext = secret_store.decrypt(ciphertext)
            data = json.loads(plaintext)
        except Exception as exc:
            raise SecretsProviderError(
                f"Failed to decrypt {self._path}: {exc}. "
                "Either the vault is corrupt or the Fernet key has "
                "rotated since the vault was written. Restore from "
                "backup or rerun migrate_dotenv_to_vault.py.",
            ) from exc
        if not isinstance(data, dict):
            raise SecretsProviderError(
                f"{self._path} payload is not a JSON object",
            )
        self._cache = {str(k): str(v) for k, v in data.items()}
        return self._cache

    def get(self, key: str) -> str | None:
        value = self._load().get(key)
        return value or None

    def list_keys(self) -> Iterable[str]:
        return list(self._load().keys())

    def put(self, key: str, value: str) -> None:
        from backend import secret_store
        data = dict(self._load())
        if value:
            data[key] = value
        else:
            data.pop(key, None)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = secret_store.encrypt(json.dumps(data, sort_keys=True))
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(ciphertext, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(str(tmp), str(self._path))
        self._cache = data

    def reset_cache(self) -> None:
        """Drop the in-memory cache. For tests + post-rotation reload."""
        self._cache = None


class HashiCorpVaultProvider(SecretsProvider):
    """Thin shim over ``hvac``.

    Reads from ``<mount>/data/<path>`` (KV v2). The ``hvac`` SDK
    is imported lazily so deployments that pick a different backend
    don't have to install it. Connection knobs:

      * ``OMNISIGHT_VAULT_ADDR``   — Vault server URL.
      * ``OMNISIGHT_VAULT_TOKEN``  — auth token (or set via the
        regular ``VAULT_TOKEN`` env which ``hvac`` reads natively).
      * ``OMNISIGHT_VAULT_MOUNT``  — KV mount, defaults to ``secret``.
      * ``OMNISIGHT_VAULT_PATH``   — secret path (no ``data/``
        prefix; defaults to ``omnisight/<env>``).

    Failure modes are loud — a misconfigured Vault deploy refuses
    to boot rather than silently falling back to env vars.
    """

    def __init__(
        self,
        addr: str | None = None,
        token: str | None = None,
        mount: str = "secret",
        path: str = "omnisight",
    ) -> None:
        self._addr = addr or os.environ.get("OMNISIGHT_VAULT_ADDR", "")
        self._token = token or os.environ.get("OMNISIGHT_VAULT_TOKEN", "") \
            or os.environ.get("VAULT_TOKEN", "")
        self._mount = mount
        self._path = path
        self._cache: dict[str, str] | None = None
        self._client = None  # lazy

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import hvac  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SecretsProviderError(
                "hvac SDK not installed. Add ``hvac>=1.2`` to "
                "backend/requirements.txt or pick a different "
                "OMNISIGHT_SECRETS_BACKEND value.",
            ) from exc
        if not self._addr:
            raise SecretsProviderError(
                "OMNISIGHT_VAULT_ADDR is empty; set it to the Vault "
                "server URL.",
            )
        client = hvac.Client(url=self._addr, token=self._token)
        if not client.is_authenticated():
            raise SecretsProviderError(
                f"Vault at {self._addr} rejected the supplied token; "
                "check OMNISIGHT_VAULT_TOKEN / VAULT_TOKEN.",
            )
        self._client = client
        return client

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        client = self._get_client()
        try:
            response = client.secrets.kv.v2.read_secret_version(
                path=self._path,
                mount_point=self._mount,
            )
        except Exception as exc:
            raise SecretsProviderError(
                f"Vault read of {self._mount}/{self._path} failed: {exc}",
            ) from exc
        data = (response or {}).get("data", {}).get("data", {})
        if not isinstance(data, dict):
            raise SecretsProviderError(
                f"Vault payload at {self._mount}/{self._path} is not a dict",
            )
        self._cache = {str(k): str(v) for k, v in data.items()}
        return self._cache

    def get(self, key: str) -> str | None:
        return self._load().get(key) or None

    def list_keys(self) -> Iterable[str]:
        return list(self._load().keys())

    def put(self, key: str, value: str) -> None:
        client = self._get_client()
        # KV v2 patch — read-modify-write because the SDK doesn't
        # expose a single-key patch on every Vault version.
        data = dict(self._load())
        if value:
            data[key] = value
        else:
            data.pop(key, None)
        client.secrets.kv.v2.create_or_update_secret(
            path=self._path,
            secret=data,
            mount_point=self._mount,
        )
        self._cache = data


# ─────────────────────────────────────────────────────────────────
#  Factory + module singleton
# ─────────────────────────────────────────────────────────────────

_provider: SecretsProvider | None = None


def make_secrets_provider(backend: str | None = None) -> SecretsProvider:
    """Construct a provider per the ``OMNISIGHT_SECRETS_BACKEND`` env.

    Values:
      * ``env`` (default) — :class:`EnvSecretsProvider`. Legacy
        path; preserves pre-OP-764 behaviour for deploys that
        haven't migrated yet.
      * ``file`` — :class:`FileSecretsProvider` against
        ``data/secrets.vault`` (override path via
        ``OMNISIGHT_SECRETS_FILE``).
      * ``hvac`` — :class:`HashiCorpVaultProvider`.

    Tests pass an explicit ``backend`` argument to bypass the env;
    production callers leave it ``None`` and the resolved value is
    cached as a module singleton.
    """
    backend = (backend or os.environ.get("OMNISIGHT_SECRETS_BACKEND") or "env").strip().lower()
    if backend == "env":
        return EnvSecretsProvider()
    if backend == "file":
        path = os.environ.get("OMNISIGHT_SECRETS_FILE") or None
        return FileSecretsProvider(path=path)
    if backend == "hvac":
        return HashiCorpVaultProvider(
            mount=os.environ.get("OMNISIGHT_VAULT_MOUNT", "secret"),
            path=os.environ.get("OMNISIGHT_VAULT_PATH", "omnisight"),
        )
    raise SecretsProviderError(
        f"OMNISIGHT_SECRETS_BACKEND={backend!r} is invalid. "
        "Valid: env / file / hvac.",
    )


def get_secrets_provider() -> SecretsProvider:
    """Return the process-wide provider singleton, creating it on
    first access."""
    global _provider
    if _provider is None:
        _provider = make_secrets_provider()
    return _provider


def _reset_for_tests() -> None:
    """Drop the singleton — pytest fixtures call this between tests
    that flip ``OMNISIGHT_SECRETS_BACKEND``."""
    global _provider
    _provider = None
