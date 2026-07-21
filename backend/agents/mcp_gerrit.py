"""META OP-814 / A6 — MCP-Gerrit integration (read-only).

Same pattern as A5 (META OP-808 JIRA MCP) but for Gerrit. Exposes a
minimal MCP-shaped server, named ``mcp_gerrit``, that lets the agent:

  * call ``mcp_gerrit__queryChanges(filter)`` — issue arbitrary
    ``gerrit query`` filters and parse the JSON response,
  * call ``mcp_gerrit__getReview(change_id)`` — fetch one change's
    current-patch-set + review comments + approval votes,
  * call ``mcp_gerrit__hasOpenPsForTicket(ticket_key)`` — the
    idempotency-check helper used by the synthetic acceptance test
    ("model checks if its ticket has open PS before starting work").

All three tools are *read-only*. PS push stays in
:func:`backend.agents.jira_dispatch.push_to_gerrit_for_review` so the
runner's ``[runner-pushed-to-gerrit]`` JIRA-comment audit trail is
preserved (AC #3).

Implementation reuses the SSH key + bot-username resolution from
``jira_dispatch`` so we never duplicate the per-instance bot identity
logic (OP-783). Each subprocess call is gated by the existing
``BREAKERS["gerrit_ssh"]`` circuit breaker so an unreachable Gerrit
falls back to the same recovery path as the rest of the runner.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from backend.agents.circuit_breaker import BREAKERS
from backend.agents.jira_dispatch import (
    GERRIT_PROJECT_PATH,
    GERRIT_SSH_HOST,
    GERRIT_SSH_PORT,
    _gerrit_auth_for_instance,
)

logger = logging.getLogger(__name__)


SERVER_NAME = "mcp_gerrit"
"""Tool-name prefix exposed to the agent (``mcp_gerrit__<method>``)."""

# ─── Tunable constants ───────────────────────────────────────────
# Per-subprocess SSH-CLI ceiling. Sized to comfortably absorb a slow
# Gerrit query without indefinitely blocking the circuit breaker.
DEFAULT_TIMEOUT_SECS = 30

# Maximum stderr bytes embedded in a ``RuntimeError`` when the
# gerrit-ssh-cli call fails. Keeps a single broken call from flooding
# the agent's tool-result transcript with multi-KB SSH banners.
STDERR_TRUNCATE_LIMIT = 500

# HTTPS port that fronts Gerrit's web UI / REST surface. Distinct from
# ``GERRIT_SSH_PORT`` (the gerrit-ssh-cli port) — used only to synthesize
# fallback change URLs when ``gerrit query`` does not return one.
GERRIT_WEB_PORT = 29420

# Default cap on rows returned by ``query_changes``. Prevents a runaway
# model query from pulling a multi-MB JSON payload through the audit
# log; the caller can override per-call up to ``MAX_QUERY_CHANGES_LIMIT``.
DEFAULT_QUERY_CHANGES_LIMIT = 25

# Validation bounds advertised in the MCP tool schema for the
# ``queryChanges.limit`` argument. Upper bound matches Gerrit's own
# server-side default page size, lower bound rejects ``0`` / negative
# limits that would silently return an empty list.
MIN_QUERY_CHANGES_LIMIT = 1
MAX_QUERY_CHANGES_LIMIT = 100

# Row cap for the idempotency check in ``has_open_ps_for_ticket``. The
# subject-match filter is already narrow, so a small window is enough
# to spot a sibling open PS without scanning the full open-PS backlog.
OPEN_PS_LOOKUP_LIMIT = 5


@dataclass(frozen=True)
class GerritChangeSummary:
    """One row from a ``gerrit query`` result."""

    change_number: int
    change_id: str
    subject: str
    status: str
    owner: str
    project: str
    branch: str
    url: str

    def to_json(self) -> dict[str, Any]:
        return {
            "change_number": self.change_number,
            "change_id": self.change_id,
            "subject": self.subject,
            "status": self.status,
            "owner": self.owner,
            "project": self.project,
            "branch": self.branch,
            "url": self.url,
        }


@dataclass(frozen=True)
class GerritReviewDetails:
    """Detailed view of one change including comments + approvals."""

    change_number: int
    change_id: str
    subject: str
    status: str
    owner: str
    current_revision: str | None
    approvals: tuple[dict[str, Any], ...]
    comments: tuple[dict[str, Any], ...]
    url: str

    def to_json(self) -> dict[str, Any]:
        return {
            "change_number": self.change_number,
            "change_id": self.change_id,
            "subject": self.subject,
            "status": self.status,
            "owner": self.owner,
            "current_revision": self.current_revision,
            "approvals": list(self.approvals),
            "comments": list(self.comments),
            "url": self.url,
        }


def _ssh_argv(
    extra_args: list[str],
    *,
    agent_class: str,
    instance_id: str | None,
) -> list[str]:
    """Build the gerrit-ssh-cli argv used by ``jira_dispatch`` callers."""
    bot_username, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    return [
        "ssh",
        "-i", str(ssh_key),
        "-p", str(GERRIT_SSH_PORT),
        f"{bot_username}@{GERRIT_SSH_HOST}",
        *extra_args,
    ]


def _run_gerrit_query(
    argv: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> str:
    """Run a gerrit-ssh-cli subprocess through the shared circuit breaker."""
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gerrit-ssh-cli failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()[:STDERR_TRUNCATE_LIMIT]}"
        )
    return result.stdout


def _iter_gerrit_json_lines(stdout: str):
    """Yield decoded JSON dicts from a ``--format=JSON`` stdout, skipping
    the trailing ``{"type": "stats", ...}`` row."""
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "stats":
            continue
        yield obj


def _build_change_url(change: dict[str, Any]) -> str:
    url = str(change.get("url") or "")
    if url:
        return url
    number = change.get("number") or change.get("_number") or "?"
    return f"https://{GERRIT_SSH_HOST}:{GERRIT_WEB_PORT}/c/{GERRIT_PROJECT_PATH}/+/{number}"


def _parse_change_summary(change: dict[str, Any]) -> GerritChangeSummary | None:
    number = change.get("number") or change.get("_number")
    if number is None:
        return None
    try:
        change_number = int(number)
    except (TypeError, ValueError):
        return None
    owner_blob = change.get("owner") or {}
    return GerritChangeSummary(
        change_number=change_number,
        change_id=str(change.get("id") or ""),
        subject=str(change.get("subject") or ""),
        status=str(change.get("status") or ""),
        owner=str(owner_blob.get("username") or owner_blob.get("email") or ""),
        project=str(change.get("project") or ""),
        branch=str(change.get("branch") or ""),
        url=_build_change_url(change),
    )


# ─── Read-only tools exposed via mcp_gerrit__* ────────────────────


def query_changes(
    filter: str,
    *,
    agent_class: str = "subscription-claude",
    instance_id: str | None = None,
    limit: int = DEFAULT_QUERY_CHANGES_LIMIT,
) -> list[GerritChangeSummary]:
    """Run ``gerrit query --format=JSON <filter>`` and return parsed rows.

    ``filter`` is a free-form Gerrit search expression, e.g.
    ``"is:open owner:claude-bot project:omnisight/OmniSight-Productizer"``.
    Caller is responsible for whitespace-quoting; the SSH layer does not
    re-shell-expand.

    ``limit`` caps the returned rows so a runaway model query can't pull
    a multi-MB JSON payload through the audit log.
    """
    if not filter or not filter.strip():
        raise ValueError("query_changes: filter must be a non-empty string")
    argv = _ssh_argv(
        ["gerrit", "query", "--format=JSON", filter],
        agent_class=agent_class,
        instance_id=instance_id,
    )
    stdout = _run_gerrit_query(argv)
    rows: list[GerritChangeSummary] = []
    for obj in _iter_gerrit_json_lines(stdout):
        summary = _parse_change_summary(obj)
        if summary is None:
            continue
        rows.append(summary)
        if len(rows) >= limit:
            break
    return rows


def get_review(
    change_id: str,
    *,
    agent_class: str = "subscription-claude",
    instance_id: str | None = None,
) -> GerritReviewDetails | None:
    """Fetch one change's review state + current patch-set metadata.

    ``change_id`` may be either a Gerrit Change-Id (``I…``) or a numeric
    change number — both are accepted by ``gerrit query change:<id>``.
    Returns None if Gerrit returns no rows.
    """
    if not change_id or not str(change_id).strip():
        raise ValueError("get_review: change_id must be a non-empty string")

    argv = _ssh_argv(
        [
            "gerrit", "query", "--format=JSON",
            "--current-patch-set",
            "--all-approvals",
            "--comments",
            f"change:{change_id}",
        ],
        agent_class=agent_class,
        instance_id=instance_id,
    )
    stdout = _run_gerrit_query(argv)
    for obj in _iter_gerrit_json_lines(stdout):
        number = obj.get("number") or obj.get("_number")
        if number is None:
            continue
        try:
            change_number = int(number)
        except (TypeError, ValueError):
            continue
        owner_blob = obj.get("owner") or {}
        current_ps = obj.get("currentPatchSet") or {}
        approvals = tuple(current_ps.get("approvals") or ())
        comments_raw = obj.get("comments") or []
        comments = tuple(
            {
                "timestamp": c.get("timestamp"),
                "reviewer": (c.get("reviewer") or {}).get("username")
                or (c.get("reviewer") or {}).get("email"),
                "message": c.get("message"),
            }
            for c in comments_raw
        )
        return GerritReviewDetails(
            change_number=change_number,
            change_id=str(obj.get("id") or ""),
            subject=str(obj.get("subject") or ""),
            status=str(obj.get("status") or ""),
            owner=str(owner_blob.get("username") or owner_blob.get("email") or ""),
            current_revision=str(current_ps.get("revision") or "") or None,
            approvals=approvals,
            comments=comments,
            url=_build_change_url(obj),
        )
    return None


# U6-0 T8-B1 (OP-2611): identity-bound merge verification for the episodic
# verified-write path. An UNAUTHENTICATED webhook must not be trusted about
# whether a change merged — this re-derives the truth from Gerrit, bound to
# the trusted project + the exact instance-global change number, and requires
# both MERGED status and a NON-BOT Code-Review>=2 (via learned_item_provenance).
_MAX_CHANGE_NUMBER = 10_000_000


@dataclass(frozen=True)
class VerifiedMerge:
    change_number: int
    change_id: str
    canonical_subject: str
    revision: str
    project: str
    branch: str
    plus2_reviewer: str = ""  # NON-BOT +2 approver (review_span.by); "" if unknown
    # β-1: current patchset NUMBER (== patchset count) — the hard-ticket
    # gate's struggle signal. None when the payload lacks it.
    patchset_count: "int | None" = None


def verify_merged_change(
    *,
    change_number: int,
    project: str,
    expected_change_id: str = "",
    expected_revision: str = "",
    agent_class: str = "subscription-claude",
    instance_id: str | None = None,
) -> "VerifiedMerge | None":
    """Independently confirm that ``change_number`` really merged in the
    trusted project, returning Gerrit-CANONICAL identity + content, or None.

    Fails CLOSED (returns None, never raises) on: an out-of-bounds number, a
    project other than the configured :data:`GERRIT_PROJECT_PATH`, zero or
    more-than-one matching rows (kills Change-Id ambiguity), a status other
    than MERGED, a missing NON-BOT Code-Review>=2, an identity mismatch
    against ``expected_change_id``/``expected_revision`` when supplied, or any
    query/parse error. Callers run it in an executor (sync SSH).
    """
    import datetime as _dt

    from backend import learned_item_provenance as _prov

    # Bounded numeric — keep a giant/negative id off argv before any query.
    if not isinstance(change_number, int) or change_number <= 0 or change_number > _MAX_CHANGE_NUMBER:
        return None
    # Trust the project allowlist from CONFIG, never the webhook.
    if project != GERRIT_PROJECT_PATH:
        return None

    argv = _ssh_argv(
        [
            "gerrit", "query", "--format=JSON",
            "--current-patch-set", "--all-approvals",
            f"change:{change_number}", f"project:{GERRIT_PROJECT_PATH}",
        ],
        agent_class=agent_class,
        instance_id=instance_id,
    )
    try:
        stdout = _run_gerrit_query(argv)
        rows = [r for r in _iter_gerrit_json_lines(stdout)]
    except Exception:  # noqa: BLE001 — verify must never raise
        return None

    # Exactly one row — 0 or >1 is ambiguous, fail closed.
    if len(rows) != 1:
        return None
    row = rows[0]

    # Bind identity to the AUTHENTICATED row, never the webhook body.
    row_number = row.get("number") or row.get("_number")
    try:
        if int(row_number) != change_number:
            return None
    except (TypeError, ValueError):
        return None
    if str(row.get("project") or "") != GERRIT_PROJECT_PATH:
        return None
    row_change_id = str(row.get("id") or "")
    current_ps = row.get("currentPatchSet") or {}
    revision = str(current_ps.get("revision") or "")
    if expected_change_id and row_change_id != expected_change_id:
        return None
    if expected_revision and revision != expected_revision:
        return None

    # Merge + NON-BOT-review gate. Build the shape derive_ground_truths
    # wants from the RAW row (the deriver reads currentPatchSet.approvals).
    # change_ref MUST be the bare number — a "gerrit:" prefix is rejected by
    # normalize_change_ref (silent no-op). Keep the tuple so we can carry the
    # NON-BOT approver identity into the VerifiedMerge (β-F ledger evidence).
    gerrit_change = {
        "status": row.get("status"),
        "currentPatchSet": {"approvals": current_ps.get("approvals") or []},
    }
    try:
        _truths = _prov.derive_ground_truths(
            change_ref=str(change_number),
            gerrit_change=gerrit_change,
            jira_labels=None,
            now=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        )
    except _prov.ProvenanceUnconfirmable:
        return None
    if not {"merged", "review_plus2"} <= {t.kind for t in _truths}:
        return None
    reviewer = next(
        (str((t.evidence_span or {}).get("by") or "")
         for t in _truths if t.kind == "review_plus2"),
        "",
    )
    # β-1: currentPatchSet.number == patchset count (gerrit query sends it
    # as a string; same coercion idiom as gerrit_jira_bridge).
    try:
        ps_count = int(current_ps.get("number"))
    except (TypeError, ValueError):
        ps_count = None

    return VerifiedMerge(
        change_number=change_number,
        change_id=row_change_id,
        canonical_subject=str(row.get("subject") or ""),
        revision=revision,
        project=GERRIT_PROJECT_PATH,
        branch=str(row.get("branch") or ""),
        plus2_reviewer=reviewer,
        patchset_count=ps_count,
    )


# Gerrit prefixes every REST JSON body with an XSSI guard line so a
# `<script>` include can't execute the payload; strip it before decode.
_GERRIT_XSSI_PREFIX = ")]}'"


def _parse_gerrit_rest_json(text: str) -> Any:
    """Strip Gerrit's XSSI guard prefix and decode the REST JSON body."""
    body = text.lstrip()
    if body.startswith(_GERRIT_XSSI_PREFIX):
        body = body[len(_GERRIT_XSSI_PREFIX):]
    return json.loads(body)


