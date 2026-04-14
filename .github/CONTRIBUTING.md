# Contributing to OmniSight Productizer

Thanks for helping improve the command center. This file is terse on
purpose — it covers the non-obvious conventions that come up during
every PR review. For architecture and phase history see
`HANDOFF.md`; for operator-facing features see `README.md`.

## Before you start

- Read `CLAUDE.md` — the core rules (checkpatch, no force-push,
  platform toolchain for cross-compile, test_assets read-only) are
  baked into CI.
- For any backend change that touches `pipeline.py`, `decision_*.py`,
  `events.py`, or the agent graph, follow
  `docs/sop/implement_phase_step.md` end-to-end.
- If you're fixing a bug, first reproduce it with a test; only then
  write the fix. See `feedback_debug_hypothesis` in the team memory
  for the scientific-method expectation.

## Branch & commit

- Work on `master` or a short-lived topic branch; we squash-merge.
- Commit messages: imperative subject ≤ 72 chars, then a blank line,
  then a short body explaining **why**. Reference audit ids when
  touching audit-fix work (e.g. `R2 #20`, `B12`, `A4/C7`).
- Do NOT force-push to `master` or to shared review branches.
- Do NOT bypass pre-commit hooks (`--no-verify`) — fix the hook
  failure instead.

## Tests

- Backend: `pytest backend/tests/ -x` for fast feedback;
  scope to a specific module during development (full suite can
  take 60–180 min and times out in CI shards).
- Frontend unit: `npx vitest run test/`.
- Frontend E2E: `npx playwright test` — requires backend + next
  dev running. `playwright.config.ts` boots them automatically.
- A PR that claims "fixes X" without a test regression reproducer
  will be asked for one.

## Style

- Python: 4-space indent, type hints on every new function.
  `logger = logging.getLogger(__name__)` at module top.
- TypeScript: strict mode; no `any` unless unavoidable; no
  `@ts-ignore` without a comment explaining why.
- React: default to Server Components; use `"use client"` only
  when state/effects are needed.
- CSS: prefer Tailwind tokens + `var(--…)` over hex codes; the
  theme is dark-only (see README → Theme).

## Adding an env var

Any new `OMNISIGHT_*` env var MUST appear in `.env.example` with a
one-line comment, otherwise downstream operators have no way to
discover it. The audit found 7 undocumented entries — we're
keeping the list at zero going forward.

## Adding an SSE event

1. Add the Pydantic model to `backend/sse_schemas.py` and register it
   in `SSE_EVENT_SCHEMAS`.
2. Add the event name to `SSE_EVENT_TYPES` in `lib/api.ts`.
3. Extend `SSEEvent` union with the payload type.
4. Emit from the backend via `bus.publish("…", {…})`.
5. Consumers should subscribe via `subscribeEvents()`.

CI compares the two lists; mismatches fail the build. (If the CI
check doesn't exist yet, consider adding it while you're here.)

## Audit trail

Findings from the three deep-audit passes are tracked inline in the
`Unreleased` section of `CHANGELOG.md` and summarised per-phase in
`HANDOFF.md`. When closing an audit id, either:

- Fix it and mention the id in the commit message, OR
- Mark it wontfix/design-decision with the rationale in the commit
  message so the next auditor doesn't re-raise it.

Do NOT ignore audit ids silently.
