# Design — dedicated conference-appliance runner via a scoped agent-class

**Date:** 2026-06-14 · **Status:** design (for review before any code) · **Touches the runner's own execution path → mandatory human +2 per L1.**

## Problem
Case 5 (conference-appliance) work must be built by the runner using the same JIRA→Gerrit→GitLab flow as the main project, but pushed to a **different Gerrit project** (`omnisight/conference-appliance`), not `OmniSight-Productizer`.

The runner routes by **JIRA project key** (`product_sources`/`delivery_target`) and its wrapper hardcodes the Gerrit URL to productizer. The pickup JQL filters by **both** project and class:
```
PICKUP_JQL_TEMPLATE = project = "{project}" AND issuetype = Story AND status = "To Do"
  AND assignee is EMPTY AND labels = "class:{cls}" AND labels not in ("tier:X") ...
```
Today every Case 5 ticket sits in project **OP** with `class:subscription-claude`, so the **normal** claude/codex runners match and pick them — then clone the productizer worktree and have nowhere correct to land. Observed twice on 2026-06-13/14: normal runners auto-picked OP-2170/2171/2172/2173 into the productizer worktree (rescued before any Gerrit push, zero pollution). The pickup gate is purely label-driven, so any pickup label (`agent:auto`, `capability:enable=gerrit_push`) re-arms them.

## Goal
A **dedicated conference runner instance** that:
1. only picks Case 5 (conference) tickets, and
2. the **normal** runners NEVER pick those tickets, and
3. pushes to the conference-appliance Gerrit project (review flow + stopgap GitLab mirror already working).

## Key insight (small change, leans on existing mechanisms)
Both scoping levers already exist:
- **class** — the JQL already filters `labels = "class:{cls}"`. A NEW class that the normal runners don't run is a clean, label-driven scope. Normal runners keep `subscription-claude/codex`; they will *never* match a conference-classed ticket. This is the robust fix for the double-pickup (no label can re-arm a normal runner for a conf ticket).
- **Gerrit URL** — `run-ephemeral.sh` already honors `OMNISIGHT_GERRIT_URL` (env override). Just set it per conf instance.
- **project key** — `make_client` already honors `OMNISIGHT_JIRA_PROJECT_KEY` (default OP). Conf tickets can stay in OP (chosen Option 2) — the **class** does the scoping, not the project.

## ⚠️ Blast-radius finding (grep done 2026-06-14 — bigger than "3 maps")
A grep for the 4 legacy class string-literals across `backend/agents/` + `auto-runner-jira.py` found **77 hits**. The conf classes must be registered in **every map that gates behavior**, not just the 3 in the runner pickup/auth path. Maps that MUST learn the conf variants (else the conf runner fails at that layer):
- `provider_orchestrator.py:148-153` — class→provider tuple (quota/orchestration). **Required.**
- `routing_policy.py:76-77` — provider→{classes} frozensets. **Required** (reverse map).
- `capability_registry.py AGENT_CLASS_PROFILE` — class→provider profile. **Required.**
- `jira_dispatch.py _BASE_BOT_BY_CLASS` + `_GERRIT_AUTH_BY_CLASS` — class→bot/ssh. **Required.**
- `cost_estimator_baseline.py:29-45` — class→cost baseline. Needed for cost telemetry (degrades, not blocks).
- Benign (default-only / no enumeration gate): `runner_log_parser`, `session_resume`, `runner_orphan_reaper`, `sprint_replan`, `contribution_pr_tracker`, `release_notes_generator` — these use a class as a *default value*, not as a membership gate; conf runner overrides via env. Confirm each during implementation but no change expected.

**Cleaner alternative to evaluate in review:** instead of adding the conf classes to N maps, **derive** the conf variant's provider from its base (strip the `-conf` suffix → look up the base class). One helper `base_class(cls)` consulted at each map miss would shrink the change to the maps that genuinely need distinct behavior. Trade-off: a derivation helper touches more call sites but removes the "forgot map #6" failure mode. Reviewer to pick: explicit-N-maps vs derive-from-base.

