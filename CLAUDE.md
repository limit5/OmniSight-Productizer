# OmniSight Core Rules (L1 Memory — Immutable)

These rules are injected into EVERY agent prompt. They cannot be overridden.

## Compilation Rules
- When cross-compiling for a target SoC, ALWAYS use the platform toolchain from `get_platform_config`. NEVER use the system default gcc.
- If a CMake toolchain file exists for the platform, ALWAYS pass `-DCMAKE_TOOLCHAIN_FILE=...`.
- If a vendor sysroot is mounted, ALWAYS use `--sysroot=...`.

## Code Quality Rules
- All C/C++ code must pass `checkpatch.pl --strict` before commit.
- All commits must include a descriptive message referencing the task ID.
- Memory safety: run Valgrind on all algo-track simulations. Zero leaks required.

## Safety Rules
- NEVER modify files in `test_assets/` — they are read-only ground truth.
- NEVER force-push to main/master branches.
- NEVER bypass Gerrit Code Review. AI reviewer max score is +1. Human +2 required for merge.
  - **Exception (O6 Merger Agent, #269):** The `merger-agent-bot` account MAY cast Code-Review: +2 on patchsets it produced to resolve merge conflicts, **scoped strictly to the correctness of the conflict-resolution block**. The merger's +2 never substitutes for a human +2 — the O7 submit-rule enforces a dual-sign gate: at least one +2 from `merger-agent-bot` AND at least one +2 from a member of the `non-ai-reviewer` group. Any AI reviewer (merger / lint-bot / security-bot / future AIs) in addition to the merger adds no extra authority — a human +2 remains the hard gate for submission.
- NEVER store API keys, tokens, or secrets in source code or commits.

## Agent Behavior
- When retrying after failure, always analyze the error before attempting the same approach.
- After 2 identical errors, escalate to human instead of retrying.
- When completing a task, always generate HANDOFF.md with the resolution summary.
- Answer in the same language as the user's question.
