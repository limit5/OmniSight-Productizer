"""BP.S.1 — Tier 0..3 sandbox model + Guild × Tier admission matrix.

Pure-Python, runtime-free declaration of the 4-tier sandbox model from
``docs/design/tiered-sandbox-architecture.md`` plus the **Guild × Tier
admission matrix** that says which Guild is allowed to dispatch into
which Tier(s).

This module is **declarative only** — no syscalls, no Docker calls, no
gVisor calls, no I/O of any kind. Downstream consumers (BP.S.4 PEP
Gateway tier-aware policy / BP.B Guild dispatcher / future audit emitters)
read from this module to learn what is structurally allowed before
attempting any actual sandbox provisioning.

Source-of-truth ordering
------------------------
1. **Code**: this module (compile-time constants, hashable enums).
2. **Operator overrides**: ``configs/sandbox_tier_policy.yaml`` (BP.S.2)
   layers on top at runtime. Operator config can *narrow* the matrix
   (e.g. forbid frontend → T2 in air-gapped deployments), it can NOT
   *widen* it past what this module declares (e.g. operator can not
   give the auditor Guild T1 admission — the audit chain integrity
   contract says auditors observe-only).
3. **Audit doc**: ``docs/design/sandbox-tier-audit.md`` (BP.S.3) cites
   this module's matrix verbatim — diff guard test (BP.S.6) ensures the
   doc and the code do not drift.

Scope discipline (BP.S.1 row only)
----------------------------------
This file deliberately ships **without tests** — BP.S.6 is the dedicated
``backend/tests/test_sandbox_tier_policy.py`` row (~20 contract tests
covering matrix shape / policy parsing / PEP integration). The module is
designed to be testable: every public symbol is hashable, frozen, and
deterministic. **No mutable module-level state** — the matrix is a
``MappingProxyType`` view over a frozen dict; the Guild/Tier enums are
``str``-backed for stable serialisation.

SOP §1 module-global state audit
--------------------------------
**Qualified answer #1** (every worker derives the same value from the
same source). All public symbols are immutable compile-time constants
or frozen views. Cross-worker consistency is trivial — every uvicorn
worker imports this module and reaches byte-identical
``GUILD_TIER_ADMISSION_MATRIX``. No singleton, no cache, no env knob,
no first-boot race.

Cross-references
----------------
* Design doc: ``docs/design/tiered-sandbox-architecture.md`` §I (4-tier
  model definitions).
* Sibling module: ``backend/sandbox_capacity.py`` (DRF token weights per
  sandbox class — orthogonal axis: this file says *who can use what
  tier*, capacity says *how much of it they get*).
* PEP integration: BP.S.4 will document how
  ``backend/policy_enforcement_point.py`` reads this matrix during tool
  dispatch and refuses out-of-matrix Guild → Tier requests.
* Risk register: BP.S.5 records R12 (gVisor cost-weight only / not
  actual runtime) — this module's tier definitions are nominal until
  BP.W3.13 (Phase U) lands real gVisor adoption.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import FrozenSet, Mapping


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SandboxTier — 4-tier execution-environment model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class SandboxTier(str, Enum):
    """4-tier sandbox model from ``tiered-sandbox-architecture.md`` §I.

    Each tier represents a different *execution environment* with
    progressively tighter API surfaces and stronger isolation guarantees.
    The string value is the wire / log / metric label — keep it stable
    across releases (alembic 0019 ``guild_id`` column from BP.B.1 will
    eventually carry tier labels in the same format).
    """

    #: **Control Plane** — orchestrator, state manager, OpenRouter gateway.
    #: No sandbox. Holds API keys / Gerrit SSH keys. Forbidden from
    #: directly executing AI-generated scripts or compile commands.
    T0 = "T0"

    #: **Strict Sandbox** — ephemeral microVM (Firecracker / gVisor) or
    #: tightly-locked Docker. Air-gapped except for whitelisted egress
    #: (Git server only). 4 vCPU / 8 GB cap. Destroyed after each task.
    #: Used for ``make`` cross-compile, Valgrind, Python data scripts.
    T1 = "T1"

    #: **Networked Sandbox** — Docker container with egress permission
    #: but VPC-isolated from internal LAN segments. Used for MLOps
    #: dataset crawl / 3rd-party API testing. Mitigates prompt-injection
    #: → internal-network-scan attack surface.
    T2 = "T2"

    #: **Hardware Bridge** — bare-metal host wired to physical EVK
    #: (development board). Agents NEVER ssh in; they speak JSON-only
    #: RPC to a "Hardware Daemon" gatekeeper that validates + executes
    #: requests like ``flash_board`` / ``read_uart`` / ``capture_signal``.
    T3 = "T3"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Guild — 21-Guild taxonomy from BP.B.2
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class Guild(str, Enum):
    """21 Guild definitions (mirror of BP.B.2 ``backend/agents/guilds/``).

    Slugs are lower-snake-case to match the eventual ``guild_id``
    column (TEXT) added by alembic 0019 (BP.B.1). BP.B is not yet
    landed; this module declares the taxonomy ahead so BP.S.1..S.6
    can ship in the foundation window without blocking on B.

    Naming aligns with TODO.md line 153::

        架構 / SA-SD / UX / PM / Gateway / BSP / HAL / Algo-CV /
        Optical / ISP / Audio / Frontend / Backend / SRE / QA /
        Auditor / RedTeam / Forensics / Intel / Reporter / Custom
    """

    #: Architect — system design, ADR, blueprint. Cloud brain only.
    architect = "architect"

    #: SA-SD — Software Architecture / Software Design (analysis +
    #: detailed-design specialist; sits between architect and
    #: backend/frontend implementers).
    sa_sd = "sa_sd"

    #: UX — UX research / wireframes / interaction design.
    ux = "ux"

    #: PM — product management, requirements grooming, sprint planning.
    pm = "pm"

    #: Gateway — orchestrator gateway, A2A / MCP entry, traffic shaping.
    gateway = "gateway"

    #: BSP — Board Support Package (kernel / U-Boot / device tree).
    bsp = "bsp"

    #: HAL — Hardware Abstraction Layer (vendor SDK glue, driver stubs).
    hal = "hal"

    #: Algo-CV — computer-vision algorithm (detection / pose / barcode).
    algo_cv = "algo_cv"

    #: Optical — optics / lens / IR-cut / 3A tuning.
    optical = "optical"

    #: ISP — Image Signal Processor pipeline tuning.
    isp = "isp"

    #: Audio — audio DSP, AEC / ANS / NR.
    audio = "audio"

    #: Frontend — web UI (Next.js / React / Tailwind).
    frontend = "frontend"

    #: Backend — Python / FastAPI / Alembic / Postgres.
    backend = "backend"

    #: SRE — observability, deploy, on-call, incident response.
    sre = "sre"

    #: QA — test plans, contract tests, E2E, regression coverage.
    qa = "qa"

    #: Auditor — read-only audit-chain observer; **never** executes.
    auditor = "auditor"

    #: RedTeam — adversarial / pentest / prompt-injection probes.
    red_team = "red_team"

    #: Forensics — post-incident root-cause, log archaeology.
    forensics = "forensics"

    #: Intel — SecOps threat intel / CVE feed / 0-day watch (BP.I).
    intel = "intel"

    #: Reporter — generate human-facing summaries / changelogs / docs.
    reporter = "reporter"

    #: Custom — operator-defined Guild slot (escape hatch); admission
    #: defaults to T0+T2 (same as Frontend) — operator override via
    #: ``configs/sandbox_tier_policy.yaml`` is expected before use.
    custom = "custom"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Guild × Tier admission matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Reading the matrix
# ------------------
# ``GUILD_TIER_ADMISSION_MATRIX[Guild.bsp]`` returns the frozen set of
# Tiers that the BSP Guild is *structurally* allowed to dispatch into.
# A Guild → Tier request that is NOT in this set is a **policy
# violation** — PEP Gateway (BP.S.4) MUST refuse it.
#
# Design rationale per row
# ------------------------
# The 4-tier model says "different responsibilities live in different
# tiers". The matrix therefore admits each Guild only to the tier(s)
# matching its job description:
#
# * **Cloud-brain Guilds** (architect / SA-SD / UX / PM / Gateway /
#   Reporter / Auditor) → T0 only or T0+T2. They reason / orchestrate /
#   review. They never compile, never touch hardware, never execute
#   AI-generated payloads. Some need T2 for external-source lookups
#   (architect researches new patterns; reporter pulls release-notes
#   templates) but never T1/T3.
#
# * **Compile + simulate Guilds** (BSP / HAL / Algo-CV / ISP / Audio) →
#   T1 + T3. T1 is where ``make`` runs; T3 is where the binary gets
#   flashed onto the EVK. They do NOT need T2 (no internet during
#   compile = reproducible) and do NOT belong in T0 (they execute
#   AI-generated build scripts).
#
# * **Networked-but-safe Guilds** (Backend / Frontend / Optical / Intel /
#   QA) → T0 + T2. They need outbound network (npm registry, pip,
#   threat-intel feeds, optical-lab references) but not direct hardware
#   access. T0 is for the LangGraph reasoning portion that lives in the
#   control plane.
#
# * **Adversarial / observability Guilds** (RedTeam / Forensics / SRE)
#   → admitted to multiple tiers because their job is to probe across
#   the fence. RedTeam needs T1+T2 (probe both isolated and networked
#   sandboxes). Forensics gets all four (it must read crash dumps from
#   anywhere). SRE gets T0+T2 (deploy + monitoring are cloud-side, but
#   they need outbound to call PagerDuty / Slack / DNS).
#
# * **Custom** → defaults to the safest non-trivial set (T0 + T2).
#   Operator override via configs/sandbox_tier_policy.yaml is required
#   before granting it T1 or T3.
#
# Auditor specifically
# --------------------
# Auditor is **T0-only** by design — auditors observe the audit chain;
# they never execute payloads. A Guild → Tier request from auditor for
# T1/T2/T3 is a *policy bug*, not just a denial. KS.1 audit-chain
# integrity depends on this invariant.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Internal raw mapping. Public consumers MUST go through
# ``GUILD_TIER_ADMISSION_MATRIX`` (the read-only view) — keeping the
# raw dict ``_`` -prefixed keeps it out of ``from backend.sandbox_tier
# import *`` and out of casual mutation paths.
_RAW_GUILD_TIER_ADMISSION_MATRIX: dict[Guild, FrozenSet[SandboxTier]] = {
    # Cloud-brain Guilds
    Guild.architect: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.sa_sd: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.ux: frozenset({SandboxTier.T0}),
    Guild.pm: frozenset({SandboxTier.T0}),
    Guild.gateway: frozenset({SandboxTier.T0}),
    # Compile + simulate Guilds (need T1 sandbox + T3 hardware bridge)
    Guild.bsp: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.hal: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.algo_cv: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.isp: frozenset({SandboxTier.T1, SandboxTier.T3}),
    Guild.audio: frozenset({SandboxTier.T1, SandboxTier.T3}),
    # Networked-but-safe Guilds
    Guild.frontend: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.backend: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.optical: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.intel: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.qa: frozenset({SandboxTier.T0, SandboxTier.T2}),
    # Adversarial / observability Guilds
    Guild.red_team: frozenset({SandboxTier.T1, SandboxTier.T2}),
    Guild.forensics: frozenset(
        {SandboxTier.T0, SandboxTier.T1, SandboxTier.T2, SandboxTier.T3}
    ),
    Guild.sre: frozenset({SandboxTier.T0, SandboxTier.T2}),
    # Cloud-brain (review / report) — read-mostly Guilds
    Guild.reporter: frozenset({SandboxTier.T0, SandboxTier.T2}),
    Guild.auditor: frozenset({SandboxTier.T0}),
    # Operator escape hatch — defaults to safest networked set
    Guild.custom: frozenset({SandboxTier.T0, SandboxTier.T2}),
}

#: Read-only view over ``_RAW_GUILD_TIER_ADMISSION_MATRIX``.
#:
#: Use this in production code paths. The ``MappingProxyType`` wrapper
#: rejects ``__setitem__`` / ``__delitem__`` / ``update`` / ``pop`` so
#: callers can not silently widen the matrix at runtime — operator
#: overrides go through ``configs/sandbox_tier_policy.yaml`` (BP.S.2)
#: and are expected to *narrow*, not widen.
GUILD_TIER_ADMISSION_MATRIX: Mapping[Guild, FrozenSet[SandboxTier]] = (
    MappingProxyType(_RAW_GUILD_TIER_ADMISSION_MATRIX)
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class GuildTierViolation(ValueError):
    """Raised when a Guild → Tier dispatch is rejected by the matrix.

    Distinct exception class so PEP Gateway / dispatcher can catch
    ``GuildTierViolation`` specifically without swallowing unrelated
    ``ValueError``s. The string message names the offending Guild,
    the requested Tier, and the structurally-permitted set so the
    operator-facing audit log carries actionable info.
    """


def admitted_tiers(guild: Guild) -> FrozenSet[SandboxTier]:
    """Return the frozen set of Tiers the given Guild may dispatch into.

    Pure lookup; never raises (every ``Guild`` enum member has an
    entry — guarded by ``_assert_matrix_complete`` at import time).
    """
    return GUILD_TIER_ADMISSION_MATRIX[guild]


def is_admitted(guild: Guild, tier: SandboxTier) -> bool:
    """Whether ``guild`` is structurally allowed to run in ``tier``.

    PEP Gateway (BP.S.4) is the canonical caller — if this returns
    ``False``, refuse the dispatch and emit an audit event before any
    sandbox provisioning attempt.
    """
    return tier in GUILD_TIER_ADMISSION_MATRIX[guild]


def assert_admitted(guild: Guild, tier: SandboxTier) -> None:
    """Raise :class:`GuildTierViolation` if ``guild`` may not run in ``tier``.

    Convenience wrapper for callers who prefer fail-loud over
    ``if not is_admitted(...): ...`` boilerplate. The error message
    names the offending pair plus the structurally-permitted set so
    the audit log is actionable.
    """
    permitted = GUILD_TIER_ADMISSION_MATRIX[guild]
    if tier not in permitted:
        permitted_list = sorted(t.value for t in permitted)
        raise GuildTierViolation(
            f"Guild {guild.value!r} is not admitted to {tier.value!r}; "
            f"permitted tiers: {permitted_list}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Import-time invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _assert_matrix_complete() -> None:
    """Guarantee every ``Guild`` member has a matrix entry.

    Catches the "added a new Guild but forgot the admission rule" case
    at import time rather than letting a ``KeyError`` show up in some
    rare PEP code path months later. BP.S.6 will add a parameterised
    test ``test_every_guild_has_admission_entry`` that covers the same
    invariant from the test side as a defence-in-depth.
    """
    missing = [g for g in Guild if g not in _RAW_GUILD_TIER_ADMISSION_MATRIX]
    if missing:
        missing_names = sorted(g.value for g in missing)
        raise RuntimeError(
            "sandbox_tier admission matrix is incomplete; missing entries "
            f"for Guild members: {missing_names}. Update "
            "_RAW_GUILD_TIER_ADMISSION_MATRIX in backend/sandbox_tier.py."
        )


def _assert_tier_values_are_valid() -> None:
    """Guarantee every matrix value contains only real ``SandboxTier`` members.

    Belt-and-suspenders against typos like ``frozenset({"T0", "T2"})``
    (raw strings) sneaking past code review.
    """
    for guild, tiers in _RAW_GUILD_TIER_ADMISSION_MATRIX.items():
        for t in tiers:
            if not isinstance(t, SandboxTier):
                raise RuntimeError(
                    f"sandbox_tier matrix entry for Guild {guild.value!r} "
                    f"contains non-SandboxTier value: {t!r}"
                )


_assert_matrix_complete()
_assert_tier_values_are_valid()


__all__ = [
    "Guild",
    "GUILD_TIER_ADMISSION_MATRIX",
    "GuildTierViolation",
    "SandboxTier",
    "admitted_tiers",
    "assert_admitted",
    "is_admitted",
]
