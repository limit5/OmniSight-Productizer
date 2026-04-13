/**
 * Phase 49A smoke test — proves the vitest + jsdom + jest-dom + path alias
 * pipeline is wired. No component rendering yet; that lands in 49B/C.
 */

import { describe, expect, it } from "vitest"
import { SLASH_COMMANDS } from "@/lib/slash-commands"

describe("smoke: test runner", () => {
  it("runs in jsdom", () => {
    expect(typeof window).toBe("object")
    expect(typeof document).toBe("object")
  })

  it("has the jest-dom matcher installed", () => {
    const node = document.createElement("div")
    node.textContent = "hi"
    document.body.appendChild(node)
    expect(node).toBeInTheDocument()
    expect(node).toHaveTextContent("hi")
  })

  it("resolves the @/ path alias", () => {
    expect(Array.isArray(SLASH_COMMANDS)).toBe(true)
    expect(SLASH_COMMANDS.length).toBeGreaterThan(0)
  })

  it("has the mocked EventSource polyfill", () => {
    const Ctor = (globalThis as unknown as { EventSource: typeof EventSource }).EventSource
    expect(typeof Ctor).toBe("function")
    const es = new Ctor("http://example.test")
    expect(es.readyState).toBe(1)
    es.close()
    expect(es.readyState).toBe(2)
  })
})
