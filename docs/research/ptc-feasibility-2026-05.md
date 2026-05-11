# Spike Report — Programmatic Tool Calling Feasibility (OP-861)

**Date**: 2026-05-11
**Scope**: Spike-only research for runner tool orchestration. No production integration. Deliverables: this report, `scripts/spike_ptc_compare.py`, and focused tests in `backend/tests/test_spike_ptc_compare.py`.
**Out-of-area domains untouched**: db, devops, embedded, frontend, security, tooling platform code. The new script is explicitly listed in OP-861's "Files touched" section and does not change runner/tooling behavior.
**Reference inputs**:
- Anthropic PTC docs: `https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling`
- Anthropic PTC cookbook: `https://platform.claude.com/cookbook/tool-use-programmatic-tool-calling-ptc`
- Existing runner surfaces: `backend/agents/tool_schemas.py`, `backend/agents/tool_dispatcher.py`, `backend/agents/tools.py`, `scripts/run_s1_via_anthropic_sdk.py`
- Prior discipline: L-OP-843 says vendor feature announcements must be disambiguated before deleting local resilience.

---

## TL;DR — Recommendation

**Partial integrate later, read-only only.** PTC is a good fit for runner discovery/preflight workflows that batch 3+ idempotent reads and then return a compact summary. It is not safe as a broad runner replacement because most valuable runner actions are writes, external effects, nested agents, MCP connectors, or arbitrary shell execution.

Recommended follow-up: a small opt-in child that exposes a **read-only PTC bundle** for locator/preflight flows:

- `jira_fetch`, `gerrit_query`, `gh_pr_view`, `gh_issue_view`, `Read`, `Grep`, `Glob`, and read-only git state helpers.
- No write tools, no JIRA transitions/comments, no Gerrit push/review, no `Agent`, no MCP connector tools, and no arbitrary `Bash`.
- Feature flag default OFF until live Anthropic sandbox behavior is verified against our runner credentials and logging.

Do not integrate PTC into coder/submit/review actions yet. The safety boundary is not the syntax; it is repeatability and side effects.

---

## 1. PTC Docs Verification

Anthropic's current docs define Programmatic Tool Calling as Claude writing Python code inside a code execution container that calls user tools, instead of sampling the model between each tool invocation.

Verified API surface as of 2026-05-11:

| Item | Current docs state | Impact |
|---|---|---|
| Feature flag / tool version | First-party Claude API docs show GA-style use with `{"type": "code_execution_20260120", "name": "code_execution"}`. Error table still mentions `missing_beta_header` for Bedrock / Vertex AI. | Our direct Claude API path should model no extra beta header, but provider adapters need a `missing_beta_header` fallback. |
| Supported models | `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-opus-4-5-20251101`, `claude-sonnet-4-5-20250929`. | Runner default `claude-sonnet-4-6` is compatible. |
| Syntax | Include code execution as a tool and add `allowed_callers: ["code_execution_20260120"]` to tools callable from the sandbox. Programmatic `tool_use` blocks include `caller.type == "code_execution_20260120"`. | We can gate eligibility per tool at schema assembly time. |
| Key limitation | MCP connector tools cannot currently be called programmatically. Structured-output strict tools, forced `tool_choice`, and `disable_parallel_tool_use` are incompatible. | MCP-backed surfaces must stay direct for now. |
| Data retention | PTC is not ZDR eligible and uses code-execution container retention. | Do not pass sensitive ticket secrets or security-domain material into PTC. |

The cookbook's older example uses `code_execution_20250825`; the docs page has moved to `code_execution_20260120`. The spike harness intentionally uses the newer docs page value.

Error catalog handling:

- `PTCSchemaUnexpected`: raise/pause if live API rejects `code_execution_20260120`, omits `caller`, or requires a beta header on first-party Claude API contrary to docs.
- `PTCExecutionRefused`: document the model/sandbox refusal reason and fall back to direct runner reads. Do not retry the same PTC request more than once without changing the tool allowlist or prompt.

---

## 2. Runner Tool Catalog

