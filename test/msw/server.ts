/**
 * N3 — Node-side MSW server for vitest.
 *
 * Import this from a test and call `server.listen()` in `beforeAll`
 * (or use the helper below). We don't wire it into `test/setup.ts`
 * globally because the existing suite relies on direct fetch mocks
 * via `vi.stubGlobal`; forcing MSW on every test would change their
 * semantics. Contract tests opt in explicitly.
 */
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll } from "vitest"
import { handlers } from "./handlers"

export const server = setupServer(...handlers)

/** Use from a `describe` block to mount and tear down MSW. */
export function useMswServer() {
  beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
  afterEach(() => server.resetHandlers())
  afterAll(() => server.close())
}
