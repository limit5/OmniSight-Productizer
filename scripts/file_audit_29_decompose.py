#!/usr/bin/env python3
"""Decompose 6 AUDIT-29 sub-METAs into 25 runner-pickable children.

After this batch:
  - 29a (was 1d/3 deliverables)  → 3 children (29a-1..3)
  - 29b (was 4-6d/6 deliverables) → 6 children (29b-1..6) — long pole shrunk
  - 29c (was 0.75d/4)              → 4 children (29c-1..4)
  - 29d (was 1d/4)                 → 4 children (29d-1..4)
  - 29e (was 1.5d/5)               → 5 children (29e-1..5)
  - 29g (was 1.5d/4)               → 3 children (29g-1..3)

  29h/29i/29j/29k stay single (≤0.5d each). Their type:meta + priority:meta
  labels REMOVED so runners can pick them up; class:* added.

  29a/29b/29c/29d/29e/29g remain as type:meta roll-up sub-METAs (children
  block them). class:subscription-codex previously added to OP-988/989
  is REMOVED.

Critical bug fix: NEW children do NOT carry type:meta or priority:meta
(that label combo caused the "META routing → read-only capabilities"
runner-capability-blocked loop observed on OP-988).

Each new child applies same 4-AC discipline as parent batch.

Usage:
  scripts/file_audit_29_decompose.py --dry-run
  scripts/file_audit_29_decompose.py --execute

State: ~/.cache/omnisight/audit-29-decompose-state.json
Parent keys loaded from: ~/.cache/omnisight/audit-29-bootstrap-state.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

try:
    from jira_label_validator import format_issues, validate
except ModuleNotFoundError:  # pragma: no cover - import path used by tests
    from scripts.jira_label_validator import format_issues, validate

CRED_DIR = Path("~/.config/omnisight").expanduser()
PARENT_STATE = Path("~/.cache/omnisight/audit-29-bootstrap-state.json").expanduser()
STATE_FILE = Path("~/.cache/omnisight/audit-29-decompose-state.json").expanduser()
USER_AGENT = "OmniSight-audit-29-decompose/1.0"
TARGET_FIX_VERSION = "v0.5.0-rc2"


@dataclass
class ChildSpec:
    alias: str  # e.g. "29a-1"
    parent_alias: str  # e.g. "29a"
    summary: str
    scope_text: str
    code_ac: list[str]
    deploy_ac: list[str]
    integration_ac: list[str]
    exercised_ac: list[str]
    go_live: str
    areas: list[str]
    tier: str
    runner_class: str  # 'subscription-codex' or 'subscription-claude'
    extra_caps: list[str] = field(default_factory=list)
    priority: str = "Medium"
    extra_notes: str = ""


@dataclass
class ChildLink:
    blocker: str  # alias
    blocked: str  # alias
    why: str


def _ac(items: list[str]) -> str:
    return "\n".join(f"- [ ] {x}" for x in items) if items else "_(N/A)_"


def build_description(c: ChildSpec, parent_summary: str) -> str:
    return f"""# {c.summary}

## Lineage
- AUDIT-29 META (Pre-rc2 stabilization)
- AUDIT-{c.parent_alias} (parent sub-META): {parent_summary}

## Scope
{c.scope_text}

## Acceptance criteria (4-section)

### 1. Code AC
{_ac(c.code_ac)}

### 2. Deploy AC
{_ac(c.deploy_ac)}

### 3. Integration AC
{_ac(c.integration_ac)}

### 4. Exercised AC
{_ac(c.exercised_ac)}

## Go-Live target
{c.go_live}

