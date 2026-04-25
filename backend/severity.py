"""R9 row 2935 (#315) — severity-tag spec for tiered notifications.

Design decision (covers TODO row 2935 + its 3 sub-bullets verbatim):

  *Do not* introduce a separate P1/P2/P3 routing tier. Instead, treat
  ``severity`` as an **orthogonal tag** that rides on top of the
  existing L1-L4 ``NotificationLevel`` ladder
  (``info`` / ``warning`` / ``action`` / ``critical``) which already
  owns external-channel routing
  (Slack / Jira / PagerDuty / SMS) in :mod:`backend.notifications`.

The mapping from severity → activated tiers below is the load-bearing
contract for R9; row 2939's ``send_notification(tier, severity,
payload, interactive=False)`` extension consumes :data:`SEVERITY_TIER_
MAPPING` to decide which fan-out paths (PagerDuty / Jira / Slack /
ChatOps interactive / log+email) actually fire.

Why a tag, not a new tier
─────────────────────────
``NotificationLevel`` is a *channel-routing* concept (which transport
gets the message). ``Severity`` is an *operational-priority* concept
(how urgently a human must act). Conflating them — e.g. a separate
P-ladder Pydantic enum on ``Notification.priority`` — duplicates the
ladder and forces every existing call-site to pick BOTH a level and a
priority, doubling the cognitive surface for negligible gain.
A tag co-exists: legacy callers (40+ ``notify(level=...)`` sites in
agents / watchdog / chatops) keep working unchanged, severity-aware
callers (the new R9 watchdog event taxonomy
``watchdog.p1_system_down`` / ``...p2_cognitive_deadlock`` /
``...p3_auto_recovery``) attach the tag and the dispatcher fans out
correctly.

The tier mapping below is *strictly additive* with respect to the
underlying ``NotificationLevel`` ladder — picking a severity raises
the effective tier *floor* (P1 floors at L4, P2 at L3, P3 at L1) but
never overrides an explicit higher level the caller passed in. The
fan-out semantics are spelled out per row in ``Tiers``.

TODO row 2935 sub-bullet contract (verbatim from TODO.md):

  - P1 (系統崩潰)        → L4 PagerDuty + L3 Jira (severity: P1)
                          + L2 Slack/Discord @everyone + SMS
  - P2 (任務卡死)        → L3 Jira (severity: P2, label: blocked)
                          + L2 ChatOps interactive (R1)
  - P3 (自動修復中)      → L1 log + email digest

Module-global state: this module is pure constants — no module-level
mutable state, no module-global singletons. Every worker imports the
same frozenset values (qualifying answer #1: derived deterministically
from source). No cross-worker coordination required.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType


class Severity(str, Enum):
    """Operational priority tag for tiered notifications.

    Rides on top of ``NotificationLevel`` — see module docstring for
    why this is a tag, not a separate tier.
    """

    P1 = "P1"  # 系統崩潰 — pager-worthy, immediate human response
    P2 = "P2"  # 任務卡死 — blocked work, on-call should look soon
    P3 = "P3"  # 自動修復中 — informational, batched digest only


# ─────────────────────────────────────────────────────────────────
#  Tier identifiers
# ─────────────────────────────────────────────────────────────────
#
# These string constants name the *fan-out destinations* a severity
# activates. They are NOT redefining ``NotificationLevel`` — the
# correspondence is intentional but loose:
#
#   * "L1"   — system log + email digest (info-level transport)
#   * "L2"   — Slack/Discord IM webhook OR ChatOps interactive bridge
#   * "L3"   — Jira / issue-tracker ticket creation
#   * "L4"   — PagerDuty + SMS (paging fan-out)
#
# A single severity may activate multiple tiers (e.g. P1 → L4+L3+L2).
# The dispatcher (row 2939) reads this set and fires each leg
# independently with the correct payload variant (e.g. Jira ticket
# carries ``severity=P2, label=blocked``; Slack message carries
# ``@everyone`` for P1 but plain interactive card for P2).

L1_LOG_EMAIL = "L1_LOG_EMAIL"
L2_IM_WEBHOOK = "L2_IM_WEBHOOK"
L2_CHATOPS_INTERACTIVE = "L2_CHATOPS_INTERACTIVE"
L3_JIRA = "L3_JIRA"
L4_PAGERDUTY = "L4_PAGERDUTY"
L4_SMS = "L4_SMS"


# ─────────────────────────────────────────────────────────────────
#  Severity → activated tiers
# ─────────────────────────────────────────────────────────────────

SEVERITY_TIER_MAPPING: "MappingProxyType[Severity, frozenset[str]]" = (
    MappingProxyType({
        Severity.P1: frozenset({
            L4_PAGERDUTY,
            L4_SMS,
            L3_JIRA,
            L2_IM_WEBHOOK,
        }),
        Severity.P2: frozenset({
            L3_JIRA,
            L2_CHATOPS_INTERACTIVE,
        }),
        Severity.P3: frozenset({
            L1_LOG_EMAIL,
        }),
    })
)
"""Read-only mapping of severity tag → activated dispatch tiers.

Wrapped in :class:`MappingProxyType` so callers can't mutate the spec
at runtime (any attempted ``SEVERITY_TIER_MAPPING[Severity.P1] = ...``
raises ``TypeError``).

Each frozenset is *strictly additive* — the dispatcher fires every
named tier independently. Empty intersection between severities by
design: the spec is hierarchical (P1 ⊃ P2 routing-wise) only in the
"P1 reaches Slack/Discord, P2 reaches ChatOps interactive" sense,
not via subset-check; row 2935 sub-bullets explicitly diverge in
*which* L2 channel each severity hits.
"""


# ─────────────────────────────────────────────────────────────────
#  Severity → highest NotificationLevel floor
# ─────────────────────────────────────────────────────────────────

SEVERITY_LEVEL_FLOOR: "MappingProxyType[Severity, str]" = MappingProxyType({
    Severity.P1: "critical",  # pager-tier
    Severity.P2: "action",    # ticket-tier
    Severity.P3: "info",      # log-only
})
"""Severity → minimum :class:`NotificationLevel` the dispatcher should
treat the notification as. A caller explicitly passing a *higher*
level wins; this floor only applies when the level was unspecified or
lower than the severity warrants.
"""


def tiers_for(severity: Severity | str) -> frozenset[str]:
    """Return the set of activated tier identifiers for ``severity``.

    Accepts either the :class:`Severity` enum or its string value
    ("P1" / "P2" / "P3") for ergonomic call-site use. Unknown inputs
    return an empty frozenset (caller can treat that as "no severity
    tag — fall back to plain level routing").
    """
    if isinstance(severity, str):
        try:
            severity = Severity(severity)
        except ValueError:
            return frozenset()
    return SEVERITY_TIER_MAPPING.get(severity, frozenset())


def level_floor_for(severity: Severity | str) -> str | None:
    """Return the ``NotificationLevel.value`` floor for ``severity``,
    or ``None`` for unknown severities.
    """
    if isinstance(severity, str):
        try:
            severity = Severity(severity)
        except ValueError:
            return None
    return SEVERITY_LEVEL_FLOOR.get(severity)


__all__ = [
    "Severity",
    "SEVERITY_TIER_MAPPING",
    "SEVERITY_LEVEL_FLOOR",
    "L1_LOG_EMAIL",
    "L2_IM_WEBHOOK",
    "L2_CHATOPS_INTERACTIVE",
    "L3_JIRA",
    "L4_PAGERDUTY",
    "L4_SMS",
    "tiers_for",
    "level_floor_for",
]
