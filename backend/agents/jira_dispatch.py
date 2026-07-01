"""JIRA dispatch — shared module for runner ticket pickup, transitions, comments.

Per ``docs/sop/jira-ticket-conventions.md`` §16. Replaces the TODO.md
parsing logic in ``auto-runner-codex.py`` with JIRA REST-driven
discovery. Used by ``auto-runner-jira.py`` as the dispatch backbone.

Public API:
- ``fetch_pickable_tickets(agent_class)`` — JQL search per §16, parsed
  into TicketSnapshot objects (compatible with backend.agents.scheduler)
- ``transition_to_in_progress(key, bot_account_id)`` — TODO → In Progress
- ``transition_to_under_review(key, gerrit_url)`` — In Progress → Under Review
- ``transition_back_to_todo(key, reason)`` — In Progress → TODO (revert)
- ``add_comment(key, body)`` — append ADF comment
- ``parse_prerequisites(ticket)`` — extract YAML block from description

Authentication: reads ``~/.config/omnisight/jira-claude.env`` /
``~/.config/omnisight/jira-codex.env`` etc. based on agent_class.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from backend.config import settings
from backend.agents import (
    capability_registry,
    feature_dup_detector,
    model_deconfliction,
    provider_orchestrator,
    runner_coordination,
    runner_progress,
    runner_sandbox,
)
from backend.agents.circuit_breaker import BREAKERS
from backend.agents.delivery_target import (
    DeliveryTarget,
    DeliveryTargetError,
    resolve_delivery_credential,
    resolve_delivery_target,
)
from backend.agents.idempotency import DEFAULT_STORE
from backend.agents import routed_repo
from backend.agents.routed_repo import RoutedRepo
from backend.agents.scheduler import TicketSnapshot
from backend.agents.scope_to_paths import (
    ALWAYS_TOUCHED,
    ALWAYS_TOUCHED_TEMPLATE,
    FILES_SECTION_RE,
    HOT_FILES,
    PATH_TOKEN_RE,
    SCOPE_TO_PATHS,
    parse_files_section,
)

log = logging.getLogger(__name__)

PS_STALENESS_WARN_DAYS = 7.0
PS_STALENESS_WARN_COMMITS = 25
PS_STALENESS_ABSTAIN_DAYS = 14.0
PS_STALENESS_ABSTAIN_COMMITS = 100

MIGRATION_IN_FLIGHT_LABEL = "migration:in-flight"
MIGRATION_OVERRIDE_LABEL = "migration:override"
MIGRATION_SCOPE_PREFIX = "migration:scope="

# ADR-0033 §6 / S12.G v2 spec §3.6: runners (L3) MUST refuse pickup of any
# ticket carrying a ``class:operator-window-*`` or ``class:operator-rehearsal``
# label, even when the same ticket also carries ``class:subscription-*``.
# Refusal is silent — no JIRA comment, only a structured audit log line — so
# operators (L1/L2) can drive the ticket without runner interference.
REFUSAL_LABEL_PREFIXES = ("class:operator-window-", "class:operator-rehearsal")


def _runner_refuses_pickup(labels: list[str]) -> tuple[bool, str | None]:
    """Return ``(True, matching_label)`` if any label triggers L3 refusal.

    Operator-window-* / operator-rehearsal labels always win over a
    co-present ``class:subscription-*`` — the runner refuses pickup so the
    ticket stays available for the operator's window.
    """
    for label in labels:
        if not isinstance(label, str):
            continue
        for prefix in REFUSAL_LABEL_PREFIXES:
            if label.startswith(prefix):
                return True, label
    return False, None


def _emit_runner_refusal_audit(ticket_key: str, refusal_label: str) -> None:
    """Emit structured ``runner_refusal_by_class`` event — no JIRA write."""
    log.info(
        "runner_refusal_by_class %s",
        json.dumps(
            {
                "event": "runner_refusal_by_class",
                "ticket_key": ticket_key,
                "refusal_label": refusal_label,
                "runner_instance": _instance_id_from_env(),
            },
            sort_keys=True,
        ),
    )


def _emit_deconfliction_refusal_audit(
    ticket_key: str,
    agent_class: str,
    decision: "model_deconfliction.DispatchDecision",
) -> None:
    """Emit structured ``runner_deconfliction_refusal`` event — no JIRA write.

    Mirrors :func:`_emit_runner_refusal_audit` so operators can grep both
    refusal modes from the same audit feed. The blocking incident
    metadata (failure class + runner that failed) is recorded so the
    operator can confirm the thrash-prevention decision without
    re-running the ticket.
    """
    blocking = decision.blocking_incident
    log.info(
        "runner_deconfliction_refusal %s",
        json.dumps(
            {
                "event": "runner_deconfliction_refusal",
                "ticket_key": ticket_key,
                "agent_class": agent_class,
                "reason": decision.reason,
                "blocking_failure_class": (
                    blocking.failure_class.value if blocking else None
                ),
                "blocking_runner_class": (
                    blocking.runner_class if blocking else None
                ),
                "blocking_incident_id": (
                    blocking.incident_id if blocking else None
                ),
                "runner_instance": _instance_id_from_env(),
            },
            sort_keys=True,
        ),
    )

# ── Auth + config per agent_class ─────────────────────────────────

CRED_DIR = Path("~/.config/omnisight").expanduser()

# Per OP-783: a single host can run N runner processes, each pinned to its
# own bot account (codex-bot, codex-bot-2, codex-bot-3, claude-bot, ...).
# The instance_id distinguishes the processes; "default" = the legacy
# single-instance setup and MUST keep the existing cred / state-file paths.
DEFAULT_INSTANCE_ID = "default"


def _instance_id_from_env() -> str:
    """Resolve runner instance ID from env, defaulting to 'default'.

    Empty string is treated as unset to make ``unset OMNISIGHT_RUNNER_INSTANCE_ID``
    indistinguishable from setting it to ``default`` — matching how the
    auto-runner loads the variable.
    """
    return os.environ.get("OMNISIGHT_RUNNER_INSTANCE_ID", "").strip() or DEFAULT_INSTANCE_ID


_BASE_BOT_BY_CLASS = {
    "subscription-codex": "codex-bot",
    "api-openai": "codex-bot",
    "subscription-claude": "claude-bot",
    "api-anthropic": "claude-bot",
    # Gemini/Antigravity brain (dogfood 2026-07-01). Gerrit key resolves to
    # ~/.config/omnisight/gerrit-gemini-bot-ed25519 via _gerrit_ssh_key_for_bot.
    "subscription-gemini": "gemini-bot",
    # Grok/xAI brain (dogfood 2026-07-01). Key ~/.config/omnisight/gerrit-grok-bot-ed25519.
    "subscription-grok": "grok-bot",
}


def resolve_bot_username(agent_class: str, instance_id: str | None = None) -> str:
    """Map (agent_class, instance_id) → Gerrit/JIRA bot username.

    Default instance returns the legacy bare names (``codex-bot`` /
    ``claude-bot``). Non-default instances append the ID (``codex-bot-2``,
    ``claude-bot-3``...). The bot-username is the canonical instance key
    used downstream for SSH key paths, JIRA cred files, backpressure
    state, and idempotency DBs.
    """
    if instance_id is None:
        instance_id = _instance_id_from_env()
    base = _BASE_BOT_BY_CLASS.get(agent_class)
    if base is None:
        raise ValueError(f"unknown agent_class: {agent_class}")
    if instance_id == DEFAULT_INSTANCE_ID:
        return base
    return f"{base}-{instance_id}"


def _gerrit_ssh_key_for_bot(bot_username: str) -> Path:
    """Per-bot SSH private key path: ``~/.config/omnisight/gerrit-<bot>-ed25519``."""
    return CRED_DIR / f"gerrit-{bot_username}-ed25519"


def _gerrit_auth_for_instance(
    agent_class: str, instance_id: str | None = None
) -> tuple[str, Path]:
    """Return (bot_username, ssh_key_path) for (agent_class, instance_id).

    For ``instance_id == "default"`` returns the legacy entry from
    ``_GERRIT_AUTH_BY_CLASS`` so existing ssh-key paths are preserved
    bit-for-bit. For non-default instances, derives a per-bot path
    (``gerrit-codex-bot-2-ed25519`` etc.).
    """
    if instance_id is None:
        instance_id = _instance_id_from_env()
    if instance_id == DEFAULT_INSTANCE_ID:
        legacy = _GERRIT_AUTH_BY_CLASS.get(agent_class)
        if legacy is not None:
            return legacy
    bot_username = resolve_bot_username(agent_class, instance_id)
    return bot_username, _gerrit_ssh_key_for_bot(bot_username)


def _cred_paths(agent_class: str, instance_id: str | None = None) -> tuple[Path, Path]:
    """Return (env_file, token_file) for (agent_class, instance_id).

    Default instance keeps legacy filenames (``jira-codex.env`` /
    ``jira-claude.env``) so existing single-instance setups continue to
    work without re-provisioning. Non-default instances use bot-keyed
    filenames (``jira-codex-bot-2.env``, ``jira-claude-bot-3.env`` ...).
    """
    if instance_id is None:
        instance_id = _instance_id_from_env()
    if instance_id == DEFAULT_INSTANCE_ID:
        if agent_class in ("subscription-codex", "api-openai"):
            return CRED_DIR / "jira-codex.env", CRED_DIR / "jira-codex-token"
        if agent_class == "subscription-gemini":
            return CRED_DIR / "jira-gemini.env", CRED_DIR / "jira-gemini-token"
        if agent_class == "subscription-grok":
            return CRED_DIR / "jira-grok.env", CRED_DIR / "jira-grok-token"
        return CRED_DIR / "jira-claude.env", CRED_DIR / "jira-claude-token"
    bot_username = resolve_bot_username(agent_class, instance_id)
    return CRED_DIR / f"jira-{bot_username}.env", CRED_DIR / f"jira-{bot_username}-token"


def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


@dataclass(frozen=True)
class DispatchClient:
    """Authenticated JIRA REST client scoped to one agent_class."""

    agent_class: str
    base_url: str
    project_key: str
    auth_header: str
    bot_account_id: str
    bot_email: str


def make_client(agent_class: str, instance_id: str | None = None) -> DispatchClient:
    if instance_id is None:
        instance_id = _instance_id_from_env()
    env_file, token_file = _cred_paths(agent_class, instance_id)
    env = _load_env(env_file)
    token = token_file.read_text().strip()
    # Email key resolution: prefer the instance-agnostic ``OMNISIGHT_JIRA_BOT_EMAIL``
    # (recommended for new per-instance .env files), then fall back to the legacy
    # class-specific keys so existing default-instance .env files keep working.
    legacy_key = "OMNISIGHT_JIRA_CLAUDE_EMAIL" if "claude" in env_file.name else "OMNISIGHT_JIRA_CODEX_EMAIL"
    email = env.get("OMNISIGHT_JIRA_BOT_EMAIL") or env.get(legacy_key)
    if not email:
        raise RuntimeError(
            f"{env_file} has no email key set; expected OMNISIGHT_JIRA_BOT_EMAIL "
            f"or {legacy_key}."
        )
    raw = f"{email}:{token}".encode()
    auth = "Basic " + b64encode(raw).decode()
    site = env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/")
    project = env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP")
    # accountId via /myself
    me = _request_raw("GET", site + "/rest/api/3/myself", auth, None)
    return DispatchClient(
        agent_class=agent_class,
        base_url=site + "/rest/api/3",
        project_key=project,
        auth_header=auth,
        bot_account_id=me["accountId"],
        bot_email=email,
    )


def _request_raw(
    method: str,
    url: str,
    auth_header: str,
    body: dict | None,
    idem_key: str | None = None,
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idem_key:
        headers["X-Atlassian-Token"] = "no-check"
        headers["X-Idempotency-Key"] = idem_key
    req = urllib.request.Request(
        url, data=data, method=method,
        headers=headers,
    )
    try:
        with BREAKERS["jira_rest"].call(urllib.request.urlopen, req, timeout=30) as resp:
            payload = resp.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        raise RuntimeError(f"{method} {url} → {e.code}: {body_text}") from e


def _request(
    client: DispatchClient,
    method: str,
    path: str,
    body: dict | None = None,
    idem_key: str | None = None,
) -> dict:
    return _request_raw(method, client.base_url + path, client.auth_header, body, idem_key)


def _request_idempotent(
    client: DispatchClient,
    method: str,
    path: str,
    body: dict | None,
    idem_key: str,
) -> dict:
    return DEFAULT_STORE.run(idem_key, lambda: _request(client, method, path, body, idem_key))


# ── ADF helpers ───────────────────────────────────────────────────


def _adf_paragraph(text: str) -> dict:
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# ── Ticket fetch + JQL ────────────────────────────────────────────

# Gotcha: JQL `issuetype = "ストーリー"` (JP-locale name) does NOT match
# Story-type issues even though /myself reports the localised name. JQL
# accepts the untranslated English `Story` or numeric ID. Verified
# 2026-05-06 against soraapp.atlassian.net OP project.
PICKUP_JQL_TEMPLATE = (
    'project = "{project}" '
    'AND issuetype = Story '
    'AND status = "To Do" '
    'AND assignee is EMPTY '
    'AND labels = "class:{cls}" '
    'AND status != "Waiting for External" '
    'AND labels not in ("tier:X") '
    'ORDER BY priority DESC, created ASC'
)


def fetch_pickable_tickets(client: DispatchClient, max_results: int = 50) -> list[dict]:
    """Run pickup JQL per §16. Returns raw issue dicts (not snapshots).

    Per ADR-0033 §6, tickets carrying any :data:`REFUSAL_LABEL_PREFIXES`
    label are silently dropped from the candidate list and emit a
    ``runner_refusal_by_class`` audit line. No JIRA comment is posted —
    operator-window tickets are L1/L2-only by design.

    Per OP-1117 (v2-Ⅹ-5e), tickets that another runner ``agent_class``
    failed on recently are dropped when the failure class is one where
    swapping models is unlikely to help (see
    :mod:`backend.agents.model_deconfliction`). A
    ``runner_deconfliction_refusal`` audit line records the decision.
    """
    # Lazy import: character_registry imports this module (for _BASE_BOT_BY_CLASS),
    # so a top-level import here would be circular.
    from backend.agents import character_registry
    jql = PICKUP_JQL_TEMPLATE.format(project=client.project_key, cls=client.agent_class)
    resp = _request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["summary", "labels", "status", "issuetype", "fixVersions",
                   "created", "components", "issuelinks", "parent"],
        "maxResults": max_results,
    })
    pickable: list[dict] = []
    for issue in resp.get("issues", []):
        ticket_key = issue.get("key", "?")
        labels = ((issue.get("fields") or {}).get("labels")) or []
        refused, refusal_label = _runner_refuses_pickup(labels)
        if refused:
            _emit_runner_refusal_audit(ticket_key, refusal_label)
            continue

        tier_denial = character_registry.character_tier_denial_from_labels(labels)
        if tier_denial is not None:
            log.info(
                "runner_character_tier_refusal %s",
                json.dumps(
                    {
                        "event": "runner_character_tier_refusal",
                        "ticket_key": ticket_key,
                        "reason": tier_denial,
                        "runner_instance": _instance_id_from_env(),
                    },
                    sort_keys=True,
                ),
            )
            continue

        quota_denial = capability_registry.quota_health_denial_from_labels(labels)
        if quota_denial is not None:
            log.info(
                "runner_quota_health_refusal %s",
                json.dumps(
                    {
                        "event": "runner_quota_health_refusal",
                        "ticket_key": ticket_key,
                        "reason": quota_denial,
                        "runner_instance": _instance_id_from_env(),
                    },
                    sort_keys=True,
                ),
            )
            continue

        decision = model_deconfliction.should_pickup_after_prior_failure(
            ticket_key=ticket_key,
            current_runner_class=client.agent_class,
        )
        if not decision.allowed:
            _emit_deconfliction_refusal_audit(
                ticket_key, client.agent_class, decision
            )
            continue

        pickable.append(issue)
    return pickable


def to_snapshot(issue: dict) -> TicketSnapshot:
    """Convert raw JIRA issue payload to TicketSnapshot for scheduler.

    Component lookup order: JIRA Component field (operator-set) →
    `priority:X` label (migration-set fallback) → "default".
    """
    f = issue["fields"]
    labels = f.get("labels", [])
    component = "default"
    components_field = f.get("components") or []
    if components_field:
        component = components_field[0].get("name", "default")
    else:
        for label in labels:
            if label.startswith("priority:"):
                component = label.split(":", 1)[1].upper()
                break
    fix_v = None
    if f.get("fixVersions"):
        fix_v = f["fixVersions"][0].get("name")
    created_str = f.get("created", "")
    try:
        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        days_since = (datetime.now(timezone.utc) - created_dt).total_seconds() / 86400
    except (ValueError, TypeError):
        days_since = 1.0
    days_to_fv: float | None = None
    if fix_v and re.match(r"^v\d", fix_v):
        # crude: assume 30 days from now if SemVer; operator can override later
        days_to_fv = 30.0
    mutex_labels = tuple(l for l in labels if l.startswith("mutex:"))
    # downstream blockers + mutex sibling: deferred to ticket-fetch enrichment
    return TicketSnapshot(
        key=issue["key"],
        component=component,
        fix_version=fix_v,
        created_at=created_str,
        days_since_created=days_since,
        days_to_fix_version=days_to_fv,
        downstream_blocked_count=0,  # enrichment via separate JQL pass (deferred)
        mutex_labels=mutex_labels,
        has_mutex_in_progress_sibling=False,  # deferred mutex check
        labels=tuple(labels),
    )


# ── State transitions ─────────────────────────────────────────────

# OP project workflow transition IDs (verified 2026-05-06 against
# soraapp.atlassian.net via /transitions endpoint). Must match Atlassian
# Cloud project's workflow config. If workflow changes, update this map
# AND `docs/sop/jira-ticket-conventions.md` §10 mapping table.
TRANSITION_IDS = {
    "to_in_progress": "21",      # JP locale: "進行中"
    "back_to_todo": "11",        # JP locale: "To Do"
    "to_under_review": "3",      # "Submit for Review" — In Progress → Under Review
    "to_approved": "4",          # "Approve" — Under Review → Approved
    "to_published": "7",         # "Deploy" — Approved → Published; bridge-only per ADR 0003
    "to_archived": "8",          # "Archive" — Published → Archived
}


# Gerrit endpoints (Track C verified 2026-05-05; reference_gerrit_self_hosted memory)
GERRIT_SSH_HOST = "sora.services"
GERRIT_SSH_PORT = 29418
GERRIT_PROJECT_PATH = "omnisight/OmniSight-Productizer"
GERRIT_HOOK_URL = "https://sora.services:29420/tools/hooks/commit-msg"


def _project_filter(gerrit_project: str | None) -> str:
    """R.4 (OP-2197): additive Gerrit ``project:`` query token.

    Returns ``"project:<x> "`` when *gerrit_project* is set, else ``""`` — so a
    ``None`` argument leaves the query byte-identical to the pre-R.4 default
    (the productizer-only fleet). When a routed ticket scopes a shared gate to
    its own project, conf changes no longer pause productizer runners, collide
    on same relative paths, or match the wrong open patch (the audit finding).
    """
    return f"project:{gerrit_project} " if gerrit_project else ""


def gerrit_project_from_url(gerrit_url: str) -> str:
    """Extract the Gerrit project path (e.g. ``omnisight/conference-appliance``)
    from a routed ``gerrit_url`` for use in a ``project:`` query filter."""
    from urllib.parse import urlparse

    path = urlparse(gerrit_url).path.lstrip("/")
    return path[:-4] if path.endswith(".git") else path


def gerrit_project_for_labels(labels) -> str | None:
    """The Gerrit project a ticket's gate queries should scope to.

    Returns the routed project (from a ``repo:<name>`` label) or ``None`` for a
    normal productizer ticket (gate stays at the unchanged default). Fail-soft:
    a malformed routed config returns ``None`` here rather than raising — the
    R.1 dispatch pre-gate is the place that abstains on an unresolved
    ``repo:`` label; a gate just falls back to the default scope.
    """
    try:
        routed = routed_repo.resolve_routed_repo(labels or ())
    except routed_repo.RoutedRepoError:
        return None
    return gerrit_project_from_url(routed.gerrit_url) if routed is not None else None

# agent_class → (gerrit username, ssh private key path).
# Memory: claude-bot for subscription-claude / api-anthropic; codex-bot for subscription-codex / api-openai.
_GERRIT_AUTH_BY_CLASS: dict[str, tuple[str, Path]] = {
    "subscription-codex":  ("codex-bot",  Path("~/.config/omnisight/gerrit-codex-bot-ed25519").expanduser()),
    "api-openai":          ("codex-bot",  Path("~/.config/omnisight/gerrit-codex-bot-ed25519").expanduser()),
    "subscription-claude": ("claude-bot", Path("~/.config/omnisight/gerrit-claude-bot-ed25519").expanduser()),
    "api-anthropic":       ("claude-bot", Path("~/.config/omnisight/gerrit-claude-bot-ed25519").expanduser()),
    # Gemini/Antigravity brain (dogfood 2026-07-01). Without this entry,
    # _gerrit_auth_for_bot("gemini-bot") raises "unknown Gerrit bot username"
    # and backpressure_decide → open_ps_count_for aborts every gemini pickup.
    "subscription-gemini": ("gemini-bot", Path("~/.config/omnisight/gerrit-gemini-bot-ed25519").expanduser()),
    # Grok/xAI brain (dogfood 2026-07-01).
    "subscription-grok": ("grok-bot", Path("~/.config/omnisight/gerrit-grok-bot-ed25519").expanduser()),
}


def _backpressure_state_file(
    agent_class: str, instance_id: str | None = None
) -> Path:
    """Per-instance backpressure latch path.

    Default instance keeps the legacy ``runner-backpressure-<class>.state``
    name (so an existing single-instance runner restart picks up the
    pre-existing latch). Non-default instances key on the bot username so
    each ``codex-bot-N`` has its own quota state machine and doesn't pause
    its siblings when its review queue saturates.
    """
    if instance_id is None:
        instance_id = _instance_id_from_env()
    if instance_id == DEFAULT_INSTANCE_ID:
        return Path(f"/tmp/runner-backpressure-{agent_class}.state")
    bot_username = resolve_bot_username(agent_class, instance_id)
    return Path(f"/tmp/runner-backpressure-{bot_username}.state")


def notify_operator(channel: str, severity: str, detail: str) -> None:
    """Runner-local operator alert hook.

    Kept deliberately small for OP-732: tests monkeypatch this function
    to assert single-fire behaviour, while production gets a grep-able
    stdout alert even if the broader notification stack is unavailable.
    """
    print(f"[{channel}] {severity}: {detail}")


def _gerrit_auth_for_bot(bot_username: str) -> tuple[str, Path]:
    """Resolve (bot_username, ssh_key) for a Gerrit username.

    Default-instance bots (``codex-bot``, ``claude-bot``) come from the
    static ``_GERRIT_AUTH_BY_CLASS`` table to preserve legacy ssh-key
    paths. Per-instance bots (``codex-bot-2`` etc., per OP-783) derive
    their key path via ``_gerrit_ssh_key_for_bot``.
    """
    for username, key_path in _GERRIT_AUTH_BY_CLASS.values():
        if username == bot_username:
            return username, key_path
    if bot_username.startswith(("codex-bot-", "claude-bot-", "gemini-bot-", "grok-bot-")):
        return bot_username, _gerrit_ssh_key_for_bot(bot_username)
    raise ValueError(f"unknown Gerrit bot username: {bot_username}")


def open_ps_count_for(bot_username: str, gerrit_project: str | None = None) -> int:
    """Return open Gerrit patchsets owned by ``bot_username``.

    R.4: pass *gerrit_project* to scope the count to one project (so a routed
    project's open changes don't inflate the productizer backpressure count and
    vice-versa). ``None`` → all projects (unchanged default)."""
    user, ssh_key = _gerrit_auth_for_bot(bot_username)
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(GERRIT_SSH_PORT),
        f"{user}@{GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON",
        f"{_project_filter(gerrit_project)}is:open owner:{bot_username}",
    ]
    out = BREAKERS["gerrit_ssh"].call(
        subprocess.run, cmd, capture_output=True, text=True, timeout=10
    )
    n = 0
    for line in out.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "stats":
            n = int(data.get("rowCount", 0) or 0)
    return n


def backpressure_decide(
    agent_class: str, instance_id: str | None = None
) -> tuple[bool, str]:
    """Hysteresis gate for runner pickup.

    Returns ``(ok_to_pick_up, reason)``. A state file records only the
    paused latch, so the runner re-notifies only when it crosses from
    active to paused.

    Per OP-783, the cap/floor and PS query are scoped to the per-instance
    bot account: ``codex-bot-2`` saturating its review queue does not
    pause ``codex-bot``. Default-instance behaviour (no env var, no
    instance_id) is byte-identical to the pre-OP-783 path.
    """
    if instance_id is None:
        instance_id = _instance_id_from_env()
    bot_username, _ = _gerrit_auth_for_instance(agent_class, instance_id)

    cap = settings.runner_ps_cap
    floor = settings.runner_ps_floor
    state_file = _backpressure_state_file(agent_class, instance_id)
    paused = state_file.exists() and state_file.read_text().strip() == "paused"
    n = open_ps_count_for(bot_username)

    instance_tag = (
        agent_class
        if instance_id == DEFAULT_INSTANCE_ID
        else f"{agent_class} (instance {instance_id}, {bot_username})"
    )

    if not paused and n >= cap:
        state_file.write_text("paused")
        notify_operator(
            channel="runner-alerts",
            severity="medium",
            detail=(
                f"{instance_tag} runner paused: {n} open PSes >= {cap}. "
                "Please review + +2 to drain the queue."
            ),
        )
        return False, f"{n} open PSes (cap {cap})"
    if paused and n <= floor:
        state_file.unlink()
        return True, f"resumed: {n} open PSes (floor {floor})"
    if paused:
        return False, f"{n} open PSes (cap {cap}, floor {floor})"
    return True, f"active: {n} open PSes (cap {cap})"


@dataclass(frozen=True)
class GerritPushResult:
    """Outcome of pushing a worktree HEAD to Gerrit refs/for/<target>."""
    success: bool
    change_number: int | None
    change_url: str | None
    detail: str
    recovery_note: str = ""
    post_push_warning: str | None = None


@dataclass(frozen=True)
class GerritMergedInfo:
    """Merged Gerrit sibling found for a JIRA key."""

    change_number: str
    subject: str


@dataclass(frozen=True)
class GerritChangeInfo:
    """Open or merged Gerrit change found for a Change-Id."""

    change_number: int
    change_url: str
    subject: str


def _gerrit_ssh_url(agent_class: str, instance_id: str | None = None) -> str:
    try:
        user, _ = _gerrit_auth_for_instance(agent_class, instance_id)
    except ValueError:
        user, _ = _GERRIT_AUTH_BY_CLASS["subscription-claude"]
    return f"ssh://{user}@{GERRIT_SSH_HOST}:{GERRIT_SSH_PORT}/{GERRIT_PROJECT_PATH}"


def _bot_email_for(agent_class: str, instance_id: str | None = None) -> str:
    """Per memory: bot accounts are rt3628+<bot-username>@gmail.com (plus-addressing
    to operator's primary inbox). Returns email for the (agent_class, instance_id)
    bot. Default instance preserves the legacy ``rt3628+codex-bot@gmail.com`` /
    ``rt3628+claude-bot@gmail.com`` mapping.
    """
    try:
        bot_user, _ = _gerrit_auth_for_instance(agent_class, instance_id)
    except ValueError:
        bot_user, _ = _GERRIT_AUTH_BY_CLASS["subscription-claude"]
    return f"rt3628+{bot_user}@gmail.com"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-tenant push identity (OP-1779 / 1A.2 — closes L6)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# design §7 (per-tenant credential row), §8 (JIRA/project-namespace), §3 (L6).
#
# The shared on-disk bot SSH key + the fixed OmniSight Gerrit project
# (``GERRIT_PROJECT_PATH``) are reserved for the internal ``omnisight-self``
# tenant. Any customer tenant MUST push via a credential resolved from its
# OWN ``git_accounts`` row (``git_credentials.get_credential_registry_async``)
# scoped to the tenant's own Gerrit project. If no such row exists the push
# fails closed rather than silently borrowing the bot key (L6): a customer run
# can never push to OmniSight's — or another tenant's — project.
#
# Scope note: this covers the *push* identity (the ``git push`` to
# ``refs/for/<target>`` and the recovery query / mergeability self-fix that
# hang off a push). The develop-baseline *fetch* in the 1A.1 sync pipeline is
# a separate concern and is intentionally left untouched here.


class TenantPushIdentityError(RuntimeError):
    """No tenant-scoped Gerrit push credential could be resolved.

    Raised (and caught) inside :func:`push_to_gerrit_for_review` for any
    non-``omnisight-self`` tenant when the per-tenant credential registry
    yields no usable Gerrit account, or yields one that would breach the L6
    isolation contract (shared bot key / OmniSight project / another tenant's
    row). The push is refused — never downgraded to the bot identity.
    """


@dataclass(frozen=True)
class GerritPushIdentity:
    """Resolved ``(user, key, host, port, project)`` used to push one change.

    ``is_bot`` marks the legacy internal identity (``omnisight-self``): the
    shared bot account + the OmniSight project. For a customer tenant every
    field comes from that tenant's own ``git_accounts`` row.
    """

    tenant_id: str
    ssh_user: str
    ssh_key: Path
    ssh_host: str
    ssh_port: int
    project: str
    is_bot: bool

    @property
    def ssh_url(self) -> str:
        return f"ssh://{self.ssh_user}@{self.ssh_host}:{self.ssh_port}/{self.project}"


def _shared_bot_key_paths() -> set[Path]:
    """Resolved paths of every shared default-instance bot SSH key."""
    return {
        Path(key_path).expanduser().resolve(strict=False)
        for _user, key_path in _GERRIT_AUTH_BY_CLASS.values()
    }


def _is_shared_bot_key(ssh_key: Path) -> bool:
    """True if *ssh_key* is (or looks like) a shared bot key under CRED_DIR.

    Matches both the static default-instance table and the per-instance
    ``gerrit-(claude|codex)-bot*`` naming convention so a customer credential
    row can never smuggle the shared bot key in by path.
    """
    resolved = ssh_key.expanduser().resolve(strict=False)
    if resolved in _shared_bot_key_paths():
        return True
    cred_dir = CRED_DIR.resolve(strict=False)
    name = resolved.name
    return resolved.parent == cred_dir and (
        name.startswith("gerrit-claude-bot") or name.startswith("gerrit-codex-bot")
    )


def _run_coro(coro: Any) -> Any:
    """Drive an async coroutine to completion from this sync push path.

    :func:`push_to_gerrit_for_review` is sync but the canonical credential
    registry read (``get_credential_registry_async``) is async. Mirrors the
    bridge in :mod:`backend.agents.cognee_integration`: run inline when no
    loop is active, else hand off to a private loop in a worker thread.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            box["exc"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "exc" in box:
        raise box["exc"]
    return box.get("value")


def _resolve_tenant_gerrit_account(tenant_id: str) -> Optional[dict]:
    """Return the customer tenant's OWN enabled Gerrit ``git_accounts`` row.

    Reads the canonical per-tenant registry. Only rows that genuinely belong
    to *tenant_id* are eligible — the legacy ``_build_registry`` shim fallback
    (which synthesises an OmniSight-global ``default-gerrit`` row tagged
    ``tenant_id="t-default"``) is deliberately excluded, so a customer tenant
    with no real row resolves to ``None`` and the push fails closed. Prefers an
    ``is_default`` row, else the first enabled Gerrit row (registry order).
    """
    from backend import git_credentials

    registry = _run_coro(
        git_credentials.get_credential_registry_async(tenant_id)
    ) or []
    candidates = [
        e
        for e in registry
        if e.get("platform") == "gerrit"
        and e.get("enabled", True)
        and e.get("tenant_id") == tenant_id
    ]
    for entry in candidates:
        if entry.get("is_default"):
            return entry
    return candidates[0] if candidates else None


def resolve_gerrit_push_identity(
    agent_class: str,
    instance_id: str | None = None,
    *,
    tenant_id: str | None = None,
) -> GerritPushIdentity:
    """Resolve the Gerrit push identity for the current tenant (L6).

    * ``omnisight-self`` (and the no-tenant-bound back-compat default) keep the
      existing per-agent-class bot account + the OmniSight project, so internal
      pushes behave exactly as before.
    * Any customer tenant pushes via its own ``git_accounts`` Gerrit row, scoped
      to that tenant's project. The shared bot key and the OmniSight project are
      refused (raising :class:`TenantPushIdentityError`); there is no silent
      fallback to the bot identity.

    *tenant_id* defaults to the tenant bound into the DB/FS context at pickup
    (``db_context.set_tenant_id`` in 1A.1); explicit callers may override it.
    """
    from backend import db_context
    from backend.agents import runner_tenant

    tid = (
        tenant_id
        or db_context.current_tenant_id()
        or runner_tenant.OMNISIGHT_SELF_TENANT
    )

    if runner_tenant.is_self_tenant(tid):
        bot_username, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
        return GerritPushIdentity(
            tenant_id=tid,
            ssh_user=bot_username,
            ssh_key=ssh_key,
            ssh_host=GERRIT_SSH_HOST,
            ssh_port=GERRIT_SSH_PORT,
            project=GERRIT_PROJECT_PATH,
            is_bot=True,
        )

    account = _resolve_tenant_gerrit_account(tid)
    if account is None:
        raise TenantPushIdentityError(
            f"tenant {tid!r} has no enabled Gerrit credential of its own in the "
            "registry; refusing to push with the shared bot key (L6 / design §3)."
        )

    raw_key = str(account.get("ssh_key") or "").strip()
    project = str(account.get("project") or "").strip()
    ssh_host = str(account.get("ssh_host") or "").strip()
    ssh_user = str(account.get("username") or "").strip()
    ssh_port = int(account.get("ssh_port") or 0) or GERRIT_SSH_PORT

    if not raw_key:
        raise TenantPushIdentityError(
            f"tenant {tid!r} Gerrit credential has no ssh_key configured."
        )
    ssh_key = Path(raw_key).expanduser()
    if _is_shared_bot_key(ssh_key):
        raise TenantPushIdentityError(
            f"tenant {tid!r} Gerrit credential resolves to the shared bot key "
            f"{ssh_key}; refused (L6)."
        )
    if not project:
        raise TenantPushIdentityError(
            f"tenant {tid!r} Gerrit credential has no project namespace; a "
            "customer push must be scoped to the tenant's own project."
        )
    if project == GERRIT_PROJECT_PATH:
        raise TenantPushIdentityError(
            f"tenant {tid!r} Gerrit credential points at the OmniSight project "
            f"{project!r}; a customer run cannot push to OmniSight's project (L6)."
        )
    if not ssh_host:
        raise TenantPushIdentityError(
            f"tenant {tid!r} Gerrit credential has no ssh_host."
        )
    if not ssh_user:
        raise TenantPushIdentityError(
            f"tenant {tid!r} Gerrit credential has no username."
        )

    return GerritPushIdentity(
        tenant_id=tid,
        ssh_user=ssh_user,
        ssh_key=ssh_key,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        project=project,
        is_bot=False,
    )


def assert_worktree_config_enabled(repo_root: Path) -> None:
    """Fail fast unless the main repo enables per-worktree git config.

    Without ``extensions.worktreeConfig=true``, runner worktrees can read
    sibling identity writes from the shared ``.git/config`` and Gerrit may
    reject pushes with the wrong bot email.
    """
    import subprocess
    import sys

    r = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--get", "extensions.worktreeConfig"],
        capture_output=True, text=True,
    )
    if r.stdout.strip().lower() == "true":
        return

    sys.exit(
        "FATAL: extensions.worktreeConfig is not enabled in main repo. "
        "Set it before starting the runner:\n"
        f"  git -C {repo_root} config core.repositoryformatversion 1\n"
        f"  git -C {repo_root} config extensions.worktreeConfig true\n"
        "See OP-729 for the cross-runner bot identity race."
    )


def _git_common_dir(worktree_path: Path) -> Path:
    """Resolve worktree's COMMON git dir (where hooks actually run from).

    Critical distinction (lessons-learned L14): `git rev-parse --git-dir`
    returns the worktree-specific dir (e.g. `.git/worktrees/foo`), but
    git executes hooks from `--git-common-dir` (the parent's `.git`).
    Hooks installed at the worktree-specific path silently never fire.
    """
    import subprocess
    out = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=worktree_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else (worktree_path / p).resolve()


def install_commit_msg_hook(worktree_path: Path) -> bool:
    """Idempotent: install Gerrit commit-msg hook in worktree's COMMON git dir.

    Returns True if hook is now present (whether installed or already there).
    Per memory `reference_gerrit_self_hosted.md` gotcha #1: scp subsystem
    is disabled, so we use HTTP fallback to fetch the hook script.
    Per L14: hook MUST live in `--git-common-dir/hooks/`, not
    `--git-dir/hooks/` — git's worktree pattern looks at common-dir.
    """
    hook_path = _git_common_dir(worktree_path) / "hooks" / "commit-msg"
    if hook_path.exists() and hook_path.stat().st_size > 0:
        return True
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    with BREAKERS["gerrit_rest"].call(urllib.request.urlopen, GERRIT_HOOK_URL, timeout=10) as r:
        hook_path.write_bytes(r.read())
    hook_path.chmod(0o755)
    return True


def set_bot_identity_in_worktree(
    worktree_path: Path,
    agent_class: str,
    instance_id: str | None = None,
) -> None:
    """Set worktree-local `git config user.email/user.name` to the bot identity
    matching (agent_class, instance_id).

    Critical (L15/L25): without this, codex commits use whatever the
    worktree's git config defaults to (typically the operator's env user
    `Agent-row7-self-agent <row7-self-agent@omnisight.local>`). Bare
    ``git config`` writes land in the shared repo config, so sibling
    runners can overwrite each other's identity; ``--worktree`` keeps each
    runner isolated.

    Idempotent: setting same value twice is a no-op.
    """
    import subprocess
    try:
        bot_user, _ = _gerrit_auth_for_instance(agent_class, instance_id)
    except ValueError:
        bot_user, _ = _GERRIT_AUTH_BY_CLASS["subscription-claude"]
    bot_email = _bot_email_for(agent_class, instance_id)
    subprocess.run(
        ["git", "config", "--worktree", "user.email", bot_email],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "--worktree", "user.name", bot_user],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    )


