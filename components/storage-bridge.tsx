"use client"

import { useEffect, useRef } from "react"
import { useAuth } from "@/lib/auth-context"
import { useI18n, type Locale } from "@/lib/i18n/context"
import { getUserStorage, migrateAllLegacyKeys, onStorageChange } from "@/lib/storage"

const LOCALE_KEY = "omnisight-locale"

export function StorageBridge() {
  const { user } = useAuth()
  const { setLocale, locale } = useI18n()
  const userId = user?.id ?? null
  const migratedRef = useRef<string | null>(null)

  useEffect(() => {
    if (!userId || migratedRef.current === userId) return
    migratedRef.current = userId
    migrateAllLegacyKeys(userId)
    const store = getUserStorage(userId)
    const saved = store.getItem(LOCALE_KEY)
    if (saved && saved !== locale) {
      setLocale(saved as Locale)
    }
  }, [userId, locale, setLocale])

  useEffect(() => {
    if (!userId) return
    const store = getUserStorage(userId)
    store.setItem(LOCALE_KEY, locale)
  }, [userId, locale])

  useEffect(() => {
    if (!userId) return
    const store = getUserStorage(userId)
    const fullKey = store.key(LOCALE_KEY)
    return onStorageChange((changedKey, newValue) => {
      if (changedKey === fullKey && newValue && newValue !== locale) {
        setLocale(newValue as Locale)
      }
    })
  }, [userId, locale, setLocale])

  return null
}
