"""O5 (#268) — IntentSource abstraction for JIRA / GitHub Issues / GitLab.

The Orchestrator Gateway (O4) expects a ``story`` to come in via webhook, a
set of sub-tasks to be created in the tracker, and status to flow back out
once CATCs progress through the queue → Gerrit → submit pipeline.  Different
customers run different trackers — JIRA for the enterprise, GitHub Issues
for small OSS-adjacent shops, GitLab Issues for self-hosted.

Rather than branching every orchestrator path on ``if jira elif github``,
this module defines a single protocol that every adapter implements:

    fetch_story(ticket)          → IntentStory
    create_subtasks(parent, …)   → list[SubtaskRef]
    update_status(ticket, status) → dict
    comment(ticket, body)         → dict
    verify_webhook(headers, body) → bool

Concrete adapters live in ``backend.jira_adapter`` / ``backend.github_adapter``
/ ``backend.gitlab_adapter``.  Pick one via ``get_source()`` or rely on
``detect_vendor()`` to inspect an incoming webhook and pick automatically.

Every outbound call MUST go through ``audit_outbound()`` so the audit log
captures a request/response hash + truncated preview — enough to trace a
request through the system without leaking PII into the chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Status enum — vendor-agnostic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class IntentStatus(str, Enum):
    """Vendor-agnostic status values that adapters translate to their own.

    * ``backlog``     → JIRA "To Do", GitHub "open" (no status:* label)
    * ``in_progress`` → JIRA "In Progress" + GitHub/GitLab status:in_progress
    * ``reviewing``   → JIRA "In Review"  (Worker has pushed to Gerrit)
    * ``blocked``     → JIRA "Blocked"
    * ``done``        → JIRA "Done"       (dual +2 + Gerrit submit complete)
    """
    backlog = "backlog"
    in_progress = "in_progress"
    reviewing = "reviewing"
    blocked = "blocked"
    done = "done"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class IntentStory:
    """Normalised User Story — the input to the orchestrator pipeline."""
    vendor: str
    ticket: str                  # "PROJ-123" / "owner/repo#42" / "group/proj#17"
    summary: str
    description: str = ""
    priority: str = ""
    labels: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubtaskPayload:
    """One sub-task to create in the tracker — built from a CATC TaskCard."""
    title: str
    acceptance_criteria: str
    impact_scope_allowed: list[str]
    impact_scope_forbidden: list[str]
    handoff_protocol: list[str]
    domain_context: str = ""
    labels: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_task_card(cls, card: Any) -> "SubtaskPayload":
        """Build a SubtaskPayload from a ``backend.catc.TaskCard``.

        Uses attribute access so the import-time dependency is one-way
        (catc → intent_source, never the reverse).
        """
        nav = card.navigation
        scope = nav.impact_scope
        return cls(
            title=card.jira_ticket,
            acceptance_criteria=card.acceptance_criteria,
            impact_scope_allowed=list(scope.allowed),
            impact_scope_forbidden=list(scope.forbidden),
            handoff_protocol=list(card.handoff_protocol),
            domain_context=card.domain_context,
            labels=[],
        )


@dataclass
class SubtaskRef:
    """What an adapter returns after creating a sub-task."""
    vendor: str
    ticket: str                  # e.g. "PROJ-1001" / "owner/repo#43"
    url: str = ""
    parent: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "ticket": self.ticket,
            "url": self.url,
            "parent": self.parent,
        }


class AdapterError(RuntimeError):
    """Raised by adapters on non-recoverable failures (auth, 4xx, malformed)."""

    def __init__(self, vendor: str, action: str, message: str, *,
                 status_code: int | None = None,
                 response: Any | None = None) -> None:
        super().__init__(f"[{vendor}:{action}] {message}")
        self.vendor = vendor
        self.action = action
        self.message = message
        self.status_code = status_code
        self.response = response


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Protocol
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@runtime_checkable
class IntentSource(Protocol):
    """Every tracker adapter exposes this surface.

    Implementations live in vendor-specific modules — pick one via
    ``get_source(vendor)`` or ``detect_vendor(headers, body)``.

    Adapters should be constructed with explicit credentials + base URL
    (no hidden singleton state) so tests can instantiate a fake
    implementation without monkey-patching.
    """

    vendor: str

    async def fetch_story(self, ticket: str) -> IntentStory: ...

    async def create_subtasks(self, parent: str,
                              payloads: list[SubtaskPayload],
                              ) -> list[SubtaskRef]: ...

    async def update_status(self, ticket: str, status: IntentStatus,
                            *, comment: str = "") -> dict[str, Any]: ...

    async def comment(self, ticket: str, body: str) -> dict[str, Any]: ...

    async def verify_webhook(self, headers: Mapping[str, str],
                             body: bytes) -> bool: ...

    def parse_webhook(self, body: dict[str, Any]
                      ) -> tuple[str, str]: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_registry: dict[str, IntentSource] = {}
_factories: dict[str, Callable[[], IntentSource]] = {}


def register_source(source: IntentSource) -> None:
    """Register a ready-to-use adapter instance."""
    _registry[source.vendor] = source


def register_factory(vendor: str, factory: Callable[[], IntentSource]) -> None:
    """Register a lazy factory — called on first ``get_source(vendor)``.

    Preferred for production wiring so credentials aren't read at import
    time and tests can override without running the factory first.
    """
    _factories[vendor] = factory


def unregister_source(vendor: str) -> None:
    """Test helper — drops a vendor from both registries."""
    _registry.pop(vendor, None)
    _factories.pop(vendor, None)


def reset_registry_for_tests() -> None:
    _registry.clear()
    _factories.clear()


def get_source(vendor: str) -> IntentSource:
    """Return the adapter for ``vendor``.  Raises KeyError if unknown."""
    if vendor in _registry:
        return _registry[vendor]
    if vendor in _factories:
        try:
            inst = _factories[vendor]()
        except Exception as exc:
            raise KeyError(
                f"intent_source factory for {vendor!r} failed: {exc}"
            ) from exc
        _registry[vendor] = inst
        return inst
    raise KeyError(
        f"no IntentSource registered for vendor={vendor!r} "
        f"(have: {sorted(list_vendors())})"
    )


def list_vendors() -> list[str]:
    """All registered vendors (both direct + factory)."""
    return sorted(set(_registry.keys()) | set(_factories.keys()))


def default_vendor() -> str:
    """The vendor to use when none is specified.

    Reads ``OMNISIGHT_INTENT_VENDOR`` (env) first.  Falls back to JIRA
    when credentials are present; otherwise the first registered source;
    otherwise ``"jira"`` as a soft default so error messages are stable.
    """
    env = os.environ.get("OMNISIGHT_INTENT_VENDOR", "").strip().lower()
    if env and env in list_vendors():
        return env
    # Heuristic: prefer JIRA if registered (matches design doc §O5).
    if "jira" in list_vendors():
        return "jira"
    remaining = list_vendors()
    return remaining[0] if remaining else "jira"


def detect_vendor(headers: Mapping[str, str], body: bytes) -> str | None:
    """Inspect webhook headers + raw body to guess the vendor.

    Matches the real-world signature headers:

      * GitHub → ``X-GitHub-Event`` + ``X-Hub-Signature-256``
      * GitLab → ``X-Gitlab-Event`` or ``X-Gitlab-Token``
      * JIRA   → ``X-Jira-Webhook-Secret`` / ``Authorization: Bearer``
                 or payload containing ``"issue": {"key": "PROJ-…"}``

    Returns ``None`` when we can't tell — callers should fall back to
    ``default_vendor()``.
    """
    h = {k.lower(): v for k, v in headers.items()}
    if "x-github-event" in h or "x-hub-signature-256" in h:
        return "github"
    if "x-gitlab-event" in h or "x-gitlab-token" in h:
        return "gitlab"
    if "x-jira-webhook-secret" in h:
        return "jira"
    # Authorization: Bearer <secret> is ambiguous — fall through to body.
    try:
        parsed = json.loads(body.decode() or "{}")
    except Exception:
        return None
    if isinstance(parsed, dict):
        issue = parsed.get("issue") or {}
        if isinstance(issue, dict):
            key = issue.get("key") or ""
            if isinstance(key, str) and _looks_like_jira_key(key):
                return "jira"
        if "pull_request" in parsed or "repository" in parsed:
            return "github"
        if "object_attributes" in parsed or "object_kind" in parsed:
            return "gitlab"
    return None


def _looks_like_jira_key(s: str) -> bool:
    import re
    return bool(re.match(r"^[A-Z][A-Z0-9_]*-\d+$", s))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outbound audit helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_PREVIEW_BYTES = 256


def _canonical(obj: Any) -> str:
    """Deterministic JSON (same rules as ``backend.audit``) for hashing."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def payload_hash(obj: Any) -> str:
    """sha256 of canonical serialisation of ``obj`` (bytes / str / dict / list)."""
    if isinstance(obj, bytes):
        return hashlib.sha256(obj).hexdigest()
    if isinstance(obj, str):
        return hashlib.sha256(obj.encode("utf-8")).hexdigest()
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _preview(obj: Any) -> str:
    """Truncated textual preview for the audit ``before``/``after`` blob."""
    if isinstance(obj, (bytes, bytearray)):
        try:
            s = obj.decode("utf-8", errors="replace")
        except Exception:
            s = repr(obj)
    elif isinstance(obj, str):
        s = obj
    else:
        s = _canonical(obj)
    if len(s) > _PREVIEW_BYTES:
        return s[:_PREVIEW_BYTES] + "…"
    return s


