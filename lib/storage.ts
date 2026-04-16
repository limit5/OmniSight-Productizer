"use client"

import { useEffect, useRef, useSyncExternalStore } from "react"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"

const LEGACY_KEYS: Record<string, string> = {
  "omnisight-locale": "omnisight:locale",
  "omnisight:intent:last_spec": "omnisight:intent:last_spec",
  "omnisight:wizard:seen": "omnisight:wizard:seen",
  "omnisight-tour-seen": "omnisight:tour:seen",
}

function prefixedKey(tenantId: string | null, userId: string | null, key: string): string {
  const canonical = LEGACY_KEYS[key] ?? key
  const tid = tenantId || "t-default"
  const uid = userId || "_anonymous"
  return `omnisight:${tid}:${uid}:${canonical.replace(/^omnisight:/, "")}`
}

function _oldPrefixedKey(userId: string | null, key: string): string {
  const canonical = LEGACY_KEYS[key] ?? key
  const uid = userId || "_anonymous"
  return `omnisight:${uid}:${canonical.replace(/^omnisight:/, "")}`
}

function migrateLegacyKey(tenantId: string, userId: string, legacyKey: string): void {
  try {
    const newKey = prefixedKey(tenantId, userId, legacyKey)
    if (localStorage.getItem(newKey) !== null) return
    const oldUserScoped = _oldPrefixedKey(userId, legacyKey)
    const oldVal = localStorage.getItem(oldUserScoped)
    if (oldVal !== null) {
      localStorage.setItem(newKey, oldVal)
      localStorage.removeItem(oldUserScoped)
      return
    }
    const bareVal = localStorage.getItem(legacyKey)
    if (bareVal !== null) {
      localStorage.setItem(newKey, bareVal)
      localStorage.removeItem(legacyKey)
    }
  } catch { /* private mode or quota */ }
}

export function migrateAllLegacyKeys(tenantId: string, userId: string): void {
  for (const legacyKey of Object.keys(LEGACY_KEYS)) {
    migrateLegacyKey(tenantId, userId, legacyKey)
  }
}

export function getUserStorage(tenantId: string | null, userId: string | null) {
  return {
    getItem(key: string): string | null {
      try {
        return localStorage.getItem(prefixedKey(tenantId, userId, key))
      } catch { return null }
    },
    setItem(key: string, value: string): void {
      try {
        localStorage.setItem(prefixedKey(tenantId, userId, key), value)
      } catch { /* quota or private mode */ }
    },
    removeItem(key: string): void {
      try {
        localStorage.removeItem(prefixedKey(tenantId, userId, key))
      } catch { /* ignore */ }
    },
    key(key: string): string {
      return prefixedKey(tenantId, userId, key)
    },
  }
}

type StorageChangeCallback = (key: string, newValue: string | null) => void
const listeners = new Set<StorageChangeCallback>()

if (typeof window !== "undefined") {
  window.addEventListener("storage", (e: StorageEvent) => {
    if (!e.key?.startsWith("omnisight:")) return
    for (const cb of listeners) {
      cb(e.key, e.newValue)
    }
  })
}

export function onStorageChange(cb: StorageChangeCallback): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

export function useUserStorage(key: string): [string | null, (v: string | null) => void] {
  const { user } = useAuth()
  const { currentTenantId } = useTenant()
  const userId = user?.id ?? null
  const fullKey = prefixedKey(currentTenantId, userId, key)

  const snapshotRef = useRef<string | null>(null)
  const keyRef = useRef(fullKey)
  keyRef.current = fullKey

  const subscribe = (onStoreChange: () => void) => {
    const unsub = onStorageChange((changedKey) => {
      if (changedKey === keyRef.current) onStoreChange()
    })
    return unsub
  }

  const getSnapshot = () => {
    try {
      const v = localStorage.getItem(fullKey)
      snapshotRef.current = v
      return v
    } catch { return null }
  }

  const getServerSnapshot = () => null

  const value = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)

  const setValue = (v: string | null) => {
    try {
      if (v === null) {
        localStorage.removeItem(fullKey)
      } else {
        localStorage.setItem(fullKey, v)
      }
    } catch { /* quota */ }
  }

  return [value, setValue]
}

export function useStorageSync(key: string, onSync: (newValue: string | null) => void): void {
  const { user } = useAuth()
  const { currentTenantId } = useTenant()
  const userId = user?.id ?? null
  const fullKey = prefixedKey(currentTenantId, userId, key)
  const onSyncRef = useRef(onSync)
  onSyncRef.current = onSync

  useEffect(() => {
    return onStorageChange((changedKey, newValue) => {
      if (changedKey === fullKey) {
        onSyncRef.current(newValue)
      }
    })
  }, [fullKey])
}
