// FX.7.11 — next-intl routing config (scaffolding).
//
// This module is the single source of truth for the locale list and the
// default locale used by both the server-side `i18n/request.ts` loader
// and the client-side `lib/i18n/context.tsx` compatibility layer.
//
// Locales mirror the four bundles under `messages/<locale>.json`. Adding
// a new locale requires:
//   1. Add a JSON bundle at `messages/<locale>.json`.
//   2. Append the locale here in `LOCALES`.
//   3. The drift-guard test (`test/lib/i18n-messages-drift.test.ts`)
//      will fail until the new bundle has the same key set as `en.json`.
//
// We intentionally keep this scaffolding "locale-by-cookie" rather than
// "locale-by-URL-segment" so existing routes (`app/login`, `app/projects`,
// etc.) do not need to be relocated under `[locale]/` for FX.7.11. A
// follow-up row can introduce path-based routing if SEO requirements
// warrant it. The user-facing locale switcher already lives in
// `lib/i18n/context.tsx::I18nProvider` (localStorage-backed) — FX.7.11
// just feeds the same value into next-intl machinery.

export const LOCALES = ["en", "zh-CN", "zh-TW", "ja"] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "en";

// Cookie name shared between the server `getRequestConfig` resolver and
// the client `I18nProvider`. Renaming requires migrating both sides plus
// the `lib/storage.ts` legacy-key migrator.
export const LOCALE_COOKIE = "omnisight-locale";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}
