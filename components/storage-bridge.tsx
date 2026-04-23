"use client"

import { useEffect, useRef } from "react"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { useI18n, type Locale } from "@/lib/i18n/context"
import {
  getUserStorage,
  migrateAllLegacyKeys,
  notifyLocalStorageChange,
  onStorageChange,
} from "@/lib/storage"
import { subscribeEvents } from "@/lib/api"

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

  // Q.3-SUB-4 (#297): cross-device preferences push. A PUT /user-
  // preferences/{key} on device A emits ``preferences.updated`` on
  // the SSE bus (scope=user). On device B we (a) mirror the value
  // into localStorage using the same tenant+user prefix the J4
  // cross-tab path uses, which auto-dispatches a native ``storage``
  // event to OTHER tabs in this browser, and (b) notify in-tab
  // listeners ourselves since native ``storage`` does not fire in
  // the originating tab. This keeps the existing J4 cross-tab
  // consumers (I18n context, first-run-tour, new-project-wizard)
  // working without each one double-subscribing. ``broadcast_scope=
  // 'user'`` is advisory until Q.4 (#298), so we self-filter on
  // ``user_id`` here to avoid writing another user's prefs into our
  // own storage.
  useEffect(() => {
    if (!userId) return
    const store = getUserStorage(currentTenantId, userId)
    const handle = subscribeEvents(
      (event) => {
        if (event.event !== "preferences.updated") return
        const d = event.data
        if (d.user_id !== userId) return
        const fullKey = store.key(d.pref_key)
        const existing = store.getItem(d.pref_key)
        if (existing === d.value) return
        store.setItem(d.pref_key, d.value)
        notifyLocalStorageChange(fullKey, d.value)
      },
      () => {
        /* bridge is passive — reconnect is handled by the shared SSE
           manager in use-engine.ts; swallow errors here. */
      },
    )
    return () => handle.close()
  }, [userId, currentTenantId])

  return null
}