async def audit_outbound(*, vendor: str, action: str, ticket: str,
                         request: Any, response: Any,
                         status_code: int | None = None,
                         actor: str = "intent_bridge",
                         ) -> int | None:
    """Log an outbound API call.

    The audit log gets:
      * ``entity_kind='intent_source'``
      * ``action=f'{vendor}:{action}'`` (e.g. ``jira:create_subtask``)
      * ``before = {request_hash, request_preview}``
      * ``after  = {response_hash, response_preview, http_status}``

    Swallows all exceptions — the audit layer is advisory, never a gate.
    """
    try:
        from backend import audit
    except Exception as exc:
        logger.debug("audit import failed for %s:%s: %s", vendor, action, exc)
        return None

    try:
        return await audit.log(
            action=f"intent_source:{vendor}:{action}",
            entity_kind="intent_source",
            entity_id=f"{vendor}:{ticket}",
            before={
                "request_hash": payload_hash(request),
                "request_preview": _preview(request),
            },
            after={
                "response_hash": payload_hash(response),
                "response_preview": _preview(response),
                "http_status": status_code,
            },
            actor=f"{actor}/{vendor}",
        )
    except Exception as exc:
        logger.debug("audit_outbound swallowed error on %s:%s: %s",
                     vendor, action, exc)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP client shim — shared by all adapters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Adapters should NOT pick their own transport.  They receive an
