"""O5 (#268) — GitHub Issues IntentSource adapter (secondary — #858).

Vendor-agnostic fallback so customers without a JIRA tenant still get
sub-task tracking.  GitHub Issues has no first-class "sub-task" concept,
so the adapter emulates one:

  * ``create_subtasks`` creates N child Issues (labelled ``omnisight-subtask``
    + ``parent:<n>``) and appends a Markdown checklist to the parent body
    listing their numbers.  GitHub's new task-list-autolink renders it as
    a sub-issue hierarchy in the UI.
  * ``update_status`` sets ``state=closed|open`` and manages a single
    ``status:*`` label reflecting the ``IntentStatus``.
  * ``verify_webhook`` does HMAC-SHA256 validation on
    ``X-Hub-Signature-256`` — matches the existing /webhooks/github path.

All outbound calls flow through ``intent_source.audit_outbound``.

Ticket shape: ``"owner/repo#number"`` — chosen over bare numeric ids
because GitHub issues aren't globally unique and the orchestrator
handles multiple repos per tenant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
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


DEFAULT_GITHUB_STATUS_MAP: dict[IntentStatus, str] = {
    IntentStatus.backlog:     "backlog",
    IntentStatus.in_progress: "in_progress",
    IntentStatus.reviewing:   "reviewing",
    IntentStatus.blocked:     "blocked",
    IntentStatus.done:        "done",
}


_TICKET_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/#\s]+)#(?P<num>\d+)$")


def parse_github_ticket(ticket: str) -> tuple[str, str, int]:
    m = _TICKET_RE.match(ticket or "")
    if not m:
        raise AdapterError(
            "github", "validate",
            f"ticket {ticket!r} must be 'owner/repo#number'",
        )
    return m.group("owner"), m.group("repo"), int(m.group("num"))


@dataclass
class GithubAdapter:
    """GitHub Issues IntentSource.

    Parameters
    ----------
    token:
        GitHub PAT with ``issues:write`` scope.
    api_base:
        Usually ``https://api.github.com``; override for GHES.
    webhook_secret:
        Shared secret for HMAC-SHA256 signature verification.
    default_repo:
        Optional ``"owner/repo"`` that rewrites bare ``#42`` tickets.
        Handy for tenants that only work in one repo.
    http_call:
        Injectable transport — default ``curl_json_call``.
    """

    token: str = ""
    api_base: str = "https://api.github.com"
    webhook_secret: str = ""
    default_repo: str = ""
    http_call: HttpCall = curl_json_call
    status_map: dict[IntentStatus, str] = field(
        default_factory=lambda: dict(DEFAULT_GITHUB_STATUS_MAP)
    )
    vendor: str = "github"

    # ─── helpers ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _api(self, method: str, path: str,
                   body: Any | None = None) -> tuple[int, Any]:
        url = self.api_base.rstrip("/") + path
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
        if ticket and ticket.startswith("#") and self.default_repo:
            return f"{self.default_repo}{ticket}"
        return ticket

    # ─── IntentSource surface ─────────────────────────────────────

    async def fetch_story(self, ticket: str) -> IntentStory:
        t = self._coerce_ticket(ticket)
        owner, repo, num = parse_github_ticket(t)
        status, body = await self._api(
            "GET", f"/repos/{owner}/{repo}/issues/{num}",
        )
        await audit_outbound(
            vendor=self.vendor, action="fetch_story", ticket=t,
            request={"method": "GET",
                     "path": f"/repos/{owner}/{repo}/issues/{num}"},
            response=body, status_code=status,
        )
        if status < 200 or status >= 300 or not isinstance(body, dict):
            raise AdapterError(
                self.vendor, "fetch_story",
                f"HTTP {status} for {t}",
                status_code=status, response=body,
            )
        labels = [lb.get("name", "") for lb in (body.get("labels") or [])
                  if isinstance(lb, dict)]
        return IntentStory(
            vendor=self.vendor,
            ticket=t,
            summary=body.get("title") or "",
            description=body.get("body") or "",
            priority="",
            labels=labels,
            raw=body,
        )

    async def create_subtasks(self, parent: str,
                              payloads: list[SubtaskPayload],
                              ) -> list[SubtaskRef]:
        t = self._coerce_ticket(parent)
        owner, repo, parent_num = parse_github_ticket(t)
        if not payloads:
            return []

        refs: list[SubtaskRef] = []
        created_bodies: list[dict[str, Any]] = []
        for p in payloads:
            issue_body = {
                "title": p.title,
                "body": _render_body(parent, p),
                "labels": sorted({"omnisight-subtask",
                                  f"parent:{parent_num}", *p.labels}),
            }
            created_bodies.append(issue_body)
            status, resp = await self._api(
                "POST", f"/repos/{owner}/{repo}/issues", issue_body,
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
            number = resp.get("number", 0)
            if not number:
                continue
            refs.append(SubtaskRef(
                vendor=self.vendor,
                ticket=f"{owner}/{repo}#{number}",
                url=resp.get("html_url", ""),
                parent=t,
                extra={"node_id": resp.get("node_id", "")},
            ))

        # Append a task-list checklist to the parent so GitHub renders
        # the hierarchy and closing a child ticks the box.
        if refs:
            checklist = "\n".join(
                f"- [ ] {r.ticket}" for r in refs
            )
            append = (
                "\n\n<!-- omnisight:subtasks -->\n"
                "## OmniSight sub-tasks\n" + checklist
            )
            await self.comment(t, append)

        return refs

    async def update_status(self, ticket: str, status: IntentStatus,
                            *, comment: str = "") -> dict[str, Any]:
        t = self._coerce_ticket(ticket)
        owner, repo, num = parse_github_ticket(t)

        label = f"status:{self.status_map.get(status, status.value)}"
        state = "closed" if status == IntentStatus.done else "open"

        request_body = {"state": state, "labels": [label]}
        http_status, body = await self._api(
            "PATCH", f"/repos/{owner}/{repo}/issues/{num}",
            {"state": state},
        )
        await audit_outbound(
            vendor=self.vendor, action="update_status_state", ticket=t,
            request={"state": state, "target": status.value},
            response=body, status_code=http_status,
        )
        if http_status < 200 or http_status >= 300:
            raise AdapterError(
                self.vendor, "update_status",
                f"state transition HTTP {http_status}",
                status_code=http_status, response=body,
            )

        # Replace the status:* label so there's only one at a time.
        lstatus, lbody = await self._api(
            "PUT", f"/repos/{owner}/{repo}/issues/{num}/labels",
            {"labels": [label]},
        )
        await audit_outbound(
            vendor=self.vendor, action="update_status_label", ticket=t,
            request={"labels": [label]}, response=lbody,
            status_code=lstatus,
        )
        if lstatus < 200 or lstatus >= 300:
            logger.warning("github set label failed: %s for %s", lstatus, t)

        if comment:
            await self.comment(t, comment)

        return {
            "ok": True, "vendor": self.vendor, "ticket": t,
            "status": status.value, "state": state, "label": label,
        }

    async def comment(self, ticket: str, body: str) -> dict[str, Any]:
        t = self._coerce_ticket(ticket)
        owner, repo, num = parse_github_ticket(t)
        if not body:
            raise AdapterError(
                self.vendor, "comment",
                "refusing to post an empty comment",
            )
        request_body = {"body": body}
        status, resp = await self._api(
            "POST", f"/repos/{owner}/{repo}/issues/{num}/comments",
            request_body,
        )
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
        sig = h.get("x-hub-signature-256", "")
        if not sig:
            return False
        expected = "sha256=" + hmac.new(
            self.webhook_secret.encode("utf-8"), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    def parse_webhook(self, body: dict[str, Any]) -> tuple[str, str]:
        issue = body.get("issue") or {}
        repo = (body.get("repository") or {}).get("full_name") or \
            self.default_repo
        number = issue.get("number") or body.get("number") or 0
        ticket = f"{repo}#{number}" if repo and number else ""
        title = issue.get("title") or ""
        body_text = issue.get("body") or ""
        text = "\n\n".join(s for s in (title, body_text) if s)
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


def build_default_github_adapter() -> GithubAdapter:
    from backend.config import settings
    return GithubAdapter(
        token=getattr(settings, "github_token", "") or "",
        webhook_secret=getattr(settings, "github_webhook_secret", "") or "",
    )


__all__ = [
    "DEFAULT_GITHUB_STATUS_MAP",
    "GithubAdapter",
    "build_default_github_adapter",
    "parse_github_ticket",
]
