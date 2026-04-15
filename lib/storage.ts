"use client"

import { useEffect, useRef, useSyncExternalStore } from "react"
import { useAuth } from "@/lib/auth-context"

const LEGACY_KEYS: Record<string, string> = {
  "omnisight-locale": "omnisight:locale",
  "omnisight:intent:last_spec": "omnisight:intent:last_spec",
  "omnisight:wizard:seen": "omnisight:wizard:seen",
  "omnisight-tour-seen": "omnisight:tour:seen",
}

function prefixedKey(userId: string | null, key: string): string {
  const canonical = LEGACY_KEYS[key] ?? key
  const uid = userId || "_anonymous"
  return `omnisight:${uid}:${canonical.replace(/^omnisight:/, "")}`
}

function migrateLegacyKey(userId: string, legacyKey: string): void {
  try {
    const val = localStorage.getItem(legacyKey)
    if (val === null) return
    const newKey = prefixedKey(userId, legacyKey)
    if (localStorage.getItem(newKey) === null) {
      localStorage.setItem(newKey, val)
    }
    localStorage.removeItem(legacyKey)
  } catch { /* private mode or quota */ }
}

export function migrateAllLegacyKeys(userId: string): void {
  for (const legacyKey of Object.keys(LEGACY_KEYS)) {
    migrateLegacyKey(userId, legacyKey)
  }
}

export function getUserStorage(userId: string | null) {
  return {
    getItem(key: string): string | null {
      try {
        return localStorage.getItem(prefixedKey(userId, key))
      } catch { return null }
    },
    setItem(key: string, value: string): void {
      try {
        localStorage.setItem(prefixedKey(userId, key), value)
      } catch { /* quota or private mode */ }
    },
    removeItem(key: string): void {
      try {
        localStorage.removeItem(prefixedKey(userId, key))
      } catch { /* ignore */ }
    },
    key(key: string): string {
      return prefixedKey(userId, key)
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
  const userId = user?.id ?? null
  const fullKey = prefixedKey(userId, key)

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
  const userId = user?.id ?? null
  const fullKey = prefixedKey(userId, key)
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
