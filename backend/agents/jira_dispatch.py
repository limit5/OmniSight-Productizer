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

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.config import settings
from backend.agents.circuit_breaker import BREAKERS
from backend.agents.idempotency import DEFAULT_STORE
from backend.agents.scheduler import TicketSnapshot
from backend.agents.scope_to_paths import (
    ALWAYS_TOUCHED,
    ALWAYS_TOUCHED_TEMPLATE,
    FILES_SECTION_RE,
    PATH_TOKEN_RE,
    SCOPE_TO_PATHS,
    parse_files_section,
)

log = logging.getLogger(__name__)

MIGRATION_IN_FLIGHT_LABEL = "migration:in-flight"
MIGRATION_OVERRIDE_LABEL = "migration:override"
MIGRATION_SCOPE_PREFIX = "migration:scope="

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
    """Run pickup JQL per §16. Returns raw issue dicts (not snapshots)."""
    jql = PICKUP_JQL_TEMPLATE.format(project=client.project_key, cls=client.agent_class)
    resp = _request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["summary", "labels", "status", "issuetype", "fixVersions",
                   "created", "components", "issuelinks", "parent"],
        "maxResults": max_results,
    })
    return resp.get("issues", [])


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
}


# Gerrit endpoints (Track C verified 2026-05-05; reference_gerrit_self_hosted memory)
GERRIT_SSH_HOST = "sora.services"
GERRIT_SSH_PORT = 29418
GERRIT_PROJECT_PATH = "omnisight/OmniSight-Productizer"
GERRIT_HOOK_URL = "https://sora.services:29420/tools/hooks/commit-msg"