def _rest_labels_to_approvals(labels: dict | None) -> list[dict]:
    """Adapt Gerrit REST ``DETAILED_LABELS`` → the raw
    ``currentPatchSet.approvals`` shape that
    :func:`learned_item_provenance.derive_ground_truths` consumes.

    This is a pure SHAPE adapter — the NON-BOT +2 decision stays in the
    one deriver (never re-implemented here). REST ``labels[<name>].all[]``
    entries carry ``value`` (int) + a top-level ``username``/``name``/
    ``_account_id``; the deriver reads ``approval.type`` + ``approval.value``
    (str-ok) + ``approval.by.{username,name}``.
    """
    approvals: list[dict] = []
    for label_name, label_body in (labels or {}).items():
        if not isinstance(label_body, dict):
            continue
        for entry in label_body.get("all") or []:
            if not isinstance(entry, dict) or "value" not in entry:
                continue
            approvals.append({
                "type": label_name,
                "value": entry.get("value"),
                "by": {
                    "username": entry.get("username") or "",
                    "name": entry.get("name") or "",
                },
            })
    return approvals


async def verify_merged_change_http(
    *,
    change_number: int,
    project: str,
    expected_change_id: str = "",
    expected_revision: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_SECS,
) -> "VerifiedMerge | None":
    """Serving-safe sibling of :func:`verify_merged_change` (β-F / leg-2).

    Re-derives a merged change's Gerrit-CANONICAL identity + content over
    the AUTHENTICATED REST API (``GET /a/changes/?o=CURRENT_REVISION&``
    ``o=DETAILED_LABELS``) using a scoped read-only HTTP account, instead
    of the SSH bot key that only exists on the runner host. This lets the
    serving backend confirm merges without the shell key — closing the
    silent fail-closed hole where ``verify_merged_change`` (SSH) raises on
    a keyless replica and drops every merge with only a warning.

    Identical fail-closed contract, and — critically — the SAME NON-BOT
    ``Code-Review>=2`` decision via ``derive_ground_truths`` (the REST
    payload is only RESHAPED, never re-judged). Returns None (never
    raises) on: missing HTTP creds (⇒ caller falls back to SSH), an
    out-of-bounds number, a project other than the configured allowlist,
    zero/more-than-one rows, a non-MERGED status, a missing non-bot +2,
    an identity mismatch, or any transport/parse error.
    """
    import datetime as _dt

    import httpx

    from backend import learned_item_provenance as _prov
    from backend.config import settings

    # Bounded numeric + project allowlist from CONFIG, never the webhook.
    if not isinstance(change_number, int) or change_number <= 0 or change_number > _MAX_CHANGE_NUMBER:
        return None
    if project != GERRIT_PROJECT_PATH:
        return None

    base = (settings.gerrit_url or "").rstrip("/")
    user = settings.gerrit_http_user or ""
    password = settings.gerrit_http_password or ""
    if not (base and user and password):
        # HTTP verify unavailable on this deploy — signal the caller to
        # fall back to the SSH path (where the key exists).
        return None

    params = {
        "q": f"change:{change_number} project:{GERRIT_PROJECT_PATH}",
        "o": ["CURRENT_REVISION", "DETAILED_LABELS"],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(
                f"{base}/a/changes/",
                params=params,
                auth=httpx.BasicAuth(user, password),
            )
        if resp.status_code != 200:
            return None
        rows = _parse_gerrit_rest_json(resp.text)
    except Exception:  # noqa: BLE001 — verify must never raise
        return None

    # Exactly one row — 0 or >1 is ambiguous, fail closed.
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]

    # Bind identity to the AUTHENTICATED row, never the webhook body.
    try:
        if int(row.get("_number")) != change_number:
            return None
    except (TypeError, ValueError):
        return None
    if str(row.get("project") or "") != GERRIT_PROJECT_PATH:
        return None
    row_change_id = str(row.get("change_id") or "")
    revision = str(row.get("current_revision") or "")
    if expected_change_id and row_change_id != expected_change_id:
        return None
    if expected_revision and revision != expected_revision:
        return None

    # Merge + NON-BOT-review gate — SAME deriver as the SSH path (the REST
    # DETAILED_LABELS payload is only RESHAPED into the approvals list, never
    # re-judged). NOTE (β-F residual, foundation-audit BLOCKER-3): the
    # non-bot test in derive_ground_truths is a USERNAME heuristic; the real
    # `non-ai-reviewer` GROUP check (which DETAILED_LABELS + a group lookup
    # could add) is a tracked follow-up upgrade — the one place this could
    # be STRONGER than the SSH path.
    gerrit_change = {
        "status": row.get("status"),
        "currentPatchSet": {
            "approvals": _rest_labels_to_approvals(row.get("labels")),
        },
    }
    try:
        _truths = _prov.derive_ground_truths(
            change_ref=str(change_number),
            gerrit_change=gerrit_change,
            jira_labels=None,
            now=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        )
    except _prov.ProvenanceUnconfirmable:
        return None
    if not {"merged", "review_plus2"} <= {t.kind for t in _truths}:
        return None
    reviewer = next(
        (str((t.evidence_span or {}).get("by") or "")
         for t in _truths if t.kind == "review_plus2"),
        "",
    )
    # β-1: under o=CURRENT_REVISION the row carries revisions[<sha>]._number —
    # the current revision's _number IS the patchset count.
    try:
        ps_count = int(
            ((row.get("revisions") or {}).get(revision) or {}).get("_number")
        )
    except (TypeError, ValueError):
        ps_count = None

    return VerifiedMerge(
        change_number=change_number,
        change_id=row_change_id,
        canonical_subject=str(row.get("subject") or ""),
        revision=revision,
        project=GERRIT_PROJECT_PATH,
        branch=str(row.get("branch") or ""),
        plus2_reviewer=reviewer,
        patchset_count=ps_count,
    )


