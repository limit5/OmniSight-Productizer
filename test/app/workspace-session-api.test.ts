/**
 * V0 #4 — Contract tests for `/api/workspace/[type]/session` route.
 *
 * Invokes the route handlers directly (no HTTP server needed).  Next's
 * app-router handlers accept a standard `Request` and a `{ params }`
 * wrapper, which is all we need to simulate from vitest.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest"

import {
  DELETE,
  GET,
  PUT,
  __resetWorkspaceSessionStoreForTests,
} from "@/app/api/workspace/[type]/session/route"

type RouteParams = { type: string }

function ctxFor(type: string): { params: Promise<RouteParams> } {
  return { params: Promise.resolve({ type }) }
}

function buildRequest(url: string, init?: RequestInit): Request {
  return new Request(url, init)
}

function validEnvelope(savedAt = "2026-04-18T10:00:00.000Z") {
  return {
    schemaVersion: 1,
    savedAt,
    state: {
      project: { id: "p-1", name: "Demo", updatedAt: savedAt },
      agentSession: { sessionId: "s-1", agentId: null, status: "idle", startedAt: null, lastEventAt: null },
      preview: { status: "ready", url: "http://preview.local/x", errorMessage: null, updatedAt: savedAt },
    },
  }
}

describe("/api/workspace/[type]/session — GET", () => {
  beforeEach(() => __resetWorkspaceSessionStoreForTests())
  afterEach(() => __resetWorkspaceSessionStoreForTests())

  it("returns 204 when there is no snapshot", async () => {
    const res = await GET(
      buildRequest("http://localhost/api/workspace/web/session"),
      ctxFor("web"),
    )
    expect(res.status).toBe(204)
  })

  it("returns 400 for an unknown workspace type", async () => {
    const res = await GET(
      buildRequest("http://localhost/api/workspace/desktop/session"),
      ctxFor("desktop"),
    )
    expect(res.status).toBe(400)
    const body = await res.json()
    expect(body.error).toBe("unknown_workspace_type")
    expect(body.message).toMatch(/web, mobile, software/)
  })

  it("returns 200 with the stored envelope after a PUT", async () => {
    const env = validEnvelope()
    const putRes = await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(env),
      }),
      ctxFor("web"),
    )
    expect(putRes.status).toBe(204)

    const getRes = await GET(
      buildRequest("http://localhost/api/workspace/web/session"),
      ctxFor("web"),
    )
    expect(getRes.status).toBe(200)
    expect(await getRes.json()).toEqual(env)
  })
})

describe("/api/workspace/[type]/session — PUT", () => {
  beforeEach(() => __resetWorkspaceSessionStoreForTests())
  afterEach(() => __resetWorkspaceSessionStoreForTests())

  it("returns 204 on success", async () => {
    const res = await PUT(
      buildRequest("http://localhost/api/workspace/mobile/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(validEnvelope()),
      }),
      ctxFor("mobile"),
    )
    expect(res.status).toBe(204)
  })

  it("returns 400 for an unknown workspace type", async () => {
    const res = await PUT(
      buildRequest("http://localhost/api/workspace/desktop/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(validEnvelope()),
      }),
      ctxFor("desktop"),
    )
    expect(res.status).toBe(400)
  })

  it("returns 400 when the body is not valid JSON", async () => {
    const res = await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: "{this is not json",
      }),
      ctxFor("web"),
    )
    expect(res.status).toBe(400)
    const body = await res.json()
    expect(body.error).toBe("invalid_json")
  })

  it("returns 400 when the envelope has the wrong schemaVersion", async () => {
    const bad = { schemaVersion: 99, savedAt: "2026-04-18T00:00:00Z", state: {} }
    const res = await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bad),
      }),
      ctxFor("web"),
    )
    expect(res.status).toBe(400)
    const body = await res.json()
    expect(body.error).toBe("invalid_envelope")
  })

  it("returns 400 when state is not an object", async () => {
    const bad = { schemaVersion: 1, savedAt: "2026-04-18T00:00:00Z", state: "oops" }
    const res = await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bad),
      }),
      ctxFor("web"),
    )
    expect(res.status).toBe(400)
  })

  it("overwrites an existing snapshot on subsequent PUTs", async () => {
    const first = validEnvelope("2026-04-18T10:00:00.000Z")
    const second = validEnvelope("2026-04-18T11:00:00.000Z")
    await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(first),
      }),
      ctxFor("web"),
    )
    await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(second),
      }),
      ctxFor("web"),
    )
    const getRes = await GET(
      buildRequest("http://localhost/api/workspace/web/session"),
      ctxFor("web"),
    )
    expect(await getRes.json()).toEqual(second)
  })

  it("isolates storage between workspace types", async () => {
    const env = validEnvelope()
    await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(env),
      }),
      ctxFor("web"),
    )
    const mobileRes = await GET(
      buildRequest("http://localhost/api/workspace/mobile/session"),
      ctxFor("mobile"),
    )
    expect(mobileRes.status).toBe(204)
  })

  it("drops unknown sub-states while accepting the valid ones", async () => {
    const mixed = {
      schemaVersion: 1,
      savedAt: "2026-04-18T00:00:00Z",
      state: {
        project: { id: "p", name: "n", updatedAt: null },
        agentSession: "not-an-object", // dropped
        preview: null, // dropped
      },
    }
    const putRes = await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mixed),
      }),
      ctxFor("web"),
    )
    expect(putRes.status).toBe(204)
    const getRes = await GET(
      buildRequest("http://localhost/api/workspace/web/session"),
      ctxFor("web"),
    )
    const body = await getRes.json()
    expect(body.state.project).toEqual({ id: "p", name: "n", updatedAt: null })
    expect(body.state.agentSession).toBeUndefined()
    expect(body.state.preview).toBeUndefined()
  })
})

describe("/api/workspace/[type]/session — DELETE", () => {
  beforeEach(() => __resetWorkspaceSessionStoreForTests())
  afterEach(() => __resetWorkspaceSessionStoreForTests())

  it("returns 204 and removes the stored snapshot", async () => {
    await PUT(
      buildRequest("http://localhost/api/workspace/web/session", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(validEnvelope()),
      }),
      ctxFor("web"),
    )
    const delRes = await DELETE(
      buildRequest("http://localhost/api/workspace/web/session", { method: "DELETE" }),
      ctxFor("web"),
    )
    expect(delRes.status).toBe(204)

    const getRes = await GET(
      buildRequest("http://localhost/api/workspace/web/session"),
      ctxFor("web"),
    )
    expect(getRes.status).toBe(204)
  })

  it("returns 400 for unknown workspace type", async () => {
    const res = await DELETE(
      buildRequest("http://localhost/api/workspace/desktop/session", { method: "DELETE" }),
      ctxFor("desktop"),
    )
    expect(res.status).toBe(400)
  })
})