@dataclass(frozen=True)
class WorktreeSyncResult:
    """Outcome of syncing a worktree to Gerrit's develop tip."""
    branch_name: str           # e.g. "feature/OP-18-runner-fresh"
    develop_sha: str           # SHA of fetched develop tip
    detail: str                # short status string
    worktree_path: Path | None = None  # OP-817: ephemeral dir when ephemeral=True


@dataclass(frozen=True)
class PatchSetStaleness:
    """Pickup/merger staleness assessment for an existing Gerrit patchset."""

    age_days: float
    commits_behind: int
    ps_parent: str
    level: str
    reason: str

    @property
    def should_warn(self) -> bool:
        return self.level == "warn"

    @property
    def should_abstain(self) -> bool:
        return self.level == "abstain"


# OP-817: per-ticket ephemeral worktree base directory. Concurrent ticks of
# different tickets must not share a single CLAUDE_WORKTREE (the 2026-05-09
# codex-2 DU file incident wedged 3 tickets when two ticks raced on `git
# switch` / `commit` / `push`). Each ephemeral worktree is created under this
# base as ``<ticket>-<run_id>`` and torn down after push (success OR failure).
EPHEMERAL_WORKTREE_BASE = Path("~/work/sora-worktrees").expanduser()


def _new_run_id() -> str:
    """Short, unique-per-tick suffix for ephemeral worktree directories."""
    return uuid.uuid4().hex[:12]


def _ephemeral_worktree_dir(ticket_key: str, run_id: str) -> Path:
    """Compute the per-tick worktree path under ``EPHEMERAL_WORKTREE_BASE``."""
    return EPHEMERAL_WORKTREE_BASE / f"{ticket_key}-{run_id}"


def cleanup_ephemeral_worktree(
    main_repo: Path,
    worktree_path: Path,
) -> None:
    """Tear down an ephemeral worktree created by ``sync_to_gerrit_develop(...,
    ephemeral=True)``.

    Idempotent and best-effort: ``git worktree remove --force`` first (so git's
    administrative metadata under ``<main_repo>/.git/worktrees/`` is cleaned
    up), then ``rm -rf`` on the directory regardless. Caller invokes this in a
    ``finally`` block — leaking an ephemeral dir defeats the whole point of
    per-tick isolation, so we never raise. Errors are logged.
    """
    import shutil
    import subprocess as _sp

    if main_repo.exists():
        try:
            _sp.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=main_repo, capture_output=True, text=True, timeout=30,
            )
        except (OSError, _sp.SubprocessError) as exc:
            log.warning(
                "cleanup_ephemeral_worktree: `git worktree remove` failed for %s: %s",
                worktree_path, exc,
            )

    if worktree_path.exists():
        try:
            shutil.rmtree(worktree_path, ignore_errors=True)
        except OSError as exc:
            log.warning(
                "cleanup_ephemeral_worktree: rmtree failed for %s: %s",
                worktree_path, exc,
            )

    # Defensive: prune dangling administrative entries even if `remove` above
    # already succeeded (no-op when already clean).
    if main_repo.exists():
        try:
            _sp.run(
                ["git", "worktree", "prune"],
                cwd=main_repo, capture_output=True, text=True, timeout=30,
            )
        except (OSError, _sp.SubprocessError):
            pass