# ``HttpCall`` at construction time; the default is a ``curl``-subprocess
# implementation so production doesn't need a new dependency, and tests
# inject a pure-Python fake.


HttpCall = Callable[
    [str, str, Mapping[str, str], bytes | None],
    Awaitable[tuple[int, bytes, dict[str, str]]],
]


async def curl_json_call(
    method: str, url: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """Default HTTP transport — wraps ``curl`` via subprocess.

    Returns ``(http_status, response_body, response_headers)``.  A non-2xx
    status does NOT raise — the caller is responsible for mapping it to
    an ``AdapterError``.  Network errors (curl rc != 0) return
    ``(0, stderr, {})`` so the caller can decide whether to retry.
    """
    import asyncio
    import tempfile
    import shlex

    hdr = dict(headers or {})
    hdr.setdefault("Accept", "application/json")

    argv: list[str] = [
        "curl", "-sS", "-X", method.upper(),
        "-o", "-",  # body to stdout
        "-w", "\n__HTTP_STATUS__=%{http_code}\n",
        "--max-time", "30",
        url,
    ]
    for k, v in hdr.items():
        argv += ["-H", f"{k}: {v}"]

    data_file: str | None = None
    try:
        if body is not None:
            f = tempfile.NamedTemporaryFile("wb", delete=False, suffix=".json")
            f.write(body)
            f.close()
            data_file = f.name
            argv += ["--data-binary", f"@{data_file}"]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=35,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (0, b"curl: timeout", {})

        if proc.returncode != 0:
            return (0, stderr or b"curl failed", {})

        text = stdout.decode("utf-8", errors="replace")
        status = 0
        if "__HTTP_STATUS__=" in text:
            body_part, _, tail = text.rpartition("__HTTP_STATUS__=")
            body_part = body_part.rstrip("\n")
            try:
                status = int(tail.strip())
            except ValueError:
                status = 0
            return (status, body_part.encode("utf-8"), {})
        return (0, stdout, {})
    except FileNotFoundError:
        # curl missing — dev environment.  Report as zero-status.
        return (0, b"curl not installed", {})
    except Exception as exc:
        return (0, f"curl error: {exc}".encode("utf-8"), {})
    finally:
        if data_file:
            try:
                os.unlink(data_file)
            except OSError:
                pass


__all__ = [
    "AdapterError",
    "HttpCall",
    "IntentSource",
    "IntentStatus",
    "IntentStory",
    "SubtaskPayload",
    "SubtaskRef",
    "audit_outbound",
    "curl_json_call",
    "default_vendor",
    "detect_vendor",
    "get_source",
    "list_vendors",
    "payload_hash",
    "register_factory",
    "register_source",
    "reset_registry_for_tests",
    "unregister_source",
]
