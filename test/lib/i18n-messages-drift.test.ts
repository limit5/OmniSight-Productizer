// FX.7.11 — drift-guard for next-intl message bundles.
//
// next-intl resolves keys against per-locale JSON bundles. If one bundle
// adds a new key (`header.welcome`) but another locale forgets to mirror
// it, the missing locale silently renders `header.welcome` as the raw
// key string — and our automated tests would never catch that. This
// suite is the SOP §"drift guard" entry for FX.7.11: every locale must
// expose the same flattened key set as `messages/en.json`, the
// canonical source.
//
// Failure modes this test catches:
//   - Adding a key to en.json without translating it in zh-CN/zh-TW/ja.
//   - Renaming a key in zh-TW.json (typo) so it diverges from en.json.
//   - Mismatched leaf shapes (one locale stores a string, another a
//     nested object — both flattenings would differ).
//   - A locale registered in `i18n/routing.ts::LOCALES` without a
//     matching `messages/<locale>.json` file.
//   - A `messages/*.json` file that isn't registered in `LOCALES`.

import { describe, expect, it } from "vitest"
import { existsSync, readdirSync, readFileSync } from "node:fs"
import path from "node:path"
import { LOCALES, DEFAULT_LOCALE } from "@/i18n/routing"

const MESSAGES_DIR = path.resolve(__dirname, "..", "..", "messages")

function readBundle(locale: string): Record<string, unknown> {
  const file = path.join(MESSAGES_DIR, `${locale}.json`)
  return JSON.parse(readFileSync(file, "utf-8")) as Record<string, unknown>
}

function flatten(obj: Record<string, unknown>, prefix = ""): string[] {
  const keys: string[] = []
  for (const [k, v] of Object.entries(obj)) {
    const dotted = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === "object" && !Array.isArray(v)) {
      keys.push(...flatten(v as Record<string, unknown>, dotted))
    } else {
      keys.push(dotted)
    }
  }
  return keys.sort()
}

describe("FX.7.11 i18n message-bundle drift guard", () => {
  it("every registered locale has a JSON bundle on disk", () => {
    for (const locale of LOCALES) {
      const file = path.join(MESSAGES_DIR, `${locale}.json`)
      expect(existsSync(file), `messages/${locale}.json must exist for registered locale ${locale}`).toBe(true)
    }
  })

  it("every JSON bundle on disk is a registered locale", () => {
    const onDisk = readdirSync(MESSAGES_DIR)
      .filter((f) => f.endsWith(".json"))
      .map((f) => f.replace(/\.json$/, ""))
    for (const locale of onDisk) {
      expect(
        (LOCALES as readonly string[]).includes(locale),
        `messages/${locale}.json is not registered in i18n/routing.ts::LOCALES`,
      ).toBe(true)
    }
  })

  it("DEFAULT_LOCALE is one of the registered locales", () => {
    expect((LOCALES as readonly string[]).includes(DEFAULT_LOCALE)).toBe(true)
  })

  it("every locale exposes the same flattened key set as DEFAULT_LOCALE", () => {
    const baseline = flatten(readBundle(DEFAULT_LOCALE))
    expect(baseline.length, "DEFAULT_LOCALE bundle must not be empty").toBeGreaterThan(0)

    for (const locale of LOCALES) {
      if (locale === DEFAULT_LOCALE) continue
      const keys = flatten(readBundle(locale))
      const missing = baseline.filter((k) => !keys.includes(k))
      const extra = keys.filter((k) => !baseline.includes(k))
      expect(
        missing,
        `messages/${locale}.json missing keys present in ${DEFAULT_LOCALE}.json: ${missing.join(", ")}`,
      ).toEqual([])
      expect(
        extra,
        `messages/${locale}.json has keys not present in ${DEFAULT_LOCALE}.json: ${extra.join(", ")}`,
      ).toEqual([])
    }
  })

  it("no locale has empty leaf strings (would render as blank UI)", () => {
    function findEmpty(obj: Record<string, unknown>, prefix = ""): string[] {
      const out: string[] = []
      for (const [k, v] of Object.entries(obj)) {
        const dotted = prefix ? `${prefix}.${k}` : k
        if (v && typeof v === "object" && !Array.isArray(v)) {
          out.push(...findEmpty(v as Record<string, unknown>, dotted))
        } else if (typeof v === "string" && v.trim() === "") {
          out.push(dotted)
        }
      }
      return out
    }
    for (const locale of LOCALES) {
      const empty = findEmpty(readBundle(locale))
      expect(
        empty,
        `messages/${locale}.json has empty leaf strings: ${empty.join(", ")}`,
      ).toEqual([])
    }
  })

  it("interpolation tokens ({foo}) match across locales for the same key", () => {
    // If en says "Hello {name}" and zh-TW says "你好" without `{name}`,
    // the locale silently drops the user's name. Catch that mismatch.
    const baseline = readBundle(DEFAULT_LOCALE)
    const baselineFlat = flatten(baseline)

    function leafAt(obj: Record<string, unknown>, dotted: string): string | undefined {
      let cursor: unknown = obj
      for (const seg of dotted.split(".")) {
        if (cursor && typeof cursor === "object" && seg in (cursor as Record<string, unknown>)) {
          cursor = (cursor as Record<string, unknown>)[seg]
        } else {
          return undefined
        }
      }
      return typeof cursor === "string" ? cursor : undefined
    }

    function tokens(s: string): string[] {
      return Array.from(s.matchAll(/\{([a-zA-Z0-9_]+)\}/g), (m) => m[1]).sort()
    }

    for (const locale of LOCALES) {
      if (locale === DEFAULT_LOCALE) continue
      const bundle = readBundle(locale)
      for (const key of baselineFlat) {
        const baseStr = leafAt(baseline, key)
        const localeStr = leafAt(bundle, key)
        if (baseStr === undefined || localeStr === undefined) continue
        const baseTok = tokens(baseStr)
        const localeTok = tokens(localeStr)
        expect(
          localeTok,
          `messages/${locale}.json key "${key}" interpolation tokens diverge from ${DEFAULT_LOCALE}.json (expected ${JSON.stringify(baseTok)}, got ${JSON.stringify(localeTok)})`,
        ).toEqual(baseTok)
      }
    }
  })
})