def sync_to_gerrit_develop(
    worktree_path: Path,
    agent_class: str,
    ticket_key: str,
    instance_id: str | None = None,
    *,
    ephemeral: bool = False,
    run_id: str | None = None,
) -> WorktreeSyncResult:
    """Fetch latest develop from Gerrit + cut a fresh feature branch.

    Per L16 + the per-ticket-fresh-sync design (see
    `docs/sop/jira-ticket-conventions.md` §10/§16):

    1. Fetch ``develop`` ref from Gerrit (canonical source).
    2. Capture the fetched SHA explicitly (FETCH_HEAD changes on
       subsequent git ops).
    3. Force-create branch ``feature/<ticket_key>-runner-fresh`` at
       the fetched develop tip; switch to it.
    4. Codex commits land on this fresh branch on top of latest develop.

    Discards any uncommitted state in worktree (warning: partial codex
    work from prior runs is lost). Acceptable per design — Gerrit is
    source of truth, JIRA tracks intent.

    OP-817: when ``ephemeral=True``, ``worktree_path`` is treated as the
    **main repo** and a brand-new worktree directory is created under
    ``EPHEMERAL_WORKTREE_BASE/<ticket_key>-<run_id>/`` via ``git worktree
    add``. Concurrent ticks of different tickets no longer race on a
    shared ``CLAUDE_WORKTREE`` (the 2026-05-09 incident wedged 3 tickets).
    Caller MUST call :func:`cleanup_ephemeral_worktree` in a ``finally``
    block on the path returned in ``WorktreeSyncResult.worktree_path``.

    Raises CalledProcessError if any git op fails.
    """
    import subprocess
    _, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)

    # OP-1777 (L3): git children get a minimal allowlisted env, NOT a full
    # os.environ.copy() that would leak OMNISIGHT_* infra secrets to the
    # subprocess. GIT_SSH_COMMAND (the per-bot gerrit key) is injected on top.
    env = runner_sandbox.build_allowlisted_env(
        extra={"GIT_SSH_COMMAND": f"ssh -i {ssh_key}"}
    )

    if ephemeral:
        # Treat ``worktree_path`` as the main repo and add a fresh worktree.
        main_repo = worktree_path
        run_id = run_id or _new_run_id()
        ephemeral_path = _ephemeral_worktree_dir(ticket_key, run_id)
        EPHEMERAL_WORKTREE_BASE.mkdir(parents=True, exist_ok=True)

        # If the dir already exists (collision on run_id), refuse rather than
        # silently reusing — the whole point of ephemeral is isolation.
        if ephemeral_path.exists():
            raise RuntimeError(
                f"ephemeral worktree dir already exists: {ephemeral_path}; "
                "pass a unique run_id or clean up the stale directory."
            )

        # Step 1: fetch develop from Gerrit into the main repo.
        BREAKERS["gerrit_ssh"].call(
            subprocess.run,
            ["git", "fetch", _gerrit_ssh_url(agent_class, instance_id), "develop"],
            cwd=main_repo, env=env, check=True, capture_output=True, text=True, timeout=60,
        )

        # Step 2: capture fetched SHA from main repo.
        develop_sha = subprocess.run(
            ["git", "rev-parse", "FETCH_HEAD"],
            cwd=main_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Step 3: add a fresh worktree at the develop tip on a new branch.
        # `-B` forces branch creation; `--detach` is avoided because callers
        # rely on a named feature branch for `git push HEAD:refs/for/develop`.
        branch_name = f"feature/{ticket_key}-runner-fresh"
        subprocess.run(
            ["git", "worktree", "add", "-B", branch_name, str(ephemeral_path), develop_sha],
            cwd=main_repo, check=True, capture_output=True, text=True,
        )

        return WorktreeSyncResult(
            branch_name=branch_name,
            develop_sha=develop_sha,
            detail=f"ephemeral worktree {ephemeral_path} at {develop_sha[:12]}",
            worktree_path=ephemeral_path,
        )

    assert_worktree_clean(worktree_path)

    # Step 1: fetch develop from Gerrit
    BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        ["git", "fetch", _gerrit_ssh_url(agent_class, instance_id), "develop"],
        cwd=worktree_path, env=env, check=True, capture_output=True, text=True, timeout=60,
    )

    # Step 2: capture fetched SHA
    develop_sha = subprocess.run(
        ["git", "rev-parse", "FETCH_HEAD"],
        cwd=worktree_path, capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Step 3: cut fresh feature branch + switch
    branch_name = f"feature/{ticket_key}-runner-fresh"
    subprocess.run(
        ["git", "switch", "-C", branch_name, develop_sha],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    )

    # Step 3b (OP-796): `git switch -C` preserves unstaged tracked-file changes,
    # so successive ticks could accumulate cross-ticket leftover edits until
    # `ensure_change_ids` ran `git rebase --exec` which refused with "cannot
    # rebase: You have unstaged changes" and wedged the runner. Hard-reset the
    # worktree to the fetched develop tip to enforce the docstring's contract.
    # `assert_worktree_clean` (pre-step) only handles in-progress git states
    # (rebase/cherry-pick/merge/...), not unstaged tracked-file edits.
    subprocess.run(
        ["git", "reset", "--hard", develop_sha],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    )

    # Step 4: clean untracked (defensive — discards stale partial work)
    subprocess.run(
        ["git", "clean", "-fdx"],
        cwd=worktree_path, check=False, capture_output=True,
    )

    return WorktreeSyncResult(
        branch_name=branch_name,
        develop_sha=develop_sha,
        detail=f"fresh branch {branch_name} at {develop_sha[:12]}",
    )


#: R.2a (OP-2194): base dir for routed-repo clones, one fresh clone per
#: (repo, instance, ticket, run) so concurrent routed work never shares a tree.
ROUTED_WORKSPACE_BASE = Path(
    os.environ.get("OMNISIGHT_ROUTED_WORKSPACE_BASE", "~/work/sora-routed-worktrees")
).expanduser()


def _routed_clone_dir(repo_name: str, instance_id: str, ticket_key: str, run_id: str) -> Path:
    """Per-(repo, instance, ticket, run) routed clone path.

    Distinct from the productizer ``_ephemeral_worktree_dir`` namespace so the
    orphan-reaper / workspace-safety sweeper can tell routed clones apart and
    two concurrent runners on two routed tickets never collide (codex finding:
    ``_repo_dir_name`` alone keys on the URL only).
    """
    safe_repo = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)
    return ROUTED_WORKSPACE_BASE / safe_repo / f"{instance_id}-{ticket_key}-{run_id}"


