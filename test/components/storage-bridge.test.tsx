/**
 * Q.3-SUB-4 (#297) — StorageBridge SSE → localStorage fan-out.
 *
 * The bridge owns the cross-device preferences sync path on the
 * frontend. When ``preferences.updated`` arrives for the current
 * user it must:
 *   1. Write the value into the tenant+user prefixed localStorage
 *      slot — which auto-dispatches a native ``storage`` event to
 *      OTHER tabs in the same browser (J4 cross-tab compat).
 *   2. Notify in-tab ``onStorageChange`` listeners directly, since
 *      native ``storage`` does NOT fire in the originating tab.
 *
 * It must NOT:
 *   - Apply events whose ``user_id`` is not the current user
 *     (``broadcast_scope='user'`` is advisory until Q.4 #298;
 *     self-filter is the contract until then).
 *   - Re-write localStorage with the same value (idempotent skip).
 */

import React from "react"
import { describe, it, vi, expect, afterEach, beforeEach } from "vitest"
import { render, waitFor, act } from "@testing-library/react"

// Mock api wholesale so subscribeEvents is listener-capturing.
vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
}))

// Mock the context hooks so we can drive them per-test.
vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))
vi.mock("@/lib/tenant-context", () => ({
  useTenant: vi.fn(),
}))
vi.mock("@/lib/i18n/context", () => ({
  useI18n: vi.fn(),
}))

import { StorageBridge } from "@/components/storage-bridge"
import * as api from "@/lib/api"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { useI18n } from "@/lib/i18n/context"
import { onStorageChange } from "@/lib/storage"
import { primeSSE as _primeSSE } from "../helpers/sse"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedUseTenant = useTenant as unknown as ReturnType<typeof vi.fn>
const mockedUseI18n = useI18n as unknown as ReturnType<typeof vi.fn>

function primeAllSSE(): ReturnType<typeof _primeSSE> {
  return _primeSSE(api)
}

beforeEach(() => {
  localStorage.clear()
  mockedUseAuth.mockReturnValue({ user: { id: "user-xyz" } })
  mockedUseTenant.mockReturnValue({ currentTenantId: "t-default" })
  mockedUseI18n.mockReturnValue({
    locale: "en",
    setLocale: vi.fn(),
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe("StorageBridge — preferences.updated SSE dispatch", () => {
  it("writes the value into the tenant+user prefixed slot", async () => {
    const sse = primeAllSSE()
    render(<StorageBridge />)

    // Bridge subscribes on mount; wait for the registration.
    await waitFor(() => expect(sse.listeners.length).toBeGreaterThan(0))

    act(() => {
      sse.emit({
        event: "preferences.updated",
        data: {
          pref_key: "locale",
          value: "ja",
          user_id: "user-xyz",
          timestamp: "2026-04-24T00:00:05",
        },
      })
    })

    // The canonical key shape is omnisight:{tenant}:{user}:{key}.
    await waitFor(() => {
      expect(localStorage.getItem("omnisight:t-default:user-xyz:locale"))
        .toBe("ja")
    })
  })

  it("ignores events for other users (user-scope self-filter)", async () => {
    const sse = primeAllSSE()
    render(<StorageBridge />)
    await waitFor(() => expect(sse.listeners.length).toBeGreaterThan(0))

    // Use a pref_key the bridge's mount-time init doesn't touch
    // (``locale`` is auto-seeded from useI18n on every render).
    act(() => {
      sse.emit({
        event: "preferences.updated",
        data: {
          pref_key: "tour_seen",
          value: "1",
          user_id: "someone-else",
          timestamp: "2026-04-24T00:00:05",
        },
      })
    })

    // Give React a tick to flush any unintended write.
    await Promise.resolve()
    expect(localStorage.getItem("omnisight:t-default:user-xyz:tour_seen"))
      .toBeNull()
  })

  it("notifies in-tab onStorageChange listeners with the resolved key", async () => {
    const sse = primeAllSSE()
    const listener = vi.fn()
    const unsub = onStorageChange(listener)
    try {
      render(<StorageBridge />)
      await waitFor(() => expect(sse.listeners.length).toBeGreaterThan(0))

      act(() => {
        sse.emit({
          event: "preferences.updated",
          data: {
            pref_key: "tour_seen",
            value: "1",
            user_id: "user-xyz",
            timestamp: "2026-04-24T00:00:05",
          },
        })
      })

      await waitFor(() => {
        expect(listener).toHaveBeenCalledWith(
          "omnisight:t-default:user-xyz:tour_seen",
          "1",
        )
      })
    } finally {
      unsub()
    }
  })

  it("skips the write when the incoming value matches what's already stored", async () => {
    // Seed the slot so the bridge takes the idempotent skip branch.
    localStorage.setItem("omnisight:t-default:user-xyz:wizard_seen", "1")

    const sse = primeAllSSE()
    const listener = vi.fn()
    const unsub = onStorageChange(listener)
    try {
      render(<StorageBridge />)
      await waitFor(() => expect(sse.listeners.length).toBeGreaterThan(0))

      act(() => {
        sse.emit({
          event: "preferences.updated",
          data: {
            pref_key: "wizard_seen",
            value: "1",
            user_id: "user-xyz",
            timestamp: "2026-04-24T00:00:05",
          },
        })
      })

      // No fresh notify — same-value replay must be a no-op so in-tab
      // consumers don't re-render on bus echo-back.
      await Promise.resolve()
      expect(listener).not.toHaveBeenCalled()
      expect(localStorage.getItem("omnisight:t-default:user-xyz:wizard_seen"))
        .toBe("1")
    } finally {
      unsub()
    }
  })
})
