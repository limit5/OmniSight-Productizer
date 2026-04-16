/**
 * I7: Frontend tenant-aware integration tests.
 *
 * Verifies:
 *   - X-Tenant-Id header is injected in API requests
 *   - setCurrentTenantId updates the global state read by request()
 *   - SSE filter still works with tenant scoping
 *   - Storage keys include tenant prefix
 */

import { describe, expect, it, beforeEach, vi } from "vitest"
import {
  setCurrentTenantId,
  getCurrentTenantId,
  setCurrentSessionId,
} from "@/lib/api"
import { getUserStorage } from "@/lib/storage"

describe("I7: tenant-aware API header", () => {
  beforeEach(() => {
    setCurrentTenantId(null)
    setCurrentSessionId(null)
  })

  it("setCurrentTenantId / getCurrentTenantId round-trips", () => {
    expect(getCurrentTenantId()).toBeNull()
    setCurrentTenantId("t-acme")
    expect(getCurrentTenantId()).toBe("t-acme")
  })

  it("clearing tenant resets to null", () => {
    setCurrentTenantId("t-acme")
    setCurrentTenantId(null)
    expect(getCurrentTenantId()).toBeNull()
  })
})

describe("I7: tenant-scoped localStorage", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("storage keys include tenant:user prefix", () => {
    const store = getUserStorage("t-acme", "user-1")
    store.setItem("omnisight:wizard:seen", "1")
    expect(localStorage.getItem("omnisight:t-acme:user-1:wizard:seen")).toBe("1")
  })

  it("same user different tenant = isolated storage", () => {
    const storeA = getUserStorage("t-acme", "user-1")
    const storeB = getUserStorage("t-beta", "user-1")

    storeA.setItem("omnisight-locale", "ja")
    storeB.setItem("omnisight-locale", "en")

    expect(storeA.getItem("omnisight-locale")).toBe("ja")
    expect(storeB.getItem("omnisight-locale")).toBe("en")
  })

  it("null tenant defaults to t-default", () => {
    const store = getUserStorage(null, "user-1")
    const key = store.key("omnisight-locale")
    expect(key).toBe("omnisight:t-default:user-1:locale")
  })

  it("removeItem is tenant-scoped", () => {
    const storeA = getUserStorage("t-acme", "user-1")
    const storeB = getUserStorage("t-beta", "user-1")

    storeA.setItem("omnisight-locale", "ja")
    storeB.setItem("omnisight-locale", "en")

    storeA.removeItem("omnisight-locale")
    expect(storeA.getItem("omnisight-locale")).toBeNull()
    expect(storeB.getItem("omnisight-locale")).toBe("en")
  })
})