def _routed_url_for_bot(gerrit_url: str, bot_user: str) -> str:
    """Rewrite the SSH userinfo of a routed Gerrit URL to *bot_user*.

    The ``routed_repos`` config carries ONE fixed SSH user in its
    ``gerrit_url`` (e.g. ``ssh://claude-bot@…``), but a routed ticket may be
    picked by ANY runner instance, and each instance authenticates with its
    own per-instance Gerrit key (``_gerrit_auth_for_instance`` →
    ``codex-bot-codex-1`` etc.). Gerrit SSH binds the presented key to the
    account named in the URL, so a codex instance cloning a ``claude-bot@``
    URL with the codex key is rejected ``Permission denied (publickey)`` (the
    R.x e2e blocker — clone exit 128). Substitute the URL's user with the
    instance's own ``bot_user`` so URL-account and key-account always match.

    Only the userinfo is touched; scheme/host/port/path are preserved. A URL
    with no userinfo (or a non-ssh URL) is returned unchanged so the HTTP
    camviewpro lane and malformed configs degrade safely.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(gerrit_url)
    if parts.scheme != "ssh" or "@" not in parts.netloc:
        return gerrit_url
    host_port = parts.netloc.rsplit("@", 1)[1]
    new_netloc = f"{bot_user}@{host_port}"
    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def sync_routed_repo(
    routed: RoutedRepo,
    ticket_key: str,
    agent_class: str,
    instance_id: str | None = None,
    *,
    run_id: str | None = None,
) -> WorktreeSyncResult:
    """Clone + branch a ROUTED Gerrit repo for a ticket (OP-2194 / R.2a).

    The routed analog of :func:`sync_to_gerrit_develop`. Deliberately a
    SEPARATE function (not a parameterization of the productizer-shaped one,
    per the 3-way audit) because the routed flow CLONES a fresh repo rather
    than reusing the wrapper's productizer worktree:

    1. Fresh ``git clone`` of ``routed.gerrit_url`` into a per-instance dir
       (``ROUTED_WORKSPACE_BASE/<repo>/<instance>-<ticket>-<run_id>/``) — the
       wrapper's productizer clone is unused for a routed ticket.
    2. Fetch the routed develop ref + capture its SHA.
    3. Cut a fresh ``feature/<ticket>-runner-fresh`` branch at that tip.

    Auth: the same per-bot Gerrit SSH key as the normal lane
    (``_gerrit_auth_for_instance``); only the project URL differs (``routed``).
    R.2a does NOT push (that is R.3 / GerritReviewDelivery) — it only prepares
    a clean clone for the agent to work in. Caller MUST clean up the returned
    ``worktree_path`` (R.2b wires orphan-reaper / workspace-safety).

    Raises CalledProcessError if any git op fails.
    """
    import subprocess

    bot_user, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    # The config gerrit_url carries one fixed SSH user; rewrite it to THIS
    # instance's bot account so the URL-account matches the per-instance key
    # (else a codex instance cloning a claude-bot@ URL is rejected → exit 128).
    clone_url = _routed_url_for_bot(routed.gerrit_url, bot_user)
    env = runner_sandbox.build_allowlisted_env(
        extra={"GIT_SSH_COMMAND": f"ssh -i {ssh_key}"}
    )
    instance = instance_id or DEFAULT_INSTANCE_ID
    run_id = run_id or _new_run_id()
    clone_dir = _routed_clone_dir(routed.name, instance, ticket_key, run_id)

    if clone_dir.exists():
        raise RuntimeError(
            f"routed clone dir already exists: {clone_dir}; "
            "pass a unique run_id or clean up the stale directory."
        )
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: fresh clone of the ROUTED repo (NOT the productizer worktree).
    # clone_url carries THIS instance's bot SSH user (see _routed_url_for_bot);
    # GIT_SSH_COMMAND supplies the matching per-instance key. No token in the
    # URL (Gerrit SSH, unlike the camviewpro HTTP lane).
    BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        ["git", "clone", clone_url, str(clone_dir)],
        env=env, check=True, capture_output=True, text=True, timeout=180,
    )

    # Step 2: fetch develop from the routed repo + capture the SHA.
    BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        ["git", "fetch", clone_url, "develop"],
        cwd=clone_dir, env=env, check=True, capture_output=True, text=True, timeout=60,
    )
    develop_sha = subprocess.run(
        ["git", "rev-parse", "FETCH_HEAD"],
        cwd=clone_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Step 3: cut a fresh feature branch at the routed develop tip.
    branch_name = f"feature/{ticket_key}-runner-fresh"
    subprocess.run(
        ["git", "switch", "-C", branch_name, develop_sha],
        cwd=clone_dir, check=True, capture_output=True, text=True,
    )

    # Step 4: set the bot git identity on the routed clone with --local.
    # A routed clone is a fresh `git clone` (its OWN repo), NOT a linked
    # `git worktree`, so set_bot_identity_in_worktree's `git config --worktree`
    # would write to .git/config.worktree — which git IGNORES unless
    # extensions.worktreeConfig=true (it isn't on a plain clone). The result
    # was the agent committing with the host's GLOBAL git identity, whose email
    # is not a registered Gerrit email for the bot account → the routed push
    # was rejected with `settings#EmailAddresses` (forgeAuthor block). --local
    # is the correct scope for a dedicated clone and is always read.
    bot_email = _bot_email_for(agent_class, instance_id)
    subprocess.run(
        ["git", "config", "--local", "user.email", bot_email],
        cwd=clone_dir, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "--local", "user.name", bot_user],
        cwd=clone_dir, check=True, capture_output=True, text=True,
    )

    return WorktreeSyncResult(
        branch_name=branch_name,
        develop_sha=develop_sha,
        detail=f"routed clone {clone_dir} ({routed.name}) at {develop_sha[:12]}",
        worktree_path=clone_dir,
    )


def is_routed_clone(path: Path) -> bool:
    """True iff *path* lives under :data:`ROUTED_WORKSPACE_BASE`.

    A sweeper / the cleanup helper uses this to distinguish routed clones
    (R.2a) from the productizer worktree and the per-tenant workspaces — and
    to refuse to ``rm`` anything outside the routed base.
    """
    try:
        path.resolve().relative_to(ROUTED_WORKSPACE_BASE.resolve())
        return True
    except (ValueError, OSError):
        return False


def cleanup_routed_clone(worktree_path: Path) -> None:
    """Tear down a routed clone created by :func:`sync_routed_repo` (R.2b).

    Idempotent + best-effort (never raises — leaking a routed clone defeats
    the per-(repo,instance,ticket,run) isolation), BUT fail-closed on path:
    refuses to remove anything that is not under
    :data:`ROUTED_WORKSPACE_BASE` so a bad caller can never ``rm -rf`` the
    productizer worktree or a tenant workspace.
    """
    import shutil

    if not is_routed_clone(worktree_path):
        log.warning(
            "cleanup_routed_clone: refusing to remove %s — not under the "
            "routed workspace base %s",
            worktree_path, ROUTED_WORKSPACE_BASE,
        )
        return
    if worktree_path.exists():
        try:
            shutil.rmtree(worktree_path, ignore_errors=True)
        except OSError as exc:
            log.warning("cleanup_routed_clone: rmtree failed for %s: %s", worktree_path, exc)


def push_routed_for_review(
    worktree_path: Path,
    routed: RoutedRepo,
    agent_class: str,
    instance_id: str | None = None,
) -> GerritPushResult:
    """Push a routed clone's HEAD to the ROUTED Gerrit project's review queue
    (OP-2196 / R.3 — GerritReviewDelivery).

    The routed analog of :func:`push_to_gerrit_for_review`. Deliberately does
    NOT call :func:`resolve_gerrit_push_identity` (which is hardcoded to the
    productizer project for the self tenant — the audit blocker): the push
    target is ``routed.gerrit_url`` with the per-bot Gerrit SSH key, and the
    ref is ``routed.ref`` (e.g. ``refs/for/develop``). Caller must have run
    :func:`ensure_change_ids` so every commit carries a Change-Id (the
    commit-msg hook is installed in the routed clone in R.2b); the L1 dual
    co-author trailers come from the agent's commits, same as the normal lane.

    Returns a :class:`GerritPushResult` (Change number + URL on success).
    Retries transient Gerrit failures with the shared backoff schedule.
    """
    import subprocess

    bot_user, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    if not ssh_key.exists():
        return GerritPushResult(False, None, None, f"SSH key not found at {ssh_key}")
    # Match the push URL's SSH account to this instance's per-instance key
    # (same reason as the clone in sync_routed_repo — a fixed config user would
    # reject codex instances). _routed_url_for_bot is a no-op for the matching
    # claude instance and for non-ssh URLs.
    push_url = _routed_url_for_bot(routed.gerrit_url, bot_user)
    env = runner_sandbox.build_allowlisted_env(
        extra={"GIT_SSH_COMMAND": f"ssh -i {ssh_key}"}
    )

    retry_notes: list[str] = []
    max_attempts = len(_GERRIT_PUSH_RETRY_BACKOFFS) + 1
    result: subprocess.CompletedProcess[str] | None = None
    blob = ""
    for attempt in range(1, max_attempts + 1):
        result = BREAKERS["gerrit_ssh"].call(
            subprocess.run,
            ["git", "push", "--no-thin", push_url, f"HEAD:{routed.ref}"],
            cwd=worktree_path, capture_output=True, text=True, env=env, timeout=120,
        )
        blob = (result.stderr + "\n" + result.stdout).strip()
        if result.returncode == 0:
            break
        if attempt == max_attempts or not _is_transient_gerrit_push_failure(blob):
            break
        backoff = _GERRIT_PUSH_RETRY_BACKOFFS[attempt - 1]
        note = (
            f"attempt {attempt}/{max_attempts} failed with transient Gerrit "
            f"push error; retrying in {backoff}s"
        )
        retry_notes.append(note)
        log.warning("%s: %s", note, blob[-500:])
        time.sleep(backoff)

    if result is None:
        return GerritPushResult(False, None, None, "git push did not run")
    if result.returncode != 0:
        detail = blob[-1500:]
        if retry_notes:
            detail = "\n".join([*retry_notes, detail])
        return GerritPushResult(False, None, None, detail)

    m = _GERRIT_CHANGE_URL_RE.search(blob)
    if not m:
        return GerritPushResult(
            False, None, None,
            f"push succeeded but Change URL not parsed:\n{blob[-1500:]}",
        )
    return GerritPushResult(True, int(m.group(2)), m.group(1), blob[-500:])


def assert_worktree_clean(worktree_path: Path) -> None:
    """Detect and recover stale git operation state before branch ops.

    Idempotent: clean worktrees produce no side effects or logs. If git
    reports an in-progress rebase/cherry-pick/merge/bisect/revert, this
    attempts the matching abort/reset command and raises loudly if cleanup
    fails.
    """
    git_dir_raw = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--git-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = worktree_path / git_dir

    state_artifacts = {
        "rebase-merge": ["git", "rebase", "--quit"],
        "rebase-apply": ["git", "rebase", "--quit"],
        "CHERRY_PICK_HEAD": ["git", "cherry-pick", "--abort"],
        "MERGE_HEAD": ["git", "merge", "--abort"],
        "BISECT_LOG": ["git", "bisect", "reset"],
        "REVERT_HEAD": ["git", "revert", "--abort"],
    }
    recovered: list[str] = []
    for artifact, cleanup_cmd in state_artifacts.items():
        artifact_path = git_dir / artifact
        if not artifact_path.exists():
            continue

        log.warning(
            "worktree-state-leak detected: %s; running %s",
            artifact,
            cleanup_cmd,
        )
        result = subprocess.run(
            cleanup_cmd,
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"worktree-state-leak unrecoverable: {artifact} cleanup failed "
                f"(rc={result.returncode}, stderr={result.stderr[:200]}). "
                "Manual fix needed."
            )
        recovered.append(artifact)

    if recovered:
        log.info("worktree pre-sync: recovered from %s", recovered)


class NoCommitsOnBranchError(RuntimeError):
    """Raised by :func:`ensure_change_ids` when the branch has no commits to
    push relative to ``base_ref``.

    OP-827 post-mortem: claude CLI sometimes finishes implementation, posts
    the AC-verification comment, then exits without making a commit. The
    runner's previous behaviour was to call ``git rebase`` against an empty
    branch — git's exact error code there is non-deterministic (rc=1 with a
    generic message), and the runner fell back to the wedge path
    (``[runner-gerrit-setup-fail]`` comment + ticket left orphan In Progress
    for 2.5 days in OP-811/OP-813). Surfacing the precondition explicitly
    lets the caller route to the correct recovery (revert to To Do + clear
    assignee).
    """

    def __init__(self, base_ref: str, head: str) -> None:
        super().__init__(
            f"branch has 0 commits between {base_ref[:12]}..{head[:12]} — "
            "claude CLI exited without producing a commit"
        )
        self.base_ref = base_ref
        self.head = head


class WorktreeDirtyError(RuntimeError):
    """Raised by :func:`ensure_change_ids` when the worktree has uncommitted
    changes that would block ``git rebase``.

    Same OP-827 lineage: distinguishes "claude wrote files but never
    committed them" from "claude made commits and we should rebase". The
    caller routes the former to a revert (work was lost / never landed) and
    the latter to the standard rebase path.
    """

    def __init__(self, dirty_files: list[str]) -> None:
        super().__init__(
            f"worktree has {len(dirty_files)} uncommitted path(s); rebase "
            f"refuses to run. First few: {dirty_files[:5]}"
        )
        self.dirty_files = dirty_files


def ensure_change_ids(worktree_path: Path, base_ref: str) -> None:
    """Rebase commits between base_ref..HEAD with --exec amend, triggering
    the commit-msg hook on each commit so they all get a Change-Id footer.

    Idempotent: commits already containing a Change-Id are unchanged
    (the standard Gerrit hook detects and skips).

    L16 fix: caller MUST pass an explicit base_ref (no default). Earlier
    default of "main" rebased onto local main which could contain commits
    with non-bot committer emails — Gerrit then rejects on push.

    OP-827 fix: two preconditions are checked before invoking ``git rebase``
    so the orphan-In-Progress wedge that hit OP-811/OP-813 cannot recur:

    * ``NoCommitsOnBranchError`` if ``base_ref..HEAD`` is empty — claude
      exited without committing; the right caller response is "revert to
      To Do", not "retry the rebase".
    * ``WorktreeDirtyError`` if ``git status --porcelain`` reports
      uncommitted paths — same wedge cause, different shape (claude wrote
      files but skipped both commit AND clean exit).

    Recommended usage: pass `develop_sha` from `sync_to_gerrit_develop()`.
    """
    # Precondition 1: branch has at least one commit beyond base_ref.
    rev_list = subprocess.run(
        ["git", "rev-list", f"{base_ref}..HEAD", "--count"],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    )
    commit_count = int(rev_list.stdout.strip() or "0")
    if commit_count == 0:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path, check=True, capture_output=True, text=True,
        ).stdout.strip()
        raise NoCommitsOnBranchError(base_ref=base_ref, head=head_sha)

    # Precondition 2: worktree has no uncommitted paths. ``git rebase``
    # refuses on a dirty worktree with a non-deterministic error message,
    # so we shape the diagnostic ourselves.
    #
    # OP-842 fix: exclude the OP-836 workspace-safety sentinel
    # (``.runner-cwd-sentinel``) from the dirty check. The sentinel is
    # written by ``runner_workspace_safety.write_workspace_sentinel``
    # pre-CLI as an intentionally-untracked tamper-detection marker.
    # OP-836 documented the intent to add it to ``.git/info/exclude``
    # but the implementation was never written; this filter is the
    # smaller, layered fix — the sentinel's ENTIRE purpose is to be
    # detectable by us, but it has a known fixed filename so
    # ``ensure_change_ids`` can recognise + skip it cleanly. Without
    # this filter, every Phase 3+ ticket that runs through OP-836's
    # write_workspace_sentinel would wedge here (OP-840 was the first
    # ticket to surface the bug in production).
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    )
    dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    # SP-B-X-018 / OP-1076: filter via the CANONICAL constant exported
    # by ``runner_progress`` so this site and ``_worktree_dirty`` never
    # disagree about which files are runner-own bookkeeping. Previously
    # each site maintained its own local set (SP-B-X-016 fixed one,
    # SP-B-X-017 fixed the other) — the consolidation lets future
    # additions land in one place.
    # OP-1111: import from runner_artifacts (canonical home);
    # runner_progress.RUNNER_RUNTIME_ARTIFACTS still re-exports for
    # backwards compat with pre-OP-1111 callers.
    from backend.agents.runner_artifacts import RUNNER_RUNTIME_ARTIFACTS
    dirty = [f for f in dirty if f not in RUNNER_RUNTIME_ARTIFACTS]
    if dirty:
        raise WorktreeDirtyError(dirty_files=dirty)

    # OP-2484: handle BOTH shapes of "empty commit" so the rebase + exec
    # amend does not crash on the OP-1647 regression (commit's net diff is
    # already on develop tip → ``git commit --amend --no-edit`` refuses with
    # "doing so would make it empty"). ``--keep-empty`` covers commits that
    # START empty; ``--empty=keep`` covers commits that BECOME empty after
    # rebase; ``--allow-empty`` on the amend lets the no-op amend succeed so
    # the commit-msg hook still fires and the Change-Id footer is stamped.
    # The kept-but-empty commit still has a Change-Id and pushes cleanly to
    # ``refs/for/develop``; Gerrit shows it as a zero-diff patchset, which
    # the reviewer can resolve (abandon as already-shipped, or push a real
    # follow-up). Either outcome is better than the silent re-pickup loop
    # that wedged OP-1647 prior to this fix.
    try:
        subprocess.run(
            [
                "git", "rebase", base_ref,
                "--keep-empty",
                "--empty=keep",
                "--exec", "git commit --amend --no-edit --allow-empty",
            ],
            cwd=worktree_path, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "rebase", "--quit"],
            cwd=worktree_path,
            capture_output=True,
        )
        raise


_GERRIT_CHANGE_URL_RE = re.compile(r"(https://\S+/c/[^\s]+/\+/(\d+))")
_GERRIT_CHANGE_ID_RE = re.compile(r"^Change-Id:\s*(I[0-9a-fA-F]+)\s*$", re.MULTILINE)
_OP_KEY_RE = re.compile(r"\bOP-\d+\b")
_TRANSIENT_GERRIT_PUSH_RE = re.compile(
    r"Missing tree|Unpack error|remote unpack failed|Connection reset|"
    r"Connection timed out|timed out|Broken pipe|Connection refused|"
    r"kex_exchange_identification|temporary failure",
    re.IGNORECASE,
)
_GERRIT_PUSH_RETRY_BACKOFFS = (2, 4, 8)
PRE_REVIEW_SELF_FIX_EXHAUSTED_LABELS = (
    "needs-coordinator",
    "pre-review-self-fix-exhausted",
    "class:operator",
)
PRE_REVIEW_SELF_FIX_EXHAUSTED_MAX_DIFF_CHARS = 30000


def _is_transient_gerrit_push_failure(detail: str) -> bool:
    """Return True for Gerrit/SSH push failures worth retrying immediately."""

    return bool(_TRANSIENT_GERRIT_PUSH_RE.search(str(detail or "")))


def _head_change_id(worktree_path: Path) -> str | None:
    """Return HEAD commit Change-Id, if the commit-msg hook added one."""

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _GERRIT_CHANGE_ID_RE.search(result.stdout)
    return match.group(1) if match else None


def _infer_ticket_key_from_worktree(worktree_path: Path) -> str | None:
    """Infer the source OP ticket from the branch or HEAD commit text."""

    for cmd in (
        ["git", "branch", "--show-current"],
        ["git", "log", "-1", "--format=%B"],
    ):
        try:
            result = subprocess.run(
                cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        match = _OP_KEY_RE.search(result.stdout)
        if match:
            return match.group(0)
    return None


def _pre_review_self_fix_diff_context(worktree_path: Path, target: str) -> str:
    """Return bounded diff context for the exhaustion escalation ticket."""

    commands = (
        ["git", "diff", "--stat", "FETCH_HEAD...HEAD"],
        ["git", "diff", "FETCH_HEAD...HEAD"],
    )
    chunks: list[str] = []
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            chunks.append(f"$ {' '.join(cmd)}\n<failed: {type(exc).__name__}: {exc}>")
            continue
        body = result.stdout if result.returncode == 0 else (result.stderr or result.stdout)
        chunks.append(f"$ {' '.join(cmd)}\n{body.strip()}")

    text = "\n\n".join(chunks).strip()
    if len(text) > PRE_REVIEW_SELF_FIX_EXHAUSTED_MAX_DIFF_CHARS:
        omitted = len(text) - PRE_REVIEW_SELF_FIX_EXHAUSTED_MAX_DIFF_CHARS
        text = (
            text[:PRE_REVIEW_SELF_FIX_EXHAUSTED_MAX_DIFF_CHARS]
            + f"\n\n[diff context truncated by {omitted} chars]"
        )
    return text or "<no diff context produced>"


def _adf_codeblock(text: str, language: str = "markdown") -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "attrs": {"language": language},
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def file_pre_review_self_fix_exhaustion_ticket(
    client: DispatchClient,
    *,
    source_ticket_key: str | None,
    change_number: int,
    change_url: str,
    change_id: str | None,
    attempts: int,
    target: str,
    detail: str,
    diff_context: str,
) -> str:
    """File the operator escalation when pre-review self-fix is exhausted."""

    source = source_ticket_key or "unknown-source-ticket"
    summary = f"pre-review-self-fix-exhausted: {source} Change {change_number}"
    description = "\n".join(
        [
            "@coordinator",
            "@operator fallback if coordinator is not live.",
            "",
            "Pre-review mergeability self-fix exhausted and needs operator coordination.",
            "",
            f"Source ticket: {source}",
            f"Gerrit change: {change_url}",
            f"Change-Id: {change_id or 'unknown'}",
            f"Target branch: {target}",
            f"Self-fix attempts: {attempts}",
            f"Runner detail: {detail}",
            "",
            "Diff context:",
            "```diff",
            diff_context,
            "```",
        ]
    )
    body = {
        "fields": {
            "project": {"key": client.project_key},
            "summary": summary,
            "description": _adf_codeblock(description),
            "issuetype": {"name": "Story"},
            "priority": {"name": "High"},
            "labels": list(PRE_REVIEW_SELF_FIX_EXHAUSTED_LABELS),
        }
    }
    resp = _request(client, "POST", "/issue", body)
    key = str(resp.get("key") or "")
    if not key:
        raise RuntimeError(f"JIRA POST /issue returned no key: {resp!r}")
    return key


def query_gerrit_change_by_change_id(
    change_id: str,
    agent_class: str = "subscription-codex",
    instance_id: str | None = None,
    *,
    identity: "GerritPushIdentity | None" = None,
) -> GerritChangeInfo | None:
    """Return Gerrit change metadata for ``change:<Change-Id>``, if present.

    *identity* lets a caller (the push recovery path) pin the query to the
    SAME tenant identity that performed the push, so a customer-tenant
    recovery query never reaches Gerrit over the shared bot key (L6). When
    omitted it is resolved from the current tenant context.
    """
    if identity is None:
        identity = resolve_gerrit_push_identity(agent_class, instance_id)
    cmd = [
        "ssh", "-i", str(identity.ssh_key), "-p", str(identity.ssh_port),
        f"{identity.ssh_user}@{identity.ssh_host}",
        "gerrit", "query", "--format=JSON", f"change:{change_id}",
    ]
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in result.stdout.splitlines():
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if change.get("type") == "stats":
            continue
        number = int(change.get("number") or change.get("_number") or 0)
        if not number:
            continue
        url = str(
            change.get("url")
            or f"https://{identity.ssh_host}:29420/c/{identity.project}/+/{number}"
        )
        return GerritChangeInfo(
            change_number=number,
            change_url=url,
            subject=str(change.get("subject") or ""),
        )
    return None


def _push_to_delivery_target(
    worktree_path: Path,
    delivery: DeliveryTarget,
    *,
    tenant_id: str | None = None,
) -> GerritPushResult:
    """Push worktree HEAD to a configured per-project delivery target.

    OP-1837 (1B v1). The push credential is resolved from the tenant's
    ``git_accounts`` row referenced by ``delivery.git_account_ref`` (the
    existing git_accounts model) — never inlined, never logged. If it can't
    be resolved the push fails closed: it never falls back to the shared
    OmniSight bot key.

    The OmniSight Gerrit-review post-processing (Change-URL parse, pre-review
    mergeability self-fix, transient-retry recovery query) is intentionally
    NOT run here — a customer delivery repo is not the OmniSight review
    queue. Richer per-target delivery semantics (retries, review-queue
    detection) are a follow-on.
    """
    import subprocess

    try:
        cred = _run_coro(
            resolve_delivery_credential(delivery, tenant_id=tenant_id)
        )
    except DeliveryTargetError as exc:
        return GerritPushResult(False, None, None, str(exc))

    raw_key = str(cred.get("ssh_key") or "").strip()
    if not raw_key:
        return GerritPushResult(
            False, None, None,
            f"delivery target for project {delivery.project_key!r}: "
            "git_accounts credential has no ssh_key",
        )
    ssh_key = Path(raw_key).expanduser()
    if not ssh_key.exists():
        return GerritPushResult(False, None, None, f"SSH key not found at {ssh_key}")

    # OP-1777 (L3): minimal allowlisted env + the resolved per-target key;
    # never an os.environ.copy() that would leak OMNISIGHT_* infra secrets.
    env = runner_sandbox.build_allowlisted_env(
        extra={"GIT_SSH_COMMAND": f"ssh -i {ssh_key}"}
    )
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        ["git", "push", "--no-thin", delivery.repo_url, f"HEAD:{delivery.ref_spec}"],
        cwd=worktree_path, capture_output=True, text=True, env=env, timeout=120,
    )
    blob = (result.stderr + "\n" + result.stdout).strip()
    if result.returncode != 0:
        return GerritPushResult(False, None, None, blob[-1500:])
    return GerritPushResult(
        success=True, change_number=None, change_url=None, detail=blob[-1500:]
    )


def push_to_gerrit_for_review(
    worktree_path: Path,
    agent_class: str,
    target: str = "develop",
    instance_id: str | None = None,
    tenant_id: str | None = None,
    project_key: str | None = None,
) -> GerritPushResult:
    """Push worktree HEAD to ``gerrit:refs/for/<target>``.

    Returns parsed Change number + URL on success, or detail blob on
    failure. Caller is responsible for having installed the commit-msg
    hook + ensured all commits have Change-Id footers (use
    :func:`install_commit_msg_hook` and :func:`ensure_change_ids` first).

    OP-1779 (1A.2 / L6): the push identity (SSH user + key + host + project)
    is resolved per-tenant via :func:`resolve_gerrit_push_identity`. The
    internal ``omnisight-self`` tenant keeps the bot identity + OmniSight
    project unchanged; a customer tenant pushes only via its own credential,
    and the push is refused (returned as a hard failure) rather than ever
    falling back to the shared bot key. *tenant_id* defaults to the tenant
    bound into the context at pickup.

    OP-1837 (1B v1): the delivery destination is resolved per-JIRA-project
    via :func:`resolve_delivery_target`. When *project_key* maps to a
    configured per-project target (``settings.delivery_targets``), the push
    is delivered to that target's repo/ref using the credential referenced by
    its ``git_account_ref`` (resolved via the git_accounts model). A project
    with no configured target — and every existing caller, which passes no
    *project_key* — resolves to the OmniSight default and takes the unchanged
    push path below (byte-identical back-compat).
    """
    import subprocess
    delivery = resolve_delivery_target(project_key, target=target)
    if not delivery.is_default:
        return _push_to_delivery_target(
            worktree_path, delivery, tenant_id=tenant_id
        )
    try:
        identity = resolve_gerrit_push_identity(
            agent_class, instance_id, tenant_id=tenant_id
        )
    except (ValueError, TenantPushIdentityError) as exc:
        return GerritPushResult(False, None, None, str(exc))
    bot_username = identity.ssh_user
    ssh_key = identity.ssh_key
    if not ssh_key.exists():
        return GerritPushResult(False, None, None, f"SSH key not found at {ssh_key}")

    # OP-1777 (L3): git children get a minimal allowlisted env, NOT a full
    # os.environ.copy() that would leak OMNISIGHT_* infra secrets to the
    # subprocess. GIT_SSH_COMMAND (the per-bot gerrit key) is injected on top.
    env = runner_sandbox.build_allowlisted_env(
        extra={"GIT_SSH_COMMAND": f"ssh -i {ssh_key}"}
    )

    change_id = _head_change_id(worktree_path)
    retry_notes: list[str] = []
    max_attempts = len(_GERRIT_PUSH_RETRY_BACKOFFS) + 1
    result: subprocess.CompletedProcess[str] | None = None
    blob = ""

    for attempt in range(1, max_attempts + 1):
        # --no-thin forces a full pack containing every object referenced by the
        # commit (including sub-trees git's thin-pack optimization would assume
        # the server already has). Eliminates the "Missing tree" failure class
        # when shared worktrees accumulate unreachable tree objects that get
        # reused as sub-tree refs in new commits. See OP-1015/1019/1026/1028
        # incident set (2026-05-13).
        result = BREAKERS["gerrit_ssh"].call(
            subprocess.run,
            ["git", "push", "--no-thin", identity.ssh_url, f"HEAD:refs/for/{target}"],
            cwd=worktree_path, capture_output=True, text=True, env=env, timeout=120,
        )
        blob = (result.stderr + "\n" + result.stdout).strip()
        if result.returncode == 0:
            break
        if attempt == max_attempts or not _is_transient_gerrit_push_failure(blob):
            break

        backoff = _GERRIT_PUSH_RETRY_BACKOFFS[attempt - 1]
        note = (
            f"attempt {attempt}/{max_attempts} failed with transient Gerrit "
            f"push error; retrying in {backoff}s"
        )
        retry_notes.append(note)
        log.warning("%s: %s", note, blob[-500:])
        time.sleep(backoff)

    if result is None:
        return GerritPushResult(False, None, None, "git push did not run")

    if result.returncode != 0:
        detail = blob[-1500:]
        if retry_notes:
            detail = "\n".join([*retry_notes, detail])
        if change_id and retry_notes:
            try:
                change = query_gerrit_change_by_change_id(
                    change_id, agent_class, instance_id, identity=identity
                )
            except Exception as exc:  # noqa: BLE001 - preserve original push failure path
                return GerritPushResult(
                    False,
                    None,
                    None,
                    f"{detail}\nGerrit recovery query failed: {type(exc).__name__}: {exc}",
                )
            if change:
                note = (
                    "Recovered after transient Gerrit push retries were exhausted: "
                    f"{'; '.join(retry_notes)}. Gerrit query change:{change_id} "
                    f"found Change #{change.change_number}."
                )
                return GerritPushResult(
                    True,
                    change.change_number,
                    change.change_url,
                    detail,
                    recovery_note=note,
                )
        return GerritPushResult(False, None, None, detail)

    m = _GERRIT_CHANGE_URL_RE.search(blob)
    if not m:
        return GerritPushResult(False, None, None, f"push succeeded but Change URL not parsed:\n{blob[-1500:]}")

    change_number = int(m.group(2))
    change_url = m.group(1)
    try:
        from backend.agents import auto_rebase, pre_review_self_fix

        self_fix = pre_review_self_fix.self_fix_mergeability(
            worktree_path=worktree_path,
            change_number=change_number,
            gerrit_ssh_url=identity.ssh_url,
            rest_base_url=GERRIT_HOOK_URL.rsplit("/tools/", 1)[0],
            username=bot_username,
            http_password=auto_rebase.load_owner_http_password(bot_username),
            target=target,
        )
    except Exception as exc:  # noqa: BLE001 - keep original push result diagnosable
        warning = f"pre-review mergeability self-fix failed: {type(exc).__name__}: {exc}"
        return GerritPushResult(
            True,
            change_number,
            change_url,
            blob[-1500:],
            post_push_warning=warning,
        )
    if not self_fix.mergeable:
        exhaustion_note = ""
        if self_fix.cap_exhausted:
            try:
                escalation_key = file_pre_review_self_fix_exhaustion_ticket(
                    make_client(agent_class, instance_id),
                    source_ticket_key=_infer_ticket_key_from_worktree(worktree_path),
                    change_number=change_number,
                    change_url=change_url,
                    change_id=change_id,
                    attempts=self_fix.attempts,
                    target=target,
                    detail=self_fix.detail,
                    diff_context=_pre_review_self_fix_diff_context(worktree_path, target),
                )
                exhaustion_note = f" Filed escalation ticket {escalation_key}."
            except Exception as exc:  # noqa: BLE001 - preserve original push failure path
                exhaustion_note = (
                    " Exhaustion escalation ticket filing failed: "
                    f"{type(exc).__name__}: {exc}."
                )
        warning = (
            "pre-review mergeability self-fix did not produce a mergeable "
            f"patchset: {self_fix.detail}.{exhaustion_note}"
        )
        return GerritPushResult(
            True,
            change_number,
            change_url,
            blob[-1500:],
            post_push_warning=warning,
        )

    recovery_note = ""
    if self_fix.force_pushed:
        recovery_note = (
            "Pre-review self-fix rebased and force-pushed replacement "
            f"patchset after mergeable=false ({self_fix.attempts} attempt(s))."
        )

    return GerritPushResult(
        success=True,
        change_number=change_number,
        change_url=change_url,
        detail=blob[-1500:],
        recovery_note=recovery_note,
    )


def already_merged_in_gerrit(
    jira_key: str,
    agent_class: str = "subscription-codex",
    instance_id: str | None = None,
    gerrit_project: str | None = None,
) -> GerritMergedInfo | None:
    """Return merged sibling change info for ``jira_key``, if Gerrit has one."""

    bot_username, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    # R.4: gerrit_project overrides the default productizer scope for a routed
    # ticket; None keeps the byte-identical productizer query.
    query = f"status:merged project:{gerrit_project or GERRIT_PROJECT_PATH} {jira_key}"
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(GERRIT_SSH_PORT),
        f"{bot_username}@{GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON", query,
    ]
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in result.stdout.splitlines():
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if change.get("type") == "stats":
            continue
        subject = str(change.get("subject") or "")
        # Gap D (dogfood 2026-07-01): match ONLY the change that IS for this
        # ticket — its bracketed owning tag ``[OP-123]`` / ``[OP-123/foo]`` in
        # the subject (the runner always stamps it). The old test accepted a
        # bare ``jira_key`` anywhere in the subject OR the change JSON body, so
        # a different ticket's merged change that merely referenced this key in
        # prose falsely reported it "already merged" → H12 auto-walk to
        # Published. A contextual mention is not ownership.
        if not re.search(r"\[" + re.escape(jira_key) + r"(?:/[^\]]*)?\]", subject):
            continue
        number = str(change.get("number") or change.get("_number") or "")
        if number:
            return GerritMergedInfo(number, subject)
    return None


TODO_STATUS_NAMES = {"To Do"}
IN_PROGRESS_STATUS_NAMES = {"In Progress", "進行中"}
UNDER_REVIEW_STATUS_NAME = "Under Review"
UNDER_REVIEW_STATUS_NAMES = {UNDER_REVIEW_STATUS_NAME}
APPROVED_STATUS_NAMES = {"Approved", "承認済み"}
PUBLISHED_STATUS_NAMES = {"Published", "公開済み"}
ARCHIVED_STATUS_NAMES = {"Archived"}


def get_issue_status(client: "DispatchClient", key: str) -> str:
    """Return current status name for ``key`` (e.g. "In Progress", "Under Review").

    Used by the runner's Phase 1.5 idempotency gate (OP-691): if the agent
    already transitioned the ticket itself, skip the runner's duplicate
    comment + transition.
    """
    issue = _request(client, "GET", f"/issue/{key}?fields=status")
    return str(((issue.get("fields") or {}).get("status") or {}).get("name", ""))


def post_runner_pushed_comment(
    client: "DispatchClient",
    key: str,
    gerrit_change_url: str,
    idem_key: str | None = None,
) -> None:
    """Post the ``[runner-pushed-to-gerrit]`` comment with the Gerrit URL.

    Split out from :func:`transition_to_under_review` (OP-691) so callers
    can post the comment without coupling it to the transition POST. The
    runner reads status first and only calls this when the ticket is not
    already in Under Review (avoiding the duplicate-comment audit dirt
    seen on OP-690 2026-05-07).
    """
    add_comment(
        client, key,
        (
            f"[runner-pushed-to-gerrit] Patchset on Gerrit: {gerrit_change_url}\n\n"
            f"Operator: +2 in Gerrit. Once merged, the gerrit-jira-bridge daemon "
            f"(`backend/agents/gerrit_jira_bridge.py`) will auto-transition "
            f"Approved → Published within ~5s."
        ),
        idem_key=idem_key,
    )


def _under_review_idem_key(
    key: str,
    *,
    change_number: int | None = None,
    change_url: str | None = None,
) -> str:
    operation_type = "transition"
    if change_number is not None:
        material = json.dumps(
            {"target_status": "under-review", "patchset": change_number},
            sort_keys=True,
            separators=(",", ":"),
        )
        args_hash = hashlib.sha256(material.encode()).hexdigest()[:12]
    elif change_url:
        args_hash = hashlib.sha256(change_url.encode()).hexdigest()[:12]
    else:
        material = json.dumps(
            {"target_status": "under-review", "patchset": None},
            sort_keys=True,
            separators=(",", ":"),
        )
        args_hash = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"{key}:{operation_type}:{args_hash}"


def transition_to_under_review_if_needed(
    client: "DispatchClient",
    key: str,
    idem_key: str | None = None,
    change_number: int | None = None,
) -> bool:
    """In Progress → Under Review, but only if not already there.

    Returns True if a transition POST was issued, False if the ticket was
    already in Under Review and the call was a no-op. Raises RuntimeError
    on any non-4xx HTTP failure of the transition POST itself; the runner
    catches and downgrades to a skip-comment per OP-691 AC.
    """
    if get_issue_status(client, key) == UNDER_REVIEW_STATUS_NAME:
        return False
    idem_key = idem_key or (
        _under_review_idem_key(key, change_number=change_number)
        if change_number is not None
        else f"transition-{key}-under-review-{uuid.uuid4().hex[:12]}"
    )
    _request_idempotent(
        client, "POST", f"/issue/{key}/transitions",
        {"transition": {"id": TRANSITION_IDS["to_under_review"]}},
        idem_key,
    )
    return True


def force_walk_to_published(
    client: "DispatchClient",
    key: str,
    idem_key: str | None = None,
) -> None:
    """Walk a runner ticket from its current state to Published.

    Used when Gerrit reports ``no new changes`` and a merged sibling already
    contains the work. The normal bridge may have missed the earlier
    change-merged event, so this function performs the same permissive
    convergence path as the Gerrit/JIRA bridge.
    """

    status = get_issue_status(client, key)
    if status in PUBLISHED_STATUS_NAMES:
        return

    base_key = idem_key or f"force-publish-{key}-{uuid.uuid4().hex[:12]}"
    if status in IN_PROGRESS_STATUS_NAMES:
        _request_idempotent(
            client,
            "POST",
            f"/issue/{key}/transitions",
            {"transition": {"id": TRANSITION_IDS["to_under_review"]}},
            f"{base_key}-under-review",
        )
        status = UNDER_REVIEW_STATUS_NAME
    if status == UNDER_REVIEW_STATUS_NAME:
        _request_idempotent(
            client,
            "POST",
            f"/issue/{key}/transitions",
            {"transition": {"id": TRANSITION_IDS["to_approved"]}},
            f"{base_key}-approved",
        )
        status = "Approved"
    if status in APPROVED_STATUS_NAMES:
        _request_idempotent(
            client,
            "POST",
            f"/issue/{key}/transitions",
            {"transition": {"id": TRANSITION_IDS["to_published"]}},
            f"{base_key}-published",
        )
        return

    raise RuntimeError(f"cannot force-publish {key} from status {status!r}")


def transition_to_under_review(
    client: "DispatchClient",
    key: str,
    gerrit_change_url: str,
    idem_key: str | None = None,
) -> None:
    """JIRA In Progress → Under Review, with Gerrit URL in a comment.

    Backward-compat wrapper around :func:`post_runner_pushed_comment` +
    :func:`transition_to_under_review_if_needed` (OP-691). Idempotent: if
    the ticket is already in Under Review, both the comment post and the
    transition POST are skipped.

    Operator handles +2 → Approved. OP-689 ships the events-stream
    consumer that handles Gerrit submit → Published.
    """
    if get_issue_status(client, key) == UNDER_REVIEW_STATUS_NAME:
        return
    base_key = idem_key or _under_review_idem_key(key, change_url=gerrit_change_url)
    post_runner_pushed_comment(client, key, gerrit_change_url, idem_key=f"{base_key}-comment")
    transition_to_under_review_if_needed(client, key, idem_key=f"{base_key}-transition")


def force_walk_to_published(
    client: "DispatchClient",
    key: str,
    idem_key: str | None = None,
) -> bool:
    """Force-walk a safe predecessor state to Published.

    Used by runner-side H12 self-heal after Gerrit has already confirmed a
    merged change for the ticket. Returns True when one or more transition
    POSTs were issued, False when the ticket was already Published.
    """
    status = get_issue_status(client, key)
    if status in PUBLISHED_STATUS_NAMES:
        return False
    if status in ARCHIVED_STATUS_NAMES:
        raise RuntimeError(f"{key} is archived; refusing to force-publish")

    if status in TODO_STATUS_NAMES:
        steps = ("to_in_progress", "to_under_review", "to_approved", "to_published")
    elif status in IN_PROGRESS_STATUS_NAMES:
        steps = ("to_under_review", "to_approved", "to_published")
    elif status in UNDER_REVIEW_STATUS_NAMES:
        steps = ("to_approved", "to_published")
    elif status in APPROVED_STATUS_NAMES:
        steps = ("to_published",)
    else:
        raise RuntimeError(f"{key} has unsupported status for force-publish: {status}")

    base_key = idem_key or f"force-publish-{key}-{uuid.uuid4().hex[:12]}"
    for idx, transition_name in enumerate(steps, start=1):
        _request_idempotent(
            client,
            "POST",
            f"/issue/{key}/transitions",
            {"transition": {"id": TRANSITION_IDS[transition_name]}},
            f"{base_key}-{idx}-{transition_name}",
        )
    return True


# ── OP-956 — runner ops-only ticket type ─────────────────────────
#
# Ops-only tickets describe operator/automation runbooks where the
# expected CLI output is reports + audit comments, not commits.
# Without the label, the OP-827 ``[runner-no-commits-from-cli]``
# safety check reverts the ticket every time the CLI exits cleanly
# with 0 commits, forcing the operator to hand-walk the workflow
# (To Do → In Progress → Under Review → Approved → Published) for
# every ops-only ticket — see OP-923 incident 2026-05-12.

OPS_ONLY_LABEL = "runner:no-commits-expected"


class WorkflowTransitionPermissionRefused(RuntimeError):
    """Raised by :func:`forward_transition_ops_only` when JIRA refuses a
    workflow transition (HTTP 403). The runner catches this and falls
    back to operator notification per OP-956 AC error catalog.
    """

    def __init__(self, key: str, transition_name: str, detail: str) -> None:
        super().__init__(
            f"{key}: workflow transition {transition_name!r} refused: {detail}"
        )
        self.key = key
        self.transition_name = transition_name
        self.detail = detail


def has_ops_only_label(labels: Iterable[str]) -> bool:
    """Return True if the ops-only sigil label is in ``labels``.

    Defensive against ``LabelInjectionAttack`` per OP-956 error catalog:
    we match the exact sigil string and never interpret label values.
    """
    return OPS_ONLY_LABEL in set(labels)


def fetch_ticket_labels(client: "DispatchClient", key: str) -> tuple[str, ...]:
    """Return the *current* JIRA label set for ``key``.

    OP-958 ``LabelsPropagationDrift``: the runner's ``TicketSnapshot``
    carries a point-in-time label copy taken at *selection* time (JQL
    ``fetch_pickable_tickets`` → :func:`to_snapshot`, or a single
    ``GET /issue`` for the target-override path). Operators routinely add
    ``runner:no-commits-expected`` *after* a ticket is already In
    Progress — the OP-925 R3 cascade on 2026-05-12 is the canonical
    case — so a stale snapshot makes :func:`has_ops_only_label` miss the
    sigil even though it is on the ticket. Re-reading at the no-commits
    decision point closes that drift window. Callers degrade to the
    snapshot copy if this raises (JIRA transient fault).
    """
    issue = _request(client, "GET", f"/issue/{key}?fields=labels")
    return tuple((issue.get("fields") or {}).get("labels") or ())


def forward_transition_ops_only(
    client: "DispatchClient",
    key: str,
    idem_key: str | None = None,
) -> None:
    """Walk an ops-only ticket from current status to 公開済み (Published).

    OP-956 AC #2: ``runner:no-commits-expected`` tickets do not produce
    commits, so the OP-827 revert-on-zero-commits path is wrong for them.
    Instead we forward-transition via the workflow's Submit-for-Review →
    Approve → Deploy chain (transition ids 3 → 4 → 7 in the OP project
    workflow) and post a ``[runner-ops-only-transition]`` audit comment
    per transition step.

    Idempotent: status is re-read between steps so a partial walk (e.g.
    a previous tick crashed mid-walk) can resume cleanly. Already-
    Published tickets are a no-op.

    Raises :class:`WorkflowTransitionPermissionRefused` if any transition
    POST returns 403 — the caller is expected to log + notify the
    operator and leave the ticket in its current state.
    """
    status = get_issue_status(client, key)
    if status in PUBLISHED_STATUS_NAMES:
        return
    if status in ARCHIVED_STATUS_NAMES:
        raise RuntimeError(f"{key} is archived; refusing to ops-forward")

    if status in IN_PROGRESS_STATUS_NAMES:
        steps = ("to_under_review", "to_approved", "to_published")
    elif status in UNDER_REVIEW_STATUS_NAMES:
        steps = ("to_approved", "to_published")
    elif status in APPROVED_STATUS_NAMES:
        steps = ("to_published",)
    else:
        raise RuntimeError(
            f"{key} has unsupported status for ops-only forward: {status!r}"
        )

    base_key = idem_key or f"ops-only-fwd-{key}-{uuid.uuid4().hex[:12]}"
    for idx, transition_name in enumerate(steps, start=1):
        try:
            _request_idempotent(
                client,
                "POST",
                f"/issue/{key}/transitions",
                {"transition": {"id": TRANSITION_IDS[transition_name]}},
                f"{base_key}-{idx}-{transition_name}",
            )
        except RuntimeError as exc:
            # The transport raises `RuntimeError("POST ... → 403: ...")`
            # for HTTP errors — sniff the 403 prefix on the formatted
            # message rather than introducing a new exception type at
            # the transport layer.
            if " → 403:" in str(exc):
                raise WorkflowTransitionPermissionRefused(
                    key, transition_name, str(exc)
                ) from exc
            raise
        add_comment(
            client,
            key,
            (
                f"[runner-ops-only-transition] CLI exit=0 + 0 commits + "
                f"ticket-label={OPS_ONLY_LABEL} → auto-forward "
                f"({transition_name})"
            ),
            idem_key=f"{base_key}-{idx}-{transition_name}-comment",
        )


def transition_to_in_progress(client: DispatchClient, key: str, idem_key: str | None = None) -> None:
    """Set assignee = bot, transition TODO → In Progress, add pickup comment."""
    base_key = idem_key or f"transition-{key}-in-progress-{uuid.uuid4().hex[:12]}"
    _request_idempotent(
        client, "PUT", f"/issue/{key}",
        {"fields": {"assignee": {"accountId": client.bot_account_id}}},
        f"{base_key}-assign",
    )
    _request_idempotent(
        client, "POST", f"/issue/{key}/transitions",
        {"transition": {"id": TRANSITION_IDS["to_in_progress"]}},
        f"{base_key}-transition",
    )
    add_comment(
        client, key, f"Picked up by {client.agent_class} runner.",
        idem_key=f"{base_key}-comment",
    )


def transition_back_to_todo(
    client: DispatchClient,
    key: str,
    reason: str,
    idem_key: str | None = None,
    *,
    failure_class: "str | None" = None,
    raw_traceback: str = "",
    mutex_label: str | None = None,
    area: str | None = None,
) -> None:
    """In Progress → TODO with reason comment + cleanup ownership markers.

    OP-1541 invariant: this is a destructive recovery — it clears the
    assignee. Under a shared JIRA account ONLY the current winning
    fencing-token claim holder may revert. Callers on the TOCTOU /
    assignee-divergence path MUST first prove claim ownership (see
    :func:`is_winning_claim_owner` and ``auto-runner-jira._handle_toctou_abort``)
    so a sibling instance's assignee clear does not cascade into a false
    revert (OP-1533 burst → stoploss). The non-recovery callers (CLI
    failure, capability gate, discovered dependency, pre-claim cwd-unsafe)
    are unaffected — they own or predate the claim.

    OP-854 (C2): when ``failure_class`` is supplied, an incident row
    lands in ``runner_incidents`` before the transition fires so the
    C8 failure-graph and the C2 recall path can pick it up next time
    the ticket is attempted. Recording is best-effort — a recorder
    fault never blocks the JIRA transition (the operator still needs
    the ticket reverted even if the audit shim is wedged).
    """
    base_key = idem_key or f"transition-{key}-back-to-todo-{uuid.uuid4().hex[:12]}"
    if failure_class is not None:
        try:
            # Local import keeps jira_dispatch importable without the
            # incident_recorder + failure_class chain (e.g. early-boot
            # JQL-only smoke tests).
            from backend.agents.incident_recorder import record_runner_incident

            record_runner_incident(
                ticket_key=key,
                failure_class=failure_class,
                summary=reason.splitlines()[0] if reason else "",
                raw_traceback=raw_traceback,
                runner_class=getattr(client, "agent_class", "unknown"),
                mutex_label=mutex_label,
                area=area,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort wiring
            log.warning(
                "jira_dispatch.record_runner_incident_failed key=%s err=%s",
                key,
                exc,
            )
    add_comment(
        client, key, f"Reverting to TODO. Reason:\n{reason}",
        idem_key=f"{base_key}-comment",
    )
    cleanup_reverted_ticket_claims_and_assignee(
        client, key, idem_key=f"{base_key}-cleanup-claim-assignee",
    )
    _request_idempotent(
        client, "POST", f"/issue/{key}/transitions",
        {"transition": {"id": TRANSITION_IDS["back_to_todo"]}},
        f"{base_key}-transition",
    )

    # OP-1140: stoploss circuit-breaker recording — best-effort.
    # A recorder fault (network blip, label-fetch 404 on a freshly-created
    # ticket, ...) must NEVER prevent the §11-revert from completing; the
    # operator still needs the ticket re-armed for triage even if the
    # counter slips one tick.
    try:
        from backend.agents import runner_stoploss

        labels_now = fetch_labels(client, key)
        runner_stoploss.register_revert(client, key, labels_now)
    except Exception as exc:  # noqa: BLE001 — best-effort wiring
        log.warning(
            "jira_dispatch.register_revert_failed key=%s err=%s", key, exc,
        )


def add_comment(client: DispatchClient, key: str, text: str, idem_key: str | None = None) -> None:
    idem_key = idem_key or f"comment-{key}-{uuid.uuid4().hex[:12]}"
    _request_idempotent(
        client, "POST", f"/issue/{key}/comment",
        {"body": _adf_paragraph(text)},
        idem_key,
    )


def clear_assignee(client: DispatchClient, key: str, idem_key: str | None = None) -> None:
    idem_key = idem_key or f"clear-assignee-{key}-{uuid.uuid4().hex[:12]}"
    _request_idempotent(
        client, "PUT", f"/issue/{key}",
        {"fields": {"assignee": None}},
        idem_key,
    )


def cleanup_reverted_ticket_claims_and_assignee(
    client: DispatchClient,
    key: str,
    idem_key: str | None = None,
) -> None:
    """Drop all claim labels and clear assignee for an owned revert path.

    This helper is intentionally called only from revert / abstain paths,
    after the runner has decided it owns the recovery. It must not be used
    as generic post-run cleanup because active sibling runners rely on
    assignee + winning ``claim:*`` labels while their CLI is still running.
    """
    issue = _request(client, "GET", f"/issue/{key}?fields=labels,assignee")
    fields = issue.get("fields") or {}
    labels = list(fields.get("labels") or [])
    claim_labels = [
        label for label in labels
        if isinstance(label, str) and label.startswith(CLAIM_LABEL_PREFIX)
    ]
    assignee = fields.get("assignee")
    if not claim_labels and not assignee:
        return

    body: dict[str, Any] = {}
    if assignee:
        body["fields"] = {"assignee": None}
    if claim_labels:
        body["update"] = {
            "labels": [{"remove": label} for label in sorted(set(claim_labels))]
        }
    _request_idempotent(
        client,
        "PUT",
        f"/issue/{key}",
        body,
        idem_key or f"cleanup-claim-assignee-{key}-{uuid.uuid4().hex[:12]}",
    )


def add_label(client: DispatchClient, key: str, label: str, idem_key: str | None = None) -> None:
    """Add one JIRA label without replacing the existing label set."""
    idem_key = idem_key or f"label-add-{key}-{label}-{uuid.uuid4().hex[:12]}"
    _request_idempotent(
        client, "PUT", f"/issue/{key}",
        {"update": {"labels": [{"add": label}]}},
        idem_key,
    )


def remove_label(client: DispatchClient, key: str, label: str, idem_key: str | None = None) -> None:
    """Remove one JIRA label if present; JIRA treats absent labels as a no-op."""
    idem_key = idem_key or f"label-remove-{key}-{label}-{uuid.uuid4().hex[:12]}"
    _request_idempotent(
        client, "PUT", f"/issue/{key}",
        {"update": {"labels": [{"remove": label}]}},
        idem_key,
    )


def dependency_waiting_label(blocker_key: str) -> str:
    """Return the runner label used while ``blocker_key`` gates pickup."""
    return f"{DEPENDENCY_WAITING_LABEL_PREFIX}{blocker_key}"


def dependency_waiting_labels(labels: Iterable[str]) -> list[str]:
    """Return runner dependency-waiting labels from a JIRA label list."""
    return sorted(label for label in labels if label.startswith(DEPENDENCY_WAITING_LABEL_PREFIX))


# ── Description / Prerequisites parsing ───────────────────────────


def fetch_labels(client: DispatchClient, key: str) -> list[str]:
    """Return the current label list for ``key``. Stoploss callers need this
    after a §11-revert to count recent revert labels (OP-1140)."""
    issue = _request(client, "GET", f"/issue/{key}?fields=labels")
    return list((issue.get("fields") or {}).get("labels") or [])


def fetch_description(client: DispatchClient, key: str) -> str:
    """Pull description as markdown text from ADF code-block payload."""
    issue = _request(client, "GET", f"/issue/{key}?fields=description")
    desc = issue["fields"].get("description")
    if not desc:
        return ""
    # Walk ADF tree and concat text nodes (handles codeBlock content)
    chunks: list[str] = []
    def _walk(node):
        if isinstance(node, dict):
            t = node.get("type")
            if t == "text":
                chunks.append(node.get("text", ""))
            elif t == "hardBreak":
                chunks.append("\n")
            elif t == "paragraph":
                for c in node.get("content", []):
                    _walk(c)
                chunks.append("\n\n")
            elif t == "codeBlock":
                for c in node.get("content", []):
                    _walk(c)
                chunks.append("\n")
            else:
                for c in node.get("content", []):
                    _walk(c)
    _walk(desc)
    return "".join(chunks)


# ── File-level pickup mutex (OP-731, extended in OP-800) ───────────

FILE_COLLISION_SKIP_LABEL = "runner-skipped:file-collision"
DEPENDENCY_WAITING_LABEL_PREFIX = "runner-blocked:waiting-"
# OP-800: operator escape hatch — bypass the file-overlap gate when the
# operator has an explicit hand-merge plan. Symmetric with
# ``MIGRATION_OVERRIDE_LABEL`` for migration-freeze scenarios.
FILE_OVERLAP_OVERRIDE_LABEL = "runner-mutex-override:file-overlap"
PATTERN_12_COOKBOOK_LINK = (
    "docs/sop/architecture-anti-patterns.md"
    "#12-spike--final-version-addadd-scaffold-race"
)


@dataclass(frozen=True)
class GerritFileOwner:
    """One open Gerrit patch set touching a file."""

    change_number: str
    owner: str


@dataclass(frozen=True)
class MigrationFreeze:
    """One active migration ticket and the path globs it freezes."""

    key: str
    scope_globs: tuple[str, ...]


def parse_files_section_from_description(description: str) -> set[str]:
    """Backwards-compatible alias for :func:`scope_to_paths.parse_files_section`."""
    return parse_files_section(description)


def predict_target_files(
    snapshot: TicketSnapshot,
    description: str | None = None,
) -> set[str]:
    """Predict target paths for file-level mutex checks.

    Precedence:
    1. always-touched convention paths plus explicit ``Files / Paths`` section;
    2. always-touched convention paths plus first ``scope:<name>`` label mapped
       in :mod:`scope_to_paths`;
    3. always-touched convention paths.

    Per-ticket lesson path is added via ALWAYS_TOUCHED_TEMPLATE (OP-795 Bug 1):
    each ticket predicts its own ``L-{ticket}-*.md`` glob so two tickets writing
    different lessons never collide on a shared wildcard.
    """
    files = set(ALWAYS_TOUCHED)
    files.add(ALWAYS_TOUCHED_TEMPLATE.format(ticket=snapshot.key))
    explicit = parse_files_section(description or getattr(snapshot, "description", ""))
    if explicit:
        return files | explicit

    scope = next(
        (label.split(":", 1)[1] for label in getattr(snapshot, "labels", ()) if label.startswith("scope:")),
        None,
    )
    if scope and scope in SCOPE_TO_PATHS:
        return files | set(SCOPE_TO_PATHS[scope])
    return files


def _open_bot_owned_file_owners(gerrit_project: str | None = None) -> dict[str, list[GerritFileOwner]]:
    """Return file → open bot-owned Gerrit PS metadata.

    Per OP-783, file-mutex must catch PSes owned by per-instance bots
    (``codex-bot-2``, ``claude-bot-3`` ...) too — otherwise instance-2's
    in-flight work is invisible to instance-3 and they collide on the
    same path. The query uses ``ownerin:`` group membership when an
    operator-managed group exists; the default falls back to a broad
    ``is:open`` filter with client-side bot-prefix filtering. The
    Gerrit-side filter is preferred for cost; the client-side filter is
    correctness.
    """
    _, ssh_key = _GERRIT_AUTH_BY_CLASS["subscription-claude"]
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(GERRIT_SSH_PORT),
        f"claude-bot@{GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON", "--current-patch-set", "--files",
        f"{_project_filter(gerrit_project)}is:open".strip(),
    ]
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run, cmd, capture_output=True, text=True, timeout=15
    )
    result.check_returncode()

    owners: dict[str, list[GerritFileOwner]] = {}
    for line in result.stdout.splitlines():
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if change.get("type") == "stats":
            continue
        change_number = str(change.get("number") or change.get("_number") or "?")
        owner = str(((change.get("owner") or {}).get("username")) or "?")
        # Client-side bot filter: catches codex-bot, codex-bot-2, claude-bot-3, ...
        if not (owner.startswith("codex-bot") or owner.startswith("claude-bot")):
            continue
        for file_info in (change.get("currentPatchSet") or {}).get("files", []):
            path = file_info.get("file")
            if path and path != "/COMMIT_MSG":
                owners.setdefault(path, []).append(GerritFileOwner(change_number, owner))
    return owners


def open_bot_owned_files() -> set[str]:
    """Return file paths covered by currently-open bot-owned Gerrit patch sets."""
    return set(_open_bot_owned_file_owners())


def _patchset_parent_revision(patchset: dict) -> str | None:
    parents = patchset.get("parents") or []
    if not parents:
        return None
    first = parents[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        return str(first.get("revision") or first.get("commit") or "") or None
    return None


def _patchset_upload_ts(patchset: dict) -> float | None:
    for key in ("createdOn", "created_on", "uploadedOn", "uploaded_on"):
        raw = patchset.get(key)
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str) and raw.isdigit():
            return float(raw)

    raw_dt = patchset.get("created") or patchset.get("uploaded")
    if not isinstance(raw_dt, str) or not raw_dt.strip():
        return None
    normalized = raw_dt.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", normalized):
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def assess_patchset_staleness(
    patchset: dict,
    *,
    now_ts: float | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    cwd: Path | None = None,
    develop_ref: str = "origin/develop",
) -> PatchSetStaleness | None:
    """Return PS age/commit-distance staleness, or ``None`` when unavailable."""
    ps_parent = _patchset_parent_revision(patchset)
    upload_ts = _patchset_upload_ts(patchset)
    if not ps_parent or upload_ts is None:
        return None

    current = now_ts if now_ts is not None else time.time()
    age_days = max(0.0, (current - upload_ts) / 86400.0)
    runner = run_command if run_command is not None else subprocess.run
    try:
        result = runner(
            ["git", "rev-list", "--count", f"{ps_parent}..{develop_ref}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        commits_behind = int((result.stdout or "0").strip() or "0")
    except ValueError:
        return None

    reasons: list[str] = []
    level = "fresh"
    if age_days > PS_STALENESS_ABSTAIN_DAYS:
        reasons.append(f"age_days>{PS_STALENESS_ABSTAIN_DAYS:g}")
        level = "abstain"
    if commits_behind > PS_STALENESS_ABSTAIN_COMMITS:
        reasons.append(f"commits_behind>{PS_STALENESS_ABSTAIN_COMMITS}")
        level = "abstain"
    if level != "abstain":
        if age_days > PS_STALENESS_WARN_DAYS:
            reasons.append(f"age_days>{PS_STALENESS_WARN_DAYS:g}")
            level = "warn"
        if commits_behind > PS_STALENESS_WARN_COMMITS:
            reasons.append(f"commits_behind>{PS_STALENESS_WARN_COMMITS}")
            level = "warn"

    return PatchSetStaleness(
        age_days=age_days,
        commits_behind=commits_behind,
        ps_parent=ps_parent,
        level=level,
        reason=", ".join(reasons) if reasons else "fresh",
    )


def _format_ps_staleness_comment(
    *,
    marker: str,
    assessment: PatchSetStaleness,
    change_number: str | int | None = None,
) -> str:
    change = f" change={change_number}" if change_number else ""
    action = (
        "Abstaining until the patchset is manually rebased."
        if assessment.should_abstain
        else "Warning only; runner pickup may proceed."
    )
    return (
        f"[{marker}] {action}\n"
        f"- age_days={assessment.age_days:.1f}\n"
        f"- commits_behind={assessment.commits_behind}\n"
        f"- ps_parent={assessment.ps_parent[:12]}\n"
        f"- reason={assessment.reason}{change}\n"
        "Suggested action: rebase the Gerrit patchset manually onto "
        "`origin/develop`, then retry runner pickup."
    )


def _query_open_change_for_ticket_staleness(
    key: str,
    agent_class: str,
    instance_id: str | None = None,
    gerrit_project: str | None = None,
) -> dict | None:
    try:
        gerrit_user, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    except ValueError:
        return None
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(GERRIT_SSH_PORT),
        f"{gerrit_user}@{GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON", "--current-patch-set",
        f"{_project_filter(gerrit_project)}status:open".strip(), "branch:develop",
    ]
    try:
        result = BREAKERS["gerrit_ssh"].call(
            subprocess.run, cmd, capture_output=True, text=True, timeout=15
        )
        result.check_returncode()
    except (OSError, subprocess.SubprocessError):
        return None

    for line in result.stdout.splitlines():
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if change.get("type") == "stats":
            continue
        subject = str(change.get("subject") or "")
        if key in subject:
            return change
    return None


def pickup_staleness_check(
    client: DispatchClient,
    key: str,
    *,
    now_ts: float | None = None,
    worktree_path: Path | None = None,
    gerrit_project: str | None = None,
) -> tuple[bool, str]:
    """Warn/abstain if an existing open PS for ``key`` is stale.

    R.4: *gerrit_project* scopes the open-change scan to the ticket's own
    project; ``None`` → unchanged (all open develop changes)."""
    change = _query_open_change_for_ticket_staleness(
        key, client.agent_class, gerrit_project=gerrit_project
    )
    if change is None:
        return True, "ps staleness check skipped: no open Gerrit PS found"
    assessment = assess_patchset_staleness(
        change.get("currentPatchSet") or {},
        now_ts=now_ts,
        cwd=worktree_path,
    )
    if assessment is None or assessment.level == "fresh":
        return True, "ps staleness check passed"

    marker = (
        "runner-ps-staleness-abstain"
        if assessment.should_abstain
        else "runner-ps-staleness-warn"
    )
    add_comment(
        client,
        key,
        _format_ps_staleness_comment(
            marker=marker,
            assessment=assessment,
            change_number=change.get("number") or change.get("_number"),
        ),
        idem_key=f"{marker}-{key}",
    )
    if assessment.should_abstain:
        return (
            False,
            "ps staleness exceeded: "
            f"age_days={assessment.age_days:.1f} "
            f"commits_behind={assessment.commits_behind}",
        )
    return (
        True,
        "ps staleness warning: "
        f"age_days={assessment.age_days:.1f} "
        f"commits_behind={assessment.commits_behind}",
    )


def _open_bot_owned_patch_signals(gerrit_project: str | None = None) -> list[feature_dup_detector.PatchSignal]:
    """Return open bot-owned Gerrit PS signals for feature-dup detection."""

    _, ssh_key = _GERRIT_AUTH_BY_CLASS["subscription-claude"]
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(GERRIT_SSH_PORT),
        f"claude-bot@{GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON", "--current-patch-set", "--files",
        f"{_project_filter(gerrit_project)}is:open".strip(),
    ]
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run, cmd, capture_output=True, text=True, timeout=15
    )
    result.check_returncode()

    signals: list[feature_dup_detector.PatchSignal] = []
    for line in result.stdout.splitlines():
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if change.get("type") == "stats":
            continue
        owner = str(((change.get("owner") or {}).get("username")) or "?")
        if not (owner.startswith("codex-bot") or owner.startswith("claude-bot")):
            continue
        signal = feature_dup_detector.patch_signal_from_gerrit_change(change)
        if signal is not None:
            signals.append(signal)
    return signals


def _feature_dup_candidate_signal(
    snapshot: TicketSnapshot,
    description: str,
    open_signals: list[feature_dup_detector.PatchSignal],
) -> feature_dup_detector.PatchSignal:
    """Prefer the ticket's open PS signal; fall back to planned file paths."""

    for signal in open_signals:
        if signal.ticket_key == snapshot.key:
            return feature_dup_detector.PatchSignal(
                ticket_key=signal.ticket_key,
                change_number=signal.change_number,
                subject=signal.subject,
                url=signal.url,
                files_changed=signal.files_changed,
                added_symbols=signal.added_symbols,
                description=description,
                areas=feature_dup_detector.areas_from_labels(snapshot.labels),
            )
    return feature_dup_detector.PatchSignal(
        ticket_key=snapshot.key,
        files_changed=frozenset(predict_target_files(snapshot, description=description)),
        description=description,
        areas=feature_dup_detector.areas_from_labels(snapshot.labels),
    )


