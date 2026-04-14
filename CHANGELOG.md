# Changelog

All notable changes to OmniSight Productizer are recorded here. Format
is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) —
sections are grouped by intent rather than strict SemVer (the project
ships from `master`, phase numbers in `HANDOFF.md` are the authoritative
history).

## Unreleased

### Added
- `/api/v1/system/sse-schema` endpoint — JSON-Schema export for every
  SSE event type, for TS↔backend drift detection.
- Decision rules now persist to SQLite (`decision_rules` table) and
  reload on backend startup; operator edits survive restart.
- Sliding-window rate limit (30 req / 10 s per client IP, configurable
  via `OMNISIGHT_DECISION_RL_{WINDOW_S,MAX}`) on decision mutator
  endpoints.
- `/docs` FastAPI Swagger UI is accessible (was always there, now
  linked from README).
- Playwright deep-link E2E specs (`?panel=timeline`, `?decision=…`,
  invalid panel fallback).
- Smoke tests for EmergencyStop / NeuralGrid / LanguageToggle.
- ToastCenter: overflow chip (+N MORE PENDING), visible countdown
  with urgency pulse, Page-Visibility-aware tick loop.
- DecisionDashboard: empty-state illustration, skeleton rows during
  initial load, HTTP-class-tagged error banner (AUTH / RATE LIMITED /
  BACKEND DOWN / NETWORK), keyboard-navigable tablist.
- `prefers-reduced-motion` honoured across neural-grid and toast
  pulses.
- `color-scheme: dark` declaration so native form controls stay dark
  under OS Light mode (app is intentionally dark-only).
- `.env.example`: all `OMNISIGHT_DECISION_*`, `BASH_TIMEOUT`,
  `SDK_CLONE_MAX_MB`, `GIT_CREDENTIALS_FILE` documented.
- `README.md`: Theme section, mandatory `.env` setup step, `/docs`
  pointer, docker compose alternative.

### Changed
- `decision_engine.propose()` now validates option ids (non-empty
  strings, unique, default present). Garbage-in callers get a
  `ValueError` instead of silent misbehaviour.
- `decision_rules.apply()` failures are logged at `warning` and
  surfaced via `decision.source.rule_engine_error` so operators
  can see when a proposal fell back to the mode × severity policy.
- Mobile nav quick-access dots expanded to 44×44 hit targets
  (visual dot stays 8 px) to meet WCAG 2.5.5.
- Destructive-severity approve/reject now require `window.confirm()`.
- Toast `aria-live` upgraded from `polite` to `assertive` +
  `aria-atomic`.
- Header / panel layout stability sweep — every dynamic-width
  element in the dashboard header (WSL2 / USB / MODE error / ARCH
  chip / EmergencyStop) and 5 panel headers (task-backlog,
  decision-dashboard, budget-strategy, pipeline-timeline,
  decision-rules-editor, host-device-panel) now occupies a fixed
  box. Status changes never reflow neighbouring elements. See
  HANDOFF "Phase 50-Layout" for the design rules.

### Fixed
- `decision_engine._reset_for_tests()` referenced deleted globals
  (`_parallel_sema`/`_parallel_cap_for_sema`); test teardown
  actually works again.
- `mobile-nav.tsx` no longer crashes on an invalid `?panel=` value
  — falls back to the first panel.
- `toast-center.tsx` validates `deadline_at` numeric/positive and
  auto-detects ms-vs-seconds payloads; countdown no longer NaNs.
- `lib/api.ts streamInvoke/streamChat` surface trailing-buffer
  truncation as an explicit `stream_truncated` error frame instead
  of silently discarding it as clean EOF; reader lock released in
  `finally`.
- `app/page.tsx` deep-link hydration mismatch: server and first
  client render now agree on the initial panel (`orchestrator`),
  URL sync happens after hydration.
- `backend/db._migrate()` PRAGMA failures raise `RuntimeError`
  instead of being logged and swallowed.
- `_agent_error_history` ring buffer mutation guarded by a
  `threading.Lock` so the watchdog cannot observe a half-mutated
  list during trim-and-append.
- `budget-strategy-panel` error banner auto-clears after 10 s.

### Deprecated / Removed
- Nothing yet.

### Security
- Optional bearer-token gate on decision mutators
  (`OMNISIGHT_DECISION_BEARER`) now rate-limited to prevent
  brute-forcing short tokens.

See `HANDOFF.md` for the full per-phase audit-fix narrative and
`docs/sop/` for contributor-facing conventions.
