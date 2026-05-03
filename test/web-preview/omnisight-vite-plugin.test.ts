/**
 * W15.1 — Contract tests for `packages/omnisight-vite-plugin`.
 *
 * Pins the plugin's wire shape, schema-version drift guard against
 * the backend literal, payload-builder semantics, best-effort
 * transport, and the runtime-overlay HTML injection.  The Python
 * sibling lives at `backend/tests/test_w15_1_vite_error_endpoint.py`
 * and locks the receiving end.
 */

import { describe, it, expect, vi } from "vitest"

import {
  ALLOWED_PHASES,
  MESSAGE_MAX_BYTES,
  OMNISIGHT_VITE_ERROR_SCHEMA_VERSION,
  OMNISIGHT_VITE_PLUGIN_NAME,
  OMNISIGHT_VITE_PLUGIN_VERSION,
  STACK_TRACE_MAX_BYTES,
  buildErrorEndpoint,
  buildErrorPayload,
  extractErrorLocation,
  omnisightVitePlugin,
  postErrorPayload,
  renderRuntimeOverlayScript,
  truncateUtf8,
  // @ts-expect-error — JS module without .d.ts; vitest resolves the .js
} from "../../packages/omnisight-vite-plugin/index.js"

// ────────────────────────────────────────────────────────────────────
// §A — Schema / drift guards
// ────────────────────────────────────────────────────────────────────

describe("W15.1 §A — schema / drift guards", () => {
  it("schema_version matches the backend literal", () => {
    // Backend pin lives in
    // backend/web_sandbox_vite_errors.py::WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION.
    // The pytest drift guard pins it to "1.0.0"; this test fails red
    // if either side bumps in isolation.
    expect(OMNISIGHT_VITE_ERROR_SCHEMA_VERSION).toBe("1.0.0")
  })

  it("ALLOWED_PHASES matches the backend tuple ordering", () => {
    expect(ALLOWED_PHASES).toEqual([
      "config",
      "buildStart",
      "load",
      "transform",
      "hmr",
      "client",
    ])
  })

  it("plugin name and version pinned", () => {
    expect(OMNISIGHT_VITE_PLUGIN_NAME).toBe("omnisight-vite-plugin")
    expect(OMNISIGHT_VITE_PLUGIN_VERSION).toBe("0.1.0")
  })

  it("byte caps match the backend caps", () => {
    expect(MESSAGE_MAX_BYTES).toBe(4 * 1024)
    expect(STACK_TRACE_MAX_BYTES).toBe(8 * 1024)
  })
})

// ────────────────────────────────────────────────────────────────────
// §B — buildErrorPayload
// ────────────────────────────────────────────────────────────────────

describe("W15.1 §B — buildErrorPayload", () => {
  it("produces the canonical wire shape", () => {
    const err = Object.assign(new Error("Failed to parse module"), {
      id: "src/App.tsx",
      loc: { line: 42, column: 7 },
    })
    const payload = buildErrorPayload({
      kind: "compile",
      phase: "transform",
      error: err,
      occurredAt: 1714760400.123,
    })
    expect(payload).toMatchObject({
      schema_version: "1.0.0",
      kind: "compile",
      phase: "transform",
      message: "Failed to parse module",
      file: "src/App.tsx",
      line: 42,
      column: 7,
      plugin: "omnisight-vite-plugin",
      plugin_version: "0.1.0",
      occurred_at: 1714760400.123,
    })
    expect(typeof payload.stack).toBe("string")
  })

  it("rejects unknown kind", () => {
    expect(() =>
      buildErrorPayload({ kind: "warning" as never, phase: "transform", error: new Error("x") }),
    ).toThrow(/kind must be/)
  })

  it("rejects unknown phase", () => {
    expect(() =>
      buildErrorPayload({ kind: "compile", phase: "unknown" as never, error: new Error("x") }),
    ).toThrow(/phase must be/)
  })

  it("falls back to null location fields when error has none", () => {
    const payload = buildErrorPayload({
      kind: "runtime",
      phase: "client",
      error: { message: "x" },
    })
    expect(payload.file).toBeNull()
    expect(payload.line).toBeNull()
    expect(payload.column).toBeNull()
  })

  it("populates occurred_at with Date.now() / 1000 when omitted", () => {
    const before = Date.now() / 1000
    const payload = buildErrorPayload({
      kind: "compile",
      phase: "buildStart",
      error: new Error("x"),
    })
    const after = Date.now() / 1000
    expect(payload.occurred_at).toBeGreaterThanOrEqual(before - 0.1)
    expect(payload.occurred_at).toBeLessThanOrEqual(after + 0.1)
  })

  it("truncates oversized message and stack", () => {
    const big = "a".repeat(MESSAGE_MAX_BYTES + 200)
    const stk = "b".repeat(STACK_TRACE_MAX_BYTES + 200)
    const err = Object.assign(new Error(big), { stack: stk })
    const payload = buildErrorPayload({
      kind: "compile",
      phase: "transform",
      error: err,
    })
    expect(new TextEncoder().encode(payload.message).byteLength).toBeLessThanOrEqual(
      MESSAGE_MAX_BYTES,
    )
    expect(payload.stack).not.toBeNull()
    expect(new TextEncoder().encode(payload.stack as string).byteLength).toBeLessThanOrEqual(
      STACK_TRACE_MAX_BYTES,
    )
  })

  it("normalises non-Error error-like objects", () => {
    const payload = buildErrorPayload({
      kind: "runtime",
      phase: "client",
      error: { message: "ReferenceError", file: "src/x.js", line: 9, column: 2 },
    })
    expect(payload.message).toBe("ReferenceError")
    expect(payload.file).toBe("src/x.js")
    expect(payload.line).toBe(9)
    expect(payload.column).toBe(2)
  })
})

