import { describe, expect, it, beforeEach, vi } from "vitest"
import { getUserStorage, migrateAllLegacyKeys, onStorageChange } from "@/lib/storage"

describe("getUserStorage", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("prefixes keys with user_id", () => {
    const store = getUserStorage("user-123")
    store.setItem("omnisight:wizard:seen", "1")
    expect(localStorage.getItem("omnisight:user-123:wizard:seen")).toBe("1")
  })

  it("reads back user-scoped values", () => {
    const store = getUserStorage("user-123")
    store.setItem("omnisight:wizard:seen", "1")
    expect(store.getItem("omnisight:wizard:seen")).toBe("1")
  })

  it("different users get different keys", () => {
    const storeA = getUserStorage("alice")
    const storeB = getUserStorage("bob")
    storeA.setItem("omnisight-locale", "ja")
    storeB.setItem("omnisight-locale", "zh-CN")
    expect(storeA.getItem("omnisight-locale")).toBe("ja")
    expect(storeB.getItem("omnisight-locale")).toBe("zh-CN")
  })

  it("anonymous fallback for null userId", () => {
    const store = getUserStorage(null)
    store.setItem("omnisight-locale", "en")
    expect(localStorage.getItem("omnisight:_anonymous:locale")).toBe("en")
  })

  it("removeItem works", () => {
    const store = getUserStorage("user-123")
    store.setItem("omnisight-locale", "ja")
    store.removeItem("omnisight-locale")
    expect(store.getItem("omnisight-locale")).toBeNull()
  })
})

describe("migrateAllLegacyKeys", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("migrates omnisight-locale to user-prefixed key", () => {
    localStorage.setItem("omnisight-locale", "ja")
    migrateAllLegacyKeys("user-1")
    expect(localStorage.getItem("omnisight-locale")).toBeNull()
    expect(localStorage.getItem("omnisight:user-1:locale")).toBe("ja")
  })

  it("migrates omnisight:wizard:seen to user-prefixed key", () => {
    localStorage.setItem("omnisight:wizard:seen", "1")
    migrateAllLegacyKeys("user-1")
    expect(localStorage.getItem("omnisight:wizard:seen")).toBeNull()
    expect(localStorage.getItem("omnisight:user-1:wizard:seen")).toBe("1")
  })

  it("migrates omnisight-tour-seen to user-prefixed key", () => {
    localStorage.setItem("omnisight-tour-seen", "1")
    migrateAllLegacyKeys("user-1")
    expect(localStorage.getItem("omnisight-tour-seen")).toBeNull()
    expect(localStorage.getItem("omnisight:user-1:tour:seen")).toBe("1")
  })

  it("migrates omnisight:intent:last_spec to user-prefixed key", () => {
    const spec = JSON.stringify({ raw_text: "test" })
    localStorage.setItem("omnisight:intent:last_spec", spec)
    migrateAllLegacyKeys("user-1")
    expect(localStorage.getItem("omnisight:intent:last_spec")).toBeNull()
    expect(localStorage.getItem("omnisight:user-1:intent:last_spec")).toBe(spec)
  })

  it("does not overwrite existing user-prefixed key", () => {
    localStorage.setItem("omnisight-locale", "ja")
    localStorage.setItem("omnisight:user-1:locale", "en")
    migrateAllLegacyKeys("user-1")
    expect(localStorage.getItem("omnisight:user-1:locale")).toBe("en")
  })
})

describe("onStorageChange", () => {
  it("fires callback on storage events for omnisight keys", () => {
    const cb = vi.fn()
    const unsub = onStorageChange(cb)

    window.dispatchEvent(new StorageEvent("storage", {
      key: "omnisight:user-1:locale",
      newValue: "ja",
    }))

    expect(cb).toHaveBeenCalledWith("omnisight:user-1:locale", "ja")
    unsub()
  })

  it("ignores non-omnisight keys", () => {
    const cb = vi.fn()
    const unsub = onStorageChange(cb)

    window.dispatchEvent(new StorageEvent("storage", {
      key: "some-other-key",
      newValue: "val",
    }))

    expect(cb).not.toHaveBeenCalled()
    unsub()
  })

  it("unsubscribe stops future callbacks", () => {
    const cb = vi.fn()
    const unsub = onStorageChange(cb)
    unsub()

    window.dispatchEvent(new StorageEvent("storage", {
      key: "omnisight:user-1:locale",
      newValue: "ja",
    }))

    expect(cb).not.toHaveBeenCalled()
  })
})
