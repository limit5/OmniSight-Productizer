"use client"

import { createContext, useContext, useState, useEffect, useMemo, type ReactNode } from "react"
import { NextIntlClientProvider } from "next-intl"

import enMessages from "@/messages/en.json"
import zhCNMessages from "@/messages/zh-CN.json"
import zhTWMessages from "@/messages/zh-TW.json"
import jaMessages from "@/messages/ja.json"
import {
  DEFAULT_LOCALE,
  LOCALES,
  LOCALE_COOKIE,
  isLocale,
  type Locale,
} from "@/i18n/routing"

// FX.7.11 — the legacy bespoke `useI18n() / t()` API is preserved as a
// thin facade so existing call sites (40+ components reference
// `useI18n()` today) keep working. New code should prefer next-intl's
// `useTranslations()` directly — both APIs read from the same
// `messages/<locale>.json` bundles, so they cannot drift.
//
// Migration path (out of scope for FX.7.11 scaffolding):
//   - Replace `const { t } = useI18n()` with
//     `const t = useTranslations()` (or namespaced
//     `useTranslations("header")`).
//   - Locale-switcher UI keeps using `useI18n().setLocale(...)` because
//     next-intl's locale source is the cookie that this provider sets.

export type { Locale } from "@/i18n/routing"

type MessageBundle = Record<string, unknown>

const MESSAGES: Record<Locale, MessageBundle> = {
  en: enMessages as MessageBundle,
  "zh-CN": zhCNMessages as MessageBundle,
  "zh-TW": zhTWMessages as MessageBundle,
  ja: jaMessages as MessageBundle,
}

interface I18nContextType {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: string, params?: Record<string, string | number>) => string
}

const I18nContext = createContext<I18nContextType | null>(null)

// Detect browser language. Mirrors the negotiation logic that the
// previous inline implementation used: exact match wins, otherwise
// fall back by language prefix.
function detectBrowserLocale(): Locale {
  if (typeof window === "undefined") return DEFAULT_LOCALE

  const browserLang = navigator.language || (navigator as unknown as { userLanguage?: string }).userLanguage || DEFAULT_LOCALE

  if (isLocale(browserLang)) {
    return browserLang
  }

  const langPrefix = browserLang.split("-")[0]
  if (langPrefix === "zh") {
    return browserLang.includes("TW") || browserLang.includes("HK") ? "zh-TW" : "zh-CN"
  }
  if (langPrefix === "ja") return "ja"

  return DEFAULT_LOCALE
}

// Walk a dot-separated key (`header.title`) into a nested message
// object. Returns the leaf string when found, otherwise `undefined`.
// Kept inline so the legacy `t()` shim does not depend on next-intl's
// internal resolver.
function resolveDotKey(bundle: MessageBundle, key: string): string | undefined {
  const segments = key.split(".")
  let cursor: unknown = bundle
  for (const seg of segments) {
    if (cursor && typeof cursor === "object" && seg in (cursor as Record<string, unknown>)) {
      cursor = (cursor as Record<string, unknown>)[seg]
    } else {
      return undefined
    }
  }
  return typeof cursor === "string" ? cursor : undefined
}

// Mirror locale changes into a cookie so next-intl's server-side
// `getRequestConfig` resolver picks the same locale on subsequent
// navigations. SameSite=Lax + 1y expiry matches typical preference
// cookies; no auth significance, no httpOnly required.
function persistLocaleCookie(locale: Locale): void {
  if (typeof document === "undefined") return
  const oneYearSec = 60 * 60 * 24 * 365
  document.cookie = `${LOCALE_COOKIE}=${locale}; path=/; max-age=${oneYearSec}; samesite=lax`
}

interface I18nProviderProps {
  children: ReactNode
}

export function I18nProvider({ children }: I18nProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE)
  const [mounted, setMounted] = useState(false)

  // Initialize locale from localStorage / cookie / browser detection
  // after mount. Falling back order: localStorage → cookie → navigator
  // → DEFAULT_LOCALE. localStorage wins because it's the per-user
  // explicit preference; cookie is only used as a server-readable
  // mirror of that preference.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- mount-time hydration from localStorage/cookie/navigator
    setMounted(true)
    try {
      const saved = localStorage.getItem(LOCALE_COOKIE)
      const detected: Locale = isLocale(saved) ? saved : detectBrowserLocale()
      setLocaleState(detected)
      persistLocaleCookie(detected)
      document.documentElement.lang = detected
    } catch {
      // localStorage / cookies not available — stay on the default.
    }
  }, [])

  const setLocale = (newLocale: Locale) => {
    setLocaleState(newLocale)
    try {
      localStorage.setItem(LOCALE_COOKIE, newLocale)
    } catch {
      // localStorage not available
    }
    persistLocaleCookie(newLocale)
    if (typeof document !== "undefined") {
      document.documentElement.lang = newLocale
    }
  }

  // Active bundle is the resolved locale once mounted; before mount we
  // pin to `en` so SSR/CSR markup matches and React doesn't trigger a
  // hydration mismatch warning.
  const activeLocale: Locale = mounted ? locale : DEFAULT_LOCALE
  const messages = MESSAGES[activeLocale] ?? MESSAGES[DEFAULT_LOCALE]

  const t = (key: string, params?: Record<string, string | number>): string => {
    const text = resolveDotKey(messages, key) ?? resolveDotKey(MESSAGES[DEFAULT_LOCALE], key) ?? key
    if (!params) return text
    let out = text
    for (const [paramKey, value] of Object.entries(params)) {
      out = out.replace(new RegExp(`\\{${paramKey}\\}`, "g"), String(value))
    }
    return out
  }

  const ctxValue = useMemo<I18nContextType>(
    () => ({ locale: activeLocale, setLocale, t }),
    // `t` and `setLocale` are stable references for the lifetime of
    // this provider; only `activeLocale` actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeLocale],
  )

  return (
    <I18nContext.Provider value={ctxValue}>
      <NextIntlClientProvider locale={activeLocale} messages={messages}>
        {children}
      </NextIntlClientProvider>
    </I18nContext.Provider>
  )
}

export function useI18n() {
  const context = useContext(I18nContext)
  if (!context) {
    throw new Error("useI18n must be used within an I18nProvider")
  }
  return context
}

// Re-export the canonical locale list so call sites that need to render
// a locale picker can avoid hard-coding the array.
export { LOCALES, DEFAULT_LOCALE }
