"""W14.3 — Dynamic Cloudflare Tunnel ingress rules for web-preview sidecars.

Owns the lifecycle of per-sandbox ingress rules inside the operator's
existing CF Tunnel (provisioned by B12). For every ``WebSandboxManager.
launch()`` it adds a rule routing
``preview-{sandbox_id}.{tunnel_host}`` → ``http://127.0.0.1:{host_port}/``
to the tunnel's configuration; for every ``stop()`` it removes that rule.

Why a separate module (sibling to :mod:`backend.cloudflare_client`)
==================================================================

* :mod:`backend.cloudflare_client` (B12) is a *general* CF API v4 client
  consumed by the one-click tunnel-provisioning wizard. Its ``put_tunnel
  _config`` method takes a *fully-authored* ingress list and PUTs it
  wholesale — the wizard doesn't think about per-sandbox rules.
* W14.3 needs a *targeted* delta — fetch the live config, splice in (or
  out) one rule, PUT it back. That delta logic + a thread-safe local
  cache + sandbox-id-keyed bookkeeping does not belong inside the B12
  wizard's API surface.
* W14.3 also runs synchronously from inside :class:`backend.web_sandbox.
  WebSandboxManager.launch()`, which is itself a sync method called from
  a FastAPI ``async def`` route. The B12 client uses ``httpx.AsyncClient``;
  reusing it here would force ``asyncio.run_coroutine_threadsafe`` from
  a sync context — clumsy and brittle. We use ``httpx.Client`` (sync) in
  :class:`HttpxCFIngressClient` and keep the B12 async wizard untouched.

Row boundary
============

W14.3 owns:

  1. :class:`CFIngressManager` — thread-safe registry of live ingress
     rules keyed on ``sandbox_id``, with idempotent ``create`` /
     ``delete`` methods.
  2. :class:`CFIngressClient` Protocol + :class:`HttpxCFIngressClient`
     production impl + the sync httpx GET/PUT wrappers.
  3. Pure helpers (:func:`build_ingress_hostname`,
     :func:`build_ingress_service_url`, :func:`compute_ingress_rules_add`,
     :func:`compute_ingress_rules_remove`) — composable, testable in
     isolation, deterministic.
  4. :class:`CFIngressConfig` — immutable settings snapshot read from
     :func:`backend.config.get_settings` at construction time so the
     manager never re-reads env mid-flight.
  5. Typed errors (:class:`CFIngressError` base +
     :class:`CFIngressAPIError` / :class:`CFIngressNotFound` /
     :class:`CFIngressMisconfigured` subclasses).

W14.3 explicitly does NOT own:

  - CF Tunnel provisioning / token rotation (B12 wizard).
  - Cloudflare Access SSO on the ingress URL (W14.4 row).
  - DNS CNAME record creation — the operator's existing wildcard
    ``*.{tunnel_host}`` CNAME already covers ``preview-*`` subdomains;
    when no wildcard is set, W14.4's per-host SSO setup will own DNS
    CNAME provisioning.
  - Idle-kill reaper that triggers ``stop()`` (W14.5).
  - Persistent ingress-rule audit log (W14.10 alembic 0059).

Module-global state audit (SOP §1)
==================================

:class:`CFIngressManager` holds a per-uvicorn-worker
``_rules: dict[str, str]`` (sandbox_id → full hostname) cache guarded
by an ``RLock``. The cache is an *optimisation* — every public method
fetches the live tunnel config from the CF API before mutating it.
Cross-worker consistency answer = SOP §1 type **#2 (PG/Redis/CF
coordination)**: the canonical state is the CF Tunnel configuration
itself, served by the Cloudflare API. Two workers concurrently
launching distinct sandboxes both fetch fresh, splice their own rule,
and PUT — CF accepts last-write-wins. The bounded race window can
*lose* one of two simultaneous PUTs; mitigation: the W14.10 row will
replace the splice operation with a PG-serialised mutation via
``pg_advisory_xact_lock(crc('cf_ingress_mutation'))``. Until then the
race is acknowledged as a known limitation in the row's HANDOFF entry.

Read-after-write timing audit (SOP §2)
======================================

Fresh module — no compat→pool migration. The only race surface is the
GET-then-PUT window inside :meth:`CFIngressManager.create_rule` /
:meth:`CFIngressManager.delete_rule`: two workers may both read the
same baseline config, both insert their own rule, and the second PUT
overwrites the first. We mitigate with idempotent merging (a hostname
already in the list is not re-inserted) but the lossy case stands.

Compat fingerprint grep (SOP §3): N/A — fresh module, zero compat
artefacts.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

import httpx

logger = logging.getLogger(__name__)


__all__ = [
    "CF_INGRESS_SCHEMA_VERSION",
    "CF_API_BASE",
    "DEFAULT_HTTP_TIMEOUT_S",
    "PREVIEW_HOSTNAME_PREFIX",
    "DEFAULT_INGRESS_FALLBACK",
    "CFIngressError",
    "CFIngressAPIError",
    "CFIngressNotFound",
    "CFIngressMisconfigured",
    "CFIngressConfig",
    "CFIngressClient",
    "HttpxCFIngressClient",
    "CFIngressManager",
    "build_ingress_hostname",
    "build_ingress_service_url",
    "compute_ingress_rules_add",
    "compute_ingress_rules_remove",
    "find_ingress_rule",
    "extract_fallback_rule",
    "is_fallback_rule",
    "validate_tunnel_host",
    "validate_account_id",
    "validate_tunnel_id",
    "validate_sandbox_id",
    "token_fingerprint",
]


#: Bump when :class:`CFIngressConfig` / :class:`CFIngressManager` shape
#: changes — the W14.10 alembic 0059 audit row keys persisted entries
#: on this version so a forward-compat read of an older row keeps
#: parsing.
CF_INGRESS_SCHEMA_VERSION = "1.0.0"

#: The CF API v4 base. Pinned in this module rather than re-imported
#: from :mod:`backend.cloudflare_client` so the W14.3 module is
#: self-contained even if a future refactor moves the B12 client.
CF_API_BASE = "https://api.cloudflare.com/client/v4"

#: Default sync HTTP timeout for CF API calls.
DEFAULT_HTTP_TIMEOUT_S = 30.0

#: Hostname prefix every dynamic ingress rule starts with — the W14
#: epic header pins this naming scheme so the operator can grep
#: ``cloudflared logs | grep preview-`` and see only sandbox traffic.
PREVIEW_HOSTNAME_PREFIX = "preview-"

#: The fallback ingress rule CF requires at the end of every config
#: list. We always preserve any caller-authored fallback we find;
#: when one is missing we synthesise this default.
DEFAULT_INGRESS_FALLBACK: Mapping[str, Any] = MappingProxyType(
    {"service": "http_status:404"}
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class CFIngressError(RuntimeError):
    """Base for all W14.3 ingress-manager errors."""


class CFIngressAPIError(CFIngressError):
    """Raised when the CF API returns a non-2xx response.

    Carries the upstream status code (when known) so callers can
    map to HTTP responses without re-parsing the message string.
    """

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class CFIngressNotFound(CFIngressError):
    """Raised when :meth:`CFIngressManager.delete_rule` is called for a
    sandbox_id that has no recorded rule.

    Typically a programmer error or stale-state race; callers ought to
    catch and treat as no-op (the rule is already gone, which is the
    desired terminal state).
    """


class CFIngressMisconfigured(CFIngressError):
    """Raised at :class:`CFIngressManager` construction time when one
    of the four required Settings fields is empty.

    The router catches this in :func:`get_manager` and falls back to
    a manager without CF wiring — equivalent to the W14.2 dev path —
    so the *absence* of W14.3 config never fails the launch endpoint
    itself.
    """


# ───────────────────────────────────────────────────────────────────
#  Validation helpers (pure)
# ───────────────────────────────────────────────────────────────────


_TUNNEL_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?){1,}")
_CF_UUID_RE = re.compile(r"[0-9a-f]{32}")
_SANDBOX_ID_RE = re.compile(r"ws-[0-9a-f]{6,32}")


def validate_tunnel_host(value: str) -> None:
    """Reject empty / whitespace / non-string / RFC-illegal tunnel hosts.

    Relaxed check — we don't want to be a full RFC 1035 implementation;
    the goal is to reject obvious garbage (whitespace, ``://`` URLs,
    leading dot, single-label) before we splice the value into a
    public hostname.
    """

    if not isinstance(value, str) or not value.strip():
        raise CFIngressMisconfigured("tunnel_host must be a non-empty string")
    if value != value.strip():
        raise CFIngressMisconfigured(
            f"tunnel_host must not have leading/trailing whitespace: {value!r}"
        )
    if "://" in value or "/" in value:
        raise CFIngressMisconfigured(
            f"tunnel_host must be a bare hostname, not a URL: {value!r}"
        )
    if value.startswith(".") or value.endswith("."):
        raise CFIngressMisconfigured(
            f"tunnel_host must not start/end with '.': {value!r}"
        )
    if "." not in value:
        raise CFIngressMisconfigured(
            f"tunnel_host must have at least 2 labels: {value!r}"
        )
    if not _TUNNEL_HOST_RE.fullmatch(value):
        raise CFIngressMisconfigured(
            f"tunnel_host has invalid DNS chars: {value!r}"
        )


def validate_account_id(value: str) -> None:
    """Reject empty / non-32-char-hex CF account UUIDs."""

    if not isinstance(value, str) or not value.strip():
        raise CFIngressMisconfigured("cf_account_id must be a non-empty string")
    if not _CF_UUID_RE.fullmatch(value.strip()):
        raise CFIngressMisconfigured(
            f"cf_account_id must be a 32-char hex UUID: {value!r}"
        )


def validate_tunnel_id(value: str) -> None:
    """Reject empty / non-32-char-hex CF tunnel UUIDs."""

    if not isinstance(value, str) or not value.strip():
        raise CFIngressMisconfigured("cf_tunnel_id must be a non-empty string")
    if not _CF_UUID_RE.fullmatch(value.strip()):
        raise CFIngressMisconfigured(
            f"cf_tunnel_id must be a 32-char hex UUID: {value!r}"
        )


def validate_sandbox_id(value: str) -> None:
    """Reject sandbox_ids that aren't shaped like the W14.2 emitter.

    W14.2's :func:`backend.web_sandbox.format_sandbox_id` returns
    ``ws-{12hex}`` deterministically. We allow 6-32 hex chars to give
    callers some leeway (the W14.10 row may extend the hex length for
    a wider keyspace) but reject anything that doesn't start with
    ``ws-`` or contains non-hex digits — defence in depth so a
    caller-supplied ``sandbox_id`` can't smuggle a ``..`` traversal
    or shell metachar into the CF API hostname.
    """

    if not isinstance(value, str) or not value.strip():
        raise CFIngressError("sandbox_id must be a non-empty string")
    if not _SANDBOX_ID_RE.fullmatch(value):
        raise CFIngressError(
            f"sandbox_id must match 'ws-' + 6-32 lowercase hex chars: {value!r}"
        )


def token_fingerprint(token: str) -> str:
    """Return the last-4 fingerprint of a token for logging.

    Mirrors :func:`backend.cloudflare_client.token_fingerprint` so the
    log style is consistent across the B12 + W14.3 surfaces.
    """

    if not isinstance(token, str) or len(token) <= 8:
        return "****"
    return f"…{token[-4:]}"


# ───────────────────────────────────────────────────────────────────
#  Pure ingress-rule helpers
# ───────────────────────────────────────────────────────────────────


def build_ingress_hostname(sandbox_id: str, tunnel_host: str) -> str:
    """Return the public hostname for a sandbox.

    Layout: ``preview-{sandbox_id}.{tunnel_host}``. Pure function —
    caller passes already-validated ``sandbox_id`` (see
    :func:`validate_sandbox_id`) and ``tunnel_host`` (see
    :func:`validate_tunnel_host`).
    """

    validate_sandbox_id(sandbox_id)
    validate_tunnel_host(tunnel_host)
    return f"{PREVIEW_HOSTNAME_PREFIX}{sandbox_id}.{tunnel_host}"


def build_ingress_service_url(host_port: int, *, host: str = "127.0.0.1") -> str:
    """Return the in-tunnel target URL ``http://{host}:{host_port}``.

    The CF Tunnel connector resolves the target URL from inside the
    container running ``cloudflared``. In Path-B compose deployments
    (`docker-compose.prod.yml`) the connector is in the same compose
    network as the host's docker daemon — the W14.2 sidecar listens
    on ``127.0.0.1:{host_port}`` because docker port-publish (``-p``)
    binds to the host loopback, and the connector reaches it through
    its bind to the host network namespace via the ``omnisight_net``
    bridge. Operators with a non-default topology pass an alternate
    ``host`` (e.g. ``host.docker.internal`` on macOS).
    """

    if not isinstance(host_port, int) or not (1 <= host_port <= 65535):
        raise CFIngressError(f"host_port out of range: {host_port!r}")
    if not isinstance(host, str) or not host.strip():
        raise CFIngressError("host must be non-empty")
    return f"http://{host}:{host_port}"


def is_fallback_rule(rule: Mapping[str, Any]) -> bool:
    """Return True when ``rule`` is the catch-all fallback rule.

    A CF tunnel config's last entry must be a *fallback* — a rule
    without a ``hostname`` field. Detection: presence of ``service``
    plus absence of ``hostname``. Robust against rules that carry
    extra metadata (``originRequest``, ``path``).
    """

    if not isinstance(rule, Mapping):
        return False
    return "service" in rule and "hostname" not in rule


def extract_fallback_rule(rules: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Return the last rule when it is a valid fallback, else
    :data:`DEFAULT_INGRESS_FALLBACK`."""

    if rules and is_fallback_rule(rules[-1]):
        return rules[-1]
    return DEFAULT_INGRESS_FALLBACK