async def fetch_merged_change_context(
    change_number: int,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_SECS,
) -> "dict | None":
    """β-1 read-only REST context for the worker-loop distiller: the merged
    change's commit message + changed-file list + patchset count.

    Same scoped HTTP auth as :func:`verify_merged_change_http` (one query,
    ``o=CURRENT_REVISION&o=CURRENT_COMMIT&o=CURRENT_FILES``). Returns None on
    ANY failure — missing creds, non-200, 0/многие rows, parse error — the
    distiller then falls back to the deterministic minimal record. This is
    CONTEXT for a quarantined draft, not a trust decision: the trust gate
    stays in verify (non-bot +2), which already ran at ledger-write time.
    """
    import httpx

    from backend.config import settings

    if not isinstance(change_number, int) or change_number <= 0 or change_number > _MAX_CHANGE_NUMBER:
        return None
    base = (settings.gerrit_url or "").rstrip("/")
    user = settings.gerrit_http_user or ""
    password = settings.gerrit_http_password or ""
    if not (base and user and password):
        return None

    params = {
        "q": f"change:{change_number} project:{GERRIT_PROJECT_PATH}",
        "o": ["CURRENT_REVISION", "CURRENT_COMMIT", "CURRENT_FILES"],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(
                f"{base}/a/changes/",
                params=params,
                auth=httpx.BasicAuth(user, password),
            )
        if resp.status_code != 200:
            return None
        rows = _parse_gerrit_rest_json(resp.text)
    except Exception:  # noqa: BLE001 — context fetch must never raise
        return None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    current = str(row.get("current_revision") or "")
    rev = (row.get("revisions") or {}).get(current) or {}
    commit = rev.get("commit") or {}
    files = rev.get("files") or {}
    try:
        ps_count = int(rev.get("_number"))
    except (TypeError, ValueError):
        ps_count = None
    return {
        "commit_message": str(commit.get("message") or ""),
        # Magic paths (/COMMIT_MSG, /MERGE_LIST) start with "/" — drop them.
        "files": sorted(f for f in files if isinstance(f, str) and not f.startswith("/")),
        "patchset_count": ps_count,
    }


def http_verify_available() -> bool:
    """True when a scoped read-only Gerrit HTTP account is configured, so
    :func:`verify_merged_change_http` can run on a serving replica (no SSH
    shell key required)."""
    from backend.config import settings

    return bool(
        (settings.gerrit_url or "")
        and (settings.gerrit_http_user or "")
        and (settings.gerrit_http_password or "")
    )


def verify_capability() -> str:
    """Which merge-verify path is usable on THIS host: ``http`` | ``ssh`` |
    ``degraded``.

    ``degraded`` means neither the REST HTTP account nor the SSH bot key is
    present — every merge verification will fail-closed and NO ground truth
    can be minted here. β-F surfaces this LOUDLY (the ``merge_verify_total``
    ``path=degraded`` counter + an audit log) instead of the historical
    silent warning-log drop.
    """
    if http_verify_available():
        return "http"
    try:
        _bot, key = _gerrit_auth_for_instance("subscription-claude")
        if key.exists():
            return "ssh"
    except Exception:  # noqa: BLE001 — capability probe must never raise
        pass
    return "degraded"


def has_open_ps_for_ticket(
    ticket_key: str,
    *,
    agent_class: str = "subscription-claude",
    instance_id: str | None = None,
) -> bool:
    """Return True if Gerrit has an *open* PS whose subject mentions
    ``ticket_key``.

    Used by the META OP-814 synthetic acceptance: the agent calls this
    before starting work so the launcher's idempotency check is also
    visible inside the model's reasoning context (rather than only at
    the runner-level pre-flight gate).
    """
    if not ticket_key or not ticket_key.strip():
        raise ValueError("has_open_ps_for_ticket: ticket_key required")
    filter = (
        f"is:open project:{GERRIT_PROJECT_PATH} "
        f"message:{ticket_key.strip()}"
    )
    rows = query_changes(
        filter,
        agent_class=agent_class,
        instance_id=instance_id,
        limit=OPEN_PS_LOOKUP_LIMIT,
    )
    return any(ticket_key in r.subject for r in rows)


# ─── MCP-shaped dispatch surface (mcp_gerrit__<method>) ───────────


def _tool_query_changes(input: dict[str, Any]) -> list[dict[str, Any]]:
    rows = query_changes(
        filter=input["filter"],
        agent_class=input.get("agent_class", "subscription-claude"),
        instance_id=input.get("instance_id"),
        limit=int(input.get("limit", DEFAULT_QUERY_CHANGES_LIMIT)),
    )
    return [r.to_json() for r in rows]


def _tool_get_review(input: dict[str, Any]) -> dict[str, Any] | None:
    review = get_review(
        change_id=input["change_id"],
        agent_class=input.get("agent_class", "subscription-claude"),
        instance_id=input.get("instance_id"),
    )
    return review.to_json() if review is not None else None


def _tool_has_open_ps_for_ticket(input: dict[str, Any]) -> dict[str, Any]:
    has_open = has_open_ps_for_ticket(
        ticket_key=input["ticket_key"],
        agent_class=input.get("agent_class", "subscription-claude"),
        instance_id=input.get("instance_id"),
    )
    return {"ticket_key": input["ticket_key"], "has_open_ps": has_open}


# Read-only by design — every entry below is a query, never a mutation.
# Keep this dict in lock-step with ``MCP_GERRIT_TOOL_SCHEMAS`` so the
# registration in :mod:`mcp_integration` cannot drift.
TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    f"{SERVER_NAME}__queryChanges": _tool_query_changes,
    f"{SERVER_NAME}__getReview": _tool_get_review,
    f"{SERVER_NAME}__hasOpenPsForTicket": _tool_has_open_ps_for_ticket,
}


