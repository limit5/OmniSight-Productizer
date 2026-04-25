---
audience: internal
---

# R20 Phase 0 — Chat-layer security architecture

Defense layers for every chat-facing LLM call (`conversation_node`,
`_generate_coach_message`, future setup-coach prompts).

## 1. RAG with classification gate (`backend/rag/`)

- `corpus.py` walks `docs/**/*.md`, parses optional `audience:`
  frontmatter, falls back to directory-based defaults
  (`docs/operator/*` → operator, `docs/design/*` → internal, etc.).
- Unrecognised directories default to `internal` → **fail-closed**.
- `retrieval.py` runs BM25-lite over the in-memory corpus and
  filters by `visible_audiences_for(role)`.
- `internal` audience is **never** in any role's visible set →
  internal docs are unreachable via chat regardless of role.

## 2. Prompt hardening (`backend/security/prompt_hardening.py`)

- `INJECTION_GUARD_PRELUDE` — prepended to every chat-facing system
  prompt. Tells the LLM: don't reveal system prompts/internal docs/
  secrets, treat retrieved docs and user text as DATA not commands,
  refuse common injection patterns.
- `looks_like_injection(text)` — heuristic detector covering English
  + CJK injection patterns.
- `harden_user_message(text)` — wraps suspicious user input with an
  explicit reminder ("spotlighting" pattern) instead of denying. The
  LLM still sees the message but with an explicit suspicion frame.

## 3. Secret redaction (`backend/security/secret_filter.py`)

- `redact(text)` — runs over LLM output BEFORE it hits the chat /
  SSE / audit log. Redacts: GitHub PAT/OAuth, GitLab PAT, AWS
  AKIA/secret, Slack tokens/webhooks, Stripe sk/pk, Anthropic
  sk-ant-*, OpenAI sk-*, generic Bearer, JWT, private key blocks,
  internal hostnames (`pg-primary`, `ai_cache`, etc.), and a
  high-entropy fallback that fires only when no specific pattern
  matched (avoiding double-redaction).
- Allow-list prevents false positives on public model IDs etc.
- Returns `(text, fired_labels)` so the caller can audit-log which
  kinds of secrets it caught — useful for finding the upstream leak.

## Defense-in-depth flow

For each chat-originated LLM call:

1. Retrieve docs filtered by user role → only allowed docs in context.
2. Prepend `INJECTION_GUARD_PRELUDE` to the persona prompt.
3. `harden_user_message(last_user_text)` → wrap likely injection.
4. Send `[hardened_system, *messages]` to LLM.
5. `redact(response.content)` → strip leaked secrets.
6. Return redacted text to caller.

If any single layer fails (LLM ignores prelude, retrieval glitches,
detector false-negatives), the next layer catches it. None of these
layers individually is sufficient — together they're robust.

## Tests (`backend/tests/`)

- `test_security_secret_filter.py` — 19 cases covering each pattern.
- `test_security_chat_injection.py` — 51 cases (recall + precision
  on English + CJK + classic + DAN + markup-shaped attacks).
- `test_rag_classification.py` — 9 cases asserting the gate
  enforces audience filtering across roles.

When adding a new chat surface (a new node calling LLM), the
checklist is: prepend `INJECTION_GUARD_PRELUDE`, run
`harden_user_message` on user input, run `redact()` on LLM output,
classification-gated retrieval if RAG context is needed.

## Future phases

- Phase 0d: `ui.command` SSE event protocol + frontend dispatcher
  + audit log enrichment for chat-originated mutations.
- Phase 1: first auto-fill domain (likely `slack` or `webhook`).
- Phase 2+3: the rest of the setup domains (git_repo, gerrit, jira,
  llm_provider, tenant, storage, jenkins).