## fixVersion
{TARGET_FIX_VERSION}
{('## Notes' + chr(10) + chr(10) + c.extra_notes) if c.extra_notes else ''}"""


# ──────────────────────────────────────────────────────────────────
#  Children spec
# ──────────────────────────────────────────────────────────────────


def _29a_children() -> list[ChildSpec]:
    return [
        ChildSpec("29a-1", "29a",
            "AUDIT-29a-1: Run scripts/deployment-audit.sh; close 4 fatal red rows",
            "Run the deployment audit baseline + remediate 4 known-fatal-red rows surfaced 2026-05-12.",
            code_ac=[
                "`scripts/deployment-audit.sh` executable + canonical path",
                "Document the 4 closed red rows in `docs/audit/2026-05-13-deployment-audit-baseline.md`",
            ],
            deploy_ac=[
                "Script runs on host with 0 fatal red rows",
                "Daily cron registered: `0 7 * * *` runs deployment-audit.sh + alerts operator on red",
            ],
            integration_ac=["Daily cron output JSONL appended to existing audit log surface"],
            exercised_ac=[">=3 consecutive days of clean daily-audit cron output"],
            go_live="T+0.5d", areas=["devops", "tooling"], tier="M",
            runner_class="subscription-codex"),
        ChildSpec("29a-2", "29a",
            "AUDIT-29a-2: Merge anti-pattern #13 (shipped-but-not-deployed) to architecture-anti-patterns.md",
            "Pattern #13 covers AUDIT-23 root cause. Doc work + lesson cross-link.",
            code_ac=[
                "`docs/sop/architecture-anti-patterns.md` includes pattern #13 with: symptom, root cause, cure, prevention",
                "`docs/sop/lessons/L-OP-NNN-shipped-not-deployed.md` cross-references pattern #13",
            ],
            deploy_ac=["docs merged via Gerrit, reachable via docs site index"],
            integration_ac=["Pattern #13 referenced from AUDIT-23 ticket comment + AUDIT-29 META description"],
            exercised_ac=["Subsequent ticket-filing scripts cite pattern #13 in their AC docstrings (e.g. file_audit_29_tickets.py)"],
            go_live="T+0.25d", areas=["docs"], tier="S",
            runner_class="subscription-claude",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update"]),
        ChildSpec("29a-3", "29a",
            "AUDIT-29a-3: CI lint hook — reject area:devops tickets lacking operator activation step",
            "Pre-commit or CI hook validates ticket description for 'activation step' bullet when area:devops present.",
            code_ac=[
                "`scripts/lint-jira-ticket-spec.py` checks area:devops → requires 'activation' bullet",
                "Hook registered in pre-commit config (and/or CI)",
                "Unit tests: pass + fail cases",
            ],
            deploy_ac=["Hook live in pre-commit; CI step passes on rebase"],
            integration_ac=["Hook catches a synthetic test PR with area:devops but no activation step"],
            exercised_ac=["Hook fires on at least 1 real ticket-filing attempt within 7d (positive guard)"],
            go_live="T+0.5d", areas=["devops", "tooling", "tests"], tier="M",
            runner_class="subscription-codex"),
    ]


def _29b_children() -> list[ChildSpec]:
    return [
        ChildSpec("29b-1", "29b",
            "AUDIT-29b-1: Deploy Cognee container (Phase 2 long pole)",
            "Bring up the Cognee container (the 3D memory primary store).",
            code_ac=[
                "`deploy/cognee/docker-compose.yml` + image pin",
                "Healthcheck endpoint + env config",
                "Cognee API auth wired",
            ],
            deploy_ac=[
                "`docker ps | grep cognee` shows healthy state",
                "Cognee API reachable via configured port",
                "Container restarts cleanly after `docker compose restart`",
            ],
            integration_ac=[
                "Cognee API responds to schema-introspection query",
                "Persistent volume backs Cognee data (survives container restart)",
            ],
            exercised_ac=[">=48h Cognee uptime", "At least 1 successful test query/write end-to-end"],
            go_live="T+1d", areas=["devops"], tier="M",
            runner_class="subscription-codex",
            extra_notes="Long-pole start. 29b-5 / 29b-6 / 29f-6 / 29f-11 all depend on this being live."),
        ChildSpec("29b-2", "29b",
            "AUDIT-29b-2: Neo4j 5.24 systemd unit + on-host TLS",
            "Neo4j backs Cognee's graph layer.",
            code_ac=[
                "`deploy/systemd/neo4j.service` with Restart=, StandardOutput=, ExecStart=",
                "Neo4j 5.24 install + TLS cert + auth password under ~/.config/omnisight/",
            ],
            deploy_ac=["`systemctl --user status neo4j` clean", "Bolt + HTTPS endpoints reachable"],
            integration_ac=["Cognee container can connect to Neo4j on configured port"],
            exercised_ac=[">=48h Neo4j uptime", "At least 1 graph query roundtrip from Cognee"],
            go_live="T+1d", areas=["devops"], tier="M",
            runner_class="subscription-codex"),
        ChildSpec("29b-3", "29b",
            "AUDIT-29b-3: Graphiti MCP service + DNS/port + auth",
            "Graphiti exposes the memory graph as MCP tool surface.",
            code_ac=[
                "Graphiti MCP service deploy unit (systemd or compose)",
                "MCP endpoint URL + auth token config",
            ],
            deploy_ac=["Service running; MCP endpoint reachable", "Auth verified via test request"],
            integration_ac=["Runner MCP client config includes Graphiti endpoint", "Test runner pickup queries Graphiti tool list"],
            exercised_ac=["At least 1 runner pickup observed using Graphiti MCP tool"],
            go_live="T+0.75d", areas=["devops", "backend"], tier="M",
            runner_class="subscription-codex"),
        ChildSpec("29b-4", "29b",
            "AUDIT-29b-4: Provision /var/omnisight/memory/ + per-fleet dirs",
            "Memory dirs for fleet (per-instance scratch + shared store).",
            code_ac=[
                "`scripts/provision-memory-dirs.sh` (idempotent)",
                "Dirs: `/var/omnisight/memory/{shared,instance-{claude,codex}-{1,2}}/`",
                "Correct ownership + 0750 perms",
            ],
            deploy_ac=["Dirs exist on host with correct ownership", "Available to runner processes via mount or path"],
            integration_ac=["Runners can write to instance-* dirs without sudo"],
            exercised_ac=["At least 1 runner observed writing scratch state to its instance dir"],
            go_live="T+0.25d", areas=["devops", "tooling"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29b-5", "29b",
            "AUDIT-29b-5: Populate agent_feature_flags module (currently empty)",
            "Sprint F shipped the module skeleton but it's empty. Populate with the canonical feature flags Coordinator + runners will read.",
            code_ac=[
                "`backend/agents/agent_feature_flags.py` defines: failure_graph, project_state, ops_only, coord_skip, cognee_recall, antipattern_inject",
                "Each flag has env-knob fallback + default value",
                "Unit tests cover env-override behavior",
            ],
            deploy_ac=["Module importable in runner runtime", "Env-knob overrides honored on next runner tick"],
            integration_ac=["Runner _build_prompt reads cognee_recall flag (consumed in 29b-6)"],
            exercised_ac=["At least 1 runner pickup observed reading non-default flag from env"],
            go_live="T+0.5d", areas=["backend", "tests"], tier="M",
            runner_class="subscription-codex"),
        ChildSpec("29b-6", "29b",
            "AUDIT-29b-6: Wire runner _build_prompt to Cognee + ingest 68 lessons + L-OP pattern doc",
            "THE meta-system mechanism: lessons surface to runner per pickup. Includes Cognee ingestion of 68 existing lesson files + 12 anti-patterns.",
            code_ac=[
                "Cognee ingestion script `scripts/cognee-ingest-lessons.py` loads docs/sop/lessons/*.md + docs/sop/architecture-anti-patterns.md",
                "Runner `_build_prompt` adds 'Relevant lessons' + 'Anti-patterns matching this ticket' blocks (Cognee top-N similarity)",
                "L-OP-NNN lesson doc: 'lesson-surface meta-mechanism' (the architectural pattern)",
                "Tests: synthetic ticket → expected lesson recall",
            ],
            deploy_ac=[
                "Ingestion ran; Cognee shows 68+ lesson nodes + 12+ anti-pattern nodes",
                "Runner restarts pick up new prompt blocks (on next tick)",
            ],
            integration_ac=[
                "Runner _build_prompt output (in debug-mode log) shows lesson-recall block for ticket pickups",
                "Anti-pattern auto-injected when ticket area matches pattern's domain",
            ],
            exercised_ac=[
                ">=10 ticket pickups observed with lesson-recall in prompt",
                ">=3 distinct lessons surfaced across the 10 pickups",
                "L-OP-NNN doc + index updated via build_lessons_index.py",
            ],
            go_live="T+1d after 29b-1 + 29b-3 + 29b-5 complete",
            areas=["backend", "docs", "tests"], tier="L",
            runner_class="subscription-claude",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
    ]


def _29c_children() -> list[ChildSpec]:
    return [
        ChildSpec("29c-1", "29c",
            "AUDIT-29c-1: Create 4 systemd user units for runners",
            "Replace tmux loops with systemd user units.",
            code_ac=[
                "4 unit files: runner-claude-bot@default.service, runner-claude-bot@worker2.service, runner-codex-bot@default.service, runner-codex-bot@worker2.service",
                "Each unit has Restart=, StandardOutput=, ExecStart= per startup contract",
            ],
            deploy_ac=["All 4 enabled + active under linger=yes", "Survive operator logout"],
            integration_ac=["Coordinator (29f-7) can `systemctl --user start runner-*@*` to revive a dead instance"],
            exercised_ac=[">=24h continuous uptime on systemd-managed runners"],
            go_live="T+0.25d", areas=["devops"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29c-2", "29c",
            "AUDIT-29c-2: Move runner wrappers to deploy/runner-wrappers/ (out of /tmp)",
            "Wrappers checked into the repo, not /tmp where they get nuked.",
            code_ac=[
                "Wrappers committed under `deploy/runner-wrappers/`",
                "/tmp/runner_wrappers/ symlinks (or removal) configured",
                "systemd unit ExecStart points at canonical path",
            ],
            deploy_ac=["Active runners now ExecStart from new path", "/tmp/runner_wrappers/ no longer the source of truth"],
            integration_ac=["29c-1 unit files reference new wrapper path"],
            exercised_ac=["Forced runner restart observed picking up wrapper from new path"],
            go_live="T+0.25d", areas=["devops"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29c-3", "29c",
            "AUDIT-29c-3: Author docs/sop/daemon-startup-contract.md",
            "Codify the 3 required directives (Restart=, StandardOutput=, ExecStart=) + when to use linger=yes.",
            code_ac=["`docs/sop/daemon-startup-contract.md` exists with required directives + examples"],
            deploy_ac=["doc merged + reachable from docs index"],
            integration_ac=["29c-4 pre-commit hook references this doc"],
            exercised_ac=["Doc cited in at least 1 follow-up systemd unit ticket"],
            go_live="T+0.25d", areas=["docs"], tier="S",
            runner_class="subscription-claude",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update"]),
        ChildSpec("29c-4", "29c",
            "AUDIT-29c-4: Pre-commit hook — validate new .service files have required directives",
            "Lint enforcement for the daemon-startup contract.",
            code_ac=[
                "Pre-commit hook script + config entry",
                "Tests: pass on conformant unit, fail on missing-directive unit",
            ],
            deploy_ac=["Hook active in pre-commit; CI step passes"],
            integration_ac=["Hook catches synthetic non-conformant unit in test PR"],
            exercised_ac=["Hook fires on at least 1 real systemd-unit commit within 7d"],
            go_live="T+0.25d", areas=["devops", "tooling", "tests"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
    ]


def _29d_children() -> list[ChildSpec]:
    return [
        ChildSpec("29d-1", "29d",
            "AUDIT-29d-1: Caddy — remove host-based staging.sora.services rule + add /audit/* path-rule",
            "Caddy config switch from subdomain to path-based.",
            code_ac=["Caddy config updated: no host-based rule for staging.sora.services; /audit/* path-rule added"],
            deploy_ac=["Caddy reloaded; new config active", "Old subdomain confirmed removed from active config"],
            integration_ac=["Smoke probe + canary probe reach staging via path-based URL"],
            exercised_ac=[">=48h Caddy uptime with new config"],
            go_live="T+0.25d", areas=["devops"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29d-2", "29d",
            "AUDIT-29d-2: Smoke + canary probes use OMNISIGHT_STAGING_URL=http://localhost:18080",
            "Probe scripts switch from subdomain default to port-based URL.",
            code_ac=[
                "`backend/agents/staging_gate.py` (smoke/canary) reads OMNISIGHT_STAGING_URL env",
                "Default = `http://localhost:18080`",
                "systemd timers updated with env",
            ],
            deploy_ac=["Probes redeployed; using port URL", "Old subdomain probes confirmed inactive"],
            integration_ac=["Smoke probe writes canary-status.jsonl + smoke-status.jsonl via port-based path"],
            exercised_ac=["At least 1 green probe cycle observed"],
            go_live="T+0.25d", areas=["devops", "backend"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29d-3", "29d",
            "AUDIT-29d-3: Author docs/sop/cross-host-portability-staging.md",
            "Codify the port-based staging pattern + 5a→5c portability checklist.",
            code_ac=["`docs/sop/cross-host-portability-staging.md` exists with 5a→5c portability checklist"],
            deploy_ac=["doc merged + reachable"],
            integration_ac=["Doc cited from AUDIT-19 META + 29d remediation comment"],
            exercised_ac=["Doc cited in at least 1 follow-up staging migration ticket"],
            go_live="T+0.25d", areas=["docs"], tier="S",
            runner_class="subscription-claude",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update"]),
        ChildSpec("29d-4", "29d",
            "AUDIT-29d-4: Linear-history-consumer audit (deferred from AUDIT-26a)",
            "Verify MERGE_ALWAYS submit-type impact on downstream consumers expecting linear history.",
            code_ac=[
                "`docs/audit/2026-05-XX-linear-history-consumers.md` lists consumers checked",
                "Each consumer marked OK / needs-fix / superseded",
            ],
            deploy_ac=["Doc merged; any consumer needing fix filed as new ticket"],
            integration_ac=["Audit findings reflected in ADR-0020 follow-up section"],
            exercised_ac=["No consumer regression observed after MERGE_ALWAYS active"],
            go_live="T+0.5d", areas=["docs", "backend"], tier="M",
            runner_class="subscription-claude",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update"]),
    ]


def _29e_children() -> list[ChildSpec]:
    return [
        ChildSpec("29e-1", "29e",
            "AUDIT-29e-1: staging-pg-snapshot — rewrite snapshot-restore.sh to use docker exec",
            "Eliminate host psql dependency.",
            code_ac=["`snapshot-restore.sh` uses `docker exec` for psql commands", "Unit test: snapshot restore on synthetic anonymized snapshot"],
            deploy_ac=["Updated script deployed; staging-pg-snapshot service uses it", "snapshot restore works without host psql"],
            integration_ac=["staging-sync (29e-3) can trigger snapshot restore end-to-end"],
            exercised_ac=["At least 1 daily snapshot restored without manual intervention"],
            go_live="T+0.25d", areas=["devops", "tooling"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29e-2", "29e",
            "AUDIT-29e-2: omnisight-staging-compose — fix HEALTHZ_URL port (19000 → 18080)",
            "Port mismatch causing healthcheck failures.",
            code_ac=["docker-compose.yml HEALTHZ_URL points at 18080", "env contract updated to match"],
            deploy_ac=["staging-compose restarted with new port", "Healthcheck green"],
            integration_ac=["Caddy (29d-1) routes via 18080 — consistent"],
            exercised_ac=[">=24h healthcheck green"],
            go_live="T+0.25d", areas=["devops"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29e-3", "29e",
            "AUDIT-29e-3: staging-sync + staging-gate-canary/smoke — verify auto-fixed by 29d",
            "These were failing due to subdomain config; Phase 4 should resolve. This ticket validates.",
            code_ac=["End-to-end test: develop tip → staging-sync → snapshot restore → smoke + canary probe → JSONL emit"],
            deploy_ac=["All 3 units active + green"],
            integration_ac=["Smoke + canary JSONL feeds release_milestone_checker for R3 readiness"],
            exercised_ac=[">=48h all 3 units green"],
            go_live="T+0.5d after 29d-1 + 29d-2 complete",
            areas=["devops", "backend"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29e-4", "29e",
            "AUDIT-29e-4: gerrit-jira-bridge-watchdog — diagnose + close or retire",
            "Watchdog has been intermittently failing. Either fix or document why retired.",
            code_ac=[
                "Diagnose root cause; document in `docs/audit/2026-05-XX-bridge-watchdog-diagnosis.md`",
                "If fix needed: code change + test. If retire: removal + replacement design",
            ],
            deploy_ac=["bridge-watchdog either green or removed cleanly"],
            integration_ac=["Either watchdog protects bridge daemon, or alternative monitoring covers the gap"],
            exercised_ac=[">=72h either green-watchdog or documented-no-watchdog state"],
            go_live="T+0.5d", areas=["devops", "backend"], tier="M",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29e-5", "29e",
            "AUDIT-29e-5: Staging API token for /audit/verify auth",
            "Token provisioned + stored under canonical creds dir.",
            code_ac=[
                "Token generated + stored at `~/.config/omnisight/staging-audit-verify-token` (0600)",
                "Backend `/audit/verify` honors token",
                "Audit-verify clients (e.g. release_milestone_checker) read token",
            ],
            deploy_ac=["Token live on host", "`/audit/verify` returns 200 with valid token, 401 without"],
            integration_ac=["Audit-verify endpoint reachable from release_milestone_checker"],
            exercised_ac=[">=1 successful /audit/verify call observed via real release-chain ticket"],
            go_live="T+0.25d", areas=["devops", "backend"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
    ]


def _29g_children() -> list[ChildSpec]:
    return [
        ChildSpec("29g-1", "29g",
            "AUDIT-29g-1: Runner self-fix code path — detect mergeable=false + rebase + force-push (≤3 attempts)",
            "Pre-+1 mergeability check + auto-rebase loop.",
            code_ac=[
                "`backend/agents/pre_review_self_fix.py` (or wired into auto-runner-jira)",
                "Detects mergeable=false via Gerrit API",
                "Performs git rebase + force-push (cap=3)",
                "Unit tests cover happy path + cap-exhaustion",
            ],
            deploy_ac=["All 4 runner instances redeployed with new logic", "Old non-self-fix runner code path deprecated"],
            integration_ac=["Self-fix triggered automatically on next runner pickup that hits mergeable=false"],
            exercised_ac=["At least 1 real merge-conflict resolved by self-fix observed in production"],
            go_live="T+0.75d", areas=["backend", "tests"], tier="M",
            runner_class="subscription-codex"),
        ChildSpec("29g-2", "29g",
            "AUDIT-29g-2: Exhaustion handler — file `pre-review-self-fix-exhausted` + @coordinator",
            "On 3 failed self-fix attempts, escalate.",
            code_ac=[
                "Exhaustion handler files dedicated ticket with full conflict context (diff, change-id, attempts)",
                "@-mentions coordinator (or operator if coord not live)",
                "Unit test: 3 attempts → exhaustion path",
            ],
            deploy_ac=["Handler deployed in runner runtime"],
            integration_ac=["Filed ticket has labels: `needs-coordinator`, `pre-review-self-fix-exhausted`, `class:operator`"],
            exercised_ac=["At least 1 exhaustion observed → escalation chain exercised end-to-end"],
            go_live="T+0.25d after 29g-1", areas=["backend"], tier="S",
            runner_class="subscription-codex",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
        ChildSpec("29g-3", "29g",
            "AUDIT-29g-3: Pattern doc + cross-codebase audit + integration test",
            "Document the self-fix pattern + ensure no duplicate logic elsewhere.",
            code_ac=[
                "`docs/sop/lessons/L-OP-NNN-pre-review-self-fix.md` (or L-COORD-NN if post-coord)",
                "Cross-codebase grep audit: no other agent does rebase-on-conflict",
                "Integration test injects merge conflict + verifies 3-attempt cap + escalation",
            ],
            deploy_ac=["Doc merged; integration test in CI"],
            integration_ac=["CI runs the integration test on every coordinator + runner commit"],
            exercised_ac=["Test runs ≥7 consecutive CI cycles without flake"],
            go_live="T+0.5d after 29g-1 + 29g-2", areas=["docs", "tests"], tier="M",
            runner_class="subscription-claude",
            extra_caps=["capability:enable=gerrit_push","capability:enable=code_edit","capability:enable=jira_update","capability:enable=run_lint","capability:enable=run_tests"]),
    ]


def all_children() -> list[ChildSpec]:
    return _29a_children() + _29b_children() + _29c_children() + _29d_children() + _29e_children() + _29g_children()


def all_links() -> list[ChildLink]:
    links: list[ChildLink] = []
    # Each child blocks its parent sub-META
    for c in all_children():
        links.append(ChildLink(c.alias, c.parent_alias, "child blocks sub-META roll-up"))
    # Internal sequencing
    # 29b: 29b-1 must be live before 29b-6 (Cognee wire)
    links.append(ChildLink("29b-1", "29b-6", "Cognee must be live before _build_prompt wires it"))
    links.append(ChildLink("29b-3", "29b-6", "Graphiti must be live before MCP tool is queryable"))
    links.append(ChildLink("29b-5", "29b-6", "feature flags must exist before _build_prompt reads them"))
    # 29c: 29c-3 doc → 29c-4 hook references doc
    links.append(ChildLink("29c-3", "29c-4", "pre-commit hook references daemon-startup-contract doc"))
    links.append(ChildLink("29c-2", "29c-1", "wrappers must be at new path before unit ExecStart can reference them"))
    # 29d: 29d-1 Caddy first, then 29d-2 probes
    links.append(ChildLink("29d-1", "29d-2", "probes need Caddy path-rule to be in place"))
    # 29e: 29e-3 needs 29d-1 + 29d-2 (transitive via 29d/29e roll-up — explicit link helps)
    links.append(ChildLink("29d-1", "29e-3", "staging-sync verification needs Caddy switch done"))
    links.append(ChildLink("29d-2", "29e-3", "staging-sync verification needs probes switched"))
    # 29g: sequential 1 → 2 → 3
    links.append(ChildLink("29g-1", "29g-2", "exhaustion handler needs self-fix code path"))
    links.append(ChildLink("29g-2", "29g-3", "pattern doc + test after impl"))
    return links


# Sub-META label-update plan (post-decompose)
SUBMETA_LABEL_UPDATES = {
    # decomposed roll-ups: REMOVE class:* (added earlier to 29a/29b only); keep type:meta + priority:meta
    "29a": {"remove": ["class:subscription-codex"], "add": []},
    "29b": {"remove": ["class:subscription-codex"], "add": []},
    # 29c/29d/29e/29g: no class:* was added, so nothing to remove. Keep type:meta + priority:meta.
    # single-phase sub-METAs: REMOVE type:meta + priority:meta, ADD class:* + missing area
    "29h": {"remove": ["type:meta", "priority:meta"],
            "add": ["class:subscription-codex", "type:feature", "area:docs"]},  # AC mentions docs/sop/release-lifecycle-states.md
    "29i": {"remove": ["type:meta", "priority:meta"],
            "add": ["class:subscription-codex", "type:feature", "area:tooling", "area:docs",
                    "capability:enable=gerrit_push", "capability:enable=code_edit", "capability:enable=jira_update", "capability:enable=run_lint", "capability:enable=run_tests"]},
    "29j": {"remove": ["type:meta", "priority:meta"],
            "add": ["class:subscription-codex", "type:feature",
                    "capability:enable=gerrit_push", "capability:enable=code_edit", "capability:enable=jira_update", "capability:enable=run_lint", "capability:enable=run_tests"]},
    "29k": {"remove": ["type:meta", "priority:meta"],
            "add": ["class:subscription-claude", "type:feature",
                    "capability:enable=gerrit_push", "capability:enable=code_edit", "capability:enable=jira_update"]},
}


# ──────────────────────────────────────────────────────────────────
#  JIRA API
# ──────────────────────────────────────────────────────────────────


def _load_env(env_file: Path) -> dict[str, str]:
    out = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _jira_config(use_codex_creds: bool = False) -> tuple[str, str, str]:
    env_file = CRED_DIR / ("jira-codex.env" if use_codex_creds else "jira-claude.env")
    tok_file = CRED_DIR / ("jira-codex-token" if use_codex_creds else "jira-claude-token")
    email_key = "OMNISIGHT_JIRA_CODEX_EMAIL" if use_codex_creds else "OMNISIGHT_JIRA_CLAUDE_EMAIL"
    env = _load_env(env_file)
    tok = tok_file.read_text().strip()
    auth = "Basic " + b64encode(f"{env[email_key]}:{tok}".encode()).decode()
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
    return {"type":"doc","version":1,"content":[
        {"type":"codeBlock","attrs":{"language":"markdown"},"content":[{"type":"text","text":md}]}]}


# ──────────────────────────────────────────────────────────────────
#  Execute
# ──────────────────────────────────────────────────────────────────


def load_parent_keys() -> dict[str, str]:
    if not PARENT_STATE.exists():
        sys.exit(f"Parent state file missing: {PARENT_STATE}. Run file_audit_29_tickets.py first.")
    return json.loads(PARENT_STATE.read_text())["alias_to_key"]


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"alias_to_key": {}, "links_created": [], "submeta_updated": []}


def save_state(s: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


def print_plan() -> None:
    children = all_children()
    links = all_links()
    parents = load_parent_keys() if PARENT_STATE.exists() else {}

    print(f"=== AUDIT-29 decompose plan ===")
    print(f"New children: {len(children)}")
    print(f"  29a: {sum(1 for c in children if c.parent_alias == '29a')}")
    print(f"  29b: {sum(1 for c in children if c.parent_alias == '29b')}")
    print(f"  29c: {sum(1 for c in children if c.parent_alias == '29c')}")
    print(f"  29d: {sum(1 for c in children if c.parent_alias == '29d')}")
    print(f"  29e: {sum(1 for c in children if c.parent_alias == '29e')}")
    print(f"  29g: {sum(1 for c in children if c.parent_alias == '29g')}")
    print(f"Links to create: {len(links)}")
    print(f"Sub-META label updates: {len(SUBMETA_LABEL_UPDATES)}")
    print()

    by_class = {"subscription-codex": [], "subscription-claude": []}
    for c in children:
        by_class[c.runner_class].append(c.alias)
    print(f"Routing distribution:")
    print(f"  codex: {len(by_class['subscription-codex'])} → {', '.join(by_class['subscription-codex'])}")
    print(f"  claude: {len(by_class['subscription-claude'])} → {', '.join(by_class['subscription-claude'])}")
    print()

    print(f"--- Children detail ---")
    for c in children:
        parent_key = parents.get(c.parent_alias, "(?)")
        caps = f" +{len(c.extra_caps)}cap" if c.extra_caps else ""
        print(f"  [{c.alias:>7s}] -> blocks {c.parent_alias}({parent_key})  "
              f"class={c.runner_class.replace('subscription-','sub-'):>10s} tier={c.tier} "
              f"areas={','.join(c.areas):<22s}{caps}")
        print(f"          GoLive: {c.go_live}")

    print()
    print(f"--- Sub-META label fixes ---")
    for alias, upd in SUBMETA_LABEL_UPDATES.items():
        print(f"  {alias} ({parents.get(alias, '?')}): remove={upd['remove']}  add={upd['add']}")


def execute(force: bool = False) -> int:
    children = all_children()
    links = all_links()
    parents = load_parent_keys()
    state = load_state()

    site, project, auth = _jira_config()

    # Step 1: Create children
    print(f"\n=== Creating {len(children)} children ===")
    for i, c in enumerate(children, 1):
        if c.alias in state["alias_to_key"]:
            print(f"  [{i:>2d}/{len(children)}] {c.alias}: already {state['alias_to_key'][c.alias]} (skip)")
            continue
        parent_key = parents.get(c.parent_alias)
        if not parent_key:
            print(f"  [{i:>2d}/{len(children)}] {c.alias}: FAILED — parent {c.parent_alias} not in parent state", file=sys.stderr)
            return 1
        # Get parent summary for lineage
        try:
            psum_resp = _request("GET", f"{site}/rest/api/3/issue/{parent_key}?fields=summary", auth, None)
            psum = psum_resp["fields"]["summary"]
        except Exception:
            psum = parent_key
        labels = [
            f"class:{c.runner_class}",
            f"tier:{c.tier}",
            "agent:auto",
            "type:feature",
            f"phase:audit-{c.parent_alias}",
            "scope:pre-rc2",
        ] + [f"area:{a}" for a in c.areas] + c.extra_caps
        if not _validate_labels_or_exit(c.alias, labels, build_description(c, psum), force=force):
            return 2
        body = {"fields": {
            "project": {"key": project},
            "summary": c.summary,
            "description": _adf(build_description(c, psum)),
            "issuetype": {"name": "Story"},
            "priority": {"name": c.priority},
            "labels": labels,
            "fixVersions": [{"name": TARGET_FIX_VERSION}],
        }}
        try:
            resp = _request("POST", f"{site}/rest/api/3/issue", auth, body)
            key = resp["key"]
            state["alias_to_key"][c.alias] = key
            save_state(state)
            print(f"  [{i:>2d}/{len(children)}] {c.alias}: created {key}")
            time.sleep(0.5)
        except Exception as e:
            print(f"  [{i:>2d}/{len(children)}] {c.alias}: FAILED — {e}", file=sys.stderr)
            return 2

    # Step 2: Create links
    all_keys = {**parents, **state["alias_to_key"]}
    print(f"\n=== Creating {len(links)} links ===")
    done_links = set(state["links_created"])
    for i, lk in enumerate(links, 1):
        lk_id = f"{lk.blocker}->{lk.blocked}"
        if lk_id in done_links:
            print(f"  [{i:>2d}/{len(links)}] {lk_id}: already (skip)")
            continue
        bk = all_keys.get(lk.blocker)
        bdk = all_keys.get(lk.blocked)
        if not bk or not bdk:
            print(f"  [{i:>2d}/{len(links)}] {lk_id}: MISSING KEY", file=sys.stderr)
            return 3
        body = {"type":{"name":"Blocks"}, "inwardIssue":{"key":bk}, "outwardIssue":{"key":bdk}}
        try:
            _request("POST", f"{site}/rest/api/3/issueLink", auth, body)
            done_links.add(lk_id)
            state["links_created"] = sorted(done_links)
            save_state(state)
            print(f"  [{i:>2d}/{len(links)}] {lk_id} ({bk} blocks {bdk}): ok — {lk.why}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  [{i:>2d}/{len(links)}] {lk_id}: FAILED — {e}", file=sys.stderr)
            return 4

    # Step 3: Sub-META label updates
    print(f"\n=== Updating {len(SUBMETA_LABEL_UPDATES)} sub-META labels ===")
    submeta_done = set(state["submeta_updated"])
    for alias, upd in SUBMETA_LABEL_UPDATES.items():
        if alias in submeta_done:
            print(f"  {alias}: already updated (skip)")
            continue
        key = parents.get(alias)
        if not key:
            print(f"  {alias}: parent key not found", file=sys.stderr)
            continue
        ops = [{"remove": x} for x in upd["remove"]] + [{"add": x} for x in upd["add"]]
        body = {"update": {"labels": ops}}
        try:
            _request("PUT", f"{site}/rest/api/3/issue/{key}", auth, body)
            submeta_done.add(alias)
            state["submeta_updated"] = sorted(submeta_done)
            save_state(state)
            print(f"  {alias} ({key}): -{len(upd['remove'])} +{len(upd['add'])}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {alias} ({key}): FAILED — {e}", file=sys.stderr)
            return 5

    # Step 4: Clean stale claim labels on OP-988 / OP-989
    print(f"\n=== Cleaning stale claim labels ===")
    for parent_alias in ["29a", "29b"]:
        key = parents.get(parent_alias)
        if not key:
            continue
        resp = _request("GET", f"{site}/rest/api/3/issue/{key}?fields=labels", auth, None)
        stale = [l for l in resp["fields"]["labels"] if l.startswith("claim:")]
        if not stale:
            print(f"  {parent_alias} ({key}): no stale claim — skip")
            continue
        ops = [{"remove": l} for l in stale]
        try:
            _request("PUT", f"{site}/rest/api/3/issue/{key}", auth, {"update": {"labels": ops}})
            print(f"  {parent_alias} ({key}): removed {len(stale)} stale claim(s)")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {parent_alias} ({key}): FAILED — {e}", file=sys.stderr)

    print(f"\n=== Done ===")
    print(f"State: {STATE_FILE}")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--execute", action="store_true")
    p.add_argument("--force", action="store_true", help="bypass label validation errors")
    args = p.parse_args(argv)
    if args.dry_run:
        _validate_all_children_or_exit(force=args.force)
        print_plan()
        return 0
    return execute(force=args.force)


def _child_labels(c: ChildSpec) -> list[str]:
    return [
        f"class:{c.runner_class}",
        f"tier:{c.tier}",
        "agent:auto",
        "type:feature",
        f"phase:audit-{c.parent_alias}",
        "scope:pre-rc2",
    ] + [f"area:{area}" for area in c.areas] + c.extra_caps


def _validate_labels_or_exit(alias: str, labels: list[str], description: str, force: bool) -> bool:
    issues = validate(labels, description)
    for line in format_issues(issues):
        print(f"{alias}: {line}", file=sys.stderr)
    return force or not any(issue.is_error for issue in issues)


def _validate_all_children_or_exit(force: bool) -> None:
    failed = False
    for child in all_children():
        ok = _validate_labels_or_exit(child.alias, _child_labels(child), build_description(child, child.parent_alias), force)
        failed = failed or not ok
    if failed:
        raise SystemExit("aborting; pass --force to skip label validation errors")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