## Change set (precise)
### 1. Register the new classes (≥5 maps — see blast-radius finding)
- `backend/agents/capability_registry.py` — `AGENT_CLASS_PROFILE`: add
  `"subscription-claude-conf": ("anthropic-subscription", "<unknown>")` and
  `"subscription-codex-conf": ("openai-subscription", "<unknown>")` (same provider profiles as their base classes → quota/capability resolution unchanged).
- `backend/agents/jira_dispatch.py` — `_BASE_BOT_BY_CLASS`: map both conf classes to the **existing** bots (`claude-bot` / `codex-bot`) so JIRA cred files + Gerrit bots are reused (no new accounts).
- `backend/agents/jira_dispatch.py` — `_GERRIT_AUTH_BY_CLASS`: map both conf classes to the existing `(claude-bot, gerrit-claude-bot-ed25519)` / `(codex-bot, gerrit-codex-bot-ed25519)` — the SAME Gerrit SSH identity; the *project* differs via `OMNISIGHT_GERRIT_URL`, not the key.
- Check `capability_matrix` / `_quota_provider_for_runner` resolve via the provider profile (they key off `AGENT_CLASS_PROFILE` → provider), so the conf classes inherit the same capabilities. Confirm no other place hard-enumerates the 4 legacy classes (grep `subscription-claude"|subscription-codex"` and add the conf variants or, better, derive from the maps).

### 2. Dedicated systemd instances
New units `runner-claude-conf@.service` / `runner-codex-conf@.service` (copies of the Family-F template) with extra env:
```
Environment=OMNISIGHT_RUNNER_CLASS=subscription-claude-conf
Environment=OMNISIGHT_GERRIT_URL=ssh://claude-bot@sora.services:29418/omnisight/conference-appliance
Environment=OMNISIGHT_JIRA_PROJECT_KEY=OP        # conf tickets stay in OP (Option 2)
```
A dedicated git mirror dir per project (the wrapper uses `--reference $MIRROR_DIR`; a conference mirror avoids object-cache confusion) OR verify `--reference` against the productizer mirror is harmless for a different repo (it is — `--reference` is just an object pool; unrelated objects are ignored). Prefer a conference mirror for cleanliness.

### 3. Re-class the 21 Case 5 tickets
Flip `class:subscription-claude` → `class:subscription-claude-conf` on OP-2169..2189 (codex variant where we want codex). After the conf runner is up + verified, add `agent:auto` + `capability:enable=gerrit_push` (pickup labels) — now safe because no normal runner matches the conf class.

## Roll-out order (each step reversible)
1. Land the 3-map change via Gerrit review (`refs/for/develop`, human +2). Pure addition; legacy classes untouched → zero behavior change for existing runners (the **safe floor**).
2. Provision conf systemd units (disabled). Start ONE conf instance.
3. Re-class ONE leaf ticket (e.g. P0.1 OP-2170) to the conf class + agent:auto. Verify: conf runner picks it, clones conference-appliance Gerrit, implements, pushes `refs/for/develop`; normal runners ignore it.
4. On green, re-class the rest + enable agent:auto on the unblocked leaves.

## Verification gates
- After step 1: existing runners still pick normal OP tickets (no regression); `python -c "import backend.agents.capability_registry"` + a unit test asserting the conf classes resolve to the right provider.
- After step 3: the conf ticket's Gerrit change appears under `omnisight/conference-appliance` (NOT productizer); GitLab mirror (stopgap timer) reflects it after submit.

## Risks / notes
- This edits the runner's own pickup/auth path → **mandatory human review** (L1). Change is additive (new map entries + new units), legacy paths bit-for-bit unchanged.
- Watch the [[feedback_class_label_reserved_agent_class]] trap: `class:` is the reserved agent-class selector — the conf class value must be exactly `subscription-claude-conf` and registered in all maps, or pickup silently no-matches.
- If any code path hard-enumerates the 4 legacy classes (not via `AGENT_CLASS_PROFILE`), it must learn the conf variants — the grep in step 1 is the gate.
