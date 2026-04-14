/**
 * N11 — tsconfig ↔ vitest `@/` alias sync check.
 *
 * Scope: exactly the two configs that declare the alias by hand —
 * tsconfig.json `compilerOptions.paths` (which Next.js and Playwright's
 * TS loader both pick up automatically) and vitest.config.ts
 * `resolve.alias`. Keeping those two in agreement is enough to cover
 * every runtime we actually use; other tools inherit from them.
 *
 * Parsing is structural: JSONC-aware stripper for tsconfig, dynamic
 * import for vitest.config. Harmless formatting changes (whitespace,
 * quote style, key order) do not trigger false positives.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const ROOT = resolve(__dirname, "..")

function stripJsonc(text: string): string {
  // String-aware JSONC stripper: walks one char at a time so // or /*
  // inside a string literal (e.g. "@/*") aren't mistaken for comments.
  let out = ""
  let i = 0
  let inStr: string | null = null
  while (i < text.length) {
    const c = text[i]
    if (inStr) {
      out += c
      if (c === "\\" && i + 1 < text.length) { out += text[i + 1]; i += 2; continue }
      if (c === inStr) inStr = null
      i++
      continue
    }
    if (c === '"' || c === "'") { inStr = c; out += c; i++; continue }
    if (c === "/" && text[i + 1] === "/") {
      while (i < text.length && text[i] !== "\n") i++
      continue
    }
    if (c === "/" && text[i + 1] === "*") {
      i += 2
      while (i < text.length && !(text[i] === "*" && text[i + 1] === "/")) i++
      i += 2
      continue
    }
    out += c
    i++
  }
  // Trailing commas before } or ].
  return out.replace(/,(\s*[}\]])/g, "$1")
}

describe("path alias sync", () => {
  it("tsconfig @/* and vitest @/ resolve to the same root", async () => {
    const tsconfigText = readFileSync(resolve(ROOT, "tsconfig.json"), "utf8")
    const tsconfig = JSON.parse(stripJsonc(tsconfigText)) as {
      compilerOptions?: { paths?: Record<string, string[]> }
    }
    const tsPath = tsconfig.compilerOptions?.paths?.["@/*"]?.[0]
    expect(tsPath).toBeDefined()
    // Resolve "./*" → repo root.
    const tsResolved = resolve(ROOT, (tsPath as string).replace(/\*$/, ""))

    const cfgModule = await import("../vitest.config")
    const cfg = (cfgModule.default ?? cfgModule) as unknown as {
      resolve?: { alias?: Record<string, string> }
    }
    const vitestAlias = cfg.resolve?.alias?.["@"]
    expect(vitestAlias).toBeDefined()
    const vitestResolved = resolve(vitestAlias as string)

    expect(tsResolved).toBe(vitestResolved)
  })
})
