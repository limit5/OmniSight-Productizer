# Frontend Test Suite

Vitest (jsdom + @testing-library/react) for unit & component tests,
Playwright for browser-level E2E. Backend has its own pytest suite
under `backend/tests/`.

## Layout

```
test/
├── setup.ts                       # jest-dom matchers + MockEventSource polyfill
├── smoke.test.ts                  # pipeline-only sanity checks
├── alias-sync.test.ts             # tsconfig ↔ vitest `@/` cross-check
├── helpers/
│   └── sse.ts                     # primeSSE — mock subscribeEvents in component tests
├── components/                    # RTL component tests, one file per component
│   ├── mode-selector.test.tsx
│   ├── budget-strategy-panel.test.tsx
│   └── decision-dashboard.test.tsx
└── integration/                   # imports real lib modules (no full @/lib/api mock)
    └── sse-shared-stream.test.ts  # shared-SSE ref-counting contract

e2e/                               # Playwright specs (live backend + next start)
└── decision-happy-path.spec.ts
```

## Commands

| Command                 | What it does                                |
| ----------------------- | ------------------------------------------- |
| `npm test`              | vitest run (unit + component + integration) |
| `npm run test:watch`    | vitest in watch mode                        |
| `npm run test:coverage` | vitest + v8 coverage, enforces threshold    |
| `npm run test:e2e`      | playwright (builds frontend + starts both)  |
| `npm run test:ci`       | coverage gate → then E2E                    |

Playwright needs extra shared libs on minimal Linux hosts. Export
`OMNISIGHT_PW_LIB_DIR=$HOME/.local/lib/playwright-deps` (contains
`libnss3.so` / `libnspr4.so` / `libasound2.so`) before running.

Enable cross-browser E2E with `OMNISIGHT_PW_BROWSERS=all` — default is
chromium-only for speed.

## Conventions

### Component tests

- `vi.mock("@/lib/api", () => ({ ...vi.fn() for each used export }))` at
  the top of the file. Every REST helper used in the file must be
  listed; missed mocks hit the real fetcher.
- Import `primeSSE` from `../helpers/sse` to stub `subscribeEvents`. It
  returns `{ emit, closeCount }` — do not reinvent in each test.
- For async state, prefer `findByText` / `findByRole` over polling
  `waitFor`. Use `waitFor` only when asserting on something that
  already rendered and then changes.

### Fake timers

- Always wrap in `try { ... } finally { vi.useRealTimers() }`. An
  assertion failure must not leak fake timers into the next test.
- When the component's internal `setInterval` needs to fire, use
  `vi.useFakeTimers({ shouldAdvanceTime: true })` so mocked promises
  can still resolve.
- Avoid mixing `vi.waitFor` with fake timers — vitest 4 has a known
  race between the waitFor real-timer poll and the component's
  fake-timer callbacks. Advance synchronously inside `act()` and
  assert directly.

### MockEventSource (test/setup.ts)

The global `EventSource` in tests is a stub with:

- `emit(type, data)` — dispatch to both `addEventListener` listeners
  AND the matching property handler (`onmessage` for "message")
- `emitOpen()` / `emitError()` — open / error handshake
- `close()` — clears listeners so cross-test leaks cannot fire

Reach the latest instance via `MockEventSource.latest()` or the
global `__MockEventSource` symbol.

### Integration tests (`test/integration/*`)

Opposite of component tests: *do not* blanket-mock `@/lib/api`. Swap
the global `EventSource` before `await import("@/lib/api")` so the
real SSE manager uses your tracked stub. `vi.resetModules()` in
`beforeEach` defeats the module-scoped singleton.

### Playwright

- Use `next build && next start`. Next 16 dev mode (both Turbopack and
  webpack) leaves onClick handlers unhydrated intermittently under E2E,
  so dev mode is not safe for UI-click assertions.
- For dual-rendered components (e.g. `<ModeSelector compact />` + full),
  chain `.filter({ visible: true })` so the click lands on the painted
  twin, not its `md:hidden` sibling.
- Prefer asserting backend state first (`expect.poll` on the REST
  endpoint) and the UI second — the backend is the durable contract
  and bypasses React re-render jitter.

### Coverage scope

`vitest.config.ts` scopes coverage to the Phase 48 Autonomous-Decision
surface only (mode-selector, budget-strategy-panel, decision-dashboard).
Expanding this scope is Phase 51 work; raise thresholds carefully to
avoid a perma-red gate from un-tested legacy files.

## Adding a new test file

1. Pick the right folder: components (wraps RTL render), integration
   (real modules), e2e (Playwright).
2. Copy the nearest sibling as a template.
3. Name the file `<subject>.test.ts[x]`.
4. Run `npm test -- <subject>` while iterating.
5. Run `npm run test:ci` before opening a PR.
