# lessons_distiller

**Purpose**: RPG.W5.2 — at `lessons_learned` write time (i.e. every successful `backend.db.insert_episodic_memory` call), compute a `<= 200` token markdown summary suitable for the L2 distilled-skill layer described in ADR-0008 *Memory hierarchy*. Pure-function module; no DB writes of its own.

**Key types / public surface**:
- `MAX_SUMMARY_TOKENS = 200` — ADR-0008 budget cap. Tight enough that L2 top-K retrieval can return several hits under the 2 KB pre-task injection budget.
- `estimate_tokens(text)` — Anthropic's ~4 chars/token heuristic; matches `backend.agents.stale_refresh_strategy.estimate_tokens`.
- `distill_lesson_summary(lesson)` — pure function. Accepts the same dict shape `insert_episodic_memory` consumes (`error_signature`, `solution`, optional `soc_vendor` / `sdk_version` / `hardware_rev` / `gerrit_change_id` / `tags`); returns a `<= MAX_SUMMARY_TOKENS` markdown string.
- `on_lesson_written(lesson)` — best-effort async hook fired from `insert_episodic_memory` after a successful INSERT. Returns the distilled summary (for inspection / contract tests); swallows exceptions because lesson recording is the load-bearing operation.

**Key invariants**:
- The returned summary always satisfies `estimate_tokens(summary) <= MAX_SUMMARY_TOKENS`. Oversized inputs are truncated with a `...` marker; the truncation budget reserves space for the marker.
- Secrets / PII are scrubbed via `backend.skills_scrubber.scrub` before the token-budget check, so the final string is safe to hand to the dim-memory writer.
- Lesson-write hook failure is non-fatal — the `episodic_memory` INSERT always wins. A failing distiller logs at `DEBUG` and returns `None`.

**Cross-module touchpoints**:
- Called by `backend.db.insert_episodic_memory` after the INSERT statement commits (per-row, sync within the same async task).
- Reuses `backend.skills_scrubber.scrub` so the redaction surface stays in one place (also used by `backend.skill_distiller`).
- L2 storage backend (BP.M dim memory) is a separate row (RPG.W5.1); this module produces the *content* that backend will eventually persist.