// ────────────────────────────────────────────────────────────────────
// §C — Pure helpers
// ────────────────────────────────────────────────────────────────────

describe("W15.1 §C — pure helpers", () => {
  it("truncateUtf8 leaves short strings untouched", () => {
    expect(truncateUtf8("hello", 100)).toBe("hello")
  })

  it("truncateUtf8 caps oversize strings", () => {
    const big = "x".repeat(2000)
    const out = truncateUtf8(big, 500) as string
    expect(new TextEncoder().encode(out).byteLength).toBeLessThanOrEqual(500)
  })

  it("truncateUtf8 ignores non-strings (passthrough)", () => {
    expect(truncateUtf8(42 as unknown as string, 5)).toBe(42)
  })

  it("extractErrorLocation prefers err.id over loc.file", () => {
    const out = extractErrorLocation({
      id: "src/A.tsx",
      loc: { file: "src/B.tsx", line: 1, column: 1 },
    })
    expect(out.file).toBe("src/A.tsx")
  })

  it("extractErrorLocation handles missing fields", () => {
    const out = extractErrorLocation({})
    expect(out.file).toBeNull()
    expect(out.line).toBeNull()
    expect(out.column).toBeNull()
  })

  it("buildErrorEndpoint trims trailing slashes and encodes ws id", () => {
    const ep = buildErrorEndpoint("https://api.example.com/", "ws/with space")
    expect(ep).toBe(
      "https://api.example.com/web-sandbox/preview/ws%2Fwith%20space/error",
    )
  })

  it("buildErrorEndpoint refuses empty inputs", () => {
    expect(() => buildErrorEndpoint("", "ws-1")).toThrow(/non-empty/)
    expect(() => buildErrorEndpoint("https://x", "")).toThrow(/non-empty/)
  })
})

// ────────────────────────────────────────────────────────────────────
// §D — postErrorPayload (best-effort transport)
// ────────────────────────────────────────────────────────────────────

describe("W15.1 §D — postErrorPayload", () => {
  it("returns ok=true for a 200 response", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ status: 200 })
    const res = await postErrorPayload({
      endpoint: "https://example.test/x",
      payload: { foo: "bar" },
      fetch: fetchImpl,
    })
    expect(res).toEqual({ ok: true, status: 200, error: null })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe("https://example.test/x")
    expect(init.method).toBe("POST")
    expect(init.headers["content-type"]).toBe("application/json")
    expect(JSON.parse(init.body as string)).toEqual({ foo: "bar" })
  })

  it("returns ok=false with status code for non-2xx", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ status: 422 })
    const res = await postErrorPayload({
      endpoint: "https://example.test/x",
      payload: {},
      fetch: fetchImpl,
    })
    expect(res).toEqual({ ok: false, status: 422, error: "http-422" })
  })

  it("swallows network errors and reports ok=false", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error("ECONNREFUSED"))
    const warn = vi.fn()
    const res = await postErrorPayload({
      endpoint: "https://example.test/x",
      payload: {},
      fetch: fetchImpl,
      logger: { warn },
    })
    expect(res.ok).toBe(false)
    expect(res.status).toBe(0)
    expect(res.error).toMatch(/ECONNREFUSED/)
    expect(warn).toHaveBeenCalled()
  })

  it("attaches Authorization header when authToken is supplied", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ status: 200 })
    await postErrorPayload({
      endpoint: "https://example.test/x",
      payload: {},
      fetch: fetchImpl,
      authToken: "secret-token",
    })
    const [, init] = fetchImpl.mock.calls[0]
    expect(init.headers["authorization"]).toBe("Bearer secret-token")
  })
})

// ────────────────────────────────────────────────────────────────────
// §E — renderRuntimeOverlayScript
// ────────────────────────────────────────────────────────────────────

