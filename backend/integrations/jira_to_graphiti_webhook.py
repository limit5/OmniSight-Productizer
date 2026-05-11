"""OP-901 — JIRA changelog webhook forwarding to Graphiti.

JIRA remains the source of truth for ticket state. This adapter verifies
the inbound JIRA webhook token, normalizes the changelog payload, and
forwards it to Graphiti's service-side ``/ingest/jira`` endpoint with the
Graphiti MCP bearer token.
"""
from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class GraphitiWebhookConfigError(RuntimeError):
    """Required Graphiti/JIRA webhook configuration is missing."""


class GraphitiIngestionWebhookUnreachable(RuntimeError):
    """Graphiti's ingestion endpoint could not accept the event."""


@dataclass(frozen=True)
class GraphitiWebhookConfig:
    graphiti_base_url: str
    graphiti_token: str
    jira_webhook_secret: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "GraphitiWebhookConfig":
        src = env if env is not None else os.environ
        base_url = src.get(
            "OMNISIGHT_MCP_GRAPHITI_URL",
            "https://mcp-graphiti.sora.services",
        ).rstrip("/")
        token = src.get("OMNISIGHT_MCP_GRAPHITI_TOKEN", "").strip()
        jira_secret = src.get("OMNISIGHT_JIRA_WEBHOOK_SECRET", "").strip()
        if not token:
            raise GraphitiWebhookConfigError(
                "OMNISIGHT_MCP_GRAPHITI_TOKEN is required"
            )
        if not jira_secret:
            raise GraphitiWebhookConfigError(
                "OMNISIGHT_JIRA_WEBHOOK_SECRET is required"
            )
        return cls(
            graphiti_base_url=base_url,
            graphiti_token=token,
            jira_webhook_secret=jira_secret,
        )


def verify_jira_bearer(auth_header: str, expected_secret: str) -> bool:
    if not auth_header.startswith("Bearer "):
        return False
    actual = auth_header.removeprefix("Bearer ").strip()
    return bool(actual) and hmac.compare_digest(actual, expected_secret)


def normalize_jira_changelog(payload: dict[str, Any]) -> dict[str, Any]:
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    changelog = payload.get("changelog") or {}
    user = payload.get("user") or {}

    return {
        "source": "jira",
        "event": payload.get("webhookEvent") or payload.get("issue_event_type_name"),
        "issue_key": issue.get("key"),
        "issue_id": issue.get("id"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "updated": fields.get("updated"),
        "actor": {
            "account_id": user.get("accountId"),
            "display_name": user.get("displayName"),
        },
        "changelog": {
            "id": changelog.get("id"),
            "items": changelog.get("items") or [],
        },
        "raw": payload,
    }


def forward_jira_changelog_to_graphiti(
    payload: dict[str, Any],
    config: GraphitiWebhookConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    body = json.dumps(normalize_jira_changelog(payload)).encode()
    request = urllib.request.Request(
        f"{config.graphiti_base_url}/ingest/jira",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.graphiti_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with opener(request, timeout=config.timeout_seconds) as response:
            data = response.read().decode()
            return json.loads(data) if data else {"status": "ok"}
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise GraphitiIngestionWebhookUnreachable(str(exc)) from exc


def handle_jira_webhook(
    payload: dict[str, Any],
    *,
    authorization: str,
    config: GraphitiWebhookConfig,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if not verify_jira_bearer(authorization, config.jira_webhook_secret):
        return {"status": "rejected", "reason": "invalid_jira_webhook_token"}
    result = forward_jira_changelog_to_graphiti(
        payload,
        config,
        opener=opener,
    )
    return {"status": "forwarded", "graphiti": result}
