"use client"

import { useEffect, useRef } from "react"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { useI18n, type Locale } from "@/lib/i18n/context"
import { getUserStorage, migrateAllLegacyKeys, onStorageChange } from "@/lib/storage"

const LOCALE_KEY = "omnisight-locale"

export function StorageBridge() {
  const { user } = useAuth()
  const { currentTenantId } = useTenant()
  const { setLocale, locale } = useI18n()
  const userId = user?.id ?? null
  const migratedRef = useRef<string | null>(null)

  useEffect(() => {
    if (!userId || !currentTenantId) return
    const migrationKey = `${currentTenantId}:${userId}`
    if (migratedRef.current === migrationKey) return
    migratedRef.current = migrationKey
    migrateAllLegacyKeys(currentTenantId, userId)
    const store = getUserStorage(currentTenantId, userId)
    const saved = store.getItem(LOCALE_KEY)
    if (saved && saved !== locale) {
      setLocale(saved as Locale)
    }
  }, [userId, currentTenantId, locale, setLocale])

  useEffect(() => {
    if (!userId) return
    const store = getUserStorage(currentTenantId, userId)
    store.setItem(LOCALE_KEY, locale)
  }, [userId, currentTenantId, locale])

  useEffect(() => {
    if (!userId) return
    const store = getUserStorage(currentTenantId, userId)
    const fullKey = store.key(LOCALE_KEY)
    return onStorageChange((changedKey, newValue) => {
      if (changedKey === fullKey && newValue && newValue !== locale) {
        setLocale(newValue as Locale)
      }
    })
  }, [userId, currentTenantId, locale, setLocale])

  return null
}