def _feature_dup_idem_key(left_key: str, right_key: str, reasons: Iterable[str]) -> str:
    pair = "-".join(sorted((left_key, right_key)))
    reason_key = "-".join(sorted(reasons))
    return f"feature-dup-{pair}-{reason_key}"


def post_feature_dup_warnings(
    client: DispatchClient,
    snapshot: TicketSnapshot,
    *,
    description: str,
    open_signals: list[feature_dup_detector.PatchSignal] | None = None,
) -> list[feature_dup_detector.FeatureDupHit]:
    """Post non-blocking feature-dup warnings for the pickup candidate."""

    if open_signals is None:
        open_signals = _open_bot_owned_patch_signals()
    candidate = _feature_dup_candidate_signal(snapshot, description, open_signals)
    hits = feature_dup_detector.detect_feature_duplicates(candidate, open_signals)
    for hit in hits:
        current_comment = feature_dup_detector.format_feature_dup_warning(
            candidate, hit
        )
        reverse_hit = feature_dup_detector.FeatureDupHit(
            other=candidate,
            reasons=hit.reasons,
            shared_symbols=hit.shared_symbols,
            file_jaccard=hit.file_jaccard,
            jira_similarity=hit.jira_similarity,
        )
        other_comment = feature_dup_detector.format_feature_dup_warning(
            hit.other, reverse_hit
        )
        idem = _feature_dup_idem_key(snapshot.key, hit.other.ticket_key, hit.reasons)
        add_comment(client, snapshot.key, current_comment, idem_key=f"{idem}-current")
        add_comment(client, hit.other.ticket_key, other_comment, idem_key=f"{idem}-other")
    return hits