def find_ingress_rule(
    rules: Iterable[Mapping[str, Any]], hostname: str
) -> Mapping[str, Any] | None:
    """Return the first rule whose ``hostname`` matches ``hostname``.

    Returns ``None`` when no rule matches. Pure function.
    """

    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        if rule.get("hostname") == hostname:
            return rule
    return None


def compute_ingress_rules_add(
    existing: list[Mapping[str, Any]],
    *,
    hostname: str,
    service_url: str,
) -> list[dict[str, Any]]:
    """Return a new rules list with ``hostname → service_url`` spliced
    in **before** the fallback rule.

    Idempotent: if the rules list already contains a rule for
    ``hostname`` whose ``service`` matches ``service_url`` exactly,
    the input is returned unchanged (deep-copied to avoid aliasing).
    If the hostname matches but the service URL drifted (operator
    relaunched on a different host_port), the existing rule is
    *replaced* — the W14.3 contract is "this hostname always points
    at the latest sandbox for this workspace_id", which keeps the CF
    config from accumulating dead rules across restarts.

    Pure function — caller is responsible for the live-config GET +
    PUT round-trip.
    """

    if not isinstance(hostname, str) or not hostname.strip():
        raise CFIngressError("hostname must be non-empty")
    if not isinstance(service_url, str) or not service_url.strip():
        raise CFIngressError("service_url must be non-empty")
    if not isinstance(existing, list):
        raise CFIngressError("existing must be a list of mappings")

    new_rule = {"hostname": hostname, "service": service_url}
    fallback = extract_fallback_rule(existing)
    body: list[dict[str, Any]] = []
    replaced = False
    for rule in existing:
        if not isinstance(rule, Mapping):
            continue
        if is_fallback_rule(rule):
            # Drop the fallback while we splice — re-appended below.
            continue
        if rule.get("hostname") == hostname:
            body.append(dict(new_rule))
            replaced = True
        else:
            body.append(dict(rule))
    if not replaced:
        body.append(dict(new_rule))
    body.append(dict(fallback))
    return body


