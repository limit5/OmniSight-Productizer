"""O5 (#268) — GitLab Issues IntentSource adapter (secondary — #858).

GitLab's REST v4 has first-class issue + sub-issue semantics but keeps
them independent — the adapter therefore follows the same pattern as
``github_adapter``: create N Issues, append a parent checklist.

Ticket shape: ``"group/project#iid"`` — matches GitLab's own ``@iid``
convention and lets us reuse ``url_encoded_path = quote_plus(project)``
in the REST URL.

Webhook verification uses the ``X-Gitlab-Token`` shared secret (GitLab's
native mechanism — no HMAC signature).
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import urllib.parse
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


DEFAULT_GITLAB_STATUS_MAP: dict[IntentStatus, str] = {
    IntentStatus.backlog:     "backlog",
    IntentStatus.in_progress: "in_progress",
    IntentStatus.reviewing:   "reviewing",
    IntentStatus.blocked:     "blocked",
    IntentStatus.done:        "done",
}


_TICKET_RE = re.compile(
    r"^(?P<project>[A-Za-z0-9_.\-/]+)#(?P<iid>\d+)$"
)


def parse_gitlab_ticket(ticket: str) -> tuple[str, int]:
    m = _TICKET_RE.match(ticket or "")
    if not m:
        raise AdapterError(
            "gitlab", "validate",
            f"ticket {ticket!r} must be 'group/project#iid'",
        )
    return m.group("project"), int(m.group("iid"))


@dataclass
class GitlabAdapter:
    """GitLab Issues IntentSource.

    Parameters
    ----------
    token:
        GitLab PAT with ``api`` scope.
    api_base:
        Base URL (no trailing ``/api/v4``).  Default
        ``https://gitlab.com``; override for self-hosted.
    webhook_secret:
        Shared secret for ``X-Gitlab-Token`` header equality check.
    default_project:
        Optional ``"group/project"`` path so bare ``#42`` tickets work.
    http_call:
        Injectable transport — default ``curl_json_call``.
    """

    token: str = ""
    api_base: str = "https://gitlab.com"
    webhook_secret: str = ""
    default_project: str = ""
    http_call: HttpCall = curl_json_call
    status_map: dict[IntentStatus, str] = field(
        default_factory=lambda: dict(DEFAULT_GITLAB_STATUS_MAP)
    )
    vendor: str = "gitlab"

    # ─── helpers ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            h["PRIVATE-TOKEN"] = self.token
        return h

    async def _api(self, method: str, path: str,
                   body: Any | None = None) -> tuple[int, Any]:
        url = self.api_base.rstrip("/") + "/api/v4" + path
        encoded = (json.dumps(body, ensure_ascii=False).encode("utf-8")
                   if body is not None else None)
        status, raw, _ = await self.http_call(
            method, url, self._headers(), encoded,
        )
        try:
            decoded: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            decoded = raw.decode("utf-8", errors="replace") if raw else ""
        return status, decoded

    def _coerce_ticket(self, ticket: str) -> str:
        if ticket and ticket.startswith("#") and self.default_project:
            return f"{self.default_project}{ticket}"
        return ticket

    @staticmethod
    def _encode_project(project: str) -> str:
        return urllib.parse.quote(project, safe="")

    # ─── IntentSource surface ─────────────────────────────────────

    async def fetch_story(self, ticket: str) -> IntentStory:
        t = self._coerce_ticket(ticket)
        project, iid = parse_gitlab_ticket(t)
        path = f"/projects/{self._encode_project(project)}/issues/{iid}"
        status, body = await self._api("GET", path)
        await audit_outbound(
            vendor=self.vendor, action="fetch_story", ticket=t,
            request={"method": "GET", "path": path},
            response=body, status_code=status,
        )
        if status < 200 or status >= 300 or not isinstance(body, dict):
            raise AdapterError(
                self.vendor, "fetch_story",
                f"HTTP {status} for {t}",
                status_code=status, response=body,
            )
        return IntentStory(
            vendor=self.vendor,
            ticket=t,
            summary=body.get("title") or "",
            description=body.get("description") or "",
            priority="",
            labels=list(body.get("labels") or []),
            raw=body,
        )

    async def create_subtasks(self, parent: str,
                              payloads: list[SubtaskPayload],
                              ) -> list[SubtaskRef]:
        t = self._coerce_ticket(parent)
        project, parent_iid = parse_gitlab_ticket(t)
        if not payloads:
            return []

        project_enc = self._encode_project(project)
        refs: list[SubtaskRef] = []
        for p in payloads:
            issue_body = {
                "title": p.title,
                "description": _render_body(parent, p),
                "labels": ",".join(sorted(
                    {"omnisight-subtask",
                     f"parent:{parent_iid}", *p.labels}
                )),
            }
            status, resp = await self._api(
                "POST", f"/projects/{project_enc}/issues", issue_body,
            )
            await audit_outbound(
                vendor=self.vendor, action="create_subtask", ticket=t,
                request=issue_body, response=resp, status_code=status,
            )
            if status < 200 or status >= 300 or not isinstance(resp, dict):
                raise AdapterError(
                    self.vendor, "create_subtasks",
                    f"create sub-task failed: HTTP {status}",
                    status_code=status, response=resp,
                )
            iid = resp.get("iid", 0)
            if not iid:
                continue
            refs.append(SubtaskRef(
                vendor=self.vendor,
                ticket=f"{project}#{iid}",
                url=resp.get("web_url", ""),
                parent=t,
                extra={"id": resp.get("id", "")},
            ))

        if refs:
            checklist = "\n".join(
                f"- [ ] {r.ticket}" for r in refs
            )
            await self.comment(
                t,
                "<!-- omnisight:subtasks -->\n"
                "## OmniSight sub-tasks\n" + checklist,
            )

        return refs

    async def update_status(self, ticket: str, status: IntentStatus,
                            *, comment: str = "") -> dict[str, Any]:
        t = self._coerce_ticket(ticket)
        project, iid = parse_gitlab_ticket(t)
        project_enc = self._encode_project(project)

        label = f"status::{self.status_map.get(status, status.value)}"
        state_event = "close" if status == IntentStatus.done else "reopen"
        body = {
            "state_event": state_event,
            "add_labels": label,
        }
        http_status, resp = await self._api(
            "PUT", f"/projects/{project_enc}/issues/{iid}", body,
        )
        await audit_outbound(
            vendor=self.vendor, action="update_status", ticket=t,
            request={"state_event": state_event,
                     "add_labels": label,
                     "target": status.value},
            response=resp, status_code=http_status,
        )
        if http_status < 200 or http_status >= 300:
            raise AdapterError(
                self.vendor, "update_status",
                f"PUT issue HTTP {http_status}",
                status_code=http_status, response=resp,
            )
        if comment:
            await self.comment(t, comment)
        return {
            "ok": True, "vendor": self.vendor, "ticket": t,
            "status": status.value, "state_event": state_event,
            "label": label,
        }

    async def comment(self, ticket: str, body: str) -> dict[str, Any]:
        t = self._coerce_ticket(ticket)
        project, iid = parse_gitlab_ticket(t)
        if not body:
            raise AdapterError(
                self.vendor, "comment",
                "refusing to post an empty comment",
            )
        request_body = {"body": body}
        path = (f"/projects/{self._encode_project(project)}"
                f"/issues/{iid}/notes")
        status, resp = await self._api("POST", path, request_body)
        await audit_outbound(
            vendor=self.vendor, action="comment", ticket=t,
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
            "ok": True, "vendor": self.vendor, "ticket": t,
            "id": (resp.get("id") if isinstance(resp, dict) else ""),
        }

    async def verify_webhook(self, headers: Mapping[str, str],
                             body: bytes) -> bool:
        if not self.webhook_secret:
            return False
        h = {k.lower(): v for k, v in headers.items()}
        token = h.get("x-gitlab-token", "")
        return bool(token) and hmac.compare_digest(token, self.webhook_secret)

    def parse_webhook(self, body: dict[str, Any]) -> tuple[str, str]:
        attrs = body.get("object_attributes") or {}
        project = (body.get("project") or {}).get("path_with_namespace") or \
            self.default_project
        iid = attrs.get("iid") or 0
        ticket = f"{project}#{iid}" if project and iid else ""
        title = attrs.get("title") or ""
        description = attrs.get("description") or ""
        text = "\n\n".join(s for s in (title, description) if s)
        return (ticket, text)


def _render_body(parent: str, p: SubtaskPayload) -> str:
    lines = [
        f"**Parent:** {parent}",
        "",
        "### Acceptance Criteria",
        p.acceptance_criteria,
        "",
        "### impact_scope.allowed",
        *(f"- `{g}`" for g in p.impact_scope_allowed),
    ]
    if p.impact_scope_forbidden:
        lines += ["", "### impact_scope.forbidden",
                  *(f"- `{g}`" for g in p.impact_scope_forbidden)]
    if p.handoff_protocol:
        lines += ["", "### handoff_protocol",
                  *(f"- {step}" for step in p.handoff_protocol)]
    if p.domain_context:
        lines += ["", "### domain_context", p.domain_context]
    return "\n".join(lines)


def build_default_gitlab_adapter() -> GitlabAdapter:
    from backend.config import settings
    return GitlabAdapter(
        token=getattr(settings, "gitlab_token", "") or "",
        api_base=(getattr(settings, "gitlab_url", "") or "https://gitlab.com"),
        webhook_secret=getattr(settings, "gitlab_webhook_secret", "") or "",
    )


__all__ = [
    "DEFAULT_GITLAB_STATUS_MAP",
    "GitlabAdapter",
    "build_default_gitlab_adapter",
    "parse_gitlab_ticket",
]
