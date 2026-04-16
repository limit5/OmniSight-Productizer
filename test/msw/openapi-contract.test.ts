/**
 * N3 — Contract smoke test.
 *
 * Verifies that:
 *   1. The MSW + openapi-msw wiring actually intercepts `lib/api.ts`
 *      calls through the same wire format the backend speaks.
 *   2. The fixtures compile against the generated schema (enforced at
 *      `handlers.ts` module load).
 *
 * If this test fails, either MSW is mis-wired or a fixture drifted
 * from the schema — both indicate a real contract regression.
 */
import { describe, expect, it, beforeAll, afterAll, afterEach } from "vitest"
import { server } from "./server"
import { fixtures } from "./handlers"
import { listAgents, listTasks } from "@/lib/api"

describe("openapi contract — msw handlers", () => {
  beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
  afterEach(() => server.resetHandlers())
  afterAll(() => server.close())

  it("listAgents matches the generated Agent schema", async () => {
    const agents = await listAgents()
    expect(agents).toHaveLength(1)
    expect(agents[0].id).toBe(fixtures.sampleAgent.id)
    // Load-bearing field presence: if FastAPI drops any of these, the
    // handlers.ts `satisfies` would have broken compile already.
    expect(agents[0]).toMatchObject({
      id: expect.any(String),
      name: expect.any(String),
      status: expect.any(String),
    })
  })

  it("listTasks matches the generated Task schema", async () => {
    const tasks = await listTasks()
    expect(tasks).toHaveLength(1)
    expect(tasks[0].id).toBe(fixtures.sampleTask.id)
    expect(tasks[0]).toMatchObject({
      id: expect.any(String),
      title: expect.any(String),
      priority: expect.any(String),
      status: expect.any(String),
    })
  })
})