Classification rule:

- **PTC-safe** = idempotent read, structured enough to parse in Python, no external mutation, no user-visible side effect, and no connector limitation.
- **Not PTC-safe** = local write, external write, arbitrary execution, nested agent execution, long-running target workflow, or MCP connector.

| Tool | Class | PTC-safe? | Reason |
|---|---|---:|---|
| `Read` | idempotent_read | Yes | Filesystem read. |
| `Grep` | idempotent_read | Yes | Filesystem search. |
| `Glob` | idempotent_read | Yes | Filesystem glob. |
| `git_status` | idempotent_read | Yes | Local git state read. |
| `git_log` | idempotent_read | Yes | Git history read. |
| `git_diff` | idempotent_read | Yes | Diff read. |
| `git_diff_staged` | idempotent_read | Yes | Diff read. |
| `git_branch` | idempotent_read | Yes | Branch read. |
| `jira_fetch` | idempotent_read | Yes | Ticket read. |
| `gerrit_query` | idempotent_read | Yes | Gerrit change read. |
| `gh_pr_view` | idempotent_read | Yes | GitHub PR read. |
| `gh_issue_view` | idempotent_read | Yes | GitHub issue read. |
| `mcp_list` | idempotent_read | No | Anthropic docs say MCP connector tools cannot currently be called programmatically. |
| `mcp_search` | idempotent_read | No | Same MCP connector limitation. |
| `WebSearch` | idempotent_read | No | External network and injection-prone results; keep model-visible. |
| `Skill` | idempotent_read | Yes | Loads checked-in skill text; safe if output is bounded. |
| `Agent` | unsafe_for_ptc | No | Spawns nested model/tool loop. |
| `Write` | local_side_effect | No | Mutates worktree. |
| `Edit` | local_side_effect | No | Mutates worktree. |
| `Bash` | unsafe_for_ptc | No | Arbitrary command surface. |
| `git_add` | local_side_effect | No | Mutates index. |
| `git_commit` | local_side_effect | No | Mutates history. |
| `git_push` | external_side_effect | No | Remote mutation. |
| `gerrit_push` | external_side_effect | No | Remote mutation. |
| `gerrit_post_comment` | external_side_effect | No | Review-visible write. |
| `gerrit_submit_review` | external_side_effect | No | Review-visible write / score. |
| `jira_update` | external_side_effect | No | Ticket mutation. |
| `jira_comment` | external_side_effect | No | Ticket mutation. |
| `jira_transition` | external_side_effect | No | Workflow mutation. |
| `run_tests` | local_side_effect | No | Host process execution; keep in runner. |
| `run_simulation` | unsafe_for_ptc | No | Long-running target/toolchain workflow. |
| `image_generate` | external_side_effect | No | Remote generation call. |

Summary from `scripts/spike_ptc_compare.py`:

- Cataloged tools: 32
- PTC-safe: 13
- Safe fraction: 40.6%
- Idempotent reads: 16, but 3 are excluded (`mcp_*`, `WebSearch`) because docs and injection risk make them poor PTC candidates.

---

## 3. Gap Analysis

PTC-safe workflow segments:

- Ticket/context preflight: fetch issue, parent, linked tickets, related Gerrit/GitHub changes, then summarize.
- Locator read pass: `Read` + `Grep` + `Glob` over candidate files, returning only candidate paths and line anchors.
- Git state summarization: status/log/diff reads collapsed into a short "what changed" JSON.
- Skill lookup: load and filter checked-in skill text before adding a compact plan hint to context.

Not PTC-safe workflow segments:

- Coder edits: `Write`, `Edit`, text-editor mutation, shell writes, and git index changes.
- Submit/review: `git_push`, `gerrit_push`, `gerrit_submit_review`, `jira_comment`, `jira_transition`.
- Nested delegation: `Agent` would hide a separate model/tool loop inside a sandbox call.
- MCP connector use: docs currently exclude MCP connector tools from programmatic calls.
- Any workflow requiring immediate operator-visible progress events.

Fraction of runner workflow that could move to PTC:

- **Tool catalog by count**: 13/32 tools are candidates (40.6%).
- **End-to-end runner by phase**: about 20-30% of a normal ticket's tool turns are good candidates, concentrated in preflight/locator/review-read phases. The write/submit phase should remain direct.
- **High-value use case**: read-only fan-out with 3+ dependent calls. PTC is not worth the code-execution overhead for single reads.

Risk notes:

- PTC hides intermediate tool results from model context by design. This is good for token cost, but bad if the operator needs every intermediate result visible for audit.
- Tool results are strings passed into executable Python context. Tool output validation is mandatory before any broader rollout.
- Container retention and non-ZDR eligibility rule out security-sensitive material.

---

## 4. Minimal PTC Sample

Workflow: **fetch ticket + fetch parent META + fetch related Gerrit changes**.

Programmatic procedure modeled by the harness:

```python
async def _claude_code():
    ticket = await jira_fetch("OP-861")
    parent_key = ticket["parent"]
    parent = await jira_fetch(parent_key)
    changes = await gerrit_query(f'topic:{ticket["key"]} OR topic:{parent_key}')
    merged = [c for c in changes if c.get("status") == "MERGED"]
    print(json.dumps({
        "ticket": ticket["key"],
        "parent": parent["key"],
        "acceptance_count": ticket["acceptance_count"],
        "merged_related_changes": len(merged),
    }, sort_keys=True))
```

Offline comparison from `python3 scripts/spike_ptc_compare.py --json /tmp/op861.json --output /tmp/op861.md`:

| Mode | Model turns | Tool calls | Input tokens | Output tokens | Wall s |
|---|---:|---:|---:|---:|---:|
| Traditional direct tool calling | 4 | 3 | 995 | 263 | 8.25 |
| PTC modeled path | 2 | 3 | 769 | 113 | 5.85 |

Modeled delta:

- Input tokens: 22.7% lower
- Output tokens: 57.0% lower
- Wall time: 29.1% lower
- Model turns: 2 fewer

The savings are modest because the sample data is small. The same pattern gets more valuable when raw tool output grows, which matches Anthropic's docs and cookbook: PTC wins when intermediate results are large, repeated, or need filtering before context.

Fallback behavior:

- If PTC is unavailable/refused, the harness returns `ptc-fallback` metrics identical to the direct path and records `PTC feature unavailable or disabled`.
- This is the required production posture: feature flag off or refused should not block the runner's read-only workflow.

---

## 5. Decision

**Decision: partial / defer production integration.**

Integrate only after a follow-up ticket adds:

1. A read-only PTC allowlist builder with explicit `allowed_callers: ["code_execution_20260120"]`.
2. Live API smoke gated by feature flag and credentials, including `PTCSchemaUnexpected` and `PTCExecutionRefused` handling.
3. Per-call audit logging of `caller.type` so direct vs programmatic calls are distinguishable.
4. A hard denylist for side-effect tools and MCP connector tools.
5. Token/latency telemetry comparing direct and PTC paths on real runner preflights.

Do not use PTC for writes, comments, transitions, pushes, shell, or nested agents. The current runner's explicit orchestration remains the right boundary for side effects.

---

## 6. Reviewer Pointers

- Harness: `scripts/spike_ptc_compare.py`
- Tests: `backend/tests/test_spike_ptc_compare.py`
- Existing built-in PTC sandbox boundary code for context: `backend/agents/tool_dispatcher.py` (`PTCSandbox`, `ptc_sandbox_handler`)
- Existing launcher PTC spec for context: `scripts/run_s1_via_anthropic_sdk.py` (`BUILT_IN_TOOLS_SPEC`)
- Official docs lines to re-check during any follow-up:
  - Model/tool version and docs shape: PTC requires `code_execution_20260120` and supports Claude 4.5/4.6/4.7 family models.
  - `allowed_callers` and `caller` fields are the enforcement/audit surface.
  - MCP connector tools are excluded from programmatic calling.
  - Data retention is inherited from code execution containers and is not ZDR.