def _paths_overlap(targets: set[str], in_flight: set[str]) -> set[str]:
    import fnmatch

    overlaps: set[str] = set()
    for target in targets:
        for path in in_flight:
            if target == path or fnmatch.fnmatch(path, target) or fnmatch.fnmatch(target, path):
                overlaps.add(path)
    return overlaps


def _agent_class_from_snapshot(snapshot: TicketSnapshot) -> str:
    """Resolve the runner class label carried by a scheduler snapshot."""
    for label in getattr(snapshot, "labels", ()):
        if isinstance(label, str) and label.startswith("class:"):
            return label.split(":", 1)[1]
    return "subscription-codex"


def _check_active_claim_hot_overlap(
    snapshot: TicketSnapshot,
    hot_in_target: set[str],
) -> tuple[bool, str]:
    """Block hot-file pickup when another claimed ticket declares same path."""
    client = make_client(_agent_class_from_snapshot(snapshot), _instance_id_from_env())
    jql = (
        f'project = "{client.project_key}" '
        f'AND labels ~ "{CLAIM_LABEL_PREFIX}*" '
        f'AND key != "{snapshot.key}"'
    )
    resp = _request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["labels"],
        "maxResults": 50,
    })
    for issue in resp.get("issues", []):
        key = issue.get("key", "?")
        labels = ((issue.get("fields") or {}).get("labels")) or []
        if not any(isinstance(label, str) and label.startswith(CLAIM_LABEL_PREFIX) for label in labels):
            continue
        description = fetch_description(client, key)
        overlap = _paths_overlap(hot_in_target, parse_files_section(description))
        if not overlap:
            continue
        first = sorted(overlap)[0]
        return (
            False,
            (
                f"hot-file claim collision: {first} already claimed by {key}; "
                f"[runner-hot-file-mutex] pre-PS claim-level mutex blocked pickup"
            ),
        )
    return True, "no active hot-file claim collision"


def migration_scope_globs(labels: Iterable[str]) -> tuple[str, ...]:
    """Extract ``migration:scope=<glob>`` labels from a JIRA label list."""
    scopes: list[str] = []
    for label in labels:
        if label.startswith(MIGRATION_SCOPE_PREFIX):
            scope = label[len(MIGRATION_SCOPE_PREFIX):].strip()
            if scope:
                scopes.append(scope)
    return tuple(scopes)


def find_active_migrations(
    client: DispatchClient,
    exclude_key: str,
) -> list[MigrationFreeze]:
    """Return active META migrations holding path-scope freeze labels."""
    jql = (
        f'project = "{client.project_key}" '
        f'AND labels = "{MIGRATION_IN_FLIGHT_LABEL}" '
        f'AND status not in ("Published", "公開済み", "Archived") '
        f'AND key != "{exclude_key}"'
    )
    resp = _request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["labels", "status"],
        "maxResults": 50,
    })
    migrations: list[MigrationFreeze] = []
    for issue in resp.get("issues", []):
        fields = issue.get("fields") or {}
        scopes = migration_scope_globs(fields.get("labels") or [])
        if scopes:
            migrations.append(MigrationFreeze(key=issue.get("key", "?"), scope_globs=scopes))
    return migrations


def migration_freeze_check(
    client: DispatchClient,
    snapshot: TicketSnapshot,
    description: str | None = None,
) -> tuple[bool, str]:
    """Gate pickup while a META migration freezes overlapping paths."""
    target_paths = predict_target_files(snapshot, description=description)
    active = find_active_migrations(client, exclude_key=snapshot.key)
    for migration in active:
        overlap = _paths_overlap(target_paths, set(migration.scope_globs))
        if not overlap:
            continue
        first = sorted(overlap)[0]
        if MIGRATION_OVERRIDE_LABEL in set(getattr(snapshot, "labels", ())):
            add_comment(
                client,
                snapshot.key,
                (
                    "[runner-migration-override] migration:override bypassed "
                    f"{migration.key} freeze for {first}."
                ),
                idem_key=f"migration-override-{snapshot.key}-{migration.key}",
            )
            return True, f"migration override: {migration.key} overlaps {first}"
        add_comment(
            client,
            snapshot.key,
            f"[runner-migration-freeze] paused — waiting on {migration.key} migration to complete",
            idem_key=f"migration-freeze-{snapshot.key}-{migration.key}",
        )
        return False, f"migration freeze: {migration.key} overlaps {first}"
    return True, "no active migration freeze"


