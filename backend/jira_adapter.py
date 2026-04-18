"""O5 (#268) — JIRA IntentSource adapter.

Implements the ``IntentSource`` protocol over the Atlassian JIRA REST
v2 API.  Supports:

  * ``fetch_story(key)``                    — GET /rest/api/2/issue/{key}
  * ``create_subtasks(parent, payloads)``   — POST /rest/api/2/issue/bulk
  * ``update_status(key, IntentStatus)``    — GET + POST transitions
  * ``comment(key, body)``                  — POST /rest/api/2/issue/{key}/comment
  * ``verify_webhook(headers, body)``       — Bearer / X-Jira-Webhook-Secret

Field mapping for sub-tasks uses an injectable ``JiraFieldMap`` so the
specific ``customfield_XXXXX`` IDs (unique per JIRA instance) can be
configured without touching the code path — see
``JiraFieldMap.from_env()``.

Every outbound call routes through ``intent_source.audit_outbound`` so
the audit log keeps a hash of request + response payloads.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.intent_source import (
    AdapterError,
    HttpCall,
    IntentStatus,
    IntentStory,
    SubtaskPayload,
    SubtaskRef,
    audit_outbound,
    curl_json_call,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Status map  —  vendor-agnostic → JIRA workflow names
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


DEFAULT_JIRA_STATUS_MAP: dict[IntentStatus, str] = {
    IntentStatus.backlog:     "To Do",
    IntentStatus.in_progress: "In Progress",
    IntentStatus.reviewing:   "In Review",
    IntentStatus.blocked:     "Blocked",
    IntentStatus.done:        "Done",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Field mapping — CATC → JIRA custom fields
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class JiraFieldMap:
    """Maps CATC sub-task attributes to JIRA custom field IDs.

    Each JIRA installation allocates custom field IDs when the admin
    defines the schema — e.g. ``customfield_10050``.  Ops sets the
    mapping once (env or DB) and the adapter reads it here.
    """
    impact_scope_allowed: str = "customfield_omni_impact_scope_allowed"
    impact_scope_forbidden: str = "customfield_omni_impact_scope_forbidden"
    acceptance_criteria: str = "customfield_omni_acceptance_criteria"
    handoff_protocol: str = "customfield_omni_handoff_protocol"
    domain_context: str = "customfield_omni_domain_context"

    @classmethod
    def from_env(cls) -> "JiraFieldMap":
        """Pick up ``OMNISIGHT_JIRA_FIELD_*`` overrides."""
        return cls(
            impact_scope_allowed=os.environ.get(
                "OMNISIGHT_JIRA_FIELD_IMPACT_SCOPE_ALLOWED",
                cls.impact_scope_allowed,
            ),
            impact_scope_forbidden=os.environ.get(
                "OMNISIGHT_JIRA_FIELD_IMPACT_SCOPE_FORBIDDEN",
                cls.impact_scope_forbidden,
            ),
            acceptance_criteria=os.environ.get(
                "OMNISIGHT_JIRA_FIELD_ACCEPTANCE_CRITERIA",
                cls.acceptance_criteria,
            ),
            handoff_protocol=os.environ.get(
                "OMNISIGHT_JIRA_FIELD_HANDOFF_PROTOCOL",
                cls.handoff_protocol,
            ),
            domain_context=os.environ.get(
                "OMNISIGHT_JIRA_FIELD_DOMAIN_CONTEXT",
                cls.domain_context,
            ),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Adapter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class JiraAdapter:
    """JIRA-backed ``IntentSource``.

    Parameters
    ----------
    base_url:
        JIRA instance URL, e.g. ``https://jira.company.com``.  Trailing
        slash stripped.
    token:
        Personal access token OR base64(email:api_token) for JIRA Cloud.
        Selected via ``auth_mode``.
    auth_mode:
        ``"bearer"`` (default — JIRA DC) or ``"basic"`` (JIRA Cloud).
    project_key:
        Parent project for sub-task creation (e.g. ``"PROJ"``).  When
        unset, sub-tasks inherit the parent's project automatically.
    webhook_secret:
        Shared secret for ``verify_webhook``.  Accepts either
        ``Authorization: Bearer <secret>`` or ``X-Jira-Webhook-Secret``
        header (matches ``backend.routers.webhooks.jira_webhook`` +
        ``backend.routers.orchestrator._verify_jira_signature``).
    field_map:
        CATC → custom-field ID mapping.  ``JiraFieldMap.from_env()`` is
        a sensible default.
    http_call:
        Injectable HTTP transport for testability.  Default is
        ``curl_json_call``.
    status_map:
        Override the IntentStatus → JIRA workflow name mapping.
    """

    base_url: str
    token: str = ""
    auth_mode: str = "bearer"
    project_key: str = ""
    webhook_secret: str = ""
    field_map: JiraFieldMap = field(default_factory=JiraFieldMap.from_env)
    http_call: HttpCall = curl_json_call
    status_map: dict[IntentStatus, str] = field(
        default_factory=lambda: dict(DEFAULT_JIRA_STATUS_MAP)
    )
    vendor: str = "jira"

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    # ─── helpers ──────────────────────────────────────────────────

    def _auth_header(self) -> str:
        if not self.token:
            return ""
        if self.auth_mode == "basic":
            # Allow pre-encoded "email:token" or plain pair — we
            # re-encode only if it contains ":".
            if ":" in self.token:
                encoded = base64.b64encode(self.token.encode()).decode()
                return f"Basic {encoded}"
            return f"Basic {self.token}"
        return f"Bearer {self.token}"

    async def _api(self, method: str, path: str,
                   body: Any | None = None) -> tuple[int, Any]:
        """Hit a JIRA REST endpoint.  Returns (status, decoded_json).

        When the response isn't valid JSON we return the raw string so
        the caller can report context in an ``AdapterError``.
        """
        url = self.base_url + path
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth

        encoded: bytes | None = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")

        status, raw, _ = await self.http_call(method, url, headers, encoded)
        try:
            decoded: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            decoded = raw.decode("utf-8", errors="replace") if raw else ""
        return status, decoded

    def _status_name(self, status: IntentStatus) -> str:
        return self.status_map.get(status, status.value)

    # ─── IntentSource surface ─────────────────────────────────────

    async def fetch_story(self, ticket: str) -> IntentStory:
        _require_ticket(ticket)
        status, body = await self._api("GET", f"/rest/api/2/issue/{ticket}")
        await audit_outbound(
            vendor=self.vendor, action="fetch_story", ticket=ticket,
            request={"method": "GET", "path": f"/rest/api/2/issue/{ticket}"},
            response=body, status_code=status,
        )
        if status < 200 or status >= 300 or not isinstance(body, dict):
            raise AdapterError(
                self.vendor, "fetch_story",
                f"HTTP {status} for {ticket}",
                status_code=status, response=body,
            )
        fields = body.get("fields") or {}
        summary = fields.get("summary") or ""
        description = _coerce_description(fields.get("description"))
        priority_obj = fields.get("priority") or {}
        priority = (priority_obj.get("name") or "") if isinstance(
            priority_obj, dict) else ""
        labels = fields.get("labels") or []
        return IntentStory(
            vendor=self.vendor,
            ticket=ticket,
            summary=summary,
            description=description,
            priority=priority,
            labels=list(labels) if isinstance(labels, list) else [],
            raw=body,
        )

    async def create_subtasks(self, parent: str,
                              payloads: list[SubtaskPayload],
                              ) -> list[SubtaskRef]:
        _require_ticket(parent)
        if not payloads:
            return []

        project = self.project_key or _extract_project_key(parent)
        issueUpdates = [self._payload_to_issue(parent, project, p)
                        for p in payloads]
        request_body = {"issueUpdates": issueUpdates}
        status, body = await self._api(
            "POST", "/rest/api/2/issue/bulk", request_body,
        )
        await audit_outbound(
            vendor=self.vendor, action="create_subtasks", ticket=parent,
            request=request_body, response=body, status_code=status,
        )
        if status < 200 or status >= 300 or not isinstance(body, dict):
            raise AdapterError(
                self.vendor, "create_subtasks",
                f"bulk create failed: HTTP {status}",
                status_code=status, response=body,
            )

        issues = body.get("issues") or []
        errors = body.get("errors") or []
        if errors:
            # Partial success — surface the errors but still return the
            # refs for the ones that did get created.
            logger.warning(
                "jira create_subtasks partial failure: parent=%s errors=%s",
                parent, errors,
            )
        refs: list[SubtaskRef] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            key = issue.get("key") or ""
            if not key:
                continue
            refs.append(SubtaskRef(
                vendor=self.vendor,
                ticket=key,
                url=self._browse_url(key),
                parent=parent,
                extra={"id": issue.get("id", "")},
            ))
        return refs

    async def update_status(self, ticket: str, status: IntentStatus,
                            *, comment: str = "") -> dict[str, Any]:
        _require_ticket(ticket)
        target_name = self._status_name(status)

        # 1) Find matching transition.
        tstatus, tbody = await self._api(
            "GET", f"/rest/api/2/issue/{ticket}/transitions",
        )
        await audit_outbound(
            vendor=self.vendor, action="list_transitions", ticket=ticket,
            request={"method": "GET",
                     "path": f"/rest/api/2/issue/{ticket}/transitions"},
            response=tbody, status_code=tstatus,
        )
        if tstatus < 200 or tstatus >= 300 or not isinstance(tbody, dict):
            raise AdapterError(
                self.vendor, "update_status",
                f"list_transitions HTTP {tstatus}",
                status_code=tstatus, response=tbody,
            )
        transitions = tbody.get("transitions") or []
        transition_id = _match_transition(transitions, target_name)
        if not transition_id:
            available = [t.get("name", "?") for t in transitions
                         if isinstance(t, dict)]
            raise AdapterError(
                self.vendor, "update_status",
                f"no transition matching {target_name!r}; "
                f"available={available}",
                status_code=tstatus, response=tbody,
            )

        # 2) POST the transition.
        payload = {"transition": {"id": transition_id}}
        if comment:
            payload["update"] = {"comment": [{"add": {"body": comment}}]}
        pstatus, pbody = await self._api(
            "POST", f"/rest/api/2/issue/{ticket}/transitions", payload,
        )
        await audit_outbound(
            vendor=self.vendor, action="update_status", ticket=ticket,
            request={"target": target_name, "transition_id": transition_id,
                     "comment_len": len(comment)},
            response=pbody, status_code=pstatus,
        )
        if pstatus < 200 or pstatus >= 300:
            raise AdapterError(
                self.vendor, "update_status",
                f"transition POST HTTP {pstatus}",
                status_code=pstatus, response=pbody,
            )
        return {
            "ok": True, "vendor": self.vendor, "ticket": ticket,
            "status": status.value, "jira_status": target_name,
            "transition_id": transition_id,
        }

    async def comment(self, ticket: str, body: str) -> dict[str, Any]:
        _require_ticket(ticket)
        if not body:
            raise AdapterError(
                self.vendor, "comment",
                "refusing to post an empty comment",
            )
        request_body = {"body": body}
        status, resp = await self._api(
            "POST", f"/rest/api/2/issue/{ticket}/comment", request_body,
        )
        await audit_outbound(
            vendor=self.vendor, action="comment", ticket=ticket,
            request={"body_len": len(body)}, response=resp,
            status_code=status,
        )
        if status < 200 or status >= 300:
            raise AdapterError(
                self.vendor, "comment",
                f"comment HTTP {status}",
                status_code=status, response=resp,
            )
        return {
            "ok": True, "vendor": self.vendor, "ticket": ticket,
            "id": (resp.get("id") if isinstance(resp, dict) else ""),
        }

    async def verify_webhook(self, headers: Mapping[str, str],
                             body: bytes) -> bool:
        if not self.webhook_secret:
            # Treat "no secret configured" as "no webhook accepted" — the
            # orchestrator falls back to operator auth in this mode.
            return False
        h = {k.lower(): v for k, v in headers.items()}
        auth = h.get("authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(
            auth[len("Bearer "):], self.webhook_secret,
        ):
            return True
        alt = h.get("x-jira-webhook-secret", "")
        if alt and hmac.compare_digest(alt, self.webhook_secret):
            return True
        return False

    def parse_webhook(self, body: dict[str, Any]) -> tuple[str, str]:
        """Extract (ticket_key, story_text) from a JIRA webhook body.

        Delegates to ``orchestrator_gateway.parse_jira_webhook`` so both
        the legacy + intent paths agree on the JIRA ADF description
        flattener.
        """
        from backend.orchestrator_gateway import parse_jira_webhook
        return parse_jira_webhook(body)

    # ─── internal helpers ─────────────────────────────────────────

    def _payload_to_issue(self, parent: str, project: str,
                          p: SubtaskPayload) -> dict[str, Any]:
        fm = self.field_map
        issue_fields: dict[str, Any] = {
            "project": {"key": project},
            "parent":  {"key": parent},
            "summary": p.title,
            "description": _render_description(p),
            "issuetype": {"name": "Sub-task"},
            fm.impact_scope_allowed:    list(p.impact_scope_allowed),
            fm.impact_scope_forbidden:  list(p.impact_scope_forbidden),
            fm.acceptance_criteria:     p.acceptance_criteria,
            fm.handoff_protocol:        list(p.handoff_protocol),
            fm.domain_context:          p.domain_context,
        }
        if p.labels:
            issue_fields["labels"] = list(p.labels)
        # Merge any adapter-specific extras without overwriting the
        # well-known fields above.
        for k, v in (p.extra or {}).items():
            issue_fields.setdefault(k, v)
        return {"fields": issue_fields}

    def _browse_url(self, key: str) -> str:
        if not self.base_url:
            return ""
        return f"{self.base_url}/browse/{key}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module-level helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_JIRA_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)-\d+$")


def _require_ticket(ticket: str) -> None:
    if not ticket or not _JIRA_KEY_RE.match(ticket or ""):
        raise AdapterError(
            "jira", "validate",
            f"ticket {ticket!r} does not match PROJ-123 format",
        )


def _extract_project_key(ticket: str) -> str:
    m = _JIRA_KEY_RE.match(ticket)
    return m.group(1) if m else ""


def _match_transition(transitions: list[Any], target_name: str
                      ) -> str | None:
    """Find the best JIRA transition for ``target_name``.

    Prefers exact case-insensitive match on ``to.name`` (the resulting
    status), then on the transition ``name`` itself, then substring.
    """
    t_lower = target_name.lower()
    for strict in (True, False):
        for t in transitions:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            to = t.get("to") or {}
            to_name = (to.get("name") if isinstance(to, dict) else "") or ""
            tname = t.get("name") or ""
            if strict:
                if to_name.lower() == t_lower or tname.lower() == t_lower:
                    return tid
            else:
                if (t_lower in to_name.lower()
                        or t_lower in tname.lower()):
                    return tid
    return None


def _coerce_description(desc: Any) -> str:
    """Flatten a JIRA description which can be plain text or ADF dict."""
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc
    if isinstance(desc, list):
        return "\n".join(_coerce_description(d) for d in desc if d)
    if isinstance(desc, dict):
        if isinstance(desc.get("text"), str):
            return desc["text"]
        inner = desc.get("content") or desc.get("children") or []
        return _coerce_description(inner)
    return str(desc)


def _render_description(p: SubtaskPayload) -> str:
    """Human-readable description used when the custom-field path is
    disabled or the JIRA project schema rejects our custom fields."""
    lines = [
        "*Acceptance Criteria*",
        p.acceptance_criteria,
        "",
        "*impact_scope.allowed*",
        *(f"- `{g}`" for g in p.impact_scope_allowed),
    ]
    if p.impact_scope_forbidden:
        lines += [
            "",
            "*impact_scope.forbidden*",
            *(f"- `{g}`" for g in p.impact_scope_forbidden),
        ]
    if p.handoff_protocol:
        lines += [
            "",
            "*handoff_protocol*",
            *(f"- {step}" for step in p.handoff_protocol),
        ]
    if p.domain_context:
        lines += ["", "*domain_context*", p.domain_context]
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Factory — reads config.settings at first call
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_default_jira_adapter() -> JiraAdapter:
    """Build a JiraAdapter from ``backend.config.settings`` + env.

    O10 (#273): JIRA tokens are preferentially resolved via the
    Fernet-encrypted ``backend.secret_store`` so they never live in
    plaintext on disk / in the settings dump.  Resolution order:

      1. ``OMNISIGHT_JIRA_TOKEN_CIPHERTEXT`` env (ciphertext straight
         from ``secret_store.encrypt``) — preferred.
      2. ``notification_jira_token`` from settings (plaintext) —
         legacy fallback; logs a warning so operators migrate.
      3. Empty string — adapter returned but unauthenticated; calls
         will return 401.
    """
    from backend.config import settings
    token = _resolve_jira_token(settings)
    return JiraAdapter(
        base_url=getattr(settings, "notification_jira_url", "") or "",
        token=token,
        auth_mode=os.environ.get("OMNISIGHT_JIRA_AUTH_MODE", "bearer"),
        project_key=getattr(settings, "notification_jira_project", "") or "",
        webhook_secret=getattr(settings, "jira_webhook_secret", "") or "",
        field_map=JiraFieldMap.from_env(),
    )


def _resolve_jira_token(settings) -> str:
    """Prefer the encrypted source; fall back to plaintext with a warn."""
    ciphertext = (os.environ.get("OMNISIGHT_JIRA_TOKEN_CIPHERTEXT") or "").strip()
    if ciphertext:
        try:
            from backend import secret_store
            return secret_store.decrypt(ciphertext)
        except Exception as exc:
            logger.error(
                "O10: OMNISIGHT_JIRA_TOKEN_CIPHERTEXT failed to decrypt: %s; "
                "falling back to plaintext settings.notification_jira_token",
                exc,
            )
    plaintext = getattr(settings, "notification_jira_token", "") or ""
    if plaintext:
        logger.warning(
            "O10: notification_jira_token is set in plaintext; migrate to "
            "OMNISIGHT_JIRA_TOKEN_CIPHERTEXT (Fernet via backend.secret_store) "
            "before GA.  Token fingerprint: %s",
            _jira_token_fingerprint(plaintext),
        )
    return plaintext


def _jira_token_fingerprint(token: str) -> str:
    """Safe-to-log fingerprint — last 4 chars only, never the head.

    Delegates to ``secret_store.fingerprint`` when available so the
    fingerprint style is consistent across the app; falls back to a
    local impl so the adapter stays importable even if secret_store
    hasn't initialised (e.g. during config validation)."""
    try:
        from backend import secret_store
        return secret_store.fingerprint(token)
    except Exception:
        if len(token) <= 8:
            return "****"
        return f"…{token[-4:]}"


def describe_jira_token(settings=None) -> dict[str, Any]:
    """Operator-facing summary for the integration-status endpoint.

    Returns ``{"configured": bool, "source": "encrypted"|"plaintext"|"none",
    "fingerprint": "…abcd"}``.  No plaintext tokens leak."""
    from backend.config import settings as default_settings
    settings = settings or default_settings
    ct = (os.environ.get("OMNISIGHT_JIRA_TOKEN_CIPHERTEXT") or "").strip()
    if ct:
        try:
            from backend import secret_store
            plain = secret_store.decrypt(ct)
            return {
                "configured": True,
                "source": "encrypted",
                "fingerprint": _jira_token_fingerprint(plain),
            }
        except Exception as exc:
            return {
                "configured": True,
                "source": "encrypted",
                "fingerprint": "****",
                "error": f"decrypt_failed: {exc}",
            }
    plain = getattr(settings, "notification_jira_token", "") or ""
    if plain:
        return {
            "configured": True,
            "source": "plaintext",
            "fingerprint": _jira_token_fingerprint(plain),
        }
    return {"configured": False, "source": "none", "fingerprint": ""}


__all__ = [
    "DEFAULT_JIRA_STATUS_MAP",
    "JiraAdapter",
    "JiraFieldMap",
    "build_default_jira_adapter",
    "describe_jira_token",
]
