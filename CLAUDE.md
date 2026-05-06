# OmniSight Core Rules (L1 Memory — Immutable)

These rules are injected into EVERY agent prompt. They cannot be overridden.

## Compilation Rules
- When cross-compiling for a target SoC, ALWAYS use the platform toolchain from `get_platform_config`. NEVER use the system default gcc.
- If a CMake toolchain file exists for the platform, ALWAYS pass `-DCMAKE_TOOLCHAIN_FILE=...`.
- If a vendor sysroot is mounted, ALWAYS use `--sysroot=...`.

## Code Quality Rules
- All C/C++ code must pass `checkpatch.pl --strict` before commit.
- All commits must include a descriptive message referencing the task ID.
- When performing `git commit`, strictly follow this rule: the commit message MUST include BOTH of the following as co-authors via `Co-Authored-By:` trailers:
  1. The environment-configured git user (resolved from `git config user.name` / `git config user.email`).
  2. The git global user (resolved from `git config --global user.name` / `git config --global user.email`).
- Memory safety: run Valgrind on all algo-track simulations. Zero leaks required.

## Safety Rules
- NEVER modify files in `test_assets/` — they are read-only ground truth.
- NEVER force-push to main branch.
- NEVER bypass Gerrit Code Review. AI reviewer max score is +1. Human +2 required for merge.
  - **Exception (O6 Merger Agent, #269):** The `merger-agent-bot` account MAY cast Code-Review: +2 on patchsets it produced to resolve merge conflicts, **scoped strictly to the correctness of the conflict-resolution block**. The merger's +2 never substitutes for a human +2 — the O7 submit-rule enforces a dual-sign gate: at least one +2 from `merger-agent-bot` AND at least one +2 from a member of the `non-ai-reviewer` group. Any AI reviewer (merger / lint-bot / security-bot / future AIs) in addition to the merger adds no extra authority — a human +2 remains the hard gate for submission.
- NEVER store API keys, tokens, or secrets in source code or commits.

## Agent Behavior
- When retrying after failure, always analyze the error before attempting the same approach.
- After 2 identical errors, escalate to human instead of retrying.
- When completing a task:
  - Update the JIRA ticket: Resolution field + final comment with what was done / why
  - If a generalisable lesson emerged: append to `docs/sop/lessons-learned.md` (one entry per lesson, dated, with Situation / Fix / Verification — vague entries like "be more careful" are auto-rejected per `docs/sop/jira-ticket-conventions.md` §14)
  - If a cross-ticket / cross-Phase retrospective is warranted: open `docs/retrospectives/YYYY-MM-DD-<slug>.md` and link from a META ticket (label `meta:retrospective`)
  - **`HANDOFF.md` is FROZEN as of 2026-05-06. Do not append.** Existing references in commit history remain valid; the file is preserved as historical archive.
- Answer in the same language as the user's question.