def file_mutex_check(
    snapshot: TicketSnapshot,
    description: str | None = None,
) -> tuple[bool, str]:
    """Return whether ``snapshot`` can be picked up without file collision.

    OP-800: extended to detect overlap with currently-open Gerrit PSes
    (Pattern 12 cure 3 — runtime layer). On collision the reason cites the
    blocker's Gerrit change number, points at the Pattern 12 cookbook entry,
    and lists the three operator resolution paths. The
    ``runner-mutex-override:file-overlap`` label on the candidate ticket
    bypasses the gate for cases where the operator has an explicit
    hand-merge plan.
    """
    target = predict_target_files(snapshot, description=description)
    if not target:
        return True, "no prediction available - mutex check skipped"

    try:
        # R.4: scope the file-mutex scan to the ticket's own Gerrit project so a
        # routed ticket doesn't collide on same relative paths with productizer
        # changes (and vice-versa). None for a normal ticket → unchanged.
        in_flight_owners = _open_bot_owned_file_owners(
            gerrit_project=gerrit_project_for_labels(getattr(snapshot, "labels", ()))
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return True, f"gerrit query failed - mutex check skipped: {type(exc).__name__}: {exc}"

    overlap = _paths_overlap(target, set(in_flight_owners))
    if not overlap:
        hot_in_target = target & HOT_FILES
        if hot_in_target:
            try:
                ok, reason = _check_active_claim_hot_overlap(snapshot, hot_in_target)
            except Exception as exc:  # noqa: BLE001 - hot-file JIRA probe fails open
                log.warning(
                    "hot-file claim mutex query failed for %s: %s: %s",
                    snapshot.key,
                    type(exc).__name__,
                    exc,
                )
                return (
                    True,
                    "no open-PS collision; hot-file claim query failed - "
                    f"pre-PS mutex skipped: {type(exc).__name__}: {exc}",
                )
            if not ok:
                return False, reason
        return True, "no collision"

    first_path = sorted(overlap)[0]
    owner = in_flight_owners[first_path][0]

    if FILE_OVERLAP_OVERRIDE_LABEL in set(getattr(snapshot, "labels", ())):
        hot_in_target = target & HOT_FILES
        if hot_in_target:
            try:
                ok, reason = _check_active_claim_hot_overlap(snapshot, hot_in_target)
            except Exception as exc:  # noqa: BLE001 - hot-file JIRA probe fails open
                log.warning(
                    "hot-file claim mutex query failed for %s: %s: %s",
                    snapshot.key,
                    type(exc).__name__,
                    exc,
                )
                ok = True
            if not ok:
                return False, reason
        return (
            True,
            f"file-overlap override: {FILE_OVERLAP_OVERRIDE_LABEL} bypassed "
            f"PS #{owner.change_number} on {first_path} (operator hand-merge plan)",
        )

    return (
        False,
        (
            f"file collision: {first_path} already in open PS #{owner.change_number} "
            f"(owner: {owner.owner})\n"
            f"Pattern 12 cure 3 ({PATTERN_12_COOKBOOK_LINK}). Resolution: "
            f"(a) merge PS #{owner.change_number} first then this ticket rebases, "
            f"(b) rebase the conflicting PS onto the merged base, or "
            f"(c) re-scope this ticket to a non-overlapping path set per Pattern 12 cure 1. "
            f"Override: add label `{FILE_OVERLAP_OVERRIDE_LABEL}` for an explicit hand-merge plan."
        ),
    )


# ── OP-838 → AUDIT-24/OP-977: atomic ticket-claim mutex ──────────────
#
# Why this exists: the JQL pickup filter (``assignee is EMPTY``) and the
# ``transition_to_in_progress`` + assign call are separated by several
# seconds of worktree prep / pre-pickup checks. Two runners ticking on
# similar wall-clock minutes can both see a ticket as pickable, both
# proceed past the pre-pickup gates, and both reach
# ``transition_to_in_progress`` — JIRA accepts both writes and both CLIs
# then race to push to Gerrit, generating duplicate Change-Ids
# (different subjects → distinct changes, one merged + one abandoned),
# or the loser's failure-recovery reverts the winner's work. Observed on
# OP-836 #356 / OP-837 #358 (2026-05-11, cross-bot) and OP-974
# (2026-05-12, same-instance — operator rescue required at 16:45).
#
# OP-838 (2026-05-11) — SUPERSEDED — added a ``claim:{instance_id}`` label
# "mutex": GET-assignee, PUT assignee+label, GET-readback. It serialises
# the *cross-bot* shape (assignee is single-valued, last-writer-wins) but
# CANNOT serialise two runners that share an ``instance_id``: Atlassian's
# ``update.labels.add`` is set-union (idempotent), not compare-and-swap,
# so both PUT the same label, both read it back, both believe they won.
# OP-974 is exactly that failure.
#
# AUDIT-24/OP-977 (2026-05-12) replaces the bare-label mutex with a
# FENCING TOKEN. Each attempt PUTs a unique-per-tick label
# ``claim:{instance_id}:{token}`` where ``token = f"{epoch_us:016d}-{uuid8}"``.
# After the PUT the runner GETs the labels back and the LOWEST token among
# ``claim:{instance_id}:*`` is the canonical winner — lexicographic order
# over the 16-digit zero-padded microsecond prefix == chronological order,
# so "lowest token" == "earliest claimer", with the uuid8 suffix breaking
# same-microsecond ties. Because the winner is decided by a *total order
# over the readback set* (not by "did my idempotent write succeed"), two
# same-instance runners agree on exactly one winner; the loser observes
# its token is not lowest and returns ``ok=False`` ("foreign claim").
# Stale tokens left by a crashed runner are swept on the next claim's
# pre-GET once aged past ``2× CLI timeout`` (``OrphanClaimLabel``).
# Pre-AUDIT-24 bare ``claim:{instance_id}`` labels are treated as expired
# and GC'd on next encounter (``BackwardCompatStaleClaim``).
#
# Rollback: ``OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY=1`` reverts to the
# OP-838 bare-label path (acknowledged-racey-but-known-working baseline).
# The label formats are mutually forward-compatible: old code reading a
# new ``claim:default:0017..-ab12`` label sees instance ``"default"`` via
# ``_claim_label_instance`` (so it correctly treats a foreign-instance
# fenced claim as foreign and skips), and never mistakes the suffix for a
# live ``instance_id``. See ``docs/sop/runner-pickup-mutex.md``.

CLAIM_LABEL_PREFIX = "claim:"

# AUDIT-24 fencing-token width: ``token = f"{epoch_us:016d}-{uuid4().hex[:8]}"``.
# 16 zero-padded digits hold microsecond Unix epochs comfortably past the
# year 2286, so lexicographic order over tokens == chronological order.
_CLAIM_TOKEN_EPOCH_WIDTH = 16

# Stale fencing-token sweep: a ``claim:{inst}:{token}`` label whose epoch
# prefix is older than this is assumed orphaned by a crashed runner and
# removed on the next claim's pre-GET. Default ``2× CLI hard timeout``
# (3600s) per the OP-977 error catalog (``OrphanClaimLabel``). Tunable via
# ``OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S``.
try:
    _STALE_CLAIM_MAX_AGE_S = int(
        os.environ.get("OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S", str(2 * 3600))
    )
except ValueError:
    _STALE_CLAIM_MAX_AGE_S = 2 * 3600

# Atlassian's PUT-then-GET is occasionally eventually-consistent: the label
# we just added can be missing from the very next GET for ~100ms. After the
# claim PUT we re-GET up to ``_CLAIM_READBACK_RETRIES`` times (first attempt
# immediate, subsequent attempts ``_CLAIM_READBACK_DELAY_S`` apart) until we
# see our own label. Worst-case added latency ≈ (retries-1) × delay ≈ 0.4s.
# Per the OP-977 error catalog (``JIRAPutEventualConsistencyDelay``).
_CLAIM_READBACK_RETRIES = 3
_CLAIM_READBACK_DELAY_S = 0.2

# Rollback flag — see module header.
_LEGACY_CLAIM_ENV = "OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY"
# OP-1168 shadow-write flag. Default-on: labels remain authoritative while
# runner_coordination receives best-effort shadow writes for observation.
_CLAIM_SHADOW_ENV = "OMNISIGHT_RUNNER_CLAIM_SHADOW"


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of :func:`claim_ticket_atomic`.

    - ``ok=True, lost_to=None``: caller may proceed to
      ``transition_to_in_progress``. Either we won this race (our fencing
      token is the lowest among ``claim:{instance_id}:*``) or we're
      re-claiming a ticket we already held (idempotent re-entry after a
      runner restart with the same instance_id).
    - ``ok=False, lost_to=<who>``: another claim won. Caller MUST skip the
      ticket and MUST NOT call ``transition_to_in_progress``. ``lost_to``
      is the winning ``claim:{inst}:{token}`` label, an
      ``assignee:<accountId>`` string for a cross-bot race, or
      ``"claim-label-missing-from-readback"`` if our PUT did not
      materialise within the eventual-consistency bound — used verbatim in
      the ``[runner-mutex-lost]`` log line.

    ``claim_token`` records ``{instance_id}:{token}`` for the attempt,
    independent of label/assignee write outcome. Logged on both win and
    loss so a post-mortem can correlate the two sides of the race. Under
    the legacy (OP-838) path it is ``{instance_id}:{utc_iso}``.
    """

    ok: bool
    lost_to: str | None = None
    claim_token: str | None = None
    coordination_lease_id: str | None = None
    coordination_fencing_token: str | None = None


def _coordination_resource_key(key: str) -> str:
    """Resource key used by the OP-1107 shadow coordination table write."""
    return f"ticket:{key}"


def _shadow_acquire_claim(
    client: DispatchClient,
    key: str,
    instance_id: str,
    *,
    label_fencing_token: str | None = None,
) -> runner_coordination.ClaimLease | None:
    """Best-effort OP-1168 shadow table claim.

    The JIRA label path remains load-bearing during the observation
    period, so coordination-table write failures are logged but do not
    change pickup behaviour.
    """
    if not _claim_shadow_enabled():
        return None
    try:
        refs = {"source": "jira_dispatch.claim_ticket_atomic"}
        refs.update(_bridge_external_refs_for_claim(key))
        if label_fencing_token is not None:
            refs["label_fencing_token"] = label_fencing_token
        return runner_coordination.acquire_claim(
            ticket_key=key,
            resource_key=_coordination_resource_key(key),
            owner_agent_class=getattr(client, "agent_class", "unknown"),
            owner_instance_id=instance_id,
            phase="pickup",
            external_refs=refs,
        )
    except Exception as exc:  # noqa: BLE001 - shadow write must not alter label path
        log.warning("runner_coordination.acquire_claim shadow failed key=%s err=%s", key, exc)
        return None


def _shadow_record_phase(
    lease: runner_coordination.ClaimLease | None,
    phase: str,
) -> None:
    if lease is None or not _claim_shadow_enabled():
        return
    try:
        runner_coordination.record_phase(
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            phase=phase,
        )
    except Exception as exc:  # noqa: BLE001 - shadow write must not alter label path
        log.warning(
            "runner_coordination.record_phase shadow failed lease_id=%s phase=%s err=%s",
            lease.lease_id,
            phase,
            exc,
        )


def _shadow_release_claim(
    lease_id: str | None,
    fencing_token: str | None,
    reason: str,
) -> None:
    if not lease_id or not fencing_token or not _claim_shadow_enabled():
        return
    try:
        runner_coordination.release_claim(
            lease_id=lease_id,
            fencing_token=fencing_token,
            release_reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 - shadow release must not alter label path
        log.warning(
            "runner_coordination.release_claim shadow failed lease_id=%s err=%s",
            lease_id,
            exc,
        )


class RunnerMutexLost(RuntimeError):
    """Soft signal: readback indicates another claim won.

    Per the OP-838 / AUDIT-24 ``Error catalog`` (``MultiClaimDetected``).
    The runner-facing API surface returns this as :class:`ClaimResult`
    (loser path returns, not raises) to keep the happy path branch-free,
    but the typed class is exported for future programmatic callers that
    prefer exception-based control flow.
    """

    def __init__(self, key: str, claim_token: str, observed_token: str | None) -> None:
        super().__init__(
            f"{key}: claim {claim_token!r} lost to {observed_token!r}"
        )
        self.key = key
        self.claim_token = claim_token
        self.observed_token = observed_token


class RunnerMutexAPIError(RuntimeError):
    """Wraps a transport / HTTP failure during the claim sequence.

    Distinguishes "another instance won" (soft skip, retry next tick) from
    "JIRA itself is unreachable" (escalate / pause). The runner converts
    both to ``return 0`` so the cron tick exits cleanly, but the typed
    class lets callers branch in tests / future code.
    """

    def __init__(self, key: str, step: str, cause: BaseException) -> None:
        super().__init__(
            f"{key}: claim {step} failed: {type(cause).__name__}: {cause}"
        )
        self.key = key
        self.step = step
        self.__cause__ = cause


def _legacy_claim_mode() -> bool:
    """True iff ``OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY`` selects the OP-838 path."""
    return os.environ.get(_LEGACY_CLAIM_ENV, "").strip().lower() not in ("", "0", "false", "no")


def _claim_shadow_enabled() -> bool:
    """True unless ``OMNISIGHT_RUNNER_CLAIM_SHADOW`` explicitly disables it."""
    return os.environ.get(_CLAIM_SHADOW_ENV, "on").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _our_claim_label(instance_id: str) -> str:
    """Legacy (OP-838) bare mutex marker: ``claim:{instance_id}``.

    Retained for the rollback path and as the cleanup target for
    pre-AUDIT-24 labels. The AUDIT-24 fenced path uses
    :func:`_fenced_claim_label` instead.
    """
    return f"{CLAIM_LABEL_PREFIX}{instance_id}"


def _mint_claim_token(now_us: int | None = None) -> str:
    """A unique-per-tick fencing token ``{epoch_us:016d}-{uuid8}``.

    The microsecond-epoch prefix makes lexicographic order == chronological
    order; the 8-hex-char uuid suffix breaks ties between two runners that
    mint within the same microsecond.
    """
    if now_us is None:
        now_us = time.time_ns() // 1000
    return f"{now_us:0{_CLAIM_TOKEN_EPOCH_WIDTH}d}-{uuid.uuid4().hex[:8]}"


def _fenced_claim_label(instance_id: str, token: str) -> str:
    """AUDIT-24 fencing-token label: ``claim:{instance_id}:{token}``."""
    return f"{CLAIM_LABEL_PREFIX}{instance_id}:{token}"


def _parse_claim_label(label: str) -> tuple[str, str | None] | None:
    """Split a ``claim:*`` label into ``(instance_id, token | None)``.

    - ``claim:default:0017..-ab12`` → ``("default", "0017..-ab12")`` — a
      fenced AUDIT-24 claim.
    - ``claim:default``             → ``("default", None)`` — a pre-AUDIT-24
      bare claim (treated as expired by the fenced path).
    - anything that is not a ``claim:<non-empty>`` label → ``None``.
    """
    if not label.startswith(CLAIM_LABEL_PREFIX):
        return None
    rest = label[len(CLAIM_LABEL_PREFIX):]
    if not rest:
        return None
    inst, sep, token = rest.partition(":")
    if not inst:
        return None
    return (inst, token if sep else None)


def _claim_label_instance(label: str) -> str | None:
    """Instance-id portion of a ``claim:*`` label, or ``None`` if not one.

    Works for both the legacy bare form and the AUDIT-24 fenced form
    (returns the ``instance_id``, never the token suffix).
    """
    parsed = _parse_claim_label(label)
    return parsed[0] if parsed else None


def _claim_token_epoch_us(token: str) -> int | None:
    """Microsecond Unix-epoch prefix of a fencing token, or ``None``.

    A token minted by :func:`_mint_claim_token` is ``{digits}-{uuid8}``;
    the leading digit run is the epoch. Returns ``None`` for tokens that do
    not follow that shape (defensive — such a token simply never ages out).
    """
    head, _, _ = token.partition("-")
    return int(head) if head.isdigit() else None


def _claim_token_is_stale(token: str, now_us: int, max_age_s: int) -> bool:
    """True iff ``token``'s epoch prefix is older than ``max_age_s`` seconds."""
    epoch_us = _claim_token_epoch_us(token)
    if epoch_us is None:
        return False
    return (now_us - epoch_us) > max_age_s * 1_000_000


def _lowest_uuid_claim_winner(
    labels: Iterable[str],
    instance_id: str,
    *,
    now_us: int,
    max_age_s: int = _STALE_CLAIM_MAX_AGE_S,
) -> str | None:
    """Lowest *live* fencing token for ``instance_id`` among ``labels``.

    "Live" = the label is a fenced ``claim:{instance_id}:{token}`` (not a
    pre-AUDIT-24 bare label) whose epoch prefix is younger than
    ``max_age_s``. Returns the bare ``token`` (not the full label), or
    ``None`` if our instance has no live claim. Foreign-instance labels are
    intentionally ignored here — they are handled by the assignee guard and
    the pre/post foreign-claim checks in :func:`claim_ticket_atomic`.

    This is the deterministic core of the AUDIT-24 mutex (AC #1): every
    runner sharing ``instance_id`` runs it over the same readback set and
    therefore agrees on the same winning token.
    """
    best: str | None = None
    for label in labels:
        parsed = _parse_claim_label(label)
        if parsed is None:
            continue
        inst, token = parsed
        if inst != instance_id or token is None:
            continue
        if _claim_token_is_stale(token, now_us, max_age_s):
            continue
        if best is None or token < best:
            best = token
    return best


def is_winning_claim_owner(
    labels: Iterable[str],
    instance_id: str,
    token: str | None,
    *,
    now_us: int | None = None,
) -> bool:
    """True iff ``claim:{instance_id}:{token}`` is the live winning claim
    among ``labels`` — i.e. this runner still holds the fencing-token claim
    it acquired at pickup.

    OP-1541: the TOCTOU recheck and the destructive-recovery gate use this to
    tell a shared-account false-abort from a genuine ownership loss. All
    ``codex-*`` (resp. ``claude-*``) instances authenticate as ONE JIRA
    account, so the assignee cannot distinguish instances of a class. When a
    sibling instance clears the assignee (its own revert → ``clear_assignee``
    → ``None``) while OUR per-instance fenced claim is still the live winner,
    the bot→None drift is benign and the run continues. When our token is
    gone / stale / no longer the lowest live token, we have genuinely lost the
    claim and must NOT clear assignee or revert a ticket we no longer own.

    Ownership requires (a) a non-empty ``token``, (b) our exact fenced label
    ``claim:{instance_id}:{token}`` present in ``labels``, (c) that token not
    stale, and (d) that token being the lowest live token across **all**
    instances' fenced claims. Unlike :func:`_lowest_uuid_claim_winner` (which
    is per-instance because the claim path rejects foreign claims outright),
    this is a *global* total order: under one shared JIRA account two
    instances each hold a per-instance-lowest token, so only a cross-instance
    fencing-token comparison decides the single rightful owner. The earliest
    minted token (lowest ``{epoch_us}-{uuid}``) wins — so the non-owner's
    leftover/loser label can never make it (wrongly) believe it owns the
    ticket, which is what stops it from clobbering the owner's run.

    A ``None``/empty token (legacy or unfenced pickup) returns ``False``; the
    caller then falls back to the strict OP-1062 behaviour.
    """
    if not token:
        return False
    if now_us is None:
        now_us = time.time_ns() // 1000
    our_label = _fenced_claim_label(instance_id, token)
    have_ours = False
    lowest_live: str | None = None
    for label in labels:
        parsed = _parse_claim_label(label)
        if parsed is None:
            continue
        _inst, tok = parsed
        if tok is None:  # pre-AUDIT-24 bare claim — not a live fenced claim
            continue
        if _claim_token_is_stale(tok, now_us, _STALE_CLAIM_MAX_AGE_S):
            continue
        if label == our_label:
            have_ours = True
        if lowest_live is None or tok < lowest_live:
            lowest_live = tok
    return have_ours and lowest_live == token


def claim_ticket_atomic(
    client: DispatchClient,
    key: str,
    instance_id: str,
) -> ClaimResult:
    """Atomically claim ``key`` before ``transition_to_in_progress``.

    OP-1168 shadow phase: JIRA claim labels remain the load-bearing
    ownership path. When ``OMNISIGHT_RUNNER_CLAIM_SHADOW`` is unset or
    ``on``, the same acquire/release lifecycle is also written
    best-effort to :mod:`runner_coordination` for observation. Shadow
    failures are logged and swallowed.

    Raises :class:`RunnerMutexAPIError` for transport failures during the
    claim sequence. Returns :class:`ClaimResult` for the mutex-lost path.
    """
    if _legacy_claim_mode():
        return _claim_ticket_atomic_legacy(client, key, instance_id)
    return _claim_ticket_atomic_fenced(client, key, instance_id)


def _claim_ticket_atomic_table_only(
    client: DispatchClient,
    key: str,
    instance_id: str,
) -> ClaimResult:
    """OP-1110 cutover claim path: coordination table is sole authority.

    Sequence:

    1. ``runner_coordination.acquire_claim`` on the ticket resource_key.
       The table's partial unique index on ``(resource_key) WHERE
       state='active'`` atomically serializes contenders — on conflict
       the call raises :class:`runner_coordination.ClaimBlocked` carrying
       the existing lease.
    2. Best-effort JIRA assignee PUT for human observability. Failures
       here are logged but do not roll back the table claim — the table
       row is the load-bearing state.

    No labels written, no eventual-consistency retry loop, no label-based
    tie-break. The table's atomic INSERT replaces the entire fenced-label
    protocol. Cross-bot conflicts are caught by the same unique-index
    serialization (each bot+instance has a distinct
    ``owner_agent_class`` + ``owner_instance_id``).

    Raises :class:`RunnerMutexAPIError` if the coordination table is
    unavailable — the cutover requires the table to be reachable; falling
    back silently to label-only would re-introduce the race the cutover
    is closing. Operators with a coordination-DB outage can set
    ``OMNISIGHT_RUNNER_LABEL_CLAIM_LEGACY=1`` to revert to dual-write.
    """
    our_claim_token = f"{instance_id}:{_mint_claim_token(time.time_ns() // 1000)}"

    try:
        refs = {"source": "claim_ticket_atomic_table_only"}
        refs.update(_bridge_external_refs_for_claim(key))
        lease = runner_coordination.acquire_claim(
            ticket_key=key,
            resource_key=_coordination_resource_key(key),
            owner_agent_class=getattr(client, "agent_class", "unknown"),
            owner_instance_id=instance_id,
            phase="pickup",
            external_refs=refs,
        )
    except runner_coordination.ClaimBlocked as exc:
        existing = exc.existing_lease
        lost_to = (
            existing.fencing_token if existing is not None
            else f"resource:{_coordination_resource_key(key)}"
        )
        return ClaimResult(
            ok=False, lost_to=lost_to, claim_token=our_claim_token,
        )
    except Exception as exc:
        # OP-1110: cutover requires the table. Surface as transport-style
        # error so the runner skips this tick rather than silently
        # bypassing mutex enforcement on a DB outage.
        raise RunnerMutexAPIError(key, "table-acquire", exc) from exc

    # Observability: set JIRA assignee so humans see who's working. Best-
    # effort; the table claim is already held and is the authoritative
    # state, so a 5xx here does not invalidate ownership.
    try:
        _request(
            client, "PUT", f"/issue/{key}",
            {"fields": {"assignee": {"accountId": client.bot_account_id}}},
        )
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        log.warning(
            "claim_ticket_atomic_table_only: assignee PUT for %s failed "
            "(non-fatal — table claim still held): %s", key, e,
        )

    return ClaimResult(
        ok=True,
        lost_to=None,
        claim_token=our_claim_token,
        coordination_lease_id=lease.lease_id,
        coordination_fencing_token=lease.fencing_token,
    )


def _claim_ticket_atomic_fenced(
    client: DispatchClient,
    key: str,
    instance_id: str,
) -> ClaimResult:
    """AUDIT-24 fencing-token claim (AC #1).

    Sequence:

    1. pre-GET ``/issue/<key>?fields=assignee,labels`` — fast-fail on a
       *live* foreign claim (another instance's fenced label, or a foreign
       assignee); collect *stale* fenced tokens and *bare* pre-AUDIT-24
       labels to GC in the same PUT.
    2. PUT ``/issue/<key>``: add our ``claim:{instance_id}:{token}`` label
       (+ assignee), and ``remove`` every stale/bare claim label spotted in
       step 1 — one request.
    3. post-GET readback, retried up to ``_CLAIM_READBACK_RETRIES`` times
       until our label appears (Atlassian eventual-consistency window).
    4. assignee readback must equal our bot account (cross-bot guard, kept
       from OP-838), then the LOWEST live token among ``claim:{instance_id}:*``
       wins (AC #1). If ours is not lowest we return ``ok=False`` and leave
       our label for the next pre-GET's stale sweep — we never delete it
       eagerly, so the winner observing it does not flip.
    """
    now_us = time.time_ns() // 1000
    token = _mint_claim_token(now_us)
    our_label = _fenced_claim_label(instance_id, token)
    our_claim_token = f"{instance_id}:{token}"

    # Step 1: pre-GET.
    try:
        pre = _request(client, "GET", f"/issue/{key}?fields=assignee,labels")
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        raise RunnerMutexAPIError(key, "pre-GET", e) from e
    pre_fields = pre.get("fields") or {}
    pre_assignee_id = (pre_fields.get("assignee") or {}).get("accountId")
    pre_labels = list(pre_fields.get("labels") or [])

    stale_to_remove: list[str] = []
    live_foreign: str | None = None
    for label in pre_labels:
        parsed = _parse_claim_label(label)
        if parsed is None:
            continue
        inst, tok = parsed
        if tok is None:
            # Pre-AUDIT-24 bare ``claim:<inst>`` — BackwardCompatStaleClaim:
            # treat as expired, GC it, never count it as a live claim.
            stale_to_remove.append(label)
            continue
        if _claim_token_is_stale(tok, now_us, _STALE_CLAIM_MAX_AGE_S):
            # OrphanClaimLabel: crashed-runner leftover — GC and ignore.
            stale_to_remove.append(label)
            continue
        if inst != instance_id and (live_foreign is None or label < live_foreign):
            live_foreign = label
    if live_foreign is not None:
        return ClaimResult(ok=False, lost_to=live_foreign, claim_token=our_claim_token)
    if pre_assignee_id and pre_assignee_id != client.bot_account_id:
        return ClaimResult(
            ok=False, lost_to=f"assignee:{pre_assignee_id}", claim_token=our_claim_token
        )

    coordination_lease = _shadow_acquire_claim(
        client,
        key,
        instance_id,
        label_fencing_token=token,
    )

    # Step 2: atomic PUT — add our fenced label (+ assignee), GC the rest.
    update_ops: list[dict] = [{"add": our_label}]
    for label in dict.fromkeys(stale_to_remove):  # de-dup, preserve order
        update_ops.append({"remove": label})
    try:
        _request(
            client, "PUT", f"/issue/{key}",
            {
                "fields": {"assignee": {"accountId": client.bot_account_id}},
                "update": {"labels": update_ops},
            },
        )
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-error",
            )
        raise RunnerMutexAPIError(key, "PUT", e) from e

    # Step 3: post-GET readback, with the eventual-consistency retry loop.
    post_labels: list[str] = []
    post_assignee_id: str | None = None
    for attempt in range(_CLAIM_READBACK_RETRIES):
        if attempt:
            time.sleep(_CLAIM_READBACK_DELAY_S)
        try:
            post = _request(client, "GET", f"/issue/{key}?fields=assignee,labels")
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            raise RunnerMutexAPIError(key, "post-GET", e) from e
        post_fields = post.get("fields") or {}
        post_assignee_id = (post_fields.get("assignee") or {}).get("accountId")
        post_labels = list(post_fields.get("labels") or [])
        if our_label in post_labels:
            break
    else:
        # JIRAPutEventualConsistencyDelay exceeded — do not proceed on
        # inconsistent state; the caller retries on the next tick.
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-lost",
            )
        return ClaimResult(
            ok=False, lost_to="claim-label-missing-from-readback", claim_token=our_claim_token
        )

    # Step 4a: cross-bot guard — assignee is single-valued, last-writer-wins.
    if post_assignee_id != client.bot_account_id:
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-lost",
            )
        return ClaimResult(
            ok=False, lost_to=f"assignee:{post_assignee_id}", claim_token=our_claim_token
        )

    # Step 4b: a live *foreign-instance* fenced claim that landed between
    # our pre-GET and post-GET (degenerate same-bot-different-instance
    # config — the assignee guard already covers cross-bot).
    foreign_live = [
        label
        for label in post_labels
        if (p := _parse_claim_label(label)) is not None
        and p[1] is not None
        and p[0] != instance_id
        and not _claim_token_is_stale(p[1], now_us, _STALE_CLAIM_MAX_AGE_S)
    ]
    if foreign_live:
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-lost",
            )
        return ClaimResult(ok=False, lost_to=min(foreign_live), claim_token=our_claim_token)

    # Step 4c: lowest live token among our instance's claims wins (AC #1).
    winning_token = _lowest_uuid_claim_winner(post_labels, instance_id, now_us=now_us)
    if winning_token is not None and winning_token != token:
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-lost",
            )
        return ClaimResult(
            ok=False,
            lost_to=_fenced_claim_label(instance_id, winning_token),
            claim_token=our_claim_token,
        )
    _shadow_record_phase(coordination_lease, "label-claimed")
    return ClaimResult(
        ok=True,
        lost_to=None,
        claim_token=our_claim_token,
        coordination_lease_id=(
            coordination_lease.lease_id if coordination_lease is not None else None
        ),
        coordination_fencing_token=(
            coordination_lease.fencing_token if coordination_lease is not None else None
        ),
    )