def compute_ingress_rules_remove(
    existing: list[Mapping[str, Any]],
    *,
    hostname: str,
) -> list[dict[str, Any]]:
    """Return a new rules list with all rules for ``hostname`` removed.

    The fallback rule is always preserved (or synthesised). Idempotent:
    removing an already-absent hostname is a no-op (returns a deep
    copy of the input minus duplicates of the fallback). Pure function.
    """

    if not isinstance(hostname, str) or not hostname.strip():
        raise CFIngressError("hostname must be non-empty")
    if not isinstance(existing, list):
        raise CFIngressError("existing must be a list of mappings")

    fallback = extract_fallback_rule(existing)
    body: list[dict[str, Any]] = []
    for rule in existing:
        if not isinstance(rule, Mapping):
            continue
        if is_fallback_rule(rule):
            continue
        if rule.get("hostname") == hostname:
            continue
        body.append(dict(rule))
    body.append(dict(fallback))
    return body


# ───────────────────────────────────────────────────────────────────
#  Config snapshot
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CFIngressConfig:
    """Immutable snapshot of the four W14.3 settings.

    Constructed once at :class:`CFIngressManager` init from
    :func:`backend.config.get_settings` so the manager never re-reads
    env mid-flight (cross-worker consistency = SOP §1 type-1: every
    worker derives the same config from the same source).
    """

    tunnel_host: str
    api_token: str
    account_id: str
    tunnel_id: str
    service_host: str = "127.0.0.1"
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S

    def __post_init__(self) -> None:
        validate_tunnel_host(self.tunnel_host)
        if not isinstance(self.api_token, str) or not self.api_token.strip():
            raise CFIngressMisconfigured("api_token must be a non-empty string")
        validate_account_id(self.account_id)
        validate_tunnel_id(self.tunnel_id)
        if not isinstance(self.service_host, str) or not self.service_host.strip():
            raise CFIngressMisconfigured("service_host must be non-empty")
        if (
            not isinstance(self.http_timeout_s, (int, float))
            or self.http_timeout_s <= 0
        ):
            raise CFIngressMisconfigured("http_timeout_s must be positive")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict with the api_token redacted to fingerprint."""

        return {
            "schema_version": CF_INGRESS_SCHEMA_VERSION,
            "tunnel_host": self.tunnel_host,
            "api_token_fingerprint": token_fingerprint(self.api_token),
            "account_id": self.account_id,
            "tunnel_id": self.tunnel_id,
            "service_host": self.service_host,
            "http_timeout_s": float(self.http_timeout_s),
        }

    @classmethod
    def from_settings(cls, settings: Any) -> "CFIngressConfig":
        """Build a :class:`CFIngressConfig` from a
        :class:`backend.config.Settings` instance.

        Raises :class:`CFIngressMisconfigured` when any of the four
        fields is empty / invalid. The router catches this and falls
        back to "no CF wiring" — *missing* config is a soft failure,
        *malformed* config is a hard one.
        """

        tunnel_host = (getattr(settings, "tunnel_host", "") or "").strip()
        api_token = (getattr(settings, "cf_api_token", "") or "").strip()
        account_id = (getattr(settings, "cf_account_id", "") or "").strip()
        tunnel_id = (getattr(settings, "cf_tunnel_id", "") or "").strip()
        if not (tunnel_host and api_token and account_id and tunnel_id):
            missing = [
                name
                for name, value in (
                    ("OMNISIGHT_TUNNEL_HOST", tunnel_host),
                    ("OMNISIGHT_CF_API_TOKEN", api_token),
                    ("OMNISIGHT_CF_ACCOUNT_ID", account_id),
                    ("OMNISIGHT_CF_TUNNEL_ID", tunnel_id),
                )
                if not value
            ]
            raise CFIngressMisconfigured(
                "W14.3 CF Tunnel ingress requires all four env knobs to be "
                f"set; missing: {', '.join(missing)}"
            )
        return cls(
            tunnel_host=tunnel_host,
            api_token=api_token,
            account_id=account_id,
            tunnel_id=tunnel_id,
        )


# ───────────────────────────────────────────────────────────────────
#  HTTP client (Protocol + sync httpx impl)
# ───────────────────────────────────────────────────────────────────


class CFIngressClient(Protocol):
    """Structural Protocol the manager calls into.

    Implementations: :class:`HttpxCFIngressClient` (production) and
    test fakes that capture call sequences without talking to CF.
    """

    def get_tunnel_config(self) -> dict[str, Any]:
        """GET ``/accounts/{id}/cfd_tunnel/{id}/configurations``.

        Returns the unwrapped ``config`` dict (the API wraps it in
        ``{result: {config: {...}}}``).
        """

    def put_tunnel_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """PUT the full ``config`` dict back to the same endpoint.

        Returns the API response payload.
        """


class HttpxCFIngressClient:
    """Sync :class:`CFIngressClient` impl using ``httpx.Client``.

    Why sync: :meth:`backend.web_sandbox.WebSandboxManager.launch` is
    sync, called from a FastAPI ``async def`` route handler that
    delegates to it via ``Depends`` injection. Running the launch sync
    avoids the impedance mismatch of calling an async client from a
    sync method (would require ``asyncio.run_coroutine_threadsafe``).

    The router runs the launch inside FastAPI's threadpool by virtue
    of being a regular function call — FastAPI offloads the sync body
    of the ``await asyncio.to_thread`` (or its internal threadpool
    wrapping) so the event loop is never blocked.
    """

    def __init__(self, config: CFIngressConfig) -> None:
        if not isinstance(config, CFIngressConfig):
            raise TypeError("config must be a CFIngressConfig")
        self._config = config

    @property
    def config(self) -> CFIngressConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }

    def _path(self) -> str:
        return (
            f"/accounts/{self._config.account_id}"
            f"/cfd_tunnel/{self._config.tunnel_id}/configurations"
        )

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            body = response.json()
        except Exception:
            body = {}
        message = ""
        errors = body.get("errors") if isinstance(body, Mapping) else None
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping):
                message = str(first.get("message") or "")
        if not message:
            message = response.text or f"HTTP {response.status_code}"
        raise CFIngressAPIError(
            f"CF API {response.status_code}: {message}",
            status=response.status_code,
        )

    def get_tunnel_config(self) -> dict[str, Any]:
        url = f"{CF_API_BASE}{self._path()}"
        with httpx.Client(timeout=self._config.http_timeout_s) as client:
            response = client.get(url, headers=self._headers())
        self._raise(response)
        try:
            payload = response.json()
        except Exception as exc:
            raise CFIngressAPIError(
                f"CF API returned non-JSON body: {exc}", status=response.status_code
            ) from exc
        if not isinstance(payload, Mapping):
            raise CFIngressAPIError(
                "CF API response must be a JSON object", status=response.status_code
            )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            # Empty / freshly-provisioned tunnel returns null — treat
            # as "no rules yet" so the first launch can splice cleanly.
            return {"ingress": []}
        config = result.get("config")
        if not isinstance(config, Mapping):
            return {"ingress": []}
        return dict(config)

    def put_tunnel_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(config, Mapping):
            raise TypeError("config must be a mapping")
        url = f"{CF_API_BASE}{self._path()}"
        body = {"config": dict(config)}
        with httpx.Client(timeout=self._config.http_timeout_s) as client:
            response = client.put(url, headers=self._headers(), json=body)
        self._raise(response)
        try:
            payload = response.json()
        except Exception as exc:
            raise CFIngressAPIError(
                f"CF API returned non-JSON body on PUT: {exc}",
                status=response.status_code,
            ) from exc
        if not isinstance(payload, Mapping):
            raise CFIngressAPIError(
                "CF API PUT response must be a JSON object",
                status=response.status_code,
            )
        return dict(payload)


# ───────────────────────────────────────────────────────────────────
#  Manager
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CFIngressRuleRecord:
    """In-memory record of a live ingress rule.

    Lightweight shadow of the CF-side rule — the manager keeps these
    in ``_rules`` indexed by sandbox_id so :meth:`delete_rule` does not
    need the caller to remember the hostname (the W14.10 row will move
    this into PG with extra audit columns).
    """

    sandbox_id: str
    hostname: str
    service_url: str


class CFIngressManager:
    """Thread-safe lifecycle manager for dynamic CF tunnel ingress rules.

    One manager per uvicorn worker. Each worker maintains its own
    in-memory cache of rules it has created — every public method
    fetches the live tunnel config before mutating it, so the cache
    is an *optimisation* not source of truth (cross-worker correctness
    = CF API is canonical, see SOP §1 audit at module top).

    Lifecycle (called from :class:`backend.web_sandbox.WebSandboxManager`
    via the optional ``cf_ingress_manager`` constructor param):

      1. ``manager.create_rule(sandbox_id, host_port)`` on launch →
         returns the public ``https://...`` URL the launcher pins onto
         :attr:`WebSandboxInstance.ingress_url`.
      2. ``manager.delete_rule(sandbox_id)`` on stop — idempotent.
      3. ``manager.list_rules()`` for triage / W14.6 frontend display.
      4. ``manager.cleanup()`` for test teardown / worker shutdown.
    """

    def __init__(
        self,
        *,
        config: CFIngressConfig,
        client: CFIngressClient | None = None,
    ) -> None:
        if not isinstance(config, CFIngressConfig):
            raise TypeError("config must be a CFIngressConfig")
        self._config = config
        self._client: CFIngressClient = client or HttpxCFIngressClient(config)
        self._lock = threading.RLock()
        self._rules: dict[str, CFIngressRuleRecord] = {}

    @property
    def config(self) -> CFIngressConfig:
        return self._config

    @property
    def client(self) -> CFIngressClient:
        return self._client

    # ─────────────── Public API ───────────────

    def create_rule(
        self,
        *,
        sandbox_id: str,
        host_port: int,
    ) -> str:
        """Add (or refresh) a rule for ``sandbox_id`` and return the
        public ``https://...`` URL.

        Idempotent: calling twice with the same ``sandbox_id`` +
        ``host_port`` is a no-op on the CF side (the splice helper
        detects the equal entry). When ``host_port`` drifts (worker
        restarted with a different port), the existing rule is
        *replaced* — the public URL stays stable, the in-tunnel
        target updates to the new port.

        Raises :class:`CFIngressAPIError` when the CF API rejects
        either the GET or the PUT — the launcher catches and folds
        into a per-instance warning so the launch itself doesn't
        fail just because CF is briefly unreachable.
        """

        validate_sandbox_id(sandbox_id)
        hostname = build_ingress_hostname(sandbox_id, self._config.tunnel_host)
        service_url = build_ingress_service_url(
            host_port, host=self._config.service_host
        )
        with self._lock:
            current = self._client.get_tunnel_config()
            ingress = list(current.get("ingress") or [])
            new_ingress = compute_ingress_rules_add(
                ingress, hostname=hostname, service_url=service_url
            )
            if new_ingress != ingress:
                merged = dict(current)
                merged["ingress"] = new_ingress
                self._client.put_tunnel_config(merged)
                logger.info(
                    "cf_ingress: rule added for sandbox_id=%s host=%s "
                    "(token=%s tunnel=%s)",
                    sandbox_id,
                    hostname,
                    token_fingerprint(self._config.api_token),
                    self._config.tunnel_id,
                )
            else:
                logger.debug(
                    "cf_ingress: rule already present for sandbox_id=%s host=%s",
                    sandbox_id,
                    hostname,
                )
            self._rules[sandbox_id] = CFIngressRuleRecord(
                sandbox_id=sandbox_id,
                hostname=hostname,
                service_url=service_url,
            )
        return f"https://{hostname}"

    def delete_rule(self, sandbox_id: str) -> bool:
        """Remove the rule for ``sandbox_id``. Idempotent.

        Returns ``True`` when a rule was actually removed, ``False``
        when none was present (already gone). Raises
        :class:`CFIngressAPIError` when the CF API itself rejects the
        operation.
        """

        validate_sandbox_id(sandbox_id)
        hostname = build_ingress_hostname(sandbox_id, self._config.tunnel_host)
        with self._lock:
            current = self._client.get_tunnel_config()
            ingress = list(current.get("ingress") or [])
            if find_ingress_rule(ingress, hostname) is None:
                self._rules.pop(sandbox_id, None)
                logger.debug(
                    "cf_ingress: rule already absent for sandbox_id=%s host=%s",
                    sandbox_id,
                    hostname,
                )
                return False
            new_ingress = compute_ingress_rules_remove(ingress, hostname=hostname)
            merged = dict(current)
            merged["ingress"] = new_ingress
            self._client.put_tunnel_config(merged)
            self._rules.pop(sandbox_id, None)
            logger.info(
                "cf_ingress: rule removed for sandbox_id=%s host=%s "
                "(token=%s tunnel=%s)",
                sandbox_id,
                hostname,
                token_fingerprint(self._config.api_token),
                self._config.tunnel_id,
            )
        return True

    def get_rule(self, sandbox_id: str) -> CFIngressRuleRecord | None:
        """Return the cached record for ``sandbox_id`` (does not GET CF)."""

        with self._lock:
            return self._rules.get(sandbox_id)

    def list_rules(self) -> tuple[CFIngressRuleRecord, ...]:
        """Return all cached records (does not GET CF)."""

        with self._lock:
            return tuple(self._rules.values())

    def public_url_for(self, sandbox_id: str) -> str:
        """Return the public URL for ``sandbox_id`` without touching CF.

        Pure helper for callers that want to compute the URL ahead of
        time (e.g. router-level pre-warning / SSE event payload).
        """

        validate_sandbox_id(sandbox_id)
        hostname = build_ingress_hostname(sandbox_id, self._config.tunnel_host)
        return f"https://{hostname}"

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe dict describing the manager's cached state."""

        with self._lock:
            return {
                "schema_version": CF_INGRESS_SCHEMA_VERSION,
                "config": self._config.to_dict(),
                "rules": [
                    {
                        "sandbox_id": r.sandbox_id,
                        "hostname": r.hostname,
                        "service_url": r.service_url,
                    }
                    for r in self._rules.values()
                ],
                "count": len(self._rules),
            }

    def cleanup(self) -> int:
        """Remove every rule the manager has cached. Returns count.

        Best-effort: errors removing individual rules are logged and
        do not stop the loop. Used by tests + worker shutdown when the
        manager wants to leave the CF config clean.
        """

        with self._lock:
            sandbox_ids = list(self._rules.keys())
        removed = 0
        for sid in sandbox_ids:
            try:
                if self.delete_rule(sid):
                    removed += 1
            except CFIngressError as exc:
                logger.warning(
                    "cf_ingress: cleanup failed for sandbox_id=%s: %s",
                    sid,
                    exc,
                )
        return removed
