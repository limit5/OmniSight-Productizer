/**
 * N11 — tsconfig/vitest alias sync check.
 *
 * Both tsconfig.json (paths) and vitest.config.ts (resolve.alias) declare
 * an `@/` alias pointing to the repo root. They're maintained by hand
 * today; this test fails loudly if someone edits one without the other.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

describe("path alias sync", () => {
  it("tsconfig @/* and vitest @/ resolve to the same root", () => {
    // tsconfig.json can contain JSONC comments / trailing commas that
    // JSON.parse rejects, so we assert via regex on the relevant fragment
    // instead of a full parse. That's enough to catch a drift where the
    // two configs stop agreeing on where "@/" points.
    const root = resolve(__dirname, "..")
    const tsconfigText = readFileSync(resolve(root, "tsconfig.json"), "utf8")
    expect(tsconfigText).toMatch(/"@\/\*"\s*:\s*\[\s*"\.\/\*"\s*\]/)

    const cfg = readFileSync(resolve(root, "vitest.config.ts"), "utf8")
    expect(cfg).toMatch(/"@":\s*path\.resolve\(__dirname,\s*"\.\/"\)/)
  })
})