MCP_GERRIT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": f"{SERVER_NAME}__queryChanges",
        "description": (
            "Run a Gerrit search (``gerrit query --format=JSON <filter>``) "
            "and return parsed change rows. Read-only. Use to inspect "
            "open patchsets the bot owns, recent merged work for similar "
            "tickets, or sibling Change-Ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": (
                        "Gerrit search expression, e.g. "
                        "'is:open owner:claude-bot project:omnisight/OmniSight-Productizer'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": MIN_QUERY_CHANGES_LIMIT,
                    "maximum": MAX_QUERY_CHANGES_LIMIT,
                    "default": DEFAULT_QUERY_CHANGES_LIMIT,
                },
            },
            "required": ["filter"],
        },
    },
    {
        "name": f"{SERVER_NAME}__getReview",
        "description": (
            "Fetch one change's current patch-set + review approvals + "
            "comments. Read-only. Accepts either a Change-Id ('I...') or "
            "a numeric change number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string"},
            },
            "required": ["change_id"],
        },
    },
    {
        "name": f"{SERVER_NAME}__hasOpenPsForTicket",
        "description": (
            "Return ``{has_open_ps: bool}`` for a JIRA ticket key. "
            "Read-only idempotency check the agent calls before starting "
            "work to catch a sibling open PS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_key": {
                    "type": "string",
                    "description": "JIRA ticket key, e.g. 'OP-814'.",
                },
            },
            "required": ["ticket_key"],
        },
    },
]


def is_mcp_gerrit_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` is one of the registered Gerrit MCP tools."""
    return tool_name in TOOL_HANDLERS


def dispatch_tool(tool_name: str, input: dict[str, Any]) -> Any:
    """Execute a registered ``mcp_gerrit__*`` tool call."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise KeyError(
            f"Unknown mcp_gerrit tool {tool_name!r}; "
            f"known: {sorted(TOOL_HANDLERS)}"
        )
    return handler(input)
