import { describe, expect, it, beforeEach, vi } from "vitest"
import { getUserStorage, migrateAllLegacyKeys, onStorageChange } from "@/lib/storage"

describe("getUserStorage", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("prefixes keys with tenant_id and user_id", () => {
    const store = getUserStorage("t-acme", "user-123")
    store.setItem("omnisight:wizard:seen", "1")
    expect(localStorage.getItem("omnisight:t-acme:user-123:wizard:seen")).toBe("1")
  })

  it("reads back tenant+user-scoped values", () => {
    const store = getUserStorage("t-acme", "user-123")
    store.setItem("omnisight:wizard:seen", "1")
    expect(store.getItem("omnisight:wizard:seen")).toBe("1")
  })

  it("different tenants get different keys", () => {
    const storeA = getUserStorage("t-acme", "user-1")
    const storeB = getUserStorage("t-beta", "user-1")
    storeA.setItem("omnisight-locale", "ja")
    storeB.setItem("omnisight-locale", "zh-CN")
    expect(storeA.getItem("omnisight-locale")).toBe("ja")
    expect(storeB.getItem("omnisight-locale")).toBe("zh-CN")
  })

  it("different users get different keys", () => {
    const storeA = getUserStorage("t-default", "alice")
    const storeB = getUserStorage("t-default", "bob")
    storeA.setItem("omnisight-locale", "ja")
    storeB.setItem("omnisight-locale", "zh-CN")
    expect(storeA.getItem("omnisight-locale")).toBe("ja")
    expect(storeB.getItem("omnisight-locale")).toBe("zh-CN")
  })

  it("anonymous fallback for null userId", () => {
    const store = getUserStorage(null, null)
    store.setItem("omnisight-locale", "en")
    expect(localStorage.getItem("omnisight:t-default:_anonymous:locale")).toBe("en")
  })

  it("defaults tenantId to t-default when null", () => {
    const store = getUserStorage(null, "user-123")
    store.setItem("omnisight-locale", "en")
    expect(localStorage.getItem("omnisight:t-default:user-123:locale")).toBe("en")
  })

  it("removeItem works", () => {
    const store = getUserStorage("t-acme", "user-123")
    store.setItem("omnisight-locale", "ja")
    store.removeItem("omnisight-locale")
    expect(store.getItem("omnisight-locale")).toBeNull()
  })
})

describe("migrateAllLegacyKeys", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("migrates bare legacy key to tenant+user-prefixed key", () => {
    localStorage.setItem("omnisight-locale", "ja")
    migrateAllLegacyKeys("t-acme", "user-1")
    expect(localStorage.getItem("omnisight-locale")).toBeNull()
    expect(localStorage.getItem("omnisight:t-acme:user-1:locale")).toBe("ja")
  })

  it("migrates old user-scoped key to tenant+user-prefixed key", () => {
    localStorage.setItem("omnisight:user-1:wizard:seen", "1")
    migrateAllLegacyKeys("t-acme", "user-1")
    expect(localStorage.getItem("omnisight:user-1:wizard:seen")).toBeNull()
    expect(localStorage.getItem("omnisight:t-acme:user-1:wizard:seen")).toBe("1")
  })

  it("migrates omnisight-tour-seen to tenant+user-prefixed key", () => {
    localStorage.setItem("omnisight-tour-seen", "1")
    migrateAllLegacyKeys("t-acme", "user-1")
    expect(localStorage.getItem("omnisight-tour-seen")).toBeNull()
    expect(localStorage.getItem("omnisight:t-acme:user-1:tour:seen")).toBe("1")
  })

  it("migrates omnisight:intent:last_spec to tenant+user-prefixed key", () => {
    const spec = JSON.stringify({ raw_text: "test" })
    localStorage.setItem("omnisight:intent:last_spec", spec)
    migrateAllLegacyKeys("t-acme", "user-1")
    expect(localStorage.getItem("omnisight:intent:last_spec")).toBeNull()
    expect(localStorage.getItem("omnisight:t-acme:user-1:intent:last_spec")).toBe(spec)
  })

  it("does not overwrite existing tenant+user-prefixed key", () => {
    localStorage.setItem("omnisight:user-1:locale", "ja")
    localStorage.setItem("omnisight:t-acme:user-1:locale", "en")
    migrateAllLegacyKeys("t-acme", "user-1")
    expect(localStorage.getItem("omnisight:t-acme:user-1:locale")).toBe("en")
  })
})

describe("onStorageChange", () => {
  it("fires callback on storage events for omnisight keys", () => {
    const cb = vi.fn()
    const unsub = onStorageChange(cb)

    window.dispatchEvent(new StorageEvent("storage", {
      key: "omnisight:t-acme:user-1:locale",
      newValue: "ja",
    }))

    expect(cb).toHaveBeenCalledWith("omnisight:t-acme:user-1:locale", "ja")
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
      key: "omnisight:t-acme:user-1:locale",
      newValue: "ja",
    }))

    expect(cb).not.toHaveBeenCalled()
  })
})
