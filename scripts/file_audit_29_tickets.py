#!/usr/bin/env python3
"""Batch-file AUDIT-29 META tree + 2 follow-up cleanup tickets.

Tickets created (28 total):
  - 1 top META (AUDIT-29 Pre-rc2 stabilization)
  - 11 sub-METAs (Phases 1-11, AUDIT-29a..29k)
  - 14 grandchildren under AUDIT-29f (Coordinator per ADR-0021 §13)
  - 2 follow-ups (LABEL-CLEANUP-1, TODO-BACKLOG-SWEEP) — independent of AUDIT-29

Every ticket uses 4-section AC discipline (AUDIT-23 anti-pattern protection):
  - Code AC      — what is written
  - Deploy AC    — what is running
  - Integration AC — wired into which broader flow
  - Exercised AC — observed in production

Plus Go-Live target (date or T+Nd from sub-META start).

fixVersion: 26 AUDIT-29 tickets bind to v0.5.0-rc2. 2 follow-ups have no fixVersion.

L-OP-870 trap: JIRA Blocks link direction is inwardIssue=blocker, outwardIssue=blocked.

Usage:
  scripts/file_audit_29_tickets.py --dry-run
  scripts/file_audit_29_tickets.py --execute
  scripts/file_audit_29_tickets.py --link-only

State file: ~/.cache/omnisight/audit-29-bootstrap-state.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from jira_label_validator import format_issues, validate
except ModuleNotFoundError:  # pragma: no cover - import path used by tests
    from scripts.jira_label_validator import format_issues, validate

CRED_DIR = Path("~/.config/omnisight").expanduser()
STATE_FILE = Path("~/.cache/omnisight/audit-29-bootstrap-state.json").expanduser()
USER_AGENT = "OmniSight-audit-29-bootstrap/2.0"
TARGET_FIX_VERSION = "v0.5.0-rc2"


# ──────────────────────────────────────────────────────────────────
#  TICKET SPEC
# ──────────────────────────────────────────────────────────────────


@dataclass
class TicketSpec:
    alias: str
    summary: str
    scope_text: str
    code_ac: list[str]
    deploy_ac: list[str]
    integration_ac: list[str]
    exercised_ac: list[str]
    go_live: str
    labels: list[str]
    parent_refs: list[str] = field(default_factory=list)  # human-readable lineage
    priority: str = "Medium"
    issue_type: str = "Story"
    fix_version: str | None = TARGET_FIX_VERSION
    extra_notes: str = ""


@dataclass
class LinkSpec:
    blocker_alias: str
    blocked_alias: str
    why: str = ""


def _ac(items: list[str]) -> str:
    if not items:
        return "_(N/A for this ticket — explain at closure if this stays empty)_"
    return "\n".join(f"- [ ] {line}" for line in items)


def build_description(spec: TicketSpec) -> str:
    parents = "\n".join(f"- {p}" for p in spec.parent_refs) if spec.parent_refs else "_(top-level)_"
    extra = f"\n## Notes\n\n{spec.extra_notes}\n" if spec.extra_notes else ""
    return f"""# {spec.summary}

## Lineage

{parents}

## Scope

{spec.scope_text}

## Acceptance criteria (4-section discipline — AUDIT-23 protection)

### 1. Code AC — 寫好了

{_ac(spec.code_ac)}

### 2. Deploy AC — 跑起來了 (on host / container / service)

{_ac(spec.deploy_ac)}

### 3. Integration AC — 串起來了 (wired into broader flow)

{_ac(spec.integration_ac)}

### 4. Exercised AC — 真的有人用 (observable in production)

{_ac(spec.exercised_ac)}

## Go-Live target

{spec.go_live}

## fixVersion