# agent_class → (gerrit username, ssh private key path).
# Memory: claude-bot for subscription-claude / api-anthropic; codex-bot for subscription-codex / api-openai.
_GERRIT_AUTH_BY_CLASS: dict[str, tuple[str, Path]] = {
    "subscription-codex":  ("codex-bot",  Path("~/.config/omnisight/gerrit-codex-bot-ed25519").expanduser()),
    "api-openai":          ("codex-bot",  Path("~/.config/omnisight/gerrit-codex-bot-ed25519").expanduser()),
    "subscription-claude": ("claude-bot", Path("~/.config/omnisight/gerrit-claude-bot-ed25519").expanduser()),
    "api-anthropic":       ("claude-bot", Path("~/.config/omnisight/gerrit-claude-bot-ed25519").expanduser()),
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
    if bot_username.startswith(("codex-bot-", "claude-bot-")):
        return bot_username, _gerrit_ssh_key_for_bot(bot_username)
    raise ValueError(f"unknown Gerrit bot username: {bot_username}")


def open_ps_count_for(bot_username: str) -> int:
    """Return open Gerrit patchsets owned by ``bot_username``."""
    user, ssh_key = _gerrit_auth_for_bot(bot_username)
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(GERRIT_SSH_PORT),
        f"{user}@{GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON",
        f"is:open owner:{bot_username}",
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


@dataclass(frozen=True)
class GerritMergedInfo:
    """Merged Gerrit sibling found for a JIRA key."""

    change_number: str
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


def sync_to_gerrit_develop(
    worktree_path: Path,
    agent_class: str,
    ticket_key: str,
    instance_id: str | None = None,
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

    Raises CalledProcessError if any git op fails.
    """
    assert_worktree_clean(worktree_path)

    import os
    import subprocess
    _, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key}"

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


def ensure_change_ids(worktree_path: Path, base_ref: str) -> None:
    """Rebase commits between base_ref..HEAD with --exec amend, triggering
    the commit-msg hook on each commit so they all get a Change-Id footer.

    Idempotent: commits already containing a Change-Id are unchanged
    (the standard Gerrit hook detects and skips).

    L16 fix: caller MUST pass an explicit base_ref (no default). Earlier
    default of "main" rebased onto local main which could contain commits
    with non-bot committer emails — Gerrit then rejects on push.

    Recommended usage: pass `develop_sha` from `sync_to_gerrit_develop()`.
    """
    try:
        subprocess.run(
            [
                "git", "rebase", base_ref,
                "--keep-empty",
                "--exec", "git commit --amend --no-edit",
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


def push_to_gerrit_for_review(
    worktree_path: Path,
    agent_class: str,
    target: str = "develop",
    instance_id: str | None = None,
) -> GerritPushResult:
    """Push worktree HEAD to ``gerrit:refs/for/<target>``.

    Returns parsed Change number + URL on success, or detail blob on
    failure. Caller is responsible for having installed the commit-msg
    hook + ensured all commits have Change-Id footers (use
    :func:`install_commit_msg_hook` and :func:`ensure_change_ids` first).
    """
    import os
    import subprocess
    try:
        _, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    except ValueError as exc:
        return GerritPushResult(False, None, None, str(exc))
    if not ssh_key.exists():
        return GerritPushResult(False, None, None, f"SSH key not found at {ssh_key}")

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key}"

    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        ["git", "push", _gerrit_ssh_url(agent_class, instance_id), f"HEAD:refs/for/{target}"],
        cwd=worktree_path, capture_output=True, text=True, env=env, timeout=120,
    )
    blob = (result.stderr + "\n" + result.stdout).strip()
    if result.returncode != 0:
        return GerritPushResult(False, None, None, blob[-1500:])

    m = _GERRIT_CHANGE_URL_RE.search(blob)
    if not m:
        return GerritPushResult(False, None, None, f"push succeeded but Change URL not parsed:\n{blob[-1500:]}")

    return GerritPushResult(
        success=True,
        change_number=int(m.group(2)),
        change_url=m.group(1),
        detail=blob[-1500:],
    )


def already_merged_in_gerrit(
    jira_key: str,
    agent_class: str = "subscription-codex",
    instance_id: str | None = None,
) -> GerritMergedInfo | None:
    """Return merged sibling change info for ``jira_key``, if Gerrit has one."""

    bot_username, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    query = f"status:merged project:{GERRIT_PROJECT_PATH} {jira_key}"
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
        if jira_key not in subject and jira_key not in json.dumps(change):
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


def transition_to_under_review_if_needed(
    client: "DispatchClient",
    key: str,
    idem_key: str | None = None,
) -> bool:
    """In Progress → Under Review, but only if not already there.

    Returns True if a transition POST was issued, False if the ticket was
    already in Under Review and the call was a no-op. Raises RuntimeError
    on any non-4xx HTTP failure of the transition POST itself; the runner
    catches and downgrades to a skip-comment per OP-691 AC.
    """
    if get_issue_status(client, key) == UNDER_REVIEW_STATUS_NAME:
        return False
    idem_key = idem_key or f"transition-{key}-under-review-{uuid.uuid4().hex[:12]}"
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
    base_key = idem_key or f"transition-{key}-under-review-{uuid.uuid4().hex[:12]}"
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
) -> None:
    """In Progress → TODO with reason comment + clear assignee."""
    base_key = idem_key or f"transition-{key}-back-to-todo-{uuid.uuid4().hex[:12]}"
    add_comment(
        client, key, f"Reverting to TODO. Reason:\n{reason}",
        idem_key=f"{base_key}-comment",
    )
    clear_assignee(client, key, idem_key=f"{base_key}-clear-assignee")
    _request_idempotent(
        client, "POST", f"/issue/{key}/transitions",
        {"transition": {"id": TRANSITION_IDS["back_to_todo"]}},
        f"{base_key}-transition",
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


def _open_bot_owned_file_owners() -> dict[str, list[GerritFileOwner]]:
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
        "is:open",
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


def _paths_overlap(targets: set[str], in_flight: set[str]) -> set[str]:
    import fnmatch

    overlaps: set[str] = set()
    for target in targets:
        for path in in_flight:
            if target == path or fnmatch.fnmatch(path, target) or fnmatch.fnmatch(target, path):
                overlaps.add(path)
    return overlaps


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
        in_flight_owners = _open_bot_owned_file_owners()
    except (OSError, subprocess.SubprocessError) as exc:
        return True, f"gerrit query failed - mutex check skipped: {type(exc).__name__}: {exc}"

    overlap = _paths_overlap(target, set(in_flight_owners))
    if not overlap:
        return True, "no collision"

    first_path = sorted(overlap)[0]
    owner = in_flight_owners[first_path][0]

    if FILE_OVERLAP_OVERRIDE_LABEL in set(getattr(snapshot, "labels", ())):
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


def find_mutex_holders(
    client: "DispatchClient",
    mutex_labels: list[str],
    exclude_key: str,
) -> list[dict]:
    """Return JIRA issues currently holding any of ``mutex_labels``.

    "Holding" = status in :data:`MUTEX_HOLDING_STATUSES`. Used by
    :func:`pre_pickup_ok` (OP-687) to enforce that two agents never
    concurrently work tickets sharing a ``mutex:<resource-id>``.
    """
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


def pre_pickup_ok(
    client: DispatchClient,
    snapshot: TicketSnapshot,
    worktree_path: Path | None = None,
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
    """
    from backend.agents.live_state_check import evaluate, all_passed, format_failures
    from backend.agents.file_coordinator import has_unresolved_blockedby
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

    return True, "pre-pickup checks passed"