def _claim_ticket_atomic_legacy(
    client: DispatchClient,
    key: str,
    instance_id: str,
) -> ClaimResult:
    """OP-838 bare-label claim path — SUPERSEDED, rollback baseline only.

    Reachable only via ``OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY=1``. Known to
    NOT serialise two runners that share an ``instance_id`` (Atlassian's
    label-add is set-union, not compare-and-swap) — that is the OP-974
    failure AUDIT-24 fixed. Kept verbatim so an emergency rollback returns
    to a known-working-for-the-cross-bot-case baseline.

    Sequence: pre-GET (fast-fail on foreign claim label / foreign
    assignee), PUT ``assignee + labels.add = claim:<instance_id>``,
    post-GET (assignee readback discriminates cross-bot races, label
    readback confirms our write landed). Idempotent for the same
    ``(bot, instance_id)``.
    """
    our_label = _our_claim_label(instance_id)
    utc_iso = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    our_token = f"{instance_id}:{utc_iso}"

    # Step a: pre-GET.
    try:
        pre = _request(client, "GET", f"/issue/{key}?fields=assignee,labels")
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        raise RunnerMutexAPIError(key, "pre-GET", e) from e

    pre_fields = pre.get("fields") or {}
    pre_assignee_id = (pre_fields.get("assignee") or {}).get("accountId")
    pre_labels = list(pre_fields.get("labels") or [])

    foreign_claim = next(
        (
            l for l in pre_labels
            if (inst := _claim_label_instance(l)) is not None and inst != instance_id
        ),
        None,
    )
    if foreign_claim:
        return ClaimResult(ok=False, lost_to=foreign_claim, claim_token=our_token)

    if pre_assignee_id and pre_assignee_id != client.bot_account_id:
        return ClaimResult(
            ok=False,
            lost_to=f"assignee:{pre_assignee_id}",
            claim_token=our_token,
        )

    coordination_lease = _shadow_acquire_claim(
        client,
        key,
        instance_id,
        label_fencing_token=utc_iso,
    )

    # Step b: atomic PUT — assignee + label add in one request.
    try:
        _request(
            client, "PUT", f"/issue/{key}",
            {
                "fields": {"assignee": {"accountId": client.bot_account_id}},
                "update": {"labels": [{"add": our_label}]},
            },
        )
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-error",
            )
        raise RunnerMutexAPIError(key, "PUT", e) from e

    # Step c: post-GET readback.
    try:
        post = _request(client, "GET", f"/issue/{key}?fields=assignee,labels")
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        raise RunnerMutexAPIError(key, "post-GET", e) from e

    post_fields = post.get("fields") or {}
    post_assignee_id = (post_fields.get("assignee") or {}).get("accountId")
    post_labels = list(post_fields.get("labels") or [])

    # Step d.i: primary discriminator — assignee field is single-valued
    # and last-writer-wins. Two concurrent PUTs from different bots end
    # with exactly one bot account in the readback; everybody else loses.
    if post_assignee_id != client.bot_account_id:
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-lost",
            )
        return ClaimResult(
            ok=False,
            lost_to=f"assignee:{post_assignee_id}",
            claim_token=our_token,
        )

    # Step d.ii: our label must have landed. A missing label in the
    # readback means the PUT was rejected or partial — treat as a loss so
    # the caller doesn't proceed on inconsistent state.
    if our_label not in post_labels:
        if coordination_lease is not None:
            _shadow_release_claim(
                coordination_lease.lease_id,
                coordination_lease.fencing_token,
                "label-claim-lost",
            )
        return ClaimResult(
            ok=False,
            lost_to="claim-label-missing-from-readback",
            claim_token=our_token,
        )

    _shadow_record_phase(coordination_lease, "label-claimed")
    return ClaimResult(
        ok=True,
        lost_to=None,
        claim_token=our_token,
        coordination_lease_id=(
            coordination_lease.lease_id if coordination_lease is not None else None
        ),
        coordination_fencing_token=(
            coordination_lease.fencing_token if coordination_lease is not None else None
        ),
    )


def release_ticket_claim(
    client: DispatchClient,
    key: str,
    instance_id: str,
    token: str | None = None,
    *,
    coordination_lease_id: str | None = None,
    coordination_fencing_token: str | None = None,
) -> None:
    """Release this instance's claim on ``key``.

    OP-1168 shadow phase: sweep all ``claim:{instance_id}:*`` fenced
    labels plus the legacy bare ``claim:{instance_id}`` label via the
    existing JIRA label path, then best-effort release the coordination
    table lease when one was captured during acquire.
    """
    targets: list[str] = []
    try:
        cur = _request(client, "GET", f"/issue/{key}?fields=labels")
        labels = list((cur.get("fields") or {}).get("labels") or [])
        for label in labels:
            parsed = _parse_claim_label(label)
            if parsed is not None and parsed[0] == instance_id:
                targets.append(label)
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        log.warning("release_ticket_claim: GET labels for %s failed: %s", key, e)
        targets = [
            _fenced_claim_label(instance_id, token) if token else _our_claim_label(instance_id)
        ]

    targets = list(dict.fromkeys(targets))
    if not targets:
        _shadow_release_claim(
            coordination_lease_id,
            coordination_fencing_token,
            "label-claim-released",
        )
        return
    try:
        _request(
            client, "PUT", f"/issue/{key}",
            {"update": {"labels": [{"remove": label} for label in targets]}},
        )
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        log.warning("release_ticket_claim: removing %r from %s failed: %s", targets, key, e)
    _shadow_release_claim(
        coordination_lease_id,
        coordination_fencing_token,
        "label-claim-released",
    )


PREREQS_RE = re.compile(
    r"##\s+Prerequisites.*?```yaml\s*(.+?)\s*```",
    re.DOTALL | re.IGNORECASE,
)


def parse_prerequisites(description: str) -> dict[str, list]:
    """Extract Prerequisites YAML block; return parsed dict.

    Returns empty dict if no block found (treats as no prerequisites).
    """
    m = PREREQS_RE.search(description)
    if not m:
        return {
            "blocks_on": [], "soft_prereqs": [], "mutex_with": [],
            "schema_locks": [], "live_state_requires": [], "external_blockers": [],
        }
    import yaml
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}  # malformed YAML → caller treats as fail-safe
    # Normalize all keys present
    for k in ("blocks_on", "soft_prereqs", "mutex_with", "schema_locks",
              "live_state_requires", "external_blockers"):
        data.setdefault(k, [])
    return data


# ── Pre-pickup check (combined live-state + mutex + blocker) ──────

# Statuses that mean a ticket currently *holds* its mutex_with resources.
# Anything else (TODO, Approved, Published, Archived, ...) is "released" —
# pickup of a sibling sharing the same mutex:<path> may proceed.
MUTEX_HOLDING_STATUSES = ("In Progress", "Under Review")
BRIDGE_DEGRADED_AFTER_SECONDS = 300
BRIDGE_STALE_AFTER_SECONDS = 900
BRIDGE_GATE_GLOBAL_ENV = "OMNISIGHT_RUNNER_BRIDGE_GATE_GLOBAL"
BRIDGE_STALE_AT_ACQUIRE = "stale-at-acquire"
BRIDGE_REVIEW_YIELDING_LABELS = frozenset({
    "runner-batch-merge-candidate",
    "runner-glance-required",
    "class:subscription-codex-batch-merge",
})
_BRIDGE_STATE_AT_ACQUIRE_BY_TICKET: dict[str, str] = {}


def find_mutex_holders(
    client: "DispatchClient",
    mutex_labels: list[str],
    exclude_key: str,
) -> list[dict]:
    """Return JIRA issues currently holding any of ``mutex_labels``.

    "Holding" = status in :data:`MUTEX_HOLDING_STATUSES`. Used by
    :func:`pre_pickup_ok` (OP-687) to enforce that two agents never
    concurrently work tickets sharing a ``mutex:<resource-id>``.

    OP-1108 (v2-Ⅹ-2bc): authoritative source moved from JIRA-label JQL
    to the ``runner_claims`` coordination table populated by the OP-1107
    shadow-write integration. JQL is retained as a degraded-mode
    fallback when the table is unavailable (DB down / schema missing) —
    this keeps pickup pre-checks functional during operational incidents
    where the coordination DB is offline but JIRA is still reachable.

    Return shape stays as the legacy list-of-issue-dicts so callers
    (notably :func:`pre_pickup_ok`'s error-message formatter) do not
    have to be refactored alongside this read-path swap. The dicts
    synthesised from coordination rows carry status ``"In Progress"``
    and a single-element ``labels`` list with the matching mutex
    resource_key — enough for the caller's
    ``set(labels) & set(mutex_labels)`` intersection logic to surface
    the right mutex name in the conflict report.
    """
    if not mutex_labels:
        return []

    try:
        from backend.agents import runner_coordination as rc

        # If the coordination DB hasn't been bootstrapped yet, fall back
        # immediately — saves a noisy stacktrace on fresh-install hosts
        # where the table migration hasn't run.
        if not rc._db_path().exists():
            return _find_mutex_holders_jql(client, mutex_labels, exclude_key)

        leases = rc.find_active_holders(
            resource_keys=mutex_labels,
            exclude_ticket=exclude_key,
        )
        return [_lease_to_mutex_holder_dict(lease) for lease in leases]
    except Exception as exc:  # noqa: BLE001 - degraded mode must never raise
        log.warning(
            "find_mutex_holders: coordination-table read failed, "
            "falling back to JQL; err=%s",
            exc,
        )
        return _find_mutex_holders_jql(client, mutex_labels, exclude_key)


_JQL_FALLBACK_DEPRECATION_WARNED = False


def _find_mutex_holders_jql(
    client: "DispatchClient",
    mutex_labels: list[str],
    exclude_key: str,
) -> list[dict]:
    """Pre-OP-1108 JIRA-label-JQL implementation of :func:`find_mutex_holders`.

    Retained as the degraded-mode fallback path. Operates on the same
    contract — list of ``{key, fields: {status, labels}}`` dicts.

    OP-1110: this path is deprecated and will be removed one sprint after
    Atlas closure. Each process logs a single deprecation warning the
    first time it falls through here so operators can spot lingering
    coordination-table outages from the runner logs.
    """
    global _JQL_FALLBACK_DEPRECATION_WARNED
    if not _JQL_FALLBACK_DEPRECATION_WARNED:
        log.warning(
            "find_mutex_holders: JQL fallback path active — coordination "
            "table read failed, using deprecated JIRA-label search. This "
            "fallback will be removed one sprint after Sprint Atlas "
            "closure. Investigate runner_claims table availability "
            "(OMNISIGHT_DATABASE_PATH, alembic head, DB process)."
        )
        _JQL_FALLBACK_DEPRECATION_WARNED = True
    if not mutex_labels:
        return []
    label_clause = " OR ".join(f'labels = "{m}"' for m in mutex_labels)
    status_clause = ", ".join(f'"{s}"' for s in MUTEX_HOLDING_STATUSES)
    jql = (
        f'project = "{client.project_key}" '
        f'AND status in ({status_clause}) '
        f'AND ({label_clause}) '
        f'AND key != "{exclude_key}"'
    )
    resp = _request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["status", "labels"],
        "maxResults": 50,
    })
    return resp.get("issues", [])


def _lease_to_mutex_holder_dict(lease) -> dict:
    """Adapt a :class:`runner_coordination.ClaimLease` to the legacy
    JIRA-issue dict shape consumed by :func:`pre_pickup_ok`'s error
    formatter.

    An active claim row always represents a ticket that has been
    transitioned to In Progress (or is mid-pickup en route there), so
    we synthesise ``status.name = "In Progress"`` rather than reading
    JIRA. The synthesised ``labels`` list carries the matching
    resource_key so the caller's mutex-label intersection logic
    surfaces a stable label in the conflict report.
    """
    return {
        "key": lease.ticket_key,
        "fields": {
            "status": {"name": "In Progress"},
            "labels": [lease.resource_key],
        },
    }


PartyMembershipCheck = Callable[[str], Optional[str]]
"""RPG.W17 pre-pickup membership probe.

Maps an ``agent_id`` to the ``party_id`` currently gating that agent
from individual task pickups (``None`` if the agent is free). The
helper exists so :func:`pre_pickup_ok` stays storage-agnostic —
production wires :func:`backend.agents.party.member_is_gated` behind
an asyncpg connection factory; tests inject a dict lookup.
"""


def _party_membership_reason(
    snapshot: TicketSnapshot,
    membership_check: "PartyMembershipCheck | None",
) -> str | None:
    """Run the W17 :class:`MemberInActiveParty` pre-pickup gate.

    Returns ``None`` if the gate passes (or is not configured), and a
    structured reason string otherwise. The reason uses the
    ``MemberInActiveParty:`` prefix so the runner can parse it and
    fall through to the next pickup candidate, mirroring the existing
    ``mutex conflict:`` reason format.

    The agent identity is resolved from ``client.agent_class`` —
    `claude-bot`/`codex-bot` snapshots are tied to the dispatcher
    that fetched them, so the active-party probe checks the *picker*
    (not the ticket assignee field, which is stale by definition
    during pre-pickup).
    """
    if membership_check is None:
        return None
    agent_id = (snapshot.labels and _pickup_agent_from_labels(snapshot.labels)) or ""
    if not agent_id:
        return None
    party_id = membership_check(agent_id)
    if not party_id:
        return None
    return (
        f"MemberInActiveParty:{agent_id} gated by party {party_id} "
        f"holding an active Tier L+ task; ticket {snapshot.key} skipped"
    )


def _pickup_agent_from_labels(labels: Iterable[str]) -> str | None:
    """Extract ``agent:<id>`` from a snapshot's labels tuple, if present."""
    for label in labels:
        if isinstance(label, str) and label.startswith("agent:"):
            return label.split(":", 1)[1].strip() or None
    return None


def _bridge_health_pickup_reason(
    snapshot: TicketSnapshot,
    enabled_capabilities: Iterable[str] | None,
    bridge_health_check: Callable[[], tuple[bool, float, Path]] | None,
) -> str | None:
    """Return a bridge-health refusal reason, or ``None`` when pickup may proceed.

    OP-1758 / family10 §8 grades the old fleet-wide bridge gate by the
    resolved capability set. Stale bridge state hard-blocks only
    ``gerrit_push`` pickups; non-push pickups proceed and carry
    ``external_refs.bridge_state = "stale-at-acquire"`` into the
    coordination claim. Operators can restore the old global gate with
    ``OMNISIGHT_RUNNER_BRIDGE_GATE_GLOBAL=1``.
    """
    _BRIDGE_STATE_AT_ACQUIRE_BY_TICKET.pop(snapshot.key, None)
    if enabled_capabilities is None:
        return None
    caps = frozenset(enabled_capabilities)

    if bridge_health_check is None:
        from backend.agents.gerrit_jira_bridge import check_bridge_heartbeat

        bridge_health_check = check_bridge_heartbeat

    _is_fresh, age_sec, path = bridge_health_check()
    if age_sec <= BRIDGE_STALE_AFTER_SECONDS:
        return None

    _BRIDGE_STATE_AT_ACQUIRE_BY_TICKET[snapshot.key] = BRIDGE_STALE_AT_ACQUIRE
    global_gate = os.environ.get(BRIDGE_GATE_GLOBAL_ENV, "").strip().lower() not in (
        "", "0", "false", "no", "off",
    )
    if "gerrit_push" not in caps and not global_gate:
        return None

    age_repr = "missing" if age_sec == float("inf") else f"{age_sec:.0f}s"
    return (
        "bridge_health_stale: Gerrit-finalizing pickup requires a fresh "
        f"bridge heartbeat; heartbeat at {path} age={age_repr}"
    )


def _bridge_external_refs_for_claim(key: str) -> dict[str, str]:
    """Return pickup-time bridge metadata for coordination claim refs."""
    bridge_state = _BRIDGE_STATE_AT_ACQUIRE_BY_TICKET.get(key)
    if bridge_state is None:
        return {}
    return {"bridge_state": bridge_state}


def _provider_task_for_pickup(
    snapshot: TicketSnapshot,
    agent_class: str,
) -> provider_orchestrator.TaskSpec:
    tier = "M"
    areas: list[str] = []
    for label in snapshot.labels:
        if label.startswith("tier:"):
            tier = label.split(":", 1)[1].strip().upper() or tier
        elif label.startswith("area:"):
            area = label.split(":", 1)[1].strip()
            if area:
                areas.append(area)
    return provider_orchestrator.TaskSpec(
        prompt=snapshot.key,
        agent_class=agent_class,
        tier=tier,
        area=areas,
        correlation_id=snapshot.key,
    )


def pre_pickup_ok(
    client: DispatchClient,
    snapshot: TicketSnapshot,
    worktree_path: Path | None = None,
    party_membership_check: "PartyMembershipCheck | None" = None,
    enabled_capabilities: Iterable[str] | None = None,
    bridge_health_check: Callable[[], tuple[bool, float, Path]] | None = None,
) -> tuple[bool, str]:
    """Combined pre-pickup gate. Returns (ok, reason).

    Per L17 (2026-05-06): when ``worktree_path`` is provided, live-state
    checks resolve relative to that path — the agent's actual workspace —
    instead of the runner host's main repo. This is the correct cwd
    because the runner has already fresh-synced the worktree to Gerrit
    develop tip via ``sync_to_gerrit_develop`` before this gate runs.

    Backward-compatible: ``worktree_path=None`` falls back to
    ``live_state_check.REPO_ROOT`` (the legacy main-repo behaviour).

    Mutex enforcement (OP-687): if Prerequisites YAML declares
    ``mutex_with`` and any sibling ticket is currently holding one of
    those labels, return False with a "mutex conflict:" reason. The
    runner skips and tries the next pickup candidate; the JIRA workflow
    validator handles ``blocks_on`` (§10) separately.

    Party gate (OP-220 / W17): when ``party_membership_check`` is
    provided, an agent whose party holds an active Tier L+ task is
    refused individual task pickup. Reason string is prefixed
    ``MemberInActiveParty:`` so the runner can recognise it; backward-
    compatible — ``party_membership_check=None`` skips the gate, so
    existing callers (auto-runner-*.py) keep working until they opt
    in.

    Bridge-health gate (OP-1113 / v2-X-4bc): when the caller provides
    the resolved OP-855 capability set, stale bridge heartbeat state
    blocks only Gerrit-finalizing pickups. Code-only tickets, and
    review-yielding tickets carrying the explicit batch/glance envelope,
    are allowed to proceed.
    """
    from backend.agents.live_state_check import evaluate, all_passed, format_failures
    from backend.agents.file_coordinator import has_unresolved_blockedby

    # ADR-0033 §6 / S12.G v2 §3.6 — FIRST gate. Refuse operator-window-* /
    # operator-rehearsal pickup before any other check (capability matrix,
    # fencing-token claim, live-state). Primary filtering happens in
    # :func:`fetch_pickable_tickets`; this branch is the defensive belt-and-
    # suspenders for direct callers and label-added-after-fetch TOCTOU races.
    refused, refusal_label = _runner_refuses_pickup(list(snapshot.labels))
    if refused:
        _emit_runner_refusal_audit(snapshot.key, refusal_label)
        return False, f"runner_refusal_by_class:{refusal_label}"

    # OP-1140: stoploss circuit-breaker — refuse any ticket whose §11-revert
    # count has tripped the threshold. The label is added by
    # :func:`backend.agents.runner_stoploss.register_revert` during the prior
    # revert; an operator clears it by stripping the
    # ``runner-stoploss:circuit-tripped-*`` label.
    from backend.agents import runner_stoploss

    stoploss_ok, stoploss_reason = runner_stoploss.pre_pickup_stoploss_ok(
        list(snapshot.labels)
    )
    if not stoploss_ok:
        return False, stoploss_reason

    bridge_reason = _bridge_health_pickup_reason(
        snapshot, enabled_capabilities, bridge_health_check
    )
    if bridge_reason is not None:
        return False, bridge_reason

    quota_denial = capability_registry.quota_health_denial_from_labels(snapshot.labels)
    if quota_denial is not None:
        return False, quota_denial

    provider_decision = provider_orchestrator.pre_pickup_provider_decision(
        _provider_task_for_pickup(snapshot, client.agent_class)
    )
    if not provider_decision.ok:
        return False, provider_decision.reason

    caps = set(enabled_capabilities or ())
    if "gerrit_push" in caps:
        staleness_ok, staleness_reason = pickup_staleness_check(
            client, snapshot.key, worktree_path=worktree_path,
            gerrit_project=gerrit_project_for_labels(getattr(snapshot, "labels", ())),
        )
        if not staleness_ok:
            return False, staleness_reason

    desc = fetch_description(client, snapshot.key)
    prereqs = parse_prerequisites(desc)

    ok, reason = migration_freeze_check(client, snapshot, description=desc)
    if not ok:
        return False, reason

    blocked, blocked_reason = has_unresolved_blockedby(client, snapshot)
    if blocked:
        blocker_key = blocked_reason.split(" ", 3)[2]
        return False, f"blocked-by:{blocker_key} {blocked_reason}"

    # Live-state checks (§13)
    if prereqs.get("live_state_requires"):
        results = evaluate(prereqs["live_state_requires"], cwd=worktree_path)
        if not all_passed(results):
            return False, "live_state_requires failed:\n" + format_failures(results)

    # RPG.W17 party gate (OP-220 AC #4). Refuse individual task pickup
    # while the agent's party holds an active Tier L+ task. Opt-in via
    # ``party_membership_check`` so runners that don't yet know about
    # parties keep working.
    party_reason = _party_membership_reason(snapshot, party_membership_check)
    if party_reason is not None:
        return False, party_reason

    # Mutex sibling check (OP-687). Concrete failure mode without this:
    # codex picks OP-A (mutex:foo) at 02:00, claude picks OP-B (mutex:foo)
    # at 03:00, both push to Gerrit, second submit silently overwrites
    # first. Hard blocker (`blocks_on`) check is JIRA workflow's job.
    mutex_labels = list(prereqs.get("mutex_with") or [])
    if mutex_labels:
        holders = find_mutex_holders(client, mutex_labels, exclude_key=snapshot.key)
        if holders:
            lines = []
            for h in holders:
                hk = h.get("key", "?")
                fields = h.get("fields") or {}
                status = ((fields.get("status") or {}).get("name")) or "?"
                hlabels = fields.get("labels") or []
                shared = sorted(set(hlabels) & set(mutex_labels))
                lbl = shared[0] if shared else mutex_labels[0]
                lines.append(f"{lbl} held by {hk} (status: {status})")
            return False, "mutex conflict:\n  " + "\n  ".join(lines)

    try:
        post_feature_dup_warnings(client, snapshot, description=desc)
    except Exception as exc:  # noqa: BLE001 — warning-only detector must fail open
        log.warning(
            "feature_dup_detector.pickup_probe_failed key=%s err=%s",
            snapshot.key,
            exc,
        )

    return True, "pre-pickup checks passed"