{spec.fix_version if spec.fix_version else "_(none — independent of release cycle)_"}
{extra}"""


# ─── Top META ─────────────────────────────────────────────────────


def _top_meta() -> TicketSpec:
    return TicketSpec(
        alias="META",
        summary="AUDIT-29 META: Pre-rc2 stabilization — 11-phase deployment-grade + 3D-memory plan",
        scope_text=(
            "Sets precondition state for v0.5.0-rc2 cut. Replaces the cancelled rc1 cut path "
            "(per AUDIT-26f Option I).\n\n"
            "**Source documents**:\n"
            "- `docs/audit/2026-05-12-audit-29-phase-0-state-audit.md` — Phase 0 state audit\n"
            "- `docs/adr/ADR-0021-release-pipeline-coordinator.md` — Phase 6 design (Accepted 2026-05-13)\n\n"
            "**11 phases**: 29a deployment audit → 29b Sprint F infra → 29c runner systemd → "
            "29d port-staging → 29e fix-failing-units → 29f Coordinator (ADR-0021) → "
            "29g pre-review self-fix → 29h auto-archive → 29i cold-start dry-run → "
            "29j rc2 bootstrap → 29k rc1 cleanup + retro."
        ),
        code_ac=[
            "All 11 sub-META tickets (29a..29k) → 公開済み or Archived",
            "ADR-0021 status: Accepted; coordinator runbook docs/operations/coordinator-runbook.md exists",
        ],
        deploy_ac=[
            "All Phase 2 (29b) infra containers + units verified live (Cognee + Neo4j + Graphiti)",
            "Coordinator daemon live under systemd; watchdog active",
            "All Phase 3 (29c) runner systemd units active; old tmux loops decommissioned",
            "Phase 4 (29d) port-based staging accessible; staging.sora.services subdomain deprecated",
        ],
        integration_ac=[
            "Runner _build_prompt observably injects Cognee-recalled lessons for >5 distinct tickets",
            "Coordinator decision log shows Tier-1 + Tier-2 + Personality + Capacity all firing within one decision",
            "Pre-review self-fix observed at least once on a real merge conflict",
            "Auto-archive observed transitioning at least one 公開済み ticket to Archived",
        ],
        exercised_ac=[
            "rc2 META + R1-R13 children flow without manual operator force-promote at R3 or R8",
            "AUDIT-26 META OP-979 → 公開済み (real, not labelled-but-not-actually)",
            "Sprint F META OP-898 → 公開済み (deployed, not just merged)",
            "docs/retrospectives/2026-XX-XX-audit-29-pre-rc2.md filed",
        ],
        go_live="2026-06-05 (T+22d from 2026-05-13 — rc2 cut + observation window)",
        labels=["agent:auto", "priority:meta", "type:meta", "meta:audit-29", "scope:pre-rc2", "adr:0021"],
        priority="High",
        extra_notes=(
            "Per operator anti-shortcut directive (2026-05-13): each phase must produce 4 deliverables — "
            "`.fix` + `.pattern` + `.audit` + `.verify`. Per-phase Go-Live is the contract that "
            "prevents AUDIT-23 'shipped but not deployed' regression."
        ),
    )


# ─── 11 sub-METAs ─────────────────────────────────────────────────


def _sub_metas() -> list[TicketSpec]:
    base_parents = ["AUDIT-29 META (Pre-rc2 stabilization)", "Source: docs/audit/2026-05-12-audit-29-phase-0-state-audit.md §6"]
    specs: list[TicketSpec] = []

    specs.append(TicketSpec(
        alias="29a",
        summary="AUDIT-29a (Phase 1): Deployment audit baseline",
        scope_text=(
            "Run `scripts/deployment-audit.sh`; close 4 fatal red rows; merge anti-pattern #13 "
            "(shipped-but-not-deployed); add CI check for area:devops activation step."
        ),
        code_ac=[
            "`scripts/deployment-audit.sh` runs clean (0 fatal red rows)",
            "`docs/sop/architecture-anti-patterns.md` has pattern #13 'shipped-but-not-deployed' merged",
            "CI lint rejects any area:devops ticket lacking an operator activation step",
            "L-OP-NNN lesson filed under docs/sop/lessons/ documenting the anti-pattern",
        ],
        deploy_ac=[
            "deployment-audit.sh installed on host at canonical location + executable",
            "CI lint rule deployed in pre-commit hooks AND server-side commit-msg hook",
            "Daily cron runs deployment-audit.sh; alerts operator on any red row",
        ],
        integration_ac=[
            "Lint hook actually triggers on a test PR with missing activation step (manual verification)",
            "deployment-audit.sh result feeds into AUDIT-29i cold-start verification step",
        ],
        exercised_ac=[
            ">=7 consecutive days of daily-audit cron producing 0 fatal reds",
            "At least 1 PR caught by the new CI lint check (positive test of guard)",
        ],
        go_live="T+1d from sub-META start",
        labels=["area:devops", "area:tests", "tier:M", "agent:auto", "priority:meta", "type:meta",
                "meta:audit-29-a", "scope:pre-rc2"],
        parent_refs=base_parents,
        priority="High",
    ))

    specs.append(TicketSpec(
        alias="29b",
        summary="AUDIT-29b (Phase 2): Sprint F infra deploy — Cognee + Neo4j + Graphiti + lesson-surface wire",
        scope_text=(
            "Deploy 3D memory infrastructure that was shipped (code) but not deployed (containers/units). "
            "Wire runner `_build_prompt` to actively query Cognee for relevant lessons + anti-patterns "
            "per ticket — this is the lesson-surface meta-mechanism that makes lessons load-bearing."
        ),
        code_ac=[
            "Cognee container image + Neo4j 5.24 systemd unit + on-host TLS config",
            "/var/omnisight/memory/ + per-fleet dirs provisioned (creation script idempotent)",
            "Graphiti MCP service + DNS/port + auth wired",
            "`backend/agents/agent_feature_flags.py` populated (currently empty)",
            "Runner _build_prompt: new prompt block 'Relevant lessons (Cognee)' + 'Active anti-patterns'",
            "Cognee ingestion job seeds initial 68 lessons + 12 anti-patterns",
        ],
        deploy_ac=[
            "Cognee container running (`docker ps | grep cognee` shows healthy)",
            "Neo4j 5.24 systemd unit active (`systemctl status` clean)",
            "Graphiti MCP service reachable from runner host (curl health endpoint OK)",
            "`/var/omnisight/memory/` mounted with correct permissions",
            "Cognee ingestion job ran at least once; data visible via API query",
        ],
        integration_ac=[
            "Runner _build_prompt invokes Cognee + Graphiti on next ticket pickup",
            "Anti-pattern auto-injected when ticket area matches pattern's domain",
            "Lesson recall observable in runner stdout (debug-mode log line per pickup)",
            "Pattern doc `docs/sop/3d-memory-architecture.md` references actual deployed endpoints",
        ],
        exercised_ac=[
            ">=10 ticket pickups with Cognee lesson-recall observed in prompt",
            ">=3 distinct lessons surfaced across the 10 pickups (not always same top-1)",
            "Anti-pattern #13 detected + flagged on at least 1 test ticket",
            "AUDIT-23 closing comment: 'Sprint F infrastructure is now actually deployed'",
        ],
        go_live="T+6d from sub-META start (long pole)",
        labels=["area:devops", "area:backend", "tier:L", "agent:auto", "priority:meta", "type:meta",
                "meta:audit-29-b", "scope:pre-rc2"],
        parent_refs=base_parents,
        priority="High",
        extra_notes="Long pole of AUDIT-29 critical path. Blocks 29c (runner wires after Cognee live), 29d, 29e, and 29f-6 / 29f-11 (Coordinator Tier-2 LLM + learning loop).",
    ))

    specs.append(TicketSpec(
        alias="29c",
        summary="AUDIT-29c (Phase 3): Runner systemd-ification",
        scope_text=(
            "Replace 4 tmux runner loops with systemd user units. Move /tmp/runner_wrappers/ to "
            "/home/user/sora-bridge/deploy/runner-wrappers/. Add daemon-startup-contract SOP."
        ),
        code_ac=[
            "4 unit files: runner-claude-bot@default.service, runner-claude-bot@worker2.service, runner-codex-bot@default.service, runner-codex-bot@worker2.service",
            "Each unit has Restart=, StandardOutput=, ExecStart= per contract",
            "Wrappers in `deploy/runner-wrappers/` (committed, not /tmp)",
            "`docs/sop/daemon-startup-contract.md` exists",
            "Pre-commit hook rejects new `.service` files missing the 3 required directives",
        ],
        deploy_ac=[
            "4 units installed under ~/.config/systemd/user/ + enabled",
            "linger=yes set for the runner user",
            "`systemctl --user status runner-*@*` clean for all 4",
            "Old tmux runner sessions killed; tmux config no longer auto-starts runners",
        ],
        integration_ac=[
            "Coordinator (29f-7) cold-start can `systemctl --user start runner-claude-bot@default.service` to revive a dead runner",
            "Runners emit JSONL to capacity-tracking dir (29f-4 dependency)",
        ],
        exercised_ac=[
            ">=72h continuous uptime across all 4 systemd-managed runners",
            "At least 1 forced restart (kill -9 or systemctl restart) observed clean re-start within 30s",
        ],
        go_live="T+0.75d after 29b complete",
        labels=["area:devops", "tier:M", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-c", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29d",
        summary="AUDIT-29d (Phase 4): Port-based staging (drop staging.sora.services subdomain)",
        scope_text=(
            "Remove host-based Caddy rule for staging.sora.services; switch to path-based on the existing "
            "host. Update smoke probes to localhost:port. Document portability pattern. Includes "
            "deferred linear-history audit from AUDIT-26a."
        ),
        code_ac=[
            "Caddy config: no host-based rule for staging.sora.services; /audit/* path-rule added",
            "Smoke probe defaults updated: OMNISIGHT_STAGING_URL=http://localhost:18080",
            "`docs/sop/cross-host-portability-staging.md` exists",
            "Linear-history-consumer audit complete; MERGE_ALWAYS impact documented in ADR-0020 follow-up",
        ],
        deploy_ac=[
            "Caddy reloaded with new config; old subdomain rule confirmed removed",
            "Smoke probe redeployed via systemd (omnisight-staging-gate-smoke.service)",
            "DNS record for staging.sora.services either removed or retired (operator action documented)",
        ],
        integration_ac=[
            "AUDIT-29i cold-start verification path includes staging port reachability check",
            "Smoke probe + canary probe + audit endpoint all reachable via localhost:port without subdomain dependency",
        ],
        exercised_ac=[
            ">=48h smoke probe green using port-based URL",
            "Canary probe (29f or AUDIT-21 phase) observable via port-based path",
        ],
        go_live="T+1d after 29b complete",
        labels=["area:devops", "area:docs", "tier:M", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-d", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29e",
        summary="AUDIT-29e (Phase 5): Fix remaining failing units",
        scope_text=(
            "Fix units that are failing for known reasons: staging-pg-snapshot (host psql dep), "
            "omnisight-staging-compose (wrong port), staging-sync, staging-gate-canary/smoke, "
            "gerrit-jira-bridge-watchdog, API token for /audit/verify."
        ),
        code_ac=[
            "`snapshot-restore.sh` uses `docker exec` (no host psql dependency)",
            "omnisight-staging-compose HEALTHZ_URL points at correct port (18080, not 19000)",
            "staging-sync unit fixed (was failing due to subdomain — auto-resolves via 29d)",
            "staging-gate-canary + staging-gate-smoke green after 29d",
            "gerrit-jira-bridge-watchdog diagnosed + fixed or retired with rationale",
            "Staging API token provisioned + stored under ~/.config/omnisight/staging-*.env",
        ],
        deploy_ac=[
            "All 6 listed units active + healthy via systemctl",
            "staging-pg-snapshot can actually restore from anonymized snapshot",
            "Staging API token usable for /audit/verify (verified via curl)",
        ],
        integration_ac=[
            "End-to-end: develop tip → staging-sync triggers → snapshot restore → /audit/verify confirms parity",
            "All staging-* units in deployment-audit.sh report green",
        ],
        exercised_ac=[
            ">=48h all units green",
            "At least 1 daily develop→staging sync completed without operator intervention",
        ],
        go_live="T+1.5d after 29d complete",
        labels=["area:devops", "area:backend", "tier:M", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-e", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f",
        summary="AUDIT-29f (Phase 6): Release Pipeline Coordinator (ADR-0021)",
        scope_text=(
            "Implement domain-specific supervisor agent per ADR-0021. Hybrid Python + LLM consultation. "
            "Multi-personality (3 modes for Phase 6). Dual-scope routing (ticket + sprint). "
            "Cold-start = canonical startup orchestrator. Bounded permission model.\n\n"
            "**14 children** (29f-1 .. 29f-14) — see ADR-0021 §13."
        ),
        code_ac=[
            "All 14 children (29f-1..29f-14) → 公開済み",
            "`backend/agents/pipeline_coordinator*.py` modules present + unit-tested",
            "`docs/operations/coordinator-runbook.md` exists",
        ],
        deploy_ac=[
            "pipeline-coordinator.service + watchdog systemd units active",
            "Heartbeat file freshness <2min",
            "Decision-log JSONL accumulating",
            "Coordinator runs under linger=yes; survives reboot",
        ],
        integration_ac=[
            "One observed decision shows Tier-1 → Tier-2 → Personality → Capacity all firing in one flow",
            "Coordinator observed restarting a dead runner during cold-start scenario",
            "Capacity tracking observed shifting at least one ticket from saturated lane to idle lane",
            "Sprint-level re-plan observed identifying at least one scope-drift or duplication",
        ],
        exercised_ac=[
            "7-day shadow mode complete (DRY_RUN=1) — decision log reviewed by operator",
            "Acting mode enabled; first 7 days produce >=0 operator-correction incidents (or all corrections converted to Tier-1 graduations)",
            ">=20 Tier-1 decisions + >=5 Tier-2 decisions + >=1 personality-mode-Rescue observation in log",
        ],
        go_live="T+15d after 29e complete (8d impl + 7d shadow)",
        labels=["area:backend", "area:devops", "area:docs", "tier:L", "agent:auto", "priority:meta", "type:meta",
                "meta:audit-29-f", "scope:pre-rc2", "adr:0021"],
        parent_refs=base_parents,
        priority="High",
    ))

    specs.append(TicketSpec(
        alias="29g",
        summary="AUDIT-29g (Phase 7): Pre-review conflict self-fix loop",
        scope_text=(
            "Runner detects mergeable=false on its own change before +1, attempts rebase + force-push "
            "(≤3 attempts). On exhaustion: file `pre-review-self-fix-exhausted` + @coordinator escalation."
        ),
        code_ac=[
            "Runner: pre-+1 mergeable check + auto-rebase + force-push (≤3 attempts)",
            "Exhaustion handler files `pre-review-self-fix-exhausted` ticket + @-mentions coordinator",
            "Pattern doc: `docs/sop/lessons/L-OP-NNN-pre-review-self-fix.md`",
            "Cross-codebase audit: no duplicate rebase-on-conflict logic in other agents",
            "Integration test: injected merge conflict → confirmed 3-attempt cap",
        ],
        deploy_ac=[
            "Runner code path deployed; all 4 runner instances picking up new logic",
            "Failure ticket template available + file-able by runner",
        ],
        integration_ac=[
            "When runner exhausts self-fix, coordinator (29f) picks up the escalation via needs-coordinator label",
            "Coordinator's rescue mode triggered by 3+ self-fix exhaustions on same ticket",
        ],
        exercised_ac=[
            "At least 1 real merge conflict resolved by self-fix observed in production",
            "At least 1 exhaustion observed → escalation flow exercised end-to-end",
        ],
        go_live="T+1.5d (parallel with 29f shadow window)",
        labels=["area:backend", "area:tests", "tier:M", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-g", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29h",
        summary="AUDIT-29h (Phase 8): Auto-archive 公開済み → Archived",
        scope_text=(
            "Bridge handler: on change_merged + ticket already 公開済み + age > retention_days "
            "(env, default 30) → transition Archived. Operator escape: `coord-keep-open` label blocks."
        ),
        code_ac=[
            "Bridge transition logic: 公開済み → Archived after retention window",
            "`OMNISIGHT_ARCHIVE_AGE_DAYS` env knob honored (default 30)",
            "Label `coord-keep-open` blocks auto-archive",
            "`docs/sop/release-lifecycle-states.md` documents 公開済み vs Archived semantics",
            "End-to-end test: To Do → 公開済み → Archived",
        ],
        deploy_ac=[
            "Bridge service deployed with new handler enabled",
            "Daily archive sweep runs (systemd timer or cron)",
        ],
        integration_ac=[
            "Coordinator (29f) decision-log records archive transitions for visibility",
            "Auto-archive does NOT trigger on tickets labelled coord-keep-open",
        ],
        exercised_ac=[
            ">=5 tickets observed transitioning 公開済み → Archived in production",
            "At least 1 coord-keep-open ticket confirmed un-archived after retention window",
        ],
        go_live="T+0.5d (parallel with 29f shadow window)",
        labels=["area:backend", "area:devops", "tier:S", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-h", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29i",
        summary="AUDIT-29i (Phase 9): Cold-start dry-run",
        scope_text=(
            "Stop all units cleanly; wait; start; verify all containers + timers + runners + coordinator "
            "come up clean. This is the `.verify` deliverable for the entire AUDIT-29 effort."
        ),
        code_ac=[
            "`scripts/cold-start-dry-run.sh` exists + executable",
            "Script captures recovery time per service to a report file",
            "`docs/operations/deployment-inventory.md` updated with current inventory",
        ],
        deploy_ac=[
            "Cold-start scripted run completed without operator intervention",
            "All units + containers up within 5min of cold-start",
            "`scripts/deployment-audit.sh` reports 0 fatal red after cold-start",
        ],
        integration_ac=[
            "Coordinator (29f) startup recovery (Startup-1..4) observed working: starts down units, reconciles JIRA, sweeps stale, enters normal loop",
            "Pre-review self-fix (29g) + auto-archive (29h) wiring intact after cold-start",
        ],
        exercised_ac=[
            "Cold-start exercised at least once on real host; documented duration <5min",
            "Follow-up tickets filed if any service takes >2min to recover",
        ],
        go_live="T+0.5d after 29f/g/h complete",
        labels=["area:devops", "area:tests", "tier:M", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-i", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29j",
        summary="AUDIT-29j (Phase 10): rc2 bootstrap",
        scope_text=(
            "Create v0.5.0-rc2 fixVersion; bootstrap RELEASE-v0.5.0-rc2 META + R1-R13 children via "
            "release_conductor_cron; verify chain runs end-to-end with REAL green gates (no force-promote)."
        ),
        code_ac=[
            "JIRA fixVersion v0.5.0-rc2 created (idempotent if exists)",
            "RELEASE-v0.5.0-rc2 META + R1-R13 children filed via release_conductor_cron",
            "All R-children correctly labeled (sprint:release, release:v0.5.0-rc2 or fixVersion-only)",
        ],
        deploy_ac=[
            "release_conductor_cron timer active + observed firing",
            "R3 fires with real green canary + smoke from 29b/29d/29e + Coordinator monitoring",
        ],
        integration_ac=[
            "Chain progresses without operator intervention through at least R5 (develop → staging promotion)",
            "Coordinator (29f) observable during R-progress: not intervening, just witnessing (or escalating if it should)",
        ],
        exercised_ac=[
            "R1 → R5 progression observed without operator force-promote",
            "Reaching R8 (canary stable) within target time window",
        ],
        go_live="T+1h after 29i complete (real-time bootstrap, slow burn after)",
        labels=["area:devops", "area:backend", "tier:S", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-j", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29k",
        summary="AUDIT-29k (Phase 11): rc1 cleanup + AUDIT-26 META close + retrospective",
        scope_text=(
            "OP-922 → Won't Do; R1-R13 → Archived per AUDIT-26f Option I; close AUDIT-26 META OP-979 + "
            "Sprint F META OP-898 + AUDIT-29 META. Operator retrospective + lesson capture."
        ),
        code_ac=[
            "`docs/retrospectives/2026-XX-XX-audit-29-pre-rc2.md` filed",
            "Lessons captured including coordinator shadow-mode learnings + AUDIT-23 pattern reinforcement",
            "L-OP-NNN lesson: 'Pre-rc cut audit + meta-system buildout pattern' (the AUDIT-29 lesson itself)",
        ],
        deploy_ac=[
            "OP-922 → Won't Do; rc1 R1-R13 → Archived",
            "AUDIT-26 META OP-979 → 公開済み",
            "Sprint F META OP-898 → 公開済み",
            "AUDIT-29 META → 公開済み",
        ],
        integration_ac=[
            "Cross-ticket consistency: no orphan rc1 references after cleanup",
            "All retrospective action items filed as new tickets (or closed)",
        ],
        exercised_ac=[
            "Retrospective held with operator; lessons committed to docs/sop/lessons/",
            "Lessons surfaced (via 29b Cognee) to subsequent agent work — confirmed by observable recall",
        ],
        go_live="T+0.5d after 29j R3 confirmed green",
        labels=["area:devops", "area:docs", "tier:M", "agent:auto", "priority:meta", "type:meta", "meta:audit-29-k", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    return specs


# ─── 14 grandchildren under 29f ──────────────────────────────────


def _f_children() -> list[TicketSpec]:
    base_parents = [
        "AUDIT-29 META (Pre-rc2 stabilization)",
        "AUDIT-29f (Phase 6 Coordinator sub-META)",
        "ADR: docs/adr/ADR-0021-release-pipeline-coordinator.md §13",
    ]
    specs: list[TicketSpec] = []

    specs.append(TicketSpec(
        alias="29f-1",
        summary="AUDIT-29f-1: ADR-0021 review + lock",
        scope_text="Operator's pass through ADR-0021 §1-§14 + 3 appendices. Update status to Accepted.",
        code_ac=[
            "ADR-0021 status: Accepted (operator +2)",
            "Any +2-time amendments captured in a separate commit on top of original ADR",
        ],
        deploy_ac=[
            "ADR-0021 merged to develop branch (via Gerrit or direct merge per repo policy)",
        ],
        integration_ac=[
            "All 14 sibling tickets (29f-2..29f-14) reference correct ADR section numbers (§ marks valid)",
        ],
        exercised_ac=[
            "Subsequent design conversations on the coordinator cite ADR-0021 instead of re-deriving",
        ],
        go_live="2026-05-13 (today, on filing of these tickets)",
        labels=["area:docs", "tier:S", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-2",
        summary="AUDIT-29f-2: pipeline_coordinator skeleton + daemon + systemd + watchdog",
        scope_text=(
            "Module skeleton + systemd user unit + watchdog. Heartbeat + SIGTERM drain. "
            "Empty decision engine that just logs."
        ),
        code_ac=[
            "Module structure: `backend/agents/pipeline_coordinator.py` + `pipeline_coordinator_modes.py` + `pipeline_coordinator_rules.py` + `pipeline_coordinator_capacity.py`",
            "`deploy/systemd/pipeline-coordinator.service` + `pipeline-coordinator-watchdog.service`",
            "SIGTERM handler drains in-flight work + writes shutdown markers",
            "Heartbeat file written every 60s",
            "Tests: `backend/tests/test_pipeline_coordinator_skeleton.py` — daemon-up, heartbeat-fresh, drain-on-SIGTERM",
        ],
        deploy_ac=[
            "Both systemd units installed under ~/.config/systemd/user/ + enabled",
            "linger=yes set; daemon survives logout",
            "`systemctl --user status pipeline-coordinator` clean",
            "Heartbeat file freshness <2min observed",
        ],
        integration_ac=[
            "Watchdog restarts daemon if heartbeat absent >90s (chaos-test)",
            "Decision-log directory writeable + JSONL append-only confirmed",
        ],
        exercised_ac=[
            ">=24h daemon uptime",
            ">=1 watchdog-driven restart observed (forced via kill -9 in test)",
        ],
        go_live="T+0.5d after 29f-1",
        labels=["area:backend", "area:devops", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-3",
        summary="AUDIT-29f-3: Tier 1 deterministic rules (initial 10)",
        scope_text="Implement 10 rules from ADR §5.1: dependency-out-of-area, stale-claim-cleanup, runner-blocked-marker-stale, merger-repeat-fail, revert-loop-quarantine, capability-blocked-known-locale, stuck-in-progress, chain-deadlock, wrong-class-routing, operator-keep-out.",
        code_ac=[
            "10 rule functions in `pipeline_coordinator_rules.py` with @rule decorator",
            "Each rule unit-tested (positive + negative cases)",
            "Rule priorities documented inline",
            "`DecisionContext` dataclass + `Action` union type defined",
        ],
        deploy_ac=[
            "Coordinator daemon (from 29f-2) restarted with new rules loaded",
            "Rule registry loaded at startup (visible in startup log line)",
        ],
        integration_ac=[
            "Rules wired into daemon's main tick loop — each tick iterates over registered rules",
            "When rule returns Action, action layer executes it (mock if action layer not yet built)",
            "Integration test: synthetic ticket triggers known rule → action recorded in decision log",
        ],
        exercised_ac=[
            "In shadow mode (29f-14): >=5 distinct rules observed firing in decision log over 7 days",
            "No rule observed mis-firing (false positive corrections from operator <3 in 7d)",
        ],
        go_live="T+1d after 29f-2",
        labels=["area:backend", "area:tests", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-4",
        summary="AUDIT-29f-4: Runner capacity tracking",
        scope_text="Runners emit per-tick quota state; coordinator tails + maintains capacity view.",
        code_ac=[
            "Runner: per-tick JSONL emit (tokens_in_current_week, weekly_cap, reset_at, tickets_completed)",
            "Coordinator tails files in `~/.cache/omnisight/runner-quota-state/`",
            "`runner_capacity.json` updated every coordinator tick",
            "Test: synthetic runner emit → coordinator picks up → JSON reflects state",
        ],
        deploy_ac=[
            "**All 4 existing runners modified + restarted with new emit code**",
            "Runner-quota-state directory created with correct permissions",
            "Coordinator capacity-tracking thread active (visible in startup log)",
        ],
        integration_ac=[
            "Sprint-level re-plan (29f-9) consumes `runner_capacity.json`",
            "Tier-1 rule `wrong-class-routing` can read capacity to make routing suggestions",
        ],
        exercised_ac=[
            "All 4 runners observed emitting in production over >=72h",
            "`runner_capacity.json` shows reasonable values (matches operator-known runner usage)",
            "At least 1 capacity-driven routing suggestion observed in decision log",
        ],
        go_live="T+0.5d after 29f-2",
        labels=["area:backend", "area:devops", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
        extra_notes="**Critical wiring step**: this ticket requires MODIFYING the 4 existing runner instances, not just writing new code. Verify all 4 are restarted with new emit logic before closing.",
    ))

    specs.append(TicketSpec(
        alias="29f-5",
        summary="AUDIT-29f-5: Personality mode system (Execution + Investigation + Rescue)",
        scope_text="SituationProfile (urgency × risk × novelty × reversibility) → ModeSelector → 3 modes.",
        code_ac=[
            "`SituationProfile` dataclass with 4 axes",
            "`ModeSelector` function: profile → ModeName",
            "3 mode classes (ExecutionMode / InvestigationMode / RescueMode) with behavior overrides",
            "Operator override: `coord-mode:<mode>` label forces mode for that ticket",
            "Test: each (profile combo, expected mode) pair verified",
        ],
        deploy_ac=[
            "Coordinator daemon restarted with personality system loaded",
        ],
        integration_ac=[
            "Decision engine calls ModeSelector before rule evaluation; mode flag visible in decision-log entries",
            "Tier-1 rules + Tier-2 LLM consultation see + respect the selected mode",
        ],
        exercised_ac=[
            ">=1 observation each of ExecutionMode, InvestigationMode, RescueMode firing in shadow log",
            "At least 1 operator override via coord-mode:* label observed working",
        ],
        go_live="T+0.5d after 29f-2",
        labels=["area:backend", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-6",
        summary="AUDIT-29f-6: Tier 2 LLM consultation + budget guards",
        scope_text="Context-bundle assembly + claude CLI invocation + JSON output parse + budget caps. **Depends on 29b** for Cognee lesson recall.",
        code_ac=[
            "`llm_consultation.py` module under pipeline_coordinator",
            "Context bundle: 7 sections per ADR §5.2",
            "claude CLI invocation via subprocess with timeout",
            "JSON response strict-parsed; invalid → escalate operator",
            "Action validation: only ADR §6.1 allowed actions executed",
            "Daily budget cap (env `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD`)",
            "On budget hit: coordinator degrades to Tier-1 only + operator alert",
        ],
        deploy_ac=[
            "Daemon restarted with Tier-2 path active",
            "claude CLI accessible from daemon's environment (correct PATH + creds)",
            "Cognee endpoint reachable from daemon (29b deployed)",
        ],
        integration_ac=[
            "Tier-2 triggered when Tier-1 rules all return None or conflict",
            "Tier-2 output (Action) executed via same action layer as Tier-1",
            "Decision log entries distinguish tier=1 vs tier=2",
            "Cognee lesson recall observable in Tier-2 context bundle (logged)",
        ],
        exercised_ac=[
            ">=5 Tier-2 consultations observed in shadow mode 7-day window",
            "At least 1 budget cap event simulated; coordinator degrades correctly",
            "Cost telemetry: actual $/decision matches estimate within 50%",
        ],
        go_live="T+1d after 29f-2 + 29b complete",
        labels=["area:backend", "area:tests", "tier:L", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-7",
        summary="AUDIT-29f-7: Cold-start 4-phase recovery",
        scope_text="Startup-1 (infra verify) + Startup-2 (JIRA reconcile) + Startup-3 (stale sweep) + Startup-4 (enter loop). Plus L6 crash-recovery from decision-log replay. **Depends on 29a + 29c**.",
        code_ac=[
            "4-phase recovery in `pipeline_coordinator.startup()`",
            "Startup-1: invokes `scripts/deployment-audit.sh` + systemctl --user start <unit> for any down",
            "Startup-2: queries 進行中 + reconciles per ADR §3.3 decision tree",
            "Startup-3: removes stale claim:* + bridges-old runner-blocked:waiting-* markers",
            "Startup-4: enters normal loop",
            "L6: replays last 24h decision log; identifies + resumes/completes/rolls back",
            "Integration test: kill -9 mid-decision → clean recovery on next start",
        ],
        deploy_ac=[
            "Daemon's startup() actually runs the 4-phase recovery on real boot",
            "Decision-log directory created + writeable for L6 replay",
        ],
        integration_ac=[
            "Cold-start (29i) verification calls `systemctl --user start pipeline-coordinator` and observes successful startup() within 30s",
            "Coordinator can call `systemctl --user start runner-claude-bot@default.service` to revive runners (29c integration)",
            "Coordinator reads deployment-audit.sh output to know which units to start (29a integration)",
        ],
        exercised_ac=[
            ">=1 real cold-start observed (kill all units, then restart)",
            "Daemon's startup() reconciliation observed handling at least 3 different interrupted-ticket scenarios",
            ">=1 L6 crash recovery observed (forced kill -9)",
        ],
        go_live="T+0.75d after 29f-2 + 29a + 29c complete",
        labels=["area:backend", "area:devops", "tier:L", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-8",
        summary="AUDIT-29f-8: Event sources — bridge tap + JIRA poll + subscription wiring",
        scope_text="Tail bridge-events.jsonl. JIRA poll every 60s. Hourly proactive sweep.",
        code_ac=[
            "Bridge tap: tail JSONL + reactive on each line",
            "**Bridge service modified to emit events to `~/.config/omnisight/coordinator/bridge-events.jsonl`** (this is new bridge code, not assumed)",
            "JIRA poll: 60s tick with JQL per ADR §4",
            "Hourly sweep schedule (full work-graph anomaly scan)",
            "Event deduplication: same event within 5min → coalesce",
            "Test: synthetic bridge event → coordinator processes within 1s",
        ],
        deploy_ac=[
            "Bridge service restarted with new emit code",
            "Coordinator daemon restarted with event subscriber active",
            "Both `bridge-events.jsonl` writer (bridge) + reader (coordinator) verified live",
        ],
        integration_ac=[
            "Coordinator receives events from bridge within 1s of bridge emitting",
            "Coordinator's JIRA poll observes `needs-coordinator` labelled tickets within 60s",
            "Hourly sweep observed firing (timer entry in decision log)",
        ],
        exercised_ac=[
            ">=10 bridge events processed in shadow mode 7-day window",
            ">=5 JIRA-poll cycles fired without error",
            ">=24 hourly sweeps observed (no missed firings)",
        ],
        go_live="T+0.5d after 29f-2",
        labels=["area:backend", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
        extra_notes="**Critical wiring step**: requires modifying gerrit_jira_bridge to emit JSONL — not just consuming. Both halves needed before close.",
    ))

    specs.append(TicketSpec(
        alias="29f-9",
        summary="AUDIT-29f-9: Sprint-level periodic re-plan",
        scope_text="Hourly: enumerate next pickable tickets + read capacity + LLM-consult plan + apply. **Depends on 29f-4 + 29f-6.**",
        code_ac=[
            "`sprint_replan.py` module with hourly handler",
            "Enumerates pickable tickets (priority DESC, age ASC) up to N=20",
            "LLM consult in Investigation Mode for assignment",
            "Applies relabel + files scope-review tickets",
            "Reports capacity insufficiency via @-mention",
            "Test: synthetic backlog + capacities → expected assignment",
        ],
        deploy_ac=[
            "Hourly cron / daemon-internal timer scheduled + observed firing",
            "Coordinator daemon restarted with re-plan handler registered",
        ],
        integration_ac=[
            "Re-plan reads `runner_capacity.json` (29f-4)",
            "Re-plan invokes Tier-2 LLM consult (29f-6) in Investigation Mode",
            "Re-plan's relabel actions go through action layer's validation",
        ],
        exercised_ac=[
            ">=7 hourly re-plans observed in shadow 7-day window",
            "At least 1 capacity-insufficiency alert observed (or absence justified by ample capacity)",
            "At least 1 scope-review ticket filed by re-plan",
        ],
        go_live="T+0.5d after 29f-4 + 29f-6 complete",
        labels=["area:backend", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-10",
        summary="AUDIT-29f-10: Chaos test + decision-log validation",
        scope_text="CI test: kill -9 random + verify clean recovery. Decision-log schema validator.",
        code_ac=[
            "`backend/tests/test_pipeline_coordinator_chaos.py` runs N=50 random kills",
            "Each kill followed by clean restart verification",
            "Assertion: zero orphan claim:* labels, zero double-relabels, all decisions in log",
            "Decision-log JSONL schema validator (`scripts/validate-decision-log.py`)",
            "Schema validation in CI on every coordinator commit",
        ],
        deploy_ac=[
            "Chaos test added to CI pipeline (e.g., `make test-chaos` target)",
            "Schema validator wired into pre-commit hook or CI step",
        ],
        integration_ac=[
            "CI fails any PR that breaks decision-log schema",
            "CI fails any PR where chaos test reports orphan state",
        ],
        exercised_ac=[
            "Chaos test runs in CI on at least 5 PRs without false positives",
            "Schema validator caught at least 1 real malformed entry (or absence justified)",
        ],
        go_live="T+0.5d after 29f-9",
        labels=["area:tests", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-11",
        summary="AUDIT-29f-11: Learning loop — write-back + outcome-check + Tier-1 graduation",
        scope_text="Decision → Cognee node + 24h outcome check + weekly retrospective + rule proposals. **Depends on 29b**.",
        code_ac=[
            "`learning_loop.py` module with daily + weekly handlers",
            "Decision write-back creates Cognee node with edges (after 29b deployed)",
            "24h outcome-check: did action produce expected result?",
            "Weekly self-retro: groups by (profile_class, action_type)",
            "Tier-1 rule proposals written to `coord_rule_proposals/*.py`",
            "Operator @-mention with proposal review request",
            "Test: synthetic decisions → graduation pipeline produces valid Python",
        ],
        deploy_ac=[
            "Daemon restarted with learning-loop handler active",
            "Cognee write-back endpoint verified (29b live)",
            "Daily + weekly schedules registered + firing",
        ],
        integration_ac=[
            "Every Tier-2 decision triggers write-back within 60s",
            "Outcome-check correlates decision → outcome via JIRA state change observation",
            "Proposal generator output passes syntax + lint check",
        ],
        exercised_ac=[
            ">=5 Tier-2 decisions written back to Cognee in shadow 7-day window",
            ">=1 outcome-check completed (24h after a decision)",
            ">=1 rule-graduation proposal generated (operator reviews + merges or rejects)",
        ],
        go_live="T+1d after 29f-6 + 29b complete",
        labels=["area:backend", "tier:L", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-12",
        summary="AUDIT-29f-12: Integration test — full pipeline scenario",
        scope_text="End-to-end test: runner stuck → coordinator handles → ticket flows through. Multi-scenario.",
        code_ac=[
            "`backend/tests/test_pipeline_coordinator_integration.py`",
            "Scenario A: runner can't push (no gerrit_push cap) → coordinator detects → applies capability:enable OR reroutes",
            "Scenario B: merger fails 3x → coordinator escalates to operator with full context",
            "Scenario C: stale claim from dead runner → coordinator cleans up + ticket re-pickable",
            "All scenarios run in CI",
        ],
        deploy_ac=[
            "Test infrastructure deployable (JIRA test project or mocked endpoint)",
            "CI integration: scenarios run on every coordinator commit + nightly",
        ],
        integration_ac=[
            "Test exercises Tier-1 + Tier-2 + Personality + Capacity + Event subscriber + Action layer in one flow",
            "Test verifies decision-log entries match expected sequence",
        ],
        exercised_ac=[
            "Tests run in CI for >=14 consecutive days without flakes",
            "At least 1 real production scenario observed matching test pattern A/B/C",
        ],
        go_live="T+0.5d after 29f-2..29f-11 substantially complete",
        labels=["area:tests", "tier:M", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-13",
        summary="AUDIT-29f-13: Operator runbook",
        scope_text="`docs/operations/coordinator-runbook.md` — monitoring, override, pause, decision-log inspection, troubleshooting.",
        code_ac=[
            "`docs/operations/coordinator-runbook.md` exists",
            "Sections: monitoring, override, pause, decision-log inspection, troubleshooting",
            "All env knobs documented with default values",
            "All operator-override labels documented (coord-skip, coord-mode:*, coord-keep-open, coord-resume-after:*)",
            "Common failure scenarios + recovery steps",
        ],
        deploy_ac=[
            "Runbook checked into develop branch + reachable from docs index",
            "Quick-reference card (Appendix B from ADR-0021) lifted to standalone section in runbook",
        ],
        integration_ac=[
            "Runbook references actual systemd unit names + env file paths used by 29f-2 deployment",
            "All labels in runbook match what coordinator code actually checks",
        ],
        exercised_ac=[
            "Operator successfully used runbook to pause + inspect coordinator at least once",
            "If shadow-mode review (29f-14) generated questions, runbook updated to address them",
        ],
        go_live="T+0.5d (parallel with main features)",
        labels=["area:docs", "tier:S", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    specs.append(TicketSpec(
        alias="29f-14",
        summary="AUDIT-29f-14: Deploy + 7-day shadow mode → enable acting mode",
        scope_text="Deploy coordinator with DRY_RUN=1 (shadow — log decisions only). Watch 7 days. Operator reviews. If pass → unset env → restart → acting mode.",
        code_ac=[
            "Deployment script `scripts/deploy-coordinator.sh` exists + idempotent",
            "Shadow-mode review checklist documented",
            "Acting-mode enablement procedure documented",
        ],
        deploy_ac=[
            "Coordinator deployed with `OMNISIGHT_COORDINATOR_DRY_RUN=1` set in systemd unit env",
            "Decision log accumulating without action execution",
            "All 14 prior features observable in shadow log",
        ],
        integration_ac=[
            "After 7-day shadow + operator pass: env unset + daemon restart → first 24h of acting reviewed",
            "Any operator-correction during first 24h captured as lesson",
        ],
        exercised_ac=[
            "7 consecutive days of shadow-mode decision log accumulated",
            "Operator reviewed log + signed off",
            "Acting mode enabled; first week reviewed at end of week 1",
            ">=1 Tier-1 rule graduated from learning-loop proposals during week 1 of acting",
        ],
        go_live="T+8d after 29f-12 + 29f-13 + all preceding (=T+22d from 29f-1)",
        labels=["area:devops", "tier:L", "agent:auto", "phase:audit-29f", "adr:0021", "scope:pre-rc2"],
        parent_refs=base_parents,
    ))

    return specs


# ─── 2 follow-up cleanup tickets (independent of AUDIT-29) ────────


def _followups() -> list[TicketSpec]:
    return [
        TicketSpec(
            alias="FOLLOWUP-LABEL-CLEANUP",
            summary="OP housekeeping: JIRA label cleanup — migrate legacy formats + retire stale",
            scope_text=(
                "Auditing 191 labels across 982 OP tickets surfaced ~86 stale + ~30 legacy-format labels. "
                "Migrate legacy formats to current canonical, retire stale, document the conventions. "
                "Independent of AUDIT-29 release cycle."
            ),
            code_ac=[
                "Migration script `scripts/jira-label-migrate.py` exists with --dry-run / --execute",
                "Mappings codified:",
                "  - `tier-m`/`tier-l`/`tier-s`/`tier-x` → `tier:M/L/S/X` (hyphen → colon)",
                "  - `agent-class:*` → `class:*`",
                "  - `complexity:*` → `tier:*` (where mappable)",
                "  - `priority:high/medium/critical/low` → JIRA Priority field (Highest/High/Medium/Low/Lowest)",
                "  - `release:v0.5.0-rc1` → JIRA fixVersion",
                "  - `parent:OP-*` → JIRA issuelinks (Parent of / Blocks)",
                "  - `blocked-by:*` / `blocks:*` → JIRA issuelinks",
                "Retire: 11 priority:*-track labels (hd/mp/rpg/cl/l4/l5/bp/he/fx2/wp), `refined-by:claude-direct`, 10 `runner-blocked:waiting-OP-92*` (rc1 chain), per-ticket one-offs",
                "`docs/sop/jira-label-conventions.md` documents current canonical labels + when to use each",
            ],
            deploy_ac=[
                "Migration script run end-to-end on production JIRA (operator-supervised)",
                "Label conventions doc merged + reachable from docs index",
                "Lint hook validates new tickets use canonical labels",
            ],
            integration_ac=[
                "Runner pickup JQL still works after migration (no regression)",
                "Coordinator (29f, when live) reads canonical labels only",
            ],
            exercised_ac=[
                "After migration: label count down from 191 → <100",
                "Zero new tickets created with retired labels in following 7 days",
            ],
            go_live="Independent — no specific deadline; complete within 1 week of acceptance",
            labels=["area:tooling", "area:devops", "tier:S", "agent:auto", "type:cleanup",
                    "scope:label-hygiene"],
            parent_refs=["Independent cleanup ticket — not in AUDIT-29 chain"],
            fix_version=None,
            extra_notes=(
                "Source: AUDIT-29 batch dry-run uncovered label sprawl. 191 unique labels across 982 tickets; "
                "27 load-bearing (>50 uses), 118 one-offs (<=5), 86 with 0 open tickets. See "
                "docs/audit/2026-05-12-audit-29-phase-0-state-audit.md and AUDIT-29 batch ticket conversation."
            ),
        ),
        TicketSpec(
            alias="FOLLOWUP-TODO-BACKLOG-SWEEP",
            summary="OP housekeeping: 1100+ migrated-from-todo backlog sweep — triage stale dump",
            scope_text=(
                "Three legacy labels (`runner-needs-refinement` 493 open, `migrated-from-todo-bulk` 435 open, "
                "`migrated-from-todo` 97 open) collectively hold ~1100 tickets that runners can't pick up "
                "and never get refined. They block visibility into the real backlog. "
                "Triage: (a) still relevant → refine; (b) duplicate → close; (c) abandoned → Won't Do."
            ),
            code_ac=[
                "Triage script `scripts/jira-todo-backlog-triage.py` exists with classification heuristic",
                "Classification rules documented: e.g., 'older than 30d + no comments + no fixVersion → likely abandoned'",
                "Bulk-action script can apply Won't Do transition + Won't Do reason field",
                "L-OP-NNN lesson: 'Bulk import without refinement creates dead inventory' — anti-pattern",
            ],
            deploy_ac=[
                "Triage script run end-to-end (operator-supervised)",
                "Classification report filed at `docs/audit/2026-XX-XX-todo-backlog-triage.md`",
                "Lessons doc merged",
            ],
            integration_ac=[
                "Coordinator (when live) reads refined backlog without label-noise interference",
                "Sprint planning queries see real prioritized backlog, not 1100+ noise items",
            ],
            exercised_ac=[
                "Backlog count reduced: <200 'migrated-from-todo*' tickets remaining (>900 closed/refined)",
                ">=20 refined tickets actually picked up by runners post-sweep (validates the survivors)",
                "Operator can articulate real backlog size after sweep (vs 'I have no idea' before)",
            ],
            go_live="Independent — target 1 week of effort, no hard deadline",
            labels=["area:tooling", "area:devops", "area:docs", "tier:L", "agent:auto",
                    "type:cleanup", "scope:backlog-hygiene"],
            parent_refs=["Independent cleanup ticket — not in AUDIT-29 chain"],
            priority="High",
            fix_version=None,
            extra_notes=(
                "Source: AUDIT-29 batch dry-run label probe. P0 because the 1100+ tickets are invisible to "
                "the runner JQL (lacking proper area/tier/class labels) but show up in every JIRA-wide query, "
                "confusing capacity planning + sprint review."
            ),
        ),
    ]


def all_specs() -> list[TicketSpec]:
    return [_top_meta()] + _sub_metas() + _f_children() + _followups()


# ─── Link spec ─────────────────────────────────────────────────────


def all_links() -> list[LinkSpec]:
    links: list[LinkSpec] = []
    # All 11 sub-METAs block top META
    for alias in ("29a", "29b", "29c", "29d", "29e", "29f", "29g", "29h", "29i", "29j", "29k"):
        links.append(LinkSpec(alias, "META", "sub-META blocks top META"))
    # 29f children block 29f sub-META
    for i in range(1, 15):
        links.append(LinkSpec(f"29f-{i}", "29f", "child blocks sub-META"))
    # Internal sequencing within 29f children (per ADR §13)
    links.append(LinkSpec("29f-1", "29f-2", "ADR must lock before skeleton"))
    for n in (3, 4, 5, 6, 7, 8, 13):
        links.append(LinkSpec("29f-2", f"29f-{n}", f"29f-{n} builds on skeleton"))
    for n in (3, 4, 5, 6, 7, 8):
        links.append(LinkSpec(f"29f-{n}", "29f-9", "sprint re-plan needs core engine + capacity + LLM"))
    links.append(LinkSpec("29f-9", "29f-10", "chaos test after re-plan + main engine"))
    links.append(LinkSpec("29f-6", "29f-11", "learning loop needs Tier-2 LLM consult"))
    for n in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        links.append(LinkSpec(f"29f-{n}", "29f-12", "integration test exercises all features"))
    links.append(LinkSpec("29f-12", "29f-14", "deploy needs integration test passing"))
    links.append(LinkSpec("29f-13", "29f-14", "deploy needs runbook"))
    # Cross-phase deps per ADR §13.1
    links.append(LinkSpec("29b", "29f-6", "Tier-2 LLM consult needs Cognee"))
    links.append(LinkSpec("29b", "29f-11", "learning loop needs Cognee"))
    links.append(LinkSpec("29a", "29f-7", "cold-start calls scripts/deployment-audit.sh"))
    links.append(LinkSpec("29c", "29f-7", "cold-start assumes systemd-managed runners"))
    # 29f + 29g + 29h all block 29i
    for alias in ("29f", "29g", "29h"):
        links.append(LinkSpec(alias, "29i", "cold-start dry-run validates this phase"))
    links.append(LinkSpec("29i", "29j", "rc2 bootstrap only after cold-start green"))
    links.append(LinkSpec("29j", "29k", "rc1 cleanup after rc2 chain demonstrated"))
    # Other inter-phase blockers
    for alias in ("29c", "29d", "29e"):
        links.append(LinkSpec("29a", alias, "deployment audit informs follow-on phases"))
    links.append(LinkSpec("29b", "29c", "runner systemd-ify after Cognee live (prompts wire)"))
    for alias in ("29d", "29e"):
        links.append(LinkSpec("29b", alias, "Sprint F infra propagates to staging/Caddy"))
    # NOTE: 2 follow-ups (LABEL-CLEANUP + TODO-BACKLOG-SWEEP) are NOT linked to AUDIT-29
    # They are independent housekeeping work.
    return links


# ──────────────────────────────────────────────────────────────────
#  JIRA API
# ──────────────────────────────────────────────────────────────────


def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _jira_config() -> tuple[str, str, str]:
    env = _load_env(CRED_DIR / "jira-claude.env")
    token = (CRED_DIR / "jira-claude-token").read_text().strip()
    auth = "Basic " + b64encode(f"{env['OMNISIGHT_JIRA_CLAUDE_EMAIL']}:{token}".encode()).decode()
    return env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/"), env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP"), auth


def _request(method: str, url: str, auth: str, body: dict[str, Any] | None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth, "Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"{method} {url} -> {exc.code}: {text}") from exc


def _adf(md: str) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": [
        {"type": "codeBlock", "attrs": {"language": "markdown"},
         "content": [{"type": "text", "text": md}]}
    ]}


def ensure_fix_version(site: str, project: str, auth: str, name: str) -> str:
    """Idempotent: returns version ID. Creates if absent."""
    resp = _request("GET", f"{site}/rest/api/3/project/{project}/versions", auth, None)
    if isinstance(resp, list):
        for v in resp:
            if v.get("name") == name:
                return v["id"]
    project_resp = _request("GET", f"{site}/rest/api/3/project/{project}", auth, None)
    body = {"name": name, "projectId": project_resp["id"]}
    created = _request("POST", f"{site}/rest/api/3/version", auth, body)
    return created["id"]


def create_issue(site: str, project: str, auth: str, spec: TicketSpec, force: bool = False) -> str:
    _validate_spec_or_exit(spec, force=force)
    fields: dict[str, Any] = {
        "project": {"key": project},
        "summary": spec.summary,
        "description": _adf(build_description(spec)),
        "issuetype": {"name": spec.issue_type},
        "priority": {"name": spec.priority},
        "labels": spec.labels,
    }
    if spec.fix_version:
        fields["fixVersions"] = [{"name": spec.fix_version}]
    resp = _request("POST", site + "/rest/api/3/issue", auth, {"fields": fields})
    return resp["key"]


def _validate_spec_or_exit(spec: TicketSpec, force: bool) -> None:
    issues = validate(spec.labels, build_description(spec))
    for line in format_issues(issues):
        print(f"{spec.alias}: {line}", file=sys.stderr)
    if any(issue.is_error for issue in issues) and not force:
        raise SystemExit(f"{spec.alias}: aborting; pass --force to skip label validation errors")


def create_link(site: str, auth: str, blocker_key: str, blocked_key: str) -> None:
    body = {"type": {"name": "Blocks"}, "inwardIssue": {"key": blocker_key}, "outwardIssue": {"key": blocked_key}}
    _request("POST", site + "/rest/api/3/issueLink", auth, body)


# ─── State ─────────────────────────────────────────────────────────


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"alias_to_key": {}, "links_created": [], "fix_version_id": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ─── Dry-run + execute ─────────────────────────────────────────────


def print_plan(specs: list[TicketSpec], links: list[LinkSpec]) -> None:
    print(f"\n=== AUDIT-29 batch plan (v2) ===")
    print(f"Tickets to create: {len(specs)}")
    print(f"  - Top META: 1")
    print(f"  - Sub-METAs (29a-29k): 11")
    print(f"  - Coordinator children (29f-1 .. 29f-14): 14")
    print(f"  - Follow-ups (LABEL-CLEANUP + TODO-BACKLOG-SWEEP): 2")
    print(f"Links to create:   {len(links)}")
    print(f"fixVersion: {TARGET_FIX_VERSION} (applied to 26 AUDIT-29 tickets; 2 follow-ups omit fixVersion)")
    print()
    print(f"--- 4-section AC discipline applied to every ticket ---")
    print(f"  Code AC / Deploy AC / Integration AC / Exercised AC + Go-Live target")
    print()
    print(f"--- Ticket list ---")
    for s in specs:
        fv = s.fix_version or "—"
        print(f"  [{s.alias:>25s}] {s.priority:>6s}  {fv:>14s}  {s.summary[:80]}")
        print(f"  {'':>27s}  Code:{len(s.code_ac):>2d}  Deploy:{len(s.deploy_ac):>2d}  Integ:{len(s.integration_ac):>2d}  Exer:{len(s.exercised_ac):>2d}  GoLive: {s.go_live[:60]}")
    print()
    print(f"--- Link DAG ({len(links)} edges, blocker → blocked) ---")
    by_blocker: dict[str, list[str]] = {}
    for lk in links:
        by_blocker.setdefault(lk.blocker_alias, []).append(lk.blocked_alias)
    for blocker in sorted(by_blocker.keys()):
        targets = sorted(by_blocker[blocker])
        print(f"  {blocker:>25s} blocks → {', '.join(targets)}")
    print()
    print(f"--- Sample full description for 29f-4 (capacity tracking, has critical-wiring note) ---")
    target = next(s for s in specs if s.alias == "29f-4")
    print(build_description(target)[:3000])
    print("... [truncated for dry-run]")
    print()
    print(f"--- L-OP-870 direction verification ---")
    sample = next(lk for lk in links if lk.blocker_alias == "29a" and lk.blocked_alias == "META")
    print(f"  Sample: blocker={sample.blocker_alias} blocked={sample.blocked_alias}")
    print(f"  Payload: type=Blocks, inwardIssue={sample.blocker_alias}, outwardIssue={sample.blocked_alias}")
    print(f"  Meaning: {sample.blocker_alias} IS the blocker. ✓")


def execute(specs: list[TicketSpec], links: list[LinkSpec], link_only: bool = False, force: bool = False) -> int:
    site, project, auth = _jira_config()
    state = load_state()
    alias_to_key: dict[str, str] = state.get("alias_to_key", {})

    # Ensure fixVersion exists first
    if not link_only and any(s.fix_version for s in specs):
        if not state.get("fix_version_id"):
            print(f"=== Ensuring fixVersion {TARGET_FIX_VERSION} exists ===")
            try:
                fv_id = ensure_fix_version(site, project, auth, TARGET_FIX_VERSION)
                state["fix_version_id"] = fv_id
                save_state(state)
                print(f"  fixVersion {TARGET_FIX_VERSION} id={fv_id}")
            except Exception as exc:
                print(f"  FAILED to ensure fixVersion: {exc}", file=sys.stderr)
                return 1

    if not link_only:
        print(f"\n=== Creating {len(specs)} tickets ===")
        for i, spec in enumerate(specs, 1):
            if spec.alias in alias_to_key:
                print(f"  [{i:>2d}/{len(specs)}] {spec.alias}: already {alias_to_key[spec.alias]} (skip)")
                continue
            try:
                key = create_issue(site, project, auth, spec, force=force)
                alias_to_key[spec.alias] = key
                state["alias_to_key"] = alias_to_key
                save_state(state)
                print(f"  [{i:>2d}/{len(specs)}] {spec.alias}: created {key}")
                time.sleep(0.5)
            except Exception as exc:
                print(f"  [{i:>2d}/{len(specs)}] {spec.alias}: FAILED — {exc}", file=sys.stderr)
                print(f"  State saved; rerun to resume.")
                return 2

    print(f"\n=== Creating {len(links)} links ===")
    links_created: set[str] = set(state.get("links_created", []))
    for i, lk in enumerate(links, 1):
        link_id = f"{lk.blocker_alias}->{lk.blocked_alias}"
        if link_id in links_created:
            print(f"  [{i:>3d}/{len(links)}] {link_id}: already (skip)")
            continue
        bk = alias_to_key.get(lk.blocker_alias)
        bdk = alias_to_key.get(lk.blocked_alias)
        if not bk or not bdk:
            print(f"  [{i:>3d}/{len(links)}] {link_id}: MISSING KEY", file=sys.stderr)
            return 3
        try:
            create_link(site, auth, bk, bdk)
            links_created.add(link_id)
            state["links_created"] = sorted(links_created)
            save_state(state)
            print(f"  [{i:>3d}/{len(links)}] {link_id}: ok ({bk} blocks {bdk}) — {lk.why}")
            time.sleep(0.3)
        except Exception as exc:
            print(f"  [{i:>3d}/{len(links)}] {link_id}: FAILED — {exc}", file=sys.stderr)
            return 4

    print(f"\n=== Done ===")
    print(f"State: {STATE_FILE}")
    print(f"Top META: https://soraapp.atlassian.net/browse/{alias_to_key['META']}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true")
    grp.add_argument("--execute", action="store_true")
    grp.add_argument("--link-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="bypass label validation errors")
    args = parser.parse_args(argv)

    specs = all_specs()
    links = all_links()

    if args.dry_run:
        print_plan(specs, links)
        return 0
    return execute(specs, links, link_only=args.link_only, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
