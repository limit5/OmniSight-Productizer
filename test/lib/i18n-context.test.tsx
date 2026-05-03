// FX.7.11 — exercise the dual-API shim in lib/i18n/context.tsx.
//
// The provider has to satisfy two callers simultaneously:
//   - Legacy `useI18n().t("header.title")` (40+ existing components).
//   - New next-intl `useTranslations("header").raw("title")` /
//     `useTranslations()("header.title")`.
// Both must read from the same `messages/<locale>.json` and return the
// same string for a given key. If the bridge ever drifts (e.g. someone
// changes the legacy `t()` to read a stale dictionary), this test
// surfaces it before the next deploy.

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import { useTranslations } from "next-intl"
import { I18nProvider, useI18n } from "@/lib/i18n/context"

function LegacyConsumer() {
  const { t } = useI18n()
  return <span data-testid="legacy">{t("header.title")}</span>
}

function NextIntlConsumer() {
  const t = useTranslations("header")
  return <span data-testid="nextintl">{t("title")}</span>
}

describe("FX.7.11 I18nProvider bridge — legacy + next-intl APIs read same bundle", () => {
  it("renders the same key through both APIs", () => {
    render(
      <I18nProvider>
        <LegacyConsumer />
        <NextIntlConsumer />
      </I18nProvider>,
    )
    const legacy = screen.getByTestId("legacy").textContent
    const nextIntl = screen.getByTestId("nextintl").textContent
    expect(legacy).toBeTruthy()
    expect(nextIntl).toBeTruthy()
    expect(legacy).toBe(nextIntl)
  })
})
