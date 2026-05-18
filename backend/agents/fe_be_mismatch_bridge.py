"""OP-1483 — JIRA hotfix bridge for the FEBEBundleMismatch alert.

When the ``FEBEBundleMismatch`` Prometheus alert fires (see
``prometheus/rules/image-compat.yml``), Alertmanager invokes this
module's :func:`handle_alert` either via the runner_stoploss-style
daemon or directly from an HTTP webhook. The handler files exactly
one JIRA ticket per ``(fe_bundle, be_bundle)`` pair so the runner
recognises ``runner-blocked:fe-be-mismatch`` and refuses to pick up
new work while the deploy is being reconciled — exactly the gate
Codex's 2026-05-18 morning incident needed but did not have.

Why a dedicated module, not a generic webhook receiver:

* Idempotency: re-firing the alert (each Prometheus tick is its own
  ``OBSERVATION``) must not create N tickets. We index already-filed
  tickets by ``(fe_bundle, be_bundle)`` so the second-and-onward
  firings just no-op.
* The runner-block label is what stops the auto-runner from pulling
  TODO tickets while the deploy is broken. Without that gate, the
  runner would happily keep landing patches on a backend whose API
  contract the live FE doesn't match.
* The bridge runs out-of-process from FastAPI (so a backend restart
  during the incident doesn't lose ticket-filing state); the
  in-memory index is rebuilt on cold start by querying JIRA for open
  tickets carrying the ``runner-blocked:fe-be-mismatch`` label.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

#: Label the runner reads to refuse pickups. Mirrors the
#: ``runner-blocked:waiting-<key>`` shape from
#: :data:`backend.agents.jira_dispatch.DEPENDENCY_WAITING_LABEL_PREFIX`
#: so the operator mental model stays consistent.
RUNNER_BLOCK_LABEL = "runner-blocked:fe-be-mismatch"

#: Default JIRA agent_class the bridge uses. The merger bot account
#: already has issue-create rights and the +2 capability we need for
#: dual-sign — using the same identity here keeps the audit log
#: coherent. Operators can override via ``OMNISIGHT_FE_BE_BRIDGE_AGENT_CLASS``.
DEFAULT_AGENT_CLASS = "subscription-merger"

#: Process-local guard against duplicate tickets when an Alertmanager
#: re-fire arrives within the same process lifetime. The authoritative
#: dedupe is the JIRA-side label query in :func:`_find_existing_ticket`;
#: this cache just shaves off the round-trip for the common burst case.
_lock = threading.RLock()
_seen_pairs: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class MismatchAlert:
    """Normalised Alertmanager payload — only the fields we actually use."""

    fe_bundle: str
    be_bundle: str
    fired_at: str
    summary: str


def _strip(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def parse_alertmanager_payload(payload: dict) -> list[MismatchAlert]:
    """Normalise an Alertmanager webhook body into :class:`MismatchAlert`s.

    Alertmanager batches alerts under ``alerts: [...]``; each entry
    carries ``labels`` + ``annotations`` + ``status``. We only act on
    ``status == "firing"`` so a recovery (``resolved``) does not
    re-create a ticket the operator has already triaged.
    """
    alerts: list[MismatchAlert] = []
    for entry in payload.get("alerts", []) or []:
        if not isinstance(entry, dict):
            continue
        if _strip(entry.get("status")) != "firing":
            continue
        labels = entry.get("labels") or {}
        annotations = entry.get("annotations") or {}
        if _strip(labels.get("alertname")) != "FEBEBundleMismatch":
            continue
        fe = _strip(labels.get("fe_bundle"))
        be = _strip(labels.get("be_bundle"))
        if not fe or not be or fe == be:
            # Defensive: an alert that doesn't actually name two
            # distinct bundles is noise — skip.
            continue
        alerts.append(
            MismatchAlert(
                fe_bundle=fe,
                be_bundle=be,
                fired_at=_strip(entry.get("startsAt")) or "unknown",
                summary=_strip(annotations.get("summary"))
                or "FE/BE bundle mismatch",
            )
        )
    return alerts


def _agent_class() -> str:
    return os.environ.get(
        "OMNISIGHT_FE_BE_BRIDGE_AGENT_CLASS",
        DEFAULT_AGENT_CLASS,
    ).strip() or DEFAULT_AGENT_CLASS


def _find_existing_ticket(
    client,
    fe_bundle: str,
    be_bundle: str,
) -> Optional[str]:
    """Return the key of an open mismatch ticket for ``(fe, be)``, if any.

    Authoritative dedupe — looks at JIRA itself rather than the
    in-process cache so a runner restart cannot file a second ticket
    for an incident already in flight. JQL keys on the runner-block
    label plus an exact-text match of the bundle pair in the summary
    line (formatted by :func:`_summary_for`).
    """
    from backend.agents import jira_dispatch

    summary_marker = _summary_for(fe_bundle, be_bundle)
    jql = (
        f'project = "{client.project_key}" '
        f'AND labels = "{RUNNER_BLOCK_LABEL}" '
        f'AND status not in (Done, Closed, Resolved) '
        f'AND summary ~ "{summary_marker}"'
    )
    try:
        resp = jira_dispatch._request(
            client,
            "GET",
            "/search?jql=" + _urlencode(jql) + "&fields=summary,labels",
        )
    except Exception as exc:  # pragma: no cover — JIRA-down path
        log.warning("FE/BE bridge: JQL probe failed: %s", exc)
        return None
    for issue in resp.get("issues", []) or []:
        key = issue.get("key")
        if isinstance(key, str) and key:
            return key
    return None


def _urlencode(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def _summary_for(fe_bundle: str, be_bundle: str) -> str:
    return f"[FE/BE skew] fe={fe_bundle} vs be={be_bundle}"


def _description_for(alert: MismatchAlert) -> str:
    return "\n".join(
        [
            "@operator @oncall",
            "",
            "Prometheus alert `FEBEBundleMismatch` is firing — the backend",
            "received at least one inbound request whose",
            "`X-OmniSight-Frontend-Bundle` header named a bundle id other",
            "than its own.",
            "",
            f"Frontend bundle observed: `{alert.fe_bundle}`",
            f"Backend bundle running:   `{alert.be_bundle}`",
            f"First fired at:           {alert.fired_at}",
            "",
            "While this ticket carries the `runner-blocked:fe-be-mismatch`",
            "label the auto-runner will refuse to pick up new TODO",
            "tickets — the deploy must be reconciled first.",
            "",
            "Triage runbook:",
            "  docs/operations/fe-be-compat-monitoring.md",
            "",
            "Single-replica verification: `curl /readyz | jq",
            "  .checks.frontend_compat_check`.",
            "",
            "Once the FE and BE images are realigned, close this ticket",
            "to release the runner block.",
        ]
    )


def file_ticket_for_alert(client, alert: MismatchAlert) -> str:
    """Idempotently file the JIRA ticket for one alert and return its key."""
    from backend.agents import jira_dispatch

    pair = (alert.fe_bundle, alert.be_bundle)
    with _lock:
        cached = _seen_pairs.get(pair)
    if cached:
        return cached

    existing = _find_existing_ticket(client, *pair)
    if existing:
        with _lock:
            _seen_pairs[pair] = existing
        return existing

    body = {
        "fields": {
            "project": {"key": client.project_key},
            "summary": _summary_for(*pair),
            "description": jira_dispatch._adf_codeblock(_description_for(alert)),
            "issuetype": {"name": "Story"},
            "priority": {"name": "High"},
            "labels": [
                RUNNER_BLOCK_LABEL,
                # Add a per-pair label too so a JQL search by pair
                # works without parsing the summary line.
                f"fe-be-mismatch:fe={alert.fe_bundle}",
                f"fe-be-mismatch:be={alert.be_bundle}",
            ],
        }
    }
    resp = jira_dispatch._request(client, "POST", "/issue", body)
    key = str(resp.get("key") or "")
    if not key:
        raise RuntimeError(
            f"FE/BE bridge: JIRA POST /issue returned no key: {resp!r}"
        )
    with _lock:
        _seen_pairs[pair] = key
    log.warning(
        "FE/BE bridge: filed %s for fe=%s vs be=%s",
        key, alert.fe_bundle, alert.be_bundle,
    )
    return key


def handle_alert(payload: dict, *, agent_class: Optional[str] = None) -> list[str]:
    """Top-level entry point for the Alertmanager webhook handler.

    Returns the list of JIRA keys touched (newly created or recovered
    from the existing-open search). Empty list means the payload had
    no firing alerts after filtering.
    """
    from backend.agents import jira_dispatch

    alerts = parse_alertmanager_payload(payload)
    if not alerts:
        return []

    client = jira_dispatch.make_client(agent_class or _agent_class())
    keys: list[str] = []
    for alert in alerts:
        try:
            key = file_ticket_for_alert(client, alert)
            keys.append(key)
        except Exception as exc:
            log.error(
                "FE/BE bridge: file_ticket_for_alert(%s vs %s) failed: %s",
                alert.fe_bundle, alert.be_bundle, exc,
            )
    return keys


def reset_for_tests() -> None:
    """Test-only — clear the in-process seen-pairs cache."""
    with _lock:
        _seen_pairs.clear()


def seen_pairs_snapshot() -> dict[tuple[str, str], str]:
    """Test helper — return a copy of the in-process cache."""
    with _lock:
        return dict(_seen_pairs)


__all__ = [
    "RUNNER_BLOCK_LABEL",
    "MismatchAlert",
    "parse_alertmanager_payload",
    "file_ticket_for_alert",
    "handle_alert",
    "reset_for_tests",
    "seen_pairs_snapshot",
]
