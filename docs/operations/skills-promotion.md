# Skill Promotion — Operator Guide

> Phase 62 (Knowledge Generation, L1 of agentic-self-improvement).
> Audience: operator deciding whether an auto-extracted skill should
> become part of the live agent skill library.

## What this is

When a workflow run finishes successfully and meets the difficulty
threshold (≥ 5 steps OR ≥ 3 retries), the backend writes a candidate
markdown file to `configs/skills/_pending/skill-<kind>-<run-id>.md`
and files a Decision Engine proposal of `kind=skill/promote`.

The candidate is **not yet active**. An operator must explicitly
promote (or discard) it.

## Enable / disable

```bash
# Off (default — extractor doesn't run at all)
unset OMNISIGHT_SELF_IMPROVE_LEVEL

# On — knowledge generation only
export OMNISIGHT_SELF_IMPROVE_LEVEL=l1

# On — L1 + L3 prompt-evaluator (Phase 63-C)
export OMNISIGHT_SELF_IMPROVE_LEVEL=l1+l3

# Everything (62 / 63 / 65)
export OMNISIGHT_SELF_IMPROVE_LEVEL=all
```

## Review workflow

```bash
# 1. List candidates awaiting review
curl http://localhost:8000/api/v1/skills/pending
# → {"items":[{"name":"skill-build-firmware-deadbeef.md","size_bytes":...}]}

# 2. Read one for review
curl http://localhost:8000/api/v1/skills/pending/skill-build-firmware-deadbeef.md
# → {"name":"...","body":"---\nname: ...\n---\n# Skill: ..."}
```

Review the markdown body — pay particular attention to:

| Check | Why |
|---|---|
| `trigger_kinds:` covers the right workflow kinds | otherwise the skill won't activate |
| `confidence:` is realistic | extractor seeds 0.5 conservatively |
| Failure modes block doesn't reveal internal hostnames | scrubber catches most but not all |
| Resolution path is generalisable, not a one-off fluke | otherwise it'll mislead future agents |

## Promote or discard

```bash
# 3a. Promote — moves into configs/skills/<slug>/SKILL.md
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/api/v1/skills/pending/skill-build-firmware-deadbeef.md/promote
# → {"slug":"build-firmware-deadbeef","path":"configs/skills/.../SKILL.md"}

# 3b. Or discard
curl -X DELETE -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/api/v1/skills/pending/skill-build-firmware-deadbeef.md
# → {"discarded":"skill-build-firmware-deadbeef.md"}
```

Both actions write to the audit log (Phase 53) — `skill_promoted` /
`skill_discarded` rows carry the slug + actor.

## Safety guarantees

- **Default-safe option** is `discard`, not `promote`. A timeout on
  the Decision Engine proposal means the candidate gets silently
  dropped rather than auto-installed.
- **Scrubber** redacts AWS / GitHub / GitLab / OpenAI / Anthropic /
  Slack tokens, JWTs, SSH private key blocks, env-style secrets,
  emails, /home /Users /root paths, and non-loopback IPv4 before
  the file is even written. If too many redactions fire (>25), the
  extractor refuses to write at all.
- **Path traversal** is blocked at the endpoint via
  `Path.resolve()` containment check.
- **Promotion requires admin role**; review (list/read) is operator+.

## Observability

| Metric | Meaning |
|---|---|
| `omnisight_skill_extracted_total{status}` | written / skipped_threshold / skipped_unsafe |
| `omnisight_skill_promoted_total` | operator-approved promotions |

Audit chain (Phase 53):

| action | actor | when |
|---|---|---|
| `skill_promoted` | admin email | POST .../promote returned 200 |
| `skill_discarded` | admin email | DELETE returned 200 |

## Common pitfalls

- **Forgot to set `OMNISIGHT_SELF_IMPROVE_LEVEL`** → extractor sits
  silent, `_pending/` stays empty. Verify with
  `curl /api/v1/skills/pending` after a known-eligible workflow
  completes.
- **Same workflow kind keeps producing skills** → operator should
  promote one, discard the rest. Live tree refuses duplicate slugs
  (409); rename the candidate before promoting.
- **Extractor wrote a candidate but the body looks unhelpful** →
  the v1 extractor is template-based, not LLM-rewritten. Edit the
  candidate file in-place under `_pending/` before promoting if the
  generated markdown needs polish.

## Related

- `docs/design/agentic-self-improvement.md` — L1-L4 design
- `backend/skills_extractor.py` — extraction logic
- `backend/skills_scrubber.py` — redaction patterns
- `backend/routers/skills.py` — review/promote/discard endpoints