describe("W15.1 §E — renderRuntimeOverlayScript", () => {
  it("produces a self-contained <script> tag", () => {
    const html = renderRuntimeOverlayScript({
      endpointUrl: "https://api.example.com/web-sandbox/preview/ws-1/error",
    })
    expect(html.startsWith("<script>")).toBe(true)
    expect(html.endsWith("</script>")).toBe(true)
    expect(html).toContain("window.addEventListener('error'")
    expect(html).toContain("unhandledrejection")
    expect(html).toContain("https://api.example.com/web-sandbox/preview/ws-1/error")
  })

  it("embeds the schema and plugin version pins", () => {
    const html = renderRuntimeOverlayScript({
      endpointUrl: "https://x/y",
    })
    expect(html).toContain('"schemaVersion":"1.0.0"')
    expect(html).toContain('"pluginVersion":"0.1.0"')
  })

  it("rejects empty endpoint URL", () => {
    expect(() => renderRuntimeOverlayScript({ endpointUrl: "" })).toThrow(/non-empty/)
  })
})

// ────────────────────────────────────────────────────────────────────
// §F — Plugin factory + Vite hook contract
// ────────────────────────────────────────────────────────────────────

describe("W15.1 §F — omnisightVitePlugin factory", () => {
  it("returns a Vite plugin object with the required identity", () => {
    const plugin = omnisightVitePlugin({
      workspaceId: "ws-42",
      backendUrl: "https://api.example.com",
    })
    expect(plugin.name).toBe("omnisight-vite-plugin")
    expect(typeof plugin.transformIndexHtml).toBe("function")
    expect(typeof plugin.configureServer).toBe("function")
    expect(plugin.api?.endpoint).toBe(
      "https://api.example.com/web-sandbox/preview/ws-42/error",
    )
    expect(plugin.api?.schemaVersion).toBe("1.0.0")
    expect(plugin.api?.pluginVersion).toBe("0.1.0")
  })

  it("transformIndexHtml injects the runtime script before </head>", () => {
    const plugin = omnisightVitePlugin({
      workspaceId: "ws-42",
      backendUrl: "https://api.example.com",
    })
    const before = "<html><head><title>x</title></head><body></body></html>"
    const out = plugin.transformIndexHtml!(before, undefined as never) as string
    const headIdx = out.indexOf("</head>")
    const scriptIdx = out.indexOf("<script>")
    expect(scriptIdx).toBeGreaterThanOrEqual(0)
    expect(scriptIdx).toBeLessThan(headIdx)
    expect(out).toContain("/web-sandbox/preview/ws-42/error")
  })

  it("transformIndexHtml passthroughs when injectRuntime: false", () => {
    const plugin = omnisightVitePlugin({
      workspaceId: "ws-42",
      backendUrl: "https://api.example.com",
      injectRuntime: false,
    })
    const before = "<html><head><title>x</title></head><body></body></html>"
    const out = plugin.transformIndexHtml!(before, undefined as never) as string
    expect(out).toBe(before)
  })

  it("api.reportError POSTs the canonical payload through the injected fetch", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ status: 200 })
    const plugin = omnisightVitePlugin({
      workspaceId: "ws-42",
      backendUrl: "https://api.example.com",
      fetch: fetchImpl,
    })
    const err = Object.assign(new Error("Failed to parse module"), {
      id: "src/App.tsx",
      loc: { line: 42, column: 7 },
    })
    const result = await plugin.api!.reportError("compile", "transform", err)
    expect(result.ok).toBe(true)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe("https://api.example.com/web-sandbox/preview/ws-42/error")
    expect(init.method).toBe("POST")
    const body = JSON.parse(init.body as string)
    expect(body).toMatchObject({
      schema_version: "1.0.0",
      kind: "compile",
      phase: "transform",
      file: "src/App.tsx",
      line: 42,
      column: 7,
      plugin: "omnisight-vite-plugin",
      plugin_version: "0.1.0",
    })
  })

  it("api.reportError swallows malformed inputs (best-effort)", async () => {
    const warn = vi.fn()
    const fetchImpl = vi.fn().mockResolvedValue({ status: 200 })
    const plugin = omnisightVitePlugin({
      workspaceId: "ws-42",
      backendUrl: "https://api.example.com",
      fetch: fetchImpl,
      logger: { warn },
    })
    // Bogus phase ⇒ buildErrorPayload throws ⇒ reportError should
    // still return rather than propagate.
    const result = await plugin.api!.reportError(
      "compile",
      "no-such-phase" as never,
      new Error("x"),
    )
    expect(result.ok).toBe(false)
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalled()
  })

  it("plugin factory refuses missing required options", () => {
    expect(() => omnisightVitePlugin({} as never)).toThrow(/non-empty/)
    expect(() =>
      omnisightVitePlugin({ workspaceId: "ws-1" } as never),
    ).toThrow(/non-empty/)
  })
})
