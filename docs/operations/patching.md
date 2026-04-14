# Patching — Operator Guide

> Phase 67-B. Audience: operators debugging agent edit failures,
> prompt engineers tuning the patch protocol.

## Why this exists

LLMs are expensive per-output-token and hallucinate more when they
emit long responses. A 3-line fix should not require re-emitting
a 2000-line file. The patch tool makes small edits **small**.

## The three tools

| Tool | When to use | Cap |
|---|---|---|
| `create_file(path, content)` | NEW files only. Refuses existing. | uncapped |
| `patch_file(path, patch_kind, payload)` | Edits to existing files. | N/A — diff-sized |
| `write_file(path, content)` | Legacy. OK for first-time writes; refuses existing-file overwrite > `OMNISIGHT_PATCH_MAX_INLINE_LINES`. | 50 lines (default) |

## `patch_kind`

`"search_replace"` — preferred for LLM-generated patches.
`"unified_diff"` — preferred for tool-generated patches (e.g.,
`git diff` output).

## Failure modes

| Return | Meaning | Fix |
|---|---|---|
| `[REJECTED] write_file on existing file … exceeds cap …` | Agent tried full-file overwrite | Re-emit as SEARCH/REPLACE |
| `[REJECTED] create_file on existing path …` | Path already has a file | Use `patch_file` instead |
| `[REJECTED] patch_file on missing path …` | Target doesn't exist yet | Use `create_file` first |
| `[PATCH-FAILED] PatchNotFound: …` | SEARCH didn't match | Add more surrounding context |
| `[PATCH-FAILED] PatchAmbiguous: matched N times` | SEARCH matched multiple places | Add more context to disambiguate |
| `[PATCH-FAILED] PatchMalformed: fewer than 3 …` | SEARCH had < 3 non-blank lines | Include more context |
| `[PATCH-FAILED] PatchMalformed: unbalanced …` | Missing a marker | Check `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` pairing |

## Env knobs

```bash
# Line-count cap on write_file overwrites of existing files. 0 is
# treated as "50" by the interceptor; to truly disable the cap, use
# a very large number (not recommended).
OMNISIGHT_PATCH_MAX_INLINE_LINES=50
```

## Relationship to IIS (Phase 63-A/B)

Patch failures and oversize-overwrite rejections feed the
`intelligence_score{dim="code_pass"}` Gauge as `code_pass=False`
observations for the offending agent. When that rolling rate drops
below the warning threshold, Phase 63-B files an `intelligence/calibrate`
Decision Engine proposal; the approved calibrate action re-injects
this module's prompt fragment so the next turn sees the protocol
reminder. Repeated failures escalate to L2 route (switch model).

## Canary rollout

The prompt fragment lives at `backend/agents/prompts/patch_protocol.md`
and is bootstrapped into `prompt_versions` by
`prompt_registry.bootstrap_from_disk()` on startup (Phase 56-DAG-C S3).
Operator edits follow the Phase 63-C canary flow:

1. Edit the markdown file + redeploy.
2. `bootstrap_from_disk` registers a new version (role=archive; old
   stays active).
3. Operator promotes via `prompt_registry.register_canary(path, body)`
   → 5% of agent_id hashes see v2.
4. `evaluate_canary(path)` at a later cadence promotes to active if
   pass-rate is within 5pp of baseline, else auto-rolls-back.

## Related

- `backend/agents/tools_patch.py` — the patcher
- `backend/agents/tools.py` — the `@tool` wrappers
- `backend/agents/prompts/patch_protocol.md` — the fragment to paste
  into an agent system prompt
- `docs/design/lossless-agent-acceleration.md` — Engine 2 rationale
